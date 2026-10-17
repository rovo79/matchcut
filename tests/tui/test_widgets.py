import os

from PIL import Image as PILImage
from textual.app import App, ComposeResult

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.timeline import Clip, Timeline, Transition
from matchcut.tui import images
from matchcut.tui.state import RunState, StageStatus
from matchcut.tui.widgets.frame import Frame
from matchcut.tui.widgets.ladder import StageLadder
from matchcut.tui.widgets.sparkline import EnergyCurve
from matchcut.tui.widgets.timeline import TimelineTrack, render_track
from tests.tui.conftest import run_async


def test_load_image_caches_by_mtime_and_tolerates_missing(tmp_path):
    images.clear_cache()
    p = tmp_path / "a.png"
    PILImage.new("RGB", (800, 450), (10, 20, 30)).save(p)
    img = images.load_image(p, max_width=200)
    assert img is not None and img.width == 200 and img.mode == "RGB"
    assert images.load_image(p, max_width=200) is img  # cached object
    PILImage.new("RGB", (800, 450), (200, 20, 30)).save(p)
    os.utime(p, (os.path.getmtime(p) + 5,) * 2)
    assert images.load_image(p, max_width=200) is not img
    assert images.load_image(tmp_path / "missing.png") is None
    (tmp_path / "bad.png").write_text("not an image")
    assert images.load_image(tmp_path / "bad.png") is None


def test_widget_class_selection(monkeypatch):
    from textual_image.widget import HalfcellImage, Image
    monkeypatch.delenv("MATCHCUT_TUI_IMAGES", raising=False)
    assert images.widget_class(headless=True) is HalfcellImage
    assert images.widget_class(headless=False) is Image
    monkeypatch.setenv("MATCHCUT_TUI_IMAGES", "halfcell")
    assert images.widget_class(headless=False) is HalfcellImage
    assert "half" in images.protocol_label(HalfcellImage)


class FrameApp(App):
    def compose(self) -> ComposeResult:
        yield Frame(id="f")


def test_frame_shows_image_and_placeholder():
    run_async(_frame_shows_image_and_placeholder())


async def _frame_shows_image_and_placeholder():
    app = FrameApp()
    async with app.run_test(size=(60, 20)) as pilot:
        frame = app.query_one("#f", Frame)
        frame.show(None, caption="nothing")
        await pilot.pause()
        assert "(no frame)" in frame.placeholder_text()
        frame.show(PILImage.new("RGB", (64, 36), (0, 128, 255)), caption="blue")
        await pilot.pause()
        assert frame.image_mode == "half-block"
        assert frame.query("HalfcellImage")
        frame.show(None)
        await pilot.pause()
        assert not frame.query("HalfcellImage")


class LadderApp(App):
    def compose(self) -> ComposeResult:
        yield StageLadder(id="l")


def test_stage_ladder_rows_follow_statuses():
    run_async(_stage_ladder_rows_follow_statuses())


async def _stage_ladder_rows_follow_statuses():
    app = LadderApp()
    async with app.run_test(size=(80, 24)) as pilot:
        ladder = app.query_one("#l", StageLadder)
        stages = {"ingest": StageStatus("ingest", "done", 1.5, "x"), "describe": StageStatus("describe", "running"), "pass:matchcut": StageStatus("pass:matchcut")}
        ladder.update_stages(stages)
        await pilot.pause()
        assert ladder.row_count == 3
        row = ladder.get_row("describe")
        assert "running" in str(row[1])
        assert "1.5" in str(ladder.get_row("ingest")[2])
        stages["describe"].state = "waiting"
        ladder.update_stages(stages)
        await pilot.pause()
        assert "waiting" in str(ladder.get_row("describe")[1])


def make_tl() -> Timeline:
    tl = Timeline(source="x.mp4", fps=25.0)
    tl.clips = [
        Clip("c001", 0, 0.0, 2.0, "b1"),
        Clip("c002", 1, 3.0, 5.0, "b1", locked_in=True),
        Clip("c003", 2, 6.0, 7.0, "b2", transition_in=Transition("dissolve", 0.5)),
        Clip("c004", 3, 8.0, 9.0, "b2", transition_in=Transition("dip_to_black", 0.4)),
    ]
    tl.clips[0].locked_out = True
    return tl


def test_render_track_glyphs_and_widths():
    text = render_track(make_tl(), width=60, selected=1)
    plain = text.plain.splitlines()[0]
    assert len(plain) <= 60
    assert "L" in plain and "~" in plain and "#" in plain  # locked junction, dissolve, dip
    assert plain.count("c001") == 1 or "c1" in plain or "1" in plain  # labels are present in some abbreviated form
    with_beats = render_track(make_tl(), width=60, selected=0, beats=[0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0], music_start=0.0)
    lines = with_beats.plain.splitlines()
    assert len(lines) == 2 and "." in lines[1]
    assert render_track(Timeline(source="x", fps=25.0), width=40, selected=0).plain.strip() != ""  # empty timeline shows a message


class TrackApp(App):
    def compose(self) -> ComposeResult:
        yield TimelineTrack(id="t")


def test_timeline_track_selection_is_clamped():
    run_async(_timeline_track_selection_is_clamped())


async def _timeline_track_selection_is_clamped():
    app = TrackApp()
    async with app.run_test(size=(80, 10)) as pilot:
        track = app.query_one("#t", TimelineTrack)
        track.set_timeline(make_tl())
        await pilot.pause()
        assert track.selected == 0
        track.select_prev()
        assert track.selected == 0
        for _ in range(10):
            track.select_next()
        assert track.selected == 3
        track.set_timeline(None)
        assert track.selected == 0


class CurveApp(App):
    def compose(self) -> ComposeResult:
        yield EnergyCurve(id="e")


def test_energy_curve_handles_no_music_and_music():
    run_async(_energy_curve_handles_no_music_and_music())


async def _energy_curve_handles_no_music_and_music():
    from matchcut.pipeline.audio import MusicAnalysis
    import numpy as np
    app = CurveApp()
    async with app.run_test(size=(80, 10)) as pilot:
        curve = app.query_one("#e", EnergyCurve)
        curve.update_music(None)
        await pilot.pause()
        assert "no music" in curve.caption_text()
        m = MusicAnalysis(path="t.wav", duration=10.0, sample_rate=22050, tempo_bpm=120.0, beats_per_bar=4, beats=np.arange(0, 10, 0.5, dtype=np.float32), downbeats=np.arange(0, 10, 2.0, dtype=np.float32), energy=np.linspace(0, 1, 20, dtype=np.float32), hop_seconds=0.5)
        curve.update_music(m, beat_times=[0.0, 4.0])
        await pilot.pause()
        assert "120" in curve.caption_text() and "4.0" in curve.caption_text()


def make_long_tl(n: int = 60) -> Timeline:
    tl = Timeline(source="x.mp4", fps=25.0)
    tl.clips = [Clip("c%03d" % (i + 1), i, float(i), float(i) + 1.0, "b%d" % (i // 5)) for i in range(n)]
    return tl


def test_render_track_never_exceeds_width_for_many_clips():
    tl = make_long_tl(60)
    for width in (80, 20):
        text = render_track(tl, width=width, selected=59, beats=[0.0, 1.0, 2.0])
        for line in text.plain.split("\n"):
            assert len(line) <= width
        assert any("reverse" in str(span.style) for span in text.spans)  # the selected clip stays visible


def test_render_track_tolerates_zero_width():
    render_track(make_tl(), 0, 0, beats=[0.0])
