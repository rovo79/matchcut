import os

from PIL import Image as PILImage
from textual.app import App, ComposeResult

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin
from matchcut.pipeline.junctions import Junction, JunctionState
from matchcut.pipeline.matching import MatchScore
from matchcut.pipeline.timeline import Decision
from matchcut.pipeline.timeline import Clip, Timeline, Transition
from matchcut.tui import images
from matchcut.tui.state import RunState, StageStatus
from matchcut.tui.widgets.frame import Frame
from matchcut.tui.widgets.ladder import StageLadder
from matchcut.tui.widgets.inspector import render_inspector
from matchcut.tui.widgets.sparkline import EnergyCurve
from matchcut.tui.widgets.timeline import TimelineTrack, junction_columns, render_track
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


def test_protocol_label_reports_the_resolved_auto_renderer(monkeypatch):
    import textual_image.renderable as renderable
    from textual_image.widget import HalfcellImage, Image, SixelImage, TGPImage, UnicodeImage

    assert images.protocol_label(HalfcellImage) == "half-block"
    assert images.protocol_label(UnicodeImage) == "unicode"
    assert images.protocol_label(SixelImage) == "sixel"
    assert images.protocol_label(TGPImage) == "kitty graphics"
    for resolved, label in [
        (renderable.TGPImage, "kitty graphics"),
        (renderable.SixelImage, "sixel"),
        (renderable.HalfcellImage, "half-block"),
        (renderable.UnicodeImage, "unicode"),
    ]:
        monkeypatch.setattr(renderable, "Image", resolved)
        assert images.protocol_label(Image) == label  # the auto widget reports what it resolved to
    assert "auto" not in images.protocol_label(Image)


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


def test_blend_is_the_average_of_the_two_frames_at_the_first_size():
    a = PILImage.new("RGB", (40, 20), (200, 0, 0))
    b = PILImage.new("RGB", (80, 40), (0, 0, 200))
    out = images.blend(a, b)
    assert out.size == (40, 20) and out.mode == "RGB"
    assert out.getpixel((5, 5)) == (100, 0, 100)


def five_clips():
    tl = Timeline(source="s.mp4", fps=25.0)
    tl.clips = [Clip(f"c00{i}", i, 0.0, 1.0 + i * 0.1, "b1" if i < 3 else "b2") for i in range(1, 6)]
    return tl


def test_junction_columns_sit_between_the_spans():
    tl = five_clips()
    cols = junction_columns(tl, 60, center=0)
    assert sorted(cols) == [0, 1, 2, 3]
    plain = render_track(tl, 60, selected=-1, selected_junction=2).plain
    for j, col in cols.items():
        assert plain[col] in "|~#L"


def test_the_selected_junction_glyph_is_highlighted_and_no_clip_is():
    text = render_track(five_clips(), 60, selected=0, selected_junction=1)
    col = junction_columns(five_clips(), 60, center=1)[1]
    styles = [str(span.style) for span in text.spans if span.start <= col < span.end]
    assert any("yellow" in s for s in styles)
    assert not any("reverse" in str(span.style) for span in text.spans)


class TrackApp(App):
    def __init__(self):
        super().__init__()
        self.clicked = []

    def compose(self) -> ComposeResult:
        yield TimelineTrack(id="t")

    def on_timeline_track_junction_clicked(self, message):
        self.clicked.append(message.index)


def test_clicking_a_junction_glyph_posts_its_index():
    async def body():
        app = TrackApp()
        async with app.run_test(size=(60, 6)) as pilot:
            track = app.query_one("#t", TimelineTrack)
            track.set_timeline(five_clips())
            track.set_junction(0)
            await pilot.pause()
            col = junction_columns(five_clips(), track.size.width, center=0)[2]
            await pilot.click("#t", offset=(col, 0))
            await pilot.pause()
            assert app.clicked == [2]
            track.set_junction(99)
            assert track.selected_junction == 3

    run_async(body())


def inspected_junction() -> Junction:
    s0 = MatchScore(0.41, 0.38, 0.50, 0.60, 0.45, 0.40, 0.44, False, "static")
    s1 = MatchScore(0.63, 0.71, 0.55, 0.62, 0.66, 0.75, 0.69, False, "moving")
    j = Junction(1, "c002", "c003", 1, 3, "b1", "b2", 25.0)
    j.states = [
        JunctionState("roughcut", 5.0, 7.0, score=s0),
        JunctionState("matchcut", 4.88, 7.12, locked_out=True, locked_in=True, score=s1),
        JunctionState("final", 4.88, 7.12, "cut", 0.0, True, True, s1),
    ]
    j.decisions = [Decision("matchcut", "match_cut", ["c002", "c003"], "motion match cut (trimmed to best junction): accepted on feature score alone", 0.69, {"match_type": "motion"})]
    return j


def test_the_inspector_explains_the_junction():
    plain = render_inspector(inspected_junction(), "final", None).plain
    assert "Junction 2 · c002 → c003 · shot 1 → shot 3 · beat b1 → b2" in plain
    assert "MATCH CUT (motion)" in plain and "locked by matchcut" in plain and "beat boundary" in plain
    assert "combined" in plain and "0.44" in plain and "0.69" in plain and "+0.25" in plain
    assert "out 5.00s → 4.88s [-0.12s (-3f)]" in plain and "in 7.00s → 7.12s [+0.12s (+3f)]" in plain
    assert "why: matchcut: motion match cut" in plain
    assert "matchcut: out -0.12s (-3f), in +0.12s (+3f), locked" in plain


def test_the_inspector_flags_a_pending_pin_and_missing_scores():
    j = inspected_junction()
    plain = render_inspector(j, "final", Pin(1, 3, 4.70, 7.12)).plain
    assert "pin pending: run again (R) to apply" in plain and "pinned (locked)" not in plain
    for s in j.states:
        s.score = None
    assert "scores unavailable (no usable analysis for this workspace)" in render_inspector(j, "final", None).plain


def test_the_expanded_inspector_lists_every_decision():
    plain = render_inspector(inspected_junction(), "final", None, expanded=True).plain
    assert "decisions:" in plain and "matchcut:match_cut" in plain


def editor_decision(action, out_time, in_time, detail="", **extra):
    data = {"out_shot": 1, "in_shot": 3, "out_time": out_time, "in_time": in_time, "origin": "locked", "note": ""}
    data.update(extra)
    return Decision("editor", action, ["c002", "c003"], detail, None, data)


def test_a_applied_pin_is_not_pending_at_any_inspected_stage():
    j = inspected_junction()
    applied = editor_decision("pinned", 4.88, 7.12, "locked by the editor: out 4.88s, in 7.12s", out=4.88, in_=7.12)
    for stage in ("roughcut", "final"):
        plain = render_inspector(j, stage, Pin(1, 3, 4.88, 7.12), pin_decision=applied).plain
        assert "pinned (locked)" in plain and "pending" not in plain and "adjusted" not in plain


def test_a_pin_without_a_decision_from_the_latest_stage_is_pending():
    j = inspected_junction()
    j.decisions.append(editor_decision("pinned", 4.88, 7.12, out=4.88, in_=7.12))  # attribution is not what the badge reads
    assert "pin pending: run again (R) to apply" in render_inspector(j, "final", Pin(1, 3, 4.88, 7.12)).plain
    assert "pin pending: run again (R) to apply" in render_inspector(j, "final", Pin(1, 3, 4.88, 7.12), pin_decision=None).plain


def test_a_clamped_pin_says_where_it_was_placed():
    j = inspected_junction()
    decision = editor_decision("pinned", 4.50, 7.12, out=4.88, in_=7.12)
    plain = render_inspector(j, "final", Pin(1, 3, 4.50, 7.12), pin_decision=decision).plain
    assert "pinned (locked), adjusted to out 4.88s / in 7.12s to fit the clips" in plain
    assert "pending" not in plain


def test_an_unplaced_pin_says_why():
    j = inspected_junction()
    decision = editor_decision("pin_unplaced", 4.70, 7.12, "not enough room in these clips for the pinned points")
    plain = render_inspector(j, "final", Pin(1, 3, 4.70, 7.12), pin_decision=decision).plain
    assert "pin not placed: not enough room in these clips for the pinned points" in plain
    assert "pending" not in plain


def test_a_pin_changed_since_the_last_run_is_pending():
    j = inspected_junction()
    decision = editor_decision("pinned", 4.88, 7.12, out=4.88, in_=7.12)
    plain = render_inspector(j, "final", Pin(1, 3, 4.70, 7.12), pin_decision=decision).plain
    assert "pin pending: run again (R) to apply" in plain


def test_a_decision_for_other_cut_points_never_counts_as_applied():
    decision = editor_decision("pinned", 4.88, 7.12, out=4.88, in_=7.12)
    plain = render_inspector(inspected_junction(), "final", Pin(1, 3, 4.88, 7.20), pin_decision=decision).plain
    assert "pin pending" in plain and "pinned (locked)" not in plain


def test_match_cut_and_lock_badges_follow_the_inspected_stage():
    j = inspected_junction()
    assert "MATCH CUT" not in render_inspector(j, "roughcut", None).plain
    assert "MATCH CUT (motion)" in render_inspector(j, "final", None).plain


def test_the_baseline_stage_shows_a_single_score_column():
    lines = render_inspector(inspected_junction(), "roughcut", None).plain.splitlines()
    header = lines[2]  # title, badges, then the score table's header
    assert header.split() == ["roughcut"]
    combined = next(line for line in lines if line.startswith("combined"))
    assert combined.split() == ["combined", "0.44"]


def test_the_inspector_survives_a_zero_fps_junction():
    j = inspected_junction()
    j.fps = 0.0
    assert "Junction 2" in render_inspector(j, "final", Pin(1, 3, 4.70, 7.12)).plain
