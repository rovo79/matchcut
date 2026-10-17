import json

from matchcut.pipeline.config import PipelineConfig
from matchcut.tui.app import MatchcutApp
from matchcut.tui.views.shots import ShotsView
from tests.tui.conftest import run_async, wait_for


def test_shots_view_lists_shots_and_toggles_usable(mock_workspace):
    run_async(_shots(mock_workspace))


async def _shots(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    app = MatchcutApp(cfg)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        view = app.query_one(ShotsView)
        table = view.query_one("#shots-table")
        assert table.row_count == 8
        table.focus()
        await pilot.press("down")
        await wait_for(pilot, lambda: view.current_shot_index() == 1)
        await wait_for(pilot, lambda: view.query_one("#shot-mid").image_mode == "half-block")
        assert app.state.image_mode == "half-block"
        desc = json.loads((mock_workspace / "descriptions.json").read_text())["1"]["description"]
        assert desc and desc in str(view.query_one("#shot-desc").content)
        assert "Shot 1" in str(view.query_one("#shot-desc").content)
        before = json.loads((mock_workspace / "descriptions.json").read_text())["1"]["usable"]
        await pilot.press("u")
        await pilot.pause()
        after = json.loads((mock_workspace / "descriptions.json").read_text())["1"]["usable"]
        assert after is (not before)
        assert ("no" if not after else "yes") in str(table.get_row("1")[8])
        await pilot.press("u")  # restore
        await pilot.pause()
        assert json.loads((mock_workspace / "descriptions.json").read_text())["1"]["usable"] is before


class StubBridge:
    """Records set_descriptions; just enough of PipelineBridge for the app's pump."""

    def __init__(self):
        self.calls = []

    def set_descriptions(self, descriptions):
        self.calls.append(descriptions)

    def drain(self):
        return []

    def is_running(self):
        return False

    def stop(self):
        pass


def test_usable_toggle_reaches_the_bridge(mock_workspace):
    run_async(_bridge(mock_workspace))


async def _bridge(mock_workspace):
    path = mock_workspace / "descriptions.json"
    original = path.read_text()
    try:
        cfg = PipelineConfig.load(mock_workspace / "config.json")
        app = MatchcutApp(cfg)
        bridge = StubBridge()
        async with app.run_test(size=(140, 44)) as pilot:
            app.bridge = bridge
            await pilot.press("2")
            await pilot.pause()
            view = app.query_one(ShotsView)
            await wait_for(pilot, lambda: view.current_shot_index() == 0)
            before = json.loads(original)["0"]["usable"]
            await pilot.press("u")
            await pilot.pause()
            assert len(bridge.calls) == 1
            assert bridge.calls[0][0]["usable"] is (not before)
            assert "usable: %s" % (not before) in str(view.query_one("#shot-desc").content)
            await pilot.press("u")
            await pilot.pause()
            assert len(bridge.calls) == 2
            assert bridge.calls[1][0]["usable"] is before
            assert "usable: %s" % before in str(view.query_one("#shot-desc").content)
    finally:
        path.write_text(original)


def test_shots_view_with_an_empty_workspace(tmp_path):
    run_async(_empty(tmp_path))


async def _empty(tmp_path):
    cfg = PipelineConfig(source=str(tmp_path / "missing.mp4"), workspace=str(tmp_path / "ws"))
    app = MatchcutApp(cfg)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        view = app.query_one(ShotsView)
        assert view.query_one("#shots-table").row_count == 0
        assert view.current_shot_index() is None
        assert "no shots" in str(view.query_one("#shot-desc").content)
        await pilot.press("u", "down")
        await pilot.pause()


def test_refresh_is_idempotent_when_nothing_changed(mock_workspace):
    run_async(_idempotent(mock_workspace))


async def _idempotent(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    app = MatchcutApp(cfg)
    async with app.run_test(size=(140, 44)) as pilot:
        await pilot.press("2")
        await pilot.pause()
        view = app.query_one(ShotsView)
        table = view.query_one("#shots-table")
        await wait_for(pilot, lambda: view.query_one("#shot-mid").image_mode == "half-block")
        table.move_cursor(row=3)
        await wait_for(pilot, lambda: view.current_shot_index() == 3)
        await wait_for(pilot, lambda: view._shown is not None and view._shown[0] == 3)
        shown = []
        original = view.query_one("#shot-mid").show
        view.query_one("#shot-mid").show = lambda *a, **k: (shown.append(1), original(*a, **k))[1]
        rows = list(table.rows)
        for _ in range(5):
            app.refresh_views()
        await pilot.pause()
        assert shown == []  # no frame reload
        assert list(table.rows) == rows and table.cursor_row == 3
