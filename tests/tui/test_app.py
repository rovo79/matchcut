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
