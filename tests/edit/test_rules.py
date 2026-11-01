"""Proposal-wide rules (whichever operation caused a change), guards, the timing and change reports, and describe."""
from __future__ import annotations

import pytest

from matchcut.edit.model import EditContext, frame_rows
from matchcut.edit.ops import apply_ops
from matchcut.edit.rules import GUARDS, changes, describe, guard_problems, problems, timing
from matchcut.pipeline.timeline import Clip, Transition

from .util import R25, click, ctx_for, cut, match_cut, pin, with_pins

BASE = [("c001", 0, 10, 60, "b1"), ("c002", 1, 110, 170, "b1", ("dissolve", 8)), ("c003", 2, 220, 270, "b2"),
        ("c004", 3, 320, 380, "b2", ("dissolve", 10)), ("c005", 4, 420, 470, "b3")]


@pytest.fixture
def ctx():
    return ctx_for(R25)


@pytest.fixture
def tl(ctx):
    return cut(ctx, BASE)


def propose(tl, ctx, ops, guards=None):
    res = apply_ops(tl, ops, ctx)
    return res, problems(tl, res.timeline, res.touched, ctx, guards)


def test_a_clean_edit_has_no_problems(tl, ctx):
    for ops in ([{"op": "trim", "clip": "c003", "edge": "out", "frames": 5}], [{"op": "roll", "junction": 3, "frames": -4}],
                [{"op": "remove", "clip": "c001"}, {"op": "remove", "clip": "c002"}], [{"op": "move", "clip": "c005", "before": "c001"}],
                [{"op": "insert", "shot": 6, "in_frame": 600, "out_frame": 650, "after": "c005"},
                 {"op": "set_transition", "junction": 5, "kind": "dip_to_black", "frames": 6}]):
        assert propose(tl, ctx, ops)[1] == [], ops


# -- untouched clips and junctions -------------------------------------------------------------------------------------------------
def test_an_untouched_clip_that_changed_is_a_problem(tl, ctx):
    after = tl.copy()
    after.clips[3].source_out = ctx.s(379)
    assert problems(tl, after, {"c003"}, ctx) == ["c004 changed but no operation touched it",
                                                  "junction c004|c005 changed in frames but no operation touched it"]
    after = tl.copy()
    after.clips[4].tags.append("x")
    assert problems(tl, after, set(), ctx) == ["c005 changed but no operation touched it"]


def test_an_untouched_junction_moved_by_rounding_is_a_problem(ctx):
    # transitions not on whole frames (an old workspace): layout rounds them over the running total, so removing one moves the other
    tl = cut(ctx, [("c001", 0, 10, 60), ("c002", 1, 110, 170), ("c003", 2, 220, 270), ("c004", 3, 320, 380)])
    tl.clips[1].transition_in = Transition("dissolve", 0.336)  # 8.4 frames
    tl.clips[3].transition_in = Transition("dissolve", 0.336)
    assert [r.tr for r in frame_rows(tl, ctx.rate)] == [0, 8, 0, 9]
    res, probs = propose(tl, ctx, [{"op": "set_transition", "junction": 1, "kind": "cut"}])
    assert [r.tr for r in frame_rows(res.timeline, ctx.rate)] == [0, 0, 0, 8]
    assert probs == ["junction c003|c004 changed in frames but no operation touched it"]


# -- shot bounds, lengths, validate -----------------------------------------------------------------------------------------------
def test_bounds_and_lengths_count_only_new_problems(tl, ctx):
    after = tl.copy()
    after.clips[2].source_out = ctx.s(301)
    assert problems(tl, after, {"c003"}, ctx) == ["c003 lies outside shot 2 (frames 220-301; the shot is frames 200-300)"]
    after.clips[2].source_out, after.clips[2].source_in = ctx.s(270), ctx.s(255)
    assert "c003 is 15 frames, below the minimum clip length (19 frames)" in problems(tl, after, {"c003"}, ctx)
    # a clip the pipeline left under the minimum and outside its shot: tolerated while it gets no worse
    tight = EditContext(ctx.rate, {**ctx.bounds, 2: (9.0, 12.0)})
    base = cut(tight, [("c001", 0, 10, 60), ("c002", 1, 110, 125), ("c003", 2, 220, 270)])
    after = base.copy()
    after.clips[1].source_out = ctx.s(127)
    assert problems(base, after, {"c002"}, tight) == []
    after.clips[1].source_in = ctx.s(113)
    assert problems(base, after, {"c002"}, tight) == ["c002 is 14 frames, below the minimum clip length (19 frames)"]
    after = base.copy()
    after.clips[2].source_in = ctx.s(219)
    assert problems(base, after, {"c003"}, tight) == ["c003 lies outside shot 2 (frames 219-270; the shot is frames 225-300)"]


def test_validate_problems_are_counted_when_new(tl, ctx):
    after = tl.copy()
    after.clips[4].id = "c004"
    assert "duplicate clip id c004" in problems(tl, after, {"c005", "c004"}, ctx)
    after = tl.copy()
    after.clips.append(Clip("e001", 42, 0.0, 2.0))
    assert "e001: unknown shot 42" in problems(tl, after, {"e001"}, ctx)


# -- transitions -----------------------------------------------------------------------------------------------------------------
def test_the_transition_limit_holds_after_a_trim(tl, ctx):
    _, probs = propose(tl, ctx, [{"op": "trim", "clip": "c003", "edge": "in", "frames": 26}])  # c003 24 frames: at most 9.6
    assert probs == ["the 10-frame dissolve at c003|c004 is longer than 40% of the shorter clip (9 frames)"]


def test_the_transition_limit_holds_after_a_move(ctx):
    tl = cut(ctx, [("c001", 0, 10, 60), ("c002", 1, 110, 160, "b1", ("dissolve", 10)), ("c003", 2, 220, 240), ("c004", 3, 320, 370)])
    assert problems(tl, tl, set(), ctx) == []
    _, probs = propose(tl, ctx, [{"op": "move", "clip": "c003", "before": "c002"}])  # c003 (20 frames) now meets c002's dissolve
    assert probs == ["the 10-frame dissolve at c003|c002 is longer than 40% of the shorter clip (8 frames)"]


def test_the_transition_limit_holds_after_a_replace_and_a_roll(tl, ctx):
    _, probs = propose(tl, ctx, [{"op": "replace", "clip": "c004", "shot": 6, "frames": 20}])  # keeps its 10-frame dissolve
    assert probs == ["the 10-frame dissolve at c003|e001 is longer than 40% of the shorter clip (8 frames)"]
    _, probs = propose(tl, ctx, [{"op": "roll", "junction": 2, "frames": 26}])  # c003 24 frames
    assert probs == ["the 10-frame dissolve at c003|c004 is longer than 40% of the shorter clip (9 frames)"]


def test_a_transition_already_over_the_limit_is_tolerated_but_may_not_get_worse(ctx):
    tl = cut(ctx, [("c001", 0, 10, 60), ("c002", 1, 110, 140, "b1", ("dissolve", 15))])  # 15 > 12
    assert propose(tl, ctx, [{"op": "trim", "clip": "c001", "edge": "in", "frames": 2}])[1] == []
    assert propose(tl, ctx, [{"op": "trim", "clip": "c002", "edge": "out", "frames": 2}])[1] == []  # 32 frames: the excess shrinks
    assert propose(tl, ctx, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -2}])[1] == [
        "the 15-frame dissolve at c001|c002 is longer than 40% of the shorter clip (11 frames)"]


def test_transitions_must_be_whole_frames_and_the_first_clip_has_none(tl, ctx):
    after = tl.copy()
    after.clips[2].transition_in = Transition("dissolve", 0.25)  # 6.25 frames
    assert "the dissolve into c003 is not a whole number of frames (0.25s)" in problems(tl, after, {"c003"}, ctx)
    after = tl.copy()
    after.clips[0].transition_in = Transition("dissolve", 0.32)
    assert problems(tl, after, {"c001"}, ctx) == ["the first clip c001 has an incoming dissolve"]


def test_a_cut_layout_refuses_is_a_problem():
    ctx = ctx_for(R25, min_clip=0.1, frac=1.0)  # no transition limit: only the layout stops it
    tl = cut(ctx, [("c001", 0, 10, 60), ("c002", 1, 110, 120, "b1", ("dissolve", 8)), ("c003", 2, 220, 270, "b1", ("dissolve", 8))])
    assert problems(tl, tl, set(), ctx) == []
    _, probs = propose(tl, ctx, [{"op": "trim", "clip": "c002", "edge": "in", "frames": 2}])
    assert probs == ["c002 is no longer than its transitions (8 frames, 8 of them in transitions): the cut cannot be laid out"]


# -- pins (decision 2) -----------------------------------------------------------------------------------------------------------
@pytest.fixture
def pinned(tl, ctx):
    """c002|c003 pinned; the context knows the pin."""
    out, p = pin(tl, ctx, 1)
    return out, with_pins(ctx, [p])


def test_removing_or_moving_a_pinned_clip_breaks_the_pin(pinned):
    tl, ctx = pinned
    assert propose(tl, ctx, [{"op": "remove", "clip": "c002"}])[1] == ["breaks the editor pin s1->s2: clip removed (shot 1)"]
    assert propose(tl, ctx, [{"op": "move", "clip": "c003", "after": "c005"}])[1] == ["breaks the editor pin s1->s2: clips are no longer adjacent"]
    assert propose(tl, ctx, [{"op": "insert", "shot": 6, "in_frame": 600, "out_frame": 640, "after": "c002"}])[1] == [
        "breaks the editor pin s1->s2: clips are no longer adjacent"]


def test_a_pin_survives_edits_around_it(pinned):
    tl, ctx = pinned
    for ops in ([{"op": "trim", "clip": "c001", "edge": "out", "frames": -5}], [{"op": "remove", "clip": "c005"}],
                [{"op": "move", "clip": "c005", "before": "c001"}], [{"op": "trim", "clip": "c002", "edge": "in", "frames": 3}],
                [{"op": "set_transition", "junction": 2, "kind": "dissolve", "frames": 6}],
                [{"op": "set_transition", "junction": 1, "kind": "cut"}]):
        assert propose(tl, ctx, ops)[1] == [], ops


def test_a_protected_transition_is_checked_on_the_whole_proposal(tl, ctx):
    tl2, p = pin(tl, ctx, 0, hold_transition=True, held_transition={"kind": "dissolve", "frames": 8})
    pc = with_pins(ctx, [p])
    after = tl2.copy()
    after.clips[1].transition_in = Transition()
    assert problems(tl2, after, {"c002"}, pc) == ["changes the transition the editor pin s0->s1 protects (dissolve 8f -> cut)"]
    assert propose(tl2, pc, [{"op": "set_transition", "junction": 2, "kind": "dissolve", "frames": 4}])[1] == []
    unprotected = with_pins(ctx, [type(p)(**{**p.__dict__, "hold_transition": False})])
    assert problems(tl2, after, {"c002"}, unprotected) == []  # decision 2: a pinned junction may change its transition


def test_a_pin_not_held_in_before_is_not_enforced(tl, ctx):
    tl2, p = pin(tl, ctx, 1)
    moved = tl2.copy()
    moved.clips[1].source_out = ctx.s(165)  # the editor's cut no longer holds the pin
    pc = with_pins(ctx, [p])
    assert propose(moved, pc, [{"op": "remove", "clip": "c003"}])[1] == []


# -- match cuts ------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def matched(tl):
    return match_cut(tl, 2)  # c003|c004 (c004 carries a dissolve in BASE: make it a hard cut, as the match-cut pass does)


def test_insert_remove_or_move_inside_a_match_cut_breaks_it(matched, ctx):
    matched.clips[3].transition_in = Transition()
    why = "breaks the match cut c003|c004: its clips must stay adjacent, with the same cut points and a hard cut"
    assert propose(matched, ctx, [{"op": "insert", "shot": 6, "in_frame": 600, "out_frame": 640, "after": "c003"}])[1] == [why]
    assert propose(matched, ctx, [{"op": "remove", "clip": "c004"}])[1] == [why]
    assert propose(matched, ctx, [{"op": "move", "clip": "c004", "before": "c001"}])[1] == [why]
    assert propose(matched, ctx, [{"op": "move", "clip": "c003", "after": "c005"}])[1] == [why]
    assert propose(matched, ctx, [{"op": "move", "clip": "c005", "before": "c001"}])[1] == []
    after = matched.copy()
    after.clips[3].transition_in = Transition("dissolve", ctx.s(4))
    assert why in problems(matched, after, {"c004"}, ctx)


# -- guards (decision 3) ---------------------------------------------------------------------------------------------------------
def test_a_new_dissolve_moves_later_junctions_earlier_and_is_reported(tl, ctx):
    res, probs = propose(tl, ctx, [{"op": "set_transition", "junction": 2, "kind": "dissolve", "frames": 6}])
    assert probs == []
    t = timing(tl, res.timeline, ctx)
    assert t["duration_frames"] == [252, 246] and t["duration_change"] == -6
    assert t["moved"] == [{"id": "s1-s2", "clips": ["c002", "c003"], "before": 102, "after": 96, "delta": -6},
                          {"id": "s2-s3", "clips": ["c003", "c004"], "before": 142, "after": 136, "delta": -6},
                          {"id": "s3-s4", "clips": ["c004", "c005"], "before": 202, "after": 196, "delta": -6}]
    assert t["added"] == [] and t["removed"] == [] and t["beat_offsets"] is None


def test_keep_record_frames_refuses_it(tl, ctx):
    ops = [{"op": "set_transition", "junction": 2, "kind": "dissolve", "frames": 6}]
    assert propose(tl, ctx, ops, {"keep_record_frames": ["s0-s1"]})[1] == []
    assert propose(tl, ctx, ops, {"keep_record_frames": ["s3-s4", 1]})[1] == [
        "guard keep_record_frames: junction s3-s4 (c004|c005) moves from record frame 202 to 196 (-6f)"]
    assert propose(tl, ctx, [{"op": "remove", "clip": "c005"}], {"keep_record_frames": [4]})[1] == [
        "guard keep_record_frames: junction s3-s4 (c004|c005) is no longer a junction"]
    assert propose(tl, ctx, ops, {"keep_record_frames": ["s9-s9"]})[1] == [
        "guard keep_record_frames: no junction s9-s9 (junctions: s0-s1, s1-s2, s2-s3, s3-s4)"]


def test_max_duration_change(tl, ctx):
    ops = [{"op": "set_transition", "junction": 2, "kind": "dissolve", "frames": 6}]
    assert propose(tl, ctx, ops, {"max_duration_change_frames": 6})[1] == []
    assert propose(tl, ctx, ops, {"max_duration_change_frames": 5})[1] == [
        "guard max_duration_change_frames: the running time changes by -6 frames (at most 5 allowed)"]
    assert propose(tl, ctx, [{"op": "roll", "junction": 1, "frames": 3}], {"max_duration_change_frames": 0})[1] == []


def test_keep_on_beat_and_the_beat_report():
    ctx = ctx_for(R25, music=click(beat=0.4, per_bar=4))  # beats every 10 frames
    tl = cut(ctx, [("c001", 0, 10, 50), ("c002", 1, 110, 170), ("c003", 2, 220, 263), ("c004", 3, 320, 370)])  # junctions at 40, 100, 143
    ops = [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 6}]
    res, probs = propose(tl, ctx, ops, {"keep_on_beat": ["s1-s2"]})
    assert probs == ["guard keep_on_beat: junction s1-s2 (c002|c003, s1-s2 after the edit) would be 4 frames after the beat"]
    t = timing(tl, res.timeline, ctx)
    assert [(o["id"], o["before"], o["after"]) for o in t["beat_offsets"]] == [("s0-s1", 0, 4), ("s1-s2", 0, 4), ("s2-s3", 3, -3)]
    assert t["left_beat"] == ["s0-s1", "s1-s2"] and t["reached_beat"] == []
    res, probs = propose(tl, ctx, [{"op": "snap_to_beat", "junction": 3}], {"keep_on_beat": [3, 1]})
    assert probs == [] and timing(tl, res.timeline, ctx)["reached_beat"] == ["s2-s3"]
    no_music = ctx_for(R25)
    assert propose(tl, no_music, ops, {"keep_on_beat": [1]})[1] == ["guard keep_on_beat: this workspace has no music track"]


def test_malformed_and_unknown_guards(tl, ctx):
    assert guard_problems(tl, tl, ctx, None) == [] and guard_problems(tl, tl, ctx, {}) == []
    assert guard_problems(tl, tl, ctx, ["keep_on_beat"])[0].startswith("guards must be an object")
    assert guard_problems(tl, tl, ctx, {"keep_timing": True}) == [f"unknown guard 'keep_timing' (guards: {', '.join(GUARDS)})"]
    for bad in (-1, 1.5, True, "3"):
        assert guard_problems(tl, tl, ctx, {"max_duration_change_frames": bad})[0].startswith("guard max_duration_change_frames must be")
    assert guard_problems(tl, tl, ctx, {"keep_record_frames": {"a": 1}})[0].startswith("guard keep_record_frames must be a list")
    assert guard_problems(tl, tl, ctx, {"keep_record_frames": "s0-s1"}) == []  # a single junction


# -- the reports -----------------------------------------------------------------------------------------------------------------
def test_timing_lists_added_and_removed_junctions(tl, ctx):
    res = apply_ops(tl, [{"op": "remove", "clip": "c003"}], ctx)
    t = timing(tl, res.timeline, ctx)
    assert t["added"] == [{"id": "s1-s3", "clips": ["c002", "c004"], "record_frame": 92}]
    assert [r["id"] for r in t["removed"]] == ["s1-s2", "s2-s3"]
    assert t["duration_change"] == -50


def test_changes_by_clip_id(tl, ctx):
    res = apply_ops(tl, [{"op": "set_transition", "junction": 3, "kind": "cut"}, {"op": "move", "clip": "c004", "before": "c003"},
                         {"op": "remove", "clip": "c005"}, {"op": "slip", "clip": "c003", "frames": 2},
                         {"op": "insert", "shot": 6, "in_frame": 600, "out_frame": 640, "after": "c003"},
                         {"op": "trim", "clip": "c001", "edge": "in", "frames": 2}, {"op": "trim", "clip": "c002", "edge": "out", "frames": -3}], ctx)
    got = changes(tl, res.timeline, ctx)
    assert set(got) == {"removed c005 (shot 4, frames 420-470)", "c001: in +2f", "c002: out -3f", "c002|c004: dissolve 10f -> cut",
                        "c003: slipped +2f", "added e001 (shot 6, frames 600-640)", "order: c001 c002 c004 c003"}
    assert changes(tl, tl.copy(), ctx) == []


def test_describe(tl, ctx):
    tl2, p = pin(match_cut(tl, 3), ctx, 0, hold_transition=True)  # c004|c005 a match cut, c001|c002 pinned and protected
    d = describe(tl2, with_pins(ctx, [p]))
    assert (d["rate"], d["fps"], d["duration_frames"]) == ("25", 25.0, 252)
    c = d["clips"][1]
    assert c == {"id": "c002", "key": tl.clips[1].origin, "shot": 1, "beat": "b1", "source_frames": [110, 170], "frames": 60,
                 "record_frames": [42, 102], "transition_in": {"kind": "dissolve", "frames": 8},
                 "locks": {"in": "the in-point of c002 is held by an editor pin"}, "purpose": ""}
    assert d["clips"][3]["locks"] == {"out": "the out-point of c004 is held by a match cut"}
    j = d["junctions"]
    assert [x["id"] for x in j] == ["s0-s1", "s1-s2", "s2-s3", "s3-s4"] and [x["n"] for x in j] == [1, 2, 3, 4]
    assert j[0] == {"n": 1, "id": "s0-s1", "out": "c001", "in": "c002", "record_frame": 42, "transition": {"kind": "dissolve", "frames": 8},
                    "beat_change": False, "match_cut": False, "pinned": True, "pin": "s0->s1", "hold_transition": True,
                    "beat_offset_frames": None}
    assert j[1]["beat_change"] and not j[1]["pinned"] and j[3]["match_cut"] and not j[3]["hold_transition"]
    with_music = describe(tl, ctx_for(R25, music=click(beat=0.4)))
    assert [x["beat_offset_frames"] for x in with_music["junctions"]] == [2, 2, 2, 2]
