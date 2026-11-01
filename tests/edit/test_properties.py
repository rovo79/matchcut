"""Property tests: random edit sequences on random valid cuts (fixed seeds; no extra dependency).

Every accepted proposal is checked by code independent of the rules (``interchange.common.layout`` for the frames, ``pin_outcome`` for the
pins, ``is_match_cut`` for the match cuts):
- clips no operation touched, and the junctions between two of them, are identical in frames;
- the running time changes exactly by the clip lengths and the transition overlaps (and by the trim for a single trim);
- every pin the cut held is still held; every match cut keeps its clips adjacent, its cut points and its hard cut;
- no violation is added: shot bounds, minimum length, transition limits, a cut the layout accepts;
- the timing report and the keep_record_frames guard agree with the layout.
"""
from __future__ import annotations

import random
from fractions import Fraction
from typing import Any, Dict, List, Tuple

import pytest

from matchcut.edit.model import EditContext, Refused
from matchcut.edit.ops import apply_ops, held_pins
from matchcut.edit.rules import problems, timing
from matchcut.interchange.common import junction_ids, layout, record_frames
from matchcut.pipeline.editorial import Pin, apply_pins, pin_outcome, pin_site
from matchcut.pipeline.passes import is_match_cut
from matchcut.pipeline.timeline import HUMAN_STAGE, Clip, Timeline, Transition, clip_origin

from .util import R23976, R25, R2997, R5994, bounds_for, click

RATES = (R25, R2997, R23976, R5994)
SHOTS, SHOT_FRAMES = 10, 240


def random_context(rng: random.Random) -> EditContext:
    rate = rng.choice(RATES)
    music = click(beat=rng.choice([0.4, 0.5, 0.48, 0.6])) if rng.random() < 0.6 else None
    return EditContext(rate, bounds_for(SHOTS, SHOT_FRAMES, rate), rng.choice([0.5, 0.75, 1.0]), rng.choice([0.3, 0.4, 0.5]), (), music,
                       rng.choice([0.0, 0.25]), 0.35, rng.random() < 0.5)


def random_cut(rng: random.Random, ctx: EditContext) -> Tuple[Timeline, EditContext]:
    """A valid cut: clips inside their shots and at least the minimum long, transitions within the limit, maybe a match cut and a pin."""
    n = rng.randint(4, 9)
    beats = sorted(rng.choice(["b1", "b2", "b3"]) for _ in range(n))
    tl = Timeline(source="s.mp4", fps=float(ctx.rate))
    lengths: List[int] = []
    for k in range(n):
        shot = rng.randrange(SHOTS)
        lo, hi = ctx.shot_frames(shot)
        length = rng.randint(ctx.min_frames, min(ctx.min_frames + 90, hi - lo - 10))
        start = rng.randint(lo, hi - length)
        c = Clip(f"c{k + 1:03d}", shot, ctx.s(start), ctx.s(start + length), beats[k])
        c.origin = clip_origin(c)
        if k and rng.random() < 0.45:
            limit = int(ctx.max_fraction * min(length, lengths[-1]))
            if limit >= 1:
                c.transition_in = Transition(rng.choice(["dissolve", "dip_to_black"]), ctx.s(rng.randint(1, limit)))
        tl.clips.append(c)
        lengths.append(length)
    pins: List[Pin] = []
    junctions = list(range(n - 1))
    rng.shuffle(junctions)
    if rng.random() < 0.5:
        j = junctions.pop()
        a, b = tl.clips[j], tl.clips[j + 1]
        b.transition_in = Transition()
        a.locked_out, b.locked_in = True, True
        a.tags.append("match_out:motion")
        b.tags.append("match_in:motion")
    if rng.random() < 0.6 and junctions:
        j = junctions.pop()
        a, b = tl.clips[j], tl.clips[j + 1]
        if (a.shot_index, b.shot_index) not in {(c.shot_index, d.shot_index) for c, d in zip(tl.clips, tl.clips[1:]) if (c, d) != (a, b)}:
            p = Pin(a.shot_index, b.shot_index, round(a.source_out, 3), round(b.source_in, 3), "locked", "", clip_origin(a), clip_origin(b),
                    hold_transition=rng.random() < 0.5)
            tl, decisions = apply_pins(tl, [p], ctx.bounds, ctx.min_clip_seconds)
            assert decisions[-1].action == "pinned"
            pins.append(p)
    ctx = EditContext(ctx.rate, ctx.bounds, ctx.min_clip_seconds, ctx.max_fraction_of_clip, tuple(pins), ctx.music, ctx.music_start,
                      ctx.snap_window, ctx.prefer_downbeats)
    assert len(held_pins(tl, ctx)) == len(pins)
    return tl, ctx


def random_op(rng: random.Random, tl: Timeline, ctx: EditContext) -> Dict[str, Any]:
    ids = [c.id for c in tl.clips]
    j = rng.randint(1, max(1, len(tl.clips) - 1))
    d = rng.choice([-12, -6, -3, -2, -1, 1, 2, 3, 6, 12, 25])
    kind = rng.choice(OPS_WEIGHTED)
    shot = rng.randrange(SHOTS)
    lo, _ = ctx.shot_frames(shot)
    if kind == "trim":
        return {"op": "trim", "clip": rng.choice(ids), "edge": rng.choice(["in", "out"]), "frames": d}
    if kind == "roll":
        return {"op": "roll", "junction": rng.choice([j, junction_ids(tl)[j - 1]]) if len(tl.clips) > 1 else 1, "frames": d}
    if kind == "slip":
        return {"op": "slip", "clip": rng.choice(ids), "frames": d}
    if kind == "replace":
        op = {"op": "replace", "clip": rng.choice(ids), "shot": shot}
        if rng.random() < 0.3:
            op["frames"] = rng.randint(ctx.min_frames, ctx.min_frames + 40)
        return op
    if kind == "insert":
        start = lo + rng.randint(0, 100)
        return {"op": "insert", "shot": shot, "in_frame": start, "out_frame": start + rng.randint(ctx.min_frames - 2, ctx.min_frames + 60),
                rng.choice(["before", "after"]): rng.choice(ids)}
    if kind == "remove":
        return {"op": "remove", "clip": rng.choice(ids)}
    if kind == "move":
        return {"op": "move", "clip": rng.choice(ids), rng.choice(["before", "after"]): rng.choice(ids)}
    if kind == "set_transition":
        k = rng.choice(["cut", "dissolve", "dip_to_black"])
        return {"op": "set_transition", "junction": j, "kind": k, **({} if k == "cut" else {"frames": rng.randint(1, 30)})}
    return {"op": "snap_to_beat", "junction": j, "mode": rng.choice(["roll", "roll", "ripple"])}


OPS_WEIGHTED = ["trim"] * 3 + ["roll"] * 3 + ["slip"] * 2 + ["replace", "insert", "remove", "move"] + ["set_transition"] * 3 + ["snap_to_beat"] * 2


# -- the independent checks ------------------------------------------------------------------------------------------------------
def cuts(tl: Timeline, rate: Fraction) -> Dict[Tuple[str, str], Tuple[int, int, str, int, int]]:
    rows = layout(tl, rate)
    return {(tl.clips[i].id, tl.clips[i + 1].id): (rows[i].out_f, rows[i + 1].in_f, rows[i + 1].kind, rows[i + 1].tr, rows[i + 1].rec)
            for i in range(len(tl.clips) - 1)}


def check_accepted(before: Timeline, after: Timeline, touched, ops: List[Dict[str, Any]], ctx: EditContext) -> None:
    rate = ctx.rate
    lb = layout(before, rate)
    la = layout(after, rate)  # no clip shorter than its transitions (the base has none)
    old = {c.id: c for c in before.clips}
    # untouched clips and junctions
    for c in after.clips:
        if c.id in old and c.id not in touched:
            assert c.to_dict() == old[c.id].to_dict(), c.id
    jb, ja = cuts(before, rate), cuts(after, rate)
    for k, v in ja.items():
        if k in jb and not set(k) & set(touched):
            assert v[:4] == jb[k][:4], k
    # running time: the clip lengths minus the overlaps
    lengths = lambda rows: sum(r.out_f - r.in_f for r in rows)  # noqa: E731
    overlaps = lambda rows: sum(r.tr for r in rows)  # noqa: E731
    delta = record_frames(la) - record_frames(lb)
    assert delta == (lengths(la) - lengths(lb)) - (overlaps(la) - overlaps(lb))
    if len(ops) == 1:
        op = ops[0]
        if op["op"] == "trim":
            assert delta == (op["frames"] if op["edge"] == "out" else -op["frames"])
        elif op["op"] in ("roll", "slip") or (op["op"] == "snap_to_beat" and op["mode"] == "roll"):
            assert delta == 0
        elif op["op"] == "set_transition":
            j = op["junction"]  # 1-based: the incoming clip is clip j, in both cuts
            assert after.clips[j].id == before.clips[j].id and delta == -(la[j].tr - lb[j].tr)
    # pins (judged with pin_outcome / pin_site directly, not through the rules' held_pins) and the transitions they protect
    for pin in ctx.pins:
        d0 = pin_outcome(before, pin, HUMAN_STAGE)
        if d0 is None or d0.action != "pinned":
            continue
        site = pin_site(before, pin)
        pair = (before.clips[site].id, before.clips[site + 1].id)
        d1 = pin_outcome(after, pin, HUMAN_STAGE)
        assert d1 is not None and d1.action == "pinned", d1 and d1.detail
        assert pair in ja and ja[pair][:2] == jb[pair][:2], pair  # the very clips, adjacent, same cut points
        if pin.hold_transition:
            assert ja[pair][2:4] == jb[pair][2:4], (pair, jb[pair], ja[pair])
    # match cuts: adjacent, same cut points, a hard cut
    for i in range(len(before.clips) - 1):
        a, b = before.clips[i], before.clips[i + 1]
        if is_match_cut(a, b):
            assert (a.id, b.id) in ja and ja[(a.id, b.id)][:2] == jb[(a.id, b.id)][:2] and ja[(a.id, b.id)][3] == 0
    # nothing gets worse: shot bounds, the minimum length, transition limits (a base the pipeline left imperfect stays as it was, or better)
    old_rows = {c.id: (c, r) for c, r in zip(before.clips, lb)}
    for c, r in zip(after.clips, la):
        lo, hi = ctx.shot_frames(c.shot_index)
        prev = old_rows.get(c.id)
        same = prev is not None and prev[0].shot_index == c.shot_index and prev[0].origin == c.origin
        excess = max(0, lo - r.in_f) + max(0, r.out_f - hi)
        was = (max(0, lo - prev[1].in_f) + max(0, prev[1].out_f - hi)) if same else 0
        assert excess <= was, c.id
        if r.out_f - r.in_f < ctx.min_frames:
            assert same and r.out_f - r.in_f >= prev[1].out_f - prev[1].in_f, c.id
    def over(rows, i):  # noqa: E306
        return rows[i].tr - ctx.max_fraction * min(rows[i - 1].out_f - rows[i - 1].in_f, rows[i].out_f - rows[i].in_f)
    pos_b = {(before.clips[i - 1].id, before.clips[i].id): i for i in range(1, len(before.clips))}
    for i in range(1, len(la)):
        k = pos_b.get((after.clips[i - 1].id, after.clips[i].id))
        assert over(la, i) <= max(0, over(lb, k) if k is not None else 0), (after.clips[i - 1].id, after.clips[i].id)
    assert after.clips[0].transition_in.is_cut
    # the timing report and the record-frame guard agree with the layout
    t = timing(before, after, ctx)
    moved = {tuple(m["clips"]): m["delta"] for m in t["moved"]}
    ids_b = junction_ids(before)
    for k, v in ja.items():
        if k in jb:
            assert moved.get(k, 0) == v[4] - jb[k][4]
            i = [c.id for c in before.clips].index(k[0])
            guarded = problems(before, after, touched, ctx, {"keep_record_frames": [ids_b[i]]})
            assert bool(guarded) == (v[4] != jb[k][4]), (k, guarded)
    assert t["duration_frames"] == [record_frames(lb), record_frames(la)]


def imperfect(rng: random.Random, tl: Timeline, ctx: EditContext) -> Timeline:
    """What the pipeline can leave: one clip a few frames under the minimum, one transition over the fraction limit (the cut still lays
    out).  Neither is on a pinned or match-cut junction."""
    out = tl.copy()
    rows = layout(out, ctx.rate)
    free = [k for k, c in enumerate(out.clips) if not (c.locked_in or c.locked_out)]
    if free:
        k = rng.choice(free)
        c, r = out.clips[k], rows[k]
        c.source_out = ctx.s(r.in_f + ctx.min_frames - rng.randint(1, 3))
        n = ctx.min_frames - 3
        for x in (c, out.clips[k + 1] if k + 1 < len(out.clips) else None):
            if x is not None and not x.transition_in.is_cut and ctx.f(x.transition_in.duration) > n // 3:
                x.transition_in = Transition(x.transition_in.kind, ctx.s(max(1, n // 3)))
    sites = {s for _, s in held_pins(out, ctx)}
    rows = layout(out, ctx.rate)
    for i in rng.sample(range(1, len(out.clips)), len(out.clips) - 1):
        a, b = out.clips[i - 1], out.clips[i]
        if is_match_cut(a, b) or i - 1 in sites:
            continue
        limit = int(ctx.max_fraction * min(rows[i - 1].out_f - rows[i - 1].in_f, rows[i].out_f - rows[i].in_f))
        trial = out.copy()
        trial.clips[i].transition_in = Transition("dissolve", ctx.s(limit + rng.randint(1, 4)))
        try:
            layout(trial, ctx.rate)
        except Exception:
            continue
        out = trial
        break
    return out


def run_sequence(rng: random.Random, tl: Timeline, ctx: EditContext, steps: int) -> Tuple[int, int]:
    """Random proposals from ``tl``, each accepted one checked and taken as the next head, as a session does (it reserves every id it
    has seen).  (accepted, refused)."""
    accepted = refused = 0
    reserved = {c.id for c in tl.clips}
    for _ in range(steps):
        ops = [random_op(rng, tl, ctx) for _ in range(rng.choice([1, 1, 1, 2, 3]))]
        snapshot = tl.to_dict()
        try:
            res = apply_ops(tl, ops, ctx, reserved)
        except Refused:
            assert tl.to_dict() == snapshot
            refused += 1
            continue
        assert tl.to_dict() == snapshot  # never mutated
        assert not ({c.id for c in res.timeline.clips} - {c.id for c in tl.clips}) & reserved  # a new id is never one used before
        probs = problems(tl, res.timeline, res.touched, ctx)
        if probs:
            refused += 1
            continue
        check_accepted(tl, res.timeline, res.touched, ops, ctx)
        accepted += 1
        reserved |= {c.id for c in res.timeline.clips}
        tl = res.timeline
    return accepted, refused


@pytest.mark.parametrize("seed", range(40))
def test_random_edit_sequences_keep_every_invariant(seed):
    rng = random.Random(seed)
    ctx = random_context(rng)
    tl, ctx = random_cut(rng, ctx)
    assert problems(tl, tl, set(), ctx) == []
    accepted, refused = run_sequence(rng, tl, ctx, 60)
    assert accepted >= 10, (accepted, refused)  # one proposal in six at least (about a third over all seeds)


@pytest.mark.parametrize("seed", range(30))
def test_random_edits_on_an_imperfect_base_make_nothing_worse(seed):
    rng = random.Random(500 + seed)
    ctx = random_context(rng)
    tl, ctx = random_cut(rng, ctx)
    base = imperfect(rng, tl, ctx)
    rows = layout(base, ctx.rate)
    under = [r for r in rows if r.out_f - r.in_f < ctx.min_frames]
    over = [i for i in range(1, len(rows)) if rows[i].tr > ctx.max_fraction * min(rows[i - 1].out_f - rows[i - 1].in_f, rows[i].out_f - rows[i].in_f)]
    assert under or over
    accepted, refused = run_sequence(rng, base, ctx, 60)
    assert accepted >= 10, (accepted, refused)


@pytest.mark.parametrize("seed", range(20))
def test_the_rules_catch_a_change_no_operation_made(seed):
    """Sabotage an accepted result: move a frame of a clip no operation touched; the rules must say so."""
    rng = random.Random(1000 + seed)
    ctx = random_context(rng)
    tl, ctx = random_cut(rng, ctx)
    caught = 0
    for _ in range(30):
        try:
            res = apply_ops(tl, [random_op(rng, tl, ctx)], ctx)
        except Refused:
            continue
        if problems(tl, res.timeline, res.touched, ctx):
            continue
        idle = [c for c in res.timeline.clips if c.id not in res.touched and any(c.id == b.id for b in tl.clips)]
        if not idle:
            continue
        bad = res.timeline.copy()
        pick = rng.choice(idle).id
        victim = next(c for c in bad.clips if c.id == pick)
        victim.source_out = ctx.s(ctx.f(victim.source_out) - 1)
        assert any(f"{victim.id} changed but no operation touched it" == p for p in problems(tl, bad, res.touched, ctx))
        caught += 1
        tl = res.timeline
    assert caught >= 1


@pytest.mark.parametrize("seed", range(10))
def test_guards_on_random_transitions(seed):
    """A dissolve added anywhere moves every later junction earlier by its length; max_duration_change_frames sees it exactly."""
    rng = random.Random(2000 + seed)
    ctx = random_context(rng)
    tl, ctx = random_cut(rng, ctx)
    rows = layout(tl, ctx.rate)
    protected = {site for pin, site in held_pins(tl, ctx) if pin.hold_transition}
    for j in range(1, len(tl.clips)):
        if j - 1 in protected:
            continue
        limit = int(ctx.max_fraction * min(rows[j - 1].out_f - rows[j - 1].in_f, rows[j].out_f - rows[j].in_f))
        if rows[j].tr or limit < 2 or is_match_cut(tl.clips[j - 1], tl.clips[j]):
            continue
        n = rng.randint(1, limit)
        res = apply_ops(tl, [{"op": "set_transition", "junction": j, "kind": "dissolve", "frames": n}], ctx)
        t = timing(tl, res.timeline, ctx)
        assert t["duration_change"] == -n
        assert [m["delta"] for m in t["moved"]] == [-n] * (len(tl.clips) - j)  # the junction itself and every later one
        assert problems(tl, res.timeline, res.touched, ctx, {"max_duration_change_frames": n}) == []
        assert problems(tl, res.timeline, res.touched, ctx, {"max_duration_change_frames": n - 1}) == [
            f"guard max_duration_change_frames: the running time changes by {-n:+d} frames (at most {n - 1} allowed)"]
