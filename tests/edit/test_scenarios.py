"""The prototype proof's scenarios (docs/EDITING-API-FEASIBILITY.md, steps 2b-8), re-expressed against ``matchcut.edit`` on a demo-like
cut in memory, at 25 fps and at 29.97 fps.  Where decision 2 changed the answer (a pinned junction's transition), the test says so."""
from __future__ import annotations

from typing import Any, Dict

import pytest

from matchcut.edit.model import Refused, frame_rows
from matchcut.edit.ops import apply_ops
from matchcut.edit.rules import changes, describe, problems, timing

from .util import R25, R2997, click, ctx_for, cut, frames_of, match_cut, pin, with_pins

# shots of 150 frames; c004 (50 frames) carries a 19-frame dissolve (40% of 50 = 20); c005 (18 frames) was left under the minimum
DEMO = [("c001", 0, 20, 80, "b1"), ("c002", 1, 170, 230, "b1", ("dissolve", 8)), ("c003", 3, 470, 530, "b2"),
        ("c004", 6, 920, 970, "b2", ("dissolve", 19)), ("c005", 7, 1070, 1088, "b3")]


def propose(tl, ctx, ops, guards=None) -> Dict[str, Any]:
    """A proposal as the session will run it: all operations, then the proposal-wide rules."""
    try:
        res = apply_ops(tl, ops, ctx)
    except Refused as exc:
        return {"ok": False, "reasons": str(exc), "problems": []}
    probs = problems(tl, res.timeline, res.touched, ctx, guards)
    return {"ok": not probs, "reasons": " ".join(probs), "problems": probs, "timeline": res.timeline, "details": res.details,
            "changes": changes(tl, res.timeline, ctx), "timing": timing(tl, res.timeline, ctx)}


@pytest.fixture(params=[R25, R2997], ids=["25", "29.97"])
def demo(request):
    """(the pinned demo cut, its context): junction 1 pinned as the Edit view's ``l`` does, music beats every 0.5 s."""
    ctx = ctx_for(request.param, n_shots=8, frames_per_shot=150, music=click(beat=0.5))
    tl, p = pin(cut(ctx, DEMO), ctx, 0)
    return tl, with_pins(ctx, [p])


@pytest.fixture(params=[R25, R2997], ids=["25", "29.97"])
def nopin(request):
    """The same cut without the pin, with c004|c005 marked a match cut as the match-cut pass marks one."""
    ctx = ctx_for(request.param, n_shots=8, frames_per_shot=150, music=click(beat=0.5))
    tl = match_cut(cut(ctx, DEMO), 3)
    return tl, ctx


def test_0_the_fixture(demo):
    tl, ctx = demo
    assert tl.clips[0].locked_out and tl.clips[1].locked_in
    assert problems(tl, tl, set(), ctx) == []
    d = describe(tl, ctx)
    assert len(d["clips"]) == 5 and len(d["junctions"]) == 4
    assert [j["pinned"] for j in d["junctions"]] == [True, False, False, False]
    assert all(isinstance(j["beat_offset_frames"], int) for j in d["junctions"])


@pytest.mark.parametrize("ops, why", [
    ([{"op": "trim", "clip": "c001", "edge": "out", "frames": -2}], "held by an editor pin"),
    ([{"op": "roll", "junction": 1, "frames": 2}], "held by an editor pin"),
    ([{"op": "snap_to_beat", "junction": 1, "window_s": 5.0}], "held by an editor pin"),
    ([{"op": "roll", "junction": 2, "frames": 71}], "c002 would leave shot 1"),
    ([{"op": "trim", "clip": "c003", "edge": "out", "frames": -45}], "below the minimum clip length"),
    ([{"op": "remove", "clip": "c002"}], "breaks the editor pin"),
    ([{"op": "move", "clip": "c002", "after": "c004"}], "breaks the editor pin"),
    ([{"op": "roll", "junction": 3, "frames": 1}, {"op": "trim", "clip": "c001", "edge": "out", "frames": -1}], "held by an editor pin"),
    ([{"op": "trim", "clip": "c999", "edge": "in", "frames": 1}], "no clip c999"),
    ([{"op": "explode"}], "unknown op"),
    ([{"op": "trim", "clip": "c003", "edge": "out", "frames": 1.9}], "whole number"),
    ([{"op": "trim", "clip": "c004", "edge": "in", "frames": 25}], "longer than 40%"),
], ids=["pinned-trim", "pinned-roll", "pinned-snap", "past-shot", "below-min", "remove-pinned", "move-pinned", "batch-whole", "unknown-clip",
        "unknown-op", "fractional", "trim-transition-limit"])
def test_2b_refusals(demo, ops, why):
    tl, ctx = demo
    before = tl.to_dict()
    p = propose(tl, ctx, ops)
    assert not p["ok"] and why in p["reasons"], p["reasons"]
    assert tl.to_dict() == before


def test_2b_decision_2_a_pinned_junction_may_now_change_its_transition(demo):
    tl, ctx = demo  # the prototype refused this; decision 2 allows it while the pin does not protect the transition
    p = propose(tl, ctx, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])
    assert p["ok"] and p["changes"] == ["c001|c002: dissolve 8f -> dissolve 4f"]
    protected, q = pin(cut(ctx, DEMO), ctx, 0, hold_transition=True, held_transition={"kind": "dissolve", "frames": 8})
    p = propose(protected, with_pins(ctx, [q]), [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])
    assert not p["ok"] and "protected by the editor pin" in p["reasons"]


def test_2b_changing_one_transition_moves_no_other_junction(demo):
    tl, ctx = demo
    p = propose(tl, ctx, [{"op": "set_transition", "junction": 2, "kind": "dissolve", "frames": 6}])
    rb, ra = frame_rows(tl, ctx.rate), frame_rows(p["timeline"], ctx.rate)
    assert p["ok"] and p["changes"] == ["c002|c003: cut -> dissolve 6f"]
    assert [(r.in_f, r.out_f, r.tr) for r in ra] == [(r.in_f, r.out_f, r.tr) for r in rb[:2]] + [(470, 530, 6)] + [
        (r.in_f, r.out_f, r.tr) for r in rb[3:]]
    p = propose(tl, ctx, [{"op": "set_transition", "junction": 1, "kind": "cut"}])
    assert p["ok"] and p["changes"] == ["c001|c002: dissolve 8f -> cut"]


@pytest.mark.parametrize("ops, why", [
    ([{"op": "insert", "shot": 5, "in_frame": 760, "out_frame": 800, "after": "c004"}], "breaks the match cut c004|c005"),
    ([{"op": "remove", "clip": "c005"}], "breaks the match cut c004|c005"),
    ([{"op": "move", "clip": "c005", "before": "c003"}], "breaks the match cut c004|c005"),
    ([{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 4}], "match cuts stay hard cuts"),
    ([{"op": "trim", "clip": "c004", "edge": "out", "frames": -2}], "held by a match cut"),
], ids=["insert-inside", "remove-half", "move-half", "dissolve", "trim-edge"])
def test_2d_match_cuts(nopin, ops, why):
    tl, ctx = nopin
    p = propose(tl, ctx, ops)
    assert not p["ok"] and why in p["reasons"], p["reasons"]


def test_2d_first_clip(nopin):
    tl, ctx = nopin
    # the prototype's case: after a move, c004's dissolve meets a clip too short for it
    short = cut(ctx, [("c001", 0, 20, 80), ("c002", 1, 170, 200), ("c003", 3, 470, 530), ("c004", 6, 920, 970, "b2", ("dissolve", 19))])
    p = propose(short, ctx, [{"op": "move", "clip": "c003", "before": "c001"}])
    assert not p["ok"] and "the 19-frame dissolve at c002|c004 is longer than 40%" in p["reasons"]
    p = propose(tl, ctx, [{"op": "remove", "clip": "c001"}, {"op": "remove", "clip": "c002"}])
    assert p["ok"] and p["changes"] == ["removed c001 (shot 0, frames 20-80)", "removed c002 (shot 1, frames 170-230)"]
    p = propose(tl, ctx, [{"op": "remove", "clip": "c001"}])
    assert p["ok"] and "c002: incoming dissolve 8f dropped (it is the first clip now)" in p["changes"]


def test_3_roll(demo):
    tl, ctx = demo
    p = propose(tl, ctx, [{"op": "roll", "junction": 3, "frames": 3}])
    assert not p["ok"] and "the 19-frame dissolve at c003|c004 is longer than 40% of the shorter clip (18 frames)" in p["reasons"]
    p = propose(tl, ctx, [{"op": "roll", "junction": 3, "frames": 1}])
    assert p["ok"] and sorted(p["changes"]) == ["c003: out +1f", "c004: in +1f"]
    assert p["timing"]["duration_change"] == 0 and p["timing"]["moved"] == [
        {"id": "s3-s6", "clips": ["c003", "c004"], "before": frame_rows(tl, ctx.rate)[3].rec, "after": frame_rows(tl, ctx.rate)[3].rec + 1, "delta": 1}]


def test_4_replace(demo):
    tl, ctx = demo
    p = propose(tl, ctx, [{"op": "replace", "clip": "c004", "shot": 4}])
    assert p["ok"] and p["timing"]["duration_change"] == 0
    assert sorted(e.split(" (")[0] for e in p["changes"]) == ["added e001", "removed c004"]
    assert any(e.startswith("added e001 (shot 4, frames 650-700, incoming dissolve 19f)") for e in p["changes"])
    new = p["timeline"].clips[3]
    assert new.id == "e001" and new.transition_in.kind == "dissolve"


def test_5_transitions_overlap_their_clips(demo):
    tl, ctx = demo
    p = propose(tl, ctx, [{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 40}])
    assert not p["ok"] and "longer than 40%" in p["reasons"]
    p = propose(tl, ctx, [{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 6}])
    assert p["ok"] and p["changes"] == ["c004|c005: cut -> dissolve 6f"]
    rec = frame_rows(tl, ctx.rate)[4].rec
    assert p["timing"]["moved"] == [{"id": "s6-s7", "clips": ["c004", "c005"], "before": rec, "after": rec - 6, "delta": -6}]
    assert p["timing"]["duration_change"] == -6
    p = propose(tl, ctx, [{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 6}], {"keep_record_frames": ["s6-s7"]})
    assert not p["ok"] and p["problems"] == [f"guard keep_record_frames: junction s6-s7 (c004|c005) moves from record frame {rec} to {rec - 6} (-6f)"]


def test_6_beats(demo):
    tl, ctx = demo
    offsets = [j["beat_offset_frames"] for j in describe(tl, ctx)["junctions"]]
    j = next(k for k in (2, 3, 4) if offsets[k - 1] != 0 and abs(offsets[k - 1]) <= 8)
    p = propose(tl, ctx, [{"op": "snap_to_beat", "junction": j}])
    assert p["ok"] and "f onto the " in p["details"][0]
    after = [x["beat_offset_frames"] for x in describe(p["timeline"], ctx)["junctions"]]
    assert after[j - 1] == 0 and after[:j - 1] + after[j:] == offsets[:j - 1] + offsets[j:]  # a roll ripples nothing
    assert p["timing"]["reached_beat"] == [describe(tl, ctx)["junctions"][j - 1]["id"]]
    again = propose(p["timeline"], ctx, [{"op": "snap_to_beat", "junction": j}])
    assert again["ok"] and again["changes"] == [] and "is already on the " in again["details"][0]
    # a new dissolve pulls a junction on its beat off it
    on = propose(p["timeline"], ctx, [{"op": "set_transition", "junction": j - 1 if j > 2 else j + 1, "kind": "dissolve", "frames": 5}])
    assert on["ok"]
    if j - 1 >= 2:
        assert describe(p["timeline"], ctx)["junctions"][j - 1]["id"] in on["timing"]["left_beat"]


def test_7_batch_insert_and_dip(demo):
    tl, ctx = demo
    p = propose(tl, ctx, [{"op": "insert", "shot": 5, "in_frame": 760, "out_frame": 800, "after": "c005"},
                          {"op": "set_transition", "junction": 5, "kind": "dip_to_black", "frames": 6}])
    assert p["ok"] and len(p["timing"]["added"]) == 1
    assert frames_of(p["timeline"], ctx)[-1] == ("e001", 760, 800, "dip_to_black", 6)


def test_8_a_cut_a_move_a_remove_and_a_slip_in_one_proposal(demo):
    tl, ctx = demo
    tl2 = propose(tl, ctx, [{"op": "replace", "clip": "c004", "shot": 4}])["timeline"]
    p = propose(tl2, ctx, [{"op": "set_transition", "junction": 3, "kind": "cut"}, {"op": "move", "clip": "e001", "before": "c003"},
                           {"op": "remove", "clip": "c005"}, {"op": "slip", "clip": "c003", "frames": 2}])
    assert p["ok"], p["reasons"]
    assert sorted(p["changes"]) == ["c002|e001: dissolve 19f -> cut", "c003: slipped +2f", "order: c001 c002 e001 c003",
                                    "removed c005 (shot 7, frames 1070-1088)"]


def test_a_clip_the_pipeline_left_under_the_minimum_is_tolerated(demo):
    tl, ctx = demo
    assert frame_rows(tl, ctx.rate)[4].length == 18 < ctx.min_frames
    assert propose(tl, ctx, [{"op": "trim", "clip": "c005", "edge": "out", "frames": 2}])["ok"]
    p = propose(tl, ctx, [{"op": "trim", "clip": "c005", "edge": "out", "frames": -1}])
    assert not p["ok"] and "c005 would be 17 frames" in p["reasons"]
    assert propose(tl, ctx, [{"op": "trim", "clip": "c003", "edge": "out", "frames": -3}])["ok"]  # c005 is untouched: not refused
