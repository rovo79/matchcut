import os
import time

from matchcut.pipeline import events
from matchcut.pipeline.config import PipelineConfig
from matchcut.tui.state import PRICES, RunState, StageStatus


def test_stage_names_follow_the_config(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    st = RunState(cfg)
    assert st.stage_names() == ["ingest", "describe", "narrative", "roughcut"] + [f"pass:{p}" for p in cfg.passes] + ["render", "report"]
    assert all(isinstance(s, StageStatus) and s.state == "pending" for s in st.stages.values())
    assert st.run_status == "idle" and st.checkpoint is None


def test_apply_events_drives_statuses_and_usage(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    st = RunState(cfg)
    st.apply(events.RunStarted(source=cfg.source, workspace=str(mock_workspace), config={}))
    assert st.run_status == "running" and st.started_at is not None
    st.apply(events.StageCached(stage="ingest", path="x/shots.json"))
    st.apply(events.StageStarted(stage="describe"))
    assert st.stages["ingest"].state == "cached" and st.stages["describe"].state == "running"
    st.apply(events.ModelCall(name="describe", images=1, seconds=1.0, input_tokens=1000, output_tokens=200, model="m"))
    assert st.calls == 1 and st.input_tokens == 1000 and st.output_tokens == 200 and st.model == "m"
    st.apply(events.StageFinished(stage="describe", seconds=2.5, path="x/descriptions.json"))
    assert st.stages["describe"].state == "done" and st.stages["describe"].seconds == 2.5
    st.apply(events.Checkpoint(stage="describe", summary={"shots": 8}))
    assert st.run_status == "waiting" and st.checkpoint.stage == "describe" and st.stages["describe"].state == "waiting"
    st.resolve_checkpoint()
    assert st.checkpoint is None and st.run_status == "running" and st.stages["describe"].state == "done"
    st.apply(events.StageStarted(stage="pass:boom"))  # unknown stage names are tolerated
    st.apply(events.RunFailed(stage="pass:boom", error="kaboom", traceback="tb"))
    assert st.run_status == "failed" and st.stages["pass:boom"].state == "failed" and st.last_error == "kaboom"
    assert len(st.events) == 8


def test_run_finished_and_cost(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    st = RunState(cfg)
    st.apply(events.RunStarted(source="", workspace="", config={}))
    st.apply(events.ModelCall(name="x", images=0, seconds=0, input_tokens=1_000_000, output_tokens=100_000, model="claude-opus-5-5"))
    inp, out = PRICES["claude-opus-5-5"]
    assert abs(st.cost_estimate() - (inp + out * 0.1)) < 1e-9
    st.apply(events.RunFinished(final_path="f", render_path="", report_path="r", seconds=12.0))
    assert st.run_status == "finished" and st.finished_at is not None and st.elapsed() >= 0
    st.apply(events.ModelCall(name="x", images=0, seconds=0, input_tokens=1, output_tokens=1, model="unknown-model"))
    assert st.cost_estimate() >= inp + out * 0.1  # unknown models add nothing but never raise


def test_workspace_readers(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    st = RunState(cfg)
    assert st.media() is not None and st.media().fps == 25.0
    assert len(st.shots()) == 8
    assert set(st.descriptions()) == {s.index for s in st.shots()}
    assert st.narrative()["beats"]
    assert st.music() is None
    stages = st.timeline_stages()
    assert stages[0] == "roughcut" and stages[-1] == "final" and "matchcut" in stages
    final = st.timeline("final")
    assert final is not None and final.clips
    assert st.timeline("nope") is None
    cid = final.clips[0].id
    assert isinstance(st.decisions_for(cid, "final"), list)


def test_readers_tolerate_an_empty_workspace(tmp_path):
    cfg = PipelineConfig(source="missing.mp4", workspace=str(tmp_path / "empty"))
    st = RunState(cfg)
    assert st.media() is None and st.shots() == [] and st.descriptions() == {} and st.narrative() == {}
    assert st.music() is None and st.timeline_stages() == [] and st.timeline("final") is None


def test_readers_reload_when_the_file_changes(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    st = RunState(cfg)
    before = st.narrative()["logline"]
    path = mock_workspace / "narrative.json"
    data = path.read_text().replace(before, "A changed logline")
    path.write_text(data)
    os.utime(path, (time.time() + 5, time.time() + 5))  # guarantee a new mtime
    st.invalidate(str(path))
    assert st.narrative()["logline"] == "A changed logline"
    path.write_text(data.replace("A changed logline", before))
    os.utime(path, (time.time() + 10, time.time() + 10))
    assert st.narrative()["logline"] == before  # mtime change alone is enough, no invalidate needed


def test_event_count_keeps_counting_after_the_event_buffer_is_full(mock_workspace):
    st = RunState(PipelineConfig.load(mock_workspace / "config.json"))
    for i in range(5010):
        st.apply(events.Log(message=f"m{i}"))
    assert st.event_count == 5010
    assert len(st.events) == 5000
    st.apply(events.RunStarted())
    assert st.event_count == 1
