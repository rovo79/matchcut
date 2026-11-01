"""Render a timeline to a video file with ffmpeg (cuts, dissolves, dips, fades).

Every clip reads the source through its own ffmpeg input, seeked to just before the clip (``clip_inputs``). With one shared input,
clips that play out of source order (or repeat a range) made ffmpeg buffer, starve or stall the branches it could not feed yet: the
audio of a reordered dissolve came out empty, a reordered cut under music came out short, a repeated range never finished, and a
reordered video held seconds of raw frames in memory.  Each input decodes only its clip (plus ``SEEK_MARGIN_SECONDS`` before it),
and the trims stay exact: they are applied to the decoded frames, relative to the seek point.
"""
from __future__ import annotations

import functools
import logging
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import MusicConfig, RenderConfig
from .media import MediaError, MediaInfo, _require, run, video_extent
from .timeline import Timeline

log = logging.getLogger("matchcut.pipeline")

SEEK_MARGIN_SECONDS = 0.25  # decoded before each clip's in-point: the trims, not the input seek, decide the first frame and sample
READ_MARGIN_SECONDS = 0.5  # read past each clip's out-point
# Video trims start and end this much early.  Points are stored to 4 decimals, so at 29.97 fps an in-point can land up to 0.05 ms after
# its own frame's timestamp, and an exact trim then starts a frame late.  It must stay far below any stream's offset from the file's
# start (a video stream that starts 22 ms after its audio is common): half a frame would pick the previous frame there.
TRIM_EPSILON_SECONDS = 0.0005
SINGLE_THREAD_DECODE_ABOVE = 8  # more clips than this: one decoder thread per input (measured: memory grows ~20 MB per 720p input otherwise)
INLINE_GRAPH_LIMIT = 100_000  # a longer filter graph goes to ffmpeg in a file (Linux refuses one argument over 128 KiB: ~460 clips)
FILES_PER_RENDER = 32  # open files ffmpeg needs besides one per clip input (output, music, libraries)


@dataclass(frozen=True)
class ClipInput:
    """Where clip ``index`` reads the source: ``-ss seek -t length -i source``; its in/out points relative to ``seek``."""

    index: int
    seek: float
    length: float
    start: float
    end: float


# containers with an index ffmpeg seeks by (to the keyframe before the seek point); in others (MPEG-TS such as AVCHD .mts, MPEG program
# streams, raw streams) an input seek can land past the only keyframe in reach and decode nothing, so their clips read from the start
INDEXED_FORMATS = ("mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi", "mxf", "asf", "flv")


@functools.lru_cache(maxsize=64)
def _format_names(path: str, mtime_ns: int) -> tuple:
    proc = run([_require("ffprobe"), "-v", "error", "-show_entries", "format=format_name", "-of", "csv=p=0", path])
    return tuple(x.strip() for x in proc.stdout.decode("utf-8", "replace").strip().strip('"').split(","))


def seekable(path: str) -> bool:
    """Whether ffmpeg can seek ``path`` by an index (see ``INDEXED_FORMATS``); unknown or unreadable files are read from the start."""
    try:
        names = _format_names(str(path), os.stat(path).st_mtime_ns)
    except (MediaError, OSError):
        return False
    return any(n in INDEXED_FORMATS for n in names)


def clip_inputs(tl: Timeline, fps: float, seek: bool = True) -> List[ClipInput]:
    """One input per clip.  ``seek`` on: on the frame grid, ``SEEK_MARGIN_SECONDS`` before the in-point (or 0); off: from the start of the file."""
    out: List[ClipInput] = []
    for i, c in enumerate(tl.clips):
        if not seek:
            out.append(ClipInput(i, 0.0, round(c.source_out + READ_MARGIN_SECONDS, 6), round(c.source_in, 6), round(c.source_out, 6)))
            continue
        seek_at = max(0.0, math.floor((c.source_in - SEEK_MARGIN_SECONDS) * fps + 1e-6) / fps) if fps > 0 else max(0.0, c.source_in - SEEK_MARGIN_SECONDS)
        seek_at = round(seek_at, 6)
        out.append(ClipInput(i, seek_at, round(c.source_out - seek_at + READ_MARGIN_SECONDS, 6), round(c.source_in - seek_at, 6), round(c.source_out - seek_at, 6)))
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
    seek: bool = True,
) -> str:
    """ffmpeg -filter_complex graph assembling the timeline: clip i from input i (see ``clip_inputs``), the music from input ``len(clips)``.

    Transitions are placed by frame count: each clip contributes its whole frames, each transition its whole frames, and an xfade starts
    ``TRIM_EPSILON_SECONDS`` before its first frame's time, written to the microsecond against the exact frame rate (``30000/1001``, not
    ``29.9700``).  Rounded seconds started transitions a frame late at 29.97 and 30 fps."""
    fps = media.fps
    rate = _rate_text(fps)
    width = cfg.proxy_width if cfg.proxy else cfg.width
    scale = f",scale={width}:-2:flags=bicubic" if width else ""
    parts: List[str] = []
    has_music = bool(music is not None and music.path)
    audio = media.has_audio and not (has_music and music.only)
    eps = TRIM_EPSILON_SECONDS
    for ci in clip_inputs(tl, fps, seek):
        i = ci.index
        parts.append(
            f"[{i}:v]trim=start={max(0.0, ci.start - eps):.6f}:end={ci.end - eps:.6f},setpts=PTS-STARTPTS,"
            f"fps={rate},settb=AVTB,format=yuv420p,setsar=1{scale}[v{i}]"
        )
        if audio:
            parts.append(f"[{i}:a]atrim=start={ci.start:.6f}:end={ci.end:.6f},asetpts=PTS-STARTPTS,asettb=AVTB[a{i}]")

    # whole frames, rounded as interchange.common.to_frames rounds them (halves away from zero, with a millionth of a frame of slack, so a
    # float sum such as 0.3 + 0.3 + 0.3 = 0.8999... s still rounds 22.5 frames up, as the exporters' layout does)
    frames = lambda seconds: int(math.floor(seconds * fps + 0.5 + 1e-6)) if fps > 0 else 0  # noqa: E731
    cur_v, cur_a = "[v0]", "[a0]"
    cur_frames = frames(tl.clips[0].duration)
    # transition frames by error diffusion over the running total, as interchange.common.layout lays them out: rounding each one on its
    # own would let the error add up along a cut of fractional dissolves (8 frames after 15 x 0.75 s at 30 fps)
    cum, done = 0.0, 0
    for i, c in enumerate(tl.clips[1:], start=1):
        tr = c.transition_in
        nv, na = f"[xv{i}]", f"[xa{i}]"
        tr_frames = 0
        if not tr.is_cut:
            cum += tr.duration
            tr_frames = max(frames(cum) - done, 0)
            done += tr_frames
        if not tr_frames:
            parts.append(f"{cur_v}[v{i}]concat=n=2:v=1:a=0{nv}")
            if audio:
                parts.append(f"{cur_a}[a{i}]concat=n=2:v=0:a=1{na}")
            cur_frames += frames(c.duration)
        else:
            kind = "fadeblack" if tr.kind == "dip_to_black" else "fade"
            offset = max(0.0, (cur_frames - tr_frames) / fps - eps)
            length = tr_frames / fps
            parts.append(f"{cur_v}[v{i}]xfade=transition={kind}:duration={length:.6f}:offset={offset:.6f}{nv}")
            if audio:
                parts.append(f"{cur_a}[a{i}]acrossfade=d={length:.6f}{na}")
            cur_frames += frames(c.duration) - tr_frames
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
        label = "[asrc]" if has_music else "[apre]"
        parts.append(f"{cur_a}{','.join(afilters) if afilters else 'anull'}{label}")
    if has_music:
        parts += _music_chain(music, cur_dur, audio, music_duration, music_input=len(tl.clips))
    if audio or has_music:
        # the audio lasts exactly as long as the video.  Its timestamps are first renumbered from its samples: loudnorm leaves a 20-100 ms
        # jump about 2.9 s before the end, and each junction can overlap by a sample, so trimming by timestamp would cut real audio
        parts.append(f"[apre]asetpts=N/SR/TB,apad=whole_dur={cur_dur:.6f},atrim=duration={cur_dur:.6f}[aout]")
    return ";".join(parts)


def _rate_text(fps: float) -> str:
    """The frame rate for ffmpeg's fps filter: an exact ratio for the usual rates (30000/1001), else six decimals."""
    exact = Fraction(fps).limit_denominator(1001)
    return f"{exact.numerator}/{exact.denominator}" if abs(float(exact) - fps) < 1e-6 * max(fps, 1.0) else f"{fps:.6f}"


class RangeError(MediaError):
    """A clip's source range the video cannot play; raised before ffmpeg starts."""


def fit_ranges(tl: Timeline, media: MediaInfo) -> Timeline:
    """The timeline the renderer will play, checked against the video stream before ffmpeg starts.

    Refused, with one line (``RangeError``): a clip that starts before the file, is shorter than a frame, lies entirely before the first
    frame of the picture or starts at or after its end (half a frame before it; the container's end when the stream records no length),
    or that trimming to the picture (below) would leave shorter than its own transitions.  A dissolve into a clip with no picture made ffmpeg wait forever.

    Kept, with a warning: an in-point before the first frame, or an out-point past the last, when the clip overlaps the picture.  Shots
    used to span the container (whose audio can start before, or run on after, the picture), so older workspaces have such clips; the copy
    starts or ends them at the picture (the stage itself is not changed).  The picture's extent is the video stream's own, in frames."""
    fps = media.fps if media.fps > 0 else 25.0
    extent = video_extent(media.path)
    out = tl.copy()
    name = Path(media.path).name
    frames = lambda t: t * fps  # noqa: E731
    for k, c in enumerate(out.clips):
        if c.source_in < -1e-6:
            raise RangeError(f"clip {c.id} starts at {c.source_in:.3f} s, before the start of {name}")
        if frames(c.source_out - c.source_in) < 1 - 1e-3:
            raise RangeError(f"clip {c.id} is shorter than one frame ({c.source_in:.3f}-{c.source_out:.3f} s)")
        if extent is None:
            continue
        slack = 0.01 + extent.slack * fps  # frames: 4-decimal points, and a millisecond-precision end
        start_f, end_f = frames(extent.start), frames(extent.end)
        where = (f"the video of {name} runs {extent.start:.3f}-{extent.end:.3f} s" if extent.exact else
                 f"{name} records no length for its video; it starts at {extent.start:.3f} s and the file ends at {extent.end:.3f} s")
        if extent.exact and extent.container_end is not None and extent.container_end > extent.end + 0.5 / fps:
            where += f" (the file, with its audio, runs to {extent.container_end:.3f} s)"
        if frames(c.source_in) >= end_f - 0.5:
            raise RangeError(f"clip {c.id} starts at {c.source_in:.3f} s, at or after the end of the video: {where}")
        if frames(c.source_out) <= start_f + 0.5:
            raise RangeError(f"clip {c.id} ends at {c.source_out:.3f} s, before the first frame of the video: {where}")
        trimmed = False
        if frames(c.source_in) < start_f - slack:
            trimmed = True
            first = math.ceil(start_f - 1e-3) / fps
            log.warning("clip %s starts at %.3f s, before the first frame of the picture: rendered from %.3f s (%s)", c.id, c.source_in, first, where)
            c.source_in = round(first, 6)
        if extent.exact and frames(c.source_out) > end_f + slack:
            trimmed = True
            last = math.floor(end_f + slack) / fps
            log.warning("clip %s ends at %.3f s, past the end of the picture: rendered to %.3f s (%s)", c.id, c.source_out, last, where)
            c.source_out = round(last, 6)
        if frames(c.source_out - c.source_in) < 1 - 1e-3:
            raise RangeError(f"clip {c.id} has less than a frame of picture: {where}")
        trs = [t.duration for t in (c.transition_in if k else None, out.clips[k + 1].transition_in if k + 1 < len(out.clips) else None)
               if t is not None and not t.is_cut]
        if trimmed and trs and frames(c.source_out - c.source_in) < frames(max(trs)) + 1 - 1e-3:
            raise RangeError(f"clip {c.id} has {frames(c.source_out - c.source_in):.0f} frames of picture, too few for its "
                             f"{frames(max(trs)):.0f}-frame transition: {where}")
    return out


_UNSUPPORTED = ("Unrecognized option", "Option not found", "unrecognized option")


@functools.lru_cache(maxsize=8)
def graph_file_option(ffmpeg: str) -> str:
    """The option that makes *this* ffmpeg read its filter graph from a file, found by asking it: ``-/filter_complex`` (ffmpeg 7.1 and later;
    ffmpeg 9 removed ``-filter_complex_script``) or ``-filter_complex_script`` (older versions).  An option counts as unsupported only when
    ffmpeg says it does not know it: a probe that fails for another reason (a build without the probe's filters) still found the option."""
    fd, name = tempfile.mkstemp(prefix=".graph-probe-", suffix=".txt")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("anullsrc=r=8000:cl=mono,atrim=duration=0.01[o]")
        for option in GRAPH_FILE_OPTIONS:
            try:
                proc = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-nostdin", option, name, "-map", "[o]", "-f", "null", "-"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            except subprocess.TimeoutExpired as exc:
                raise MediaError(f"{ffmpeg} did not answer within 60 s when asked about {option}") from exc
            stderr = proc.stderr.decode("utf-8", "replace")
            if proc.returncode == 0 or not any(marker in stderr for marker in _UNSUPPORTED):
                return option
    finally:
        Path(name).unlink(missing_ok=True)
    try:
        version = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, timeout=30).stdout.splitlines()[:1]
    except (subprocess.TimeoutExpired, OSError):
        version = []
    raise MediaError(f"{ffmpeg} ({version[0] if version else 'unknown version'}) accepts neither {' nor '.join(GRAPH_FILE_OPTIONS)}: "
                     "it cannot read this cut's filter graph from a file")


def _ensure_open_files(n_inputs: int) -> None:
    """The renderer opens the source once per clip: raise the soft open-file limit toward the hard one if the cut needs more, or say why
    it cannot (a 256-file default, as on macOS, stops a cut of about 220 clips)."""
    try:
        import resource
    except ImportError:  # Windows: no rlimit to raise
        return
    need = n_inputs + FILES_PER_RENDER
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= need:
        return
    if hard != resource.RLIM_INFINITY and hard < need:
        raise MediaError(f"this cut has {n_inputs} clips and the renderer opens the source once per clip, but the open-file limit is {hard}: "
                         f"raise it (ulimit -n {need}) and render again")
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (need, hard))
    except (ValueError, OSError) as exc:  # e.g. above macOS's kern.maxfilesperproc
        raise MediaError(f"this cut has {n_inputs} clips and the renderer opens the source once per clip, but the open-file limit "
                         f"({soft}) cannot be raised to {need}: {exc}") from exc


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
            parts.append("[asrc0][ducked]amix=inputs=2:duration=longest:normalize=0[amixed]")
        else:
            parts.append("[asrc][music]amix=inputs=2:duration=longest:normalize=0[amixed]")
    else:
        parts.append("[music]anull[amixed]")
    final = f"loudnorm=I={music.target_lufs:.1f}:TP=-1.5:LRA=11,aresample=48000" if music.loudnorm else "anull"
    parts.append(f"[amixed]{final}[apre]")
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
    tl = fit_ranges(tl, media)
    _ensure_open_files(len(tl.clips))
    ffmpeg = _require("ffmpeg")
    has_music = bool(music is not None and music.path)
    can_seek = seekable(media.path)  # decided once: the graph's trims and the inputs' seeks must agree
    graph = build_filter_graph(tl, media, cfg, music=music, music_duration=music_duration, seek=can_seek)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y" if overwrite else "-n"]
    threads = ["-threads", "1"] if len(tl.clips) > SINGLE_THREAD_DECODE_ABOVE else []
    for ci in clip_inputs(tl, media.fps, can_seek):
        cmd += [*threads, "-ss", f"{ci.seek:.6f}", "-t", f"{ci.length:.6f}", "-i", media.path]
    if has_music:
        cmd += ["-i", str(music.path)]
    graph_file = None
    if len(graph) > INLINE_GRAPH_LIMIT:
        option = graph_file_option(ffmpeg)  # asked before the file exists, so a refusal leaves nothing behind
        fd, name = tempfile.mkstemp(prefix=".graph-", suffix=".txt", dir=output.parent)
        with os.fdopen(fd, "w") as fh:
            fh.write(graph)
        graph_file = Path(name)
        cmd += [option, str(graph_file), "-map", "[vout]"]
    else:
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
    try:
        run(cmd, capture=False)
    finally:
        if graph_file is not None:
            graph_file.unlink(missing_ok=True)
    return output
