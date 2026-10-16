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
