"""Thin wrappers around ffmpeg/ffprobe.

Everything that touches the footage on disk goes through here so the rest of
the pipeline can work with numpy arrays, PIL images and plain numbers.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

import numpy as np
from PIL import Image


class MediaError(RuntimeError):
    pass


def _require(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise MediaError(f"{binary} not found on PATH; install ffmpeg to use the pipeline")
    return path


def run(cmd: Sequence[str], capture: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        list(cmd),
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-4000:]
        raise MediaError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{err}")
    return proc


@dataclass(frozen=True)
class MediaInfo:
    path: str
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    codec: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "MediaInfo":
        return cls(**d)


def _parse_rate(rate: str) -> float:
    if "/" in rate:
        num, den = rate.split("/")
        return float(num) / float(den) if float(den) else 0.0
    return float(rate or 0)


def probe(path: str) -> MediaInfo:
    ffprobe = _require("ffprobe")
    proc = run(
        [
            ffprobe,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
    )
    data = json.loads(proc.stdout.decode("utf-8"))
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError(f"no video stream in {path}")
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0.0)
    fps = _parse_rate(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0")
    if fps <= 0:
        fps = _parse_rate(video.get("r_frame_rate") or "25")
    return MediaInfo(
        path=str(path),
        duration=duration,
        width=int(video["width"]),
        height=int(video["height"]),
        fps=fps or 25.0,
        has_audio=has_audio,
        codec=str(video.get("codec_name", "")),
    )


@dataclass(frozen=True)
class VideoExtent:
    """Where the first video stream lies, in seconds from the start of the file (the time matchcut's timelines count in).

    ``exact`` says ``end`` is the video stream's own end (its duration, frame count, or Matroska DURATION tag); when no stream length is
    recorded, ``end`` is the container's end, an upper bound (the audio can run on past the picture).  ``container_end`` is the file's end."""

    start: float
    end: float
    container_end: Optional[float]
    exact: bool


def _tag_seconds(text: object) -> Optional[float]:
    """A Matroska DURATION tag (``00:00:10.021000000``) in seconds."""
    try:
        h, m, sec = str(text).split(":")
        return int(h) * 3600 + int(m) * 60 + float(sec)
    except (ValueError, TypeError):
        return None


def video_extent(path: str) -> Optional[VideoExtent]:
    """The first video stream's extent (``VideoExtent``), or None when ffprobe finds no video stream or no length at all."""
    ffprobe = _require("ffprobe")
    proc = run([ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=start_time,duration,nb_frames,avg_frame_rate:stream_tags=DURATION:format=start_time,duration",
                "-of", "json", str(path)])
    data = json.loads(proc.stdout.decode("utf-8"))
    streams, fmt = data.get("streams") or [], data.get("format") or {}
    if not streams:
        return None
    v = streams[0]

    def num(x: object) -> Optional[float]:
        try:
            return float(x)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    origin = num(fmt.get("start_time")) or 0.0
    stream_start = num(v.get("start_time"))
    start = (stream_start if stream_start is not None else origin) - origin
    container = num(fmt.get("duration"))
    length = num(v.get("duration"))
    if length is None:
        frames, rate = num(v.get("nb_frames")), _parse_rate(v.get("avg_frame_rate") or "0")
        length = frames / rate if frames and rate > 0 else None
    if length is not None:
        return VideoExtent(start, start + length, container, True)
    tag = _tag_seconds((v.get("tags") or {}).get("DURATION"))
    if tag is not None:  # Matroska/WebM: the stream's end time, counted like the file's timestamps
        return VideoExtent(start, tag - origin, container, True)
    if container is not None:
        return VideoExtent(start, container, container, False)
    return None


def iter_frames(
    path: str,
    fps: float,
    width: int,
    height: int,
    start: Optional[float] = None,
    duration: Optional[float] = None,
) -> Iterator[np.ndarray]:
    """Stream RGB frames (H, W, 3) uint8 at a fixed rate and resolution."""
    ffmpeg = _require("ffmpeg")
    cmd: List[str] = [ffmpeg, "-v", "error", "-nostdin"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += [
        "-vf",
        f"fps={fps},scale={width}:{height}:flags=area",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-an",
        "-",
    ]
    frame_bytes = width * height * 3
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    try:
        while True:
            buf = proc.stdout.read(frame_bytes)
            if len(buf) < frame_bytes:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3)
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
        proc.wait()
        if proc.returncode not in (0, None) and "Broken pipe" not in stderr:
            raise MediaError(f"ffmpeg frame decode failed: {stderr[-2000:]}")


def extract_frame(path: str, t: float, width: int, height: Optional[int] = None) -> Image.Image:
    """Grab one frame at time ``t`` as a PIL image, scaled to ``width``."""
    ffmpeg = _require("ffmpeg")
    scale = f"scale={width}:{height if height else -2}:flags=area"
    proc = run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-ss",
            f"{max(t, 0.0):.3f}",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            scale,
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
    )
    if not proc.stdout:
        raise MediaError(f"could not extract frame at {t:.3f}s from {path}")
    return Image.open(io.BytesIO(proc.stdout)).convert("RGB")


def audio_energy(path: str, sample_rate: int, hop_seconds: float) -> np.ndarray:
    """RMS audio energy per hop window (0..1). Empty array when there is no audio."""
    ffmpeg = _require("ffmpeg")
    cmd = [
        ffmpeg,
        "-v",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "s16le",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0 or not proc.stdout:
        return np.zeros(0, dtype=np.float32)
    samples = np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    hop = max(1, int(sample_rate * hop_seconds))
    n = len(samples) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    chunks = samples[: n * hop].reshape(n, hop)
    rms = np.sqrt(np.mean(chunks * chunks, axis=1))
    return rms.astype(np.float32)


def seconds_to_timecode(t: float, fps: float) -> str:
    total_frames = int(round(max(t, 0.0) * fps))
    fps_i = int(round(fps))
    frames = total_frames % fps_i if fps_i else 0
    secs = total_frames // fps_i if fps_i else 0
    return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}:{frames:02d}"
