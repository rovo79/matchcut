"""Headless demonstration of the M2b review: the real controller drives a real mpv that writes the frames it displays.

For each junction the tool prepares the A/B pair, plays A in mpv (``--vo=image``, so every frame mpv displays is
saved as an image) until just after A's cut, toggles to B at the same offset from the cut, plays B to the end of its window, and builds one contact sheet from those frames:
the top half is A, the bottom half is B. Nothing is shown on screen and there is no sound; the sheet proves what
mpv displayed, not how it looks or feels in a window (that is the human checklist in docs/M2B-ACCEPTANCE.md).

    python3 tools/demo_review.py WORKSPACE OUTDIR --index 1 --index 2 [--stage final] [--seconds 2.2]
    python3 tools/demo_review.py WORKSPACE OUTDIR --index 1 --svg m2b-edit-review.svg
    python3 tools/demo_review.py WORKSPACE OUTDIR --make-synthetic --index 3 --index 4   # builds WORKSPACE/ws first, then uses it

``--svg`` also saves the Edit tab after ``p``: the real controller with a stub mpv session, so the status line is
the controller's own text but nothing is played.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matchcut.pipeline.config import PipelineConfig  # noqa: E402
from matchcut.review.controller import ReviewController  # noqa: E402
from matchcut.review.items import ReviewItem, prepare_review  # noqa: E402
from matchcut.review.mpv import MpvError, MpvSession  # noqa: E402
from matchcut.review.window import ReviewError  # noqa: E402

MAX_TILES = 24  # per sheet: 12 for A and 12 for B
COLUMNS = 6
TILE_WIDTH = 210
CAPTION_HEIGHT = 30
# With mpv 0.37 the vo=image's own yuv-to-rgb conversion failed ("zimg initialization failed") and then crashed mpv
# (SIGSEGV) on small footage at the loop restart; converting in a filter before the VO avoids it.
VIDEO_ARGS = ["--vo=image", "--vo-image-format=jpg", "--ao=null", "--vf=format=rgb24"]


def _load_cfg(ws: Path) -> PipelineConfig:
    path = ws / "config.json"
    cfg = PipelineConfig.load(path) if path.exists() else PipelineConfig()
    cfg.workspace = str(ws)
    return cfg


def make_synthetic_workspace(ws: Path) -> None:
    """A mock-backend workspace over the synthetic demo footage, for a junction with a dissolve and one hard cut played exactly.

    Fades are off and the review window is 1.0 s before / 1.5 s after the cut, so the last junction (a hard cut) is an EDL on
    both sides; the junction before it (a 0.75 s dissolve in the final cut) is rendered.
    """
    from matchcut.pipeline.demo import make_demo_footage
    from matchcut.pipeline.pipeline import Pipeline

    ws.mkdir(parents=True, exist_ok=True)
    video = make_demo_footage(ws / "demo.mp4")
    cfg = PipelineConfig(source=str(video), workspace=str(ws / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    cfg.transitions.fade_in_seconds = 0.0
    cfg.transitions.fade_out_seconds = 0.0
    cfg.review.pre_seconds = 1.0
    cfg.review.post_seconds = 1.5
    Pipeline(cfg).run()


def _frames(frames_dir: Path) -> List[Path]:
    return sorted(frames_dir.glob("*.jpg"))


def _ffmpeg(args: Sequence[str]) -> None:
    exe = shutil.which("ffmpeg")
    if exe is None:
        raise ReviewError("ffmpeg not found on PATH")
    done = subprocess.run([exe, "-v", "error", "-nostdin", "-y", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if done.returncode != 0:
        lines = [ln for ln in done.stderr.splitlines() if ln.strip()]
        raise ReviewError(f"ffmpeg failed: {lines[-1] if lines else done.returncode}")


def _has_drawtext() -> bool:
    exe = shutil.which("ffmpeg")
    if exe is None:
        return False
    done = subprocess.run([exe, "-hide_banner", "-filters"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
    return " drawtext " in done.stdout


def caption_text(item: ReviewItem, tag: str, frames: int, extra: str) -> str:
    label = item.label.split("  ", 1)[-1] if item.label[:3] in ("A  ", "B  ") else item.label
    return f"{tag}: {label}  [{item.kind}, cut at {item.cut:.2f} s of {item.duration:.2f} s; {frames} frames shown, {{tiled}} tiled{extra}]"


def build_strip(frames_dir: Path, first: int, count: int, caption: str, out: Path, scratch: Path, drawtext: bool) -> int:
    """One half of a sheet: ``count`` frames from number ``first`` (1-based), every Nth, tiled, with a caption bar. Returns the tiles."""
    step = max(1, math.ceil(count / (MAX_TILES // 2)))
    tiles = len(range(0, count, step))
    rows = max(1, math.ceil(tiles / COLUMNS))
    vf = f"trim=end_frame={count},select='not(mod(n\\,{step}))',scale={TILE_WIDTH}:-2,tile={COLUMNS}x{rows}:padding=2:color=black"
    vf += f",pad=iw:ih+{CAPTION_HEIGHT}:0:{CAPTION_HEIGHT}:color=0x202020"
    if drawtext:
        text = scratch / (out.stem + ".txt")
        text.write_text(caption.format(tiled="every frame" if step == 1 else f"1 in {step} frames"), encoding="utf-8")
        vf += f",drawtext=textfile={text}:fontcolor=white:fontsize=15:x=8:y=7"
    _ffmpeg(["-framerate", "24", "-start_number", str(first), "-i", str(frames_dir / "%08d.jpg"), "-vf", vf, "-frames:v", "1", str(out)])
    return tiles


class Capture:
    """What one attempt of playing A then toggling to B left in the frames directory."""

    def __init__(self) -> None:
        self.n_a = 0  # frames 1..n_a were displayed while A played
        self.n_load = 0  # frames written while B loaded and seeked (the start of the file, before the seek): dropped
        self.n_b = 0  # frames n_a + n_load + 1 .. are B after the seek
        self.a_pos = 0.0
        self.b_pos = 0.0
        self.status_a = ""
        self.status_b = ""


def _play_once(cfg: PipelineConfig, pair: Any, frames_dir: Path, a_seconds: float) -> Capture:
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir(parents=True)
    session: List[Any] = []

    def factory(binary: str) -> MpvSession:
        s = MpvSession(binary, headless=True, video_args=VIDEO_ARGS + [f"--vo-image-outdir={frames_dir}"])
        session.append(s)
        return s

    cap = Capture()
    controller = ReviewController(cfg, session_factory=factory)
    try:
        if not controller.has_mpv():
            raise ReviewError("mpv not found or did not start")
        controller.play_pair(pair)
        cap.status_a = controller.status
        time.sleep(a_seconds)
        cap.n_a = len(_frames(frames_dir))
        cap.a_pos = float(session[0].get("time-pos"))
        if not controller.toggle():
            raise ReviewError("toggle did nothing")
        cap.status_b = controller.status
        after_toggle = len(_frames(frames_dir))
        cap.n_load = after_toggle - cap.n_a
        cap.b_pos = float(session[0].get("time-pos"))
        time.sleep(max(0.3, pair.b.duration - cap.b_pos - 0.15))  # to the end of B's window, short of the loop restart
        fps = float(session[0].get("container-fps") or 24)
        # frames past the end of B's window are the loop restarting (the sleep can overshoot by a frame): not part of the window
        cap.n_b = min(len(_frames(frames_dir)) - after_toggle, int((pair.b.duration - cap.b_pos) * fps))
        session[0].get("time-pos")  # raises if mpv died meanwhile
    finally:
        controller.close()
    return cap


def demo_junction(ws: Path, cfg: PipelineConfig, index: int, stage: Optional[str], out_dir: Path, seconds: Optional[float]) -> Tuple[Path, str]:
    """Play A until just after its cut, toggle to B at the same offset and play B to the end of its window; build ``review-j<index>.jpg``."""
    pair = prepare_review(ws, cfg, stage, index - 1, ab=True)
    if pair.a is None:
        raise ReviewError(pair.note or "no A to compare")
    work = out_dir / f"work-j{index}"
    frames_dir = work / "frames"
    work.mkdir(parents=True, exist_ok=True)
    a_seconds = seconds if seconds is not None else min(pair.a.cut + 0.15, pair.a.duration - 0.2)
    cap: Optional[Capture] = None
    for attempt in range(1, 4):  # this mpv build's vo=image occasionally aborts at a file start or loop: try again
        try:
            cap = _play_once(cfg, pair, frames_dir, a_seconds)
            break
        except (MpvError, ReviewError) as e:  # a crashed session surfaces as either, depending on which call met it
            print(f"  attempt {attempt}: mpv stopped ({e})")
    if cap is None:
        raise ReviewError("mpv kept stopping under vo=image; no frames to show")
    print(f"  {cap.status_a}")
    offset = cap.a_pos - pair.a.cut
    print(f"  toggled at A time-pos {cap.a_pos:.3f} s ({offset:+.3f} s from the cut); B then read time-pos {cap.b_pos:.3f} s ({cap.b_pos - pair.b.cut:+.3f} s from its cut): {cap.status_b}")
    drawtext = _has_drawtext()
    sheet = out_dir / f"review-j{index}.jpg"
    strip_a, strip_b = work / "a.png", work / "b.png"
    ta = build_strip(frames_dir, 1, cap.n_a, caption_text(pair.a, "A", cap.n_a, f"; played from the start to {offset:+.2f} s from the cut"), strip_a, work, drawtext)
    first_b = cap.n_a + cap.n_load + 1
    tb = build_strip(frames_dir, first_b, cap.n_b, caption_text(pair.b, "B", cap.n_b, f"; from {cap.b_pos - pair.b.cut:+.2f} s from the cut to the end of the window"), strip_b, work, drawtext)
    _ffmpeg(["-i", str(strip_a), "-i", str(strip_b), "-filter_complex", "[0:v][1:v]vstack=inputs=2", "-q:v", "5", "-frames:v", "1", str(sheet)])
    summary = (
        f"  A: {pair.a.kind} {pair.a.transition}, {cap.n_a} frames ({ta} tiles); B: {pair.b.kind} {pair.b.transition}, {cap.n_b} frames ({tb} tiles), "
        f"{cap.n_load} frames written while B loaded and seeked were dropped; caption text {'drawn' if drawtext else 'skipped (no drawtext filter)'}"
    )
    print(summary)
    print(f"  {sheet}")
    shutil.rmtree(work, ignore_errors=True)  # the captured frames and strips are scratch; only the sheet is kept
    return sheet, summary


class StubSession:
    """Stands in for mpv so the Edit tab can show the controller's own status line without playing anything."""

    def __init__(self, binary: str = "mpv"):
        self._alive = False

    def start(self) -> None:
        self._alive = True

    def alive(self) -> bool:
        return self._alive

    def close(self) -> None:
        self._alive = False

    def load(self, path: Any, timeout: float = 10.0) -> None:
        pass

    def set(self, name: str, value: Any, timeout: float = 2.0) -> None:
        pass

    def get(self, name: str, timeout: float = 2.0) -> Any:
        return 0.0

    def command(self, *args: Any, timeout: float = 2.0) -> Any:
        return None


async def capture_edit_svg(ws: Path, cfg: PipelineConfig, out: Path, keys: Sequence[str], size: Tuple[int, int] = (140, 46)) -> None:
    from matchcut.tui.app import MatchcutApp
    from matchcut.tui.views.edit import FRAME_IDS, EditView
    from matchcut.tui.widgets.frame import Frame

    app = MatchcutApp(cfg)
    app._review = ReviewController(cfg, session_factory=StubSession)
    async with app.run_test(size=size) as pilot:
        await pilot.press("4")
        view = app.query_one(EditView)
        for _ in range(400):
            if view.junctions and all(app.query_one(f"#{i}", Frame).image_mode for i in FRAME_IDS):
                break
            await pilot.pause(0.05)
        if not view.junctions:
            raise ReviewError(f"no junctions in {ws}")
        for key in keys:
            await pilot.press(key)
        for _ in range(400):  # preparation runs in a thread worker
            if str(app.query_one("#review-status").content).startswith("playing"):
                break
            await pilot.pause(0.05)
        await pilot.pause(0.3)
        print(f"  status line: {app.query_one('#review-status').content}")
        out.parent.mkdir(parents=True, exist_ok=True)
        app.save_screenshot(filename=out.name, path=str(out.parent))


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("workspace")
    p.add_argument("outdir")
    p.add_argument("--index", type=int, action="append", help="junction number, 1-based (repeatable)")
    p.add_argument("--make-synthetic", action="store_true", help="build the synthetic demo workspace at WORKSPACE/ws first (WORKSPACE/demo.mp4 is the footage)")
    p.add_argument("--stage", default=None, help="stage to inspect (default: the last one)")
    p.add_argument("--seconds", type=float, default=None, help="seconds A plays before the toggle (default: until 0.15 s after its cut; keep it below A's duration, a longer run loops and may crash vo=image); B always plays to the end of its window")
    p.add_argument("--svg", default=None, help="also save the Edit tab after p (junction 1 selected) under this name in OUTDIR")
    a = p.parse_args(argv)
    ws, out_dir = Path(a.workspace), Path(a.outdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        if a.make_synthetic:
            make_synthetic_workspace(ws)
            ws = ws / "ws"
            print(f"synthetic workspace: {ws}")
        cfg = _load_cfg(ws)
        for index in a.index or []:
            print(f"junction {index}:")
            demo_junction(ws, cfg, index, a.stage, out_dir, a.seconds)
        if a.svg:
            print("edit tab:")
            asyncio.run(capture_edit_svg(ws, cfg, out_dir / a.svg, ["p"]))
            print(f"  {out_dir / a.svg}")
    except ReviewError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
