"""Shared fixtures: synthetic footage rendered once per session with ffmpeg."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from matchcut.pipeline.demo import make_click_track, make_demo_footage
from matchcut.pipeline.llm import MockBackend

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


class RecordingMock(MockBackend):
    """Mock backend that keeps the prompt text of every call, keyed by request name."""

    def __init__(self, cfg, log=None):
        super().__init__(cfg, log)
        self.texts = {}

    def structured(self, request):
        self.texts.setdefault(request.name, []).append(request.text)
        return super().structured(request)


def moving_box(bg: tuple, x_expr: str, y: int = 60, size: int = 50) -> str:
    cond = f"between(X,{x_expr},{x_expr}+{size})*between(Y,{y},{y + size})"
    r, g, b = bg
    return (
        "color=c=black:s={w}x{h}:r={fps},format=rgb24,"
        f"geq=r='if({cond},255,{r})':g='if({cond},255,{g})':b='if({cond},255,{b})'"
    )


# shot layout of the match-cut fixture (seconds)
MC_SHOTS = [
    (moving_box((32, 64, 128), "20+80*T"), 2.5),  # 0: box moving right, ends near x=220
    ("smptebars=s={w}x{h}:r={fps}", 2.0),  # 1: filler
    (moving_box((128, 48, 32), "220+80*T"), 1.5),  # 2: continues shot 0's motion from x=220
    (moving_box((32, 128, 64), "20"), 2.0),  # 3: static box at the left
    ("testsrc2=s={w}x{h}:r={fps}", 2.0),  # 4: busy pattern
]


@pytest.fixture(scope="session")
def footage_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("footage")


@pytest.fixture(scope="session")
def demo_video(footage_dir: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return make_demo_footage(footage_dir / "demo.mp4")


@pytest.fixture(scope="session")
def matchcut_video(footage_dir: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return make_demo_footage(footage_dir / "mc.mp4", shots=MC_SHOTS)


@pytest.fixture(scope="session")
def click_track(footage_dir: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return make_click_track(footage_dir / "click.wav", seconds=40.0, bpm=120.0)
