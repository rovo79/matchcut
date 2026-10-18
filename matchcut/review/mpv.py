"""An mpv process and a stdlib client for its JSON IPC (one JSON object per line over a Unix socket).

Replies are matched to requests by an integer ``request_id``; events are kept (bounded) for ``load``
to wait on. No call blocks without a timeout, and ``close`` always ends the process.
"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Sequence


class MpvError(RuntimeError):
    """mpv is missing, not answering, or refused a command; the message is one line."""


class MpvSession:
    def __init__(self, binary: str = "mpv", headless: bool = False, video_args: Optional[Sequence[str]] = None):
        self.binary = binary
        if video_args is None:
            video_args = ["--vo=null", "--ao=null"] if headless else ["--force-window=yes"]
        self.video_args: List[str] = list(video_args)
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()
        self._proc: Optional["subprocess.Popen[bytes]"] = None
        self._sock: Optional[socket.socket] = None
        self._sock_path = ""
        self._reader: Optional[threading.Thread] = None
        self._replies: Dict[int, Dict[str, Any]] = {}
        self._events: Deque[Dict[str, Any]] = deque(maxlen=2000)
        self._next_id = 0
        self._eof = True  # no connection yet
        self._closing = False

    # -- lifecycle ----------------------------------------------------------------------------------

    def start(self) -> None:
        if self.alive():
            return
        self.close()
        if sys.platform == "win32":
            raise MpvError("mpv control is not supported on Windows yet")
        tmp = tempfile.mkdtemp(prefix="matchcut-mpv-")
        self._sock_path = os.path.join(tmp, "ipc.sock")
        cmd = [self.binary, "--idle=yes", "--keep-open=yes", "--no-terminal", f"--input-ipc-server={self._sock_path}", *self.video_args]
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._cleanup()
            raise MpvError(f"mpv not found: cannot run {self.binary} ({e.strerror or e})") from e
        atexit.register(self.close)
        try:
            self._connect()
        except MpvError:
            self.close()
            raise
        with self._cond:
            self._replies.clear()
            self._events.clear()
            self._eof = False
        self._reader = threading.Thread(target=self._read_loop, args=(self._sock,), name="mpv-ipc", daemon=True)
        self._reader.start()

    def _connect(self) -> None:
        deadline = time.monotonic() + 5.0
        while True:
            if self._proc is None or self._proc.poll() is not None:
                raise MpvError("mpv exited while starting")
            if os.path.exists(self._sock_path):
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s.settimeout(1.0)
                    s.connect(self._sock_path)
                    s.settimeout(None)
                    self._sock = s
                    return
                except OSError:
                    s.close()
            if time.monotonic() > deadline:
                raise MpvError("mpv did not open its control socket")
            time.sleep(0.02)

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and not self._eof

    def close(self) -> None:
        """Quit mpv (kill after 2 s) and remove the socket directory; safe to call any number of times."""
        self._closing = True
        try:
            proc, sock, reader = self._proc, self._sock, self._reader
            if proc is not None and proc.poll() is None:
                if sock is not None and not self._eof:
                    try:
                        with self._send_lock:
                            sock.settimeout(0.5)
                            sock.sendall(b'{"command": ["quit"]}\n')
                    except OSError:
                        pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=1.0)
            with self._cond:
                self._eof = True
                self._cond.notify_all()
            self._proc = self._sock = self._reader = None
            self._cleanup()
            try:
                atexit.unregister(self.close)
            except Exception:
                pass
        finally:
            self._closing = False

    def _cleanup(self) -> None:
        if self._sock_path:
            shutil.rmtree(os.path.dirname(self._sock_path), ignore_errors=True)
            self._sock_path = ""

    # -- protocol -----------------------------------------------------------------------------------

    def _read_loop(self, sock: Optional[socket.socket]) -> None:
        buf = b""
        try:
            while sock is not None:
                data = sock.recv(65536)
                if not data:
                    break
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        msg = json.loads(line.decode("utf-8", "replace"))
                    except ValueError:
                        continue
                    if not isinstance(msg, dict):
                        continue
                    with self._cond:
                        if "event" in msg:
                            self._events.append(msg)
                        elif isinstance(msg.get("request_id"), int):
                            self._replies[msg["request_id"]] = msg
                        self._cond.notify_all()
        except OSError:
            pass
        finally:
            with self._cond:
                self._eof = True
                self._cond.notify_all()

    def command(self, *args: Any, timeout: float = 2.0) -> Any:
        """Send one command and return its ``data``; raises ``MpvError`` on an error reply or no reply in time."""
        name = str(args[0]) if args else "(empty)"
        if not self.alive() or self._sock is None:
            raise MpvError(f"{name}: mpv is not running")
        with self._cond:
            self._next_id += 1
            rid = self._next_id
        payload = json.dumps({"command": list(args), "request_id": rid}).encode("utf-8") + b"\n"
        try:
            with self._send_lock:
                self._sock.settimeout(timeout)
                self._sock.sendall(payload)
        except (OSError, AttributeError) as e:
            raise MpvError(f"{name}: cannot reach mpv ({e})") from e
        deadline = time.monotonic() + timeout
        with self._cond:
            while rid not in self._replies:
                left = deadline - time.monotonic()
                if self._eof:
                    raise MpvError(f"{name}: mpv has exited")
                if left <= 0:
                    raise MpvError(f"{name}: no reply from mpv")
                self._cond.wait(left)
            reply = self._replies.pop(rid)
        if reply.get("error") != "success":
            detail = args[1] if name in ("get_property", "set_property") and len(args) > 1 else None
            raise MpvError(f"{name}{' ' + str(detail) if detail else ''}: {reply.get('error')}")
        return reply.get("data")

    def get(self, name: str, timeout: float = 2.0) -> Any:
        return self.command("get_property", name, timeout=timeout)

    def set(self, name: str, value: Any, timeout: float = 2.0) -> None:
        self.command("set_property", name, value, timeout=timeout)

    def _wait_event(self, names: Sequence[str], timeout: float, error_on_end: bool = False) -> Optional[Dict[str, Any]]:
        """Consume events until one of ``names`` (or, with ``error_on_end``, an ``end-file`` error); None on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                while self._events:
                    ev = self._events.popleft()
                    if ev.get("event") in names:
                        return ev
                    if error_on_end and ev.get("event") == "end-file" and ev.get("reason") == "error":
                        return ev
                left = deadline - time.monotonic()
                if self._eof:
                    raise MpvError("mpv has exited")
                if left <= 0:
                    return None
                self._cond.wait(left)

    def clear_events(self) -> None:
        with self._cond:
            self._events.clear()

    def wait_restart(self, timeout: float = 1.0) -> bool:
        """Wait for the ``playback-restart`` that follows a seek (so ``time-pos`` is the new position)."""
        try:
            return self._wait_event(("playback-restart",), timeout) is not None
        except MpvError:
            return False

    def load(self, path: Any, timeout: float = 10.0) -> None:
        """Load a file (replacing the current one) and wait until mpv has opened it."""
        path = str(path)
        self.clear_events()
        self.command("loadfile", path, "replace")
        ev = self._wait_event(("file-loaded",), timeout, error_on_end=True)
        if ev is None:
            raise MpvError(f"mpv did not open {path} in time")
        if ev.get("event") == "end-file":
            raise MpvError(f"mpv could not open {path}")
