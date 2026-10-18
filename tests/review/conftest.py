"""Shared fixtures: synthetic footage rendered once per session with ffmpeg."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from matchcut.pipeline.demo import make_demo_footage


@pytest.fixture(scope="session")
def footage_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("footage")


@pytest.fixture(scope="session")
def demo_video(footage_dir: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return make_demo_footage(footage_dir / "demo.mp4")


@pytest.fixture(scope="session")
def silent_video(footage_dir: Path) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return make_demo_footage(footage_dir / "silent.mp4", audio=False)
