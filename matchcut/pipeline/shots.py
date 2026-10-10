"""Shot boundary detection on the per-frame series."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Sequence

import numpy as np

from .config import ShotConfig
from .features import FrameSeries


@dataclass(frozen=True)
class Shot:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def middle(self) -> float:
        return (self.start + self.end) / 2

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = round(self.duration, 3)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Shot":
        return cls(index=int(d["index"]), start=float(d["start"]), end=float(d["end"]))


def _rolling_median_mad(x: np.ndarray, window: int) -> tuple:
    n = len(x)
    med = np.zeros(n, dtype=np.float32)
    mad = np.zeros(n, dtype=np.float32)
    for i in range(n):
        lo, hi = max(0, i - window), min(n, i + window + 1)
        seg = x[lo:hi]
        m = float(np.median(seg))
        med[i] = m
        mad[i] = float(np.median(np.abs(seg - m)))
    return med, mad


def detect_boundaries(series: FrameSeries, cfg: ShotConfig) -> List[int]:
    """Return frame indices where a new shot starts (never includes 0)."""
    d = series.hist_dist
    p = series.pixel_dist
    n = len(d)
    if n < 2:
        return []
    med, mad = _rolling_median_mad(d, cfg.adaptive_window)
    adaptive = med + cfg.adaptive_k * np.maximum(mad, 0.01)
    min_gap = max(1, int(round(cfg.min_shot_seconds * series.fps)))

    candidates = []
    for i in range(1, n):
        strong = d[i] >= cfg.hist_threshold or p[i] >= cfg.pixel_threshold
        if not strong or d[i] < adaptive[i]:
            continue
        lo, hi = max(1, i - 2), min(n, i + 3)
        if d[i] < d[lo:hi].max():
            continue  # not a local peak
        candidates.append(i)

    boundaries: List[int] = []
    last = 0
    for i in candidates:
        if i - last < min_gap:
            # keep the stronger of the two boundaries that are too close together
            if boundaries and d[i] > d[boundaries[-1]] and (boundaries[-1] - (boundaries[-2] if len(boundaries) > 1 else 0)) >= min_gap:
                boundaries[-1] = i
                last = i
            continue
        boundaries.append(i)
        last = i
    if n - last < min_gap and boundaries:
        boundaries.pop()
    return boundaries


def detect_shots(series: FrameSeries, cfg: ShotConfig, duration: float | None = None) -> List[Shot]:
    total = duration if duration is not None else series.duration
    bounds = detect_boundaries(series, cfg)
    if len(bounds) + 1 > cfg.max_shots:
        # keep the strongest boundaries only
        strengths = sorted(bounds, key=lambda i: -series.hist_dist[i])[: cfg.max_shots - 1]
        bounds = sorted(strengths)
    starts = [0.0] + [b / series.fps for b in bounds]
    ends = starts[1:] + [total]
    return [Shot(index=i, start=s, end=e) for i, (s, e) in enumerate(zip(starts, ends)) if e > s]


def save_shots(shots: Sequence[Shot], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([s.to_dict() for s in shots], indent=2))


def load_shots(path: Path) -> List[Shot]:
    return [Shot.from_dict(d) for d in json.loads(Path(path).read_text())]
