"""Render a timeline to a video file with ffmpeg (cuts, dissolves, dips, fades)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .config import MusicConfig, RenderConfig
from .media import MediaInfo, _require, run
from .timeline import Timeline

log = logging.getLogger("matchcut.pipeline")


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
    """ffmpeg -filter_complex graph assembling the timeline from input 0 (and music from input 1)."""
    fps = media.fps
    width = cfg.proxy_width if cfg.proxy else cfg.width
    scale = f",scale={width}:-2:flags=bicubic" if width else ""
    parts: List[str] = []
    has_music = bool(music is not None and music.path)
    audio = media.has_audio and not (has_music and music.only)
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
        label = "[asrc]" if has_music else "[aout]"
        parts.append(f"{cur_a}{','.join(afilters) if afilters else 'anull'}{label}")
    if has_music:
        parts += _music_chain(music, cur_dur, audio, music_duration)
    return ";".join(parts)


def _music_chain(music: MusicConfig, total: float, source_audio: bool, music_duration: Optional[float] = None) -> List[str]:
    """Trim, fade, gain, duck (optional), mix and normalise the music (input 1) under [asrc]."""
    parts: List[str] = []
    start = max(0.0, music.start)
    available = total if music_duration is None else max(0.0, min(total, music_duration - start))
    fade = min(music.fade_out, available / 2)
    parts.append(
        f"[1:a]atrim=start={start:.3f}:end={start + available:.3f},asetpts=PTS-STARTPTS,"
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
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y" if overwrite else "-n", "-i", media.path]
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
