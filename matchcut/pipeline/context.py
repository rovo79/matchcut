"""Shared state handed to stages and passes."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from .audio import MusicAnalysis
from .config import PipelineConfig
from .features import Analysis
from .llm import LLMBackend
from .matching import MatchScorer
from .media import MediaInfo
from .sampling import FrameCache
from .shots import Shot


@dataclass
class Context:
    cfg: PipelineConfig
    workspace: Path
    media: MediaInfo
    analysis: Analysis
    shots: List[Shot]
    frames: FrameCache
    llm: LLMBackend
    descriptions: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    narrative: Dict[str, Any] = field(default_factory=dict)
    music: Optional[MusicAnalysis] = None
    notes: Dict[str, List[str]] = field(default_factory=dict)  # director's notes per stage, from checkpoints

    @property
    def scorer(self) -> MatchScorer:
        if not hasattr(self, "_scorer"):
            self._scorer = MatchScorer(self.analysis.cells, self.cfg.matchcut)
        return self._scorer

    @property
    def shot_bounds(self) -> Dict[int, tuple]:
        return {s.index: (s.start, s.end) for s in self.shots}

    def shot(self, index: int) -> Shot:
        return self.shots[index]

    # -- machine notes about a shot -------------------------------------
    def shot_stats(self, shot: Shot) -> Dict[str, Any]:
        cells = self.analysis.cells
        idx = cells.window(shot.start, shot.end)
        mag = cells.motion_mag[idx]
        dirs = cells.motion_dir[idx]
        mean_dir = dirs.mean(axis=0) if len(dirs) else np.zeros(2)
        peak = float(cells.times[idx[int(np.argmax(mag))]]) if len(idx) else shot.middle
        series = self.analysis.series
        f0, f1 = int(shot.start * series.fps), max(int(shot.start * series.fps) + 1, int(shot.end * series.fps))
        brightness = float(series.brightness[f0:f1].mean()) if f1 > f0 and len(series.brightness) else 0.0
        return dict(
            shot_id=shot.index,
            start=round(shot.start, 3),
            end=round(shot.end, 3),
            duration=round(shot.duration, 3),
            brightness=round(brightness, 3),
            motion_energy=round(float(mag.mean()) if len(mag) else 0.0, 4),
            motion_direction=direction_label(mean_dir),
            interest_peak=round(peak, 3),
            audio_energy=round(self.audio_energy(shot.start, shot.end), 4),
        )

    def audio_energy(self, start: float, end: float) -> float:
        e = self.analysis.audio_energy
        if len(e) == 0:
            return 0.0
        hop = self.analysis.audio_hop_seconds
        lo, hi = int(start / hop), max(int(start / hop) + 1, int(end / hop))
        seg = e[lo:hi]
        return float(seg.mean()) if len(seg) else 0.0

    def catalog(self, shot_ids: Optional[List[int]] = None) -> List[Dict[str, Any]]:
        """Shot catalog rows combining machine stats with model descriptions."""
        rows = []
        for shot in self.shots:
            if shot_ids is not None and shot.index not in shot_ids:
                continue
            row = self.shot_stats(shot)
            desc = self.descriptions.get(shot.index)
            if desc:
                row.update({k: v for k, v in desc.items() if k != "shot_id"})
            rows.append(row)
        return rows

    def target_text(self) -> str:
        t = self.cfg.target_duration
        return f"{t:.0f} seconds" if t else "your call (choose something the footage supports)"

    def narrative_text(self) -> str:
        return json.dumps(self.narrative, indent=1) if self.narrative else "(none)"

    def music_text(self) -> str:
        """Prompt paragraph describing the music track, or a plain 'none'."""
        if self.music is None:
            return "Music track: none."
        s = self.music.summary(every=5.0)
        return (
            "Music track (the cut plays over it; the cut starts "
            f"{self.cfg.music.start:.1f}s into the track): {json.dumps(s)}\n"
            "Energy is 0..1 per 5 s. Let the beat structure follow the music: calm beats on low "
            "energy, the climax beat on the biggest rise, and beat durations that land beat changes "
            "near rises or drops. Cuts will be snapped to the beat grid afterwards."
        )

    def notes_text(self, stage: str) -> str:
        """Prompt paragraph with the director's notes recorded for ``stage``."""
        notes = self.notes.get(stage, [])
        if not notes:
            return "Director's notes: (none)"
        return "Director's notes:\n" + "\n".join(f"- {n}" for n in notes)


def direction_label(v: np.ndarray) -> str:
    x, y = float(v[0]), float(v[1])
    if abs(x) < 0.2 and abs(y) < 0.2:
        return "none"
    if abs(x) >= abs(y):
        return "right" if x > 0 else "left"
    return "down" if y > 0 else "up"
