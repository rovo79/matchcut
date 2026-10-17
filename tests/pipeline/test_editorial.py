"""Editorial pins: storage, application before the passes, prompt constraints, regeneration."""
from __future__ import annotations

from pathlib import Path

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore, apply_pins, pin_constraints
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Timeline
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
