"""Refinement passes applied to a timeline, in order.

Every pass is a pure function of (timeline, context) that returns a new
timeline plus the decisions it made. Passes never mutate their input, so any
intermediate cut can be inspected or re-run.

    matchcut     find and tighten match cuts (feature search + model verification)
    continuity   remove jump cuts and flash frames, merge contiguous clips
    pacing       hit the beat/running-time targets with the intended rhythm
    transitions  dissolves / dips at beat changes, fades at the ends
    polish       the model reviews the storyboard and proposes final tweaks
    beatsync     move cuts onto the beats of the music track (no-op without music); runs last
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional, Sequence, Tuple

from . import prompts, schemas
from .context import Context
from .llm import LLMRequest
from .matching import MatchScore
from .sampling import chunked, pair_sheet, storyboard_sheet
from .timeline import Clip, Decision, PassResult, Timeline, Transition

log = logging.getLogger("matchcut.pipeline")


class Pass:
    name = "pass"

    def apply(self, timeline: Timeline, ctx: Context) -> PassResult:  # pragma: no cover - interface
        raise NotImplementedError

    def decision(self, action: str, clips: Sequence[str], detail: str, score: Optional[float] = None, **data) -> Decision:
        return Decision(self.name, action, list(clips), detail, score, dict(data))


def _beat_groups(tl: Timeline) -> List[List[int]]:
    """Indices of clips grouped by consecutive beat id."""
    groups: List[List[int]] = []
    for i, c in enumerate(tl.clips):
        if groups and tl.clips[groups[-1][-1]].beat_id == c.beat_id:
            groups[-1].append(i)
        else:
            groups.append([i])
    return groups


# ==========================================================================
# match cuts
# ==========================================================================
class MatchCutPass(Pass):
    name = "matchcut"

    def apply(self, timeline: Timeline, ctx: Context) -> PassResult:
        cfg = ctx.cfg.matchcut
        tl = timeline.copy()
        decisions: List[Decision] = []
        scorer = ctx.scorer

        if cfg.allow_reorder:
            decisions += self._reorder(tl, ctx)

        # ---- search better in/out points around every junction ------------
        candidates: List[Dict] = []
        min_clip = ctx.cfg.pacing.min_clip_seconds
        for i in range(1, len(tl.clips)):
            a, b = tl.clips[i - 1], tl.clips[i]
            base = scorer.score_cut(a.source_out, b.source_in)
            entry = dict(
                index=i,
                a=a.id,
                b=b.id,
                a_shot_index=a.shot_index,
                b_shot_index=b.shot_index,
                base=base,
                best=None,
                out=a.source_out,
                in_=b.source_in,
            )
            if base.jump_cut_risk:
                b.tags.append("jump_cut_risk")
                decisions.append(self.decision("jump_cut_risk", [a.id, b.id], "near-identical frames across the cut", base.combined))
            if a.locked_out or b.locked_in:
                continue  # already a verified match cut (or deliberately fixed): leave it alone
            out_lo = max(a.source_in + min_clip, a.source_out - cfg.trim_slack_seconds)
            in_hi = min(b.source_out - min_clip, b.source_in + cfg.trim_slack_seconds)
            if out_lo >= a.source_out and in_hi <= b.source_in:
                candidates.append(entry)
                continue
            out_t, in_t, best = scorer.best_junction((out_lo, a.source_out), (b.source_in, max(b.source_in, in_hi)))
            if best.combined > base.combined + 1e-6:
                entry.update(best=best, out=out_t, in_=in_t)
            candidates.append(entry)

        # ---- decide which junctions are match cuts -----------------------
        proposals = []
        for e in candidates:
            score: MatchScore = e["best"] or e["base"]
            if score.combined >= cfg.candidate_threshold and not score.jump_cut_risk:
                proposals.append(e)
        proposals.sort(key=lambda e: -(e["best"] or e["base"]).combined)
        proposals = proposals[: cfg.max_llm_pairs]

        verdicts: Dict[str, schemas.PairVerdict] = {}
        if proposals and cfg.verify_with_llm:
            verdicts = self._verify(proposals, ctx)

        for e in proposals:
            a, b = tl.clip_by_id(e["a"]), tl.clip_by_id(e["b"])
            score: MatchScore = e["best"] or e["base"]
            pair_id = f"{a.id}-{b.id}"
            verdict = verdicts.get(pair_id)
            if verdict is None:
                accept = score.combined >= cfg.strong_threshold
                kind = score.motion_type and ("motion" if score.motion_type == "moving" else "graphic")
                note = "accepted on feature score alone" if accept else "below strong threshold"
            else:
                accept = verdict.verdict == "match_cut"
                kind = verdict.match_type
                note = verdict.note
                if verdict.verdict == "jump_cut":
                    b.tags.append("jump_cut_risk")
                    decisions.append(self.decision("jump_cut_risk", [a.id, b.id], f"model: {note}", score.combined))
                    continue
            if not accept:
                decisions.append(self.decision("rejected", [a.id, b.id], note, score.combined, scores=score.to_dict()))
                continue
            moved = abs(e["out"] - a.source_out) > 1e-6 or abs(e["in_"] - b.source_in) > 1e-6
            a.source_out, b.source_in = round(float(e["out"]), 3), round(float(e["in_"]), 3)
            a.locked_out, b.locked_in = True, True
            a.tags.append(f"match_out:{kind}")
            b.tags.append(f"match_in:{kind}")
            decisions.append(
                self.decision(
                    "match_cut",
                    [a.id, b.id],
                    f"{kind} match cut{' (trimmed to best junction)' if moved else ''}: {note}",
                    score.combined,
                    out=a.source_out,
                    in_=b.source_in,
                    match_type=kind,
                    scores=score.to_dict(),
                    baseline=e["base"].combined,
                )
            )
        tl.stage = self.name
        tl.history.extend(decisions)
        return PassResult(tl, decisions)

    # ---- reorder within beats to create match-cut opportunities -----------
    def _reorder(self, tl: Timeline, ctx: Context) -> List[Decision]:
        cfg = ctx.cfg.matchcut
        scorer = ctx.scorer
        decisions: List[Decision] = []

        def gain(a: Clip, b: Clip) -> float:
            s = scorer.score_cut(a.source_out, b.source_in)
            if s.jump_cut_risk:
                return -0.5
            return max(0.0, s.combined - cfg.candidate_threshold)

        def total(order: List[Clip]) -> float:
            return sum(gain(order[k - 1], order[k]) for k in range(1, len(order)))

        new_clips: List[Clip] = []
        for group in _beat_groups(tl):
            clips = [tl.clips[i] for i in group]
            if len(clips) < 3 or any(c.locked_in or c.locked_out for c in clips):
                new_clips.extend(clips)
                continue
            best_order, best_val = clips, total(clips)
            improved = True
            while improved:  # greedy pairwise-swap hill climb, opening clip stays put
                improved = False
                for x in range(1, len(best_order)):
                    for y in range(x + 1, len(best_order)):
                        cand = best_order[:]
                        cand[x], cand[y] = cand[y], cand[x]
                        v = total(cand)
                        if v > best_val + 1e-9:
                            best_order, best_val, improved = cand, v, True
            if best_val - total(clips) >= cfg.reorder_min_gain:
                decisions.append(
                    self.decision(
                        "reorder",
                        [c.id for c in best_order],
                        f"beat {clips[0].beat_id}: reordered for match-cut potential (+{best_val - total(clips):.2f})",
                        best_val,
                    )
                )
                new_clips.extend(best_order)
            else:
                new_clips.extend(clips)
        tl.clips = new_clips
        return decisions

    # ---- model verification ------------------------------------------------
    def _verify(self, proposals: List[Dict], ctx: Context) -> Dict[str, schemas.PairVerdict]:
        cfg = ctx.cfg.sampling
        sheets_dir = ctx.workspace / "sheets"
        sheets_dir.mkdir(parents=True, exist_ok=True)
        verdicts: Dict[str, schemas.PairVerdict] = {}
        for batch_no, batch in enumerate(chunked(proposals, cfg.pairs_per_sheet)):
            pair_rows = []
            notes = []
            for e in batch:
                pair_id = f"{e['a']}-{e['b']}"
                score: MatchScore = e["best"] or e["base"]
                last_frame = max(float(e["out"]) - 1.0 / ctx.media.fps, 0.0)  # out points are exclusive
                pair_rows.append((pair_id, last_frame, float(e["in_"]), int(e["a_shot_index"]), int(e["b_shot_index"])))
                notes.append(
                    dict(
                        pair_id=pair_id,
                        frame=round(score.frame, 3),
                        motion=round(score.motion, 3),
                        combined=round(score.combined, 3),
                        motion_type=score.motion_type,
                        jump_cut_risk=score.jump_cut_risk,
                    )
                )
            img = pair_sheet(ctx.frames, pair_rows, cfg)
            img.save(sheets_dir / f"verify_{batch_no:03d}.jpg", quality=cfg.jpeg_quality)
            text = prompts.VERIFY_PAIRS.format(
                pair_notes=json.dumps(notes, indent=0),
                pair_ids=[n["pair_id"] for n in notes],
            )
            response = ctx.llm.structured(
                LLMRequest(
                    name="verify_pairs",
                    system=prompts.EDITOR_SYSTEM,
                    text=text,
                    images=[img],
                    schema=schemas.VerifyResponse,
                    payload={"pairs": notes, "threshold": ctx.cfg.matchcut.candidate_threshold},
                )
            )
            assert isinstance(response, schemas.VerifyResponse)
            for v in response.pairs:
                verdicts[v.pair_id] = v
        return verdicts


# ==========================================================================
# continuity
# ==========================================================================
class ContinuityPass(Pass):
    name = "continuity"

    def apply(self, timeline: Timeline, ctx: Context) -> PassResult:
        tl = timeline.copy()
        decisions: List[Decision] = []
        min_clip = ctx.cfg.pacing.min_clip_seconds
        dissolve = ctx.cfg.transitions.dissolve_seconds

        # 1. merge contiguous pieces of the same shot
        merged: List[Clip] = []
        for c in tl.clips:
            prev = merged[-1] if merged else None
            if prev is not None and prev.shot_index == c.shot_index and abs(prev.source_out - c.source_in) < 0.05 and not prev.locked_out:
                decisions.append(self.decision("merge", [prev.id, c.id], "contiguous pieces of the same shot merged"))
                prev.source_out = c.source_out
                prev.locked_out = c.locked_out
                prev.tags = [t for t in prev.tags if not t.startswith("match_out")] + [t for t in c.tags if t.startswith("match_out")]
                continue
            merged.append(c)
        tl.clips = merged

        # 2. drop flash frames (unless they are match-cut anchors)
        kept: List[Clip] = []
        for c in tl.clips:
            if c.duration < min_clip and not (c.locked_in or c.locked_out):
                decisions.append(self.decision("drop", [c.id], f"flash frame {c.duration:.2f}s"))
                continue
            kept.append(c)
        tl.clips = kept

        # 3. jump cuts: same shot back to back, or flagged by the match pass
        i = 1
        while i < len(tl.clips):
            a, b = tl.clips[i - 1], tl.clips[i]
            risky = a.shot_index == b.shot_index or "jump_cut_risk" in b.tags
            if not risky or b.locked_in:
                i += 1
                continue
            sep = self._find_separator(tl, i)
            if sep is not None:
                clip = tl.clips.pop(sep)
                tl.clips.insert(i, clip)
                decisions.append(self.decision("insert", [a.id, clip.id, b.id], f"moved {clip.id} between {a.id} and {b.id} to break a jump cut"))
                i += 2
                continue
            if b.transition_in.is_cut:
                d = min(dissolve, a.duration * 0.4, b.duration * 0.4)
                b.transition_in = Transition("dissolve", round(d, 3))
                decisions.append(self.decision("dissolve", [a.id, b.id], f"softened jump cut with {d:.2f}s dissolve"))
            i += 1
        for c in tl.clips:
            c.tags = [t for t in c.tags if t != "jump_cut_risk"]
        tl.renumber()
        tl.stage = self.name
        tl.history.extend(decisions)
        return PassResult(tl, decisions)

    @staticmethod
    def _find_separator(tl: Timeline, i: int) -> Optional[int]:
        """Index of a later clip in the same beat that can be moved between i-1 and i."""
        a, b = tl.clips[i - 1], tl.clips[i]
        for j in range(i + 1, len(tl.clips)):
            c = tl.clips[j]
            if c.beat_id != b.beat_id:
                break
            prev_locked = tl.clips[j - 1].locked_out or c.locked_in
            next_locked = j + 1 < len(tl.clips) and (c.locked_out or tl.clips[j + 1].locked_in)
            if c.shot_index not in (a.shot_index, b.shot_index) and not prev_locked and not next_locked and not c.locked_in and not c.locked_out:
                return j
        return None


# ==========================================================================
# pacing
# ==========================================================================
class PacingPass(Pass):
    name = "pacing"

    def apply(self, timeline: Timeline, ctx: Context) -> PassResult:
        cfg = ctx.cfg.pacing
        tl = timeline.copy()
        decisions: List[Decision] = []
        beats = {b["id"]: b for b in ctx.narrative.get("beats", [])}

        for group in _beat_groups(tl):
            clips = [tl.clips[i] for i in group]
            beat = beats.get(clips[0].beat_id, {})
            pace = beat.get("pace", "medium")
            avg_target = cfg.pace_targets.get(pace, cfg.pace_targets["medium"])
            target_total = float(beat.get("target_duration_s") or sum(c.duration for c in clips))

            # rhythm: no clip should wildly exceed the beat's typical length
            for c in clips:
                limit = min(cfg.max_clip_seconds, avg_target * 1.6)
                if c.duration > limit + 0.05:
                    before = c.duration
                    self._shorten(c, c.duration - limit, ctx)
                    if c.duration < before - 1e-6:
                        decisions.append(self.decision("trim", [c.id], f"{pace} beat rhythm: {before:.2f}s -> {c.duration:.2f}s"))

            decisions += self._fit(clips, target_total, cfg.duration_tolerance, ctx, label=f"beat {clips[0].beat_id}")

        # drop clips that got removed in _fit (they are tagged) and renumber
        tl.clips = [c for c in tl.clips if "dropped" not in c.tags]

        if ctx.cfg.target_duration:
            decisions += self._fit(tl.clips, float(ctx.cfg.target_duration), cfg.duration_tolerance, ctx, label="total")
            tl.clips = [c for c in tl.clips if "dropped" not in c.tags]

        tl.renumber()
        tl.stage = self.name
        tl.history.extend(decisions)
        return PassResult(tl, decisions)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _shorten(c: Clip, amount: float, ctx: Context) -> float:
        """Remove ``amount`` seconds from the unlocked end(s), keeping the interest peak. Returns removed."""
        min_clip = ctx.cfg.pacing.min_clip_seconds
        amount = min(amount, max(0.0, c.duration - min_clip))
        if amount <= 0:
            return 0.0
        peak = ctx.shot_stats(ctx.shot(c.shot_index))["interest_peak"]
        head_room = 0.0 if c.locked_in else max(0.0, peak - c.source_in - min_clip / 2)
        tail_room = 0.0 if c.locked_out else max(0.0, c.source_out - peak - min_clip / 2)
        if c.locked_in and c.locked_out:
            return 0.0
        if c.locked_in:
            take_tail, take_head = amount, 0.0
        elif c.locked_out:
            take_head, take_tail = amount, 0.0
        else:
            room = head_room + tail_room
            take_head = amount * (head_room / room) if room > 0 else amount / 2
            take_tail = amount - take_head
        take_head = min(take_head, c.duration - min_clip)
        c.source_in = round(c.source_in + take_head, 3)
        take_tail = min(take_tail, c.duration - min_clip)
        c.source_out = round(c.source_out - take_tail, 3)
        return take_head + take_tail

    @staticmethod
    def _extend(c: Clip, amount: float, ctx: Context) -> float:
        max_clip = ctx.cfg.pacing.max_clip_seconds
        s, e = ctx.shot_bounds[c.shot_index]
        amount = min(amount, max(0.0, max_clip - c.duration))
        if amount <= 0:
            return 0.0
        added = 0.0
        if not c.locked_out:
            room = max(0.0, e - c.source_out)
            d = min(room, amount)
            c.source_out = round(c.source_out + d, 3)
            added += d
        if not c.locked_in and added < amount:
            room = max(0.0, c.source_in - s)
            d = min(room, amount - added)
            c.source_in = round(c.source_in - d, 3)
            added += d
        return added

    def _fit(self, clips: List[Clip], target: float, tol: float, ctx: Context, label: str) -> List[Decision]:
        decisions: List[Decision] = []
        live = [c for c in clips if "dropped" not in c.tags]
        if not live or target <= 0:
            return decisions
        total = sum(c.duration for c in live)
        if total > target * (1 + tol):
            excess = total - target
            # proportional trim of unlocked clips first
            for _ in range(3):
                flexible = [c for c in live if not (c.locked_in and c.locked_out) and c.duration > ctx.cfg.pacing.min_clip_seconds]
                if not flexible or excess <= 0.05:
                    break
                weight = sum(c.duration for c in flexible)
                for c in flexible:
                    removed = self._shorten(c, excess * c.duration / weight, ctx)
                    excess -= removed
            # still too long: drop the least valuable clips
            while excess > target * tol and len(live) > 1:
                victim = min(live, key=lambda c: self._value(c, ctx))
                victim.tags.append("dropped")
                live.remove(victim)
                excess -= victim.duration
                decisions.append(self.decision("drop", [victim.id], f"{label}: over target, dropped lowest-value clip"))
            decisions.append(self.decision("fit", [c.id for c in live], f"{label}: {total:.1f}s -> {sum(c.duration for c in live):.1f}s (target {target:.1f}s)"))
        elif total < target * (1 - tol):
            deficit = target - total
            flexible = [c for c in live if not (c.locked_in and c.locked_out)]
            for c in sorted(flexible, key=lambda c: -self._value(c, ctx)):
                if deficit <= 0.05:
                    break
                deficit -= self._extend(c, deficit / max(1, len(flexible)) * 1.5, ctx)
            decisions.append(self.decision("fit", [c.id for c in live], f"{label}: {total:.1f}s -> {sum(c.duration for c in live):.1f}s (target {target:.1f}s)"))
        return decisions

    @staticmethod
    def _value(c: Clip, ctx: Context) -> float:
        desc = ctx.descriptions.get(c.shot_index, {})
        v = float(desc.get("quality", 0.5))
        if c.locked_in or c.locked_out:
            v += 1.0
        if not desc.get("usable", True):
            v -= 1.0
        return v


# ==========================================================================
# transitions
# ==========================================================================
class TransitionsPass(Pass):
    name = "transitions"

    def apply(self, timeline: Timeline, ctx: Context) -> PassResult:
        cfg = ctx.cfg.transitions
        tl = timeline.copy()
        decisions: List[Decision] = []
        beats = {b["id"]: b for b in ctx.narrative.get("beats", [])}
        durations = {"dissolve": cfg.dissolve_seconds, "dip_to_black": cfg.dip_to_black_seconds, "cut": 0.0}

        for i in range(1, len(tl.clips)):
            a, b = tl.clips[i - 1], tl.clips[i]
            beat_change = a.beat_id != b.beat_id
            if not beat_change:
                continue
            if a.locked_out and b.locked_in:
                decisions.append(self.decision("keep_cut", [a.id, b.id], "beat change lands on a match cut: hard cut kept"))
                continue
            kind = beats.get(b.beat_id, {}).get("transition_in", "cut")
            d = min(durations.get(kind, 0.0), a.duration * cfg.max_fraction_of_clip, b.duration * cfg.max_fraction_of_clip)
            if kind == "cut" or d <= 0.05:
                b.transition_in = Transition("cut", 0.0)
                continue
            b.transition_in = Transition(kind, round(d, 3))
            decisions.append(self.decision(kind, [a.id, b.id], f"beat {a.beat_id} -> {b.beat_id}: {kind} {d:.2f}s"))

        if tl.clips:
            tl.fade_in = round(min(cfg.fade_in_seconds, tl.clips[0].duration * cfg.max_fraction_of_clip), 3)
            tl.fade_out = round(min(cfg.fade_out_seconds, tl.clips[-1].duration * cfg.max_fraction_of_clip), 3)
            decisions.append(self.decision("fades", [tl.clips[0].id, tl.clips[-1].id], f"fade in {tl.fade_in:.2f}s, fade out {tl.fade_out:.2f}s"))
        tl.stage = self.name
        tl.history.extend(decisions)
        return PassResult(tl, decisions)


# ==========================================================================
# polish (model review)
# ==========================================================================
class PolishPass(Pass):
    name = "polish"

    def apply(self, timeline: Timeline, ctx: Context) -> PassResult:
        cfg = ctx.cfg.polish
        scfg = ctx.cfg.sampling
        tl = timeline.copy()
        decisions: List[Decision] = []
        sheets_dir = ctx.workspace / "sheets"
        sheets_dir.mkdir(parents=True, exist_ok=True)

        rows = [(c.id, c.shot_index, c.source_in, c.duration, c.beat_id) for c in tl.clips]
        per_sheet = scfg.storyboard_columns * 6
        images = []
        for k, chunk in enumerate(chunked(rows, per_sheet)):
            img = storyboard_sheet(ctx.frames, chunk, scfg)
            img.save(sheets_dir / f"storyboard_{k:03d}.jpg", quality=scfg.jpeg_quality)
            images.append(img)
        images = images[: cfg.storyboards_per_request]

        cut_list = [
            dict(
                clip_id=c.id,
                shot_id=c.shot_index,
                beat_id=c.beat_id,
                in_s=c.source_in,
                out_s=c.source_out,
                duration=round(c.duration, 2),
                record_in=round(tl.record_start(i), 2),
                transition_in=c.transition_in.kind,
                locked=[k for k, v in (("in", c.locked_in), ("out", c.locked_out)) if v],
                tags=c.tags,
                purpose=c.purpose,
                subject=ctx.descriptions.get(c.shot_index, {}).get("subject", ""),
            )
            for i, c in enumerate(tl.clips)
        ]
        text = prompts.POLISH.format(
            brief=ctx.cfg.brief or "(none)",
            target=ctx.target_text(),
            notes=ctx.notes_text("pass:polish"),
            narrative=ctx.narrative_text(),
            cut_list=json.dumps(cut_list, indent=0),
            max_edits=cfg.max_edits,
        )
        response = ctx.llm.structured(
            LLMRequest(
                name="polish",
                system=prompts.EDITOR_SYSTEM,
                text=text,
                images=images,
                schema=schemas.PolishResponse,
                payload={"cut_list": cut_list},
            )
        )
        assert isinstance(response, schemas.PolishResponse)
        decisions.append(self.decision("assessment", [c.id for c in tl.clips], response.assessment))
        for edit in response.edits[: cfg.max_edits]:
            ok, why = self._apply_edit(tl, edit, ctx)
            decisions.append(self.decision(edit.op if ok else "rejected", [edit.clip_id], f"{edit.reason} -> {why}"))
        tl.renumber()
        tl.stage = self.name
        tl.history.extend(decisions)
        return PassResult(tl, decisions)

    def _apply_edit(self, tl: Timeline, edit: schemas.PolishEdit, ctx: Context) -> Tuple[bool, str]:
        pacing = ctx.cfg.pacing
        try:
            c = tl.clip_by_id(edit.clip_id)
        except KeyError:
            return False, f"unknown clip {edit.clip_id}"
        s, e = ctx.shot_bounds[c.shot_index]
        amt = abs(float(edit.seconds))
        if edit.op == "keep":
            return True, "kept"
        if edit.op in ("trim_head", "extend_head") and c.locked_in:
            return False, "in-point locked by a match cut"
        if edit.op in ("trim_tail", "extend_tail") and c.locked_out:
            return False, "out-point locked by a match cut"
        if edit.op == "trim_head":
            new_in = min(c.source_in + amt, c.source_out - pacing.min_clip_seconds)
            if new_in <= c.source_in:
                return False, "clip already at minimum length"
            c.source_in = round(new_in, 3)
            return True, f"in {c.source_in:.2f}s"
        if edit.op == "trim_tail":
            new_out = max(c.source_out - amt, c.source_in + pacing.min_clip_seconds)
            if new_out >= c.source_out:
                return False, "clip already at minimum length"
            c.source_out = round(new_out, 3)
            return True, f"out {c.source_out:.2f}s"
        if edit.op == "extend_head":
            new_in = max(s, c.source_in - amt, c.source_out - pacing.max_clip_seconds)
            if new_in >= c.source_in:
                return False, "no room to extend head"
            c.source_in = round(new_in, 3)
            return True, f"in {c.source_in:.2f}s"
        if edit.op == "extend_tail":
            new_out = min(e, c.source_out + amt, c.source_in + pacing.max_clip_seconds)
            if new_out <= c.source_out:
                return False, "no room to extend tail"
            c.source_out = round(new_out, 3)
            return True, f"out {c.source_out:.2f}s"
        if edit.op == "drop":
            if len(tl.clips) <= 1:
                return False, "cannot drop the only clip"
            if c.locked_in or c.locked_out:
                return False, "clip anchors a match cut"
            tl.clips.remove(c)
            return True, "dropped"
        if edit.op == "move_before":
            try:
                target = tl.clip_by_id(edit.target_clip_id)
            except KeyError:
                return False, f"unknown target {edit.target_clip_id}"
            if target is c:
                return False, "cannot move a clip before itself"
            i = tl.index_of(c.id)
            neighbours = [tl.clips[k] for k in (i - 1, i + 1) if 0 <= k < len(tl.clips)]
            if c.locked_in or c.locked_out or any(n.locked_in or n.locked_out for n in neighbours) or target.locked_in:
                return False, "move would break a match cut"
            tl.clips.remove(c)
            tl.clips.insert(tl.index_of(target.id), c)
            return True, f"moved before {target.id}"
        if edit.op == "set_transition":
            i = tl.index_of(c.id)
            if i == 0:
                return False, "first clip has no incoming transition"
            prev = tl.clips[i - 1]
            if (prev.locked_out and c.locked_in) and edit.transition != "cut":
                return False, "match cuts stay hard cuts"
            tcfg = ctx.cfg.transitions
            d = {"dissolve": tcfg.dissolve_seconds, "dip_to_black": tcfg.dip_to_black_seconds, "cut": 0.0}[edit.transition]
            d = min(d, prev.duration * tcfg.max_fraction_of_clip, c.duration * tcfg.max_fraction_of_clip)
            c.transition_in = Transition(edit.transition if d > 0.05 else "cut", round(d, 3) if d > 0.05 else 0.0)
            return True, f"{c.transition_in.kind} {c.transition_in.duration:.2f}s"
        return False, f"unknown op {edit.op}"


# ==========================================================================
# beat sync
# ==========================================================================
class BeatSyncPass(Pass):
    """Move every cut onto the nearest beat of the music track.

    The cut between clips i-1 and i happens at ``record_start(i)`` on the
    timeline (for a dissolve: where the incoming clip starts fading in), i.e.
    ``record_start(i) + music.start`` seconds into the track. Only the outgoing
    clip can move it: its out-point when unlocked, else its in-point (a shorter
    head lands the junction earlier). Locked ends stay. The pass runs last so
    nothing moves a cut after it is on the beat.
    """

    name = "beatsync"

    def apply(self, timeline: Timeline, ctx: Context) -> PassResult:
        tl = timeline.copy()
        decisions: List[Decision] = []
        music = ctx.music
        mcfg = ctx.cfg.music
        if music is None or not mcfg.snap:
            tl.stage = self.name
            return PassResult(tl, decisions)

        tl = tl.snap_to_frames(ctx.shot_bounds)
        fps = tl.fps
        for i in range(1, len(tl.clips)):
            prev, cur = tl.clips[i - 1], tl.clips[i]
            track_t = tl.record_start(i) + mcfg.start
            beat_change = prev.beat_id != cur.beat_id
            target: Optional[float] = None
            downbeat = False
            if beat_change and mcfg.prefer_downbeats_at_beat_changes:
                db = music.nearest_beat(track_t, downbeats=True)
                if db is not None and abs(db - track_t) <= mcfg.snap_window:
                    target, downbeat = db, True
            if target is None:
                b = music.nearest_beat(track_t)
                if b is not None and abs(b - track_t) <= mcfg.snap_window:
                    target = b
            if target is None:
                decisions.append(self.decision("skip", [prev.id, cur.id], f"no beat within {mcfg.snap_window:.2f}s of the cut"))
                continue
            delta = target - track_t
            frames = int(round(delta * fps))
            if frames == 0:
                decisions.append(self.decision("on_beat", [prev.id, cur.id], "cut already on the beat (within half a frame)", 0.0, beat=round(float(target), 4), downbeat=downbeat))
                continue
            delta = frames / fps
            if self._shift_junction(prev, delta, ctx):
                decisions.append(
                    self.decision(
                        "snap",
                        [prev.id, cur.id],
                        f"cut moved {delta:+.3f}s onto a {'downbeat' if downbeat else 'beat'} at {target:.2f}s of the track",
                        abs(delta),
                        delta=round(delta, 4),
                        beat=round(float(target), 4),
                        downbeat=downbeat,
                    )
                )
            else:
                decisions.append(self.decision("locked", [prev.id, cur.id], "both ends of the outgoing clip are locked or out of room"))
        tl.stage = self.name
        tl.history.extend(decisions)
        return PassResult(tl, decisions)

    @staticmethod
    def _shift_junction(clip: Clip, delta: float, ctx: Context) -> bool:
        """Move the junction after ``clip`` by ``delta`` seconds via one of its unlocked ends."""
        s, e = ctx.shot_bounds[clip.shot_index]
        pacing = ctx.cfg.pacing
        if not clip.locked_out:
            new_out = clip.source_out + delta
            if s <= new_out <= e and pacing.min_clip_seconds <= new_out - clip.source_in <= pacing.max_clip_seconds:
                clip.source_out = round(new_out, 4)
                return True
        if not clip.locked_in:
            new_in = clip.source_in - delta
            if s <= new_in <= e and pacing.min_clip_seconds <= clip.source_out - new_in <= pacing.max_clip_seconds:
                clip.source_in = round(new_in, 4)
                return True
        return False


PASSES: Dict[str, type] = {
    "matchcut": MatchCutPass,
    "continuity": ContinuityPass,
    "pacing": PacingPass,
    "transitions": TransitionsPass,
    "polish": PolishPass,
    "beatsync": BeatSyncPass,
}


def run_passes(timeline: Timeline, ctx: Context, names: Sequence[str], save_dir: Optional = None) -> Timeline:
    tl = timeline
    for n, name in enumerate(names, start=1):
        if name not in PASSES:
            raise ValueError(f"unknown pass {name!r}; available: {', '.join(PASSES)}")
        log.info("pass %d/%d: %s", n, len(names), name)
        result = PASSES[name]().apply(tl, ctx)
        tl = result.timeline
        problems = tl.validate(ctx.shot_bounds, min_clip=0.1)
        if problems:
            raise RuntimeError(f"pass {name} produced an invalid timeline: {problems[:5]}")
        if save_dir is not None:
            tl.name = f"after {name}"
            tl.save(save_dir / f"{n:02d}_{name}.json")
        log.info("  %d decisions, %d clips, %.1fs", len(result.decisions), len(tl.clips), tl.duration)
    return tl
