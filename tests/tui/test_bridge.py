import threading
import time

import pytest

from matchcut.pipeline import events
from matchcut.pipeline.config import PipelineConfig
from matchcut.tui.bridge import PipelineBridge
from tests.pipeline.conftest import RecordingMock


def make_cfg(demo_video, tmp_path, **kw) -> PipelineConfig:
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0, **kw)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    return cfg


def pump(bridge, until, timeout=120.0):
    """Drain events until ``until(events_so_far)`` is true; returns all events."""
    seen = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        seen += bridge.drain()
        if until(seen):
            return seen
        time.sleep(0.05)
    raise TimeoutError(f"bridge did not reach the condition; last events: {[type(e).__name__ for e in seen[-5:]]}")


def test_full_run_streams_events_and_finishes(demo_video, tmp_path):
    bridge = PipelineBridge(make_cfg(demo_video, tmp_path))
    bridge.start()
    with pytest.raises(RuntimeError):
        bridge.start()
    seen = pump(bridge, lambda s: any(isinstance(e, events.RunFinished) for e in s))
    bridge.join(10)
    assert not bridge.is_running()
    kinds = [type(e).__name__ for e in seen]
    assert kinds[0] == "RunStarted" and kinds[-1] == "RunFinished"
    assert kinds.count("Checkpoint") == 3 + len(bridge.cfg.passes)  # default handler continued every one
    assert bridge.pending is None


def test_checkpoint_blocks_until_answered_and_redo_carries_a_note(demo_video, tmp_path):
    cfg = make_cfg(demo_video, tmp_path)
    mock = RecordingMock(cfg.llm)
    bridge = PipelineBridge(cfg, backend=mock, interactive=True)
    bridge.start()
    pump(bridge, lambda s: bridge.pending is not None and bridge.pending[0] == "describe")
    assert bridge.answer(events.Continue())
    pump(bridge, lambda s: bridge.pending is not None and bridge.pending[0] == "narrative")
    time.sleep(0.3)
    assert bridge.pending[0] == "narrative"  # still blocked: nothing advanced on its own
    assert bridge.answer(events.Redo("end on the bird"))
    pump(bridge, lambda s: bridge.pending is not None and bridge.pending[0] == "narrative" and len(mock.texts["narrative"]) == 2)
    assert "- end on the bird" in mock.texts["narrative"][1]
    # answer everything else with Continue
    while bridge.is_running():
        if bridge.pending is not None:
            bridge.answer(events.Continue())
        time.sleep(0.05)
    seen = bridge.drain()
    assert any(isinstance(e, events.RunFinished) for e in seen)
    assert not bridge.answer(events.Continue())  # nothing pending any more


def test_stop_during_a_pending_checkpoint_aborts(demo_video, tmp_path):
    bridge = PipelineBridge(make_cfg(demo_video, tmp_path), interactive=True)
    bridge.start()
    pump(bridge, lambda s: bridge.pending is not None)
    bridge.stop()
    seen = pump(bridge, lambda s: any(isinstance(e, events.RunFailed) for e in s))
    bridge.join(10)
    failed = next(e for e in seen if isinstance(e, events.RunFailed))
    assert failed.error == "aborted" and failed.stage == "describe"
    assert not bridge.is_running() and bridge.pending is None


def test_stop_without_a_checkpoint_cancels_at_the_next_boundary(demo_video, tmp_path):
    bridge = PipelineBridge(make_cfg(demo_video, tmp_path))
    bridge.start()
    pump(bridge, lambda s: any(isinstance(e, events.StageFinished) and e.stage == "ingest" for e in s))
    bridge.stop()
    seen = pump(bridge, lambda s: any(isinstance(e, events.RunFailed) for e in s))
    assert next(e for e in seen if isinstance(e, events.RunFailed)).error == "aborted"


def test_redo_resaves_config_and_set_descriptions_reaches_the_context(demo_video, tmp_path):
    cfg = make_cfg(demo_video, tmp_path)
    mock = RecordingMock(cfg.llm)
    bridge = PipelineBridge(cfg, backend=mock, interactive=True)
    bridge.start()
    pump(bridge, lambda s: bridge.pending is not None and bridge.pending[0] == "describe")
    descs = dict(bridge.pipeline._ctx.descriptions)
    descs[0] = dict(descs[0], usable=False, subject="EDITED SUBJECT")
    bridge.set_descriptions(descs)
    cfg.brief = "a brief set while paused"
    assert bridge.answer(events.Continue())
    pump(bridge, lambda s: bridge.pending is not None and bridge.pending[0] == "narrative")
    assert "EDITED SUBJECT" in mock.texts["narrative"][0]
    bridge.answer(events.Redo("again"))
    pump(bridge, lambda s: len(mock.texts["narrative"]) == 2)
    saved = PipelineConfig.load(tmp_path / "ws" / "config.json")
    assert saved.brief == "a brief set while paused"
    bridge.stop()
    bridge.join(30)
