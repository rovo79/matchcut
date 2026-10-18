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


def test_play_loads_loops_seeks_unpauses_and_labels_in_order():
    made = []

    def factory(binary):
        made.append(Fake(binary))
        return made[-1]

    c = ReviewController(small_cfg(), session_factory=factory)
    try:
        it = item(cut=1.0, duration=2.0, label="B $x")
        c.play(it, from_cut=0.3)
        assert made[0].calls == [
            ("load", str(it.path)), ("set", "loop-file", "inf"),
            ("seek", 1.3, "absolute+exact"), ("set", "pause", False), ("show-text", "B $$x", 2000),
        ]
        c.play(it, from_cut=5.0)  # clamped to the end of the item
        assert ("seek", 1.95, "absolute+exact") in made[0].calls
        c.play(it, from_cut=-5.0)
        assert ("seek", 0, "absolute+exact") in made[0].calls
        c.play(it)
        assert made[0].calls[-3] == ("seek", 0, "absolute+exact")
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
        assert ("seek", pytest.approx(1.8), "absolute+exact") in made[0].calls
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
