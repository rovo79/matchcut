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


from matchcut.tui.views.cut import CutView
from matchcut.tui.views.story import StoryView
from matchcut.tui.widgets.frame import Frame
from matchcut.tui.widgets.timeline import TimelineTrack


def test_story_view_shows_beats(mock_workspace):
    run_async(_story(mock_workspace))


async def _story(mock_workspace):
    app = MatchcutApp(PipelineConfig.load(mock_workspace / "config.json"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("3")
        await pilot.pause()
        view = app.query_one(StoryView)
        narrative = app.state.narrative()
        assert len(view.query(".beat-card")) == len(narrative["beats"])
        head = str(view.query_one("#story-head").content)
        assert narrative["logline"] in head
        assert "no music" in view.query_one("#story-energy").caption_text()


def test_cut_view_steps_stages_and_selects_clips(mock_workspace):
    run_async(_cut(mock_workspace))


async def _cut(mock_workspace):
    app = MatchcutApp(PipelineConfig.load(mock_workspace / "config.json"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        await pilot.pause()
        view = app.query_one(CutView)
        stages = app.state.timeline_stages()
        assert view.stages == stages and view.current_stage() == "final"
        assert "final" in str(view.query_one("#cut-header").content)
        track = view.query_one("#track", TimelineTrack)
        final = app.state.timeline("final")
        assert track.timeline is not None and len(track.timeline.clips) == len(final.clips)
        await pilot.press("right")
        await pilot.pause()
        assert track.selected == min(1, len(final.clips) - 1)
        detail = str(view.query_one("#clip-detail").content)
        assert final.clips[track.selected].id in detail
        await wait_for(pilot, lambda: view.query_one("#clip-frame").image_mode == "half-block")
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert view.current_stage() == stages[-2]
        for _ in range(len(stages) + 2):
            await pilot.press("left_square_bracket")
        await pilot.pause()
        assert view.current_stage() == "roughcut"
        await pilot.press("right_square_bracket")
        await pilot.pause()
        assert view.current_stage() == stages[1]


def test_views_survive_an_empty_workspace(tmp_path):
    run_async(_empty_story_cut(tmp_path))


async def _empty_story_cut(tmp_path):
    cfg = PipelineConfig(source="missing.mp4", workspace=str(tmp_path / "empty"))
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        for key in ("2", "3", "4"):
            await pilot.press(key)
            await pilot.pause()
        assert app.query_one(CutView).current_stage() is None
        assert "no timeline" in str(app.query_one("#track").content).lower()
        assert "no narrative" in str(app.query_one("#story-head").content)
        assert len(app.query_one(StoryView).query(".beat-card")) == 0
        await pilot.press("left", "right", "left_square_bracket", "right_square_bracket")
        await pilot.pause()


def test_story_refresh_is_idempotent_when_nothing_changed(mock_workspace):
    run_async(_story_idempotent(mock_workspace))


async def _story_idempotent(mock_workspace):
    app = MatchcutApp(PipelineConfig.load(mock_workspace / "config.json"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("3")
        await pilot.pause()
        view = app.query_one(StoryView)
        cards = list(view.query(".beat-card"))
        assert cards
        curve = view.query_one("#story-energy")
        updates = []
        original = curve.update_music
        curve.update_music = lambda *a, **k: (updates.append(1), original(*a, **k))[1]
        for _ in range(5):
            app.refresh_views()
        await pilot.pause()
        assert list(view.query(".beat-card")) == cards  # same widgets, none rebuilt
        assert all(card.is_attached for card in cards)
        assert updates == []


def test_cut_refresh_is_idempotent_and_stepping_still_updates(mock_workspace):
    run_async(_cut_idempotent(mock_workspace))


async def _cut_idempotent(mock_workspace):
    app = MatchcutApp(PipelineConfig.load(mock_workspace / "config.json"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        await pilot.pause()
        view = app.query_one(CutView)
        frame = view.query_one("#clip-frame", Frame)
        track = view.query_one("#track", TimelineTrack)
        await wait_for(pilot, lambda: frame.image_mode == "half-block")
        shown, redraws = [], []
        original_show = frame.show
        frame.show = lambda *a, **k: (shown.append(1), original_show(*a, **k))[1]
        original_set = track.set_timeline
        track.set_timeline = lambda *a, **k: (redraws.append(1), original_set(*a, **k))[1]
        for _ in range(5):
            app.refresh_views()
        await pilot.pause()
        assert shown == [] and redraws == []  # no frame extraction, no track rebuild
        # an action still updates immediately, and the stage change reaches the track
        await pilot.press("right")
        await pilot.pause()
        assert len(shown) == 1
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert len(redraws) == 1
        assert view.current_stage() == view.stages[-2]
        assert view.stages[-2] in str(view.query_one("#cut-header").content)
        before = len(shown)
        for _ in range(3):
            app.refresh_views()
        await pilot.pause()
        assert len(shown) == before and len(redraws) == 1
