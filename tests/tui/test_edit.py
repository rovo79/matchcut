"""The Edit workbench: junction selection, evidence, pins, and the hidden-tab frame case."""
from __future__ import annotations

import json
import shutil

from types import SimpleNamespace

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines, stage_timeline_paths
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.sampling import FrameCache
from matchcut.pipeline.timeline import Timeline, clip_origin
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
            tl = view._state.timeline(view.current_stage())
            origins = dict(out_origin=clip_origin(tl.clip_by_id(j.out_clip)), in_origin=clip_origin(tl.clip_by_id(j.in_clip)))
            assert pins == [dict(out_shot=j.out_shot, in_shot=j.in_shot, out_time=round(j.current.out_time, 3), in_time=round(j.current.in_time, 3), origin="locked", note="", **origins)]
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


def test_a_pin_remembers_the_clip_instances_of_the_inspected_stage(mock_workspace, tmp_path):
    ws = own_copy(mock_workspace, tmp_path)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            assert len(view.stages) > 1
            await pilot.press("left_square_bracket")  # inspect the stage before the last
            await pilot.pause()
            stage = view.current_stage()
            assert stage != view.stages[-1]
            j = view.current_junction()
            tl = view._state.timeline(stage)
            await pilot.press("l")
            pin = json.loads((ws / "editorial.json").read_text())["pins"][0]
            assert pin["out_origin"] == clip_origin(tl.clip_by_id(j.out_clip)) and pin["in_origin"] == clip_origin(tl.clip_by_id(j.in_clip))
            assert pin["out_origin"] and pin["in_origin"]
            rough_keys = {clip_origin(c) for c in Timeline.load(ws / "roughcut.json").clips}  # an instance key a rough-cut clip really has
            assert pin["out_origin"] in rough_keys and pin["in_origin"] in rough_keys
            assert PinStore(ws).get(j.out_shot, j.in_shot).out_origin == pin["out_origin"]

    run_async(body())


def test_a_stage_file_that_predates_origins_gives_a_pin_no_origin_except_in_the_rough_cut(mock_workspace, tmp_path):
    ws = own_copy(mock_workspace, tmp_path)
    for stage, path in stage_timeline_paths(ws):  # simulate stage files written before clips carried an origin
        if path.exists() and stage != "roughcut":
            data = json.loads(path.read_text())
            for c in data["clips"]:
                del c["origin"]
            path.write_text(json.dumps(data))

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            j = view.current_junction()
            assert view.current_stage() != "roughcut"
            await pilot.press("l")
            pin = json.loads((ws / "editorial.json").read_text())["pins"][0]
            assert (pin["out_origin"], pin["in_origin"]) == ("", "")  # not a key computed from a pass's modified range
            while view.current_stage() != "roughcut":  # in the rough cut a clip's range IS its instance key
                await pilot.press("left_square_bracket")
                await pilot.pause()
            j = view.current_junction()
            tl = view._state.timeline("roughcut")
            await pilot.press("l")
            pin = PinStore(ws).get(j.out_shot, j.in_shot)
            assert pin.out_origin == clip_origin(tl.clip_by_id(j.out_clip)) and pin.in_origin == clip_origin(tl.clip_by_id(j.in_clip))
            assert pin.out_origin and pin.in_origin

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
            assert "these shots are not adjacent in the rough cut: the next run moves one of these shots next to the other, unless that would split or reorder the beats or another pin holds them" in notes[-1]

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


def test_an_imported_human_stage_is_the_last_stage_and_keeps_an_untouched_pin_applied(mock_workspace, tmp_path):
    """After ``import`` the human stage is the latest: the Edit view lands on it, and a pin it did not touch still reads applied."""
    from matchcut.interchange.workspace import export_workspace, import_timeline

    ws = own_copy(mock_workspace, tmp_path)
    j0 = next(j for j in build_junctions(load_stage_timelines(ws)) if j.baseline.stage == "roughcut" and j.beat_boundary)
    key = (j0.out_shot, j0.in_shot)
    refine_in(ws, pin=Pin(j0.out_shot, j0.in_shot, round(j0.current.out_time, 3), round(j0.current.in_time, 3)))
    cfg = PipelineConfig.load(ws / "config.json")
    export_workspace(ws, cfg, stages=("final",), formats=("otio",))
    doc = json.loads((ws / "exports" / "final.otio").read_text())
    clips = [k for k in doc["tracks"]["children"][0]["children"] if k["OTIO_SCHEMA"].startswith("Clip")]
    pinned_clips = {j.out_clip for j in build_junctions(load_stage_timelines(ws)) if (j.out_shot, j.in_shot) == key} | \
        {j.in_clip for j in build_junctions(load_stage_timelines(ws)) if (j.out_shot, j.in_shot) == key}
    target = next(k for k in clips[1:] if k["metadata"]["matchcut"]["clip_id"] not in pinned_clips)
    target["source_range"]["start_time"]["value"] += 3
    target["source_range"]["duration"]["value"] -= 3
    (tmp_path / "edit.otio").write_text(json.dumps(doc))
    assert [d.action for d in import_timeline(ws, cfg, tmp_path / "edit.otio").decisions] == ["trim"]

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            assert view.stages[-1] == "human" and view.current_stage() == "human"
            await pilot.press(*(["right"] * select_pair(view, key)))
            panel = str(app.query_one("#junction-panel").content)
            assert "pinned (locked)" in panel and "pending" not in panel and "locked by the editor" in panel
            assert "human:" not in panel  # nothing human happened at this junction

    run_async(body())


def test_an_imported_human_stage_shows_a_pin_the_editor_changed_as_changed_not_applied(mock_workspace, tmp_path):
    """The canonical editor decision is repeated in human.json's history; the Edit view must judge the editor's cut, in the panel and in the header."""
    from matchcut.interchange.workspace import export_workspace, import_timeline

    for how in ("trim", "delete"):
        (tmp_path / how).mkdir()
        ws = own_copy(mock_workspace, tmp_path / how)
        j0 = next(j for j in build_junctions(load_stage_timelines(ws)) if j.baseline.stage == "roughcut" and j.beat_boundary)
        key = (j0.out_shot, j0.in_shot)
        pin = Pin(j0.out_shot, j0.in_shot, round(j0.current.out_time, 3), round(j0.current.in_time, 3))
        refine_in(ws, pin=pin)
        cfg = PipelineConfig.load(ws / "config.json")
        export_workspace(ws, cfg, stages=("final",), formats=("otio",))
        doc = json.loads((ws / "exports" / "final.otio").read_text())
        kids = doc["tracks"]["children"][0]["children"]
        clips = [k for k in kids if k["OTIO_SCHEMA"].startswith("Clip")]
        jf = next(j for j in build_junctions(load_stage_timelines(ws)) if (j.out_shot, j.in_shot) == key)
        out_k = next(k for k, c in enumerate(clips) if c["metadata"]["matchcut"]["clip_id"] == jf.out_clip)
        if how == "trim":
            clips[out_k]["source_range"]["duration"]["value"] -= 3  # the pinned cut moves 3 frames earlier
        else:
            victim = clips[out_k + 1]
            at = kids.index(victim)
            del kids[at]
            if at < len(kids) and kids[at]["OTIO_SCHEMA"].startswith("Transition"):
                del kids[at]
            if at - 1 >= 0 and kids[at - 1]["OTIO_SCHEMA"].startswith("Transition"):
                del kids[at - 1]
        (tmp_path / f"{how}.otio").write_text(json.dumps(doc))
        import_timeline(ws, cfg, tmp_path / f"{how}.otio")

        async def body():
            app = app_for(ws)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.press("4")
                view = app.query_one(EditView)
                await wait_for(pilot, lambda: bool(view.junctions))
                assert view.stages[-1] == "human" and view.current_stage() == "human"
                assert f"pin s{key[0]}->s{key[1]} changed by the editor: " in str(app.query_one("#edit-header").content)
                if how == "trim":
                    await pilot.press(*(["right"] * select_pair(view, key)))
                    panel = str(app.query_one("#junction-panel").content)
                    assert "pin changed by the editor: cut trimmed by 3 frames (out)" in panel
                    badge_line = panel.splitlines()[1]
                    assert "pinned (" not in badge_line and "pending" not in badge_line and "locked by the editor" not in badge_line

        run_async(body())


def test_a_stale_human_stage_is_left_out_and_the_edit_view_says_why(mock_workspace, tmp_path):
    """A re-run after an import: the human stage no longer fits, so it is not the latest stage and a new pin is judged by the real one."""
    from matchcut.interchange.workspace import export_workspace, import_timeline
    from matchcut.pipeline.junctions import stale_human

    ws = own_copy(mock_workspace, tmp_path)
    cfg = PipelineConfig.load(ws / "config.json")
    export_workspace(ws, cfg, stages=("final",), formats=("otio",))
    doc = json.loads((ws / "exports" / "final.otio").read_text())
    clips = [k for k in doc["tracks"]["children"][0]["children"] if k["OTIO_SCHEMA"].startswith("Clip")]
    clips[1]["source_range"]["start_time"]["value"] += 3
    clips[1]["source_range"]["duration"]["value"] -= 3
    (tmp_path / "edit.otio").write_text(json.dumps(doc))
    import_timeline(ws, cfg, tmp_path / "edit.otio")
    assert stale_human(ws) is None
    j1 = next(j for j in build_junctions(load_stage_timelines(ws)) if j.baseline.stage == "roughcut" and j.beat_boundary)
    key = (j1.out_shot, j1.in_shot)
    refine_in(ws, pin=Pin(j1.out_shot, j1.in_shot, round(j1.baseline.out_time, 3), round(j1.baseline.in_time, 3)))
    message = stale_human(ws)
    assert message and message.startswith("human.json is stale: ")
    assert (ws / "human.json").exists()

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            assert view.stages[-1] == "final" and view.current_stage() == "final" and "human" not in view.stages
            assert message in str(app.query_one("#edit-header").content)
            await pilot.press(*(["right"] * select_pair(view, key)))
            panel = str(app.query_one("#junction-panel").content)
            assert "pinned (" in panel and "pending" not in panel

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


def _append_copies(path, specs):
    """Append a copy of an existing clip for each (source clip id, new id, origin or None) to the stage timeline at ``path``."""
    tl = Timeline.load(path)
    by_id = {c.id: c for c in tl.clips}
    for src, new_id, origin in specs:
        dup = type(by_id[src]).from_dict(by_id[src].to_dict())
        dup.id = new_id
        if origin is not None:
            dup.origin = origin
        tl.clips.append(dup)
    tl.save(path)
    return tl


def test_switching_stages_keeps_the_occurrence_of_a_repeated_shot_pair(mock_workspace, tmp_path):
    ws = own_copy(mock_workspace, tmp_path)
    # the same two shots again at the end of the final cut and of the last pass: one shot pair, two junctions
    for path in (ws / "final.json", sorted((ws / "passes").glob("*.json"))[-1]):
        _append_copies(path, [("c001", "c901", None), ("c002", "c902", None)])

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            pair = (view.junctions[0].out_shot, view.junctions[0].in_shot)
            repeats = [j.index for j in view.junctions if (j.out_shot, j.in_shot) == pair]
            assert len(repeats) == 2, "the fixture must repeat the shot pair"
            while view.selected != repeats[1]:
                await pilot.press("right")
            final = view.current_stage()
            for _ in range(2):  # and back again, twice: the second occurrence each time
                await pilot.press("left_square_bracket")
                await wait_for(pilot, lambda: view.current_stage() != final)
                assert view.selected == repeats[1], (view.current_stage(), view.selected)
                await pilot.press("right_square_bracket")
                await wait_for(pilot, lambda: view.current_stage() == final)
                assert view.selected == repeats[1], (view.current_stage(), view.selected)

    run_async(body())


def test_switching_stages_follows_the_same_clip_instances_to_another_index(mock_workspace, tmp_path):
    ws = own_copy(mock_workspace, tmp_path)
    # The final cut repeats the first shot pair at its end (new instances, own origins).  The last pass has those two copies at the
    # *front* instead (a pass moved them): the first pair's instances now sit at index 2 and the copies at index 0.
    _append_copies(ws / "final.json", [("c001", "c901", "copy-a"), ("c002", "c902", "copy-b")])
    last = sorted((ws / "passes").glob("*.json"))[-1]
    tl = _append_copies(last, [("c001", "c901", "copy-a"), ("c002", "c902", "copy-b")])
    tl.clips = tl.clips[-2:] + tl.clips[:-2]
    tl.save(last)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            final = view.current_stage()
            assert view.selected == 0 and view.current_junction().out_clip == "c001"
            await pilot.press("left_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() != final)
            earlier = view._state.timeline(view.current_stage())
            assert [c.id for c in earlier.clips[:4]] == ["c901", "c902", "c001", "c002"]
            assert view.selected == 2
            await pilot.press("right_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() == final)
            assert view.selected == 0

    run_async(body())


def _select_nth_repeat(view, pair, n):
    repeats = [j.index for j in view.junctions if (j.out_shot, j.in_shot) == pair]
    return repeats, repeats[n]


def test_switching_stages_matches_the_shot_pair_occurrence_when_the_instances_differ(mock_workspace, tmp_path):
    """Rule (b): the copies carry other origins in the pass than in the final cut, so only the occurrence finds the second one."""
    ws = own_copy(mock_workspace, tmp_path)
    _append_copies(ws / "final.json", [("c001", "c901", "fin-a"), ("c002", "c902", "fin-b")])
    _append_copies(sorted((ws / "passes").glob("*.json"))[-1], [("c001", "c901", "pas-a"), ("c002", "c902", "pas-b")])

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            repeats, second = _select_nth_repeat(view, (view.junctions[0].out_shot, view.junctions[0].in_shot), 1)
            assert len(repeats) == 2
            while view.selected != second:
                await pilot.press("right")
            final = view.current_stage()
            await pilot.press("left_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() != final)
            assert view.selected == second
            await pilot.press("right_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() == final)
            assert view.selected == second

    run_async(body())


def test_switching_stages_takes_the_nearest_junction_with_the_shot_pair_when_the_occurrence_is_missing(mock_workspace, tmp_path):
    """Rule (c): the third occurrence has no counterpart in the pass (which has two), so the pair's junction nearest the old index is kept."""
    ws = own_copy(mock_workspace, tmp_path)
    _append_copies(ws / "final.json", [("c001", "c901", "fin-a"), ("c002", "c902", "fin-b"), ("c001", "c903", "fin-c"), ("c002", "c904", "fin-d")])
    _append_copies(sorted((ws / "passes").glob("*.json"))[-1], [("c001", "c901", "pas-a"), ("c002", "c902", "pas-b")])

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            repeats, third = _select_nth_repeat(view, (view.junctions[0].out_shot, view.junctions[0].in_shot), 2)
            assert len(repeats) == 3
            while view.selected != third:
                await pilot.press("right")
            await pilot.press("left_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() != view.stages[-1])
            pair = (view.junctions[0].out_shot, view.junctions[0].in_shot)
            earlier = [j.index for j in view.junctions if (j.out_shot, j.in_shot) == pair]
            assert len(earlier) == 2 and earlier[1] < third
            assert view.selected == earlier[1], (view.selected, earlier)  # the nearest, not the first

    run_async(body())


def test_the_identity_of_the_selection_ignores_a_stage_file_rewritten_since_the_junctions_were_built(mock_workspace, tmp_path):
    import os
    ws = own_copy(mock_workspace, tmp_path)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: len(view.junctions) >= 3)
            j, stage = view.current_junction(), view.current_stage()
            built = view._state.timeline(stage)
            instances = (clip_origin(built.clip_by_id(j.out_clip)), clip_origin(built.clip_by_id(j.in_clip)))
            path = ws / "final.json"
            tl = Timeline.load(path)
            tl.clips = tl.clips[2:] + tl.clips[:2]  # rewritten behind the view's back: other clips now sit at j.index
            tl.save(path)
            st = os.stat(path)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            ident = view._identity(view._state.timeline(stage))
            assert ident.origins in (None, instances)  # never the instances of the clips that moved into j.index
            assert ident.pair == (j.out_shot, j.in_shot)

    run_async(body())


def test_a_published_edit_stage_is_listed_after_final_and_a_stale_one_is_left_out_with_why(mock_workspace, tmp_path):
    """M3a: an agent's published edit session is the stage after final; once stale it is left out (never deleted) and the header says why."""
    from matchcut.edit import publish, session
    from matchcut.pipeline.junctions import stale_edit

    ws = own_copy(mock_workspace, tmp_path)
    session.start(ws)
    longest = max(Timeline.load(ws / "final.json").clips, key=lambda c: c.duration).id
    for ops in ([{"op": "trim", "clip": longest, "edge": "out", "frames": -1}], [{"op": "trim", "clip": longest, "edge": "in", "frames": 1}],
                [{"op": "slip", "clip": longest, "frames": -1}], [{"op": "slip", "clip": longest, "frames": 1}]):
        try:
            p = session.propose(ws, ops)
            break
        except session.Refused:
            continue
    session.apply(ws, p["proposal"])
    publish.publish(ws)
    assert [s for s, _ in load_stage_timelines(ws)][-2:] == ["final", "edit"]

    async def listed():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            assert view.stages[-2:] == ["final", "edit"] and view.current_stage() == "edit"
            assert "stage edit" in str(app.query_one("#edit-header").content)

    run_async(listed())
    (ws / "edit_publish.json").write_text("{}")
    message = stale_edit(ws)
    assert message and message.startswith("edit.json is stale: ") and (ws / "edit.json").exists()

    async def left_out():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            assert "edit" not in view.stages and view.current_stage() == "final"
            assert message in str(app.query_one("#edit-header").content)

    run_async(left_out())


def test_the_tui_watches_the_edit_stage_files_even_while_it_is_not_listed(mock_workspace):
    from matchcut.tui.state import RunState
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    cfg.workspace = str(mock_workspace)
    files = RunState(cfg).timeline_files()
    assert mock_workspace / "edit.json" in files and mock_workspace / "edit_publish.json" in files


# -- a transition protection survives the TUI's l / v ---------------------------------------------
def test_locking_or_reverting_again_keeps_the_transition_protection_and_clearing_says_it_is_released(mock_workspace, tmp_path, monkeypatch):
    """Review fix: l (lock) and v (revert) replace the pin's cut points but keep the protection the edit CLI's pin_transition set (it is about the
    junction's transition, not its cut points: a revert that moves them keeps it too); x removes the pin and says the protection goes with it."""
    from matchcut.edit.view import pin_transition

    ws = own_copy(mock_workspace, tmp_path)
    notes = []

    async def body():
        app = app_for(ws)
        monkeypatch.setattr(app, "notify", lambda message, **kw: notes.append(message))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            j = view.current_junction()
            label = f"s{j.out_shot}->s{j.in_shot}"
            await pilot.press("l")
            assert "protected" not in notes[-1]
            held = {"kind": "dissolve", "frames": 6}
            assert pin_transition(ws, pin=label, hold=True, kind="dissolve", frames=6)["held_transition"] == held

            await pilot.press("l")  # lock again
            await pilot.pause()
            pin = PinStore(ws).get(j.out_shot, j.in_shot)
            assert (pin.origin, pin.hold_transition, pin.held_transition) == ("locked", True, held)
            assert "its transition stays protected (dissolve of 6 frames)" in notes[-1]

            await pilot.press("v")  # revert: back to the first cut's points, the protection kept
            await pilot.pause()
            j = view.current_junction()
            pin = PinStore(ws).get(j.out_shot, j.in_shot)
            assert (pin.origin, pin.hold_transition, pin.held_transition) == ("reverted", True, held)
            assert (pin.out_time, pin.in_time) == (round(j.baseline.out_time, 3), round(j.baseline.in_time, 3))
            assert "(reverted)" in notes[-1] and "its transition stays protected (dissolve of 6 frames)" in notes[-1]

            await pilot.press("x")  # clearing the pin releases the protection, and says so
            await pilot.pause()
            assert PinStore(ws).get(j.out_shot, j.in_shot) is None
            assert notes[-1].startswith("pin cleared and its transition protection (dissolve of 6 frames) is released")

            await pilot.press("l")  # a new pin is unprotected
            await pilot.pause()
            assert not PinStore(ws).get(j.out_shot, j.in_shot).hold_transition and "protected" not in notes[-1]

    run_async(body())
