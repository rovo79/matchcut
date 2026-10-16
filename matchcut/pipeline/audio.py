"""Music track analysis: tempo, beats, downbeats and an energy curve.

Decoding goes through ffmpeg (same as the rest of the pipeline); librosa is
used only for onset strength and beat tracking.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .config import MusicConfig
from .media import MediaError, _require

ONSET_HOP = 512  # librosa hop length in samples


def decode_audio(path: str, sample_rate: int, start: float = 0.0, duration: Optional[float] = None) -> np.ndarray:
    """Mono float32 samples in -1..1 at ``sample_rate``."""
    ffmpeg = _require("ffmpeg")
    cmd: List[str] = [ffmpeg, "-v", "error", "-nostdin"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0 or not proc.stdout:
        err = proc.stderr.decode("utf-8", "replace")[-500:]
        raise MediaError(f"could not decode audio from {path}: {err}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


@dataclass
class MusicAnalysis:
    path: str
    duration: float
    sample_rate: int
    tempo_bpm: float
    beats_per_bar: int
    beats: np.ndarray  # seconds into the track, ascending
    downbeats: np.ndarray  # subset of beats (every beats_per_bar-th, phase by onset strength)
    energy: np.ndarray  # RMS per hop, normalised so the loudest hop is 1.0
    hop_seconds: float

    # -- queries ---------------------------------------------------------
    def nearest_beat(self, t: float, downbeats: bool = False) -> Optional[float]:
        grid = self.downbeats if downbeats else self.beats
        if len(grid) == 0:
            return None
        return float(grid[int(np.argmin(np.abs(grid - t)))])

    def energy_at(self, t: float) -> float:
        if len(self.energy) == 0:
            return 0.0
        i = int(np.clip(int(t / self.hop_seconds), 0, len(self.energy) - 1))
        return float(self.energy[i])

    def summary(self, every: float = 5.0) -> Dict[str, Any]:
        """Compact description for prompts: tempo, length, energy curve, rises and drops."""
        n = int(self.duration // every) + 1
        curve = [round(self.energy_at(k * every), 2) for k in range(n)]
        rises: List[float] = []
        drops: List[float] = []
        for k in range(1, n):
            delta = curve[k] - curve[k - 1]
            if delta >= 0.25:
                rises.append(k * every)
            elif delta <= -0.25:
                drops.append(k * every)
        return dict(
            tempo_bpm=round(self.tempo_bpm, 1),
            duration_s=round(self.duration, 1),
            beats_per_bar=self.beats_per_bar,
            energy_every_s=every,
            energy_curve=curve,
            rises_at_s=rises,
            drops_at_s=drops,
        )

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        # no rounding: float32 -> float is exact, so save/load round-trips losslessly
        return dict(
            path=self.path,
            duration=self.duration,
            sample_rate=self.sample_rate,
            tempo_bpm=self.tempo_bpm,
            beats_per_bar=self.beats_per_bar,
            beats=[float(b) for b in self.beats],
            downbeats=[float(b) for b in self.downbeats],
            energy=[float(e) for e in self.energy],
            hop_seconds=self.hop_seconds,
        )

    @classmethod
    def from_dict(cls, d: dict) -> "MusicAnalysis":
        return cls(
            path=str(d["path"]),
            duration=float(d["duration"]),
            sample_rate=int(d["sample_rate"]),
            tempo_bpm=float(d["tempo_bpm"]),
            beats_per_bar=int(d.get("beats_per_bar", 4)),
            beats=np.array(d["beats"], dtype=np.float32),
            downbeats=np.array(d["downbeats"], dtype=np.float32),
            energy=np.array(d["energy"], dtype=np.float32),
            hop_seconds=float(d["hop_seconds"]),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> "MusicAnalysis":
        return cls.from_dict(json.loads(Path(path).read_text()))


def analyse_music(path: str, cfg: MusicConfig) -> MusicAnalysis:
    try:
        import librosa
    except ImportError as e:  # pragma: no cover
        raise MediaError("pip install librosa to analyse a music track") from e

    sr = cfg.sample_rate
    y = decode_audio(path, sr)
    duration = len(y) / sr

    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=ONSET_HOP)
    tempo, beat_frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=ONSET_HOP, units="frames")
    tempo_bpm = float(np.atleast_1d(tempo)[0]) if np.size(tempo) else 0.0
    beat_frames = np.asarray(beat_frames, dtype=int)
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=ONSET_HOP).astype(np.float32)

    bpb = max(1, int(cfg.beats_per_bar))
    if len(beat_frames):
        strength = onset[np.clip(beat_frames, 0, len(onset) - 1)]
        phase = int(np.argmax([float(strength[k::bpb].sum()) for k in range(bpb)]))
        downbeats = beats[phase::bpb]
    else:
        downbeats = beats

    hop = max(1, int(sr * cfg.hop_seconds))
    n = len(y) // hop
    if n:
        frames = y[: n * hop].reshape(n, hop)
        rms = np.sqrt(np.mean(frames * frames, axis=1))
        energy = (rms / max(float(rms.max()), 1e-6)).astype(np.float32)
    else:
        energy = np.zeros(0, dtype=np.float32)

    return MusicAnalysis(
        path=str(path),
        duration=duration,
        sample_rate=sr,
        tempo_bpm=tempo_bpm,
        beats_per_bar=bpb,
        beats=beats,
        downbeats=np.asarray(downbeats, dtype=np.float32),
        energy=energy,
        hop_seconds=cfg.hop_seconds,
    )
