"""The session on a workspace the (mock) pipeline made, re-run with an editor pin: r000 keeps the pin held, the context is read from the
workspace (exact rate, shots, pins), and the pinned cut is protected through the session."""
from __future__ import annotations

import copy
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from matchcut.edit import session as S
from matchcut.edit.rules import describe
from matchcut.interchange.common import source_info
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.demo import make_demo_footage
from matchcut.pipeline.editorial import Pin, PinStore
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.shots import load_shots

from .test_session import outside_edits


@pytest.fixture(scope="module")
def pinned_world(tmp_path_factory) -> SimpleNamespace:
    """A finished mock-backend workspace, re-run with a pin on its first beat-boundary junction."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("pipeline")
    video = make_demo_footage(root / "demo.mp4")
    ws = root / "ws"
    cfg = PipelineConfig(source=str(video), workspace=str(ws), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()
    j0 = next(j for j in build_junctions(load_stage_timelines(ws)) if j.baseline.stage == "roughcut" and j.beat_boundary)
    pin = Pin(j0.out_shot, j0.in_shot, round(j0.current.out_time, 3), round(j0.current.in_time, 3))
    PinStore(ws).put(pin)
    Pipeline(copy.deepcopy(cfg)).refine()
    return SimpleNamespace(ws=ws, video=video, pin=pin)


@pytest.fixture
def pws(pinned_world, tmp_path) -> Path:
    ws = tmp_path / "ws"
    shutil.copytree(pinned_world.ws, ws)
    return ws


def test_the_session_reads_the_pipeline_workspace_and_keeps_its_pin(pinned_world, pws):
    ctx = S.workspace_context(pws)
    assert ctx.rate == source_info(str(pinned_world.video)).rate
    assert ctx.bounds == {s.index: (s.start, s.end) for s in load_shots(pws / "shots.json")}
    assert [(p.out_shot, p.in_shot) for p in ctx.pins] == [(pinned_world.pin.out_shot, pinned_world.pin.in_shot)]
    before = outside_edits(pws)
    S.start(pws)
    s = S.load(pws)
    head = describe(s.timeline(), ctx)
    pinned = [j for j in head["junctions"] if j["pinned"]]
    assert [j["pin"] for j in pinned] == [f"s{pinned_world.pin.out_shot}->s{pinned_world.pin.in_shot}"]  # r000 still holds the pin
    j = pinned[0]
    with pytest.raises(S.Refused, match="pin"):
        S.propose(pws, [{"op": "roll", "junction": j["id"], "frames": 2}])
    other = next(c for c in head["clips"] if c["id"] not in (j["out"], j["in"]) and c["frames"] > 40)
    p = S.propose(pws, [{"op": "trim", "clip": other["id"], "edge": "out", "frames": -2}])
    assert p["changes"] == [f"{other['id']}: out -2f"]
    S.apply(pws, p["proposal"])
    after = describe(S.load(pws).timeline(), ctx)
    assert [x["pin"] for x in after["junctions"] if x["pinned"]] == [pinned[0]["pin"]]
    assert outside_edits(pws) == before  # the session wrote only WS/edits/
