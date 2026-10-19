"""Junctions: the cut between two consecutive clips, followed through every stage of a cut.

A junction is identified by the two clips of the reference stage. Passes drop,
merge, insert and renumber clips, so earlier stages are matched by lineage (same
source shot, overlapping source range), not by id, and a pass's decisions are
attributed through the ids that were valid when the pass recorded them.
Scores come from the same MatchScorer the match-cut pass uses, rebuilt from the
workspace's cached analysis, so the numbers shown are the numbers the pass compared.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Set, Tuple

from .config import PipelineConfig
from .features import Analysis
from .matching import MatchScore, MatchScorer
from .editorial import Pin, pin_change
from .timeline import HUMAN_STAGE, Clip, Decision, Timeline, clip_origin

# decisions about a beat or the whole cut, never about one junction, whatever clip ids they list
WHOLE_SCOPE_ACTIONS: Tuple[str, ...] = ("fit", "assessment", "reorder", "fades", "notes")
STALE_HINT = "(re-export, or re-import the editor's file: matchcut diffs it against the stage it was exported from)"
SCORE_ROWS: Tuple[str, ...] = ("structure", "composition", "luminance", "color", "frame", "motion", "combined")


def _canonical_paths(ws: Path) -> List[Tuple[str, Path]]:
    entries = [("roughcut", ws / "roughcut.json")]
    entries += [(p.stem.split("_", 1)[-1], p) for p in sorted((ws / "passes").glob("*.json"))]
    entries.append(("final", ws / "final.json"))
    return entries


def _sha256(path: Optional[Path]) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path is not None else None
    except OSError:
        return None


def stale_human(ws: Path) -> Optional[str]:
    """The one-line reason ``human.json`` (the stage an editor's re-export was imported as) no longer fits the workspace, or None.

    ``matchcut.interchange`` records, in ``human_import.json``, the sha256 of the canonical stage it compared with and of the latest
    canonical stage whose history it continues; the human stage is stale when either file has changed since (or is gone).  A
    ``human.json`` without a readable record is stale too.  No human.json: None.  The engine only reads these files.
    """
    ws = Path(ws)
    if not (ws / "human.json").exists():
        return None
    try:
        record = json.loads((ws / "human_import.json").read_text())
        checks = [(str(record["stage"]), record["stage_sha256"])]
        if "latest_stage" in record:
            checks.append((str(record["latest_stage"]), record["latest_stage_sha256"]))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return f"human.json is stale: human_import.json is missing or unreadable {STALE_HINT}"
    paths = dict(_canonical_paths(ws))
    for stage, want in checks:
        path = paths.get(stage)
        if path is None or _sha256(path) != want:
            return f"human.json is stale: {path.name if path is not None else stage + '.json'} changed since the import {STALE_HINT}"
    return None


def stage_timeline_paths(ws: Path) -> List[Tuple[str, Path]]:
    """(stage, path) for every place a stage timeline can live, in stage order, present or not.

    ``human`` is last: the cut an editor's re-export came back as (``matchcut.interchange`` writes ``human.json``; the engine never does).
    A stale ``human.json`` (``stale_human``) is left out, so it is never the latest stage; it is not deleted.
    """
    entries = _canonical_paths(ws)
    if stale_human(ws) is None:
        entries.append((HUMAN_STAGE, ws / "human.json"))
    return entries


def load_stage_timelines(ws: Path) -> List[Tuple[str, Timeline]]:
    stages: List[Tuple[str, Timeline]] = []
    for stage, path in stage_timeline_paths(ws):
        if not path.exists():
            continue
        try:
            stages.append((stage, Timeline.load(path)))
        except Exception:  # a half-written or hand-edited file: leave that stage out
            continue
    return stages


def scorer_for_workspace(ws: Path, cfg: PipelineConfig) -> Optional[MatchScorer]:
    path = ws / "analysis.npz"
    if not path.exists():
        return None
    try:
        return MatchScorer(Analysis.load(path).cells, cfg.matchcut)
    except Exception:
        return None


def signed(value: float, places: int = 2) -> str:
    """``value`` with an explicit sign, never ``-0.00``: a value that rounds to zero prints as ``+0.00``."""
    v = round(value, places)
    if v == 0:
        v = 0.0  # drops the sign of -0.0
    return f"{v:+.{places}f}"


def frames_delta(seconds: float, fps: float) -> str:
    frames = int(round(seconds * fps)) if fps > 0 else 0
    return f"{signed(seconds)}s ({frames:+d}f)"


def frame_delta(t0: float, t1: float, fps: float) -> str:
    """The move from ``t0`` to ``t1`` in seconds and in whole frames on the frame grid (round(t1*fps) - round(t0*fps))."""
    frames = int(round(t1 * fps)) - int(round(t0 * fps)) if fps > 0 else 0
    return f"{signed(t1 - t0)}s ({frames:+d}f)"


def _noise(fps: float) -> float:
    """Differences below this are snapping to the frame grid, not an edit: half a frame (0.02 s when the rate is unknown)."""
    return 0.5 / fps if fps > 0 else 0.02


@dataclass
class JunctionState:
    stage: str
    out_time: float
    in_time: float
    transition: str = "cut"
    transition_seconds: float = 0.0
    locked_out: bool = False
    locked_in: bool = False
    score: Optional[MatchScore] = None

    @property
    def locked(self) -> bool:
        return self.locked_out and self.locked_in

    def change_from(self, prev: "JunctionState", fps: float, lock_word: str = "locked") -> str:
        parts: List[str] = []
        floor = _noise(fps) - 1e-6
        if abs(self.out_time - prev.out_time) >= floor:
            parts.append(f"out {frame_delta(prev.out_time, self.out_time, fps)}")
        if abs(self.in_time - prev.in_time) >= floor:
            parts.append(f"in {frame_delta(prev.in_time, self.in_time, fps)}")
        if (self.transition, round(self.transition_seconds, 3)) != (prev.transition, round(prev.transition_seconds, 3)):
            parts.append(self.transition if self.transition == "cut" else f"{self.transition} {self.transition_seconds:.2f}s")
        if self.locked and not prev.locked:
            parts.append(lock_word)
        return ", ".join(parts)


@dataclass
class Junction:
    index: int  # junction j sits between clip j and clip j+1 of the stage it was built from
    out_clip: str
    in_clip: str
    out_shot: int
    in_shot: int
    out_beat: str
    in_beat: str
    fps: float
    states: List[JunctionState] = field(default_factory=list)  # stage order; only stages where the clips are adjacent
    decisions: List[Decision] = field(default_factory=list)  # stage order: decisions that name this junction or one of its clips
    clip_level: List[bool] = field(default_factory=list)  # parallel to decisions: True when it names only one of the two clips (a trim, a drop)
    decision_stage: List[str] = field(default_factory=list)  # parallel to decisions: the stage whose timeline recorded it
    pin_lost: str = ""  # human stage only: why a pin the inherited decisions hold at this junction no longer holds in the editor's cut (one line)
    first_stage: str = ""  # the first stage this junction was built from; a later baseline means a pass made the two clips adjacent
    adjacent_detail: str = ""  # the drop/insert that made them adjacent, when the baseline stage is not the first

    @property
    def beat_boundary(self) -> bool:
        return self.out_beat != self.in_beat

    @property
    def baseline(self) -> JunctionState:
        return self.states[0]

    @property
    def current(self) -> JunctionState:
        return self.states[-1]

    def state_at(self, stage: str) -> Optional[JunctionState]:
        return next((s for s in self.states if s.stage == stage), None)

    def latest(self, *actions: str) -> Optional[Decision]:
        for d in reversed(self.decisions):
            if not actions or d.action in actions:
                return d
        return None

    @property
    def match_type(self) -> str:
        d = self.latest("match_cut")
        return str(d.data.get("match_type") or "") if d is not None else ""

    @property
    def pinned(self) -> bool:
        """The editor's pin holds this junction in this stage.  On the human stage the inherited decision is not enough: ``pin_lost`` says the
        editor's cut no longer holds it."""
        return not self.pin_lost and any(d.pass_name == "editor" and d.action == "pinned" for d in self.decisions)

    @property
    def pin_origin(self) -> Optional[str]:
        """How the editor pinned this junction ("locked" or "reverted"), from the latest pinned decision; None without one."""
        for d in reversed(self.decisions):
            if d.pass_name == "editor" and d.action == "pinned":
                origin = d.data.get("origin")
                return str(origin) if origin else "locked"
        return None

    def _is_clip_level(self, i: int) -> bool:
        return i < len(self.clip_level) and self.clip_level[i]

    def why(self) -> str:
        """The decision that best explains this junction: editor, then the match-cut verdicts, then pass decisions about the junction, then about one of its clips."""
        ranked = list(enumerate(self.decisions))[::-1]
        joined = bool(self.first_stage) and self.baseline.stage != self.first_stage  # a stage made the two clips adjacent
        tiers = (
            lambda i, d: d.pass_name == "editor",
            lambda i, d: d.action in ("match_cut", "rejected", "jump_cut_risk") and not self._is_clip_level(i),
            lambda i, d: not self._is_clip_level(i),
            # a trim of one clip, taken in the stage that joined the two, does not say why they are joined: the drop or removal does
            lambda i, d: not (joined and self._is_clip_level(i) and self._decided_in(i) == self.baseline.stage),
        )
        for tier in tiers:
            d = next((d for i, d in ranked if tier(i, d)), None)
            if d is not None:
                return f"{d.pass_name}: {d.detail}"
        moved = self._last_move()
        if moved is not None:
            return f"{moved[0]} moved this junction: {moved[1]}"
        if self.first_stage and self.baseline.stage != self.first_stage:
            return f"made adjacent by {self.baseline.stage}" + (f": {self.adjacent_detail}" if self.adjacent_detail else "")
        return "no pass changed this junction"

    def _decided_in(self, i: int) -> str:
        return self.decision_stage[i] if i < len(self.decision_stage) else ""

    def _changes(self) -> List[Tuple[str, str]]:
        """(stage, change) for every stage after the baseline that moved something, in stage order."""
        out: List[Tuple[str, str]] = []
        origin = self.pin_origin or "locked"
        for prev, cur in zip(self.states, self.states[1:]):
            change = cur.change_from(prev, self.fps, lock_word=origin if self._pinned_in(cur.stage) else "locked")
            if change:
                out.append((cur.stage, change))
        return out

    def _pinned_in(self, stage: str) -> bool:
        return any(d.pass_name == "editor" and d.action == "pinned" and k < len(self.decision_stage) and self.decision_stage[k] == stage for k, d in enumerate(self.decisions))

    def _last_move(self) -> Optional[Tuple[str, str]]:
        changes = self._changes()
        return changes[-1] if changes else None

    def history(self) -> List[str]:
        b = self.baseline
        lines = [f"{b.stage}: {b.transition} at out {b.out_time:.2f}s / in {b.in_time:.2f}s"]
        # Pins are applied to the rough cut before the first pass runs, so the pass that saved first is not who moved it.
        lines += [f"{'editor (before ' + stage + ')' if self._pinned_in(stage) else stage}: {change}" for stage, change in self._changes()]
        return lines


def _overlap(x: Clip, r: Clip) -> float:
    """Seconds of source footage two clips share, 0 when they come from different shots."""
    if x.shot_index != r.shot_index:
        return 0.0
    return max(0.0, min(x.source_out, r.source_out) - max(x.source_in, r.source_in))


def _lineage(x: Clip, r: Clip) -> bool:
    return x.shot_index == r.shot_index and max(x.source_in, r.source_in) < min(x.source_out, r.source_out)


def _locate(timeline: Timeline, a_ref: Clip, b_ref: Clip) -> Optional[int]:
    """Index i where clip i descends from ``a_ref`` and clip i+1 from ``b_ref``; the best-overlapping pair if several."""
    best: Optional[int] = None
    best_overlap = -1.0
    for i in range(len(timeline.clips) - 1):
        a, b = timeline.clips[i], timeline.clips[i + 1]
        if _lineage(a, a_ref) and _lineage(b, b_ref):
            overlap = _overlap(a, a_ref) + _overlap(b, b_ref)
            if overlap > best_overlap:
                best, best_overlap = i, overlap
    return best


def locate_junction(timeline: Timeline, a_ref: Clip, b_ref: Clip) -> Optional[int]:
    """Index i where clip i descends from ``a_ref`` and clip i+1 from ``b_ref`` (same shot, overlapping source), or None."""
    return _locate(timeline, a_ref, b_ref)


def _best_clip(timeline: Timeline, ref: Clip) -> Optional[int]:
    best: Optional[int] = None
    best_overlap = 0.0
    for i, c in enumerate(timeline.clips):
        if _lineage(c, ref) and _overlap(c, ref) > best_overlap:
            best, best_overlap = i, _overlap(c, ref)
    return best


def _adjacency_detail(considered: Sequence[Tuple[str, Timeline]], k: int, a_ref: Clip, b_ref: Clip, cur_ids: Tuple[str, str]) -> str:
    """The drop or insert of stage ``k`` that made the junction's clips adjacent: it names a clip that sat between them, or one of them."""
    prev, timeline = considered[k - 1][1], considered[k][1]
    pa, pb = _best_clip(prev, a_ref), _best_clip(prev, b_ref)
    named = set(cur_ids)
    if pa is not None and pb is not None and pa < pb:
        named |= {c.id for c in prev.clips[pa + 1 : pb]}
    new = timeline.history[len(prev.history):]
    d = next((d for d in new if d.action in ("drop", "insert", "pin_moved", "removed", "added") and named & set(d.clip_ids)), None)
    if d is None:
        return ""
    return d.detail[len("human "):] if d.pass_name == "human" and d.detail.startswith("human ") else d.detail  # "made adjacent by human: removed c002 ..."


def _state(stage: str, timeline: Timeline, i: int, scorer: Optional[MatchScorer]) -> JunctionState:
    a, b = timeline.clips[i], timeline.clips[i + 1]
    score: Optional[MatchScore] = None
    if scorer is not None:
        try:
            score = scorer.score_cut(a.source_out, b.source_in)
        except Exception:
            score = None
    return JunctionState(stage, a.source_out, b.source_in, b.transition_in.kind, b.transition_in.duration, a.locked_out, b.locked_in, score)


def _attribute(
    new: Sequence[Decision],
    prev_ids: Optional[Tuple[str, str]],
    cur_ids: Optional[Tuple[str, str]],
    prev_all: Optional[Set[str]],
    cur_all: Set[str],
    prev_pair_only: bool = False,
) -> List[Tuple[Decision, bool]]:
    """The (decision, clip_level) pairs among a stage's new decisions that concern the junction.

    A pass records the ids of the timeline it was given, then renumbers. So a decision is read
    against the junction's ids in the previous stage first; only a decision whose ids all exist
    in this stage but not all in the previous one (a clip the pass created) is read against this
    stage's ids. In the first stage there is no previous one and its own ids apply.
    ``prev_pair_only``: the previous ids are those of clips that were not adjacent there, so only a decision naming
    both of them (a pin's move) counts; one naming a single clip (a pass's trim) says nothing about this junction.  The
    human stage's own trim of one of the two clips does count: it is what the editor did to this very cut.
    """
    found: List[Tuple[Decision, bool]] = []
    for d in new:
        ids = d.clip_ids
        if not ids or len(ids) > 3 or d.action in WHOLE_SCOPE_ACTIONS:
            continue
        candidates: List[Tuple[str, str]] = []
        if prev_ids is not None:
            candidates.append(prev_ids)
        if cur_ids is not None and (prev_all is None or (all(i in cur_all for i in ids) and not all(i in prev_all for i in ids))):
            candidates.append(cur_ids)
        for pair in candidates:
            if all(i in ids for i in pair):
                found.append((d, False))
                break
            # (the editor's own trim of a clip is the editor's word about this junction even when a removal is what joined the clips)
            if len(ids) == 1 and ids[0] in pair and not (prev_pair_only and pair is prev_ids and d.pass_name != HUMAN_STAGE):
                found.append((d, True))
                break
    return found


def build_junctions(stages: Sequence[Tuple[str, Timeline]], scorer: Optional[MatchScorer] = None, upto: Optional[str] = None) -> List[Junction]:
    """Junctions of stage ``upto`` (default: the last stage), each followed through the stages up to it."""
    if not stages:
        return []
    names = [s for s, _ in stages]
    end = names.index(upto) if upto in names else len(stages) - 1
    considered = list(stages[: end + 1])
    ref = considered[-1][1]
    # The human stage (an editor's re-export) can hold clips that are not instances of any canonical clip (added, replaced, copied).  Lineage
    # (same shot, overlapping source range) would give such a clip the history and the pass verdicts of the clip it overlaps, so a junction of
    # the human stage follows the earlier stages only when both its clips are instances the canonical stages have (``clip_origin``).
    known = {clip_origin(c) for _, t in considered[:-1] for c in t.clips} if considered[-1][0] == HUMAN_STAGE else None
    out: List[Junction] = []
    for i in range(len(ref.clips) - 1):
        a, b = ref.clips[i], ref.clips[i + 1]
        j = Junction(i, a.id, b.id, a.shot_index, b.shot_index, a.beat_id, b.beat_id, ref.fps, first_stage=names[0])
        inherited = known is None or (clip_origin(a) in known and clip_origin(b) in known)
        at: List[Optional[int]] = []  # index of the junction's first clip in each considered stage
        for k, (stage, timeline) in enumerate(considered):
            idx = i if k == len(considered) - 1 else (_locate(timeline, a, b) if inherited else None)
            at.append(idx)
            if idx is not None:
                j.states.append(_state(stage, timeline, idx, scorer))
        for k, (_, timeline) in enumerate(considered):
            prev = considered[k - 1][1] if k else None
            new = timeline.history[len(prev.history):] if prev is not None else timeline.history
            cur_ids = (timeline.clips[at[k]].id, timeline.clips[at[k] + 1].id) if at[k] is not None else None
            prev_idx = at[k - 1] if k else None
            prev_ids = (prev.clips[prev_idx].id, prev.clips[prev_idx + 1].id) if prev is not None and prev_idx is not None else None
            pair_only = False
            if prev is not None and prev_ids is None and inherited:
                # Not adjacent before this stage (a pin's move joined them): the stage's decisions still name the previous
                # stage's clips, so read them against the clips the two descend from.
                pa, pb = _best_clip(prev, a), _best_clip(prev, b)
                if pa is not None and pb is not None and pa != pb:
                    prev_ids = (prev.clips[pa].id, prev.clips[pb].id)
                    pair_only = True
            prev_all = {c.id for c in prev.clips} if prev is not None else None
            for d, clip_level in _attribute(new, prev_ids, cur_ids, prev_all, {c.id for c in timeline.clips}, pair_only):
                j.decisions.append(d)
                j.clip_level.append(clip_level)
                j.decision_stage.append(considered[k][0])
        first = next((k for k, idx in enumerate(at) if idx is not None), 0)
        if first > 0:  # the clips were not adjacent before stage ``first``: a pass joined them
            tl_first = considered[first][1]
            j.adjacent_detail = _adjacency_detail(considered, first, a, b, (tl_first.clips[at[first]].id, tl_first.clips[at[first] + 1].id))
        if known is not None:  # the human stage: a pin counts only if the editor's cut still holds it
            held = next((d for d in reversed(j.decisions) if d.pass_name == "editor" and d.action == "pinned"), None)
            if held is not None:
                try:
                    j.pin_lost = pin_change(ref, Pin.from_dict(held.data), float(held.data["out"]), float(held.data["in_"]))
                except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                    j.pin_lost = ""
        out.append(j)
    return out


def junction_line(j: Junction) -> str:
    cur = j.current
    score = f"{cur.score.combined:.2f}" if cur.score is not None else "  - "
    tags = ([f"match:{j.match_type}"] if j.match_type else []) + (["locked"] if cur.locked else []) + (["pinned"] if j.pinned else []) + (["pin-changed"] if j.pin_lost else []) + (["beat"] if j.beat_boundary else [])
    return (
        f"{j.index + 1:>3}  {j.out_clip}->{j.in_clip}  s{j.out_shot}->s{j.in_shot}  "
        f"out {cur.out_time:7.2f}  in {cur.in_time:7.2f}  {cur.transition:<12} score {score}  {' '.join(tags)}"
    ).rstrip()


def junction_detail(j: Junction) -> str:
    lines = [junction_line(j), f"why: {j.why()}"] + ([f"pin: changed by the editor: {j.pin_lost}"] if j.pin_lost else []) + ["history:"] + [f"  {h}" for h in j.history()]
    b, c = j.baseline, j.current
    if b.score is not None and c.score is not None:
        lines.append(f"scores ({b.stage} -> {c.stage}):")
        lines += [f"  {name:<12} {getattr(b.score, name):.3f} -> {getattr(c.score, name):.3f}" for name in SCORE_ROWS]
    lines.append("decisions:")
    lines += [f"  {d.pass_name}:{d.action}  {d.detail}" for d in j.decisions] or ["  (none)"]
    return "\n".join(lines)
