"""Fixtures for the interchange tests: synthetic footage, a finished mock workspace, a music file, optional external tools.

External tools are optional and their tests skip when missing: the OpenTimelineIO venv (``OTIO_VENV_PYTHON``) and, from
Task 2 on, ``xmllint`` with the FCPXML 1.10 DTD (``FCPXML_DTD``).
"""
from __future__ import annotations

import os
import shutil
import wave
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from matchcut.interchange.common import export_timeline, layout, source_info
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.demo import make_demo_footage
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines, scorer_for_workspace
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.shots import load_shots
from matchcut.pipeline.timeline import Timeline

RESEARCH = Path(os.environ.get("INTEGRATION_RESEARCH", "/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/integration"))
OTIO_VENV_PYTHON = Path(os.environ.get("OTIO_VENV_PYTHON", str(RESEARCH / "venv/bin/python")))
FCPXML_DTD = Path(os.environ.get("FCPXML_DTD", str(RESEARCH / "dtd/FCPXMLv1_10.dtd")))


def check_equivalent(a: Timeline, b: Timeline, rate: Fraction, tol: int = 0) -> None:
    """Same clips (shot, source frames), transitions (kind, frames) and record length, to ``tol`` frames."""
    la, lb = layout(a, rate), layout(b, rate)
    assert [c.shot_index for c in a.clips] == [c.shot_index for c in b.clips]
    for i, (x, y) in enumerate(zip(la, lb)):
        assert abs(x.in_f - y.in_f) <= tol and abs(x.out_f - y.out_f) <= tol, i
        assert x.transition[0] == y.transition[0] and abs(x.tr - y.tr) <= tol, i
        assert abs(x.rec - y.rec) <= tol, i
    assert abs(la[-1].rec_out - lb[-1].rec_out) <= tol


def write_wav(path: Path, seconds: float = 30.0, rate: int = 8000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(seconds * rate))
    return path


@pytest.fixture(scope="session")
def footage(tmp_path_factory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return make_demo_footage(tmp_path_factory.mktemp("footage") / "demo.mp4")


@pytest.fixture(scope="session")
def silent_footage(tmp_path_factory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return make_demo_footage(tmp_path_factory.mktemp("silent") / "silent.mp4", audio=False)


@pytest.fixture(scope="session")
def music_file(tmp_path_factory) -> Path:
    return write_wav(tmp_path_factory.mktemp("music") / "score.wav")


@pytest.fixture(scope="session")
def world(footage, tmp_path_factory) -> SimpleNamespace:
    """A finished mock-backend workspace plus everything an export needs."""
    ws = tmp_path_factory.mktemp("ws")
    cfg = PipelineConfig(source=str(footage), workspace=str(ws), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()
    stages = load_stage_timelines(ws)
    scorer = scorer_for_workspace(ws, cfg)
    jb = {name: build_junctions(stages, scorer, upto=name) for name, _ in stages}
    bounds = {s.index: (s.start, s.end) for s in load_shots(ws / "shots.json")}
    return SimpleNamespace(ws=ws, cfg=cfg, video=str(footage), src=source_info(str(footage)), stages=stages, jb=jb, bounds=bounds,
                           by_stage=dict(stages), scorer=scorer)


def exported(world: SimpleNamespace, stage: str):
    """(snapped timeline, snap notes) of a world stage."""
    return export_timeline(world.by_stage[stage], world.bounds, world.src.rate)
