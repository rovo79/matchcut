"""The review controller (fake sessions plus one real headless mpv) and the review CLI."""
from __future__ import annotations

import io
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from matchcut.pipeline.timeline import Clip, Timeline, Transition
from matchcut.review import __main__ as cli
from matchcut.review.controller import ReviewController, open_with_system
from matchcut.review.items import ReviewItem, ReviewPair, prepare_review
from matchcut.review.mpv import MpvError, MpvSession
from matchcut.review.window import ReviewError

from .test_items import BLUE, RED, small_cfg, two_shots, workspace

needs_mpv = pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")


def headless(binary):
    return MpvSession(binary, headless=True)


def item(kind="render", cached=False, label="B  final  shot 3 -> shot 4  cut", path="/tmp/x.mp4", cut=1.0, duration=2.0):
    return ReviewItem("final", 0, label, Path(path), kind, cut, duration, "cut", cached)


class Dead:
    def __init__(self, binary):
        raise MpvError("mpv not found")


class Fake:
    """A scripted session: records commands, answers get() from a dict."""

    def __init__(self, binary="mpv", props=None):
        self.calls, self.props, self.up = [], props or {}, False

    def start(self):
        self.up = True

    def alive(self):
        return self.up

    def load(self, path, timeout=10.0):
        self.calls.append(("load", path))
        self.props["path"] = path

    def set(self, name, value, timeout=2.0):
        self.calls.append(("set", name, value))

    def get(self, name, timeout=2.0):
        return self.props[name]

    def command(self, *args, timeout=2.0):
        self.calls.append(args)

    def close(self):
        self.up = False


def test_without_mpv_a_rendered_item_goes_to_the_system_opener():
    opened = []
    c = ReviewController(small_cfg(), session_factory=Dead, opener=lambda p: (opened.append(p) or (True, f"opened {p.name} in the system player")))
    assert c.has_mpv() is False
    it = item()
    c.play(it)
    assert opened == [it.path]
    assert c.status == "mpv not found: opened x.mp4 in the system player (no A/B toggle)"
    assert c.toggle() is False
    assert c.status.startswith("mpv not found:")
    assert c.current is it and c.pair is None


def test_without_mpv_and_without_a_player_the_status_names_the_file():
    c = ReviewController(small_cfg(), session_factory=Dead, opener=lambda p: (False, f"no player found: the file is {p}"))
    c.play(item(path="/w/r.mp4"))
    assert c.status == "mpv not found: no player found: the file is /w/r.mp4"


def test_without_mpv_an_edl_item_is_refused():
    c = ReviewController(small_cfg(), session_factory=Dead, opener=lambda p: (True, "x"))
    with pytest.raises(ReviewError, match="cannot play an EDL window without mpv: prepare it with force_render"):
        c.play(item("edl", path="/w/x.edl"))


def test_has_mpv_is_false_when_start_fails_and_never_raises():
    class Fails(Fake):
        def start(self):
            raise MpvError("boom")

    assert ReviewController(small_cfg(), session_factory=Fails).has_mpv() is False
    assert ReviewController(small_cfg(), session_factory=lambda b: 1 / 0).has_mpv() is False


def test_stop_with_the_mpv_window_closed_says_so_instead_of_keeping_the_stale_status():
    c = ReviewController(small_cfg(), session_factory=Fake)
    try:
        c.play(item())
        assert c.status.startswith("playing ")
        c._session.up = False  # the editor closed the mpv window
        assert c.stop() is False
        assert c.status == "mpv is not running"
        assert ReviewController(small_cfg(), session_factory=Fake).stop() is False  # never started
    finally:
        c.close()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_failed_start_is_retried_after_30_seconds_or_when_the_mpv_setting_changes():
    clock, made = Clock(), []

    class Fails(Fake):
        def start(self):
            made.append(self)
            raise MpvError("boom")

    cfg = small_cfg()
    c = ReviewController(cfg, session_factory=Fails, clock=clock)
    assert c.has_mpv() is False and len(made) == 1
    clock.now += 29.0
    assert c.has_mpv() is False and len(made) == 1  # too soon: not retried
    clock.now += 2.0
    assert c.has_mpv() is False and len(made) == 2  # 31 s after the failure
    clock.now += 5.0
    assert c.has_mpv() is False and len(made) == 2
    cfg.review.mpv = "/opt/mpv/bin/mpv"  # the editor fixed the setting
    assert c.has_mpv() is False and len(made) == 3


def test_a_retry_that_works_starts_mpv_and_forgets_the_failure():
    clock, tries = Clock(), []

    def factory(binary):
        tries.append(binary)
        return Fake(binary) if len(tries) > 1 else Dead(binary)

    c = ReviewController(small_cfg(), session_factory=factory, clock=clock)
    try:
        assert c.has_mpv() is False
        clock.now += 31.0
        assert c.has_mpv() is True and c.has_mpv() is True and len(tries) == 2
        c._session.up = False  # the window is closed again: a new session starts at once (no 30 s wait)
        assert c.has_mpv() is True and len(tries) == 3
    finally:
        c.close()


@pytest.mark.parametrize("kind,cached,tail", [
    ("edl", False, "(exact source)"),
    ("edl", True, "(exact source, cached)"),
    ("render", False, "(rendered)"),
    ("render", True, "(rendered, cached)"),
])
def test_the_status_strings(kind, cached, tail):
    c = ReviewController(small_cfg(), session_factory=Fake)
    try:
        it = item(kind, cached)
        c.play(it)
        assert c.status == f"playing {it.label} {tail}"
        assert c.current is it and c.pair is None
        c.stop()
        assert c.status == "paused"
    finally:
        c.close()


def test_play_opens_the_file_at_the_target_then_loops_unpauses_and_labels():
    made = []

    def factory(binary):
        made.append(Fake(binary))
        return made[-1]

    c = ReviewController(small_cfg(), session_factory=factory)
    try:
        it = item(cut=1.0, duration=2.0, label="B $x")
        c.play(it, from_cut=0.3)
        assert made[0].calls == [
            ("set", "start", "1.3000"), ("load", str(it.path)), ("set", "start", "none"), ("set", "loop-file", "inf"),
            ("set", "pause", False), ("show-text", "B $$x", 2000),
        ]
        c.play(it, from_cut=5.0)  # clamped to the end of the item
        assert ("set", "start", "1.9500") in made[0].calls
        c.play(it, from_cut=-5.0)
        assert ("set", "start", "0.0000") in made[0].calls
        c.play(it)
        assert made[0].calls[-6] == ("set", "start", "0.0000")  # the file is opened at its own start, never seeked
        assert not [x for x in made[0].calls if x[0] == "seek"]
    finally:
        c.close()


def test_play_pair_plays_a_when_present_and_toggle_keeps_the_offset_from_the_cut():
    made = []

    def factory(binary):
        made.append(Fake(binary, {"time-pos": 1.0 + 0.3}))
        return made[-1]

    a, b = item(label="A", path="/w/a.mp4", cut=1.0), item(label="B", path="/w/b.mp4", cut=1.5)
    c = ReviewController(small_cfg(), session_factory=factory)
    try:
        c.play_pair(ReviewPair(b, a))
        assert c.current is a and c.pair.b is b
        assert c.toggle() is True
        assert c.current is b
        assert ("set", "start", "1.8000") in made[0].calls
        made[0].props["time-pos"] = 1.8
        assert c.toggle() is True and c.current is a
    finally:
        c.close()


def test_toggle_still_switches_when_the_position_cannot_be_read():
    class NoPos(Fake):
        def get(self, name, timeout=2.0):
            raise MpvError("get_property time-pos: property unavailable")

    a, b = item(label="A", cut=1.0), item(label="B", cut=1.5)
    c = ReviewController(small_cfg(), session_factory=NoPos)
    try:
        c.play_pair(ReviewPair(b, a))
        assert c.toggle() is True and c.current is b
        assert c.status.endswith("(position unknown: started at the cut)") and c.status.startswith("playing B")
    finally:
        c.close()


def test_toggle_without_a_pair_or_with_only_b_returns_false_and_keeps_the_status():
    c = ReviewController(small_cfg(), session_factory=Fake)
    try:
        assert c.toggle() is False
        c.play_pair(ReviewPair(item(), None, "nothing earlier to compare"))
        before = c.status
        assert c.toggle() is False and c.status == before
        assert c.pair is not None and c.current is c.pair.b
    finally:
        c.close()


def test_an_mpv_error_becomes_a_review_error_and_a_dead_mpv_is_restarted():
    sessions = []

    class Flaky(Fake):
        def load(self, path, timeout=10.0):
            if not sessions or self is sessions[0]:
                self.up = False
                raise MpvError("mpv has exited")
            super().load(path, timeout)

    def factory(binary):
        sessions.append(Flaky(binary))
        return sessions[-1]

    c = ReviewController(small_cfg(), session_factory=factory)
    try:
        c.play(item())
        assert len(sessions) == 2 and c.status.startswith("playing ")
    finally:
        c.close()

    class Broken(Fake):
        def load(self, path, timeout=10.0):
            raise MpvError("mpv could not open /x")

    c = ReviewController(small_cfg(), session_factory=Broken)
    try:
        with pytest.raises(ReviewError, match="could not open"):
            c.play(item())
    finally:
        c.close()


def test_open_with_system(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setattr("sys.platform", "linux")
    ok, msg = open_with_system(Path("/w/r.mp4"))
    assert ok and msg == "opened r.mp4 in the system player"
    assert calls[0][0] == ["xdg-open", "/w/r.mp4"] and calls[0][1]["start_new_session"] is True
    monkeypatch.setattr("sys.platform", "darwin")
    open_with_system(Path("/w/r.mp4"))
    assert calls[1][0][0] == "open"
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert open_with_system(Path("/w/r.mp4")) == (False, "no player found: the file is /w/r.mp4")


# A/B with a real mpv ------------------------------------------------------------------------------

def ab_workspace(tmp_path, video):
    """roughcut: blue -> red hard cut; final: the same junction with a 0.5 s dissolve (so B renders, A is an EDL)."""
    rough = Timeline(source="demo.mp4", fps=25.0, stage="roughcut", clips=[
        Clip("c001", BLUE, 0.5, 2.0, "b1"), Clip("c002", RED, 5.0, 6.5, "b2")])
    final = two_shots("dissolve", 0.5)
    return workspace(tmp_path, video, roughcut=rough, final=final)


@needs_mpv
def test_toggling_a_and_b_keeps_the_same_offset_from_the_cut(demo_video, tmp_path):
    ws = ab_workspace(tmp_path, demo_video)
    cfg = small_cfg(1.0, 1.0)
    pair = prepare_review(ws, cfg, "final", 0, ab=True)
    assert pair.a is not None and pair.a.kind == "edl" and pair.b.kind == "render"
    c = ReviewController(cfg, session_factory=headless)
    try:
        assert c.has_mpv()
        c.play_pair(pair)
        assert c.current is pair.a and c.status.startswith("playing A")
        c._session.set("pause", True)
        c._session.command("seek", pair.a.cut + 0.3, "absolute+exact")
        assert abs(c._session.get("time-pos") - (pair.a.cut + 0.3)) < 0.1
        assert c.toggle() is True
        assert c.current is pair.b
        pos_b = c._session.get("time-pos")
        print(f"A at cut+0.3 -> B time-pos {pos_b:.3f} (b.cut+0.3 = {pair.b.cut + 0.3:.3f})")
        assert abs(pos_b - (pair.b.cut + 0.3)) < 0.1
        c._session.set("pause", True)
        assert c.toggle() is True
        assert c.current is pair.a
        pos_a = c._session.get("time-pos")
        print(f"B back to A: time-pos {pos_a:.3f} (a.cut+0.3 = {pair.a.cut + 0.3:.3f})")
        assert abs(pos_a - (pair.a.cut + 0.3)) < 0.1
    finally:
        c.close()
    assert c._session is None


# no frame of the wrong place after a toggle (vo=image writes every frame mpv shows) ----------------

RAMP_SECONDS = 6.0
RAMP_PER_SECOND = 40  # left half's luma = 40 * t: a frame says when it is from; the right half says which file it is


def ramp_video(path, marker):
    """6 s at 25 fps; the left half's luma is 40 * t and the right half is ``marker`` (so A and B frames tell apart)."""
    graph = f"color=c=black:size=64x48:rate=25:duration={RAMP_SECONDS},format=yuv420p,geq=lum='if(gte(X,W/2),{marker},{RAMP_PER_SECOND}*T)':cb=128:cr=128"
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", graph, "-f", "lavfi", "-i", f"sine=frequency=440:duration={RAMP_SECONDS}"]
    # (with an audio track mpv plays in real time under --ao=null; without one vo=image runs unpaced)
    subprocess.run(cmd + ["-c:v", "libx264", "-qp", "0", "-g", "250", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


def frame_times(frames_dir, first):
    """(marker, seconds) of every frame mpv wrote from number ``first`` on, read back through a 2x1 downscale."""
    names = sorted(frames_dir.glob("*.png"))[first:]
    if not names:
        return []
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-start_number", str(first + 1), "-i", str(frames_dir / "%08d.png"), "-vf", "scale=2:1:flags=area,format=gray", "-f", "rawvideo", "-"],
        capture_output=True, check=True).stdout
    return [(raw[2 * k + 1], raw[2 * k] / RAMP_PER_SECOND) for k in range(len(raw) // 2)]


@needs_mpv
def test_a_toggle_shows_no_frame_from_before_the_target(tmp_path):
    a_path, b_path = ramp_video(tmp_path / "a.mp4", 0), ramp_video(tmp_path / "b.mp4", 255)
    a = ReviewItem("final", 0, "A", a_path, "render", 3.0, RAMP_SECONDS, "cut", False)
    b = ReviewItem("final", 0, "B", b_path, "render", 3.0, RAMP_SECONDS, "cut", False)
    frames = tmp_path / "frames"
    frames.mkdir()
    cfg = small_cfg()
    for attempt in range(3):  # this mpv build's vo=image sometimes aborts at a file start: try again
        for old in frames.glob("*"):
            old.unlink()
        c = ReviewController(cfg, session_factory=lambda binary: MpvSession(binary, headless=True, video_args=["--vo=image", "--vo-image-format=png", "--ao=null", "--vf=format=rgb24", f"--vo-image-outdir={frames}"]))
        try:
            c.play_pair(ReviewPair(b, a))
            c._session.set("pause", True)
            c._session.command("seek", a.cut + 0.3, "absolute+exact")  # A at cut + 0.3, held there
            time.sleep(0.3)
            before = len(list(frames.glob("*.png")))
            assert c.toggle() is True and c.current is b
            deadline = time.time() + 5
            while time.time() < deadline and len([f for f in frame_times(frames, before) if f[0] > 200]) < 6:
                time.sleep(0.1)
            seen = [t for marker, t in frame_times(frames, before) if marker > 200]  # the B frames after the toggle
            start_prop = c._session.get("start")
            break
        except MpvError:
            continue
        finally:
            c.close()
    else:
        pytest.fail("mpv kept stopping under vo=image")
    assert len(seen) >= 6, seen
    assert min(seen) >= 3.3 - 0.05, f"a frame from before the target was shown: {sorted(seen)[:4]}"
    assert abs(seen[0] - 3.3) <= 0.1
    assert start_prop == "none"  # later loops start at 0 again
    print(f"B frames after the toggle start at t={seen[0]:.2f} (target {b.cut + 0.3:.2f})")


@needs_mpv
def test_a_looping_item_wraps_to_its_own_start_after_being_opened_at_a_target(tmp_path):
    a = ReviewItem("final", 0, "A", ramp_video(tmp_path / "a.mp4", 0), "render", 3.0, RAMP_SECONDS, "cut", False)
    c = ReviewController(small_cfg(), session_factory=headless)
    try:
        c.play(a, from_cut=2.5)  # opened 0.5 s before its end
        time.sleep(1.2)
        assert float(c._session.get("time-pos")) < 2.0  # it looped to 0 (not to 5.5)
    finally:
        c.close()


@needs_mpv
def test_the_session_survives_pause_and_silence_and_no_second_mpv_is_spawned(demo_video, tmp_path):
    ws = ab_workspace(tmp_path, demo_video)
    cfg = small_cfg(1.0, 1.0)
    pair = prepare_review(ws, cfg, "final", 0, ab=True)
    made = []

    def factory(binary):
        made.append(headless(binary))
        return made[-1]

    c = ReviewController(cfg, session_factory=factory)
    try:
        c.play_pair(pair)
        pid = c._session._proc.pid
        c.stop()
        assert c.status == "paused"
        c._session.command("seek", pair.a.cut + 0.3, "absolute+exact")
        c._session.wait_restart(1.0)
        time.sleep(2.5)  # mpv is silent while paused; the connection must stay up
        assert c._session.alive()
        assert c.toggle() is True
        assert c.current is pair.b and c.status.startswith("playing B")
        assert abs(c._session.get("time-pos") - (pair.b.cut + 0.3)) < 0.1
        c.stop()
        assert c.status == "paused"
        time.sleep(2.5)
        assert c.toggle() is True and c.current is pair.a
        assert len(made) == 1 and c._session._proc.pid == pid
    finally:
        c.close()


@needs_mpv
def test_the_controller_closes_mpv(demo_video, tmp_path):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    cfg = small_cfg()
    c = ReviewController(cfg, session_factory=headless)
    try:
        c.play(prepare_review(ws, cfg, "final", 0).b)
        s = c._session
        assert s.alive()
    finally:
        c.close()
        c.close()
    assert not s.alive()


# the CLI ------------------------------------------------------------------------------------------

def run_cli(capsys, *argv, **kw):
    code = cli.main([str(a) for a in argv], **kw)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_prints_one_line_for_the_junction(demo_video, tmp_path, capsys):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    (ws / "config.json").write_text('{"review": {"pre_seconds": 1.0, "post_seconds": 1.0}}')
    code, out, err = run_cli(capsys, ws, "--index", "1")
    assert code == 0 and err == ""
    lines = out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("B edl ") and " cut=1.00 duration=2.00 cut" in lines[0]
    assert str(ws / "review") in lines[0] and not lines[0].endswith("cached")
    code, out, _ = run_cli(capsys, ws, "--index", "1")
    assert out.strip().endswith("cut cached")


def test_cli_ab_prints_a_and_b_and_the_note(demo_video, tmp_path, capsys):
    ws = ab_workspace(tmp_path, demo_video)
    (ws / "config.json").write_text('{"review": {"pre_seconds": 1.0, "post_seconds": 1.0, "width": 160}}')
    code, out, _ = run_cli(capsys, ws, "--index", "1", "--ab")
    assert code == 0
    lines = out.strip().splitlines()
    assert lines[0].startswith("A edl ") and lines[1].startswith("B render ") and "dissolve 0.50s" in lines[1]
    code, out, _ = run_cli(capsys, ws, "--index", "1", "--ab", "--stage", "roughcut")
    assert code == 0 and "nothing earlier to compare" in out and out.startswith("B edl ")


def test_cli_reports_errors_on_stderr(demo_video, tmp_path, capsys):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    code, out, err = run_cli(capsys, ws, "--index", "9")
    assert code == 1 and out == "" and "junction 9 is out of range" in err
    code, _, err = run_cli(capsys, tmp_path / "empty", "--index", "1")
    assert code == 1 and err.strip()


@needs_mpv
def test_cli_play_toggles_pauses_and_quits(demo_video, tmp_path, capsys):
    ws = ab_workspace(tmp_path, demo_video)
    (ws / "config.json").write_text('{"review": {"pre_seconds": 1.0, "post_seconds": 1.0, "width": 160}}')
    made = []

    def factory(cfg):
        made.append(ReviewController(cfg, session_factory=headless))
        return made[0]

    code, out, err = run_cli(capsys, ws, "--index", "1", "--ab", "--play", stdin=io.StringIO("\n\ns\nq\n"), controller_factory=factory)
    assert code == 0, err
    assert out.count("playing A") == 2 and out.count("playing B") == 1 and "paused" in out
    assert made[0]._session is None


class Keys:
    """A stdin that runs ``before(n)`` ahead of handing out its n-th line (0-based)."""

    def __init__(self, lines, before):
        self.lines, self.before, self.n = list(lines), before, 0

    def readline(self):
        if self.n >= len(self.lines):
            return ""
        self.before(self.n)
        line = self.lines[self.n]
        self.n += 1
        return line


def test_cli_enter_after_the_mpv_window_was_closed_plays_the_pair_again(demo_video, tmp_path, capsys):
    ws = ab_workspace(tmp_path, demo_video)
    (ws / "config.json").write_text('{"review": {"pre_seconds": 1.0, "post_seconds": 1.0, "width": 160}}')
    sessions, controllers = [], []

    def make_session(binary):
        sessions.append(Fake(binary, {"time-pos": 1.0}))
        return sessions[-1]

    def factory(cfg):
        controllers.append(ReviewController(cfg, session_factory=make_session))
        return controllers[0]

    def before(n):
        if n == 1:
            sessions[0].up = False  # the editor closes the mpv window before the second Enter

    code, out, err = run_cli(capsys, ws, "--index", "1", "--ab", "--play", stdin=Keys(["\n", "\n", "\n", "q\n"], before), controller_factory=factory)
    assert code == 0, err
    assert "nothing to toggle" not in out
    played = [ln for ln in out.splitlines() if ln.startswith("playing ")]
    assert [ln.split()[1] for ln in played] == ["A", "B", "A", "B"]  # A, toggle to B; closed; Enter replays A; toggle to B
    assert len(sessions) == 2


def test_cli_enter_with_a_single_item_and_a_live_window_still_says_there_is_nothing_to_toggle(demo_video, tmp_path, capsys):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    controllers = []

    def factory(cfg):
        controllers.append(ReviewController(cfg, session_factory=Fake))
        return controllers[0]

    code, out, _ = run_cli(capsys, ws, "--index", "1", "--play", stdin=io.StringIO("\nq\n"), controller_factory=factory)
    assert code == 0 and "nothing to toggle" in out


def test_cli_a_malformed_timeline_prints_one_line_and_exits_1(demo_video, tmp_path, capsys, monkeypatch):
    ws = workspace(tmp_path, demo_video, final=two_shots())

    def boom(*a, **k):
        raise KeyError("shot_index")

    monkeypatch.setattr(cli, "prepare_review", boom)
    code, out, err = run_cli(capsys, ws, "--index", "1")
    assert (code, out, err) == (1, "", "review failed: KeyError: 'shot_index'\n")


def test_cli_ab_play_without_mpv_opens_the_ab_file_through_the_controller_opener(demo_video, tmp_path, capsys):
    ws = ab_workspace(tmp_path, demo_video)
    (ws / "config.json").write_text('{"review": {"pre_seconds": 1.0, "post_seconds": 1.0, "width": 160}}')
    opened = []

    def factory(cfg):
        return ReviewController(cfg, session_factory=Dead, opener=lambda p: (opened.append(p) or (True, f"opened {p.name} in the system player")))

    code, out, err = run_cli(capsys, ws, "--index", "1", "--ab", "--play", stdin=io.StringIO(""), controller_factory=factory)
    assert code == 0, err
    assert len(opened) == 1 and opened[0].name.startswith("ab_")
    assert out.strip().splitlines()[-1] == f"mpv not found: opened {opened[0].name} in the system player (no A/B toggle)"


@needs_mpv
def test_cli_play_closes_mpv_on_eof(demo_video, tmp_path, capsys):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    made = []

    def factory(cfg):
        made.append(ReviewController(cfg, session_factory=headless))
        return made[0]

    code, out, _ = run_cli(capsys, ws, "--index", "1", "--play", stdin=io.StringIO(""), controller_factory=factory)
    assert code == 0 and "playing final" in out
    assert made[0]._session is None


def test_cli_play_without_mpv_renders_and_opens_the_file(demo_video, tmp_path, capsys):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    (ws / "config.json").write_text('{"review": {"pre_seconds": 1.0, "post_seconds": 1.0, "width": 160}}')
    opened = []

    def factory(cfg):
        return ReviewController(cfg, session_factory=Dead, opener=lambda p: (opened.append(p) or (True, f"opened {p.name} in the system player")))

    code, out, err = run_cli(capsys, ws, "--index", "1", "--play", stdin=io.StringIO(""), controller_factory=factory)
    assert code == 0, err
    assert len(opened) == 1 and opened[0].suffix == ".mp4"
    assert "mpv not found: opened" in out and "(no A/B toggle)" in out
