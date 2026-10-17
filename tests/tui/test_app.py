from matchcut.pipeline import events
from matchcut.pipeline.config import PipelineConfig
from matchcut.tui.app import MatchcutApp
from matchcut.tui.views.run import RunView
from matchcut.tui.widgets.ladder import StageLadder
from tests.tui.conftest import run_async, wait_for


def load_cfg(mock_workspace) -> PipelineConfig:
    return PipelineConfig.load(mock_workspace / "config.json")


def test_app_opens_a_finished_workspace_read_only(mock_workspace):
    run_async(_opens(mock_workspace))


async def _opens(mock_workspace):
    app = MatchcutApp(load_cfg(mock_workspace))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        ladder = app.query_one("#ladder", StageLadder)
        assert ladder.row_count == len(app.state.stage_names())
        status = str(app.query_one("#status").content)
        assert "idle" in status and "~$" in status
        for key, tab in [("2", "tab-shots"), ("3", "tab-story"), ("4", "tab-cut"), ("5", "tab-config"), ("1", "tab-run")]:
            await pilot.press(key)
            await pilot.pause()
            assert app.query_one("TabbedContent").active == tab


def test_run_with_checkpoints_advances_the_ladder(demo_video, tmp_path):
    run_async(_run_with_checkpoints(demo_video, tmp_path))


async def _run_with_checkpoints(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("R")
        await wait_for(pilot, lambda: app.state.checkpoint is not None and app.state.checkpoint.stage == "describe")
        run_view = app.query_one(RunView)
        assert run_view.query_one("#checkpoint").display is True
        assert "describe" in run_view.pending_text() and "shots" in run_view.pending_text()
        assert app.state.stages["describe"].state == "waiting"
        await pilot.press("c")
        await wait_for(pilot, lambda: app.state.checkpoint is not None and app.state.checkpoint.stage == "narrative")
        # type a note and redo
        await pilot.click("#note")
        await pilot.press(*"end on the bird")
        await pilot.press("enter")  # leaves the note field so the r binding fires
        await pilot.press("r")
        await wait_for(pilot, lambda: app.state.checkpoint is not None and app.state.checkpoint.stage == "narrative" and sum(1 for e in app.state.events if isinstance(e, events.StageStarted) and e.stage == "narrative") == 2)
        assert any(isinstance(e, events.Decision) and e.action == "note" and e.detail == "end on the bird" for e in app.state.events)
        # continue through the rest
        while app.state.run_status not in ("finished", "failed"):
            if app.state.checkpoint is not None:
                await pilot.press("c")
            await pilot.pause(0.1)
        assert app.state.run_status == "finished"
        assert app.state.stages["report"].state == "done"
        status = str(app.query_one("#status").content)
        assert "finished" in status
        assert "shots detected" in "\n".join(str(e.message) for e in app.state.events if isinstance(e, events.Log))


def test_stop_during_checkpoint_aborts_and_quit_needs_confirmation(demo_video, tmp_path):
    run_async(_stop_and_quit(demo_video, tmp_path))


async def _stop_and_quit(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("R")
        await wait_for(pilot, lambda: app.state.checkpoint is not None)
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running  # first q while a run is active only warns
        await pilot.press("S")
        await wait_for(pilot, lambda: app.state.run_status == "failed")
        assert app.state.last_error == "aborted"
        assert app.query_one("#checkpoint").display is False
        await pilot.press("R")  # a new run can start after a failure
        await wait_for(pilot, lambda: app.state.run_status in ("running", "waiting"))
        await pilot.press("S")
        await wait_for(pilot, lambda: app.state.run_status == "failed")


def test_help_overlay_and_run_ignored_while_running(demo_video, tmp_path):
    run_async(_help(demo_video, tmp_path))


async def _help(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("question_mark")
        await pilot.pause()
        assert app.screen.name == "help" or app.query("#help")
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("R")
        await wait_for(pilot, lambda: app.state.checkpoint is not None)
        first_bridge = app.bridge
        await pilot.press("R")
        await pilot.pause()
        assert app.bridge is first_bridge  # ignored while running
        await pilot.press("S")
        await wait_for(pilot, lambda: app.state.run_status == "failed")


def test_run_keys_regain_focus_and_note_field_can_be_left(mock_workspace):
    run_async(_focus_paths(mock_workspace))


async def _focus_paths(mock_workspace):
    app = MatchcutApp(load_cfg(mock_workspace))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        # clicking the Run tab while it is already active leaves focus on the tab bar
        await pilot.click("Tab#--content-tab-tab-run")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        assert app.focused.id == "ladder"
        await pilot.press("2")
        await pilot.pause()
        await pilot.press("1")
        await pilot.pause()
        assert app.focused.id == "ladder"
        # Escape leaves the note field without submitting
        app.state.apply(events.Checkpoint(stage="describe", summary={"shots": 3}))
        app.refresh_views()
        await pilot.pause()
        await pilot.click("#note")
        await pilot.pause()
        assert app.focused.id == "note"
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused.id == "ladder"
        # hiding the checkpoint panel while the note is focused must not drop focus
        await pilot.click("#note")
        await pilot.pause()
        app.state.resolve_checkpoint()
        app.refresh_views()
        await pilot.pause()
        assert app.query_one("#checkpoint").display is False
        assert app.focused.id == "ladder"


def test_log_keeps_streaming_after_the_event_buffer_is_full(mock_workspace):
    run_async(_log_after_buffer_full(mock_workspace))


async def _log_after_buffer_full(mock_workspace):
    app = MatchcutApp(load_cfg(mock_workspace))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        for i in range(5000):
            app.state.apply(events.Log(message=f"line-{i}"))
        app.refresh_views()
        await pilot.pause()
        for i in range(5000, 5010):
            app.state.apply(events.Log(message=f"line-{i}"))
        app.refresh_views()
        await pilot.pause()
        text = "\n".join(strip.text for strip in log.lines)
        assert "line-5009" in text and "line-5000" in text


def test_open_target_depends_on_the_active_tab(mock_workspace, monkeypatch):
    run_async(_open_target(mock_workspace, monkeypatch))


async def _open_target(mock_workspace, monkeypatch):
    from matchcut.pipeline.sampling import shot_sample_times
    from matchcut.tui import images
    from matchcut.tui.views.shots import ShotsView
    from matchcut.tui.widgets.timeline import TimelineTrack

    opened = []
    monkeypatch.setattr(images, "open_externally", lambda p: opened.append(p) or True)
    app = MatchcutApp(load_cfg(mock_workspace))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.current_open_target() is None  # no render in this workspace
        await pilot.press("o")
        await pilot.pause()
        assert opened == []
        await pilot.press("2")
        await pilot.pause()
        target = app.current_open_target()
        assert target is not None and target.suffix == ".jpg" and target.exists()
        shot = next(s for s in app.state.shots() if s.index == app.query_one(ShotsView).current_shot_index())
        mid = shot_sample_times(shot, 3)[1]  # the frame the Shots view shows as "middle"
        assert f"t{int(round(mid * 1000)):09d}" in target.name
        await pilot.press("o")
        await pilot.pause()
        assert opened == [target]
        # moving the cursor moves the target
        await pilot.press("down")
        await pilot.pause()
        assert app.current_open_target() != target
        await pilot.press("4")
        await pilot.pause()
        first = app.current_open_target()
        assert first is not None and first.suffix == ".jpg" and first.exists()
        track = app.query_one("#track", TimelineTrack)
        clip = track.timeline.clips[track.selected]
        assert f"t{int(round(clip.source_in * 1000)):09d}" in first.name
        await pilot.press("right")
        await pilot.pause()
        assert app.current_open_target() != first
        await pilot.press("3")
        await pilot.pause()
        assert app.current_open_target() is None


def test_open_prefers_the_full_render(mock_workspace, tmp_path, monkeypatch):
    run_async(_open_render(mock_workspace, tmp_path, monkeypatch))


async def _open_render(mock_workspace, tmp_path, monkeypatch):
    import shutil

    from matchcut.tui import images

    ws = tmp_path / "ws"
    shutil.copytree(mock_workspace, ws)
    cfg = load_cfg(ws)
    cfg.workspace = str(ws)
    opened = []
    monkeypatch.setattr(images, "open_externally", lambda p: opened.append(p) or True)
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        (ws / "final_proxy.mp4").write_bytes(b"x")
        assert app.current_open_target() == ws / "final_proxy.mp4"
        (ws / "final.mp4").write_bytes(b"x")
        assert app.current_open_target() == ws / "final.mp4"
        await pilot.press("o")
        await pilot.pause()
        assert opened == [ws / "final.mp4"]


def test_image_mode_notice_fires_once(mock_workspace, monkeypatch):
    run_async(_image_notice(mock_workspace, monkeypatch))


async def _image_notice(mock_workspace, monkeypatch):
    app = MatchcutApp(load_cfg(mock_workspace))
    messages = []
    real_notify = app.notify
    monkeypatch.setattr(app, "notify", lambda message, *a, **k: messages.append(str(message)) or real_notify(message, *a, **k))
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: app.state.image_mode is not None)
        await pilot.pause(0.2)
        await pilot.press("4")
        await pilot.pause(0.2)
        await pilot.press("2")
        await pilot.pause(0.2)
        assert app.state.image_mode == "half-block"
        assert [m for m in messages if "images rendered" in m] == ["images rendered as half-block"]
