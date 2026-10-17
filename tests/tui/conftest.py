"""Fixtures for the TUI tests: a demo clip and a finished mock workspace."""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Callable

import pytest

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.demo import make_demo_footage
from matchcut.pipeline.pipeline import Pipeline


def run_async(coro):
    """Run an async test body without pytest-asyncio (Textual's run_test is async)."""
    return asyncio.run(coro)


@pytest.fixture(scope="session")
def demo_video(tmp_path_factory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    return make_demo_footage(tmp_path_factory.mktemp("footage") / "demo.mp4")


@pytest.fixture(scope="session")
def mock_workspace(demo_video, tmp_path_factory) -> Path:
    """A workspace with every stage output from a full mock run (no render)."""
    ws = tmp_path_factory.mktemp("ws")
    cfg = PipelineConfig(source=str(demo_video), workspace=str(ws), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()
    return ws


async def wait_for(pilot, predicate: Callable[[], bool], timeout: float = 90.0) -> None:
    """Pause the pilot until ``predicate()`` is true."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("condition not met in time")
        await pilot.pause(0.1)
