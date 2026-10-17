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
        assert "idle" in status and "MOCK" in status
        for key, tab in [("2", "tab-shots"), ("3", "tab-story"), ("4", "tab-cut"), ("5", "tab-config"), ("1", "tab-run")]:
            await pilot.press(key)
            await pilot.pause()
            assert app.query_one("TabbedContent").active == tab


def _assert_checkpoint_controls_visible(app):
    """The summary never pushes the hint line and the note field out of the panel."""
    panel = app.query_one("#checkpoint")
    assert panel.region.height <= 12
    for selector in ("#checkpoint-summary", "#checkpoint-hint", "#note"):
        widget = app.query_one(selector)
        assert widget.display and widget.region.height > 0, selector
        assert panel.region.contains_region(widget.region), f"{selector} {widget.region} outside {panel.region}"
    assert "[c] continue" in str(app.query_one("#checkpoint-hint").content)
    assert app.query_one("#note").region.y > app.query_one("#checkpoint-summary").region.y


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
        _assert_checkpoint_controls_visible(app)
        assert "deterministic" not in run_view.pending_text()  # describe takes a prompt: no pass hint
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
        assert "MOCK" in status and "finished · analysis only" in status
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
        await wait_for(pilot, lambda: app.state.image_mode != "")
        await pilot.pause(0.2)
        await pilot.press("4")
        await pilot.pause(0.2)
        await pilot.press("2")
        await pilot.pause(0.2)
        assert app.state.image_mode == "half-block"
        assert [m for m in messages if "images rendered" in m] == ["images rendered as half-block"]


def _mock_cfg(demo_video, tmp_path) -> PipelineConfig:
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    return cfg


def _record_notifications(app, monkeypatch):
    seen = []
    real_notify = app.notify
    monkeypatch.setattr(app, "notify", lambda message, *a, **k: seen.append((str(message), k.get("severity", "information"))) or real_notify(message, *a, **k))
    return seen


def test_a_failing_run_start_is_reported_not_fatal(demo_video, tmp_path, monkeypatch):
    run_async(_run_start_fails(demo_video, tmp_path, monkeypatch))


async def _run_start_fails(demo_video, tmp_path, monkeypatch):
    cfg = _mock_cfg(demo_video, tmp_path)
    cfg.llm.backend = "nope"
    app = MatchcutApp(cfg)
    seen = _record_notifications(app, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("R")
        await pilot.pause()
        assert app.is_running
        assert app.state.run_status == "failed"
        assert "nope" in app.state.last_error
        assert app.bridge is None
        assert any(sev == "error" and "nope" in msg for msg, sev in seen)
        cfg.llm.backend = "mock"  # the typo is fixed in the Config view
        await pilot.press("R")
        await wait_for(pilot, lambda: app.state.checkpoint is not None)
        assert app.state.run_status == "waiting"
        await pilot.press("S")
        await wait_for(pilot, lambda: app.state.run_status == "failed")


def test_a_start_error_in_the_bridge_is_reported(demo_video, tmp_path, monkeypatch):
    run_async(_bridge_start_fails(demo_video, tmp_path, monkeypatch))


async def _bridge_start_fails(demo_video, tmp_path, monkeypatch):
    class Boom:
        def start(self):
            raise OSError("workspace is not writable")

    app = MatchcutApp(_mock_cfg(demo_video, tmp_path), bridge_factory=lambda cfg: Boom())
    seen = _record_notifications(app, monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("R")
        await pilot.pause()
        assert app.is_running and app.bridge is None
        assert app.state.run_status == "failed" and app.state.last_error == "workspace is not writable"
        assert ("workspace is not writable", "error") in seen


def test_quit_confirmation_is_reset_between_runs(demo_video, tmp_path, monkeypatch):
    run_async(_quit_confirmation_resets(demo_video, tmp_path, monkeypatch))


async def _quit_confirmation_resets(demo_video, tmp_path, monkeypatch):
    app = MatchcutApp(_mock_cfg(demo_video, tmp_path))
    seen = _record_notifications(app, monkeypatch)
    warned = lambda: len([m for m, _ in seen if "press q again" in m])  # noqa: E731
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("R")
        await wait_for(pilot, lambda: app.state.checkpoint is not None)
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running and warned() == 1
        await pilot.press("S")
        await wait_for(pilot, lambda: app.state.run_status == "failed")
        assert app._quit_armed is False  # the run ended: the warning is spent
        await pilot.press("R")
        await wait_for(pilot, lambda: app.state.checkpoint is not None)
        await pilot.press("q")
        await pilot.pause()
        assert app.is_running and warned() == 2  # warned again instead of quitting
        await pilot.press("S")
        await wait_for(pilot, lambda: app.state.run_status == "failed")


def test_quit_confirmation_expires_after_a_few_seconds(demo_video, tmp_path, monkeypatch):
    run_async(_quit_confirmation_expires(demo_video, tmp_path, monkeypatch))


async def _quit_confirmation_expires(demo_video, tmp_path, monkeypatch):
    from matchcut.tui import app as app_module

    monkeypatch.setattr(app_module, "QUIT_CONFIRM_SECONDS", 1.5)
    app = MatchcutApp(_mock_cfg(demo_video, tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("R")
        await wait_for(pilot, lambda: app.state.checkpoint is not None)
        await pilot.press("q")
        await pilot.pause()
        assert app._quit_armed is True
        await wait_for(pilot, lambda: app._quit_armed is False, timeout=10)
        assert app.is_running
        await pilot.press("S")
        await wait_for(pilot, lambda: app.state.run_status == "failed")


def test_pass_checkpoints_say_they_are_deterministic(demo_video, tmp_path):
    run_async(_pass_checkpoint_hint(demo_video, tmp_path))


async def _pass_checkpoint_hint(demo_video, tmp_path):
    from matchcut.tui.views.run import DETERMINISTIC_HINT

    app = MatchcutApp(_mock_cfg(demo_video, tmp_path))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("R")
        run_view = app.query_one(RunView)
        while not (app.state.checkpoint is not None and app.state.checkpoint.stage.startswith("pass:")):
            await wait_for(pilot, lambda: app.state.checkpoint is not None)
            stage = app.state.checkpoint.stage
            assert DETERMINISTIC_HINT not in run_view.pending_text(), stage
            await pilot.press("c")
            await wait_for(pilot, lambda: app.state.checkpoint is None or app.state.checkpoint.stage != stage)
        await pilot.pause()
        assert DETERMINISTIC_HINT in run_view.pending_text()
        assert app.state.checkpoint.stage in run_view.pending_text()
        _assert_checkpoint_controls_visible(app)
        await pilot.press("S")
        await wait_for(pilot, lambda: app.state.run_status == "failed")


def test_the_log_clears_when_a_new_run_starts(mock_workspace):
    run_async(_log_clears_per_run(mock_workspace))


async def _log_clears_per_run(mock_workspace):
    app = MatchcutApp(load_cfg(mock_workspace))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        log = app.query_one("#log")
        app.state.apply(events.RunStarted())
        app.state.apply(events.Log(message="first-run-line"))
        app.refresh_views()
        await pilot.pause()
        assert "first-run-line" in "\n".join(strip.text for strip in log.lines)
        app.state.apply(events.RunStarted())
        app.state.apply(events.Log(message="second-run-line"))
        app.refresh_views()
        await pilot.pause()
        text = "\n".join(strip.text for strip in log.lines)
        assert "second-run-line" in text and "first-run-line" not in text


def test_checkpoint_text_is_compact():
    from matchcut.tui.views.run import DETERMINISTIC_HINT, checkpoint_text

    beats = [{"id": f"b{i}", "title": f"Beat {i}", "pace": "slow", "target_duration_s": 3} for i in range(40)]
    text = checkpoint_text("narrative", {"logline": "A day by the sea " * 30, "beats": beats, "clips": 12})
    lines = text.plain.splitlines()
    assert lines[0] == "Checkpoint after narrative"
    assert any(line.startswith("beats: 40 items") and "Beat 0" in line for line in lines)
    assert "clips: 12" in lines
    assert all(len(line) <= 100 for line in lines) and "{" not in text.plain
    many = {f"key{i}": i for i in range(30)}
    capped = checkpoint_text("pass:pace", many).plain.splitlines()
    assert len(capped) == 8 and capped[-1] == DETERMINISTIC_HINT and "more" in capped[-2]
    assert len(checkpoint_text("describe", many).plain.splitlines()) == 8


def test_status_text_labels_mock_runs_and_prices_real_ones(tmp_path):
    from matchcut.pipeline.config import PipelineConfig
    from matchcut.tui.app import status_text
    from matchcut.tui.state import RunState

    cfg = PipelineConfig(source="/footage/demo.mp4", workspace=str(tmp_path))
    cfg.llm.backend = "mock"
    mock = status_text(RunState(cfg)).plain
    assert mock.startswith("MOCK") and "mock backend" in mock and "~$" not in mock and "tok" not in mock
    cfg.llm.backend = "anthropic"
    real = status_text(RunState(cfg)).plain
    assert "MOCK" not in real and "0/0 tok" in real and "~$0.000" in real and "demo.mp4" in real
