"""Review keys in the Edit workbench: p / a / s, the status line, non-blocking preparation, and the controller's lifetime."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import pytest

from matchcut.pipeline.config import PipelineConfig
from matchcut.review import controller as controller_mod, items as items_mod
from matchcut.review.items import ReviewItem, ReviewPair
from matchcut.review.mpv import MpvSession
from matchcut.review.window import ReviewError
from matchcut.tui.app import MatchcutApp
from matchcut.tui.views.edit import EditView
from tests.tui.conftest import run_async, wait_for
from tests.tui.test_edit import app_for, own_copy


def status_of(app) -> str:
    return str(app.query_one("#review-status").content)


def notified(app, needle: str, severity: str = None) -> bool:
    return any(needle in n.message and (severity is None or n.severity == severity) for n in app._notifications)


class FakeController:
    instances = []

    def __init__(self, cfg, *args, **kwargs):
        self.cfg = cfg
        self.mpv = True
        self.calls = []
        self.status = ""
        self.pair = None
        FakeController.instances.append(self)

    def has_mpv(self):
        return self.mpv

    def play_pair(self, pair):
        self.calls.append("play_pair")
        self.pair = pair
        self.status = f"playing {(pair.a or pair.b).label} (fake)"

    def toggle(self):
        self.calls.append("toggle")
        self.status = "toggled (fake)"
        return True

    def stop(self):
        self.calls.append("stop")
        self.status = "paused"

    def open_file(self, path):
        self.calls.append(("open_file", Path(path)))
        self.status = "opened"

    def close(self):
        self.calls.append("close")


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    FakeController.instances = []
    prepared = []
    state = {"gate": None, "error": None, "note": ""}

    def item(stage, index, label):
        path = tmp_path / f"{stage}_{index}_{label.strip() or 'b'}.mp4"
        path.write_bytes(b"x")
        return ReviewItem(stage, index, label, path, "render", 2.0, 4.0, "cut", False)

    def prepare(ws, cfg, stage=None, index=0, ab=False, force_render=False):
        prepared.append({"stage": stage, "index": index, "ab": ab, "force_render": force_render})
        if state["gate"] is not None:
            assert state["gate"].wait(30)
        if state["error"] is not None:
            raise state["error"]
        b = item(stage, index, "B  " if ab else "")
        return ReviewPair(b, item(stage, index, "A  ") if ab else None, state["note"])

    monkeypatch.setattr(controller_mod, "ReviewController", FakeController)
    monkeypatch.setattr(items_mod, "prepare_review", prepare)
    return type("Fakes", (), {"prepared": prepared, "state": state, "controller": staticmethod(lambda: FakeController.instances[-1])})


async def edit_ready(pilot, app):
    await pilot.press("4")
    view = app.query_one(EditView)
    await wait_for(pilot, lambda: len(view.junctions) >= 3)
    return view


def test_p_prepares_the_selected_junction_and_plays_it(mock_workspace, fakes):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            assert status_of(app).startswith("p play")
            await pilot.press("p")
            await wait_for(pilot, lambda: status_of(app).startswith("playing"))
            ctl = fakes.controller()
            assert ctl.calls == ["play_pair"]
            assert fakes.prepared == [{"stage": view.current_stage(), "index": view.current_junction().index, "ab": False, "force_render": False}]
            assert status_of(app) == ctl.status

    run_async(body())


def test_a_plays_the_comparison_then_toggles_on_the_same_junction(mock_workspace, fakes):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            await pilot.press("a")
            await wait_for(pilot, lambda: status_of(app).startswith("playing A"))
            ctl = fakes.controller()
            assert fakes.prepared[0]["ab"] is True and ctl.calls == ["play_pair"]
            await pilot.press("a")
            await wait_for(pilot, lambda: status_of(app) == "toggled (fake)")
            assert ctl.calls == ["play_pair", "toggle"] and len(fakes.prepared) == 1
            await pilot.press("right")
            await pilot.press("a")
            await wait_for(pilot, lambda: len(fakes.prepared) == 2 and ctl.calls == ["play_pair", "toggle", "play_pair"])
            assert fakes.prepared[1]["index"] == view.current_junction().index

    run_async(body())


def test_s_stops(mock_workspace, fakes):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("s")
            await wait_for(pilot, lambda: fakes.controller().calls == ["stop"] and status_of(app) == "paused")

    run_async(body())


def test_without_mpv_the_window_is_rendered(mock_workspace, fakes, monkeypatch):
    monkeypatch.setattr(FakeController, "has_mpv", lambda self: False)

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("p")
            await wait_for(pilot, lambda: status_of(app).startswith("playing"))
            assert fakes.prepared[0]["force_render"] is True

    run_async(body())


def test_without_mpv_ab_opens_the_comparison_file(mock_workspace, fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(FakeController, "has_mpv", lambda self: False)
    monkeypatch.setattr(items_mod, "ab_file", lambda pair, ws, cfg: tmp_path / "ab.mp4")

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("a")
            await wait_for(pilot, lambda: status_of(app) == "opened")
            assert fakes.prepared[0]["ab"] is True and fakes.prepared[0]["force_render"] is True
            assert fakes.controller().calls == [("open_file", tmp_path / "ab.mp4")]

    run_async(body())


def test_a_review_error_is_shown_and_the_app_keeps_running(mock_workspace, fakes):
    fakes.state["error"] = ReviewError("source footage not found: /x.mp4")

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            await pilot.press("p")
            await wait_for(pilot, lambda: status_of(app) == "review failed: source footage not found: /x.mp4")
            await wait_for(pilot, lambda: notified(app, "source footage not found: /x.mp4", "error"))
            await pilot.press("right")
            assert view.selected == 1 and status_of(app).startswith("p play")

    run_async(body())


def test_a_pair_note_is_appended_and_notified_once(mock_workspace, fakes):
    fakes.state["note"] = "nothing earlier to compare"

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("a")
            await wait_for(pilot, lambda: status_of(app).endswith(" · nothing earlier to compare"))
            assert sum("nothing earlier" in n.message for n in app._notifications) == 1

    run_async(body())


def test_preparation_does_not_block_the_keys_and_duplicates_are_ignored(mock_workspace, fakes):
    gate = fakes.state["gate"] = threading.Event()

    async def body():
        app = app_for(mock_workspace)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                view = await edit_ready(pilot, app)
                await pilot.press("p")
                await wait_for(pilot, lambda: len(fakes.prepared) == 1)
                first = status_of(app)
                await pilot.press("right")
                assert view.selected == 1 and status_of(app).startswith("p play")
                await pilot.press("left")
                assert view.selected == 0 and status_of(app) == first and first.startswith("preparing")
                await pilot.press("p")
                await wait_for(pilot, lambda: notified(app, "already preparing this junction"))
                assert len(fakes.prepared) == 1
                await pilot.press("right")  # leave the junction while it prepares
                gate.set()
                await wait_for(pilot, lambda: fakes.controller().calls == ["play_pair"])
                assert status_of(app).startswith("p play")  # the finished result does not overwrite the selected junction
                await pilot.press("left")
                await wait_for(pilot, lambda: status_of(app).startswith("playing"))
                assert fakes.controller().calls == ["play_pair"] and len(fakes.prepared) == 1
        finally:
            gate.set()

    run_async(body())


def test_the_idle_line_names_the_baseline_stage(mock_workspace, fakes):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            j = view.current_junction()
            assert j.baseline.stage != view.current_stage()
            assert status_of(app) == f"p play · a compare with {j.baseline.stage}"
            for _ in range(len(view.stages)):
                await pilot.press("left_square_bracket")
            await wait_for(pilot, lambda: view.stage_index == 0)
            assert view.current_stage() == view.current_junction().baseline.stage
            assert status_of(app) == "p play"

    run_async(body())


def test_the_app_closes_the_controller_on_exit(mock_workspace, fakes):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            assert app.review_controller() is app.review_controller()
            ctl = fakes.controller()
            await pilot.press("q")
        assert ctl.calls.count("close") == 1

    run_async(body())


def test_statuses_are_cleared_when_the_workspace_changes(mock_workspace, tmp_path, fakes):
    ws = own_copy(mock_workspace, tmp_path)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            await pilot.press("p")
            await wait_for(pilot, lambda: status_of(app).startswith("playing"))
            await pilot.press("l")  # pinning writes editorial.json
            await wait_for(pilot, lambda: status_of(app).startswith("p play"))

    run_async(body())


@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")
def test_a_real_headless_mpv_plays_a_junction(mock_workspace, tmp_path, monkeypatch):
    import subprocess

    ws = own_copy(mock_workspace, tmp_path)
    cfg = PipelineConfig.load(ws / "config.json")
    cfg.workspace = str(ws)
    cfg.review.width = 160
    app = MatchcutApp(cfg)
    real = {}

    def real_controller(self):
        if self._review is None:  # the app's own on_unmount closes it, as in production
            self._review = real["c"] = controller_mod.ReviewController(
                self.state.config, session_factory=lambda *a, **kw: MpvSession(headless=True)
            )
        return self._review

    monkeypatch.setattr(MatchcutApp, "review_controller", real_controller)

    async def body():
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("p")
            await wait_for(pilot, lambda: status_of(app).startswith("playing") or status_of(app).startswith("review failed"), timeout=120)
            assert status_of(app).startswith("playing"), status_of(app)
            await pilot.press("q")
        assert real["c"]._session is None  # closed by the app on exit

    run_async(body())
    assert subprocess.run(["pgrep", "-x", "mpv"], capture_output=True).returncode != 0
