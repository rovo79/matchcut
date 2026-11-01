"""The nine operations, one at a time: what each changes, in whole frames, and every refusal."""
from __future__ import annotations

import pytest

from matchcut.edit.model import Refused, frame_rows, running_frames
from matchcut.edit.ops import ARGS, OPS, apply_op, apply_ops, find_clip, find_junction, lock_reason, new_clip_id, whole
from matchcut.edit.rules import changes, problems
from matchcut.pipeline.timeline import Decision, Transition

from .util import R23976, R25, R2997, R5994, click, ctx_for, cut, frames_of, match_cut, pin, with_pins

# shots of 100 frames: shot k is frames [100k, 100k + 100]
BASE = [("c001", 0, 10, 60, "b1"), ("c002", 1, 110, 170, "b1", ("dissolve", 8)), ("c003", 2, 220, 270, "b2"),
        ("c004", 3, 320, 380, "b2", ("dissolve", 10)), ("c005", 4, 420, 470, "b3")]


@pytest.fixture
def ctx():
    return ctx_for(R25)


@pytest.fixture
def tl(ctx):
    return cut(ctx, BASE)


def run(tl, ctx, **op):
    return apply_op(tl, op, ctx)


def refused(tl, ctx, why, **op):
    with pytest.raises(Refused) as exc:
        apply_op(tl, op, ctx)
    assert why in str(exc.value), str(exc.value)
    assert "\n" not in str(exc.value)
    return str(exc.value)


def test_the_op_table():
    assert OPS == ("trim", "roll", "slip", "replace", "insert", "remove", "move", "set_transition", "snap_to_beat")
    assert set(ARGS) == set(OPS)


# -- validation ------------------------------------------------------------------------------------------------------------------
def test_an_operation_must_be_an_object_with_a_known_op(tl, ctx):
    for bad, why in (("trim", "is an object"), (["trim"], "is an object"), ({}, "unknown op None"), ({"op": "explode"}, "unknown op 'explode'"),
                     ({"op": 3}, "unknown op 3")):
        with pytest.raises(Refused) as exc:
            apply_op(tl, bad, ctx)
        assert why in str(exc.value)


def test_unknown_and_missing_arguments_are_refused(tl, ctx):
    refused(tl, ctx, "trim does not take frame (it takes clip, edge, frames)", op="trim", clip="c003", edge="out", frame=2)
    refused(tl, ctx, "trim needs frames", op="trim", clip="c003", edge="out")
    refused(tl, ctx, "trim needs clip", op="trim", edge="out", frames=1)
    refused(tl, ctx, "roll needs junction", op="roll", frames=1)
    refused(tl, ctx, "edge must be in or out", op="trim", clip="c003", edge="head", frames=1)


@pytest.mark.parametrize("value", [1.5, True, False, "2", None, [2], float("nan"), float("inf")])
def test_frame_counts_must_be_whole_numbers(tl, ctx, value):
    refused(tl, ctx, "must be a whole number", op="trim", clip="c003", edge="out", frames=value)


def test_an_integral_float_is_a_whole_number(tl, ctx):
    assert whole({"frames": 2.0}, "frames") == 2 and whole({"frames": -3}, "frames") == -3
    assert run(tl, ctx, op="trim", clip="c003", edge="out", frames=2.0).detail == "trim c003 out +2f"


def test_apply_op_never_changes_its_input(tl, ctx):
    before = tl.to_dict()
    for op in ({"op": "trim", "clip": "c003", "edge": "out", "frames": 3}, {"op": "remove", "clip": "c001"},
               {"op": "move", "clip": "c005", "before": "c001"}, {"op": "set_transition", "junction": 2, "kind": "dissolve", "frames": 4},
               {"op": "replace", "clip": "c004", "shot": 6}, {"op": "insert", "shot": 6, "in_frame": 600, "out_frame": 640, "after": "c001"}):
        out = apply_op(tl, op, ctx)
        assert out.timeline is not tl and tl.to_dict() == before


# -- addressing ------------------------------------------------------------------------------------------------------------------
def test_clips_by_id_or_key(tl, ctx):
    assert find_clip(tl, "c003").id == "c003"
    assert find_clip(tl, tl.clips[2].origin).id == "c003"
    with pytest.raises(Refused, match="no clip c999"):
        find_clip(tl, "c999")
    with pytest.raises(Refused, match="by its id or its key"):
        find_clip(tl, 3)
    twin = tl.copy()
    twin.clips[4].origin = twin.clips[2].origin
    with pytest.raises(Refused, match="ambiguous \\(c003, c005\\)"):
        find_clip(twin, twin.clips[2].origin)
    assert find_clip(twin, "c005").id == "c005"  # the id is never ambiguous


def test_junctions_by_number_or_id_including_a_repeated_shot_pair(ctx):
    tl = cut(ctx, [("c001", 1, 110, 150), ("c002", 2, 210, 250), ("c003", 1, 160, 200), ("c004", 2, 255, 295)])
    assert [find_junction(tl, r) for r in (1, "2", 3, "s1-s2", "s2-s1", "s1-s2#2")] == [0, 1, 2, 0, 1, 2]
    for bad, why in ((0, "no junction 0"), (4, "no junction 4: this cut has 3 junctions (1..3)"), ("s1-s2#3", "no junction s1-s2#3"),
                     (True, "number or its id"), (2.0, "number or its id"), (None, "number or its id")):
        with pytest.raises(Refused) as exc:
            find_junction(tl, bad)
        assert why in str(exc.value)
    out = apply_op(tl, {"op": "roll", "junction": "s1-s2#2", "frames": -2}, ctx)
    assert frames_of(out.timeline, ctx)[2:] == [("c003", 160, 198, "cut", 0), ("c004", 253, 295, "cut", 0)]


# -- trim, roll, slip ------------------------------------------------------------------------------------------------------------
def test_trim_moves_one_edge_and_later_clips_ripple(tl, ctx):
    out = run(tl, ctx, op="trim", clip="c003", edge="out", frames=5)
    assert out.touched == {"c003"} and out.detail == "trim c003 out +5f"
    assert frames_of(out.timeline, ctx)[2] == ("c003", 220, 275, "cut", 0)
    rb, ra = frame_rows(tl, ctx.rate), frame_rows(out.timeline, ctx.rate)
    assert [r.rec for r in ra] == [r.rec for r in rb[:3]] + [r.rec + 5 for r in rb[3:]]
    assert running_frames(ra) == running_frames(rb) + 5
    out = run(tl, ctx, op="trim", clip="c003", edge="in", frames=-4)
    assert frames_of(out.timeline, ctx)[2] == ("c003", 216, 270, "cut", 0)
    assert changes(tl, out.timeline, ctx) == ["c003: in -4f"]


def test_trim_stays_inside_the_shot_and_above_the_minimum(tl, ctx):
    refused(tl, ctx, "c003 would leave shot 2 (frames 220-301; the shot is frames 200-300)", op="trim", clip="c003", edge="out", frames=31)
    assert run(tl, ctx, op="trim", clip="c003", edge="out", frames=30).timeline.clips[2].source_out == 12.0
    refused(tl, ctx, "c003 would be 18 frames, below the minimum clip length (19 frames)", op="trim", clip="c003", edge="in", frames=32)
    refused(tl, ctx, "would have no frames", op="trim", clip="c003", edge="in", frames=60)
    assert frames_of(run(tl, ctx, op="trim", clip="c003", edge="in", frames=31).timeline, ctx)[2][1:3] == (251, 270)


def test_a_clip_left_under_the_minimum_may_grow_never_shrink(ctx):
    tl = cut(ctx, [("c001", 0, 10, 60), ("c002", 1, 110, 125), ("c003", 2, 220, 270)])  # c002: 15 frames, under the 19-frame minimum
    assert frames_of(run(tl, ctx, op="trim", clip="c002", edge="out", frames=2).timeline, ctx)[1][1:3] == (110, 127)
    assert frames_of(run(tl, ctx, op="slip", clip="c002", frames=3).timeline, ctx)[1][1:3] == (113, 128)  # same length
    refused(tl, ctx, "c002 would be 14 frames, below the minimum clip length (19 frames)", op="trim", clip="c002", edge="in", frames=1)
    refused(tl, ctx, "c001 would be 14 frames", op="roll", junction=1, frames=-36)


def test_a_clip_left_outside_its_shot_may_move_back_in_never_further_out(ctx):
    from matchcut.edit.model import EditContext
    tight = EditContext(ctx.rate, {**ctx.bounds, 2: (9.0, 12.0)})  # shot 2 now starts at frame 225; c003 is in at 220
    tl = cut(tight, BASE)
    assert run(tl, tight, op="trim", clip="c003", edge="out", frames=2).detail == "trim c003 out +2f"
    # an edge that moves lands inside the shot (M2a's export would move it otherwise); one that stays may stay outside
    refused(tl, tight, "c003's in-point would still lie outside shot 2 at frame 222 (the shot is frames 225-300)", op="trim", clip="c003",
            edge="in", frames=2)
    assert frames_of(run(tl, tight, op="trim", clip="c003", edge="in", frames=5).timeline, tight)[2][1] == 225
    refused(tl, tight, "c003 would leave shot 2", op="trim", clip="c003", edge="in", frames=-1)


def test_roll_moves_both_sides_of_a_cut_and_keeps_the_running_time(tl, ctx):
    out = run(tl, ctx, op="roll", junction=2, frames=3)
    assert out.touched == {"c002", "c003"} and out.detail == "roll junction 2 (c002|c003) +3f"
    assert frames_of(out.timeline, ctx)[1:3] == [("c002", 110, 173, "dissolve", 8), ("c003", 223, 270, "cut", 0)]
    assert running_frames(frame_rows(out.timeline, ctx.rate)) == running_frames(frame_rows(tl, ctx.rate))
    assert sorted(changes(tl, out.timeline, ctx)) == ["c002: out +3f", "c003: in +3f"]
    assert run(tl, ctx, op="roll", junction="s1-s2", frames=-3).timeline.clips[2].source_in == ctx.s(217)
    refused(tl, ctx, "c003 would leave shot 2", op="roll", junction=2, frames=-21)
    refused(tl, ctx, "c002 would leave shot 1", op="roll", junction=2, frames=31)


def test_slip_shows_other_frames_for_the_same_length(tl, ctx):
    out = run(tl, ctx, op="slip", clip="c003", frames=-7)
    assert frames_of(out.timeline, ctx)[2] == ("c003", 213, 263, "cut", 0) and changes(tl, out.timeline, ctx) == ["c003: slipped -7f"]
    assert running_frames(frame_rows(out.timeline, ctx.rate)) == running_frames(frame_rows(tl, ctx.rate))
    refused(tl, ctx, "would leave shot 2", op="slip", clip="c003", frames=31)


# -- replace, insert, remove, move -----------------------------------------------------------------------------------------------
def test_replace_takes_the_same_length_from_the_middle_of_the_shot_and_keeps_the_transition(tl, ctx):
    out = run(tl, ctx, op="replace", clip="c004", shot=6)
    new = out.timeline.clips[3]
    assert (new.id, new.shot_index, new.origin, new.beat_id, new.purpose) == ("e001", 6, "edit:6@620-680", "b2", "replaces c004")
    assert frames_of(out.timeline, ctx)[3] == ("e001", 620, 680, "dissolve", 10)
    assert out.touched == {"c004", "e001"} and out.detail == "replace c004 (shot 3) with e001 (shot 6, frames 620-680)"
    assert sorted(changes(tl, out.timeline, ctx)) == ["added e001 (shot 6, frames 620-680, incoming dissolve 10f)",
                                                       "removed c004 (shot 3, frames 320-380)"]
    assert problems(tl, out.timeline, out.touched, ctx) == []


def test_replace_with_a_start_and_a_length_and_unique_keys(tl, ctx):
    out = run(tl, ctx, op="replace", clip="c003", shot=6, in_frame=605, frames=30)
    assert frames_of(out.timeline, ctx)[2] == ("e001", 605, 635, "cut", 0)
    twice = apply_ops(tl, [{"op": "replace", "clip": "c003", "shot": 6}, {"op": "replace", "clip": "c005", "shot": 6}], ctx).timeline
    assert [(c.id, c.origin) for c in twice.clips if c.id.startswith("e")] == [("e001", "edit:6@625-675"), ("e002", "edit:6@625-675#2")]
    refused(tl, ctx, "no shot 99", op="replace", clip="c003", shot=99)
    refused(tl, ctx, "shot 6 has 100 frames, fewer than the 120 needed", op="replace", clip="c003", shot=6, frames=120)
    refused(tl, ctx, "would leave shot 6", op="replace", clip="c003", shot=6, in_frame=680)
    refused(tl, ctx, "below the minimum clip length", op="replace", clip="c003", shot=6, frames=10)
    refused(tl, ctx, "frames must be at least 1", op="replace", clip="c003", shot=6, frames=0)


def test_new_ids_are_the_first_free_and_never_one_the_history_names(tl, ctx):
    assert new_clip_id(tl) == "e001"
    tl.history.append(Decision("agent", "insert", ["e001"], "insert e001"))
    assert new_clip_id(tl) == "e002"
    assert run(tl, ctx, op="insert", shot=6, in_frame=600, out_frame=640, after="c001").timeline.clips[1].id == "e002"


def test_insert_puts_a_new_clip_with_a_cut_before_or_after_a_clip(tl, ctx):
    out = run(tl, ctx, op="insert", shot=6, in_frame=600, out_frame=640, after="c002")
    assert [c.id for c in out.timeline.clips] == ["c001", "c002", "e001", "c003", "c004", "c005"]
    e = out.timeline.clips[2]
    assert (e.beat_id, e.origin, e.transition_in.is_cut, e.purpose) == ("b1", "edit:6@600-640", True, "inserted")
    assert out.touched == {"e001"} and out.detail == "insert e001 (shot 6, frames 600-640) after c002"
    assert running_frames(frame_rows(out.timeline, ctx.rate)) == running_frames(frame_rows(tl, ctx.rate)) + 40
    out = run(tl, ctx, op="insert", shot=6, in_frame=600, out_frame=640, before="c001")
    assert out.timeline.clips[0].id == "e001" and out.timeline.clips[1].transition_in.is_cut
    # the clip after the new one keeps its own incoming transition
    out = run(tl, ctx, op="insert", shot=6, in_frame=600, out_frame=640, before="c002")
    assert frames_of(out.timeline, ctx)[2] == ("c002", 110, 170, "dissolve", 8)
    refused(tl, ctx, "exactly one of before / after", op="insert", shot=6, in_frame=600, out_frame=640)
    refused(tl, ctx, "exactly one of before / after", op="insert", shot=6, in_frame=600, out_frame=640, before="c001", after="c002")
    refused(tl, ctx, "below the minimum", op="insert", shot=6, in_frame=600, out_frame=610, after="c001")
    refused(tl, ctx, "e001 would leave shot 6", op="insert", shot=6, in_frame=590, out_frame=640, after="c001")
    refused(tl, ctx, "no shot 42", op="insert", shot=42, in_frame=0, out_frame=40, after="c001")
    refused(tl, ctx, "no clip c009", op="insert", shot=6, in_frame=600, out_frame=640, after="c009")


def test_remove_ripples_and_the_new_first_clip_loses_its_transition(tl, ctx):
    out = run(tl, ctx, op="remove", clip="c003")
    assert [c.id for c in out.timeline.clips] == ["c001", "c002", "c004", "c005"] and out.touched == {"c003"}
    out = apply_ops(tl, [{"op": "remove", "clip": "c001"}], ctx)
    assert out.timeline.clips[0].id == "c002" and out.timeline.clips[0].transition_in == Transition()
    assert out.touched == {"c001", "c002"}
    assert changes(tl, out.timeline, ctx) == ["removed c001 (shot 0, frames 10-60)", "c002: incoming dissolve 8f dropped (it is the first clip now)"]
    one = cut(ctx, [("c001", 0, 10, 60)])
    refused(one, ctx, "cannot remove the only clip", op="remove", clip="c001")


def test_move_keeps_the_clip_transition_and_first_clip_handling(tl, ctx):
    out = run(tl, ctx, op="move", clip="c004", before="c002")
    assert [c.id for c in out.timeline.clips] == ["c001", "c004", "c002", "c003", "c005"]
    assert frames_of(out.timeline, ctx)[1] == ("c004", 320, 380, "dissolve", 10) and out.touched == {"c004"}
    assert changes(tl, out.timeline, ctx) == ["order: c001 c004 c002 c003 c005"]  # its dissolve came with it
    out = run(tl, ctx, op="move", clip="c002", before="c001")
    assert out.timeline.clips[0].id == "c002" and out.timeline.clips[0].transition_in.is_cut and out.touched == {"c002"}
    out = run(tl, ctx, op="move", clip="c001", after="c003")
    assert out.timeline.clips[0].id == "c002" and out.timeline.clips[0].transition_in.is_cut and out.touched == {"c001", "c002"}
    assert run(tl, ctx, op="move", clip="c002", after="c001").detail == "move c002 after c001 (it is there already)"
    refused(tl, ctx, "cannot move c002 before itself", op="move", clip="c002", before="c002")
    refused(tl, ctx, "exactly one of before / after", op="move", clip="c002")


# -- transitions -----------------------------------------------------------------------------------------------------------------
def test_set_transition_kinds_and_lengths(tl, ctx):
    out = run(tl, ctx, op="set_transition", junction=2, kind="dissolve", frames=6)
    assert frames_of(out.timeline, ctx)[2] == ("c003", 220, 270, "dissolve", 6) and out.touched == {"c003"}
    assert out.detail == "junction 2 (c002|c003): cut -> dissolve 6f"
    assert out.timeline.clips[2].transition_in.duration == 0.24
    out = run(tl, ctx, op="set_transition", junction=1, kind="cut")
    assert out.timeline.clips[1].transition_in == Transition() and out.detail == "junction 1 (c001|c002): dissolve 8f -> cut"
    assert run(tl, ctx, op="set_transition", junction=1, kind="cut", frames=0).timeline.clips[1].transition_in.is_cut
    out = run(tl, ctx, op="set_transition", junction=4, kind="dip_to_black", frames=4)
    assert frames_of(out.timeline, ctx)[4][3:] == ("dip_to_black", 4)
    same = run(tl, ctx, op="set_transition", junction=1, kind="dissolve", frames=8)
    assert same.touched == frozenset() and "already dissolve 8f" in same.detail
    refused(tl, ctx, "unknown transition 'wipe'", op="set_transition", junction=1, kind="wipe", frames=4)
    refused(tl, ctx, "a dissolve needs at least 1 frame", op="set_transition", junction=1, kind="dissolve", frames=0)
    refused(tl, ctx, "set_transition needs frames", op="set_transition", junction=1, kind="dissolve")
    refused(tl, ctx, "a cut has no frames", op="set_transition", junction=1, kind="cut", frames=3)


def test_set_transition_limit_is_a_fraction_of_the_shorter_clip(tl, ctx):
    # c002 60 frames, c003 50: at most 40% of 50 = 20 frames
    assert run(tl, ctx, op="set_transition", junction=2, kind="dissolve", frames=20).touched == {"c003"}
    refused(tl, ctx, "a 21-frame dissolve at junction 2 (c002|c003) is longer than 40% of the shorter clip (20 frames)",
            op="set_transition", junction=2, kind="dissolve", frames=21)


def test_a_transition_already_over_the_limit_may_only_get_shorter(ctx):
    tl = cut(ctx, [("c001", 0, 10, 60), ("c002", 1, 110, 140, "b1", ("dissolve", 15))])  # 15 > 40% of 30 = 12
    assert frames_of(run(tl, ctx, op="set_transition", junction=1, kind="dissolve", frames=13).timeline, ctx)[1][4] == 13
    refused(tl, ctx, "longer than 40%", op="set_transition", junction=1, kind="dissolve", frames=16)


# -- locks -----------------------------------------------------------------------------------------------------------------------
def test_match_cut_edges_are_held(tl, ctx):
    mc = match_cut(tl, 2)  # c003|c004
    assert lock_reason(mc.clips[2], "out") == "the out-point of c003 is held by a match cut" and lock_reason(mc.clips[2], "in") == ""
    refused(mc, ctx, "the out-point of c003 is held by a match cut", op="trim", clip="c003", edge="out", frames=-2)
    refused(mc, ctx, "the in-point of c004 is held by a match cut", op="trim", clip="c004", edge="in", frames=2)
    refused(mc, ctx, "held by a match cut", op="roll", junction=3, frames=1)
    refused(mc, ctx, "held by a match cut", op="slip", clip="c004", frames=1)
    refused(mc, ctx, "held by a match cut", op="replace", clip="c003", shot=6)
    assert run(mc, ctx, op="trim", clip="c003", edge="in", frames=2).touched == {"c003"}  # the other edge is free


def test_a_match_cut_stays_a_hard_cut(tl, ctx):
    mc = match_cut(tl, 2)
    refused(mc, ctx, "junction 3 (c003|c004) is a match cut: match cuts stay hard cuts", op="set_transition", junction=3, kind="dissolve", frames=4)
    mc.clips[3].transition_in = Transition()
    refused(mc, ctx, "match cuts stay hard cuts", op="set_transition", junction=3, kind="dip_to_black", frames=4)
    assert run(mc, ctx, op="set_transition", junction=3, kind="cut").touched == frozenset()


def test_pinned_edges_are_held(tl, ctx):
    pinned, p = pin(tl, ctx, 1)  # c002|c003
    pc = with_pins(ctx, [p])
    refused(pinned, pc, "the out-point of c002 is held by an editor pin", op="trim", clip="c002", edge="out", frames=-2)
    refused(pinned, pc, "the out-point of c002 is held by an editor pin", op="roll", junction=2, frames=1)
    refused(pinned, pc, "the in-point of c003 is held by an editor pin", op="trim", clip="c003", edge="in", frames=1)
    refused(pinned, pc, "held by an editor pin", op="slip", clip="c003", frames=1)
    assert run(pinned, pc, op="trim", clip="c002", edge="in", frames=2).touched == {"c002"}


def test_an_edge_locked_without_a_reason_is_still_held(tl, ctx):
    tl.clips[2].locked_in = True
    refused(tl, ctx, "the in-point of c003 is locked", op="trim", clip="c003", edge="in", frames=1)


def test_decision_2_a_pinned_junction_may_change_its_transition(tl, ctx):
    pinned, p = pin(tl, ctx, 1)
    pc = with_pins(ctx, [p])
    out = run(pinned, pc, op="set_transition", junction=2, kind="dissolve", frames=6)
    assert frames_of(out.timeline, pc)[2][3:] == ("dissolve", 6)
    assert problems(pinned, out.timeline, out.touched, pc) == []


def test_decision_2_a_protected_transition_stays(tl, ctx):
    pinned, p = pin(tl, ctx, 0, hold_transition=True, held_transition={"kind": "dissolve", "frames": 8})  # c001|c002, a dissolve of 8
    pc = with_pins(ctx, [p])
    refused(pinned, pc, "the transition at junction 1 (c001|c002) is protected by the editor pin s0->s1", op="set_transition", junction=1,
            kind="cut")
    refused(pinned, pc, "protected", op="set_transition", junction=1, kind="dissolve", frames=6)
    assert run(pinned, pc, op="set_transition", junction=1, kind="dissolve", frames=8).touched == frozenset()  # unchanged: fine
    # another junction is free
    assert run(pinned, pc, op="set_transition", junction=2, kind="dissolve", frames=6).touched == {"c003"}


# -- snap to beat ----------------------------------------------------------------------------------------------------------------
@pytest.fixture
def beats():
    """Beats every 0.4 s (10 frames at 25 fps), downbeats every 1.6 s (40 frames)."""
    return click(beat=0.4, per_bar=4)


def snap_cut(ctx):
    return cut(ctx, [("c001", 0, 0, 43, "b1"), ("c002", 1, 110, 160, "b1"), ("c003", 2, 220, 270, "b2"), ("c004", 3, 300, 350, "b2")])


def test_snap_rolls_a_junction_onto_the_nearest_beat(beats):
    ctx = ctx_for(R25, music=beats)
    tl = snap_cut(ctx)  # junction 1 at frame 43: 3 frames after the beat at 40
    out = run(tl, ctx, op="snap_to_beat", junction=1)
    assert out.detail == "junction 1 (c001|c002) -3f onto the beat at 1.600s (roll)" and out.touched == {"c001", "c002"}
    assert frames_of(out.timeline, ctx)[:2] == [("c001", 0, 40, "cut", 0), ("c002", 107, 160, "cut", 0)]
    assert [r.rec for r in frame_rows(out.timeline, ctx.rate)][2:] == [r.rec for r in frame_rows(tl, ctx.rate)][2:]  # nothing else moved


def test_snap_ripple_trims_the_outgoing_clip(beats):
    ctx = ctx_for(R25, music=beats)
    tl = snap_cut(ctx)
    out = run(tl, ctx, op="snap_to_beat", junction=1, mode="ripple")
    assert frames_of(out.timeline, ctx)[:2] == [("c001", 0, 40, "cut", 0), ("c002", 110, 160, "cut", 0)] and out.touched == {"c001"}
    assert [r.rec for r in frame_rows(out.timeline, ctx.rate)][1:] == [r.rec - 3 for r in frame_rows(tl, ctx.rate)][1:]


def test_snap_prefers_a_downbeat_at_a_story_beat_change(beats):
    ctx = ctx_for(R25, music=beats, window=0.6)
    tl = snap_cut(ctx)  # junction 2 (b1 -> b2) at frame 93: beat at 90, downbeat at 80
    assert "-13f onto the downbeat at 3.200s" in run(tl, ctx, op="snap_to_beat", junction=2).detail
    ctx = ctx_for(R25, music=beats, window=0.6, downbeats=False)
    assert "-3f onto the beat at 3.600s" in run(tl, ctx, op="snap_to_beat", junction=2).detail
    ctx = ctx_for(R25, music=beats)  # the downbeat is outside the default 0.35 s window: the nearest beat
    assert "-3f onto the beat at 3.600s" in run(tl, ctx, op="snap_to_beat", junction=2).detail
    assert "onto the downbeat" in run(tl, ctx, op="snap_to_beat", junction=2, window_s=0.6).detail


def test_snap_on_the_beat_already_changes_nothing(beats):
    ctx = ctx_for(R25, music=beats)
    tl = cut(ctx, [("c001", 0, 0, 40), ("c002", 1, 110, 160)])
    out = run(tl, ctx, op="snap_to_beat", junction=1)
    assert out.touched == frozenset() and out.detail == "junction 1 (c001|c002) is already on the beat at 1.600s"
    assert frames_of(out.timeline, ctx) == frames_of(tl, ctx)


def test_snap_honours_the_music_start_and_refusals(beats):
    ctx = ctx_for(R25, music=beats, music_start=0.12)  # the cut starts 3 frames into the track: junction 1 at frame 46 of the track
    out = run(snap_cut(ctx), ctx, op="snap_to_beat", junction=1)
    assert "+4f onto the beat at 2.000s" in out.detail  # 1.84 s -> 2.0 s
    no_music = ctx_for(R25)
    refused(snap_cut(no_music), no_music, "this workspace has no music track", op="snap_to_beat", junction=1)
    sparse = ctx_for(R25, music=click(beat=4.0))
    refused(snap_cut(sparse), sparse, "no beat within 0.35s of junction 1", op="snap_to_beat", junction=1)
    refused(snap_cut(ctx), ctx, "mode must be roll or ripple", op="snap_to_beat", junction=1, mode="slide")
    refused(snap_cut(ctx), ctx, "window_s must be a number", op="snap_to_beat", junction=1, window_s="wide")
    refused(snap_cut(ctx), ctx, "window_s must be a number", op="snap_to_beat", junction=1, window_s=-1)


def test_snap_at_a_pinned_junction_is_refused(beats):
    ctx = ctx_for(R25, music=beats)
    pinned, p = pin(snap_cut(ctx), ctx, 0)
    refused(pinned, with_pins(ctx, [p]), "the out-point of c001 is held by an editor pin", op="snap_to_beat", junction=1)


# -- batches ---------------------------------------------------------------------------------------------------------------------
def test_apply_ops_is_all_or_nothing_and_names_the_refused_op(tl, ctx):
    with pytest.raises(Refused) as exc:
        apply_ops(tl, [{"op": "roll", "junction": 3, "frames": 1}, {"op": "trim", "clip": "c999", "edge": "in", "frames": 1}], ctx)
    assert str(exc.value) == "op 2 (trim): no clip c999" and exc.value.index == 1
    with pytest.raises(Refused, match="op 1: an operation is an object"):
        apply_ops(tl, ["trim"], ctx)
    for bad in ("trim", {"op": "trim"}, 3, None):
        with pytest.raises(Refused, match="ops must be a list"):
            apply_ops(tl, bad, ctx)
    res = apply_ops(tl, [{"op": "roll", "junction": 3, "frames": 1}, {"op": "slip", "clip": "c005", "frames": 2}], ctx)
    assert res.touched == {"c003", "c004", "c005"} and res.details == ("roll junction 3 (c003|c004) +1f", "slip c005 +2f")
    empty = apply_ops(tl, [], ctx)
    assert empty.timeline is not tl and empty.timeline.to_dict() == tl.to_dict() and empty.touched == frozenset()


def test_snap_is_idempotent_when_the_beat_falls_between_two_frames():
    ctx = ctx_for(R25, music=click(beat=0.5, per_bar=4))  # beats at 12.5-frame steps: every other beat falls between two frames
    from matchcut.edit.rules import describe
    for out_f in range(46, 66):
        tl = cut(ctx, [("c001", 0, 0, out_f), ("c002", 1, 110, 170)])
        first = run(tl, ctx, op="snap_to_beat", junction=1, window_s=0.5)
        assert describe(first.timeline, ctx)["junctions"][0]["beat_offset_frames"] == 0, (out_f, first.detail)
        again = run(first.timeline, ctx, op="snap_to_beat", junction=1, window_s=0.5)
        assert again.touched == frozenset() and "already on the beat" in again.detail


@pytest.mark.parametrize("rate", [R25, R2997, R23976, R5994], ids=["25", "29.97", "23.976", "59.94"])
def test_operations_are_exact_in_frames_at_every_rate(rate):
    from matchcut.interchange.common import layout
    ctx = ctx_for(rate, frames_per_shot=300)
    m = ctx.min_frames
    tl = cut(ctx, [("c001", 0, 7, 7 + 2 * m), ("c002", 1, 311, 311 + 3 * m, "b1", ("dissolve", 5)), ("c003", 2, 613, 613 + 2 * m)])
    ops = [{"op": "trim", "clip": "c001", "edge": "out", "frames": 7}, {"op": "roll", "junction": 2, "frames": -3},
           {"op": "slip", "clip": "c003", "frames": 11}, {"op": "set_transition", "junction": 2, "kind": "dip_to_black", "frames": 9}]
    out = apply_ops(tl, ops, ctx).timeline
    assert frames_of(out, ctx) == [("c001", 7, 14 + 2 * m, "cut", 0), ("c002", 311, 308 + 3 * m, "dissolve", 5),
                                   ("c003", 621, 624 + 2 * m, "dip_to_black", 9)]
    laid = layout(out, rate)
    assert [(r.in_f, r.out_f, r.tr) for r in laid] == [(x[1], x[2], x[4]) for x in frames_of(out, ctx)]
    assert all(c.source_in == ctx.s(r.in_f) and c.source_out == ctx.s(r.out_f) for c, r in zip(out.clips[:1], laid[:1]))


def test_new_ids_skip_the_reserved_ids_of_the_session(tl, ctx):
    one = run(tl, ctx, op="insert", shot=6, in_frame=600, out_frame=640, after="c001").timeline
    gone = apply_op(one, {"op": "remove", "clip": "e001"}, ctx).timeline
    assert apply_op(gone, {"op": "insert", "shot": 6, "in_frame": 650, "out_frame": 690, "after": "c001"}, ctx).timeline.clips[1].id == "e001"
    again = apply_op(gone, {"op": "insert", "shot": 6, "in_frame": 650, "out_frame": 690, "after": "c001"}, ctx, reserved_ids={"e001"})
    assert again.timeline.clips[1].id == "e002"
    assert apply_op(tl, {"op": "replace", "clip": "c003", "shot": 6}, ctx, reserved_ids=["e001", "e002"]).timeline.clips[2].id == "e003"
    assert new_clip_id(tl, {"e001", "e003"}) == "e002"
    # within one batch an id is never issued twice
    batch = apply_ops(tl, [{"op": "insert", "shot": 6, "in_frame": 600, "out_frame": 640, "after": "c001"}, {"op": "remove", "clip": "e001"},
                           {"op": "insert", "shot": 6, "in_frame": 650, "out_frame": 690, "after": "c001"}], ctx)
    assert batch.timeline.clips[1].id == "e002" and changes(tl, batch.timeline, ctx) == ["added e002 (shot 6, frames 650-690)"]
    assert apply_ops(gone, [{"op": "insert", "shot": 6, "in_frame": 650, "out_frame": 690, "after": "c001"}], ctx,
                     reserved_ids=("e001",)).timeline.clips[1].id == "e002"
