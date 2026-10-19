"""Fixtures for the interchange tests: synthetic footage, a finished mock workspace, a music file, optional external tools.

External tools are optional and their tests skip when missing: the OpenTimelineIO venv (``OTIO_VENV_PYTHON``) and, from
Task 2 on, ``xmllint`` with the FCPXML 1.10 DTD (``FCPXML_DTD``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import wave
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from matchcut.interchange.common import RECORD_TOLERANCE_S, export_timeline, record_tolerance_frames, source_info  # noqa: F401 (re-exported to the tests)
from matchcut.interchange.fidelity import differences
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.demo import make_demo_footage
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines, scorer_for_workspace
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.shots import load_shots
from matchcut.pipeline.timeline import Timeline



def _tool(env: str, relative: str) -> Path:
    """An optional external tool: the environment variable wins; the author's scratch location is only a fallback when it exists."""
    if os.environ.get(env):
        return Path(os.environ[env])
    for root in (os.environ.get("INTEGRATION_RESEARCH"), "/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/integration"):
        if root and (Path(root) / relative).exists():
            return Path(root) / relative
    return Path("/nonexistent") / relative  # absent: tests that need it skip


OTIO_VENV_PYTHON = _tool("OTIO_VENV_PYTHON", "venv/bin/python")
FCPXML_DTD = _tool("FCPXML_DTD", "dtd/FCPXMLv1_10.dtd")

def check_equivalent(a: Timeline, b: Timeline, rate: Fraction, tol: int = 0) -> None:
    """Same clips (shot, source frames), transitions (kind, frames) and record length, to ``tol`` frames (``fidelity.differences``)."""
    diffs = differences(a, b, rate, tol)
    assert not diffs, diffs[:3]


def make_cover_art_mp3(path: Path, seconds: float = 10.0) -> Path:
    """A silent MP3 with an attached cover picture: ffprobe lists the picture as a video stream (attached_pic)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    cover = path.with_suffix(".jpg")
    base = [ffmpeg, "-v", "error", "-nostdin", "-y"]
    subprocess.run(base + ["-f", "lavfi", "-i", "color=c=red:s=64x64:r=1", "-frames:v", "1", str(cover)], check=True, capture_output=True)
    subprocess.run(base + ["-f", "lavfi", "-t", str(seconds), "-i", "anullsrc=r=22050:cl=mono", "-i", str(cover), "-map", "0:a", "-map", "1:v",
                           "-c:a", "libmp3lame", "-c:v", "copy", "-id3v2_version", "3", "-disposition:v", "attached_pic", str(path)],
                   check=True, capture_output=True)
    return path


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


def _timecoded(path: Path, timecode: str, rate: str) -> Path:
    """A 3 s clip with video and audio whose container carries a start timecode (what a camera writes)."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate={rate}:duration=3", "-f", "lavfi",
           "-i", "sine=duration=3", "-timecode", timecode, "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


@pytest.fixture(scope="session")
def tc_footage(tmp_path_factory) -> Path:
    """25 fps, start timecode 01:00:00:00 (frame 90000)."""
    return _timecoded(tmp_path_factory.mktemp("tc") / "cam.mp4", "01:00:00:00", "25")


@pytest.fixture(scope="session")
def df_footage(tmp_path_factory) -> Path:
    """29.97 fps, start timecode 01:00:00;00 in drop-frame (frame 107892)."""
    return _timecoded(tmp_path_factory.mktemp("tcdf") / "camdf.mp4", "01:00:00;00", "30000/1001")


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
    return export_timeline(world.by_stage[stage], world.bounds, world.src)
