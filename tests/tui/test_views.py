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
        assert "Shot 1" in str(view.query_one("#shot-desc").content) or "shot" in str(view.query_one("#shot-desc").content).lower()
        before = json.loads((mock_workspace / "descriptions.json").read_text())["1"]["usable"]
        await pilot.press("u")
        await pilot.pause()
        after = json.loads((mock_workspace / "descriptions.json").read_text())["1"]["usable"]
        assert after is (not before)
        assert ("no" if not after else "yes") in str(table.get_row("1")[8])
        await pilot.press("u")  # restore
        await pilot.pause()
        assert json.loads((mock_workspace / "descriptions.json").read_text())["1"]["usable"] is before
