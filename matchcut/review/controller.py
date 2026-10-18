"""The review controller: play a junction item looping in mpv, toggle A/B at the same offset from the cut.

Without mpv a rendered item is handed to the system opener instead (no A/B toggle). The controller never
renders: callers prepare rendered items (``prepare_review(..., force_render=True)``) when ``has_mpv()`` is False.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

from ..pipeline.config import PipelineConfig
from .items import ReviewItem, ReviewPair
from .mpv import MpvError, MpvSession
from .window import ReviewError

_SEEK_MARGIN = 0.05
_LABEL_MS = 2000
RETRY_SECONDS = 30.0  # a failed mpv start is tried again after this long (or at once when ``review.mpv`` changes)


def open_with_system(path: Path) -> Tuple[bool, str]:
    """Open ``path`` with the platform's default player, detached. Returns (opened, message)."""
    path = Path(path)
    missing = f"no player found: the file is {path}"
    try:
        if sys.platform == "win32":
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            tool = "open" if sys.platform == "darwin" else "xdg-open"
            if shutil.which(tool) is None:
                return False, missing
            subprocess.Popen(
                [tool, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
            )
    except OSError:
        return False, missing
    return True, f"opened {path.name} in the system player"


class ReviewController:
    def __init__(
        self,
        cfg: PipelineConfig,
        session_factory: Callable[[str], Any] = MpvSession,
        opener: Callable[[Path], Tuple[bool, str]] = open_with_system,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg
        self._factory = session_factory
        self._opener = opener
        self._lock = threading.Lock()
        self._session: Any = None
        self._clock = clock
        self._failed_at: Optional[float] = None  # when starting mpv last failed, and with which binary
        self._failed_mpv = ""
        self.status: str = ""
        self.current: Optional[ReviewItem] = None
        self.pair: Optional[ReviewPair] = None  # the pair last given to play_pair; None after play() of a single item

    # -- mpv ----------------------------------------------------------------------------------------

    def has_mpv(self) -> bool:
        """True when an mpv session is running (started on first use); False when mpv is missing or fails to start."""
        with self._lock:
            return self._ensure() is not None

    def _ensure(self) -> Any:
        s = self._session
        if s is not None and s.alive():
            return s
        if s is not None:  # the editor closed the mpv window: start a new one on next use
            self._drop()
        if self._failed_at is not None:
            if self.cfg.review.mpv == self._failed_mpv and self._clock() - self._failed_at < RETRY_SECONDS:
                return None
            self._failed_at = None  # the setting changed, or it has been a while: try again
        try:
            s = self._factory(self.cfg.review.mpv)
            s.start()
        except Exception:
            self._failed_at, self._failed_mpv = self._clock(), self.cfg.review.mpv
            try:
                s.close()  # type: ignore[union-attr]
            except Exception:
                pass
            return None
        self._session = s
        return s

    def _drop(self) -> None:
        s, self._session = self._session, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

    # -- playback -----------------------------------------------------------------------------------

    def play(self, item: ReviewItem, from_cut: Optional[float] = None) -> None:
        with self._lock:
            self._play(item, from_cut)
            self.pair = None

    def play_pair(self, pair: ReviewPair) -> None:
        with self._lock:
            self._play(pair.a if pair.a is not None else pair.b, None)
            self.pair = pair

    def _play(self, item: ReviewItem, from_cut: Optional[float]) -> None:
        s = self._ensure()
        if s is None:
            self._open(item)
            return
        try:
            self._play_in(s, item, from_cut)
        except MpvError as e:
            if s.alive():
                raise ReviewError(str(e)) from e
            self._drop()  # mpv died under us: one retry with a fresh process
            s = self._ensure()
            if s is None:
                self._open(item)
                return
            try:
                self._play_in(s, item, from_cut)
            except MpvError as e2:
                raise ReviewError(str(e2)) from e2

    def _play_in(self, s: Any, item: ReviewItem, from_cut: Optional[float]) -> None:
        target = item.cut + from_cut if from_cut is not None else 0
        if from_cut is not None:
            target = min(max(target, 0), max(item.duration - _SEEK_MARGIN, 0))
        # Open the file AT the target: ``start`` is read when the file opens, so no frame before it is ever shown
        # (loading and then seeking flashed the start of the window). It is a global option, so it is reset
        # afterwards and a looping file wraps to 0. A per-file loadfile option would do the same, but the loadfile
        # argument position changed in mpv 0.38.
        s.set("start", f"{target:.4f}")
        try:
            s.load(str(item.path))
        finally:
            try:
                s.set("start", "none")
            except MpvError:
                pass
        s.set("loop-file", "inf")
        wait = getattr(s, "wait_restart", None)
        if wait is not None:
            wait(1.0)  # playback restarted at the target: time-pos already reads it
        s.set("pause", False)
        s.command("show-text", item.label.replace("$", "$$"), _LABEL_MS)
        how = "exact source" if item.kind == "edl" else "rendered"
        self.current = item
        self.status = f"playing {item.label} ({how}{', cached' if item.cached else ''})"

    def _open(self, item: ReviewItem) -> None:
        if item.kind == "edl":
            raise ReviewError("cannot play an EDL window without mpv: prepare it with force_render")
        self.open_file(item.path)
        self.current = item

    def open_file(self, path: Path) -> None:
        """Hand a file to the system opener (no mpv); the status says what happened."""
        ok, message = self._opener(Path(path))
        self.status = f"mpv not found: {message}" + (" (no A/B toggle)" if ok else "")

    def toggle(self) -> bool:
        """Switch between A and B at the same offset from the cut; False (status unchanged) when there is nothing to toggle."""
        with self._lock:
            pair, cur, s = self.pair, self.current, self._session
            if pair is None or pair.a is None or cur is None or s is None or not s.alive():
                return False
            other = pair.b if cur is pair.a else pair.a
            known = True
            try:
                offset = float(s.get("time-pos")) - cur.cut
            except (MpvError, TypeError, ValueError):
                offset, known = 0.0, False
            self._play(other, offset)
            if not known:
                self.status += " (position unknown: started at the cut)"
            return True

    def running(self) -> bool:
        """True while an mpv window is open (does not start one)."""
        with self._lock:
            return self._session is not None and self._session.alive()

    def stop(self) -> bool:
        """Pause; False (and a status saying so) when the mpv window is closed, so no stale ``playing ...`` is left."""
        with self._lock:
            s = self._session
            if s is None or not s.alive():
                self.status = "mpv is not running"
                return False
            try:
                s.set("pause", True)
            except MpvError as e:
                raise ReviewError(str(e)) from e
            self.status = "paused"
            return True

    def close(self) -> None:
        with self._lock:
            self._drop()
