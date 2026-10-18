"""The mpv session: a real headless mpv driven over JSON IPC (skipped without mpv)."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from matchcut.review.items import prepare_review
from matchcut.review import mpv as mpv_mod
from matchcut.review.mpv import MpvError, MpvSession

from .test_items import small_cfg, two_shots, workspace

pytestmark = pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")


@pytest.fixture
def session():
    s = MpvSession(headless=True)
    try:
        yield s
    finally:
        s.close()
        assert not s.alive()


@pytest.fixture
def edl_item(demo_video, tmp_path):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    return prepare_review(ws, small_cfg(1.5, 1.5), "final", 0).b


def test_a_headless_session_loads_an_edl_window_with_its_chapters(session, edl_item):
    session.start()
    assert session.alive()
    session.load(str(edl_item.path))
    assert abs(session.get("duration") - edl_item.duration) <= 0.05
    chapters = session.get("chapter-list")
    lines = [ln for ln in edl_item.path.read_text().splitlines() if ln and not ln.startswith("#")]
    assert len(chapters) == len(lines) == 2
    for ch, line in zip(chapters, lines):
        assert "title=" in line and ch["title"] == line.split("title=", 1)[1]
    assert abs(chapters[1]["time"] - edl_item.cut) <= 0.05  # the second chapter starts at the cut


def test_loop_file_wraps_the_window(session, edl_item):
    session.start()
    session.load(str(edl_item.path))
    session.set("loop-file", "inf")
    session.command("seek", edl_item.cut - 0.5, "absolute+exact")
    session.set("pause", False)
    time.sleep(edl_item.duration + 0.5)
    pos = session.get("time-pos")
    assert pos < edl_item.duration - 0.5  # without the loop it would sit at the end


def test_loading_a_missing_file_raises(session, tmp_path):
    session.start()
    with pytest.raises(MpvError, match="could not open"):
        session.load(str(tmp_path / "nothing.mp4"))
    assert session.alive()


def test_an_unknown_property_raises(session):
    session.start()
    with pytest.raises(MpvError, match="no-such-property"):
        session.get("no-such-property")


def test_close_twice_and_a_killed_process_never_hang(edl_item):
    s = MpvSession(headless=True)
    old = signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(AssertionError("hung")))
    signal.alarm(10)
    try:
        s.start()
        s.load(str(edl_item.path))
        os.kill(s._proc.pid, signal.SIGKILL)
        s._proc.wait(timeout=5)
        deadline = time.time() + 3
        while s.alive() and time.time() < deadline:
            time.sleep(0.02)
        assert s.alive() is False
        with pytest.raises(MpvError):
            s.command("get_property", "duration")
    finally:
        s.close()
        s.close()
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    assert not s.alive()


def test_a_session_starts_again_after_close_and_removes_its_socket_dir(edl_item):
    s = MpvSession(headless=True)
    try:
        s.start()
        sock_dir = os.path.dirname(s._sock_path)
        assert os.path.isdir(sock_dir)
        s.close()
        assert not os.path.exists(sock_dir)
        s.start()
        s.load(str(edl_item.path))
        assert s.alive()
    finally:
        s.close()
    assert not s.alive()


def test_a_missing_binary_raises_mpv_error():
    s = MpvSession("matchcut-no-such-mpv", headless=True)
    with pytest.raises(MpvError, match="not found"):
        s.start()
    assert not s.alive()


def test_an_idle_session_stays_alive_and_answers_after_the_command_timeout(session):
    session.start()
    assert session.get("mpv-version")
    time.sleep(2.5)  # longer than any command timeout: the reader must not give up on a silent socket
    assert session.alive()
    assert session.get("mpv-version")


def test_a_late_reply_is_dropped(session):
    session.start()
    for _ in range(20):
        try:
            session.command("get_property", "mpv-version", timeout=0.0005)
        except MpvError:
            pass
    time.sleep(0.5)
    assert session._replies == {} and session._pending == set()


def test_close_without_a_usable_socket_kills_at_once(edl_item):
    s = MpvSession(headless=True)
    s.start()
    s._eof = True  # as if the reader had lost the connection while mpv lives on
    t0 = time.monotonic()
    s.close()
    assert time.monotonic() - t0 < 1.5 and not s.alive()
    assert s._proc is None


def test_the_session_args_do_not_resume_playback_and_seek_exactly(monkeypatch):
    seen = []

    def fake_popen(cmd, **kw):
        seen.append(cmd)
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(mpv_mod.subprocess, "Popen", fake_popen)
    with pytest.raises(MpvError, match="not found"):
        MpvSession("mpv", headless=True).start()
    assert "--no-resume-playback" in seen[0] and "--hr-seek=yes" in seen[0]
    assert "--no-config" not in seen[0]  # the editor's own mpv.conf and scripts are honoured


def test_the_death_signal_hook_is_linux_only_and_never_raises(monkeypatch):
    import ctypes

    monkeypatch.setattr(sys, "platform", "darwin")
    assert mpv_mod._death_signal_hook() is None
    monkeypatch.setattr(sys, "platform", "linux")

    def broken(*a, **k):
        raise OSError("no libc")

    monkeypatch.setattr(ctypes, "CDLL", broken)
    assert mpv_mod._death_signal_hook() is None


def _gone(pid):
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return True
    return state in ("Z", "X")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux only")
def test_mpv_dies_with_the_app_even_when_the_app_is_killed(tmp_path):
    root = Path(__file__).resolve().parents[2]
    code = (
        "import time\n"
        "from matchcut.review.mpv import MpvSession\n"
        "s = MpvSession(headless=True)\n"
        "s.start()\n"
        "print(s._proc.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    app = subprocess.Popen([sys.executable, "-c", code], cwd=str(root), env={**os.environ, "PYTHONPATH": str(root)}, stdout=subprocess.PIPE, text=True)
    pid = 0
    try:
        pid = int(app.stdout.readline())
        assert not _gone(pid)
        app.kill()  # SIGKILL: no atexit, no close()
        app.wait(timeout=5)
        deadline = time.time() + 5
        while not _gone(pid) and time.time() < deadline:
            time.sleep(0.05)
        assert _gone(pid), "mpv outlived the app that started it"
    finally:
        app.kill()
        app.wait(timeout=5)
        if pid and not _gone(pid):
            os.kill(pid, signal.SIGKILL)


def test_mpv_started_from_a_worker_thread_survives_the_thread(edl_item):
    """PDEATHSIG fires when the *thread* that started mpv exits (the TUI starts it from worker threads)."""
    import threading

    s = MpvSession(headless=True)
    try:
        t = threading.Thread(target=s.start)
        t.start()
        t.join()
        time.sleep(0.5)
        assert s.alive()
        s.load(str(edl_item.path))
        assert s.get("duration") > 0
    finally:
        s.close()
    assert not s.alive()
