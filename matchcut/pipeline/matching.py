"""Match-cut scoring.

A match cut joins the *tail* of one shot to the *head* of the next so that
composition, shape, movement or colour carry across the cut. The scorer rates
the junction between any two time points of the footage using the cell
features, in the spirit of the frame-similarity and motion-similarity tasks of
the match-cutting paper, but with cheap handcrafted descriptors.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional, Tuple

import numpy as np

from .config import MatchCutConfig
from .features import CellFeatures


@dataclass(frozen=True)
class MatchScore:
    structure: float
    composition: float
    luminance: float
    color: float
    frame: float
    motion: float
    combined: float
    jump_cut_risk: bool
    motion_type: str  # "static", "moving", "mixed"

    def to_dict(self) -> Dict[str, float]:
        d = asdict(self)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in d.items()}


def _hist_intersection(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.minimum(a, b).sum())


def _l1_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(max(0.0, 1.0 - 0.5 * np.abs(a - b).sum()))


def _cosine01(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return float(max(0.0, np.dot(a, b) / (na * nb)))


class MatchScorer:
    def __init__(self, cells: CellFeatures, cfg: MatchCutConfig):
        self.cells = cells
        self.cfg = cfg

    # -- feature access ----------------------------------------------------
    def _cell(self, t: float, before: bool) -> int:
        """Cell representing the frame just before (tail) or at (head) time t."""
        i = self.cells.nearest(t)
        if before and self.cells.times[i] > t and i > 0:
            i -= 1
        if not before and self.cells.times[i] < t and i + 1 < len(self.cells):
            i += 1
        return i

    def tail_cell(self, out_time: float) -> int:
        return self._cell(out_time, before=True)

    def head_cell(self, in_time: float) -> int:
        return self._cell(in_time, before=False)

    # -- scoring -----------------------------------------------------------
    def score_cells(self, i: int, j: int) -> MatchScore:
        c, cfg = self.cells, self.cfg
        structure = _cosine01(c.structure[i], c.structure[j])
        composition = _l1_similarity(c.edges[i], c.edges[j])
        luminance = float(max(0.0, 1.0 - np.abs(c.luminance[i] - c.luminance[j]).mean() * 2))
        color = _hist_intersection(c.color[i], c.color[j])
        frame = (
            cfg.w_structure * structure
            + cfg.w_composition * composition
            + cfg.w_luminance * luminance
            + cfg.w_color * color
        )

        ma, mb = float(c.motion_mag[i]), float(c.motion_mag[j])
        moving_a = ma >= cfg.moving_energy or c.motion_speed[i] >= cfg.moving_speed
        moving_b = mb >= cfg.moving_energy or c.motion_speed[j] >= cfg.moving_speed
        if moving_a and moving_b:
            dir_cos = float(np.dot(c.motion_dir[i], c.motion_dir[j]))
            if np.linalg.norm(c.motion_dir[i]) < 0.5 or np.linalg.norm(c.motion_dir[j]) < 0.5:
                dir_cos = 0.0
            dir_score = (dir_cos + 1.0) / 2.0
            sa, sb = float(c.motion_speed[i]), float(c.motion_speed[j])
            mag_score = 0.5 * (1.0 - abs(ma - mb) / max(ma, mb, 1e-6)) + 0.5 * (1.0 - abs(sa - sb) / max(sa, sb, 1e-6))
            where = _l1_similarity(
                c.motion_grid[i] / max(c.motion_grid[i].sum(), 1e-6),
                c.motion_grid[j] / max(c.motion_grid[j].sum(), 1e-6),
            )
            motion = 0.5 * dir_score + 0.3 * mag_score + 0.2 * where
            motion_type = "moving"
        elif not moving_a and not moving_b:
            motion, motion_type = 0.5, "static"
        else:
            motion, motion_type = 0.2, "mixed"

        combined = cfg.w_frame * frame + cfg.w_motion * motion
        jump = structure >= cfg.jump_cut_structure and color >= cfg.jump_cut_color
        return MatchScore(
            structure=structure,
            composition=composition,
            luminance=luminance,
            color=color,
            frame=frame,
            motion=motion,
            combined=combined,
            jump_cut_risk=bool(jump),
            motion_type=motion_type,
        )

    def score_cut(self, out_time: float, in_time: float) -> MatchScore:
        """Score the junction: footage leaving at ``out_time`` into footage entering at ``in_time``."""
        return self.score_cells(self.tail_cell(out_time), self.head_cell(in_time))

    def best_junction(
        self,
        out_range: Tuple[float, float],
        in_range: Tuple[float, float],
    ) -> Tuple[float, float, MatchScore]:
        """Search out/in points inside the given ranges for the best junction.

        Returns (out_time, in_time, score). Ranges are inclusive and expressed
        in source seconds; candidate points are the cell centres inside them
        plus the range endpoints.
        """
        outs = self._candidates(out_range)
        ins = self._candidates(in_range)
        best: Optional[Tuple[float, float, MatchScore]] = None
        for o in outs:
            ti = self.tail_cell(o)
            for n in ins:
                hj = self.head_cell(n)
                s = self.score_cells(ti, hj)
                if best is None or s.combined > best[2].combined:
                    best = (o, n, s)
        assert best is not None
        return best

    def _candidates(self, rng: Tuple[float, float]) -> np.ndarray:
        lo, hi = rng
        if hi < lo:
            lo, hi = hi, lo
        idx = self.cells.window(lo, hi)
        pts = set(float(self.cells.times[i]) for i in idx if lo <= self.cells.times[i] <= hi)
        pts.add(float(lo))
        pts.add(float(hi))
        return np.array(sorted(pts), dtype=np.float32)
