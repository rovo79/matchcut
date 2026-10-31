"""Review keys in the Edit workbench: p / a / s, the status line, non-blocking preparation, and the controller's lifetime."""
from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path

import pytest

from matchcut.pipeline.config import PipelineConfig
from matchcut.review import controller as controller_mod, items as items_mod
from matchcut.review.controller import ReviewController as RealController
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


def test_s_pauses_what_was_played(mock_workspace, fakes):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("p")
            await wait_for(pilot, lambda: status_of(app).startswith("playing"))
            await pilot.press("s")
            await wait_for(pilot, lambda: fakes.controller().calls == ["play_pair", "stop"] and status_of(app) == "paused")

    run_async(body())


def test_s_with_nothing_playing_leaves_the_idle_line(mock_workspace, fakes):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            idle = status_of(app)
            assert idle.startswith("p play")
            await pilot.press("s")
            await pilot.pause(0.3)
            assert notified(app, "nothing is playing") and status_of(app) == idle
            assert FakeController.instances == []  # nothing was started to stop

    run_async(body())


def test_s_pauses_the_played_junction_not_the_selected_one(mock_workspace, fakes):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("p")
            await wait_for(pilot, lambda: status_of(app).startswith("playing"))
            await pilot.press("right")
            idle = status_of(app)
            assert idle.startswith("p play")
            await pilot.press("s")
            await wait_for(pilot, lambda: fakes.controller().calls == ["play_pair", "stop"])
            await pilot.pause(0.3)
            assert status_of(app) == idle  # the selected junction's line is left alone
            await pilot.press("left")
            assert status_of(app) == "paused"

    run_async(body())


def test_a_stop_that_leaves_no_status_does_not_blank_the_line(mock_workspace, fakes, monkeypatch):
    def quiet_stop(self):
        self.calls.append("stop")
        self.status = ""  # a controller with nothing to report

    monkeypatch.setattr(FakeController, "stop", quiet_stop)

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("p")
            await wait_for(pilot, lambda: status_of(app).startswith("playing"))
            await pilot.press("s")
            await wait_for(pilot, lambda: "stop" in fakes.controller().calls)
            await pilot.pause(0.3)
            assert status_of(app).startswith("playing")

    run_async(body())


def test_a_after_p_on_the_same_junction_prepares_the_comparison(mock_workspace, fakes):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("p")
            await wait_for(pilot, lambda: len(fakes.prepared) == 1 and status_of(app).startswith("playing"))
            await pilot.press("a")
            await wait_for(pilot, lambda: len(fakes.prepared) == 2)
            assert fakes.prepared[1]["ab"] is True
            await wait_for(pilot, lambda: fakes.controller().calls == ["play_pair", "play_pair"])

    run_async(body())


def test_a_toggle_that_finds_nothing_to_toggle_prepares_once(mock_workspace, fakes, monkeypatch):
    monkeypatch.setattr(FakeController, "toggle", lambda self: (self.calls.append("toggle"), False)[1])

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("a")
            await wait_for(pilot, lambda: fakes.controller().calls == ["play_pair"])
            await pilot.press("a")
            await wait_for(pilot, lambda: fakes.controller().calls == ["play_pair", "toggle", "play_pair"])
            assert len(fakes.prepared) == 2 and fakes.prepared[1]["ab"] is True

    run_async(body())


def test_a_toggle_that_finds_the_junction_busy_notifies_and_does_not_fail(mock_workspace, fakes, monkeypatch):
    monkeypatch.setattr(FakeController, "toggle", lambda self: (self.calls.append("toggle"), False)[1])

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            await pilot.press("a")
            await wait_for(pilot, lambda: fakes.controller().calls == ["play_pair"])
            await wait_for(pilot, lambda: view._ab_key is not None)
            key = view._review_key()
            assert view._claim(key)  # another request for this junction is in flight
            before = status_of(app)
            await pilot.press("a")
            await wait_for(pilot, lambda: notified(app, "already preparing this junction"))
            assert status_of(app) == before and not notified(app, "review failed")
            assert len(fakes.prepared) == 1
            view._release(key)

    run_async(body())


def test_a_change_to_the_workspace_forgets_the_prepared_pair(mock_workspace, tmp_path, fakes):
    ws = own_copy(mock_workspace, tmp_path)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            await pilot.press("a")
            await wait_for(pilot, lambda: view._ab_key is not None)
            await pilot.press("l")  # pinning rewrites editorial.json
            await wait_for(pilot, lambda: view._ab_key is None)
            await pilot.press("a")
            await wait_for(pilot, lambda: len(fakes.prepared) == 2)
            assert "toggle" not in fakes.controller().calls

    run_async(body())


def test_a_preparation_that_finishes_after_the_cut_changed_is_not_a_pair_to_toggle(mock_workspace, tmp_path, fakes):
    ws = own_copy(mock_workspace, tmp_path)
    gate = fakes.state["gate"] = threading.Event()

    async def body():
        app = app_for(ws)
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                view = await edit_ready(pilot, app)
                await pilot.press("a")
                await wait_for(pilot, lambda: len(fakes.prepared) == 1)
                before = view._review_fp
                await pilot.press("l")  # a pin rewrites editorial.json while the pair is being prepared
                await wait_for(pilot, lambda: view._review_fp != before)
                gate.set()
                ctl = fakes.controller()
                await wait_for(pilot, lambda: ctl.calls == ["play_pair"])
                # the playback the editor asked for stays, and the status says the cut moved under it
                await wait_for(pilot, lambda: "the cut changed: press a again to compare the new cut" in status_of(app))
                assert status_of(app).startswith("playing A")
                assert view._ab_key is None
                await pilot.press("a")
                await wait_for(pilot, lambda: len(fakes.prepared) == 2)
                await wait_for(pilot, lambda: ctl.calls == ["play_pair", "play_pair"])
                assert "toggle" not in ctl.calls
                assert fakes.prepared[1]["ab"] is True
                await wait_for(pilot, lambda: view._ab_key is not None)  # prepared afresh against the new cut: it toggles from here
                await pilot.press("a")
                await wait_for(pilot, lambda: ctl.calls[-1] == "toggle")
        finally:
            gate.set()

    run_async(body())


def test_a_cut_rewritten_outside_the_app_after_the_pair_was_prepared_is_not_toggled(mock_workspace, tmp_path, fakes):
    import os
    from matchcut.pipeline.timeline import Timeline
    ws = own_copy(mock_workspace, tmp_path)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            await pilot.press("a")
            await wait_for(pilot, lambda: view._ab_key is not None)
            path = ws / "final.json"  # another process (the import CLI) rewrites the cut: no run event, so no refresh
            tl = Timeline.load(path)
            tl.clips[0].source_out -= 0.04
            tl.save(path)
            st = os.stat(path)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            await pilot.pause(0.5)
            await pilot.press("a")
            ctl = fakes.controller()
            await wait_for(pilot, lambda: len(fakes.prepared) == 2)
            assert "toggle" not in ctl.calls, ctl.calls

    run_async(body())


def test_a_cut_reordered_outside_the_app_is_reviewed_at_the_junction_that_was_selected(mock_workspace, tmp_path, fakes):
    import os
    from matchcut.pipeline.timeline import Timeline
    ws = own_copy(mock_workspace, tmp_path)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            await pilot.press("right")
            before = (view.current_junction().out_clip, view.current_junction().in_clip)
            path = ws / "final.json"  # another process rotates the cut by one clip: the selected junction is now at index 0
            tl = Timeline.load(path)
            tl.clips = tl.clips[1:] + tl.clips[:1]
            tl.save(path)
            st = os.stat(path)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            await pilot.press("p")
            await wait_for(pilot, lambda: len(fakes.prepared) == 1)
            j = view.current_junction()
            assert (j.out_clip, j.in_clip) == before and view.selected == 0
            assert fakes.prepared[0]["index"] == 0

    run_async(body())


def test_two_junctions_with_the_same_shot_pair_are_reviewed_separately(mock_workspace, tmp_path, fakes):
    from matchcut.pipeline.timeline import Timeline
    ws = own_copy(mock_workspace, tmp_path)
    final = Timeline.load(ws / "final.json")
    first, second = final.clips[0], final.clips[1]
    for c, new_id in ((first, "c901"), (second, "c902")):  # the same two shots again at the end: one shot pair, two junctions
        dup = type(c).from_dict(c.to_dict())
        dup.id = new_id
        final.clips.append(dup)
    final.save(ws / "final.json")

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            pair = (view.current_junction().out_shot, view.current_junction().in_shot)
            later = [j.index for j in view.junctions if (j.out_shot, j.in_shot) == pair and j.index != view.current_junction().index]
            assert later, "the fixture must repeat the shot pair"
            await pilot.press("a")
            await wait_for(pilot, lambda: view._ab_key is not None and status_of(app).startswith("playing A"))
            while view.current_junction().index != later[0]:
                await pilot.press("right")
            await pilot.press("a")
            # the second junction is prepared and played, never toggled as if it were the first
            await wait_for(pilot, lambda: len(fakes.prepared) == 2)
            assert fakes.prepared[1]["index"] == later[0]
            assert "toggle" not in fakes.controller().calls

    run_async(body())


class FakeSession:
    def __init__(self, sessions):
        self.open = True
        sessions.append(self)

    def start(self):
        pass

    def alive(self):
        return self.open

    def close(self):
        self.open = False

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_a_job_that_finishes_after_the_app_closed_leaves_no_session(mock_workspace, fakes, monkeypatch):
    sessions = []

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = await edit_ready(pilot, app)
            real = RealController(view._state.config, session_factory=lambda *a, **k: FakeSession(sessions))
            original = items_mod.prepare_review

            def closes_midway(*a, **k):  # the app exits while the job is preparing
                app.review_closing = True
                return original(*a, **k)

            monkeypatch.setattr(items_mod, "prepare_review", closes_midway)
            job = (real, view._review_key(), view._state.workspace, view._state.config, view.current_stage(), view.current_junction().index, False, False)
            await asyncio.to_thread(view._review_job, *job)
            assert len(sessions) == 1 and not sessions[0].open
            assert real.current is None  # it never started playing

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


def test_the_s_key_is_labelled_pause_because_it_pauses():
    from matchcut.tui.views.edit import EditView

    labels = {b.key: b.description for b in EditView.BINDINGS if b.key in ("p", "a", "s")}
    assert labels == {"p": "Play", "a": "A/B", "s": "Pause"}


def test_s_after_the_mpv_window_was_closed_shows_that_mpv_is_not_running(mock_workspace, fakes, monkeypatch):
    def closed_stop(self):
        self.calls.append("stop")
        self.status = "mpv is not running"  # what the real controller reports when the window is gone

    monkeypatch.setattr(FakeController, "stop", closed_stop)

    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await edit_ready(pilot, app)
            await pilot.press("p")
            await wait_for(pilot, lambda: status_of(app).startswith("playing"))
            await pilot.press("s")
            await wait_for(pilot, lambda: status_of(app) == "mpv is not running")

    run_async(body())
