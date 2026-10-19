"""End-to-end regression scenarios: real passes on the synthetic footage with the mock backend.

The analysis, description and narrative stages are cached per workspace, so one workspace is built per module
and copied for every scenario that writes pins or edits a cached stage.

Shot layout of the demo footage (seconds): 0:[0,2] 1:[2,5] 2:[5,6.5] 3:[6.5,9] 4:[9,12] 5:[12,14] 6:[14,16] 7:[16,18.5].
The mock narrative has three beats: b1 = shots 0-2, b2 = shots 3-5 (a dissolve in), b3 = shots 6-7 (a dissolve in).
With target 8.0 the mock rough cut is: b1: 0, 1; b2: 3; b3: 6, 7.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore, pin_outcome
from matchcut.pipeline.junctions import Junction, build_junctions, junction_detail, load_stage_timelines
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Decision, Timeline, clip_origin


@pytest.fixture(scope="module")
def base_ws(demo_video, tmp_path_factory) -> Path:
    """A workspace with every stage output from one full mock run (target 8 s, no render)."""
    ws = tmp_path_factory.mktemp("regress_ws")
    cfg = PipelineConfig(source=str(demo_video), workspace=str(ws), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()
    return ws


def own_copy(base_ws: Path, tmp_path: Path, name: str = "ws") -> Tuple[Path, PipelineConfig]:
    """A private copy of the module workspace and its configuration (stages stay cached)."""
    ws = tmp_path / name
    shutil.copytree(base_ws, ws)
    cfg = PipelineConfig.load(ws / "config.json")
    cfg.workspace = str(ws)
    return ws, cfg


def final_junctions(ws: Path) -> List[Junction]:
    return build_junctions(load_stage_timelines(ws))


def pair_of(tl: Timeline, key: Tuple[int, int]) -> Tuple[Clip, Clip]:
    return next((a, b) for a, b in zip(tl.clips, tl.clips[1:]) if (a.shot_index, b.shot_index) == key)


def junction_of(ws: Path, key: Tuple[int, int]) -> Junction:
    return next(j for j in final_junctions(ws) if (j.out_shot, j.in_shot) == key)


def editor_decisions(tl: Timeline, pin: Pin, action: Optional[str] = None) -> List[Decision]:
    return [
        d
        for d in tl.history
        if d.pass_name == "editor" and d.data.get("out_shot") == pin.out_shot and d.data.get("in_shot") == pin.in_shot and (action is None or d.action == action)
    ]


def hand_built(ws: Path, cfg: PipelineConfig, clips: List[Clip]) -> Timeline:
    """A rough cut written by hand and saved as the workspace's cached roughcut.json (refine reuses it)."""
    cached = Timeline.load(ws / "roughcut.json")
    tl = Timeline(source=cached.source, fps=cached.fps, clips=clips, stage="roughcut", name="rough cut")
    assert tl.validate(min_clip=cfg.pacing.min_clip_seconds) == []
    tl.save(ws / "roughcut.json")
    return tl


# -- 1. cut points that really move ----------------------------------------------------------------
def test_cut_points_really_move_and_the_history_says_who_moved_them(base_ws, tmp_path):
    # The mock rough cut is already built to the target, so pacing has nothing to trim in a from-scratch run: the
    # rough cut of the 8 s run is re-fitted to a tight 5 s target instead (cached stages reused, passes re-run).
    ws, cfg = own_copy(base_ws, tmp_path)
    cfg.target_duration = 5.0
    Pipeline(cfg).refine()
    fps = Timeline.load(ws / "final.json").fps
    junctions = final_junctions(ws)
    moved = [j for j in junctions if abs(j.current.out_time - j.baseline.out_time) >= 1 / fps - 1e-6 or abs(j.current.in_time - j.baseline.in_time) >= 1 / fps - 1e-6]
    assert moved, "a 5 s target should make pacing trim at least one junction's cut points"
    for j in moved:
        history = j.history()
        assert any(line.startswith("pacing:") for line in history[1:]), history  # a pass stage, not only the baseline
        assert j.why() != "no pass changed this junction"
        assert "-0.00" not in junction_detail(j)


# -- 2. a pin on shots the rough cut does not pair is placed by a move ------------------------------
def test_a_pin_on_shots_that_are_not_adjacent_is_placed_by_a_move(base_ws, tmp_path):
    # No beat of the mock rough cut holds three clips, so the rough cut is built by hand: three shots in b1, one in b2.
    ws, cfg = own_copy(base_ws, tmp_path)
    rough = hand_built(
        ws,
        cfg,
        [
            Clip("c001", 0, 0.0, 1.8, "b1"),
            Clip("c002", 1, 2.5, 4.0, "b1"),
            Clip("c003", 2, 5.2, 6.3, "b1"),
            Clip("c004", 3, 6.7, 8.5, "b2"),
        ],
    )
    pin = Pin(0, 2, 1.8, 5.4)  # shot 0 then shot 2, with shot 1 in between
    assert pin.key not in {(a.shot_index, b.shot_index) for a, b in zip(rough.clips, rough.clips[1:])}
    PinStore(ws).put(pin)
    final = Pipeline(cfg).refine(rough)

    order = [c.shot_index for c in final.clips]
    assert pin.key in set(zip(order, order[1:])), order

    matchcut = dict(load_stage_timelines(ws))["matchcut"]
    actions = [d.action for d in editor_decisions(matchcut, pin)]
    assert actions == ["pin_moved", "pinned"], actions

    j = junction_of(ws, pin.key)
    assert j.pinned and j.pin_origin == "locked"
    assert j.why().startswith("editor:")
    assert j.baseline.stage == "matchcut"  # adjacent from the first pass stage on (the cached rough cut never paired them)
    a, b = pair_of(final, pin.key)
    assert a.locked_out and b.locked_in


# -- 3. a pin that cannot be placed says why --------------------------------------------------------
def test_a_pin_that_would_reorder_the_beats_reports_why(base_ws, tmp_path):
    ws, cfg = own_copy(base_ws, tmp_path)
    rough = Timeline.load(ws / "roughcut.json")
    last, first = rough.clips[-1], rough.clips[0]
    assert (first.beat_id, last.beat_id) == ("b1", "b3")
    pin = Pin(last.shot_index, first.shot_index, round(last.source_out - 0.2, 2), round(first.source_in + 0.2, 2))  # last beat -> first beat
    PinStore(ws).put(pin)
    final = Pipeline(cfg).refine()

    # the first candidate moves the incoming clip (the first beat's shot) after the outgoing one, which would reorder the beats
    expected = f"moving shot {pin.in_shot} would split or reorder the beats"
    decisions = editor_decisions(final, pin)
    assert [d.action for d in decisions] == ["pin_unplaced"], [d.action for d in decisions]
    assert decisions[0].detail == expected
    assert not any(d.action == "pin_moved" for d in final.history)
    outcome = pin_outcome(final, pin)
    assert outcome is not None and outcome.action == "pin_unplaced"
    # nothing was locked on the pin's account
    assert not any("pinned" in c.tags for c in final.clips)


# -- 4. pins keep their dissolves -------------------------------------------------------------------
def test_pins_keep_their_dissolves(base_ws, tmp_path):
    # (a) a pinned junction on a beat boundary keeps the transitions pass's dissolve
    # The mock narrative already asks for a dissolve into b2 and b3, so narrative.json needs no edit.
    ws, cfg = own_copy(base_ws, tmp_path, "boundary")
    rough = Timeline.load(ws / "roughcut.json")
    ra, rb = next((a, b) for a, b in zip(rough.clips, rough.clips[1:]) if a.beat_id != b.beat_id)
    pin = Pin(ra.shot_index, rb.shot_index, ra.source_out, round(rb.source_in + 0.2, 2))
    PinStore(ws).put(pin)
    final = Pipeline(cfg).refine()
    a, b = pair_of(final, pin.key)
    assert a.locked_out and b.locked_in and "pinned" in a.tags + b.tags
    assert b.transition_in.kind == "dissolve" and b.transition_in.duration > 0.05
    assert any(d.action == "dissolve" and d.pass_name == "transitions" and d.clip_ids == [a.id, b.id] for d in final.history)
    assert not any(d.action == "keep_cut" for d in final.history)
    assert junction_of(ws, pin.key).pinned

    # (b) a pinned jump cut (two separate pieces of one shot back to back) gets the continuity dissolve and stays together
    ws, cfg = own_copy(base_ws, tmp_path, "jump")
    rough = hand_built(
        ws,
        cfg,
        [
            Clip("c001", 1, 2.2, 3.0, "b1"),
            Clip("c002", 1, 4.0, 4.9, "b1"),
            Clip("c003", 2, 5.2, 6.3, "b1"),  # a would-be separator: continuity must not slip it between the pinned pair
            Clip("c004", 3, 6.7, 8.5, "b2"),
        ],
    )
    jump = Pin(1, 1, 3.0, 4.0)
    PinStore(ws).put(jump)
    final = Pipeline(cfg).refine(rough)
    a, b = pair_of(final, jump.key)
    assert final.clips.index(b) == final.clips.index(a) + 1
    assert a.locked_out and b.locked_in
    assert b.transition_in.kind == "dissolve" and b.transition_in.duration > 0
    assert any(d.pass_name == "continuity" and d.action == "dissolve" for d in final.history)
    assert not any(d.action == "insert" for d in final.history)

    # (c) a match cut on a beat boundary keeps its hard cut
    ws, cfg = own_copy(base_ws, tmp_path, "matchcut")
    mc_out = Clip("c001", 0, 0.0, 1.8, "b1", locked_out=True, tags=["match_out:graphic"])
    mc_in = Clip("c002", 3, 6.7, 8.5, "b2", locked_in=True, tags=["match_in:graphic"])
    rough = hand_built(ws, cfg, [mc_out, mc_in, Clip("c003", 6, 14.2, 15.8, "b3")])
    final = Pipeline(cfg).refine(rough, passes=["transitions"])
    a, b, c = final.clips
    assert b.transition_in.kind == "cut"
    assert any(d.action == "keep_cut" and d.clip_ids == [a.id, b.id] for d in final.history)
    assert c.transition_in.kind == "dissolve"  # the other beat change, with no match cut on it, still gets the beat's dissolve


# -- 5. pins persist across a second run and a regenerated rough cut --------------------------------
def test_two_pins_survive_a_second_run_and_a_regenerated_rough_cut(base_ws, tmp_path):
    ws, cfg = own_copy(base_ws, tmp_path)
    junctions = final_junctions(ws)
    locked_j = next(j for j in junctions if j.beat_boundary)  # inspected points, as the editor locks them
    reverted_j = next(j for j in junctions if (j.out_shot, j.in_shot) != (locked_j.out_shot, locked_j.in_shot) and j.baseline.stage == "roughcut")
    locked = Pin(locked_j.out_shot, locked_j.in_shot, round(locked_j.current.out_time, 3), round(locked_j.current.in_time, 3), origin="locked")
    reverted = Pin(reverted_j.out_shot, reverted_j.in_shot, round(reverted_j.baseline.out_time, 3), round(reverted_j.baseline.in_time, 3), origin="reverted")
    assert locked.key != reverted.key
    store = PinStore(ws)
    store.put(locked)
    store.put(reverted)

    def check(final: Timeline) -> None:
        for pin in (locked, reverted):
            outcome = pin_outcome(final, pin)
            assert outcome is not None and outcome.action == "pinned", (pin, outcome)
            j = junction_of(ws, pin.key)
            assert j.pin_origin == pin.origin
            lines = [line for line in j.history() if line.startswith("editor (before ")]
            assert lines and lines[-1].endswith(pin.origin), (j.history(), pin.origin)
            a, b = pair_of(final, pin.key)
            assert a.locked_out and b.locked_in

    check(Pipeline(cfg).run())
    Pipeline(cfg).invalidate_from("roughcut")
    assert not (ws / "roughcut.json").exists()
    check(Pipeline(cfg).run())
    assert (ws / "roughcut.json").exists()


# -- 6. a regenerated rough cut without a pinned shot says so ---------------------------------------
def test_a_regenerated_rough_cut_without_the_shot_reports_it(base_ws, tmp_path):
    ws, cfg = own_copy(base_ws, tmp_path)
    rough = Timeline.load(ws / "roughcut.json")
    a, b = rough.clips[0], rough.clips[1]
    pin = Pin(a.shot_index, b.shot_index, a.source_out, b.source_in)
    PinStore(ws).put(pin)
    rough.clips = [c for c in rough.clips if c.shot_index != b.shot_index]  # a rough cut regenerated without the incoming shot
    rough.save(ws / "roughcut.json")
    final = Pipeline(cfg).refine()
    outcome = pin_outcome(final, pin)
    assert outcome is not None and outcome.action == "pin_unplaced"
    assert outcome.detail == f"shot {pin.in_shot} is not in this cut"
    assert [d.action for d in editor_decisions(final, pin)] == ["pin_unplaced"]
    assert not any(c.shot_index == pin.in_shot for c in final.clips)


# -- 7. a pinned jump cut whose two pieces are apart ------------------------------------------------
JUMP_CUT = Pin(3, 3, 7.4, 7.8)  # shot 3 (6.5-9.0) cut twice: it ends at 7.4, resumes at 7.8


def two_pieces_apart() -> List[Clip]:
    return [Clip("c001", 3, 6.5, 7.4, "b2"), Clip("c002", 4, 9.2, 10.6, "b2"), Clip("c003", 3, 7.8, 9.0, "b2")]


def test_a_pinned_jump_cut_whose_pieces_are_apart_is_placed_by_a_move(base_ws, tmp_path):
    # Used to crash refine (the pin's two clips were the same clip) and then fail every later run until the pin was cleared.
    ws, cfg = own_copy(base_ws, tmp_path)
    rough = hand_built(ws, cfg, two_pieces_apart())
    PinStore(ws).put(JUMP_CUT)
    final = Pipeline(cfg).refine(rough)

    matchcut = dict(load_stage_timelines(ws))["matchcut"]
    actions = [d.action for d in editor_decisions(matchcut, JUMP_CUT)]
    assert actions == ["pin_moved", "pinned"], actions
    outcome = pin_outcome(final, JUMP_CUT)
    assert outcome is not None and outcome.action == "pinned"
    pieces = [c for c in final.clips if c.shot_index == 3]
    first, second = pieces[0], pieces[1]
    assert final.clips.index(second) == final.clips.index(first) + 1  # the two pieces now sit side by side
    assert first.locked_out and second.locked_in
    assert abs(first.source_out - 7.4) <= 0.5 / final.fps + 1e-6 and abs(second.source_in - 7.8) <= 0.5 / final.fps + 1e-6
    Pipeline(cfg).refine(rough)  # and the next run is no different


def test_the_same_pin_after_a_rough_cut_that_keeps_the_shot_once_is_reported(base_ws, tmp_path):
    ws, cfg = own_copy(base_ws, tmp_path)
    rough = hand_built(ws, cfg, [Clip("c001", 3, 6.5, 8.0, "b2"), Clip("c002", 4, 9.2, 10.6, "b2")])
    PinStore(ws).put(JUMP_CUT)
    final = Pipeline(cfg).refine(rough)
    outcome = pin_outcome(final, JUMP_CUT)
    assert outcome is not None and outcome.action == "pin_unplaced"
    assert outcome.detail == "shot 3 has only one clip in this cut"
    assert [d.action for d in editor_decisions(final, JUMP_CUT)] == ["pin_unplaced"]
    assert not any("pinned" in c.tags for c in final.clips)


# -- 8. the junction a pass made (here matchcut's reorder) is pinned on the right piece ------------
def test_a_junction_a_pass_made_is_placed_on_the_piece_the_editor_pinned(base_ws, tmp_path):
    # Rough cut: shot 3 in two pieces, then shot 4. The match-cut pass reorders them to 3, 4, 3 and the editor reverts that
    # junction (shot 4 -> shot 3) to the first cut's points, which lie in the second piece.
    ws, cfg = own_copy(base_ws, tmp_path)
    rough = hand_built(ws, cfg, [Clip("c001", 3, 6.5, 7.6, "b2"), Clip("c002", 3, 7.9, 9.0, "b2"), Clip("c003", 4, 9.2, 10.6, "b2")])
    Pipeline(cfg).refine(rough)
    j0 = next(j for j in final_junctions(ws) if (j.out_shot, j.in_shot) == (4, 3))
    assert j0.baseline.stage != "roughcut"  # a pass made this junction: the rough cut never paired the shots
    pin = Pin(4, 3, round(j0.baseline.out_time, 3), round(j0.baseline.in_time, 3), origin="reverted")
    assert round(pin.in_time, 2) == 7.9
    PinStore(ws).put(pin)
    final = Pipeline(cfg).refine(rough)

    outcome = pin_outcome(final, pin)
    assert outcome is not None and outcome.action == "pinned", outcome
    moves = editor_decisions(dict(load_stage_timelines(ws))["matchcut"], pin, "pin_moved")
    assert len(moves) == 1 and moves[0].clip_ids == ["c002", "c003"]  # the piece holding 7.9, not the first clip of shot 3
    a, b = pair_of(final, pin.key)
    assert b.locked_in and abs(b.source_in - 7.9) <= 0.5 / final.fps + 1e-6  # kept, snapped to the frame grid
    pieces = [c for c in final.clips if c.shot_index == 3]
    assert pieces[0] is not b and pieces[1] is b  # shot 3's pieces keep their source order
    assert not pieces[0].locked_in


# -- 9. clip-instance lineage: origins travel through every pass and a pin finds its pieces by them ----------
def two_pieces_then_y() -> List[Clip]:
    return [Clip("c001", 3, 6.5, 7.6, "b2"), Clip("c002", 3, 7.9, 9.0, "b2"), Clip("c003", 4, 9.2, 10.6, "b2")]


def test_every_clip_of_every_stage_carries_the_origin_of_a_rough_cut_clip(base_ws, tmp_path):
    ws, cfg = own_copy(base_ws, tmp_path)
    rough = hand_built(ws, cfg, two_pieces_then_y())
    final = Pipeline(cfg).refine(rough)
    keys = {clip_origin(c) for c in rough.clips}
    assert keys == {"3@6.500-7.600", "3@7.900-9.000", "4@9.200-10.600"}
    stages = dict(load_stage_timelines(ws))
    assert len(stages) > 2
    for name, tl in stages.items():
        if name == "roughcut":
            assert all(c.origin == "" for c in tl.clips)  # written before refine: clip_origin computes the same key
            continue
        assert tl.clips and all(c.origin in keys for c in tl.clips), name
    assert {c.origin for c in final.clips} <= keys and all(c.origin for c in final.clips)
    # the default workspace's rough cut too
    base_final = Pipeline(cfg).refine(Timeline.load(base_ws / "roughcut.json"))
    base_keys = {clip_origin(c) for c in Timeline.load(base_ws / "roughcut.json").clips}
    assert all(c.origin in base_keys for c in base_final.clips)


def test_a_pin_taken_at_final_stage_points_finds_its_pieces_by_their_origins_after_a_second_run(base_ws, tmp_path):
    ws, cfg = own_copy(base_ws, tmp_path)
    rough = hand_built(ws, cfg, two_pieces_then_y())
    final = Pipeline(cfg).refine(rough)
    j = junction_of(ws, (4, 3))  # the junction the match-cut reorder made: shot 4 -> the second piece of shot 3
    a, b = final.clip_by_id(j.out_clip), final.clip_by_id(j.in_clip)
    assert b.origin == "3@7.900-9.000"
    # exactly what the Edit view writes for "lock" at the final stage
    pin = Pin(j.out_shot, j.in_shot, round(j.current.out_time, 3), round(j.current.in_time, 3), "locked", out_origin=clip_origin(a), in_origin=clip_origin(b))
    PinStore(ws).put(pin)
    again = Pipeline(cfg).refine(rough)
    outcome = pin_outcome(again, pin)
    assert outcome is not None and outcome.action == "pinned", outcome
    a2, b2 = pair_of(again, pin.key)
    assert (a2.origin, b2.origin) == (pin.out_origin, pin.in_origin)
    assert a2.locked_out and b2.locked_in
    assert not any(d.action == "pin_unplaced" for d in editor_decisions(again, pin))
