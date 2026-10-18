"""The Edit workbench: junction selection, evidence, pins, and the hidden-tab frame case."""
from __future__ import annotations

import json
import shutil

from types import SimpleNamespace

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.sampling import FrameCache
from matchcut.tui import images
from matchcut.tui.app import MatchcutApp
from matchcut.tui.views.edit import EditView
from matchcut.tui.widgets.frame import Frame
from matchcut.tui.widgets.timeline import TimelineTrack, junction_columns
from tests.tui.conftest import run_async, wait_for

FRAMES = ("#out-frame", "#in-frame", "#blend-frame")
FRAMES_IDS = tuple(f[1:] for f in FRAMES)


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


def test_frames_render_when_the_data_arrived_while_the_tab_was_hidden(mock_workspace, monkeypatch):
    reshown, shown = [], []

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))  # loaded by refresh_views while tab 1 is shown
            original_reshow, original_show = EditView.reshow, Frame.show
            monkeypatch.setattr(EditView, "reshow", lambda self: (reshown.append(1), original_reshow(self))[1])
            monkeypatch.setattr(Frame, "show", lambda self, *a, **k: (shown.append(self.id), original_show(self, *a, **k))[1])
            await pilot.press("4")
            await wait_for(pilot, lambda: len(reshown) == 1 and set(FRAMES_IDS) <= set(shown))
            await wait_for(pilot, lambda: frames_ready(app) and min(image_heights(app)) >= 8)
            assert reshown == [1]  # the tab activation re-mounts the frames exactly once
            assert sorted(shown) == sorted(FRAMES_IDS)  # each of the three frames, once

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


def test_pins_do_not_re_render_the_frames_of_an_unmoved_junction(mock_workspace, tmp_path, monkeypatch):
    ws = own_copy(mock_workspace, tmp_path)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions) and frames_ready(app))
            calls = []
            monkeypatch.setattr(Frame, "show", lambda self, *a, **k: calls.append(self.id))
            await pilot.press("l")
            await wait_for(pilot, lambda: "pin pending" in str(app.query_one("#junction-panel").content))
            await pilot.press("x")
            await wait_for(pilot, lambda: "pin pending" not in str(app.query_one("#junction-panel").content))
            await pilot.pause()
            assert calls == []  # the workspace was rewritten twice, but the junction and its cut points did not move

    run_async(body())


# -- pins judged from the latest stage, whatever stage is inspected ------------------------------
def refine_in(ws, target=None, pin=None):
    """Re-run the passes of a workspace copy (mock backend, cached analysis), optionally with a new target and a pin."""
    cfg = PipelineConfig.load(ws / "config.json")
    cfg.workspace = str(ws)
    if target is not None:
        cfg.target_duration = target
    if pin is not None:
        PinStore(ws).put(pin)
    Pipeline(cfg).refine()
    return build_junctions(load_stage_timelines(ws))


def select_pair(view, key):
    return next(i for i, j in enumerate(view.junctions) if (j.out_shot, j.in_shot) == key)


def test_a_pin_on_a_junction_a_later_pass_created_that_would_split_the_beats_is_not_placed(mock_workspace, tmp_path, monkeypatch):
    ws = own_copy(mock_workspace, tmp_path)
    made = [j for j in refine_in(ws, target=2.0) if j.baseline.stage != "roughcut"]
    assert made, "a tight target should drop clips and join their neighbours after the rough cut"
    j0 = made[0]
    refine_in(ws, target=2.0, pin=Pin(j0.out_shot, j0.in_shot, round(j0.current.out_time, 3), round(j0.current.in_time, 3)))
    notes = []

    async def body():
        app = app_for(ws)
        monkeypatch.setattr(app, "notify", lambda message, **kw: notes.append(message))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            await pilot.press(*(["right"] * select_pair(view, (j0.out_shot, j0.in_shot))))
            panel = str(app.query_one("#junction-panel").content)
            # The mock data's pass-made junction is a pacing drop that removed a whole beat (b1 -> b3 with b2 gone), so
            # the move is rejected: moving shot {in} next to shot {out} would put a b3 clip back between b1 clips
            # (splitting or reordering the beats). The pin is therefore unplaced with the new beat reason, not moved.
            assert "pin not placed: moving shot {} would split or reorder the beats".format(j0.in_shot) in panel
            assert "pinned (" not in panel and "pin pending" not in panel and "not adjacent in this cut" not in panel
            assert "1 pin(s) not placed" in str(app.query_one("#edit-header").content)
            await pilot.press("l")
            assert f"these shots are not adjacent in the rough cut: the next run moves shot {j0.in_shot} next to shot {j0.out_shot}, unless that would split or reorder the beats or another pin holds them" in notes[-1]

    run_async(body())


def test_an_applied_pin_reads_pinned_when_the_roughcut_stage_is_inspected(mock_workspace, tmp_path):
    ws = own_copy(mock_workspace, tmp_path)
    j0 = next(j for j in build_junctions(load_stage_timelines(ws)) if j.baseline.stage == "roughcut" and j.beat_boundary)
    key = (j0.out_shot, j0.in_shot)
    refine_in(ws, pin=Pin(j0.out_shot, j0.in_shot, round(j0.current.out_time, 3), round(j0.current.in_time, 3)))

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            await pilot.press(*(["right"] * select_pair(view, key)))
            assert "pinned (locked)" in str(app.query_one("#junction-panel").content)
            for _ in range(len(view.stages) - 1):
                await pilot.press("left_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() == "roughcut")
            assert (view.current_junction().out_shot, view.current_junction().in_shot) == key  # the selection stays on the shot pair
            panel = str(app.query_one("#junction-panel").content)
            assert "pinned (locked)" in panel and "pending" not in panel

    run_async(body())


def test_a_reverted_pin_reads_reverted_everywhere_after_a_run(mock_workspace, tmp_path):
    ws = own_copy(mock_workspace, tmp_path)
    j0 = next(j for j in build_junctions(load_stage_timelines(ws)) if j.baseline.stage == "roughcut" and j.beat_boundary)
    key = (j0.out_shot, j0.in_shot)
    refine_in(ws, pin=Pin(j0.out_shot, j0.in_shot, round(j0.baseline.out_time, 3), round(j0.baseline.in_time, 3), origin="reverted"))

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            await pilot.press(*(["right"] * select_pair(view, key)))
            await pilot.press("enter")  # the expanded history
            await wait_for(pilot, lambda: view.expanded and "history:\n" in str(app.query_one("#junction-panel").content))
            panel = str(app.query_one("#junction-panel").content)
            assert "reverted by the editor" in panel and "pinned (reverted)" in panel
            assert "editor (before matchcut): reverted" in panel
            assert "locked by the editor" not in panel and "-0.00" not in panel

    run_async(body())


def test_the_apply_hint_follows_where_the_run_is(mock_workspace):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(EditView)
            idle = view._apply_hint("pinned")
            assert "next run (R)" in idle and "cached stages are reused" in idle
            for stage in ("describe", "narrative", "roughcut"):
                app.bridge = SimpleNamespace(is_running=lambda: True, pending=(stage, {}))
                assert "applies in this run when you continue" in view._apply_hint("pinned")
            for pending in (("pass:pacing", {}), None):
                app.bridge = SimpleNamespace(is_running=lambda: True, pending=pending)
                hint = view._apply_hint("pinned")
                assert "applies on the next run (R)" in hint and "continue" not in hint
            app.bridge = SimpleNamespace(is_running=lambda: False, pending=None)
            assert view._apply_hint("pinned") == idle
            app.bridge = None

    run_async(body())


def test_the_extracted_frames_sit_on_the_frame_grid(mock_workspace, tmp_path, monkeypatch):
    ws = own_copy(mock_workspace, tmp_path)
    asked = []

    class Recording(FrameCache):  # only what the Edit view asks for
        def frame(self, t, *a, **k):
            asked.append(t)
            return super().frame(t, *a, **k)

    monkeypatch.setattr("matchcut.tui.views.edit.FrameCache", Recording)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions) and frames_ready(app))
            for _ in range(len(view.stages) - 1):
                await pilot.press("left_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() == "roughcut")
            await pilot.pause(0.5)
            fps = view.junctions[0].fps
            assert len(asked) >= 4 and all(abs(t * fps - round(t * fps)) < 1e-6 for t in asked), asked

    run_async(body())


def test_stepping_to_a_stage_without_the_shot_pair_falls_back_to_the_clamped_index(mock_workspace, tmp_path):
    ws = own_copy(mock_workspace, tmp_path)
    made = [j for j in refine_in(ws, target=2.0) if j.baseline.stage != "roughcut"]
    assert made

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            pair = (view.current_junction().out_shot, view.current_junction().in_shot)
            for _ in range(len(view.stages) - 1):
                await pilot.press("left_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() == "roughcut")
            assert 0 <= view.selected < len(view.junctions) and (view.current_junction().out_shot, view.current_junction().in_shot) != pair
            for _ in range(len(view.stages) - 1):
                await pilot.press("right_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() == "final")
            assert 0 <= view.selected < len(view.junctions)

    run_async(body())
