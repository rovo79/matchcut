"""Render a timeline to a video file with ffmpeg (cuts, dissolves, dips, fades)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from .config import RenderConfig
from .media import MediaInfo, _require, run
from .timeline import Timeline

log = logging.getLogger("matchcut.pipeline")


def snap_to_frames(tl: Timeline) -> Timeline:
    """Round in/out points to the source frame grid (keeps the EDL and render in step)."""
    out = tl.copy()
    fps = out.fps
    for c in out.clips:
        c.source_in = round(round(c.source_in * fps) / fps, 4)
        c.source_out = round(round(c.source_out * fps) / fps, 4)
        if c.source_out - c.source_in < 2 / fps:
            c.source_out = round(c.source_in + 2 / fps, 4)
    return out


def build_filter_graph(tl: Timeline, media: MediaInfo, cfg: RenderConfig) -> str:
    """ffmpeg -filter_complex graph that assembles the timeline from input 0."""
    fps = media.fps
    width = cfg.proxy_width if cfg.proxy else cfg.width
    scale = f",scale={width}:-2:flags=bicubic" if width else ""
    parts: List[str] = []
    audio = media.has_audio
    for i, c in enumerate(tl.clips):
        parts.append(
            f"[0:v]trim=start={c.source_in:.4f}:end={c.source_out:.4f},setpts=PTS-STARTPTS,"
            f"fps={fps:.4f},settb=AVTB,format=yuv420p,setsar=1{scale}[v{i}]"
        )
        if audio:
            parts.append(f"[0:a]atrim=start={c.source_in:.4f}:end={c.source_out:.4f},asetpts=PTS-STARTPTS,asettb=AVTB[a{i}]")

    cur_v, cur_a = "[v0]", "[a0]"
    cur_dur = tl.clips[0].duration
    for i, c in enumerate(tl.clips[1:], start=1):
        tr = c.transition_in
        nv, na = f"[xv{i}]", f"[xa{i}]"
        if tr.is_cut:
            parts.append(f"{cur_v}[v{i}]concat=n=2:v=1:a=0{nv}")
            if audio:
                parts.append(f"{cur_a}[a{i}]concat=n=2:v=0:a=1{na}")
            cur_dur += c.duration
        else:
            kind = "fadeblack" if tr.kind == "dip_to_black" else "fade"
            offset = max(0.0, cur_dur - tr.duration)
            parts.append(f"{cur_v}[v{i}]xfade=transition={kind}:duration={tr.duration:.3f}:offset={offset:.3f}{nv}")
            if audio:
                parts.append(f"{cur_a}[a{i}]acrossfade=d={tr.duration:.3f}{na}")
            cur_dur += c.duration - tr.duration
        cur_v, cur_a = nv, na

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
        parts.append(f"{cur_a}{','.join(afilters) if afilters else 'anull'}[aout]")
    return ";".join(parts)


def render(tl: Timeline, media: MediaInfo, cfg: RenderConfig, output: Path, overwrite: bool = True) -> Path:
    if not tl.clips:
        raise ValueError("cannot render an empty timeline")
    ffmpeg = _require("ffmpeg")
    graph = build_filter_graph(tl, media, cfg)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y" if overwrite else "-n", "-i", media.path, "-filter_complex", graph, "-map", "[vout]"]
    if media.has_audio:
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
    log.info("rendering %d clips (%.1fs) -> %s", len(tl.clips), tl.duration, output)
    run(cmd, capture=False)
    return output
