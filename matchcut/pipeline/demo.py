"""Generate synthetic test footage with ffmpeg (no real video needed).

The clip is a sequence of visually distinct "shots" (solid colours, test
patterns, moving bars) with a tone on the audio track, so shot detection,
feature extraction and rendering can be exercised end to end.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .media import _require, run

DEFAULT_SHOTS: Tuple[Tuple[str, float], ...] = (
    ("color=c=0x204080:s={w}x{h}:r={fps}", 2.0),
    ("testsrc2=s={w}x{h}:r={fps}", 3.0),
    ("color=c=0xc04020:s={w}x{h}:r={fps}", 1.5),
    ("smptebars=s={w}x{h}:r={fps}", 2.5),
    ("testsrc=s={w}x{h}:r={fps}", 3.0),
    ("color=c=0x208040:s={w}x{h}:r={fps}", 2.0),
    ("rgbtestsrc=s={w}x{h}:r={fps}", 2.0),
    ("mandelbrot=s={w}x{h}:r={fps}", 2.5),
)


def make_demo_footage(
    output: Path,
    width: int = 320,
    height: int = 180,
    fps: int = 25,
    shots: Optional[Sequence[Tuple[str, float]]] = None,
    audio: bool = True,
) -> Path:
    ffmpeg = _require("ffmpeg")
    shots = list(shots or DEFAULT_SHOTS)
    cmd: List[str] = [ffmpeg, "-v", "error", "-nostdin", "-y"]
    for src, dur in shots:
        cmd += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", src.format(w=width, h=height, fps=fps)]
    total = sum(d for _, d in shots)
    if audio:
        cmd += ["-f", "lavfi", "-t", f"{total:.3f}", "-i", "sine=frequency=440:sample_rate=44100"]
    inputs = "".join(f"[{i}:v]" for i in range(len(shots)))
    graph = f"{inputs}concat=n={len(shots)}:v=1:a=0,format=yuv420p[v]"
    cmd += ["-filter_complex", graph, "-map", "[v]"]
    if audio:
        cmd += ["-map", f"{len(shots)}:a", "-c:a", "aac", "-shortest"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(fps), str(output)]
    output.parent.mkdir(parents=True, exist_ok=True)
    run(cmd, capture=False)
    return output


def make_click_track(
    output: Path,
    seconds: float = 40.0,
    bpm: float = 120.0,
    beats_per_bar: int = 4,
    sample_rate: int = 22050,
) -> Path:
    """Synthetic music: a tick on every beat and a louder low thump on every bar (WAV)."""
    ffmpeg = _require("ffmpeg")
    beat = 60.0 / bpm
    bar = beat * beats_per_bar
    expr = (
        f"0.6*sin(2*PI*880*t)*lt(mod(t,{beat:.6f}),0.05)"
        f"+0.9*sin(2*PI*110*t)*lt(mod(t,{bar:.6f}),0.08)"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-t",
            f"{seconds:.3f}",
            "-i",
            f"aevalsrc='{expr}':s={sample_rate}",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        capture=False,
    )
    return output


if __name__ == "__main__":  # pragma: no cover
    import sys

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "demo_footage.mp4")
    print(make_demo_footage(out))
