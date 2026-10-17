"""The Edit workbench: junction selection, evidence, pins, and the hidden-tab frame case."""
from __future__ import annotations

import json
import shutil

from matchcut.pipeline.config import PipelineConfig
from matchcut.tui import images
from matchcut.tui.app import MatchcutApp
from matchcut.tui.views.edit import EditView
from matchcut.tui.widgets.frame import Frame
from matchcut.tui.widgets.timeline import TimelineTrack, junction_columns
from tests.tui.conftest import run_async, wait_for

FRAMES = ("#out-frame", "#in-frame", "#blend-frame")


def app_for(ws):
    cfg = PipelineConfig.load(ws / "config.json")
    cfg.workspace = str(ws)
    return MatchcutApp(cfg)


def own_copy(mock_workspace, tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(mock_workspace, ws)
    return ws


def frames_ready(app):
    return all(app.query_one(f, Frame).image_mode for f in FRAMES)


def image_heights(app):
    return [app.query_one(f).query(".frame-image").first().region.height for f in FRAMES]


def test_a_junction_shows_three_frames_and_its_evidence(mock_workspace):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions) and frames_ready(app))
            assert app.focused is view
            assert min(image_heights(app)) >= 8
            j = view.current_junction()
            panel = str(app.query_one("#junction-panel").content)
            assert f"Junction 1 · {j.out_clip} → {j.in_clip}" in panel
            assert "combined" in panel and "cut points" in panel and "why:" in panel and "history:" in panel
            assert "junctions" in str(app.query_one("#edit-header").content)

    run_async(body())


def test_frames_render_when_the_data_arrived_while_the_tab_was_hidden(mock_workspace):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))  # loaded by refresh_views while tab 1 is shown
            await pilot.press("4")
            await wait_for(pilot, lambda: frames_ready(app) and min(image_heights(app)) >= 8)

    run_async(body())


def test_junctions_are_selected_by_key_and_by_click(mock_workspace):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: len(view.junctions) >= 3)
            await pilot.press("right")
            assert view.selected == 1 and "Junction 2" in str(app.query_one("#junction-panel").content)
            await pilot.press("left")
            assert view.selected == 0
            track = app.query_one("#edit-track", TimelineTrack)
            col = junction_columns(track.timeline, track.size.width, view.selected)[2]
            await pilot.click("#edit-track", offset=(col, 0))
            await wait_for(pilot, lambda: view.selected == 2)
            await pilot.press("enter")
            assert "decisions:" in str(app.query_one("#junction-panel").content)

    run_async(body())


def test_stepping_back_inspects_an_earlier_stage(mock_workspace):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            assert view.current_stage() == "final"
            await pilot.press("left_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() != "final")
            assert view.current_stage() in str(app.query_one("#edit-header").content)

    run_async(body())


def test_lock_revert_and_clear_write_editorial_json(mock_workspace, tmp_path):
    ws = own_copy(mock_workspace, tmp_path)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            j = view.current_junction()
            await pilot.press("l")
            pins = json.loads((ws / "editorial.json").read_text())["pins"]
            assert pins == [dict(out_shot=j.out_shot, in_shot=j.in_shot, out_time=round(j.current.out_time, 3), in_time=round(j.current.in_time, 3), origin="locked", note="")]
            await wait_for(pilot, lambda: "pin pending" in str(app.query_one("#junction-panel").content))
            await pilot.press("v")
            pins = json.loads((ws / "editorial.json").read_text())["pins"]
            assert pins[0]["origin"] == "reverted" and pins[0]["out_time"] == round(j.baseline.out_time, 3)
            assert "pin pending" in str(app.query_one("#junction-panel").content)
            await pilot.press("x")
            assert json.loads((ws / "editorial.json").read_text())["pins"] == []
            await wait_for(pilot, lambda: "pin pending" not in str(app.query_one("#junction-panel").content))
            panel = str(app.query_one("#junction-panel").content)
            assert "pinned (" not in panel and "pin not placed" not in panel

    run_async(body())


def test_refresh_is_idempotent_and_open_saves_the_blend(mock_workspace, tmp_path, monkeypatch):
    ws = own_copy(mock_workspace, tmp_path)
    opened = []
    monkeypatch.setattr(images, "open_externally", lambda p: opened.append(p) or True)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions) and frames_ready(app))
            calls = []
            monkeypatch.setattr(Frame, "show", lambda self, *a, **k: calls.append(self.id))
            for _ in range(3):
                app.refresh_views()
                await pilot.pause()
            assert calls == []
            monkeypatch.undo()
            monkeypatch.setattr(images, "open_externally", lambda p: opened.append(p) or True)
            await pilot.press("o")
            await wait_for(pilot, lambda: bool(opened))
            assert opened[0].exists() and opened[0].suffix == ".jpg"

    run_async(body())


def test_an_empty_workspace_shows_how_to_start(tmp_path):
    async def body():
        cfg = PipelineConfig(source="none.mp4", workspace=str(tmp_path))
        app = MatchcutApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            await pilot.pause()
            assert "no cut yet" in str(app.query_one("#edit-header").content)
            for key in ("left", "right", "left_square_bracket", "l", "v", "x", "enter"):
                await pilot.press(key)
            assert not (tmp_path / "editorial.json").exists()

    run_async(body())
