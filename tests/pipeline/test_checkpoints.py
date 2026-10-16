import threading

import pytest

from matchcut.pipeline import events
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.passes import run_passes
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Timeline
from tests.pipeline.conftest import RecordingMock


@pytest.fixture(scope="module")
def ctx(matchcut_video, tmp_path_factory):
    cfg = PipelineConfig(source=str(matchcut_video), workspace=str(tmp_path_factory.mktemp("ws")), target_duration=6.0)
    cfg.llm.backend = "mock"
    pipe = Pipeline(cfg)
    pipe.llm = RecordingMock(cfg.llm)
    ctx = pipe.narrative()
    ctx.narrative = {
        "beats": [
            {"id": "b1", "title": "A", "pace": "medium", "target_duration_s": 3.0, "shot_ids": [0, 2], "transition_in": "cut"},
            {"id": "b2", "title": "B", "pace": "fast", "target_duration_s": 3.0, "shot_ids": [3, 4, 1], "transition_in": "dissolve"},
        ]
    }
    return ctx


def base_timeline(ctx) -> Timeline:
    tl = Timeline(source=ctx.media.path, fps=ctx.media.fps)
    tl.clips = [
        Clip("c001", 0, 0.5, 2.5, "b1"),
        Clip("c002", 2, 4.5, 6.0, "b1"),
        Clip("c003", 3, 6.0, 8.0, "b2"),
        Clip("c004", 4, 8.0, 10.0, "b2"),
        Clip("c005", 1, 2.5, 4.5, "b2"),
    ]
    return tl


def test_run_passes_emits_stage_and_decision_events(ctx, tmp_path):
    seen = []
    run_passes(base_timeline(ctx), ctx, ["matchcut", "pacing"], save_dir=tmp_path, on_event=seen.append)
    kinds = [type(e).__name__ for e in seen]
    assert kinds[0] == "StageStarted" and seen[0].stage == "pass:matchcut"
    finished = [e for e in seen if isinstance(e, events.StageFinished)]
    assert [e.stage for e in finished] == ["pass:matchcut", "pass:pacing"]
    assert finished[0].path.endswith("01_matchcut.json") and finished[0].seconds >= 0
    decisions = [e for e in seen if isinstance(e, events.Decision)]
    assert decisions and all(d.pass_name in ("matchcut", "pacing") for d in decisions)
    assert any(d.action == "match_cut" for d in decisions)
    # events are in order: every Decision of a pass sits between its StageStarted and StageFinished
    idx = {id(e): i for i, e in enumerate(seen)}
    start = next(i for i, e in enumerate(seen) if isinstance(e, events.StageStarted) and e.stage == "pass:pacing")
    end = next(i for i, e in enumerate(seen) if isinstance(e, events.StageFinished) and e.stage == "pass:pacing")
    assert all(start < idx[id(d)] < end for d in decisions if d.pass_name == "pacing")


def test_after_pass_redo_reruns_the_pass_with_a_note(ctx, tmp_path):
    answers = iter([events.Redo("let the opening breathe"), events.Continue(), events.Continue()])
    calls = []

    def after_pass(name, result):
        calls.append(name)
        return next(answers)

    seen = []
    final = run_passes(base_timeline(ctx), ctx, ["pacing", "transitions"], save_dir=tmp_path, on_event=seen.append, after_pass=after_pass)
    assert calls == ["pacing", "pacing", "transitions"]
    assert [e.stage for e in seen if isinstance(e, events.StageStarted)] == ["pass:pacing", "pass:pacing", "pass:transitions"]
    assert ctx.notes["pass:pacing"] == ["let the opening breathe"]
    notes = [d for d in final.history if d.pass_name == "pacing" and d.action == "note"]
    assert len(notes) == 1 and notes[0].detail == "let the opening breathe"
    assert any(isinstance(e, events.Decision) and e.action == "note" and e.detail == "let the opening breathe" for e in seen)
    assert (tmp_path / "01_pacing.json").exists() and (tmp_path / "02_transitions.json").exists()
    ctx.notes.clear()


def test_after_pass_abort_raises_run_aborted(ctx, tmp_path):
    with pytest.raises(events.RunAborted) as info:
        run_passes(base_timeline(ctx), ctx, ["pacing", "transitions"], save_dir=tmp_path, after_pass=lambda name, result: events.Abort())
    assert info.value.stage == "pass:pacing"
    assert not (tmp_path / "02_transitions.json").exists()


def test_cancel_flag_stops_before_the_next_pass(ctx, tmp_path):
    cancel = threading.Event()

    def on_event(e):
        if isinstance(e, events.StageFinished) and e.stage == "pass:pacing":
            cancel.set()

    with pytest.raises(events.RunAborted) as info:
        run_passes(base_timeline(ctx), ctx, ["pacing", "transitions", "polish"], save_dir=tmp_path, on_event=on_event, cancel=cancel)
    assert info.value.stage == "pass:transitions"
    assert (tmp_path / "01_pacing.json").exists() and not (tmp_path / "02_transitions.json").exists()


def test_run_passes_without_callbacks_is_unchanged(ctx, tmp_path):
    final = run_passes(base_timeline(ctx), ctx, ["matchcut", "continuity"], save_dir=tmp_path)
    assert final.validate(ctx.shot_bounds) == []
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["01_matchcut.json", "02_continuity.json"]
