import json
import os
import shutil
from typing import Optional, Tuple

import pytest

from matchcut.pipeline.config import PipelineConfig
from matchcut.tui.app import MatchcutApp
from matchcut.tui.views.config import ConfigView, parse_value, set_value
from matchcut.tui.views.shots import ShotsView
from tests.tui.conftest import run_async, wait_for


def test_shots_view_lists_shots_and_toggles_usable(mock_workspace):
    run_async(_shots(mock_workspace))


async def _shots(mock_workspace):
    path = mock_workspace / "descriptions.json"
    original = path.read_text()
    try:
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
    finally:
        path.write_text(original)  # the workspace is session-scoped: leave it as found


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


from matchcut.tui.views.edit import EditView
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


def test_views_survive_an_empty_workspace(tmp_path):
    run_async(_empty_story_edit(tmp_path))


async def _empty_story_edit(tmp_path):
    cfg = PipelineConfig(source="missing.mp4", workspace=str(tmp_path / "empty"))
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        for key in ("2", "3", "4"):
            await pilot.press(key)
            await pilot.pause()
        assert app.query_one(EditView).current_stage() is None
        assert "no cut yet" in str(app.query_one("#edit-header").content)
        assert "no timeline" in str(app.query_one("#edit-track").content).lower()
        assert "no narrative" in str(app.query_one("#story-head").content)
        assert len(app.query_one(StoryView).query(".beat-card")) == 0
        await pilot.press("left", "right", "left_square_bracket", "right_square_bracket", "l", "v", "x", "enter")
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


def test_edit_refresh_is_idempotent_and_stepping_still_updates(mock_workspace):
    run_async(_edit_idempotent(mock_workspace))


async def _edit_idempotent(mock_workspace):
    app = MatchcutApp(PipelineConfig.load(mock_workspace / "config.json"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        view = app.query_one(EditView)
        track = view.query_one("#edit-track", TimelineTrack)
        await wait_for(pilot, lambda: bool(view.junctions) and all(view.query_one(f, Frame).image_mode for f in ("#out-frame", "#in-frame", "#blend-frame")))
        shown, redraws = [], []
        for frame in view.query(Frame):
            original_show = frame.show
            frame.show = lambda *a, _o=original_show, **k: (shown.append(1), _o(*a, **k))[1]
        original_set = track.set_timeline
        track.set_timeline = lambda *a, **k: (redraws.append(1), original_set(*a, **k))[1]
        for _ in range(5):
            app.refresh_views()
        await pilot.pause()
        assert shown == [] and redraws == []  # no frame extraction, no track rebuild
        # an action still updates immediately, and the stage change reaches the track
        await pilot.press("right")
        await pilot.pause()
        assert len(shown) == 3  # outgoing, incoming and blend
        await pilot.press("left_square_bracket")
        await pilot.pause()
        assert len(redraws) == 1
        assert view.current_stage() == view.stages[-2]
        assert view.stages[-2] in str(view.query_one("#edit-header").content)
        before_shown = len(shown)
        for _ in range(3):
            app.refresh_views()
        await pilot.pause()
        assert len(shown) == before_shown and len(redraws) == 1


def _copied_workspace(mock_workspace, tmp_path):
    """A private copy of the finished run, so a test may rewrite its files."""
    ws = tmp_path / "ws"
    shutil.copytree(mock_workspace, ws)
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    cfg.workspace = str(ws)
    return cfg, ws


def _rewrite(path, data):
    path.write_text(json.dumps(data, indent=2))
    later = path.stat().st_mtime + 5  # a quick rewrite may land on the same mtime tick
    os.utime(path, (later, later))


def test_story_view_rebuilds_when_the_narrative_changes(mock_workspace, tmp_path):
    run_async(_story_changed(*_copied_workspace(mock_workspace, tmp_path)))


async def _story_changed(cfg, ws):
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("3")
        await pilot.pause()
        view = app.query_one(StoryView)
        before = len(view.query(".beat-card"))
        assert before > 0
        narrative = json.loads((ws / "narrative.json").read_text())
        narrative["beats"].append(dict(narrative["beats"][-1], id="extra", title="An extra beat"))
        _rewrite(ws / "narrative.json", narrative)
        app.refresh_views()
        await wait_for(pilot, lambda: len(view.query(".beat-card")) == before + 1)


def test_edit_view_rebuilds_when_the_timeline_changes(mock_workspace, tmp_path):
    run_async(_edit_changed(*_copied_workspace(mock_workspace, tmp_path)))


async def _edit_changed(cfg, ws):
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("4")
        view = app.query_one(EditView)
        await wait_for(pilot, lambda: bool(view.junctions))
        track = view.query_one("#edit-track", TimelineTrack)
        redraws = []
        original = track.set_timeline
        track.set_timeline = lambda *a, **k: (redraws.append(1), original(*a, **k))[1]
        app.refresh_views()
        assert redraws == []  # untouched files: no rebuild
        final = json.loads((ws / "final.json").read_text())
        clips = len(app.state.timeline("final").clips)
        final["clips"] = final["clips"][:-1]
        _rewrite(ws / "final.json", final)
        app.refresh_views()
        await pilot.pause()
        assert len(redraws) == 1
        assert len(track.timeline.clips) == clips - 1
        assert len(view.junctions) == clips - 2


def test_parse_value_types():
    assert parse_value(True, "false") is False and parse_value(False, "yes") is True
    assert parse_value(3, "7") == 7 and parse_value(0.5, "0.75") == 0.75
    assert parse_value(("a", "b"), "matchcut, pacing") == ("matchcut", "pacing")
    assert parse_value(None, "") is None and parse_value("x", "") is None and parse_value(None, "track.mp3") == "track.mp3"
    with pytest.raises(ValueError):
        parse_value(3, "seven")


def test_parse_value_branches():
    for text in ("true", "yes", "1", "TRUE", " on "):
        assert parse_value(False, text) is True
    for text in ("false", "no", "0", "Off"):
        assert parse_value(True, text) is False
    with pytest.raises(ValueError):
        parse_value(True, "maybe")
    assert parse_value(3, " 42 ") == 42
    with pytest.raises(ValueError):
        parse_value(3, "1.5")
    assert parse_value(1.5, "2") == 2.0 and isinstance(parse_value(1.5, "2"), float)
    with pytest.raises(ValueError):
        parse_value(1.5, "fast")
    assert parse_value(("a",), "x,, y ,") == ("x", "y")
    assert parse_value(("a",), "") == ()
    assert parse_value("old", "new") == "new" and parse_value("old", "") is None
    with pytest.raises(ValueError):
        parse_value({"slow": 1.0}, "x")


def test_set_value_targets_top_level_and_sections():
    cfg = PipelineConfig()
    set_value(cfg, None, "seed", 9)
    set_value(cfg, "music", "snap", False)
    assert cfg.seed == 9 and cfg.music.snap is False


def test_config_view_edits_the_live_config(mock_workspace):
    run_async(_config(mock_workspace))


async def _config(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("5")
        await pilot.pause()
        view = app.query_one(ConfigView)
        tree = view.query_one("#config-tree")
        labels = [str(n.label) for n in tree.root.children]
        assert "music" in labels and "pacing" in labels and "brief" in " ".join(labels)
        node = next(n for n in tree.root.children if str(n.label).startswith("target_duration"))
        tree.select_node(node)
        tree.action_select_cursor()
        await pilot.pause()
        edit = view.query_one("#config-edit")
        assert edit.display is True
        edit.value = ""
        await pilot.click("#config-edit")
        await pilot.press(*"12.5", "enter")
        await pilot.pause()
        assert cfg.target_duration == 12.5  # the same object the bridge will hand to Pipeline
        assert app.state.config is cfg
        assert edit.display is False
        music = next(n for n in tree.root.children if str(n.label) == "music")
        music.expand()
        await pilot.pause()
        snap = next(n for n in music.children if str(n.label).startswith("snap "))
        tree.select_node(snap)
        tree.action_select_cursor()
        await pilot.pause()
        edit.value = ""
        await pilot.click("#config-edit")
        await pilot.press(*"false", "enter")
        await pilot.pause()
        assert cfg.music.snap is False
        # a bad value is rejected and the config untouched
        node = next(n for n in tree.root.children if str(n.label).startswith("target_duration"))
        tree.select_node(node)
        tree.action_select_cursor()
        await pilot.pause()
        edit.value = ""
        await pilot.click("#config-edit")
        await pilot.press(*"abc", "enter")
        await pilot.pause()
        assert cfg.target_duration == 12.5


def test_config_view_does_not_edit_the_workspace_of_a_running_session(mock_workspace):
    run_async(_workspace_is_fixed(mock_workspace))


async def _workspace_is_fixed(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    before = cfg.workspace
    app = MatchcutApp(cfg)
    notes = []
    app.notify = lambda message, **kw: notes.append(message)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("5")
        await pilot.pause()
        view = app.query_one(ConfigView)
        tree = view.query_one("#config-tree")
        node = next(n for n in tree.root.children if str(n.label).startswith("workspace"))
        tree.select_node(node)
        tree.action_select_cursor()
        await pilot.pause()
        assert view.query_one("#config-edit").display is False  # no editor opens
        assert cfg.workspace == before and str(app.state.workspace) == str(before)
        assert any("--workspace" in n for n in notes)


def test_config_view_rejects_bad_values_with_a_notification(mock_workspace):
    run_async(_config_bad_value(mock_workspace))


async def _config_bad_value(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    app = MatchcutApp(cfg)
    sent = []
    real_notify = app.notify

    def spy(message, **kwargs):
        sent.append((str(message), kwargs.get("severity", "information")))
        return real_notify(message, **kwargs)

    app.notify = spy
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("5")
        await pilot.pause()
        view = app.query_one(ConfigView)
        tree = view.query_one("#config-tree")
        before = cfg.seed
        node = next(n for n in tree.root.children if str(n.label).startswith("seed"))
        tree.select_node(node)
        tree.action_select_cursor()
        await pilot.pause()
        edit = view.query_one("#config-edit")
        assert edit.display is True
        edit.value = "not-a-number"
        await pilot.click("#config-edit")
        await pilot.press("enter")
        await pilot.pause()
        assert cfg.seed == before
        assert any(sev == "error" for _, sev in sent)
        assert edit.display is True  # still open so the value can be corrected
        edit.value = "5"
        await pilot.press("enter")
        await pilot.pause()
        assert cfg.seed == 5 and edit.display is False
        # Escape cancels an edit without touching the config
        tree.select_node(node)
        tree.action_select_cursor()
        await pilot.pause()
        edit.value = "99"
        await pilot.press("escape")
        await pilot.pause()
        assert cfg.seed == 5 and edit.display is False


def test_config_refresh_is_idempotent_and_keeps_tree_state(mock_workspace):
    run_async(_config_idempotent(mock_workspace))


async def _config_idempotent(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("5")
        await pilot.pause()
        view = app.query_one(ConfigView)
        tree = view.query_one("#config-tree")
        music = next(n for n in tree.root.children if str(n.label) == "music")
        music.expand()
        await pilot.pause()
        tree.move_cursor(music.children[2])
        await pilot.pause()
        line, count = tree.cursor_line, len(list(tree.root.children)) + len(music.children)
        total = sum(1 for _ in _walk(tree.root))
        for _ in range(3):
            app.refresh_views()
        await pilot.pause()
        assert tree.cursor_line == line and music.is_expanded
        assert sum(1 for _ in _walk(tree.root)) == total
        assert len(list(tree.root.children)) + len(music.children) == count
        # a changed config object is picked up
        other = PipelineConfig()
        other.seed = 77
        app.state.config = other
        app.refresh_views()
        await pilot.pause()
        seed = next(n for n in tree.root.children if str(n.label).startswith("seed"))
        assert "77" in str(seed.label)


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def test_config_tab_focuses_the_tree_so_enter_opens_the_editor(mock_workspace):
    run_async(_config_keyboard(mock_workspace))


async def _config_keyboard(mock_workspace):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("5")
        view = app.query_one(ConfigView)
        tree = view.query_one("#config-tree")
        await wait_for(pilot, lambda: app.focused is tree, timeout=10)
        await pilot.press("down", "down", "down", "enter")  # source, workspace, brief -> brief
        await wait_for(pilot, lambda: view.query_one("#config-edit").display is True, timeout=10)
        edit = view.query_one("#config-edit")
        assert app.focused is edit
        await pilot.press(*"hello", "enter")
        await pilot.pause()
        assert cfg.brief.endswith("hello") and app.focused is tree


def test_parse_value_follows_the_declared_type():
    assert parse_value(None, "12", Optional[float]) == 12.0
    assert isinstance(parse_value(None, "12", Optional[float]), float)
    assert parse_value(None, "", Optional[int]) is None
    assert parse_value(None, "640", Optional[int]) == 640
    assert parse_value(None, "", Optional[str]) is None
    assert parse_value("old", "", str) == ""
    assert parse_value("old", " new ", str) == "new"
    with pytest.raises(ValueError):
        parse_value(3, "x", int)
    with pytest.raises(ValueError):
        parse_value(1.0, "nan", float)
    with pytest.raises(ValueError):
        parse_value(None, "inf", Optional[float])
    with pytest.raises(ValueError):
        parse_value(("a",), "", Tuple[str, ...])
    assert parse_value(("a",), "x, y", Tuple[str, ...]) == ("x", "y")
    with pytest.raises(ValueError):
        parse_value(True, "maybe", bool)
    assert parse_value(True, "no", bool) is False
    # legacy callers (no hint): an unset value tries int, then float, then str
    assert parse_value(None, "12") == 12 and parse_value(None, "1.5") == 1.5 and parse_value(None, "a.mp3") == "a.mp3"


def test_config_view_edits_by_declared_type(demo_video, tmp_path):
    run_async(_config_declared_types(demo_video, tmp_path))


async def _config_declared_types(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path), brief="old")
    assert cfg.target_duration is None
    app = MatchcutApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("5")
        await pilot.pause()
        view = app.query_one(ConfigView)
        tree = view.query_one("#config-tree")
        edit = view.query_one("#config-edit")

        async def open_editor(prefix):
            node = next(n for n in tree.root.children if str(n.label).startswith(prefix))
            tree.select_node(node)
            tree.action_select_cursor()
            await pilot.pause()
            assert edit.display is True

        await open_editor("target_duration")
        edit.value = ""
        await pilot.press(*"12", "enter")
        await pilot.pause()
        assert cfg.target_duration == 12.0 and isinstance(cfg.target_duration, float)
        await open_editor("brief")
        edit.value = ""
        await pilot.press("enter")
        await pilot.pause()
        assert cfg.brief == "" and edit.display is False
        assert cfg.workspace_path == tmp_path  # non-Optional strings never become None


def test_config_edit_field_sits_above_the_footer(mock_workspace):
    run_async(_config_layout(mock_workspace))


async def _config_layout(mock_workspace):
    from textual.widgets import Footer

    app = MatchcutApp(PipelineConfig.load(mock_workspace / "config.json"))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("5")
        await pilot.pause()
        view = app.query_one(ConfigView)
        tree = view.query_one("#config-tree")
        node = next(n for n in tree.root.children if str(n.label).startswith("target_duration"))
        tree.select_node(node)
        tree.action_select_cursor()
        await pilot.pause()
        edit = view.query_one("#config-edit")
        footer = app.query_one(Footer)
        assert edit.display and edit.region.height > 0 and footer.region.height > 0
        assert view.region.contains_region(edit.region), (view.region, edit.region)
        assert edit.region.bottom <= footer.region.y, (edit.region, footer.region)
        assert tree.region.height > 0 and tree.region.bottom <= edit.region.y
