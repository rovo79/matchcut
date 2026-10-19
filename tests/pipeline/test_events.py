import pytest
import time

from matchcut.pipeline import events
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.pipeline import Pipeline
from tests.pipeline.conftest import RecordingMock


def test_event_dataclasses_serialise_with_type_and_time():
    before = time.time()
    e = events.StageFinished(stage="describe", seconds=1.25, path="/ws/descriptions.json")
    d = e.to_dict()
    assert d["type"] == "StageFinished"
    assert d["stage"] == "describe" and d["seconds"] == 1.25 and d["path"] == "/ws/descriptions.json"
    assert before <= d["time"] <= time.time()
    assert events.Decision(pass_name="matchcut", action="snap", clip_ids=["c001"], detail="x", score=0.5, data={}).to_dict()["type"] == "Decision"


def test_checkpoint_answers():
    assert events.Redo().note == ""
    assert events.Redo("tighter").note == "tighter"
    assert isinstance(events.Continue(), events.Continue) and isinstance(events.Abort(), events.Abort)
    err = events.RunAborted("roughcut")
    assert err.stage == "roughcut" and "roughcut" in str(err)


def test_notes_text_and_prompt_rendering(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    pipe = Pipeline(cfg)
    pipe.llm = RecordingMock(cfg.llm)
    ctx = pipe.ingest()
    assert ctx.notes == {}
    assert ctx.notes_text("narrative") == "Director's notes: (none)"
    ctx.notes["narrative"] = ["end on the bird", "slower opening"]
    assert ctx.notes_text("narrative") == "Director's notes:\n- end on the bird\n- slower opening"
    ctx.notes["describe"] = ["call the rabbit by name"]
    ctx.notes["roughcut"] = ["start on the sky"]
    pipe.rough_cut()
    assert "call the rabbit by name" in pipe.llm.texts["describe"][0]
    assert "- end on the bird" in pipe.llm.texts["narrative"][0]
    assert "start on the sky" in pipe.llm.texts["rough_cut"][0]
    assert "Director's notes: (none)" not in pipe.llm.texts["narrative"][0]


def test_backends_report_model_calls(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    pipe = Pipeline(cfg)
    calls = []
    pipe.llm.on_call = calls.append
    pipe.narrative()
    assert len(calls) == pipe.llm.calls == 2  # describe + narrative with 8 shots on one sheet
    assert [c.name for c in calls] == ["describe", "narrative"]
    assert calls[0].images == 1 and calls[1].images == 0
    assert all(isinstance(c, events.ModelCall) and c.model == cfg.llm.model and c.seconds >= 0 for c in calls)
    assert calls[0].input_tokens == 0 and calls[0].output_tokens == 0  # the mock reports no tokens


def test_run_emits_lifecycle_and_stage_events_in_order(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.proxy = True
    seen = []
    pipe = Pipeline(cfg, on_event=seen.append)
    final = pipe.run()
    kinds = [type(e).__name__ for e in seen]
    assert kinds[0] == "RunStarted" and kinds[-1] == "RunFinished"
    assert seen[0].source == str(demo_video) and seen[0].config["target_duration"] == 8.0
    started = [e.stage for e in seen if isinstance(e, events.StageStarted)]
    assert started[:4] == ["ingest", "describe", "narrative", "roughcut"]
    assert started[4:10] == [f"pass:{p}" for p in cfg.passes]
    assert started[10:] == ["render", "report"]
    finished = {e.stage: e for e in seen if isinstance(e, events.StageFinished)}
    assert finished["describe"].path.endswith("descriptions.json") and finished["roughcut"].path.endswith("roughcut.json")
    assert finished["render"].path.endswith("final_proxy.mp4") and finished["report"].path.endswith("report.md")
    checkpoints = [e.stage for e in seen if isinstance(e, events.Checkpoint)]
    assert checkpoints == ["describe", "narrative", "roughcut"] + [f"pass:{p}" for p in cfg.passes]
    describe_summary = next(e.summary for e in seen if isinstance(e, events.Checkpoint) and e.stage == "describe")
    assert describe_summary["shots"] == 8 and "unusable" in describe_summary
    pass_summary = next(e.summary for e in seen if isinstance(e, events.Checkpoint) and e.stage == "pass:transitions")
    assert pass_summary["pass"] == "transitions" and "decisions" in pass_summary and pass_summary["clips"] == len(final.clips)
    assert any(isinstance(e, events.Log) and "shots detected" in e.message for e in seen)
    assert sum(isinstance(e, events.ModelCall) for e in seen) == pipe.llm.calls
    done = seen[-1]
    assert done.final_path.endswith("final.json") and done.render_path.endswith("final_proxy.mp4") and done.seconds > 0
    assert not any(isinstance(e, events.StageCached) for e in seen)


def test_second_run_reports_cached_stages(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()
    seen = []
    Pipeline(PipelineConfig.load(tmp_path / "ws" / "config.json"), on_event=seen.append).run()
    cached = [e.stage for e in seen if isinstance(e, events.StageCached)]
    assert cached == ["ingest", "describe", "narrative", "roughcut"]
    assert all(e.path for e in seen if isinstance(e, events.StageCached))
    assert [e.stage for e in seen if isinstance(e, events.StageStarted)][:1] == ["pass:matchcut"]
    assert type(seen[-1]).__name__ == "RunFinished"


def test_failure_emits_run_failed_and_reraises(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.passes = ("matchcut", "nosuchpass")
    seen = []
    with pytest.raises(ValueError):
        Pipeline(cfg, on_event=seen.append).run()
    failed = seen[-1]
    assert isinstance(failed, events.RunFailed)
    assert failed.stage == "pass:nosuchpass" and "nosuchpass" in failed.error and "Traceback" in failed.traceback
    assert not any(isinstance(e, events.StageStarted) and e.stage == "pass:matchcut" for e in seen)


def test_failure_inside_a_pass_names_the_pass(demo_video, tmp_path, monkeypatch):
    from matchcut.pipeline import passes as passes_mod

    class Boom(passes_mod.Pass):
        name = "boom"

        def apply(self, timeline, ctx):
            raise RuntimeError("kaboom")

    monkeypatch.setitem(passes_mod.PASSES, "boom", Boom)
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.passes = ("matchcut", "boom")
    seen = []
    with pytest.raises(RuntimeError):
        Pipeline(cfg, on_event=seen.append).run()
    failed = seen[-1]
    assert isinstance(failed, events.RunFailed) and failed.stage == "pass:boom" and "kaboom" in failed.error


def test_log_handler_is_removed_and_level_restored_even_when_a_callback_raises(demo_video, tmp_path):
    import logging

    plog = logging.getLogger("matchcut.pipeline")
    before_handlers, before_level = list(plog.handlers), plog.level
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False

    def explode(event):
        raise RuntimeError("listener broke")

    with pytest.raises(RuntimeError):
        Pipeline(cfg, on_event=explode).run()
    assert plog.handlers == before_handlers and plog.level == before_level
    # and a run with no listener adds no handler at all
    Pipeline(cfg).run()
    assert plog.handlers == before_handlers and plog.level == before_level


def test_anthropic_backend_records_every_attempt(monkeypatch):
    from types import SimpleNamespace
    from matchcut.pipeline import schemas
    from matchcut.pipeline.config import LLMConfig
    from matchcut.pipeline.llm import AnthropicBackend, LLMRequest

    cfg = LLMConfig(backend="anthropic", refusal_fallbacks=False, max_retries=1)
    backend = AnthropicBackend.__new__(AnthropicBackend)
    backend.cfg, backend.log, backend.calls, backend.usage, backend.on_call = cfg, type("L", (), {"write": lambda self, r: None})(), 0, {"input_tokens": 0, "output_tokens": 0}, None
    import anthropic as _anthropic
    backend._anthropic = _anthropic
    responses = iter([
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json")], usage=SimpleNamespace(input_tokens=10, output_tokens=5)),
        SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"pairs": []}')], usage=SimpleNamespace(input_tokens=11, output_tokens=6)),
    ])
    backend._call = lambda kwargs: next(responses)
    seen = []
    backend.on_call = seen.append
    result = backend.structured(LLMRequest(name="verify_pairs", system="s", text="t", schema=schemas.VerifyResponse))
    assert result.pairs == []
    assert backend.calls == 2 and backend.usage == {"input_tokens": 21, "output_tokens": 11}
    assert [c.input_tokens for c in seen] == [10, 11]
