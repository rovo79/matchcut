"""The renderer plays clips in any source order: video frame-exact, audio present, in sync and audibly the right material.

Every render here is checked against identifiable media (see ``signals``): each output frame names its source frame, each stretch of
source audio names its source second, and the music is a 3 kHz tick, so a present but silent or wrong stream cannot pass.
"""
from __future__ import annotations

import shutil
import signal
from contextlib import contextmanager
from pathlib import Path

import pytest

from matchcut.pipeline import render as render_mod
from matchcut.pipeline.config import MusicConfig, PipelineConfig, RenderConfig
from matchcut.pipeline.media import probe
from matchcut.pipeline.render import SEEK_MARGIN_SECONDS, SINGLE_THREAD_DECODE_ABOVE, clip_inputs, render
from matchcut.pipeline.timeline import Clip, Timeline, Transition

from . import signals as sig

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

RCFG = RenderConfig(crf=18, preset="veryfast")


@pytest.fixture(scope="session")
def coded(tmp_path_factory) -> Path:
    return sig.make_coded_footage(tmp_path_factory.mktemp("coded") / "coded.mp4")


@pytest.fixture(scope="session")
def ticks(tmp_path_factory) -> Path:
    return sig.make_tick_music(tmp_path_factory.mktemp("ticks") / "ticks.wav")


@contextmanager
def deadline(seconds: int):
    """Fail instead of hanging: a stalled ffmpeg graph is one of the failures these tests guard against."""
    def boom(*_):
        raise TimeoutError(f"render did not finish within {seconds} s")
    old = signal.signal(signal.SIGALRM, boom)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def timeline(source: str, fps: float, ranges, transitions=None, fade_in=0.0, fade_out=0.0) -> Timeline:
    tl = Timeline(source=source, fps=fps, fade_in=fade_in, fade_out=fade_out)
    transitions = transitions or [Transition()] * len(ranges)
    tl.clips = [Clip(f"c{i + 1:03d}", i, a, b, transition_in=t if i else Transition()) for i, ((a, b), t) in enumerate(zip(ranges, transitions))]
    return tl


def do_render(tl, media, out, music=None, timeout=90, cfg=RCFG):
    with deadline(timeout):
        return render(tl, media, cfg, out, music=music, music_duration=60.0 if music is not None else None)


def assert_video_exact(tl, path, fps, width=320, height=180):
    got = sig.frame_numbers(path, width, height)
    assert len(got) == round(tl.duration * fps), (len(got), tl.duration * fps)
    want = sig.expected_frames(tl, fps)
    assert len(want) >= 0.6 * len(got)
    wrong = [(k, got[k], v) for k, v in want.items() if got[k] != v]
    assert not wrong, wrong[:5]


def assert_av_lengths(path, fps):
    st = sig.streams(path)
    assert "audio" in st, "the render has no audio stream"
    a, v = float(st["audio"]["duration"]), float(st["video"]["duration"])
    assert abs(a - v) <= sig.av_tolerance(fps), (a, v)
    gap = sig.audio_timestamp_gap(path)
    assert gap < 0.001, f"the audio timestamps jump by {gap * 1000:.1f} ms: a player would play what follows late"


def assert_source_tones(tl, y, present=True):
    for i in range(len(tl.clips)):
        found = sig.tone_window(tl, i)
        assert found is not None, f"clip {i} has no clean window"
        t, second = found
        want = sig.tone_hz(second)
        level = sig.band_rms(y, t, 0.2, want - 15, want + 15)
        if present:
            assert sig.dominant_tone(y, t) == pytest.approx(want, abs=10), (i, t, sig.dominant_tone(y, t), want)
            assert level > 0.02, (i, level)
        else:
            assert level < 0.005, (i, level)


def assert_ticks(tl, y, present=True):
    for i in range(len(tl.clips)):
        lo, hi = sig.interior(tl, i)
        level = max(sig.band_rms(y, t, 0.5, sig.TICK_HZ - 100, sig.TICK_HZ + 100) for t in (lo, max(lo, hi - 0.5)))
        if present:
            assert level > 0.0002, (i, level)  # ducked under the always-playing source tone: about 0.0006 (as before this change)
        else:
            assert level < 0.00005, (i, level)  # no music: 0.0


ORDERS = {"source order": [(1.0, 3.0), (5.0, 7.0), (9.0, 11.0)], "reverse order": [(9.0, 11.0), (5.0, 7.0), (1.0, 3.0)]}
KINDS = {"cut": Transition(), "dissolve": Transition("dissolve", 0.4), "dip_to_black": Transition("dip_to_black", 0.32)}
AUDIO = ("source only", "music only", "source and music")


@pytest.mark.parametrize("audio_mode", AUDIO)
@pytest.mark.parametrize("kind", list(KINDS))
@pytest.mark.parametrize("order", list(ORDERS))
def test_every_order_transition_and_audio_mix(coded, ticks, tmp_path, order, kind, audio_mode):
    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, ORDERS[order], [Transition()] + [KINDS[kind]] * 2)
    music = None
    if audio_mode != "source only":
        music = MusicConfig(path=str(ticks), only=audio_mode == "music only", gain_db=-6.0)
    out = do_render(tl, media, tmp_path / "out.mp4", music)
    assert_video_exact(tl, out, media.fps)
    assert_av_lengths(out, media.fps)
    y = sig.audio(out)
    assert_source_tones(tl, y, present=audio_mode != "music only")
    assert_ticks(tl, y, present=audio_mode != "source only")


def test_a_repeated_range_renders_and_does_not_stall(coded, ticks, tmp_path):
    """The same source range twice, with dissolves, under music: one shared input stalled forever here."""
    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, [(5.0, 7.0), (1.0, 3.0), (5.0, 7.0)], [Transition(), Transition("dissolve", 0.4), Transition("dissolve", 0.4)])
    for music in (None, MusicConfig(path=str(ticks), gain_db=-6.0)):
        out = do_render(tl, media, tmp_path / f"rep_{music is not None}.mp4", music, timeout=60)
        assert_video_exact(tl, out, media.fps)
        assert_av_lengths(out, media.fps)
        assert_source_tones(tl, sig.audio(out))


def test_audio_is_sample_aligned_with_its_source(coded, tmp_path):
    """Each clip's audio is the source's audio at the same offset, to within a sample, in reverse order too."""
    import numpy as np

    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, ORDERS["reverse order"])
    out = do_render(tl, media, tmp_path / "align.mp4")
    y, src = sig.audio(out), sig.audio(coded)
    for i, c in enumerate(tl.clips):
        rec = tl.record_start(i) + 0.3
        a = y[int(rec * sig.SR): int((rec + 0.2) * sig.SR)]
        base = int((c.source_in + 0.3) * sig.SR)
        errs = {d: float(np.mean((a - src[base + d: base + d + len(a)]) ** 2)) for d in range(-480, 481)}
        best = min(errs, key=errs.get)
        assert abs(best) <= 1, (i, best)


@pytest.fixture(scope="module")
def ntsc(tmp_path_factory) -> Path:
    return sig.make_coded_footage(tmp_path_factory.mktemp("ntsc") / "ntsc.mp4", seconds=12, rate="30000/1001")


def _ntsc_timeline(path: Path, transitions: bool) -> Timeline:
    fps = probe(str(path)).fps
    f = lambda n: round(n / fps, 4)  # noqa: E731  (whole frames, as the pipeline snaps them)
    trs = [Transition(), Transition("dissolve", f(12)), Transition("dip_to_black", f(10))] if transitions else None
    return timeline(str(path), fps, [(f(250), f(310)), (f(40), f(100)), (f(160), f(220))], trs)


def test_ntsc_rate_hard_cuts_are_frame_exact_in_any_order(ntsc, tmp_path):
    """At 29.97 fps a point stored to 4 decimals can land just after its frame (250 frames = 8.341675 s, stored 8.3417): the trim still
    selects that frame.  (Before this change the frame after it was shown, in any order.)"""
    media = probe(str(ntsc))
    tl = _ntsc_timeline(ntsc, transitions=False)
    assert tl.clips[0].source_in * media.fps > 250  # the stored in-point is after frame 250's timestamp
    for name, clips in (("mixed order", tl.clips), ("source order", sorted(tl.clips, key=lambda c: c.source_in))):
        tl.clips = list(clips)
        out = do_render(tl, media, tmp_path / f"ntsc_cuts_{name}.mp4")
        assert_video_exact(tl, out, media.fps)
        assert_av_lengths(out, media.fps)
        assert_source_tones(tl, sig.audio(out))


def test_ntsc_rate_transitions_are_frame_exact(ntsc, tmp_path):
    """Each transition starts on its own frame at 29.97 fps, in any order and in source order (rounded seconds started it a frame late)."""
    media = probe(str(ntsc))
    tl = _ntsc_timeline(ntsc, transitions=True)
    for name, clips in (("mixed order", list(tl.clips)), ("source order", sorted(tl.clips, key=lambda c: c.source_in))):
        tl.clips = [Clip.from_dict(c.to_dict()) for c in clips]
        tl.clips[0].transition_in = Transition()
        tl.clips[1].transition_in, tl.clips[2].transition_in = Transition("dissolve", round(12 / media.fps, 4)), Transition("dip_to_black", round(10 / media.fps, 4))
        tl.fade_in, tl.fade_out = 0.5, 0.5
        out = do_render(tl, media, tmp_path / f"ntsc_tr_{name}.mp4")
        assert_av_lengths(out, media.fps)
        assert_video_exact(tl, out, media.fps)


@pytest.mark.parametrize("transitions", [False, True], ids=["cuts", "transitions"])
def test_30_fps_is_frame_exact(tmp_path, transitions):
    """At 30 fps the old renderer showed cuts a frame late too: its in-points and fps filter were rounded to 4 decimals.  Points on frames
    2 mod 3 (41/30 s is stored 1.3667, after the frame's time) are the ones that rounding moved."""
    coded = sig.make_coded_footage(tmp_path / "f30.mp4", seconds=12, rate="30")
    media = probe(str(coded))
    f = lambda n: round(n / 30, 4)  # noqa: E731
    assert f(41) * 30 > 41 and f(101) * 30 > 101
    trs = [Transition(), Transition("dissolve", f(12)), Transition("dip_to_black", f(9))] if transitions else None
    fwd = [(f(41), f(101)), (f(161), f(221)), (f(251), f(311))]
    for name, ranges in (("source order", fwd), ("reverse order", fwd[::-1])):
        tl = timeline(str(coded), media.fps, ranges, trs)
        out = do_render(tl, media, tmp_path / f"f30_{name}.mp4")
        assert_video_exact(tl, out, media.fps)
        assert_av_lengths(out, media.fps)
        assert_source_tones(tl, sig.audio(out))


def test_a_source_with_a_start_offset_is_read_from_its_start(tmp_path):
    """Timeline seconds count from the start of the file, whatever its first timestamp is."""
    coded = sig.make_coded_footage(tmp_path / "offset.mp4", seconds=12, ts_offset=1.5)
    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, [(6.0, 8.0), (1.0, 3.0)], [Transition(), Transition("dissolve", 0.4)])
    out = do_render(tl, media, tmp_path / "offset_out.mp4")
    assert_video_exact(tl, out, media.fps)
    assert_av_lengths(out, media.fps)
    assert_source_tones(tl, sig.audio(out))


def _tick_level(y, t0, t1):
    import numpy as np

    # windows centred on the ticks (every 0.5 s of the cut: music.start is 0), so the window taper weighs every tick alike
    ticks = [k * 0.5 for k in range(int(np.ceil(t0 / 0.5)), int(np.floor(t1 / 0.5)) + 1) if t0 <= k * 0.5 < t1]
    return float(np.median([sig.band_rms(y, t - 0.225, 0.5, sig.TICK_HZ - 100, sig.TICK_HZ + 100) for t in ticks]))


def test_music_gain_ducking_and_fades(coded, ticks, tmp_path):
    """Measured on reverse-order cuts, with loudness normalisation off so levels can be compared."""
    media = probe(str(coded))
    ranges = [(12.0, 15.0), (6.0, 9.0), (1.0, 4.0)]
    tl = timeline(str(coded), media.fps, ranges)

    def level(name, **kw):
        cfg = dict(path=str(ticks), loudnorm=False, gain_db=-6.0, duck=False, fade_out=0.5)
        cfg.update(kw)
        out = do_render(tl, media, tmp_path / f"{name}.mp4", MusicConfig(**cfg))
        assert_av_lengths(out, media.fps)
        return sig.audio(out)

    loud, quiet = level("loud", only=True), level("quiet", only=True, gain_db=-18.0)
    ratio_db = 20 * __import__("math").log10(_tick_level(loud, 1.0, 7.0) / _tick_level(quiet, 1.0, 7.0))
    assert ratio_db == pytest.approx(12.0, abs=1.5), ratio_db

    plain, ducked = level("plain"), level("ducked", duck=True)
    assert _tick_level(ducked, 1.0, 7.0) < 0.7 * _tick_level(plain, 1.0, 7.0)  # the source is always playing here, so the music ducks
    assert_source_tones(tl, ducked)

    long_fade = level("fade", only=True, fade_out=3.0)  # the music fades over the last 3 s of the 9 s cut
    assert _tick_level(long_fade, 8.0, 9.0) < 0.5 * _tick_level(long_fade, 1.0, 5.0)

    faded_tl = timeline(str(coded), media.fps, ranges, fade_in=1.0, fade_out=1.0)
    out = do_render(faded_tl, media, tmp_path / "cut_fades.mp4")
    y = sig.audio(out)
    tone = lambda t: sig.band_rms(y, t, 0.2, 150, 1500)  # noqa: E731  (the source tone, whatever its second)
    assert tone(0.0) < 0.3 * tone(1.5)  # the cut fades in over 1 s
    end = faded_tl.duration
    assert tone(end - 0.2) < 0.3 * tone(end - 1.8)  # and out over the last 1 s


def test_m2b_junction_window_of_a_reordered_junction(coded, ticks, tmp_path):
    """The review window of an out-of-order dissolve under music: frames, audio length and both clips' sound."""
    from matchcut.review.render import render_window
    from matchcut.review.window import review_window, sub_timeline

    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, [(12.0, 15.0), (2.0, 5.0), (8.0, 11.0)], [Transition(), Transition("dissolve", 0.4), Transition()])
    cfg = PipelineConfig()
    cfg.music.path = str(ticks)
    w = review_window(tl, 0, 1.6, 1.6)  # whole frames at 25 fps: the window starts at 1.0 s, frame 25 of the cut
    out = tmp_path / "window.mp4"
    with deadline(90):
        render_window(tl, w, media, cfg, out)
    st = sig.streams(out)
    assert "audio" in st
    assert abs(float(st["audio"]["duration"]) - float(st["video"]["duration"])) <= sig.av_tolerance(media.fps)
    fps = media.fps
    got = sig.frame_numbers(out)
    assert len(got) == round(w.duration * fps)
    first = round(w.start * fps)
    want = {k - first: v for k, v in sig.expected_frames(tl, fps).items() if first <= k < first + len(got)}  # frames clear of the dissolve
    assert len(want) >= 0.8 * len(got)
    assert all(got[k] == v for k, v in want.items()), [(k, got[k], v) for k, v in want.items() if got[k] != v][:5]
    y = sig.audio(out)
    in_second = int(tl.clips[1].source_in + (w.end - 0.4 - tl.record_start(1)))
    for t, second in ((0.2, int(tl.clips[0].source_in + w.start + 0.2)), (w.duration - 0.4, in_second)):
        assert sig.dominant_tone(y, t) == pytest.approx(sig.tone_hz(second), abs=10), (t, second)
    for t in (0.1, w.duration - 0.6):  # the music plays under both clips (ducked under the source tone)
        assert sig.band_rms(y, t, 0.5, sig.TICK_HZ - 100, sig.TICK_HZ + 100) > 0.0002, t
    assert sub_timeline(tl, w).clips[0].source_in >= tl.clips[0].source_in


def test_one_input_per_clip_seeked_on_the_frame_grid(monkeypatch, coded, ticks, tmp_path):
    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, [(9.0, 11.0), (0.4, 2.0), (5.0, 7.0)])
    inputs = clip_inputs(tl, media.fps)
    assert [ci.seek for ci in inputs] == [8.72, 0.12, 4.72]  # at or before in-point - 0.25 s, on the frame grid, never before the start
    assert [(ci.start, ci.end) for ci in inputs] == [(0.28, 2.28), (0.28, 1.88), (0.28, 2.28)]
    assert all(abs(ci.seek * media.fps - round(ci.seek * media.fps)) < 1e-6 for ci in inputs)
    assert SEEK_MARGIN_SECONDS == 0.25
    seen = {}

    def fake_run(cmd, capture=True):
        seen["cmd"] = cmd
    monkeypatch.setattr(render_mod, "run", fake_run)
    render(tl, media, RCFG, tmp_path / "x.mp4", music=MusicConfig(path=str(ticks)), music_duration=60.0)
    cmd = seen["cmd"]
    assert cmd.count("-i") == 4 and cmd[cmd.index("-filter_complex") - 1] == str(ticks)
    assert "-threads" not in cmd  # few clips: ffmpeg's own decoder threading
    many = timeline(str(coded), media.fps, [(k * 0.5, k * 0.5 + 0.4) for k in range(SINGLE_THREAD_DECODE_ABOVE + 1)])
    render(many, media, RCFG, tmp_path / "y.mp4")
    assert seen["cmd"].count("-threads") == SINGLE_THREAD_DECODE_ABOVE + 1  # one decoder thread per input: memory grows per input


def test_the_open_file_limit_is_raised_or_explained(monkeypatch, coded, tmp_path):
    """One open file per clip: the soft limit is raised toward the hard one, and a hard limit too low is a one-line error."""
    resource = pytest.importorskip("resource")
    calls = []
    monkeypatch.setattr(resource, "getrlimit", lambda what: (64, 4096))
    monkeypatch.setattr(resource, "setrlimit", lambda what, value: calls.append(value))
    monkeypatch.setattr(render_mod, "run", lambda cmd, capture=True: None)
    media = probe(str(coded))
    many = timeline(str(coded), media.fps, [(k * 0.1, k * 0.1 + 0.08) for k in range(100)])
    render(many, media, RCFG, tmp_path / "many.mp4")
    assert calls == [(100 + render_mod.FILES_PER_RENDER, 4096)]
    monkeypatch.setattr(resource, "getrlimit", lambda what: (64, 100))
    with pytest.raises(render_mod.MediaError, match="open-file limit is 100"):
        render(many, media, RCFG, tmp_path / "many.mp4")


def test_a_long_filter_graph_goes_to_ffmpeg_in_a_file(monkeypatch, coded, tmp_path):
    """Linux refuses a single argument over 128 KiB (about 460 clips): a long graph is written to a file, and the file is removed after."""
    seen = {}

    def fake_run(cmd, capture=True):
        seen["cmd"] = cmd
        option = next(o for o in render_mod.GRAPH_FILE_OPTIONS if o in cmd)
        seen["graph"] = Path(cmd[cmd.index(option) + 1]).read_text()
    monkeypatch.setattr(render_mod, "run", fake_run)
    monkeypatch.setattr(render_mod, "seek_margin", lambda path: render_mod.SEEK_MARGIN_SECONDS)  # (run is faked: no ffprobe here)
    monkeypatch.setattr(render_mod, "INLINE_GRAPH_LIMIT", 2000)
    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, [(k * 0.5, k * 0.5 + 0.4) for k in range(20)])
    render(tl, media, RCFG, tmp_path / "long.mp4")
    assert "-filter_complex" not in seen["cmd"] and seen["graph"].endswith("[aout]")
    assert not list(tmp_path.glob(".graph-*"))


@pytest.mark.parametrize("rate, dissolve", [("30", 0.75), ("30000/1001", 0.75), ("25", 0.3)])
def test_many_fractional_dissolves_do_not_drift(tmp_path, rate, dissolve):
    """Eight 0.75 s dissolves (22.5 / 22.48 frames each): the renderer rounds the running total, as the exporters' layout does, so every clip
    starts within half a frame of Timeline.record_start and the cut is round(duration) frames long (rounding each on its own drifted by
    up to 8 frames after 15 dissolves)."""
    coded = sig.make_coded_footage(tmp_path / "frac.mp4", seconds=50, rate=rate)  # every clip inside the file (1170 frames at 25 fps)
    media = probe(str(coded))
    fps = media.fps
    f = lambda n: round(n / fps, 4)  # noqa: E731
    tl = timeline(str(coded), fps, [(f(k * 130), f(k * 130 + 90)) for k in range(9)], [Transition()] + [Transition("dissolve", dissolve)] * 8)  # 0.3 s at 25 fps: 7.5-frame ties
    from fractions import Fraction

    from matchcut.interchange.common import layout, record_frames

    rows = layout(tl, Fraction(rate))
    assert all(abs(r.rec - tl.record_start(i) * fps) <= 0.5 + 1e-6 for i, r in enumerate(rows))  # the exporters stay on record_start
    out = do_render(tl, media, tmp_path / "frac_out.mp4")
    got = sig.frame_numbers(out)
    assert len(got) == record_frames(rows) == round(tl.duration * fps)
    for i, r in enumerate(rows):  # each clip alone (between its transitions) shows the frames the exported layout puts there
        lo, hi = r.rec + r.tr, (rows[i + 1].rec if i + 1 < len(rows) else record_frames(rows))
        wrong = [(k, got[k], r.in_f + (k - r.rec)) for k in range(lo, hi) if got[k] != r.in_f + (k - r.rec)]
        assert not wrong, (i, wrong[:3])
    assert_video_exact(tl, out, fps)
    assert_av_lengths(out, fps)


def test_the_graph_file_is_removed_when_ffmpeg_fails(monkeypatch, coded, tmp_path):
    def failing_run(cmd, capture=True):
        raise render_mod.MediaError("ffmpeg failed")
    monkeypatch.setattr(render_mod, "run", failing_run)
    monkeypatch.setattr(render_mod, "seek_margin", lambda path: render_mod.SEEK_MARGIN_SECONDS)
    monkeypatch.setattr(render_mod, "INLINE_GRAPH_LIMIT", 100)
    media = probe(str(coded))
    with pytest.raises(render_mod.MediaError):
        render(timeline(str(coded), media.fps, [(1.0, 2.0), (3.0, 4.0)]), media, RCFG, tmp_path / "fail.mp4")
    assert not list(tmp_path.glob(".graph-*"))


def test_music_under_loudnorm_keeps_its_timing_to_the_end(coded, ticks, tmp_path):
    """loudnorm leaves a 20-100 ms timestamp jump about 2.9 s before the end; trimming to the video's length by timestamp then cut real
    audio and played the last seconds late.  Every tick (every 0.5 s of the cut, music.start 0) is where it should be, to the end."""
    import numpy as np

    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, [(1.0, 4.68), (6.0, 9.68), (12.0, 15.68)])  # 11.04 s
    out = do_render(tl, media, tmp_path / "ln.mp4", MusicConfig(path=str(ticks), only=True, gain_db=-6.0, fade_out=0.1))
    assert_av_lengths(out, media.fps)
    y = sig.audio(out)
    hop = 48  # 1 ms
    env = np.array([sig.band_rms(y, k * hop / sig.SR, 0.004, sig.TICK_HZ - 300, sig.TICK_HZ + 300) for k in range(len(y) // hop - 4)])
    on = env > 0.25 * env.max()
    onsets = [k * hop / sig.SR for k in range(len(on)) if on[k] and (k == 0 or not on[k - 1])]
    want = [k * 0.5 for k in range(int(tl.duration / 0.5) + 1) if k * 0.5 < tl.duration - 0.06]
    assert len(onsets) == len(want), (onsets, want)
    assert max(abs(a - b) for a, b in zip(onsets, want)) < 0.006, list(zip(onsets, want))


def _fake_ffmpeg(tmp_path: Path, name: str, accepts: tuple) -> str:
    """A stand-in for another ffmpeg version: it accepts only the graph-file options in ``accepts`` (as that version would), and runs the
    installed ffmpeg with the option it understands.  ffmpeg 9 removed -filter_complex_script and reads the graph with -/filter_complex."""
    import shutil as _sh
    real = _sh.which("ffmpeg")
    script = tmp_path / name
    script.write_text(f"""#!/bin/sh
for a in "$@"; do
  case "$a" in
    -filter_complex_script) case " {' '.join(accepts)} " in *" -filter_complex_script "*) ;; *) echo "Unrecognized option 'filter_complex_script'." >&2; exit 8;; esac;;
    -/filter_complex) case " {' '.join(accepts)} " in *" -/filter_complex "*) ;; *) echo "Unrecognized option '/filter_complex'." >&2; exit 8;; esac;;
  esac
done
if [ "$1" = "-version" ]; then echo "ffmpeg version 9.0-stand-in"; exit 0; fi
args=""
for a in "$@"; do
  if [ "$a" = "-/filter_complex" ]; then a="-filter_complex_script"; fi
  args="$args '$(printf %s "$a" | sed "s/'/'\\\\''/g")'"
done
eval exec "{real}" $args
""")
    script.chmod(0o755)
    return str(script)


def test_the_graph_file_option_is_the_one_this_ffmpeg_supports(monkeypatch, coded, tmp_path):
    """Asked of each ffmpeg: the installed one (6.1 here: -filter_complex_script); a stand-in for ffmpeg 9 (only -/filter_complex) renders a
    long graph frame-exact through -/filter_complex; an ffmpeg that accepts neither is a one-line error, with no graph file left."""
    import shutil as _sh

    from matchcut.pipeline.render import graph_file_option

    assert graph_file_option(_sh.which("ffmpeg")) in render_mod.GRAPH_FILE_OPTIONS
    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, [(9.0, 11.0), (5.0, 7.0), (1.0, 3.0)], [Transition(), Transition("dissolve", 0.4), Transition()])
    monkeypatch.setattr(render_mod, "INLINE_GRAPH_LIMIT", 100)  # force the file path
    nine = _fake_ffmpeg(tmp_path, "ffmpeg9", ("-/filter_complex",))
    assert graph_file_option(nine) == "-/filter_complex"
    seen = []
    real_run = render_mod.run
    monkeypatch.setattr(render_mod, "_require", lambda name: nine if name == "ffmpeg" else _sh.which(name))
    monkeypatch.setattr(render_mod, "run", lambda cmd, capture=True: (seen.append(cmd), real_run(cmd, capture=capture))[1])
    out = do_render(tl, media, tmp_path / "nine.mp4")
    assert "-/filter_complex" in seen[-1] and "-filter_complex_script" not in seen[-1]
    assert_video_exact(tl, out, media.fps)
    assert_av_lengths(out, media.fps)
    neither = _fake_ffmpeg(tmp_path, "ffmpeg_old", ())
    monkeypatch.setattr(render_mod, "_require", lambda name: neither if name == "ffmpeg" else _sh.which(name))
    with pytest.raises(render_mod.MediaError, match="accepts neither"):
        render(tl, media, RCFG, tmp_path / "neither.mp4")
    assert not list(tmp_path.glob(".graph-*"))


@pytest.fixture(scope="module")
def audio_tail(tmp_path_factory) -> Path:
    """10 s of video whose audio runs on to 12 s: the container is 12 s long, the picture 10 s."""
    import subprocess as sp

    out = tmp_path_factory.mktemp("tail") / "tail.mp4"
    sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=10", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=12",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)], check=True)
    return out


@pytest.fixture(scope="module")
def audio_tail_mkv(audio_tail) -> Path:
    """The same file in Matroska: its video stream records no duration (only a DURATION tag)."""
    import subprocess as sp

    out = audio_tail.with_suffix(".mkv")
    sp.run(["ffmpeg", "-v", "error", "-y", "-i", str(audio_tail), "-c", "copy", str(out)], check=True)
    return out


@pytest.mark.parametrize("container", ["mp4", "mkv"])
@pytest.mark.parametrize("ranges, transition, message", [
    # past the end of the file: the input that made ffmpeg wait forever (main hangs here)
    ([(1.0, 3.0), (12.5, 13.5)], Transition("dissolve", 0.4), "starts at 12.500 s, at or after the end of the video"),
    # inside the file's audio tail but after the picture
    ([(1.0, 3.0), (10.5, 11.5)], Transition("dissolve", 0.4), "starts at 10.500 s, at or after the end of the video"),
    ([(1.0, 3.0), (10.5, 11.5)], Transition(), "starts at 10.500 s, at or after the end of the video"),
    ([(-0.5, 1.0), (3.0, 4.0)], Transition(), "before the start"),
    ([(1.0, 1.02), (3.0, 4.0)], Transition(), "shorter than one frame"),
])
def test_a_range_the_video_cannot_play_is_refused_before_ffmpeg_starts(audio_tail, audio_tail_mkv, tmp_path, container, ranges, transition, message):
    """Run for real (ffmpeg is not patched out): a regression shows as a timeout here, not as a pass."""
    src = audio_tail if container == "mp4" else audio_tail_mkv
    media = probe(str(src))
    assert media.duration > 11.5  # the container (with the audio) runs past the video's 10 s
    tl = timeline(str(src), media.fps, ranges, [Transition(), transition])
    with pytest.raises(render_mod.RangeError, match=message) as err:
        with deadline(30):
            render(tl, media, RCFG, tmp_path / "bad.mp4")
    if "end of the video" in message:
        import re

        assert re.search(r"runs 0\.0\d\d-10\.0\d\d s", str(err.value)) and "with its audio, runs to 12." in str(err.value), str(err.value)
    assert not (tmp_path / "bad.mp4").exists()


def test_an_out_point_past_the_picture_is_rendered_to_the_last_frame_with_a_warning(audio_tail, tmp_path, caplog):
    """Older workspaces ended their last shot at the container's end: such a clip renders to the video's last frame (warned), it is not
    refused; the stage itself is not changed."""
    media = probe(str(audio_tail))
    tl = timeline(str(audio_tail), media.fps, [(1.0, 3.0), (8.0, 11.5)])
    before = tl.to_dict()
    with caplog.at_level("WARNING", logger="matchcut.pipeline"):
        out = do_render(tl, media, tmp_path / "over.mp4")
    assert any("past the end of the picture: rendered to 10.000 s" in r.getMessage() for r in caplog.records)
    assert tl.to_dict() == before
    st = sig.streams(out)
    assert int(st["video"]["nb_read_frames"]) == 100 and abs(float(st["audio"]["duration"]) - 4.0) < 0.002


def test_a_clip_ending_exactly_at_the_last_frame_at_60_fps_is_not_trimmed(tmp_path, caplog):
    """1111 frames at 60 fps end at 18.51666...; the stored 4-decimal point 18.5167 is not past the picture."""
    import subprocess as sp

    src = tmp_path / "f60.mp4"
    sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=60", "-f", "lavfi", "-i", "sine=f=440:r=48000",
            "-frames:v", "1111", "-t", "18.6", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    media = probe(str(src))
    tl = timeline(str(src), media.fps, [(1.0, 2.0), (17.0, round(1111 / 60, 4))])
    with caplog.at_level("WARNING", logger="matchcut.pipeline"):
        out = do_render(tl, media, tmp_path / "f60_out.mp4")
    assert not any("past the end of the picture" in r.getMessage() for r in caplog.records)
    assert int(sig.streams(out)["video"]["nb_read_frames"]) == 60 + 91


def test_the_pipeline_ends_its_last_shot_at_the_picture_and_renders(demo_video, tmp_path):
    """Footage whose audio runs 0.1 s past its picture (as phone footage often does): the last shot ends with the picture, and the run
    renders (it was refused: the last clip ended at the container's end)."""
    import subprocess as sp

    from matchcut.pipeline.config import PipelineConfig
    from matchcut.pipeline.pipeline import Pipeline
    from matchcut.pipeline.shots import load_shots

    src = tmp_path / "tail_demo.mp4"
    v = probe(str(demo_video)).duration
    sp.run(["ffmpeg", "-v", "error", "-y", "-i", str(demo_video), "-f", "lavfi", "-i", f"sine=f=440:r=48000:d={v + 0.12:.2f}",
            "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", str(src)], check=True)
    extent = __import__("matchcut.pipeline.media", fromlist=["video_extent"]).video_extent(str(src))
    assert extent.container_end - extent.end > 0.08
    cfg = PipelineConfig(source=str(src), workspace=str(tmp_path / "ws"), target_duration=30.0)
    cfg.llm.backend = "mock"
    cfg.render.proxy = True
    final = Pipeline(cfg).run()
    shots = load_shots(tmp_path / "ws" / "shots.json")
    assert abs(shots[-1].end - extent.end) < 1e-6
    assert all(c.source_out <= extent.end + 1e-4 for c in final.clips)
    assert (tmp_path / "ws" / "final_proxy.mp4").exists()


def test_a_clip_ending_within_a_frame_of_the_video_end_still_renders(audio_tail, tmp_path):
    media = probe(str(audio_tail))
    tl = timeline(str(audio_tail), media.fps, [(1.0, 3.0), (8.0, 10.02)])  # half a frame past the picture: as a shot ending at the file's end can
    out = do_render(tl, media, tmp_path / "edge.mp4")
    assert "audio" in sig.streams(out)


@pytest.fixture(scope="module")
def late_video(tmp_path_factory) -> dict:
    """Video that starts 2 s after the file's audio, as MPEG-TS and remuxed to Matroska (one keyframe: GOP longer than the clip)."""
    import subprocess as sp

    d = tmp_path_factory.mktemp("late")
    ts, mkv = d / "late.ts", d / "late.mkv"
    sp.run(["ffmpeg", "-v", "error", "-y", "-itsoffset", "2", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=8", "-f", "lavfi", "-i",
            "sine=f=440:r=48000:d=10", "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(ts)], check=True)
    sp.run(["ffmpeg", "-v", "error", "-y", "-i", str(ts), "-c", "copy", str(mkv)], check=True)
    return {"ts": ts, "mkv": mkv}


@pytest.mark.parametrize("ext", ["ts", "mkv"])
def test_a_clip_before_the_first_frame_is_refused_and_one_overlapping_it_is_trimmed(late_video, tmp_path, caplog, ext):
    """A dissolve into a clip with no picture before the video starts hung ffmpeg as the past-the-end case did: refused.  A clip that
    starts before the picture and runs into it renders from the first frame, with a warning."""
    from matchcut.pipeline.media import video_extent

    src = late_video[ext]
    media = probe(str(src))
    extent = video_extent(str(src))
    assert 1.9 < extent.start < 2.2
    before = timeline(str(src), media.fps, [(4.0, 6.0), (0.2, 1.5)], [Transition(), Transition("dissolve", 0.4)])
    with pytest.raises(render_mod.RangeError, match="before the first frame of the video"):
        with deadline(30):
            render(before, media, RCFG, tmp_path / "before.mp4")
    overlap = timeline(str(src), media.fps, [(4.0, 6.0), (1.0, 4.0)])
    with caplog.at_level("WARNING", logger="matchcut.pipeline"):
        out = do_render(overlap, media, tmp_path / "overlap.mp4")
    assert any("before the first frame of the picture: rendered from" in r.getMessage() for r in caplog.records)
    st = sig.streams(out)
    assert abs(float(st["audio"]["duration"]) - float(st["video"]["duration"])) <= sig.av_tolerance(media.fps)


def test_a_29_97_fps_mkv_clip_ending_at_its_last_frame_keeps_it(tmp_path, caplog):
    """Matroska writes its DURATION tag to the millisecond, a few hundredths of a frame short at 29.97 fps: the last frame stays."""
    import subprocess as sp

    mp4, mkv = tmp_path / "v.mp4", tmp_path / "v.mkv"
    sp.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30000/1001", "-frames:v", "302", "-c:v", "libx264",
            "-preset", "veryfast", "-pix_fmt", "yuv420p", str(mp4)], check=True)
    sp.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-c", "copy", str(mkv)], check=True)
    for src in (mp4, mkv):
        media = probe(str(src))
        tl = timeline(str(src), media.fps, [(1.0, 2.0), (round(200 * 1001 / 30000, 4), round(302 * 1001 / 30000, 4))])
        caplog.clear()
        with caplog.at_level("WARNING", logger="matchcut.pipeline"):
            out = do_render(tl, media, tmp_path / f"{src.suffix[1:]}_out.mp4")
        assert not any("past the end of the picture" in r.getMessage() for r in caplog.records), src.suffix
        assert int(sig.streams(out)["video"]["nb_read_frames"]) == 30 + 102, src.suffix


def test_a_trim_that_leaves_a_clip_shorter_than_its_dissolve_is_refused(audio_tail, tmp_path):
    media = probe(str(audio_tail))
    tl = timeline(str(audio_tail), media.fps, [(1.0, 3.0), (9.9, 11.0)], [Transition(), Transition("dissolve", 0.4)])
    with pytest.raises(render_mod.RangeError, match="too few for its 10-frame transition"):
        render(tl, media, RCFG, tmp_path / "short.mp4")


def test_an_mpeg_ts_source_with_long_gops_seeks_by_its_keyframe_gap(tmp_path, caplog):
    """MPEG-TS (AVCHD .mts) has no index: a seek lands on a timestamp, not a keyframe, and an input seeked 0.25 s early decoded nothing
    (47 of 140 frames).  Its inputs are seeked by the file's longest keyframe gap instead: reverse order and a dissolve, frame-exact, every
    tone right; and a clip from 0.0 keeps its first frame (the video starts a fraction of a frame after the audio) with no warning."""
    import subprocess as sp

    mp4 = sig.make_coded_footage(tmp_path / "long_gop.mp4", seconds=12, gop=300)
    ts = tmp_path / "long_gop.ts"
    sp.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-c", "copy", str(ts)], check=True)
    assert not render_mod.seekable(str(ts)) and render_mod.seekable(str(mp4))
    assert render_mod.seek_margin(str(ts)) > 1.0 and render_mod.seek_margin(str(mp4)) == render_mod.SEEK_MARGIN_SECONDS
    media = probe(str(ts))
    tl = timeline(str(ts), media.fps, [(8.0, 10.0), (5.0, 7.0), (0.0, 3.0)], [Transition(), Transition("dissolve", 0.4), Transition()])
    with caplog.at_level("WARNING", logger="matchcut.pipeline"):
        out = do_render(tl, media, tmp_path / "ts_out.mp4")
    assert not any("first frame" in r.getMessage() for r in caplog.records)
    assert_video_exact(tl, out, media.fps)
    assert_av_lengths(out, media.fps)
    assert_source_tones(tl, sig.audio(out))
