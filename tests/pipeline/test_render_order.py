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
    """At 30 fps the old renderer showed hard cuts a frame late too (its in-points and fps filter were rounded to 4 decimals)."""
    coded = sig.make_coded_footage(tmp_path / "f30.mp4", seconds=12, rate="30")
    media = probe(str(coded))
    f = lambda n: round(n / 30, 4)  # noqa: E731
    trs = [Transition(), Transition("dissolve", f(12)), Transition("dip_to_black", f(9))] if transitions else None
    for name, ranges in (("source order", [(f(40), f(100)), (f(160), f(220)), (f(250), f(310))]), ("reverse order", [(f(250), f(310)), (f(160), f(220)), (f(40), f(100))])):
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
        seen["graph"] = Path(cmd[cmd.index("-filter_complex_script") + 1]).read_text()
    monkeypatch.setattr(render_mod, "run", fake_run)
    monkeypatch.setattr(render_mod, "INLINE_GRAPH_LIMIT", 2000)
    media = probe(str(coded))
    tl = timeline(str(coded), media.fps, [(k * 0.5, k * 0.5 + 0.4) for k in range(20)])
    render(tl, media, RCFG, tmp_path / "long.mp4")
    assert "-filter_complex" not in seen["cmd"] and seen["graph"].endswith("[aout]")
    assert not list(tmp_path.glob(".graph-*"))
