"""Shot bounds off the frame grid (shots are detected at 8 fps): an operation places points only where M2a's export keeps them
(``export_timeline`` -> ``Timeline.snap_to_frames(bounds)``), and a point the base already had just outside its shot (r000 follows the
layout) is a pre-existing excess no unrelated edit is refused for."""
from __future__ import annotations

import random
from fractions import Fraction
from pathlib import Path
from typing import Dict, Tuple

import pytest

from matchcut.edit import session as S
from matchcut.edit.model import EditContext, Refused, frame_rows
from matchcut.interchange.common import export_timeline, layout
from matchcut.pipeline.shots import Shot, save_shots
from matchcut.pipeline.timeline import Clip, Timeline

from .test_properties import random_op
from .util import click

RATES = ["25", "30000/1001"]
# c002's in-point is shot 1's start (frame 100.25: the layout gives 100, an operation may place 101 at the earliest); c003's out-point is
# shot 3's end (399.75: the layout gives 400, an operation may place 399 at the latest)
CLIPS = [("c001", 0, 10, 60, "b1"), ("c002", 1, Fraction(401, 4), 170, "b1"), ("c003", 3, 320, Fraction(1599, 4), "b2", ("dissolve", 8)),
         ("c004", 5, 520, 570, "b2"), ("c005", 6, 620, 680, "b3")]


def offgrid(make_ws, rate: str) -> Tuple[Path, Dict[int, Tuple[float, float]]]:
    w = make_ws(rate=rate, clips=CLIPS)
    r = Fraction(rate)
    # shot k spans frames 100k + 0.25 to 100(k + 1) - 0.25 (shot 0 from 0); ten shots, as many as the random operations name
    shots = [Shot(k, float(Fraction(400 * k + (1 if k else 0), 4) / r), float(Fraction(400 * (k + 1) - 1, 4) / r)) for k in range(10)]
    save_shots(shots, w / "shots.json")
    S.start(w)
    return w, {s.index: (s.start, s.end) for s in shots}


@pytest.mark.parametrize("rate", ["25", "30000/1001", "24000/1001", "60000/1001", "24", "50"])
def test_shot_frames_are_the_bounds_the_export_snaps_a_point_back_to(rate):
    rng = random.Random(rate)
    r = Fraction(rate)
    fps = float(r)
    bounds = {}
    for k in range(200):
        lo = rng.uniform(0, 3000) / fps * rng.choice([1, 1, 0.999999]) if k % 7 else (k * 40 + rng.choice([0, 0.25, 0.5, 0.75])) / fps
        bounds[k] = (round(lo, rng.choice([3, 6, 9])), round(lo + rng.uniform(3, 400) / fps, rng.choice([3, 6, 9])))
    ctx = EditContext(r, bounds)
    tl = Timeline(source="s.mp4", fps=fps, clips=[Clip(f"c{k}", k, lo - 1.0, hi + 1.0) for k, (lo, hi) in bounds.items()])
    snapped = tl.snap_to_frames(bounds)
    for k, c in zip(bounds, snapped.clips):
        assert ctx.shot_frames(k) == (round(c.source_in * fps), round(c.source_out * fps)), (k, bounds[k])


@pytest.mark.parametrize("rate", RATES)
def test_r000_follows_the_layout_and_the_base_excess_is_tolerated(make_ws, rate):
    w, bounds = offgrid(make_ws, rate)
    s = S.load(w)
    r = Fraction(rate)
    rows = frame_rows(s.timeline("r000"), r)
    assert (rows[1].in_f, rows[2].out_f) == (100, 400) and s.context().shot_frames(1) == (101, 199)
    accepted = [
        [{"op": "trim", "clip": "c002", "edge": "out", "frames": -2}], [{"op": "trim", "clip": "c003", "edge": "in", "frames": 2}],
        [{"op": "roll", "junction": 1, "frames": 2}], [{"op": "roll", "junction": 3, "frames": -2}],
        [{"op": "slip", "clip": "c002", "frames": 1}], [{"op": "slip", "clip": "c003", "frames": -1}],
        [{"op": "set_transition", "junction": 2, "kind": "dissolve", "frames": 6}], [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}],
        [{"op": "replace", "clip": "c004", "shot": 2}], [{"op": "replace", "clip": "c002", "shot": 2}],
        [{"op": "insert", "shot": 7, "in_frame": 710, "out_frame": 760, "after": "c002"}], [{"op": "remove", "clip": "c001"}],
        [{"op": "move", "clip": "c004", "before": "c002"}], [{"op": "trim", "clip": "c002", "edge": "in", "frames": 1}],
    ]
    for ops in accepted:
        p = S.propose(w, ops)
        assert p["problems"] == [], ops
    for ops, why in (([{"op": "slip", "clip": "c002", "frames": -1}], "c002 would leave shot 1"),
                     ([{"op": "roll", "junction": 1, "frames": -1}], "c002 would leave shot 1"),
                     ([{"op": "trim", "clip": "c003", "edge": "out", "frames": 1}], "c003 would leave shot 3")):
        with pytest.raises(Refused, match=why):
            S.propose(w, ops)
    # an accepted edit is exported as it is: the moved points keep their frames, and the base's own excess is pulled back as for final
    S.apply(w, S.propose(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": 29}, {"op": "trim", "clip": "c003", "edge": "in",
                                                                                          "frames": -19}])["proposal"])
    tl = S.load(w).timeline()
    ex = layout(export_timeline(tl, bounds, r)[0], r)
    rows = frame_rows(tl, r)
    assert (rows[1].out_f, rows[2].in_f) == (ex[1].out_f, ex[2].in_f) == (199, 301)
    assert (ex[1].in_f, ex[2].out_f) == (101, 399)  # the base's excess, as exporting final pulls it back


@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_point_an_accepted_proposal_places_is_kept_by_the_export(make_ws, rate, seed):
    w, bounds = offgrid(make_ws, rate)
    r = Fraction(rate)
    base_ctx = S.workspace_context(w)
    ctx = EditContext(base_ctx.rate, base_ctx.bounds, base_ctx.min_clip_seconds, base_ctx.max_fraction_of_clip, base_ctx.pins,
                      click(beat=0.48), 0.0, 0.35, True)
    r0 = S.load(w, ctx).timeline("r000")
    base = {c.id: (c.shot_index, c.origin, row.in_f, row.out_f) for c, row in zip(r0.clips, frame_rows(r0, r))}
    rng = random.Random(seed)
    accepted = 0
    for _ in range(60):
        s = S.load(w, ctx)
        tl = s.timeline()
        ops = [random_op(rng, tl, ctx) for _ in range(rng.choice([1, 1, 2, 3]))]
        try:
            p = S.propose(w, ops, ctx=ctx)
        except Refused:
            continue
        S.apply(w, p["proposal"], ctx=ctx)
        accepted += 1
        tl = S.load(w, ctx).timeline()
        ex = layout(export_timeline(tl, bounds, r)[0], r)
        for c, row, er in zip(tl.clips, frame_rows(tl, r), ex):
            b = base.get(c.id)
            same = b is not None and b[:2] == (c.shot_index, c.origin)
            if not (same and row.in_f == b[2]):
                assert er.in_f == row.in_f, (ops, c.id, "in", row.in_f, er.in_f)
            if not (same and row.out_f == b[3]):
                assert er.out_f == row.out_f, (ops, c.id, "out", row.out_f, er.out_f)
        if rng.random() < 0.15:
            S.undo(w, ctx=ctx)
    assert accepted >= 12
