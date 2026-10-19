"""Editorial pins: storage, application before the passes, prompt constraints, regeneration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines
from matchcut.pipeline.editorial import Pin, PinStore, apply_pins, pin_constraints, pin_outcome
from matchcut.pipeline.passes import PacingPass, PolishPass, run_passes
from matchcut.pipeline.schemas import PolishEdit
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Timeline, Transition, clip_origin
from tests.pipeline.conftest import RecordingMock

BOUNDS = {0: (0.0, 2.5), 1: (2.5, 4.5), 2: (4.5, 6.0)}


def cut() -> Timeline:
    return Timeline(source="s.mp4", fps=25.0, clips=[Clip("c001", 0, 0.5, 2.0, "b1"), Clip("c002", 2, 4.6, 5.8, "b1"), Clip("c003", 1, 2.6, 4.0, "b2")])


def test_pin_store_round_trip_replace_and_remove(tmp_path):
    store = PinStore(tmp_path)
    assert store.load() == [] and store.get(0, 2) is None
    store.put(Pin(0, 2, 2.0, 4.6))
    store.put(Pin(3, 4, 7.5, 8.2))
    store.put(Pin(0, 2, 1.9, 4.7, origin="reverted"))
    pins = PinStore(tmp_path).load()
    assert [(p.out_shot, p.in_shot, p.out_time, p.origin) for p in pins] == [(0, 2, 1.9, "reverted"), (3, 4, 7.5, "locked")]
    assert store.get(3, 4).in_time == 8.2
    assert store.remove(0, 2) is True and store.remove(0, 2) is False
    assert [p.key for p in store.load()] == [(3, 4)]


def test_pin_store_tolerates_a_corrupt_file(tmp_path):
    (tmp_path / "editorial.json").write_text("{oops")
    assert PinStore(tmp_path).load() == []


def test_apply_pins_sets_locks_and_records_the_junction():
    source = cut()
    out, decisions = apply_pins(source, [Pin(0, 2, 2.2, 4.8)], BOUNDS, min_clip=0.75)
    a, b = out.clips[0], out.clips[1]
    assert (a.source_out, b.source_in) == (2.2, 4.8)
    assert a.locked_out and b.locked_in and "pinned" in a.tags and "pinned" in b.tags
    assert [(d.pass_name, d.action, d.clip_ids) for d in decisions] == [("editor", "pinned", ["c001", "c002"])]
    assert out.history[-1].action == "pinned"
    assert source.clips[0].source_out == 2.0 and not source.clips[0].locked_out  # the input is untouched


def test_apply_pins_clamps_to_the_shots_and_the_minimum_clip():
    out, _ = apply_pins(cut(), [Pin(0, 2, 3.0, 4.0)], BOUNDS, min_clip=0.75)
    assert (out.clips[0].source_out, out.clips[1].source_in) == (2.5, 4.5)
    out, _ = apply_pins(cut(), [Pin(0, 2, 0.9, 5.5)], BOUNDS, min_clip=0.75)
    assert (out.clips[0].source_out, out.clips[1].source_in) == (1.25, 5.05)


def four() -> Timeline:
    """b1: shot0, shot2, shot3 | b2: shot1."""
    return Timeline(source="s.mp4", fps=25.0, clips=[
        Clip("c001", 0, 0.5, 2.0, "b1"), Clip("c002", 2, 4.6, 5.8, "b1"), Clip("c003", 3, 6.2, 7.6, "b1"), Clip("c004", 1, 2.6, 4.0, "b2"),
    ])


FOUR_BOUNDS = {0: (0.0, 2.5), 1: (2.5, 4.5), 2: (4.5, 6.0), 3: (6.0, 8.0)}


def test_a_pin_moves_the_incoming_clip_next_to_the_outgoing_one():
    out, decisions = apply_pins(four(), [Pin(0, 3, 2.0, 6.2)], FOUR_BOUNDS, min_clip=0.5)
    assert [c.shot_index for c in out.clips] == [0, 3, 2, 1]
    assert [d.action for d in decisions] == ["pin_moved", "pinned"]
    assert decisions[0].detail == "moved shot 3 next to shot 0 to hold the editor's pin"
    assert decisions[0].clip_ids == ["c003", "c001"]
    a, b = out.clips[0], out.clips[1]
    assert a.locked_out and b.locked_in and (a.source_out, b.source_in) == (2.0, 6.2)
    assert [c.beat_id for c in out.clips] == ["b1", "b1", "b1", "b2"]


def test_a_pin_moves_the_outgoing_clip_when_moving_the_incoming_one_would_split_a_beat():
    # pin shot2 -> shot1: moving shot1 (b2) after shot2 splits b1; moving shot2 (b1) to just before shot1 keeps the beats
    out, decisions = apply_pins(four(), [Pin(2, 1, 5.8, 2.6)], FOUR_BOUNDS, min_clip=0.5)
    assert [c.shot_index for c in out.clips] == [0, 3, 2, 1]
    assert [d.action for d in decisions] == ["pin_moved", "pinned"]
    assert decisions[0].detail == "moved shot 2 next to shot 1 to hold the editor's pin"


def test_a_pin_that_would_reorder_the_beats_is_not_placed():
    out, decisions = apply_pins(cut(), [Pin(1, 0, 3.0, 1.0)], BOUNDS, min_clip=0.75)
    assert [d.action for d in decisions] == ["pin_unplaced"]
    assert decisions[0].detail == "moving shot 0 would split or reorder the beats"
    assert [c.id for c in out.clips] == ["c001", "c002", "c003"]


def test_a_pin_on_a_missing_shot_is_not_placed():
    _, decisions = apply_pins(cut(), [Pin(0, 7, 2.0, 1.0)], BOUNDS, min_clip=0.75)
    assert [(d.action, d.detail) for d in decisions] == [("pin_unplaced", "shot 7 is not in this cut")]


def test_a_clip_held_by_an_earlier_pin_is_never_moved():
    # first pin holds shot0 -> shot2; the second wants shot3 right after shot0, which would need shot0's out-point
    out, decisions = apply_pins(four(), [Pin(0, 2, 2.0, 4.6), Pin(0, 3, 2.0, 6.2)], FOUR_BOUNDS, min_clip=0.5)
    assert [d.action for d in decisions] == ["pinned", "pin_unplaced"]
    assert decisions[1].detail == "shot 0 is held by another pin"
    assert [c.shot_index for c in out.clips] == [0, 2, 3, 1]


def test_a_pin_held_by_one_candidate_is_placed_by_the_other():
    # an earlier pin holds shot 3's out-point: moving shot 3 (the incoming clip) is out, moving shot 0 before it is fine
    out, decisions = apply_pins(four(), [Pin(3, 1, 7.6, 2.6), Pin(0, 3, 2.0, 6.2)], FOUR_BOUNDS, min_clip=0.5)
    assert [d.action for d in decisions] == ["pinned", "pin_moved", "pinned"]
    assert decisions[1].detail == "moved shot 0 next to shot 3 to hold the editor's pin"
    assert [c.shot_index for c in out.clips] == [2, 0, 3, 1]


def test_a_pin_whose_anchor_in_point_is_held_is_not_placed():
    # shot 2's in-point belongs to the first pin: shot 2 can neither move (candidate 1) nor be the anchor of shot 3 (candidate 2)
    out, decisions = apply_pins(four(), [Pin(0, 2, 2.0, 4.6), Pin(3, 2, 7.6, 4.6)], FOUR_BOUNDS, min_clip=0.5)
    assert [(d.action, d.detail) for d in decisions][1:] == [("pin_unplaced", "shot 2 is held by another pin")]
    assert [c.shot_index for c in out.clips] == [0, 2, 3, 1]


def test_the_first_reason_wins_when_both_candidates_fail_for_different_reasons():
    # shot 2 -> shot 1 is pinned first, so shot 1's in-point is held. Pin shot 1 -> shot 0: candidate 1 would reorder
    # the beats, candidate 2 would move the held shot 1; the reported reason is candidate 1's.
    out, decisions = apply_pins(cut(), [Pin(2, 1, 5.8, 2.6), Pin(1, 0, 3.0, 1.0)], BOUNDS, min_clip=0.5)
    assert [(d.action, d.detail) for d in decisions][1:] == [("pin_unplaced", "moving shot 0 would split or reorder the beats")]
    assert [c.id for c in out.clips] == ["c001", "c002", "c003"]


def jump_cut() -> Timeline:
    """Shot 3 twice (6.0-7.0 and 7.4-8.6) with shot 1 between the two pieces."""
    return Timeline(source="s.mp4", fps=25.0, clips=[
        Clip("c001", 3, 6.0, 7.0, "b1"), Clip("c002", 1, 2.6, 4.0, "b1"), Clip("c003", 3, 7.4, 8.6, "b1"),
    ])


def test_a_pinned_jump_cut_whose_pieces_are_apart_is_placed_by_a_move():
    out, decisions = apply_pins(jump_cut(), [Pin(3, 3, 7.0, 7.4)], FOUR_BOUNDS, min_clip=0.5)
    assert [d.action for d in decisions] == ["pin_moved", "pinned"]
    assert decisions[0].clip_ids == ["c003", "c001"]
    assert [c.id for c in out.clips] == ["c001", "c003", "c002"]
    assert (out.clips[0].source_out, out.clips[1].source_in) == (7.0, 7.4)


def test_a_pinned_jump_cut_of_a_shot_with_one_clip_is_not_placed():
    cutting = Timeline(source="s.mp4", fps=25.0, clips=[Clip("c001", 3, 6.0, 7.0, "b1"), Clip("c002", 1, 2.6, 4.0, "b1")])
    out, decisions = apply_pins(cutting, [Pin(3, 3, 7.0, 7.4)], FOUR_BOUNDS, min_clip=0.5)
    assert [(d.action, d.detail) for d in decisions] == [("pin_unplaced", "shot 3 has only one clip in this cut")]
    assert [c.id for c in out.clips] == ["c001", "c002"]


def two_pieces() -> Timeline:
    """Shot 3 as two pieces (6.0-7.4 and 7.6-9.0), then shot 4: the rough cut behind a continuity insert."""
    return Timeline(source="s.mp4", fps=25.0, clips=[
        Clip("c001", 3, 6.0, 7.4, "b1"), Clip("c002", 3, 7.6, 9.0, "b1"), Clip("c003", 4, 9.5, 11.0, "b1"),
    ])


PIECE_BOUNDS = {3: (6.0, 9.0), 4: (9.0, 12.0)}


def test_the_clip_a_pin_points_at_is_the_one_that_moves():
    out, decisions = apply_pins(two_pieces(), [Pin(4, 3, 11.0, 7.6)], PIECE_BOUNDS, min_clip=0.5)
    assert [d.action for d in decisions] == ["pin_moved", "pinned"]
    assert decisions[0].clip_ids == ["c002", "c003"]
    assert [c.id for c in out.clips] == ["c001", "c003", "c002"]  # shot 3's pieces stay in source order
    assert out.clips[2].source_in == 7.6 and out.clips[0].source_out == 7.4 and not out.clips[0].locked_in


def test_a_point_in_the_gap_between_two_pieces_selects_the_piece_whose_boundary_is_nearest():
    # 7.5 sits in the gap between the pieces: the incoming side measures to the in-points (6.0 and 7.6), so the second piece
    # is chosen. (The earlier rule chose the first piece, the earlier of two ranges 0.1 s away.)
    out, decisions = apply_pins(two_pieces(), [Pin(4, 3, 11.0, 7.5)], PIECE_BOUNDS, min_clip=0.5)
    assert decisions[0].clip_ids == ["c002", "c003"] and decisions[0].action == "pin_moved"
    assert [c.id for c in out.clips] == ["c001", "c003", "c002"]


def test_a_pair_that_is_adjacent_elsewhere_is_used_only_when_the_points_select_it():
    # shot 4 -> shot 3 is adjacent at (c002, c003) but the pin's in-point lies in c001
    cutting = Timeline(source="s.mp4", fps=25.0, clips=[
        Clip("c001", 3, 6.0, 7.4, "b1"), Clip("c002", 4, 9.5, 11.0, "b1"), Clip("c003", 3, 7.6, 9.0, "b1"),
    ])
    out, decisions = apply_pins(cutting, [Pin(4, 3, 11.0, 6.2)], PIECE_BOUNDS, min_clip=0.5)
    assert [d.action for d in decisions] == ["pin_moved", "pinned"]
    assert [c.id for c in out.clips] == ["c002", "c001", "c003"]
    assert out.clips[1].source_in == 6.2 and not out.clips[2].locked_in
    out, decisions = apply_pins(cutting, [Pin(4, 3, 11.0, 7.6)], PIECE_BOUNDS, min_clip=0.5)
    assert [d.action for d in decisions] == ["pinned"]  # the points select the adjacent pair: no move
    assert [c.id for c in out.clips] == ["c001", "c002", "c003"]


def test_a_pin_without_room_leaves_the_order_untouched_and_records_no_move():
    out, decisions = apply_pins(four(), [Pin(0, 3, 2.0, 6.2)], FOUR_BOUNDS, min_clip=2.2)
    assert [(d.action, d.detail) for d in decisions] == [("pin_unplaced", "not enough room in these clips for the pinned points")]
    assert [c.id for c in out.clips] == ["c001", "c002", "c003", "c004"]
    assert not any(d.action == "pin_moved" for d in out.history)
    assert not any(c.locked_in or c.locked_out for c in out.clips)


def test_pins_that_are_already_adjacent_are_placed_before_any_pin_that_needs_a_move():
    # the first pin would move shot 3 next to shot 0, breaking the adjacent shot 2 -> shot 3 the second pin holds
    out, decisions = apply_pins(four(), [Pin(0, 3, 2.0, 6.2), Pin(2, 3, 5.8, 6.2)], FOUR_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c002", "c003"]), ("pin_unplaced", [])]
    assert decisions[1].detail == "shot 3 is held by another pin"
    assert [c.shot_index for c in out.clips] == [0, 2, 3, 1]


def test_pin_store_skips_a_malformed_entry_and_keeps_the_rest(tmp_path):
    good = Pin(0, 2, 2.0, 4.6, note="keep")
    bad = {"out_shot": 3, "in_shot": 4, "out_time": 7.5}  # no in_time
    (tmp_path / "editorial.json").write_text(json.dumps({"pins": [good.to_dict(), bad]}))
    store = PinStore(tmp_path)
    assert store.load() == [good]
    store.put(Pin(5, 6, 1.0, 2.0))
    assert [p.key for p in PinStore(tmp_path).load()] == [(0, 2), (5, 6)]
    assert PinStore(tmp_path).get(0, 2) == good


def test_pin_constraints_name_the_shot_pairs():
    assert pin_constraints([Pin(0, 2, 2.0, 4.6)]) == ["keep shot 0 immediately followed by shot 2 (an editor-locked junction)"]


def mock_cfg(video, ws: Path) -> PipelineConfig:
    cfg = PipelineConfig(source=str(video), workspace=str(ws), target_duration=6.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    return cfg


def test_pins_reach_the_rough_cut_prompt_only(matchcut_video, tmp_path):
    cfg = mock_cfg(matchcut_video, tmp_path / "ws")
    pipe = Pipeline(cfg)
    PinStore(Path(cfg.workspace)).put(Pin(0, 2, 2.0, 4.6))
    mock = RecordingMock(cfg.llm)
    pipe.llm = mock
    pipe.rough_cut()
    assert "Editor constraints" in mock.texts["rough_cut"][-1]
    assert "keep shot 0 immediately followed by shot 2" in mock.texts["rough_cut"][-1]
    assert "Editor constraints" not in mock.texts["narrative"][-1]


def test_pins_survive_regenerating_the_rough_cut(matchcut_video, tmp_path):
    cfg = mock_cfg(matchcut_video, tmp_path / "ws")
    ws = Path(cfg.workspace)
    Pipeline(cfg).run()
    rough = Timeline.load(ws / "roughcut.json")
    room = 0.2 + cfg.pacing.min_clip_seconds
    pair = next(((a, b) for a, b in zip(rough.clips, rough.clips[1:]) if a.shot_index != b.shot_index and a.duration >= room and b.duration >= room), None)
    assert pair is not None, "the mock rough cut of the fixture should have a junction with room to move"
    a, b = pair
    pin = Pin(a.shot_index, b.shot_index, round(a.source_out - 0.2, 2), round(b.source_in + 0.2, 2))
    PinStore(ws).put(pin)
    Pipeline(cfg).invalidate_from("roughcut")  # regenerate: the rough cut and everything after it
    assert not (ws / "roughcut.json").exists()
    Pipeline(cfg).run()
    final = Timeline.load(ws / "final.json")
    pairs = [(x, y) for x, y in zip(final.clips, final.clips[1:]) if (x.shot_index, y.shot_index) == pin.key]
    assert len(pairs) == 1
    x, y = pairs[0]
    tolerance = 1.0 / final.fps + 1e-6
    assert x.locked_out and y.locked_in
    assert abs(x.source_out - pin.out_time) <= tolerance and abs(y.source_in - pin.in_time) <= tolerance
    assert any(d.pass_name == "editor" and d.action == "pinned" for d in final.history)


@pytest.fixture(scope="module")
def narrative_ctx(matchcut_video, tmp_path_factory):
    cfg = PipelineConfig(source=str(matchcut_video), workspace=str(tmp_path_factory.mktemp("ws")), target_duration=6.0)
    cfg.llm.backend = "mock"
    ctx = Pipeline(cfg).narrative()
    ctx.narrative = {"beats": [{"id": "b1", "title": "A", "pace": "medium", "target_duration_s": 1.0, "shot_ids": [0, 2, 3], "transition_in": "cut"}]}
    return ctx


def test_pacing_never_drops_a_pinned_clip_even_when_it_is_the_lowest_value(narrative_ctx):
    ctx = narrative_ctx
    saved = ctx.descriptions
    ctx.descriptions = {0: {"quality": 0.1, "usable": False}, 2: {"quality": 0.1, "usable": False}, 3: {"quality": 0.9}}
    try:
        tl = Timeline(source=ctx.media.path, fps=ctx.media.fps, clips=[Clip("c001", 0, 0.5, 2.5, "b1"), Clip("c002", 2, 4.5, 6.0, "b1"), Clip("c003", 3, 6.0, 8.0, "b1")])
        pinned, _ = apply_pins(tl, [Pin(0, 2, 2.5, 4.5)], ctx.shot_bounds, ctx.cfg.pacing.min_clip_seconds)
        points = (pinned.clips[0].source_out, pinned.clips[1].source_in)
        result = PacingPass().apply(pinned, ctx)
    finally:
        ctx.descriptions = saved
    final = result.timeline
    pairs = [(a, b) for a, b in zip(final.clips, final.clips[1:]) if (a.shot_index, b.shot_index) == (0, 2)]
    assert len(pairs) == 1
    a, b = pairs[0]
    assert (a.source_out, b.source_in) == points
    assert a.locked_out and b.locked_in and "dropped" not in a.tags + b.tags
    dropped = {i for d in result.decisions if d.action == "drop" for i in d.clip_ids}
    assert not dropped & {"c001", "c002"}
    assert "c003" in dropped  # the over-target beat still sheds its unpinned clip


# -- I4: a pin holds the cut points, not the transition ------------------------------------------
def junction_pair(tl: Timeline, key):
    return next((a, b) for a, b in zip(tl.clips, tl.clips[1:]) if (a.shot_index, b.shot_index) == key)


def pin_a_dissolve(demo_video, tmp_path):
    """A mock run of the demo footage, then a pin on a beat-boundary junction that got a dissolve, its in-point moved 0.2 s later."""
    cfg = mock_cfg(demo_video, tmp_path / "ws")
    cfg.target_duration = 8.0
    ws = Path(cfg.workspace)
    Pipeline(cfg).run()
    final = Timeline.load(ws / "final.json")
    a, b = next((a, b) for a, b in zip(final.clips, final.clips[1:]) if b.transition_in.kind == "dissolve")
    rough = Timeline.load(ws / "roughcut.json")
    ra, rb = junction_pair(rough, (a.shot_index, b.shot_index))
    pin = Pin(a.shot_index, b.shot_index, ra.source_out, round(rb.source_in + 0.2, 2))
    PinStore(ws).put(pin)
    return cfg, ws, pin


def test_a_pinned_beat_boundary_junction_keeps_its_dissolve_and_its_points(demo_video, tmp_path):
    cfg, ws, pin = pin_a_dissolve(demo_video, tmp_path)
    Pipeline(cfg).refine()
    final = Timeline.load(ws / "final.json")
    a, b = junction_pair(final, pin.key)
    tolerance = 1.0 / final.fps + 1e-6
    assert a.locked_out and b.locked_in and "pinned" in a.tags and "pinned" in b.tags
    assert not any(t.startswith("match_") for t in a.tags + b.tags)
    assert abs(a.source_out - pin.out_time) <= tolerance and abs(b.source_in - pin.in_time) <= tolerance  # transitions never move points
    assert b.transition_in.kind == "dissolve" and b.transition_in.duration > 0.05
    assert not any(d.action == "keep_cut" and set(d.clip_ids) & {a.id, b.id} for d in final.history)
    # the lock lands before the first pass, so the history credits the editor, not the pass that happened to run first
    j = next(j for j in build_junctions(load_stage_timelines(ws)) if (j.out_shot, j.in_shot) == pin.key)
    assert any(line.startswith("editor (before matchcut):") and line.endswith("locked") for line in j.history())
    assert not any(line.startswith("matchcut:") and "locked" in line for line in j.history())


def test_a_match_cut_on_a_beat_boundary_still_keeps_its_hard_cut(mc_ctx_beats, tmp_path):
    ctx = mc_ctx_beats
    rough = Timeline(source=ctx.media.path, fps=ctx.media.fps, stage="roughcut")
    rough.clips = [
        Clip("c001", 0, 0.5, 2.5, "b1"),
        Clip("c002", 2, 4.5, 6.0, "b2"),  # the designed match cut now lands on the beat change
        Clip("c003", 3, 6.0, 8.0, "b2"),
    ]
    final = run_passes(rough, ctx, ["matchcut", "transitions"], save_dir=tmp_path)
    a, b = final.clips[0], final.clips[1]
    assert a.locked_out and b.locked_in and any(t.startswith("match_out:") for t in a.tags) and any(t.startswith("match_in:") for t in b.tags)
    assert b.transition_in.kind == "cut"
    assert any(d.action == "keep_cut" and d.clip_ids == [a.id, b.id] for d in final.history)


def test_polish_may_set_a_transition_on_a_pinned_junction_but_not_on_a_match_cut(mc_ctx_beats):
    ctx = mc_ctx_beats
    edit = lambda cid, kind: PolishEdit(op="set_transition", clip_id=cid, seconds=0.0, target_clip_id="", transition=kind, reason="softer")
    tl = Timeline(source=ctx.media.path, fps=ctx.media.fps, clips=[Clip("c001", 0, 0.5, 2.5, "b1"), Clip("c002", 2, 4.5, 6.0, "b1"), Clip("c003", 3, 6.0, 8.0, "b1")])
    pinned, _ = apply_pins(tl, [Pin(0, 2, 2.5, 4.5)], ctx.shot_bounds, ctx.cfg.pacing.min_clip_seconds)
    ok, why = PolishPass()._apply_edit(pinned, edit("c002", "dissolve"), ctx)
    assert ok and pinned.clips[1].transition_in.kind == "dissolve", why
    matched = pinned.copy()
    matched.clips[0].tags.append("match_out:motion")
    matched.clips[1].tags.append("match_in:motion")
    matched.clips[1].transition_in = Transition("cut", 0.0)
    ok, why = PolishPass()._apply_edit(matched, edit("c002", "dissolve"), ctx)
    assert not ok and "match cuts stay hard cuts" in why and matched.clips[1].transition_in.kind == "cut"


@pytest.fixture(scope="module")
def mc_ctx_beats(matchcut_video, tmp_path_factory):
    cfg = PipelineConfig(source=str(matchcut_video), workspace=str(tmp_path_factory.mktemp("ws")), target_duration=None)
    cfg.llm.backend = "mock"
    ctx = Pipeline(cfg).narrative()
    ctx.narrative = {
        "beats": [
            {"id": "b1", "title": "A", "pace": "medium", "target_duration_s": 2.0, "shot_ids": [0], "transition_in": "cut"},
            {"id": "b2", "title": "B", "pace": "medium", "target_duration_s": 3.5, "shot_ids": [2, 3], "transition_in": "dissolve"},
        ]
    }
    return ctx


# -- I3: refine records what became of a pin whose shots the rough cut does not pair ---------------
def test_a_pin_on_shots_the_rough_cut_does_not_pair_is_placed_by_moving_a_clip(demo_video, tmp_path):
    cfg = mock_cfg(demo_video, tmp_path / "ws")
    cfg.target_duration = 8.0
    ws = Path(cfg.workspace)
    Pipeline(cfg).run()
    rough = Timeline.load(ws / "roughcut.json")
    shots = [c.shot_index for c in rough.clips]
    pin = Pin(shots[0], shots[2], 1.0, 14.0)  # two shots of the cut that are not neighbours
    assert pin.key not in set(zip(shots, shots[1:]))
    PinStore(ws).put(pin)
    final = Pipeline(cfg).refine()
    outcome = pin_outcome(final, pin)
    assert outcome is not None and outcome.action == "pinned"  # the demo cut keeps both shots in one beat, so the move is allowed
    moves = [d for d in final.history if d.pass_name == "editor" and d.action == "pin_moved"]
    assert len(moves) == 1 and moves[0].data["out_shot"] == pin.out_shot and moves[0].data["in_shot"] == pin.in_shot
    assert moves[0].detail.endswith("to hold the editor's pin")
    shot_order = [c.shot_index for c in final.clips]
    assert pin.key in set(zip(shot_order, shot_order[1:]))
    assert pin_outcome(final, Pin(pin.out_shot, pin.in_shot, 1.5, 14.0)) is None  # a pin edited since the run has no outcome yet
    assert PinStore(ws).remove(*pin.key)  # the moved pin now holds shot 0's out-point, so the next pin starts from a clean store
    placed = Pin(shots[0], shots[1], rough.clips[0].source_out, rough.clips[1].source_in)
    PinStore(ws).put(placed)
    assert pin_outcome(Pipeline(cfg).refine(), placed).action == "pinned"


def test_pin_store_survives_an_infinite_shot_index(tmp_path):
    good = Pin(0, 2, 2.0, 4.6)
    (tmp_path / "editorial.json").write_text('{"pins": [{"out_shot": Infinity, "in_shot": 1, "out_time": 1.0, "in_time": 2.0}, %s]}' % json.dumps(good.to_dict()))
    assert PinStore(tmp_path).load() == [good]
    (tmp_path / "editorial.json").write_text('{"pins": Infinity}')
    assert PinStore(tmp_path).load() == []


# -- how a pin finds its clips: lineage first, then the nearest boundary, never a guess ----------------------------
AMBIGUOUS = "shot 3 is ambiguous: more than one piece fits the pinned point"
SPLIT_BOUNDS = {3: (6.0, 9.0), 4: (9.0, 12.0), 5: (12.0, 15.0)}


def extended_pieces(order):
    """Shot 3 as two rough-cut pieces p1 6.5-7.6 and p2 7.9-9.0, a shot 4 clip Y; ``order`` names the clips left to right.

    Pacing later extended p1's out-point to 8.2, inside p2's rough-cut range: a pin taken there points at 8.2.
    """
    clips = {"p1": Clip("c001", 3, 6.5, 7.6, "b1"), "p2": Clip("c002", 3, 7.9, 9.0, "b1"), "Y": Clip("c003", 4, 9.5, 11.0, "b1")}
    return Timeline(source="s.mp4", fps=25.0, clips=[clips[k] for k in order]), clips


def test_a_pacing_extension_inside_a_sibling_piece_keeps_an_adjacent_jump_cut_when_the_pin_remembers_its_clips():
    tl, c = extended_pieces(["p1", "p2", "Y"])
    pin = Pin(3, 3, 8.2, 7.9, out_origin=clip_origin(c["p1"]), in_origin=clip_origin(c["p2"]))
    out, decisions = apply_pins(tl, [pin], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c001", "c002"])]
    assert [x.id for x in out.clips] == ["c001", "c002", "c003"]
    assert (out.clips[0].source_out, out.clips[1].source_in) == (8.2, 7.92)  # 7.9 snapped to the 25 fps grid
    assert not any(d.action == "pin_moved" for d in out.history)


def test_the_same_pin_without_its_clips_is_ambiguous_and_leaves_the_order_alone():
    tl, _ = extended_pieces(["p1", "p2", "Y"])
    out, decisions = apply_pins(tl, [Pin(3, 3, 8.2, 7.9)], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.detail, d.clip_ids) for d in decisions] == [("pin_unplaced", AMBIGUOUS, [])]
    assert [x.id for x in out.clips] == ["c001", "c002", "c003"]
    assert not any(x.locked_in or x.locked_out or x.tags for x in out.clips)
    assert [(x.source_in, x.source_out) for x in out.clips] == [(6.5, 7.6), (7.9, 9.0), (9.5, 11.0)]


def test_a_pacing_extension_inside_a_sibling_piece_keeps_an_adjacent_junction_when_the_pin_remembers_its_clips():
    tl, c = extended_pieces(["p1", "Y", "p2"])
    pin = Pin(3, 4, 8.2, 9.5, out_origin=clip_origin(c["p1"]), in_origin=clip_origin(c["Y"]))
    out, decisions = apply_pins(tl, [pin], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c001", "c003"])]
    assert [x.id for x in out.clips] == ["c001", "c003", "c002"]
    assert out.clips[0].source_out == 8.2 and not out.clips[2].locked_in and not out.clips[2].locked_out


def test_the_same_junction_without_its_clips_is_ambiguous_and_nothing_moves():
    tl, _ = extended_pieces(["p1", "Y", "p2"])
    out, decisions = apply_pins(tl, [Pin(3, 4, 8.2, 9.5)], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.detail) for d in decisions] == [("pin_unplaced", AMBIGUOUS)]
    assert [x.id for x in out.clips] == ["c001", "c003", "c002"]
    assert [(x.source_in, x.source_out) for x in out.clips] == [(6.5, 7.6), (9.5, 11.0), (7.9, 9.0)]


def test_repeated_shots_far_apart_are_told_apart_by_the_nearest_boundary():
    tl = Timeline(source="s.mp4", fps=25.0, clips=[Clip("c001", 3, 2.0, 3.0, "b1"), Clip("c002", 4, 9.5, 11.0, "b1"), Clip("c003", 3, 7.0, 8.0, "b1")])
    out, decisions = apply_pins(tl, [Pin(3, 4, 3.0, 9.5)], {3: (1.0, 9.0), 4: (9.0, 12.0)}, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c001", "c002"])]
    assert [x.id for x in out.clips] == ["c001", "c002", "c003"]
    out, decisions = apply_pins(tl, [Pin(3, 4, 8.0, 9.5)], {3: (1.0, 9.0), 4: (9.0, 12.0)}, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pin_moved", ["c002", "c003"]), ("pinned", ["c003", "c002"])]
    assert [x.id for x in out.clips] == ["c001", "c003", "c002"]


def overlapping() -> Timeline:
    """Shot 3 as two pieces whose source ranges overlap (6.0-8.0 and 7.0-9.0), then shot 4."""
    return Timeline(source="s.mp4", fps=25.0, clips=[Clip("c001", 3, 6.0, 8.0, "b1"), Clip("c002", 3, 7.0, 9.0, "b1"), Clip("c003", 4, 9.5, 11.0, "b1")])


def test_overlapping_pieces_resolve_by_lineage_on_each_side():
    tl = overlapping()
    pin = Pin(3, 3, 8.4, 6.6, out_origin=clip_origin(tl.clips[0]), in_origin=clip_origin(tl.clips[1]))
    out, decisions = apply_pins(tl, [pin], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c001", "c002"])]
    assert (out.clips[0].source_out, out.clips[1].source_in) == (8.4, 6.6)
    assert [x.id for x in out.clips] == ["c001", "c002", "c003"]


def test_overlapping_pieces_whose_boundaries_are_close_are_ambiguous_without_lineage():
    out, decisions = apply_pins(overlapping(), [Pin(3, 3, 8.4, 6.6)], SPLIT_BOUNDS, min_clip=0.5)  # out: 0.4 vs 0.6 away
    assert [(d.action, d.detail) for d in decisions] == [("pin_unplaced", AMBIGUOUS)]
    assert [(x.source_in, x.source_out) for x in out.clips] == [(6.0, 8.0), (7.0, 9.0), (9.5, 11.0)]


def test_the_incoming_side_of_a_jump_cut_never_considers_the_clip_the_outgoing_side_took():
    tl = overlapping()  # 6.6 is 0.6 from piece 1's in-point and 0.4 from piece 2's: ambiguous, unless piece 1 is already taken
    out, decisions = apply_pins(tl, [Pin(3, 3, 8.0, 6.6, out_origin=clip_origin(tl.clips[0]))], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c001", "c002"])]


def test_a_gap_of_exactly_the_ambiguity_margin_is_clear_and_a_smaller_one_is_not():
    tl = Timeline(source="s.mp4", fps=25.0, clips=[Clip("c001", 3, 6.0, 7.0, "b1"), Clip("c002", 4, 9.5, 11.0, "b1"), Clip("c003", 3, 6.5, 7.5, "b1")])
    _, decisions = apply_pins(tl, [Pin(3, 4, 6.75, 9.5)], SPLIT_BOUNDS, min_clip=0.5)  # 0.25 vs 0.75 away: exactly 0.5 farther
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c001", "c002"])]
    _, decisions = apply_pins(tl, [Pin(3, 4, 7.1, 9.5)], SPLIT_BOUNDS, min_clip=0.5)  # 0.1 vs 0.4 away
    assert [(d.action, d.detail) for d in decisions] == [("pin_unplaced", AMBIGUOUS)]


def test_a_lineage_that_matches_no_clip_falls_back_to_the_nearest_boundary():
    tl = Timeline(source="s.mp4", fps=25.0, clips=[Clip("c001", 3, 2.0, 3.0, "b1"), Clip("c002", 4, 9.5, 11.0, "b1")])
    _, decisions = apply_pins(tl, [Pin(3, 4, 3.0, 9.5, out_origin="3@0.000-1.000")], {3: (1.0, 9.0), 4: (9.0, 12.0)}, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c001", "c002"])]


def test_lineage_prefers_the_remembered_piece_over_a_nearer_boundary():
    # the pin points at 7.6, nearest to piece 1's out-point, but remembers piece 2 (a later pass moved the out-point)
    tl = Timeline(source="s.mp4", fps=25.0, clips=[Clip("c001", 3, 6.0, 7.5, "b1"), Clip("c002", 3, 7.0, 9.0, "b1"), Clip("c003", 4, 9.5, 11.0, "b1")])
    _, decisions = apply_pins(tl, [Pin(3, 4, 7.6, 9.5, out_origin=clip_origin(tl.clips[1]))], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c002", "c003"])]
    _, decisions = apply_pins(tl, [Pin(3, 4, 7.6, 9.5)], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pin_moved", ["c003", "c001"]), ("pinned", ["c001", "c003"])]  # the nearest boundary alone picks piece 1


def test_the_incoming_clip_a_pin_remembers_wins_after_a_clip_reordering():
    # rough cut: p1 | sh1 | p2 | sh2; a match-cut reorder gave p1 | sh2 | p2 | sh1 and the editor pinned sh2 -> p2 at p2's trimmed in-point
    tl = Timeline(source="s.mp4", fps=25.0, clips=[
        Clip("c001", 3, 6.0, 7.0, "b1"), Clip("c002", 1, 2.6, 4.0, "b1"), Clip("c003", 3, 7.4, 8.6, "b1"), Clip("c004", 2, 4.6, 5.8, "b1"),
    ])
    bounds = {1: (2.5, 4.5), 2: (4.5, 6.0), 3: (6.0, 9.0)}
    pin = Pin(2, 3, 5.8, 6.8, in_origin=clip_origin(tl.clips[2]))  # 6.8 is 0.8 from piece 1's in-point and 0.6 from piece 2's
    out, decisions = apply_pins(tl, [pin], bounds, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pin_moved", ["c003", "c004"]), ("pinned", ["c004", "c003"])]
    assert [x.id for x in out.clips] == ["c001", "c002", "c004", "c003"]
    assert out.clips[3].source_in == 6.8 and out.clips[0].source_in == 6.0 and not out.clips[0].locked_in
    _, decisions = apply_pins(tl, [Pin(2, 3, 5.8, 6.8)], bounds, min_clip=0.5)
    assert [(d.action, d.detail) for d in decisions] == [("pin_unplaced", AMBIGUOUS)]


def test_the_continuity_insert_shape_is_resolved_by_the_remembered_piece():
    tl = two_pieces()  # rough cut c001 sh3 6.0-7.4, c002 sh3 7.6-9.0, c003 sh4; the stage order was c001, c003, c002
    pin = Pin(4, 3, 11.0, 7.6, in_origin=clip_origin(tl.clips[1]))
    out, decisions = apply_pins(tl, [pin], PIECE_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pin_moved", ["c002", "c003"]), ("pinned", ["c003", "c002"])]
    assert [x.id for x in out.clips] == ["c001", "c003", "c002"] and out.clips[2].source_in == 7.6
    _, decisions = apply_pins(tl, [Pin(4, 3, 11.0, 7.6)], PIECE_BOUNDS, min_clip=0.5)  # without lineage 7.6 is still clear here: 1.6 s from piece 1's in-point, 0 from piece 2's
    assert decisions[0].clip_ids == ["c002", "c003"]


def test_a_phase_two_pin_selects_by_the_ranges_the_rough_cut_had_before_phase_one():
    # Q is shot 3's piece nearest to the second pin's out-point 8.4 (0.1 away, piece P 1.4 away). The first pin, applied
    # earlier because it needs no move, extends P's out-point to 8.0 (0.4 away); reading that would make the second pin ambiguous.
    p = Clip("c001", 3, 6.0, 7.0, "b1")
    tl = Timeline(source="s.mp4", fps=25.0, clips=[p, Clip("c002", 4, 9.5, 11.0, "b1"), Clip("c003", 5, 12.5, 14.0, "b1"), Clip("c004", 3, 7.5, 8.5, "b1")])
    first = Pin(3, 4, 8.0, 9.5, out_origin=clip_origin(p))
    second = Pin(3, 5, 8.4, 12.5)
    out, decisions = apply_pins(tl, [second, first], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c001", "c002"]), ("pin_moved", ["c003", "c004"]), ("pinned", ["c004", "c003"])]
    assert [x.id for x in out.clips] == ["c001", "c002", "c004", "c003"]
    assert out.clips[0].source_out == 8.0 and out.clips[2].source_out == 8.4


def test_pin_and_clip_origins_round_trip_and_old_files_still_load(tmp_path):
    store = PinStore(tmp_path)
    store.put(Pin(3, 4, 8.2, 9.5, out_origin="3@6.500-7.600", in_origin="4@9.500-11.000"))
    pin = PinStore(tmp_path).get(3, 4)
    assert (pin.out_origin, pin.in_origin) == ("3@6.500-7.600", "4@9.500-11.000") and pin.key == (3, 4)
    (tmp_path / "editorial.json").write_text(json.dumps({"pins": [{"out_shot": 3, "in_shot": 4, "out_time": 8.2, "in_time": 9.5, "origin": "locked", "note": ""}]}))
    old = PinStore(tmp_path).get(3, 4)
    assert (old.out_origin, old.in_origin, old.out_time) == ("", "", 8.2)
    clip = Clip("c001", 3, 6.5, 7.6, "b1", origin="3@6.500-7.600")
    assert Clip.from_dict(clip.to_dict()).origin == "3@6.500-7.600"
    legacy = clip.to_dict()
    del legacy["origin"]
    assert Clip.from_dict(legacy).origin == ""
    assert Timeline(source="s.mp4", fps=25.0, clips=[clip]).copy().clips[0].origin == "3@6.500-7.600"


def test_a_clip_without_an_origin_is_identified_by_its_rough_cut_range():
    c = Clip("c001", 3, 6.5, 7.6, "b1")
    assert clip_origin(c) == "3@6.500-7.600"
    c.source_out = 8.2  # a later pass moved the out-point: a stamped clip keeps its instance
    assert clip_origin(c) == "3@6.500-8.200"
    c.origin = "3@6.500-7.600"
    assert clip_origin(c) == "3@6.500-7.600"


def test_a_pin_taken_after_pacing_extended_a_piece_is_placed_by_a_move_with_lineage_and_is_ambiguous_without():
    # the review's probe: rough cut [p1, p2, Y]; pacing extended p1's out to 8.2; the pin (shot 3 -> shot 4) was taken in [p1, Y, p2]
    tl, c = extended_pieces(["p1", "p2", "Y"])
    pin = Pin(3, 4, 8.2, 9.5, out_origin=clip_origin(c["p1"]), in_origin=clip_origin(c["Y"]))
    out, decisions = apply_pins(tl, [pin], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pin_moved", ["c003", "c001"]), ("pinned", ["c001", "c003"])]
    assert [x.id for x in out.clips] == ["c001", "c003", "c002"] and out.clips[0].source_out == 8.2
    assert not decisions[-1].detail.endswith(")") and "by_boundary" not in decisions[-1].data  # lineage: nothing to check
    tl, _ = extended_pieces(["p1", "p2", "Y"])
    out, decisions = apply_pins(tl, [Pin(3, 4, 8.2, 9.5)], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.detail) for d in decisions] == [("pin_unplaced", AMBIGUOUS)]
    assert [x.id for x in out.clips] == ["c001", "c002", "c003"]


NOTE = " (piece chosen by nearest boundary: check it, or lock it again)"


def test_a_placement_by_the_nearest_boundary_is_marked_in_the_decision():
    # both pieces were extended to ~6.5-9.0 by pacing and the pin has no lineage: the boundaries are 1.4 s and more apart, so
    # the rule picks a piece (here the wrong one for the editor who pinned p1) but says so
    tl, _ = extended_pieces(["p1", "p2", "Y"])
    out, decisions = apply_pins(tl, [Pin(3, 4, 9.0, 9.5)], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pinned", ["c002", "c003"])]
    assert decisions[0].detail == "locked by the editor: out 9.00s, in 9.52s" + NOTE  # 9.5 snapped to the 25 fps grid
    assert decisions[0].data["by_boundary"] is True
    tl, _ = extended_pieces(["p1", "p2", "Y"])
    out, decisions = apply_pins(tl, [Pin(4, 3, 11.0, 6.52)], SPLIT_BOUNDS, min_clip=0.5)
    assert [(d.action, d.clip_ids) for d in decisions] == [("pin_moved", ["c001", "c003"]), ("pinned", ["c003", "c001"])]
    assert decisions[-1].detail.endswith(NOTE) and decisions[-1].data["by_boundary"] is True
    assert "by_boundary" not in decisions[0].data
    # one candidate on each side: nothing was chosen, nothing to check
    out, decisions = apply_pins(cut(), [Pin(0, 2, 2.2, 4.8)], BOUNDS, min_clip=0.75)
    assert decisions[0].detail == "locked by the editor: out 2.20s, in 4.80s" and "by_boundary" not in decisions[0].data


def test_an_origin_that_matches_no_piece_is_a_boundary_choice_and_is_marked():
    tl, _ = extended_pieces(["p1", "p2", "Y"])
    stale = Pin(3, 4, 9.0, 9.5, out_origin="3@6.000-7.000")
    _, decisions = apply_pins(tl, [stale], SPLIT_BOUNDS, min_clip=0.5)
    assert decisions[0].action == "pinned" and decisions[0].data["by_boundary"] is True


# -- a pin on the human stage: held only if the editor's cut still holds it ---------------------------------------------
def pinned_pair(with_origins: bool = True):
    """(pin, final cut with the pin applied): c001 (shot 0) -> c002 (shot 2) held at out 2.00 s / in 4.60 s."""
    tl = cut()
    for c in tl.clips:
        c.origin = clip_origin(c)
    pin = Pin(0, 2, 2.0, 4.6, out_origin=tl.clips[0].origin if with_origins else "", in_origin=tl.clips[1].origin if with_origins else "")
    final, decisions = apply_pins(tl, [pin], BOUNDS, min_clip=0.75)
    final.stage = "final"
    assert [d.action for d in decisions] == ["pinned"]
    return pin, final


def human_of(final: Timeline, clips=None) -> Timeline:
    """The editor's cut: a copy of ``final`` (its history, the inherited editor decision included) as the stage ``human``."""
    human = final.copy()
    human.stage = "human"
    if clips is not None:
        human.clips = clips(human.clips)
    return human


def test_a_pin_the_editor_left_alone_reads_applied_on_the_human_stage():
    pin, final = pinned_pair()
    assert pin_outcome(human_of(final), pin).action == "pinned"
    assert pin_outcome(human_of(final), pin).to_dict() == pin_outcome(final, pin).to_dict()
    # a move of less than half a frame is the frame grid, not an edit (the rough cut is 1/25 s = 0.04 s per frame)
    nudged = human_of(final, lambda cs: [cs[0], cs[1], cs[2]])
    nudged.clips[0].source_out += 0.019
    assert pin_outcome(nudged, pin).action == "pinned"


@pytest.mark.parametrize("with_origins", [True, False])
def test_the_human_stage_reports_a_pin_whose_cut_the_editor_trimmed(with_origins):
    pin, final = pinned_pair(with_origins)
    out = human_of(final)
    out.clips[0].source_out -= 3 / 25
    d = pin_outcome(out, pin)
    assert (d.pass_name, d.action) == ("human", "pin_changed") and d.detail == "cut trimmed by 3 frames (out)", d
    assert "\n" not in d.detail
    both = human_of(final)
    both.clips[0].source_out += 2 / 25
    both.clips[1].source_in += 1 / 25
    assert pin_outcome(both, pin).detail == "cut trimmed by 2 frames (out) and 1 frame (in)"
    just_in = human_of(final)
    just_in.clips[1].source_in -= 4 / 25
    assert pin_outcome(just_in, pin).detail == "cut trimmed by 4 frames (in)"
    assert pin_outcome(final, pin).action == "pinned"  # the canonical stage is judged by its own history, as before


@pytest.mark.parametrize("with_origins", [True, False])
def test_the_human_stage_reports_a_pin_whose_clip_the_editor_deleted(with_origins):
    pin, final = pinned_pair(with_origins)
    for gone, other in ((1, "shot 2"), (0, "shot 0")):
        d = pin_outcome(human_of(final, lambda cs: [c for k, c in enumerate(cs) if k != gone]), pin)
        assert (d.action, d.detail) == ("pin_changed", f"clip removed ({other})"), d


@pytest.mark.parametrize("with_origins", [True, False])
def test_the_human_stage_reports_a_pin_whose_pair_the_editor_separated(with_origins):
    pin, final = pinned_pair(with_origins)
    inserted = Clip("h001", 1, 3.0, 3.6, "b1", origin="human:h001")
    d = pin_outcome(human_of(final, lambda cs: [cs[0], inserted, cs[1], cs[2]]), pin)
    assert (d.action, d.detail) == ("pin_changed", "clips are no longer adjacent"), d
    d = pin_outcome(human_of(final, lambda cs: [cs[1], cs[0], cs[2]]), pin)  # swapped: shot 2 now comes first
    assert d.action == "pin_changed" and d.detail == "clips are no longer adjacent"


def test_the_human_stage_reports_a_pin_whose_clip_is_another_piece_of_the_footage_now():
    pin, final = pinned_pair()
    elsewhere = Clip("h001", 2, 5.85, 5.95, "b1", origin="human:h001")  # same shot, other footage of it: not the clip the pin named
    d = pin_outcome(human_of(final, lambda cs: [cs[0], elsewhere, cs[2]]), pin)
    assert (d.action, d.detail) == ("pin_changed", "other footage (shot 2 is a different clip now)"), d
    # a clip the importer made over the pinned clip's range (it was razored or copied) is said to be split or copied
    over = Clip("h001", 2, 4.6, 5.8, "b1", origin="human:h001")
    d = pin_outcome(human_of(final, lambda cs: [cs[0], over, cs[2]]), pin)
    assert (d.action, d.detail) == ("pin_changed", "clip split or copied (shot 2)"), d
    half = Clip("h002", 2, 4.6, 5.2, "b1", origin="human:h002")
    d = pin_outcome(human_of(final, lambda cs: [cs[0], half, Clip("h003", 2, 5.2, 5.8, "b1", origin="human:h003"), cs[2]]), pin)
    assert d.detail == "clip split or copied (shot 2)", d
    # without the pin's clip instances the shots decide: the same shots are still held
    bare, final = pinned_pair(with_origins=False)
    assert pin_outcome(human_of(final, lambda cs: [cs[0], over, cs[2]]), bare).action == "pinned"


def test_the_inherited_editor_decision_is_never_what_makes_a_pin_read_applied_on_the_human_stage():
    pin, final = pinned_pair()
    human = human_of(final, lambda cs: cs[1:])
    assert any(d.pass_name == "editor" and d.action == "pinned" for d in human.history)  # it is in the history ...
    assert pin_outcome(human, pin).action == "pin_changed"  # ... and does not count
    # a stage name passed by the caller decides, not only the one the file says
    stageless = human_of(final, lambda cs: cs[1:])
    stageless.stage = ""
    assert pin_outcome(stageless, pin).action == "pinned" and pin_outcome(stageless, pin, stage="human").action == "pin_changed"
    # an unplaced pin and a pin the run never saw keep their outcome
    assert pin_outcome(human_of(final), Pin(0, 2, 1.0, 5.0)) is None


def test_junctions_of_the_human_stage_say_a_changed_pin_is_not_pinned():
    pin, final = pinned_pair()
    stages = [("final", final), ("human", human_of(final))]
    j = build_junctions(stages)[0]
    assert j.pinned and not j.pin_lost
    trimmed = human_of(final)
    trimmed.clips[0].source_out -= 3 / 25
    j = build_junctions([("final", final), ("human", trimmed)])[0]
    assert not j.pinned and j.pin_lost == "cut trimmed by 3 frames (out)"
    assert build_junctions([("final", final)])[0].pinned  # unchanged for the canonical stages


def test_pin_site_is_where_the_pair_or_its_surviving_clip_now_sits():
    from matchcut.pipeline.editorial import pin_site

    pin, final = pinned_pair()
    assert pin_site(human_of(final), pin) == 0
    assert pin_site(human_of(final, lambda cs: cs[1:]), pin) == 0  # c001 gone: the incoming clip's junction (clamped to the first)
    assert pin_site(human_of(final, lambda cs: [cs[0], cs[2]]), pin) == 0  # c002 gone: after the outgoing clip
    inserted = Clip("h001", 1, 3.0, 3.6, "b1", origin="human:h001")
    assert pin_site(human_of(final, lambda cs: [cs[0], inserted, cs[1], cs[2]]), pin) == 0  # separated: after the outgoing clip
    assert pin_site(human_of(final, lambda cs: [cs[2], cs[1], cs[0]]), pin) == 1  # swapped around: the outgoing clip is last, so the junction before it
    assert pin_site(human_of(final, lambda cs: [cs[2]]), pin) is None


def test_pin_states_gives_every_view_the_same_answer():
    from matchcut.pipeline.editorial import pin_states

    pin, final = pinned_pair()
    other = Pin(1, 5, 1.0, 2.0)
    held = pin_states(human_of(final), [pin, other], "human")
    assert [(p.label, p.state, p.reason) for p in held] == [("s0->s2", "applied", ""), ("s1->s5", "pending", "")]
    gone = pin_states(human_of(final, lambda cs: cs[1:]), [pin], "human")
    assert [(p.state, p.reason) for p in gone] == [("changed", "clip removed (shot 0)")]
    assert [p.state for p in pin_states(None, [pin])] == ["pending"]
