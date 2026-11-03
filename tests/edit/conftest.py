"""Small real workspaces for the session tests: a short synthetic source file (ffmpeg's test pattern), ``probe.json``, ``config.json``,
``shots.json``, ``roughcut.json``, a pass file and ``final.json``, built the same way every time."""
from __future__ import annotations

import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

import pytest

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.media import probe
from matchcut.pipeline.shots import Shot, save_shots
from matchcut.pipeline.timeline import Clip, Decision, Timeline, Transition

# shots of 100 frames; c003 has an 8-frame dissolve in (at 25 fps)
FRAMES_PER_SHOT = 100
N_SHOTS = 8
CLIPS = [("c001", 0, 10, 60, "b1"), ("c002", 1, 120, 170, "b1"), ("c003", 3, 320, 380, "b2", ("dissolve", 8)),
         ("c004", 5, 520, 570, "b2"), ("c005", 6, 620, 680, "b3")]


def make_video(path: Path, rate: str, seconds: float) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"testsrc=size=64x36:rate={rate}:duration={seconds}",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True, capture_output=True)
    return path


@pytest.fixture(scope="session")
def videos(tmp_path_factory) -> Callable[[str], Path]:
    """One synthetic source per rate ("25", "30000/1001"), shared by every workspace (so their timelines name the same source)."""
    root = tmp_path_factory.mktemp("media")
    made: Dict[str, Path] = {}

    def get(rate: str) -> Path:
        if rate not in made:
            seconds = float(Fraction(N_SHOTS * FRAMES_PER_SHOT) / Fraction(rate)) + 1.0
            made[rate] = make_video(root / f"src-{rate.replace('/', '_')}.mp4", rate, round(seconds, 3))
        return made[rate]

    return get


def write_workspace(ws: Path, video: Path, rate: Fraction, clips: Optional[Sequence[tuple]] = None, seconds: bool = False) -> Path:
    """A finished workspace in ``ws`` whose ``final.json`` holds ``clips`` ((id, shot, in, out, beat[, (kind, length)]) in frames at
    ``rate``, or in seconds with ``seconds``)."""
    ws.mkdir(parents=True, exist_ok=True)
    cfg = PipelineConfig(source=str(video), workspace=str(ws))
    cfg.save(ws / "config.json")
    (ws / "probe.json").write_text(json.dumps(probe(str(video)).to_dict(), indent=2))
    save_shots([Shot(k, float(Fraction(k * FRAMES_PER_SHOT) / rate), float(Fraction((k + 1) * FRAMES_PER_SHOT) / rate))
                for k in range(N_SHOTS)], ws / "shots.json")

    def t(x: float) -> float:
        return float(x) if seconds else round(float(Fraction(x) / rate), 6)

    tl = Timeline(source=str(video), fps=round(float(rate), 3), name="cut")
    for row in clips or CLIPS:
        cid, shot, i, o, beat = row[:5]
        kind, n = row[5] if len(row) > 5 else ("cut", 0)
        tl.clips.append(Clip(cid, shot, t(i), t(o), beat, purpose="story", transition_in=Transition(kind, t(n)) if n else Transition()))
    rough = tl.copy()
    rough.stage = "roughcut"
    rough.history = [Decision("roughcut", "select", [c.id], f"picked shot {c.shot_index}") for c in tl.clips]
    rough.save(ws / "roughcut.json")
    tidy = rough.copy()
    tidy.stage = "tidy"
    tidy.history.append(Decision("tidy", "trim", [tl.clips[1].id], "tightened"))
    tidy.save(ws / "passes" / "01_tidy.json")
    final = tidy.copy()
    final.stage = "final"
    final.save(ws / "final.json")
    return ws


@pytest.fixture
def make_ws(videos, tmp_path) -> Callable[..., Path]:
    """make_ws(name="ws", rate="25", clips=None, seconds=False) -> a fresh workspace under tmp_path."""

    def make(name: str = "ws", rate: str = "25", clips: Optional[Sequence[tuple]] = None, seconds: bool = False) -> Path:
        return write_workspace(tmp_path / name, videos(rate), Fraction(rate), clips, seconds)

    return make
