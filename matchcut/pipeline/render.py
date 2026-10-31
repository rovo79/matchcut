"""Render a timeline to a video file with ffmpeg (cuts, dissolves, dips, fades).

Every clip reads the source through its own ffmpeg input, seeked to just before the clip (``clip_inputs``). With one shared input,
clips that play out of source order (or repeat a range) made ffmpeg buffer, starve or stall the branches it could not feed yet: the
audio of a reordered dissolve came out empty, a reordered cut under music came out short, a repeated range never finished, and a
reordered video held seconds of raw frames in memory.  Each input decodes only its clip (plus ``SEEK_MARGIN_SECONDS`` before it),
and the trims stay exact: they are applied to the decoded frames, relative to the seek point.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import MusicConfig, RenderConfig
from .media import MediaInfo, _require, run
from .timeline import Timeline

log = logging.getLogger("matchcut.pipeline")

SEEK_MARGIN_SECONDS = 1.0  # decoded before each clip's in-point, so the input seek (to a keyframe) never decides the first frame
READ_MARGIN_SECONDS = 0.5  # read past each clip's out-point
# Video trims start and end this much early.  Points are stored to 4 decimals, so at 29.97 fps an in-point can land up to 0.05 ms after
# its own frame's timestamp, and an exact trim then starts a frame late.  It must stay far below any stream's offset from the file's
# start (a video stream that starts 22 ms after its audio is common): half a frame would pick the previous frame there.
TRIM_EPSILON_SECONDS = 0.0005
SINGLE_THREAD_DECODE_ABOVE = 8  # more clips than this: one decoder thread per input (measured: memory grows ~20 MB per 720p input otherwise)


@dataclass(frozen=True)
class ClipInput:
    """Where clip ``index`` reads the source: ``-ss seek -t length -i source``; its in/out points relative to ``seek``."""

    index: int
    seek: float
    length: float
    start: float
    end: float


def clip_inputs(tl: Timeline, fps: float) -> List[ClipInput]:
    """One input per clip.  ``seek`` is on the frame grid, ``SEEK_MARGIN_SECONDS`` before the in-point (or 0)."""
    out: List[ClipInput] = []
    for i, c in enumerate(tl.clips):
        seek = max(0.0, math.floor((c.source_in - SEEK_MARGIN_SECONDS) * fps + 1e-6) / fps) if fps > 0 else max(0.0, c.source_in - SEEK_MARGIN_SECONDS)
        seek = round(seek, 6)
        out.append(ClipInput(i, seek, round(c.source_out - seek + READ_MARGIN_SECONDS, 6), round(c.source_in - seek, 4), round(c.source_out - seek, 4)))
    return out


def snap_to_frames(tl: Timeline, bounds: Optional[Dict[int, Sequence[float]]] = None) -> Timeline:
    """Round in/out points to the source frame grid (keeps the EDL and render in step)."""
    return tl.snap_to_frames(bounds)


def build_filter_graph(
    tl: Timeline,
    media: MediaInfo,
    cfg: RenderConfig,
    music: Optional[MusicConfig] = None,
    music_duration: Optional[float] = None,
) -> str:
    """ffmpeg -filter_complex graph assembling the timeline: clip i from input i (see ``clip_inputs``), the music from input ``len(clips)``.

    Transitions are rendered on whole frames (``Timeline.snap_transitions``, the interchange exporters' layout), so a stage saved with a
    fractional transition renders the frames its export shows; the stage itself is not changed.  Times are written to the microsecond
    against the exact frame rate, and each transition starts ``TRIM_EPSILON_SECONDS`` before its first frame's time, so it cannot begin
    a frame late at 29.97 fps."""
    tl = tl.snap_transitions()
    fps = media.fps
    rate = _rate_text(fps)
    width = cfg.proxy_width if cfg.proxy else cfg.width
    scale = f",scale={width}:-2:flags=bicubic" if width else ""
    parts: List[str] = []
    has_music = bool(music is not None and music.path)
    audio = media.has_audio and not (has_music and music.only)
    eps = TRIM_EPSILON_SECONDS
    for ci in clip_inputs(tl, fps):
        i = ci.index
        parts.append(
            f"[{i}:v]trim=start={max(0.0, ci.start - eps):.4f}:end={ci.end - eps:.4f},setpts=PTS-STARTPTS,"
            f"fps={rate},settb=AVTB,format=yuv420p,setsar=1{scale}[v{i}]"
        )
        if audio:
            parts.append(f"[{i}:a]atrim=start={ci.start:.4f}:end={ci.end:.4f},asetpts=PTS-STARTPTS,asettb=AVTB[a{i}]")

    # the running length in whole frames (each clip's frames as its trim selects them), so offsets do not drift over a long cut
    frames = lambda seconds: int(math.floor(seconds * fps + 0.5)) if fps > 0 else 0  # noqa: E731
    cur_v, cur_a = "[v0]", "[a0]"
    cur_frames = frames(tl.clips[0].duration)
    for i, c in enumerate(tl.clips[1:], start=1):
        tr = c.transition_in
        nv, na = f"[xv{i}]", f"[xa{i}]"
        if tr.is_cut:
            parts.append(f"{cur_v}[v{i}]concat=n=2:v=1:a=0{nv}")
            if audio:
                parts.append(f"{cur_a}[a{i}]concat=n=2:v=0:a=1{na}")
            cur_frames += frames(c.duration)
        else:
            kind = "fadeblack" if tr.kind == "dip_to_black" else "fade"
            offset = max(0.0, (cur_frames - frames(tr.duration)) / fps - eps)
            parts.append(f"{cur_v}[v{i}]xfade=transition={kind}:duration={tr.duration:.6f}:offset={offset:.6f}{nv}")
            if audio:
                parts.append(f"{cur_a}[a{i}]acrossfade=d={tr.duration:.6f}{na}")
            cur_frames += frames(c.duration) - frames(tr.duration)
        cur_v, cur_a = nv, na
    cur_dur = cur_frames / fps if fps > 0 else tl.duration

    vfilters = []
    afilters = []
    if tl.fade_in > 0:
        vfilters.append(f"fade=t=in:st=0:d={tl.fade_in:.3f}")
        afilters.append(f"afade=t=in:st=0:d={tl.fade_in:.3f}")
    if tl.fade_out > 0:
        st = max(0.0, cur_dur - tl.fade_out)
        vfilters.append(f"fade=t=out:st={st:.3f}:d={tl.fade_out:.3f}")
        afilters.append(f"afade=t=out:st={st:.3f}:d={tl.fade_out:.3f}")
    parts.append(f"{cur_v}{','.join(vfilters) if vfilters else 'null'}[vout]")
    if audio:
        label = "[asrc]" if has_music else "[aout]"
        parts.append(f"{cur_a}{','.join(afilters) if afilters else 'anull'}{label}")
    if has_music:
        parts += _music_chain(music, cur_dur, audio, music_duration, music_input=len(tl.clips))
    return ";".join(parts)


def _rate_text(fps: float) -> str:
    """The frame rate for ffmpeg's fps filter: an exact ratio for the usual rates (30000/1001, not 29.9700), else six decimals."""
    exact = Fraction(fps).limit_denominator(1001)
    return f"{exact.numerator}/{exact.denominator}" if abs(float(exact) - fps) < 1e-6 * max(fps, 1.0) else f"{fps:.6f}"


def _music_chain(music: MusicConfig, total: float, source_audio: bool, music_duration: Optional[float] = None, music_input: int = 1) -> List[str]:
    """Trim, fade, gain, duck (optional), mix and normalise the music (input ``music_input``) under [asrc]."""
    parts: List[str] = []
    start = max(0.0, music.start)
    available = total if music_duration is None else max(0.0, min(total, music_duration - start))
    fade = min(music.fade_out, available / 2)
    parts.append(
        f"[{music_input}:a]atrim=start={start:.3f}:end={start + available:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=out:st={max(0.0, available - fade):.3f}:d={fade:.3f},volume={music.gain_db:.1f}dB[music]"
    )
    if source_audio:
        if music.duck:
            parts.append("[asrc]asplit[asrc0][asrc1]")
            parts.append(
                f"[music][asrc1]sidechaincompress=threshold={music.duck_threshold}:ratio={music.duck_ratio}:attack=20:release=300[ducked]"
            )
            parts.append("[asrc0][ducked]amix=inputs=2:duration=first:normalize=0[amixed]")
        else:
            parts.append("[asrc][music]amix=inputs=2:duration=first:normalize=0[amixed]")
    else:
        parts.append("[music]anull[amixed]")
    final = f"loudnorm=I={music.target_lufs:.1f}:TP=-1.5:LRA=11,aresample=48000" if music.loudnorm else "anull"
    parts.append(f"[amixed]{final}[aout]")
    return parts


def render(
    tl: Timeline,
    media: MediaInfo,
    cfg: RenderConfig,
    output: Path,
    overwrite: bool = True,
    music: Optional[MusicConfig] = None,
    music_duration: Optional[float] = None,
) -> Path:
    if not tl.clips:
        raise ValueError("cannot render an empty timeline")
    ffmpeg = _require("ffmpeg")
    has_music = bool(music is not None and music.path)
    graph = build_filter_graph(tl, media, cfg, music=music, music_duration=music_duration)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y" if overwrite else "-n"]
    threads = ["-threads", "1"] if len(tl.clips) > SINGLE_THREAD_DECODE_ABOVE else []
    for ci in clip_inputs(tl, media.fps):
        cmd += [*threads, "-ss", f"{ci.seek:.6f}", "-t", f"{ci.length:.6f}", "-i", media.path]
    if has_music:
        cmd += ["-i", str(music.path)]
    cmd += ["-filter_complex", graph, "-map", "[vout]"]
    if media.has_audio or has_music:
        cmd += ["-map", "[aout]", "-c:a", "aac", "-b:a", cfg.audio_bitrate]
    cmd += [
        "-c:v",
        "libx264",
        "-crf",
        str(cfg.crf if not cfg.proxy else max(cfg.crf, 26)),
        "-preset",
        "veryfast" if cfg.proxy else cfg.preset,
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    log.info("rendering %d clips (%.1fs)%s -> %s", len(tl.clips), tl.duration, " with music" if has_music else "", output)
    run(cmd, capture=False)
    return output
