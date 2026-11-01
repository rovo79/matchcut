"""Rendered review windows: the sub-timeline through the real render graph, trimmed to the window.

Used where an exact mpv EDL is not enough (transitions, fades, music) and for the A/B concat
of players without IPC. Files are written to ``<dir>/.tmp`` first and moved into place, so a
failed render is never reused.
"""
from __future__ import annotations

import dataclasses
import functools
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Dict, Optional, Sequence

from ..pipeline.config import PipelineConfig, RenderConfig
from ..pipeline.media import MediaError, MediaInfo, _require, run
from ..pipeline.render import render, snap_to_frames
from ..pipeline.timeline import Timeline
from .window import ReviewError, Window, music_start, sub_timeline

if TYPE_CHECKING:  # pragma: no cover
    from .items import ReviewItem

CUT_TAG_SECONDS = 0.3
AB_GAP_SECONDS = 0.5
# The music fade belongs to the end of the whole cut. A window that stops mid-cut gets a fade this short instead (1 ms is
# inaudible; afade with d=0 falls back to a default length, so "no fade" cannot be 0).
NO_FADE_SECONDS = 0.001
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._->")


@functools.lru_cache(maxsize=None)
def _has_drawtext(ffmpeg: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return False
    return any(line.split()[1:2] == ["drawtext"] for line in out.splitlines() if line.strip())


def _ffmpeg_error(e: Exception) -> ReviewError:
    from ..pipeline.render import RangeError

    if isinstance(e, RangeError):  # refused before ffmpeg started: say so as it is
        return ReviewError(str(e))
    text = str(e)
    if "not found on PATH" in text:
        return ReviewError("ffmpeg not found on PATH")
    lines = [ln.strip() for ln in text.splitlines()[1:] if ln.strip()]
    return ReviewError(f"ffmpeg failed: {lines[-1] if lines else text.splitlines()[0]}")


def _label_filter(label: str, cut_at: float) -> str:
    text = "".join(ch if ch in _SAFE else " " for ch in label)
    box = "fontcolor=white:fontsize=h/16:box=1:boxcolor=black@0.55:boxborderw=6"
    return (
        f"drawtext=text='{text}':x=12:y=12:{box},"
        f"drawtext=text='CUT':x=w-tw-12:y=12:{box}:enable='between(t,{cut_at:.3f},{cut_at + CUT_TAG_SECONDS:.3f})'"
    )


def _encode_tail(cfg: PipelineConfig, dst: Path, has_audio: bool) -> list:
    tail = []
    if has_audio:
        tail += ["-map", "[a]", "-c:a", "aac", "-b:a", cfg.render.audio_bitrate]
    tail += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cfg.review.crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]
    return tail


# Note: ``w`` must come from the frame-snapped stage timeline (items.py derives rendered windows from one), so the
# trim below lands on the cut exactly; snapping the sub-timeline again here is then a no-op.
def _trim_and_label(src: Path, dst: Path, w: Window, has_audio: bool, label: str, cfg: PipelineConfig) -> None:
    ffmpeg = _require("ffmpeg")
    v = f"[0:v]trim=start={w.offset:.4f}:duration={w.duration:.4f},setpts=PTS-STARTPTS"
    if label:
        v += "," + _label_filter(label, w.cut - w.start)
    graph = [v + "[v]"]
    if has_audio:
        graph.append(f"[0:a]atrim=start={w.offset:.4f}:duration={w.duration:.4f},asetpts=PTS-STARTPTS[a]")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(src), "-filter_complex", ";".join(graph), "-map", "[v]"]
    run(cmd + _encode_tail(cfg, dst, has_audio), capture=False)


def _audio_duration(path: str) -> Optional[float]:
    """Length of an audio file in seconds, or None when ffprobe cannot tell."""
    try:
        out = run([_require("ffprobe"), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
        return float(out.stdout.decode("utf-8", "replace").strip())
    except (MediaError, ValueError):
        return None


def _tmp(out_dir: Path, suffix: str) -> Path:
    tmp_dir = out_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=suffix, dir=tmp_dir)
    os.close(fd)
    return Path(name)


def _discard(*paths: Path) -> None:
    for f in paths:
        try:
            f.unlink()
        except OSError:
            pass


def render_window(
    tl: Timeline,
    w: Window,
    media: MediaInfo,
    cfg: PipelineConfig,
    out_path: Path,
    label: str = "",
    bounds: Optional[Dict[int, Sequence[float]]] = None,
) -> None:
    """Render the window of ``tl`` to ``out_path`` (mp4). ``label`` is burned in only when given."""
    out_path = Path(out_path)
    sub = snap_to_frames(sub_timeline(tl, w), bounds)
    music, music_duration = None, None
    if cfg.music.path:
        music = dataclasses.replace(cfg.music, start=music_start(tl, w, cfg.music.start))
        if w.last != len(tl.clips) - 1:  # the window stops mid-cut: the full cut has no fade here
            music = dataclasses.replace(music, fade_out=NO_FADE_SECONDS)
        music_duration = _audio_duration(cfg.music.path)  # as the full render: the music cannot run past its own end
    has_audio = media.has_audio or music is not None
    rcfg = RenderConfig(enabled=True, width=cfg.review.width or None, crf=cfg.review.crf, preset="veryfast", audio_bitrate=cfg.render.audio_bitrate)
    full, trimmed = _tmp(out_path.parent, ".full.mp4"), _tmp(out_path.parent, ".mp4")
    try:
        render(sub, media, rcfg, full, music=music, music_duration=music_duration)
        use_label = label if label and _has_drawtext(_require("ffmpeg")) else ""
        try:
            _trim_and_label(full, trimmed, w, has_audio, use_label, cfg)
        except MediaError:
            if not use_label:
                raise
            _trim_and_label(full, trimmed, w, has_audio, "", cfg)
        os.replace(trimmed, out_path)
    except MediaError as e:
        raise _ffmpeg_error(e) from e
    finally:
        _discard(full, trimmed)


def _ab_command(ffmpeg: str, a: "ReviewItem", b: "ReviewItem", dst: Path, has_audio: bool, cfg: PipelineConfig, labels: bool) -> list:
    gap = f"{AB_GAP_SECONDS:.3f}"

    def half(k: int, item: "ReviewItem") -> str:
        chain = _label_filter(item.label, item.cut) if labels and item.label else "null"
        return f"[{k}:v]{chain}[v{k}]"

    pad_a = f"[v0]tpad=stop_mode=add:stop_duration={gap}:color=black[va]"
    if has_audio:
        graph = ";".join([half(0, a), half(1, b), pad_a, f"[0:a]apad=pad_dur={gap}[aa]", "[va][aa][v1][1:a]concat=n=2:v=1:a=1[v][a]"])
    else:
        graph = ";".join([half(0, a), half(1, b), pad_a, "[va][v1]concat=n=2:v=1:a=0[v]"])
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(a.path), "-i", str(b.path), "-filter_complex", graph, "-map", "[v]"]
    return cmd + _encode_tail(cfg, dst, has_audio)


def render_ab(a: "ReviewItem", b: "ReviewItem", out_path: Path, has_audio: bool, cfg: PipelineConfig) -> None:
    """A, a short black gap, then B, with each half's label burned in. Both items must be rendered files."""
    out_path = Path(out_path)
    ffmpeg = _require("ffmpeg")
    labels = _has_drawtext(ffmpeg)
    tmp = _tmp(out_path.parent, ".mp4")
    try:
        try:
            run(_ab_command(ffmpeg, a, b, tmp, has_audio, cfg, labels), capture=False)
        except MediaError:
            if not labels:
                raise
            run(_ab_command(ffmpeg, a, b, tmp, has_audio, cfg, False), capture=False)
        os.replace(tmp, out_path)
    except MediaError as e:
        raise _ffmpeg_error(e) from e
    finally:
        _discard(tmp)
