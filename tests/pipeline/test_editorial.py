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
from matchcut.pipeline.timeline import Clip, Timeline, Transition
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


def test_a_pin_whose_shots_are_not_adjacent_is_reported_not_applied():
    out, decisions = apply_pins(cut(), [Pin(1, 0, 3.0, 1.0)], BOUNDS, min_clip=0.75)
    assert [d.action for d in decisions] == ["pin_unplaced"]
    assert "not adjacent" in decisions[0].detail
    assert [(c.source_in, c.source_out) for c in out.clips] == [(c.source_in, c.source_out) for c in cut().clips]


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
def test_a_pin_on_shots_the_rough_cut_does_not_pair_is_recorded_as_not_placed(demo_video, tmp_path):
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
    assert outcome is not None and outcome.action == "pin_unplaced" and "not adjacent" in outcome.detail
    assert pin_outcome(final, Pin(pin.out_shot, pin.in_shot, 1.5, 14.0)) is None  # a pin edited since the run has no outcome yet
    placed = Pin(shots[0], shots[1], rough.clips[0].source_out, rough.clips[1].source_in)
    PinStore(ws).put(placed)
    assert pin_outcome(Pipeline(cfg).refine(), placed).action == "pinned"


def test_pin_store_survives_an_infinite_shot_index(tmp_path):
    good = Pin(0, 2, 2.0, 4.6)
    (tmp_path / "editorial.json").write_text('{"pins": [{"out_shot": Infinity, "in_shot": 1, "out_time": 1.0, "in_time": 2.0}, %s]}' % json.dumps(good.to_dict()))
    assert PinStore(tmp_path).load() == [good]
    (tmp_path / "editorial.json").write_text('{"pins": Infinity}')
    assert PinStore(tmp_path).load() == []
