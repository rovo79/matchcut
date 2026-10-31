"""Identifiable test media for the renderer tests, and the readers that check a render against its timeline.

- Video: every frame carries its own frame number, 12 bits as black/white stripes, so each rendered frame names its source frame.
- Source audio: a tone whose pitch steps every second (200 Hz + 40 Hz per second), so each stretch of audio names its source second,
  and silence can never pass for content.
- Music: 50 ms ticks at 3 kHz every 0.5 s, far from the source tone's band.
"""
from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

BITS = 12
SR = 48000
TICK_HZ = 3000.0


def tone_hz(second: int) -> float:
    return 200.0 + 40.0 * second


def make_coded_footage(out: Path, seconds: int = 20, rate: str = "25", width: int = 320, height: int = 180, ts_offset: float = 0.0) -> Path:
    stripe = width // BITS
    lum = f"if(lt(X,{stripe * BITS}),255*mod(floor(N/pow(2,floor(X/{stripe}))),2),128)"
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y",
           "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={rate}:d={seconds},format=gray,geq=lum='{lum}'",
           "-f", "lavfi", "-i", f"aevalsrc=exprs='0.5*sin(2*PI*(200+40*floor(t))*t)':s={SR}:d={seconds}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p", "-g", "50", "-c:a", "aac", "-b:a", "192k"]
    if ts_offset:
        cmd += ["-output_ts_offset", f"{ts_offset}"]
    subprocess.run(cmd + [str(out)], check=True)
    return out


def make_tick_music(out: Path, seconds: int = 60) -> Path:
    expr = f"if(lt(mod(t\\,0.5)\\,0.05)\\,0.8*sin(2*PI*{TICK_HZ:.0f}*t)\\,0)"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"aevalsrc=exprs='{expr}':s=44100:d={seconds}", str(out)], check=True)
    return out


def streams(path: Path) -> Dict[str, dict]:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=codec_type,duration,nb_read_frames", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


def frame_numbers(path: Path, width: int = 320, height: int = 180) -> List[int]:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    n = len(raw) // (width * height)
    frames = np.frombuffer(raw[: n * width * height], dtype=np.uint8).reshape(n, height, width)
    stripe = width // BITS
    return [sum(1 << i for i in range(BITS) if f[:, i * stripe + 2:(i + 1) * stripe - 2].mean() > 128) for f in frames]


def audio(path: Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def _window(y: np.ndarray, t: float, w: float) -> np.ndarray:
    return y[max(0, int(t * SR)): max(0, int((t + w) * SR))]


def band_rms(y: np.ndarray, t: float, w: float, lo: float, hi: float) -> float:
    """RMS of the part of the window's spectrum between ``lo`` and ``hi`` Hz (0 for an empty window)."""
    s = _window(y, t, w)
    if len(s) < 64:
        return 0.0
    spec = np.fft.rfft(s * np.hanning(len(s)))
    freqs = np.fft.rfftfreq(len(s), 1 / SR)
    keep = (freqs >= lo) & (freqs <= hi)
    return float(np.sqrt(2 * np.sum(np.abs(spec[keep]) ** 2)) / np.sum(np.hanning(len(s))))


def dominant_tone(y: np.ndarray, t: float, w: float = 0.2) -> Optional[float]:
    s = _window(y, t, w)
    if len(s) < int(SR * w * 0.9):
        return None
    spec = np.abs(np.fft.rfft(s * np.hanning(len(s))))
    freqs = np.fft.rfftfreq(len(s), 1 / SR)
    spec[(freqs < 150) | (freqs > 1500)] = 0
    return float(freqs[int(np.argmax(spec))])


def interior(tl, i: int) -> Tuple[float, float]:
    """Record seconds where clip ``i`` plays alone (outside both transitions)."""
    rs = tl.record_start(i)
    lo = rs + (tl.clips[i].transition_in.duration if i else 0.0)
    nxt = tl.clips[i + 1].transition_in.duration if i + 1 < len(tl.clips) else 0.0
    return lo, rs + tl.clips[i].duration - nxt


def expected_frames(tl, fps: float) -> Dict[int, int]:
    """{output frame: source frame} for every frame one clip shows alone, outside the cut's fades."""
    out: Dict[int, int] = {}
    for i, c in enumerate(tl.clips):
        lo, hi = interior(tl, i)
        rs = tl.record_start(i)
        for k in range(int(np.ceil(lo * fps - 1e-6)), int(np.floor(hi * fps - 1e-6)) + 1):
            t = k / fps
            if t < lo - 1e-9 or t >= hi - 1e-9 or t < tl.fade_in or t >= tl.duration - tl.fade_out - 1e-9:
                continue
            out[k] = int(round((c.source_in + (t - rs)) * fps))
    return out


def tone_window(tl, i: int, w: float = 0.2) -> Optional[Tuple[float, int]]:
    """(record time, source second) of a window inside clip ``i`` that stays inside one source second, or None."""
    lo, hi = interior(tl, i)
    c, rs = tl.clips[i], tl.record_start(i)
    t = lo + 0.05
    while t + w <= hi - 0.05:
        src = c.source_in + (t - rs)
        if int(src + 1e-6) == int(src + w - 1e-6):
            return t, int(src + 1e-6)
        t += 0.02
    return None


def av_tolerance(fps: float) -> float:
    """How far audio and video lengths may differ: two AAC frames of priming/padding plus one video frame."""
    return 2 * 1024 / SR + 1 / fps
