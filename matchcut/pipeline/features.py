"""Visual features used for shot detection and match-cut scoring.

The footage is decoded once at low resolution. Two kinds of signal are kept:

* per-frame series (colour-histogram distance and pixel distance to the
  previous frame) used by shot detection;
* per-cell features every ``cell_seconds`` describing structure, composition,
  colour and motion, used by the match-cut scorer at arbitrary timestamps.

No ML dependencies: everything is numpy. The design mirrors the heuristics in
the match-cutting paper (frame similarity and motion similarity) in a form
that runs on a laptop without GPU embeddings.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import numpy as np

from .config import AnalysisConfig
from .media import iter_frames

STRUCT_W, STRUCT_H = 32, 18
COLOR_BINS = 4
EDGE_BINS = 8
GRID = 3


# --------------------------------------------------------------------------
# per-frame descriptors
# --------------------------------------------------------------------------
def to_gray(frame: np.ndarray) -> np.ndarray:
    f = frame.astype(np.float32)
    return 0.299 * f[..., 0] + 0.587 * f[..., 1] + 0.114 * f[..., 2]


def color_histogram(frame: np.ndarray) -> np.ndarray:
    """L1-normalised joint RGB histogram with COLOR_BINS^3 bins."""
    q = (frame.astype(np.int32) * COLOR_BINS) // 256
    idx = (q[..., 0] * COLOR_BINS + q[..., 1]) * COLOR_BINS + q[..., 2]
    hist = np.bincount(idx.ravel(), minlength=COLOR_BINS ** 3).astype(np.float32)
    return hist / max(hist.sum(), 1.0)


def chi_square(a: np.ndarray, b: np.ndarray) -> float:
    return float(0.5 * np.sum((a - b) ** 2 / (a + b + 1e-9)))


def block_mean(gray: np.ndarray, out_w: int, out_h: int) -> np.ndarray:
    h, w = gray.shape
    ys = np.linspace(0, h, out_h + 1).astype(int)
    xs = np.linspace(0, w, out_w + 1).astype(int)
    out = np.empty((out_h, out_w), dtype=np.float32)
    for i in range(out_h):
        for j in range(out_w):
            out[i, j] = gray[ys[i] : ys[i + 1], xs[j] : xs[j + 1]].mean()
    return out


def structure_vector(gray: np.ndarray) -> np.ndarray:
    """Zero-mean, unit-norm coarse luminance thumbnail (shape / layout)."""
    thumb = block_mean(gray, STRUCT_W, STRUCT_H).ravel()
    thumb = thumb - thumb.mean()
    norm = np.linalg.norm(thumb)
    return thumb / norm if norm > 1e-6 else thumb


def edge_vector(gray: np.ndarray) -> np.ndarray:
    """Gradient-orientation histogram on a GRIDxGRID layout (composition/lines)."""
    gx = np.zeros_like(gray)
    gy = np.zeros_like(gray)
    gx[:, 1:-1] = gray[:, 2:] - gray[:, :-2]
    gy[1:-1, :] = gray[2:, :] - gray[:-2, :]
    mag = np.hypot(gx, gy)
    ang = (np.arctan2(gy, gx) + np.pi) % np.pi  # unsigned orientation 0..pi
    bins = np.minimum((ang / np.pi * EDGE_BINS).astype(int), EDGE_BINS - 1)
    h, w = gray.shape
    out = np.zeros((GRID, GRID, EDGE_BINS), dtype=np.float32)
    ys = np.linspace(0, h, GRID + 1).astype(int)
    xs = np.linspace(0, w, GRID + 1).astype(int)
    for i in range(GRID):
        for j in range(GRID):
            m = mag[ys[i] : ys[i + 1], xs[j] : xs[j + 1]].ravel()
            b = bins[ys[i] : ys[i + 1], xs[j] : xs[j + 1]].ravel()
            out[i, j] = np.bincount(b, weights=m, minlength=EDGE_BINS)
    flat = out.ravel()
    return flat / max(flat.sum(), 1e-6)


def luminance_grid(gray: np.ndarray) -> np.ndarray:
    return block_mean(gray, GRID, GRID).ravel() / 255.0


def phase_shift(prev: np.ndarray, cur: np.ndarray) -> Tuple[float, float]:
    """Dominant translation (dx, dy) in pixels between two gray frames."""
    h, w = prev.shape
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    f1 = np.fft.rfft2((prev - prev.mean()) * win)
    f2 = np.fft.rfft2((cur - cur.mean()) * win)
    cross = f1 * np.conj(f2)
    cross /= np.abs(cross) + 1e-6
    corr = np.fft.irfft2(cross, s=(h, w))
    iy, ix = np.unravel_index(int(np.argmax(corr)), corr.shape)
    dy = iy - h if iy > h // 2 else iy
    dx = ix - w if ix > w // 2 else ix
    # the correlation peak gives prev->cur displacement with a sign flip
    return float(-dx), float(-dy)


def motion_grid(prev: np.ndarray, cur: np.ndarray) -> np.ndarray:
    diff = np.abs(cur - prev) / 255.0
    return block_mean(diff, GRID, GRID).ravel()


# --------------------------------------------------------------------------
# containers
# --------------------------------------------------------------------------
@dataclass
class FrameSeries:
    """Per-frame signals at the analysis frame rate."""

    fps: float
    hist_dist: np.ndarray  # chi-square distance to previous frame
    pixel_dist: np.ndarray  # mean abs gray diff to previous frame (0..1)
    brightness: np.ndarray  # mean luminance (0..1)
    struct_sim: np.ndarray  # correlation of coarse layout with previous frame (fades ~1, cuts ~0)

    @property
    def n_frames(self) -> int:
        return int(len(self.hist_dist))

    @property
    def duration(self) -> float:
        return self.n_frames / self.fps


@dataclass
class CellFeatures:
    """Feature vectors sampled every ``cell_seconds``."""

    times: np.ndarray  # (N,) cell centre time in seconds
    structure: np.ndarray  # (N, STRUCT_W*STRUCT_H)
    color: np.ndarray  # (N, COLOR_BINS**3)
    edges: np.ndarray  # (N, GRID*GRID*EDGE_BINS)
    luminance: np.ndarray  # (N, GRID*GRID)
    motion_mag: np.ndarray  # (N,) 0..1
    motion_dir: np.ndarray  # (N, 2) unit vector (dx, dy), zeros when static
    motion_speed: np.ndarray  # (N,) pixels per frame at analysis res
    motion_grid: np.ndarray  # (N, GRID*GRID)

    def __len__(self) -> int:
        return int(len(self.times))

    def index_at(self, t: float) -> int:
        if len(self.times) == 0:
            raise ValueError("no cells")
        return int(np.clip(np.searchsorted(self.times, t), 0, len(self.times) - 1))

    def nearest(self, t: float) -> int:
        i = self.index_at(t)
        if i > 0 and abs(self.times[i - 1] - t) < abs(self.times[i] - t):
            return i - 1
        return i

    def window(self, start: float, end: float) -> np.ndarray:
        """Indices of cells whose centre falls within [start, end]."""
        lo = int(np.searchsorted(self.times, start, side="left"))
        hi = int(np.searchsorted(self.times, end, side="right"))
        if hi <= lo:
            return np.array([self.nearest((start + end) / 2)])
        return np.arange(lo, hi)


@dataclass
class Analysis:
    series: FrameSeries
    cells: CellFeatures
    audio_energy: np.ndarray  # per audio hop (may be empty)
    audio_hop_seconds: float

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez_compressed(
            path,
            fps=np.array([self.series.fps]),
            hist_dist=self.series.hist_dist,
            pixel_dist=self.series.pixel_dist,
            brightness=self.series.brightness,
            struct_sim=self.series.struct_sim,
            times=self.cells.times,
            structure=self.cells.structure,
            color=self.cells.color,
            edges=self.cells.edges,
            luminance=self.cells.luminance,
            motion_mag=self.cells.motion_mag,
            motion_dir=self.cells.motion_dir,
            motion_speed=self.cells.motion_speed,
            motion_grid=self.cells.motion_grid,
            audio_energy=self.audio_energy,
            audio_hop=np.array([self.audio_hop_seconds]),
        )

    @classmethod
    def load(cls, path: Path) -> "Analysis":
        with np.load(path) as z:
            series = FrameSeries(
                fps=float(z["fps"][0]),
                hist_dist=z["hist_dist"],
                pixel_dist=z["pixel_dist"],
                brightness=z["brightness"],
                struct_sim=z["struct_sim"] if "struct_sim" in z else np.zeros_like(z["hist_dist"]),
            )
            cells = CellFeatures(
                times=z["times"],
                structure=z["structure"],
                color=z["color"],
                edges=z["edges"],
                luminance=z["luminance"],
                motion_mag=z["motion_mag"],
                motion_dir=z["motion_dir"],
                motion_speed=z["motion_speed"],
                motion_grid=z["motion_grid"],
            )
            return cls(
                series=series,
                cells=cells,
                audio_energy=z["audio_energy"],
                audio_hop_seconds=float(z["audio_hop"][0]),
            )


# --------------------------------------------------------------------------
# streaming analyser
# --------------------------------------------------------------------------
class _CellAccumulator:
    def __init__(self) -> None:
        self.times: List[float] = []
        self.structure: List[np.ndarray] = []
        self.color: List[np.ndarray] = []
        self.edges: List[np.ndarray] = []
        self.luminance: List[np.ndarray] = []
        self.motion_mag: List[float] = []
        self.motion_dir: List[np.ndarray] = []
        self.motion_speed: List[float] = []
        self.motion_grid: List[np.ndarray] = []

    def add(
        self,
        t: float,
        frame: np.ndarray,
        gray_window: List[np.ndarray],
        jumps: List[bool],
    ) -> None:
        """``jumps[k]`` marks the pair (k, k+1) of the window as a probable cut: it is
        skipped so that motion never mixes two shots."""
        gray = gray_window[len(gray_window) // 2]
        self.times.append(t)
        self.structure.append(structure_vector(gray))
        self.color.append(color_histogram(frame))
        self.edges.append(edge_vector(gray))
        self.luminance.append(luminance_grid(gray))
        mags, shifts, grids = [], [], []
        centre = len(gray_window) // 2
        # pairs on the far side of a cut (relative to the centre frame) belong to another shot
        lo = max([k + 1 for k in range(len(jumps)) if jumps[k] and k < centre], default=0)
        hi = min([k for k in range(len(jumps)) if jumps[k] and k >= centre], default=len(gray_window) - 1)
        for k, (prev, cur) in enumerate(zip(gray_window[:-1], gray_window[1:])):
            if k < lo or k >= hi:
                continue
            mags.append(float(np.mean(np.abs(cur - prev)) / 255.0))
            shifts.append(phase_shift(prev, cur))
            grids.append(motion_grid(prev, cur))
        if mags:
            mag = float(np.mean(mags))
            shift = np.mean(np.array(shifts, dtype=np.float32), axis=0)
            speed = float(np.hypot(*shift))
            direction = shift / speed if speed > 0.25 else np.zeros(2, dtype=np.float32)
            grid = np.mean(np.array(grids, dtype=np.float32), axis=0)
        else:
            mag, speed = 0.0, 0.0
            direction = np.zeros(2, dtype=np.float32)
            grid = np.zeros(GRID * GRID, dtype=np.float32)
        self.motion_mag.append(mag)
        self.motion_dir.append(direction.astype(np.float32))
        self.motion_speed.append(speed)
        self.motion_grid.append(grid)

    def build(self) -> CellFeatures:
        def stack(xs: List[np.ndarray], dim: int) -> np.ndarray:
            return np.array(xs, dtype=np.float32) if xs else np.zeros((0, dim), dtype=np.float32)

        return CellFeatures(
            times=np.array(self.times, dtype=np.float32),
            structure=stack(self.structure, STRUCT_W * STRUCT_H),
            color=stack(self.color, COLOR_BINS ** 3),
            edges=stack(self.edges, GRID * GRID * EDGE_BINS),
            luminance=stack(self.luminance, GRID * GRID),
            motion_mag=np.array(self.motion_mag, dtype=np.float32),
            motion_dir=stack(self.motion_dir, 2),
            motion_speed=np.array(self.motion_speed, dtype=np.float32),
            motion_grid=stack(self.motion_grid, GRID * GRID),
        )


def analyse_frames(frames: Iterable[np.ndarray], cfg: AnalysisConfig) -> Tuple[FrameSeries, CellFeatures]:
    """Single pass over frames: per-frame series + per-cell features."""
    hist_dist: List[float] = []
    pixel_dist: List[float] = []
    brightness: List[float] = []
    struct_sim: List[float] = []
    cells = _CellAccumulator()

    stride = max(1, int(round(cfg.cell_seconds * cfg.fps)))
    win = cfg.motion_window
    buffer: List[Tuple[int, np.ndarray, np.ndarray, float]] = []  # (idx, frame, gray, hist_dist)
    jump_threshold = 0.3  # a histogram jump this large between neighbours is treated as a cut
    prev_hist: Optional[np.ndarray] = None
    prev_gray: Optional[np.ndarray] = None
    prev_thumb: Optional[np.ndarray] = None
    pending: List[int] = []  # cell centre indices waiting for their window

    def flush(force: bool) -> None:
        while pending:
            centre = pending[0]
            have_last = buffer and buffer[-1][0] >= centre + win
            if not have_last and not force:
                return
            in_window = [b for b in buffer if centre - win <= b[0] <= centre + win]
            window = [g for (_, _, g, _) in in_window]
            jumps = [d >= jump_threshold for (_, _, _, d) in in_window[1:]]
            frame = next(f for (i, f, _, _) in buffer if i == centre)
            cells.add(centre / cfg.fps, frame, window if window else [to_gray(frame)], jumps)
            pending.pop(0)

    for idx, frame in enumerate(frames):
        gray = to_gray(frame)
        hist = color_histogram(frame)
        thumb = block_mean(gray, 16, 9).ravel()
        thumb = thumb - thumb.mean()
        if prev_hist is None:
            hist_dist.append(0.0)
            pixel_dist.append(0.0)
            struct_sim.append(1.0)
        else:
            hist_dist.append(chi_square(hist, prev_hist))
            pixel_dist.append(float(np.mean(np.abs(gray - prev_gray)) / 255.0))
            norms = float(np.linalg.norm(thumb) * np.linalg.norm(prev_thumb))
            struct_sim.append(float(np.dot(thumb, prev_thumb) / norms) if norms > 1e-6 else 0.0)
        brightness.append(float(gray.mean() / 255.0))
        prev_hist, prev_gray, prev_thumb = hist, gray, thumb

        buffer.append((idx, frame, gray, hist_dist[-1]))
        if idx % stride == stride // 2:
            pending.append(idx)
        flush(force=False)
        keep_from = (pending[0] if pending else idx) - win
        buffer = [b for b in buffer if b[0] >= keep_from]
    flush(force=True)

    series = FrameSeries(
        fps=cfg.fps,
        hist_dist=np.array(hist_dist, dtype=np.float32),
        pixel_dist=np.array(pixel_dist, dtype=np.float32),
        brightness=np.array(brightness, dtype=np.float32),
        struct_sim=np.array(struct_sim, dtype=np.float32),
    )
    return series, cells.build()


def analyse_footage(path: str, cfg: AnalysisConfig) -> Tuple[FrameSeries, CellFeatures]:
    frames = iter_frames(path, fps=cfg.fps, width=cfg.width, height=cfg.height)
    return analyse_frames(frames, cfg)
