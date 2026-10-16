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
    original = base_timeline(ctx)
    final = run_passes(original, ctx, ["pacing", "transitions"], save_dir=tmp_path, on_event=seen.append, after_pass=after_pass)
    assert all(d.action != "note" for d in original.history)  # the caller's timeline is not mutated
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


def test_redo_at_narrative_reruns_with_the_note(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    answers = {"narrative": iter([events.Redo("end on the bird"), events.Continue()])}

    def handler(stage, summary):
        return next(answers.get(stage, iter([])), events.Continue())

    seen = []
    pipe = Pipeline(cfg, on_event=seen.append, on_checkpoint=handler)
    pipe.llm = RecordingMock(cfg.llm)
    pipe.run()
    assert [e.stage for e in seen if isinstance(e, events.StageStarted) and e.stage == "narrative"] == ["narrative", "narrative"]
    assert len(pipe.llm.texts["narrative"]) == 2
    assert "end on the bird" not in pipe.llm.texts["narrative"][0]
    assert "- end on the bird" in pipe.llm.texts["narrative"][1]
    assert "- end on the bird" not in pipe.llm.texts["rough_cut"][0]  # notes are per stage
    assert len(pipe.llm.texts["describe"]) == 1  # upstream stage not re-run
    assert any(isinstance(e, events.Decision) and e.action == "note" and e.pass_name == "narrative" and e.detail == "end on the bird" for e in seen)
    assert (tmp_path / "ws" / "final.json").exists()
    # the stage was re-run on a cleared cache: narrative.json exists and the roughcut after it was computed once
    assert len(pipe.llm.texts["rough_cut"]) == 1


def test_redo_at_describe_invalidates_downstream(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()  # populate every cache
    assert (tmp_path / "ws" / "narrative.json").exists() and (tmp_path / "ws" / "passes" / "01_matchcut.json").exists()
    pipe = Pipeline(PipelineConfig.load(tmp_path / "ws" / "config.json"))
    pipe.llm = RecordingMock(cfg.llm)
    pipe.invalidate_from("describe")
    assert not (tmp_path / "ws" / "descriptions.json").exists()
    assert not (tmp_path / "ws" / "narrative.json").exists()
    assert not (tmp_path / "ws" / "roughcut.json").exists()
    assert not list((tmp_path / "ws" / "passes").glob("*.json"))
    assert not (tmp_path / "ws" / "final.json").exists() and not (tmp_path / "ws" / "report.md").exists()
    assert (tmp_path / "ws" / "shots.json").exists()  # ingest is never invalidated by a checkpoint
    pipe.run()
    assert set(pipe.llm.texts) >= {"describe", "narrative", "rough_cut"}


def test_abort_at_roughcut_emits_run_failed_and_raises(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    seen = []

    def handler(stage, summary):
        return events.Abort() if stage == "roughcut" else events.Continue()

    with pytest.raises(events.RunAborted):
        Pipeline(cfg, on_event=seen.append, on_checkpoint=handler).run()
    failed = seen[-1]
    assert isinstance(failed, events.RunFailed) and failed.stage == "roughcut" and failed.error == "aborted"
    assert not (tmp_path / "ws" / "final.json").exists()
    assert (tmp_path / "ws" / "roughcut.json").exists()  # the stage ran; the run stopped after it


def test_cancel_during_run_stops_at_the_next_boundary(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cancel = threading.Event()
    seen = []

    def on_event(e):
        seen.append(e)
        if isinstance(e, events.StageFinished) and e.stage == "narrative":
            cancel.set()

    with pytest.raises(events.RunAborted):
        Pipeline(cfg, on_event=on_event, cancel=cancel).run()
    assert isinstance(seen[-1], events.RunFailed) and seen[-1].error == "aborted" and seen[-1].stage == "roughcut"
    assert not (tmp_path / "ws" / "roughcut.json").exists()


def test_cli_style_run_without_handlers_is_unchanged(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    final = Pipeline(cfg).run()
    assert final.clips and (tmp_path / "ws" / "report.md").exists()


def test_redo_at_roughcut_through_the_loop(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    answers = {"roughcut": iter([events.Redo("start on the sky"), events.Continue()])}
    seen = []
    pipe = Pipeline(cfg, on_event=seen.append, on_checkpoint=lambda stage, summary: next(answers.get(stage, iter([])), events.Continue()))
    pipe.llm = RecordingMock(cfg.llm)
    pipe.run()
    assert [e.stage for e in seen if isinstance(e, events.StageStarted) and e.stage == "roughcut"] == ["roughcut", "roughcut"]
    assert len(pipe.llm.texts["rough_cut"]) == 2 and "- start on the sky" in pipe.llm.texts["rough_cut"][1]
    assert len(pipe.llm.texts["narrative"]) == 1
