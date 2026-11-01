"""What the edit operations and rules read: the source's exact rate, the shots, the limits, the editor's pins and the music.

Nothing here reads a workspace: the caller (the session) builds an ``EditContext`` once, from ``PipelineConfig`` and what it loaded
(``context_from``), and every operation and rule is a pure function of a timeline and that context.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Dict, List, Mapping, Optional, Sequence, Tuple, Union

from matchcut.pipeline.audio import MusicAnalysis
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin
from matchcut.pipeline.frames import snap_rate, to_frames
from matchcut.pipeline.timeline import Timeline

# slack for turning a shot boundary or a length in seconds into frames: a boundary a hair inside a frame line is on it
_EPS_FRAMES = 1e-6


class Refused(Exception):
    """One operation cannot be applied; the message says why, in one line.  ``index`` is the 0-based position of the operation in a batch
    (``ops.apply_ops``), or None for a single operation."""

    def __init__(self, message: str, index: Optional[int] = None):
        super().__init__(message)
        self.index = index


@dataclass(frozen=True, eq=False)
class EditContext:
    """Everything the operations and rules need; no workspace I/O.

    ``rate`` is the source's exact frame rate.  ``bounds`` maps a shot index to its (start, end) in seconds.  Clip points are stored in
    seconds rounded to the microsecond (``s``), and read back as whole frames (``f``, the shared rounding of ``pipeline.frames``).
    (Not comparable or hashable by value: it holds the pins and the music analysis.)
    """

    rate: Fraction
    bounds: Dict[int, Tuple[float, float]] = field(default_factory=dict)
    min_clip_seconds: float = 0.75
    max_fraction_of_clip: float = 0.4
    pins: Tuple[Pin, ...] = ()
    music: Optional[MusicAnalysis] = None
    music_start: float = 0.0
    snap_window: float = 0.35
    prefer_downbeats: bool = True

    def __post_init__(self) -> None:
        rate = self.rate if isinstance(self.rate, Fraction) else (Fraction(self.rate) if isinstance(self.rate, int) else snap_rate(float(self.rate)))
        if rate <= 0:
            raise ValueError(f"invalid frame rate {self.rate}")
        object.__setattr__(self, "rate", rate)
        object.__setattr__(self, "bounds", {int(k): (float(v[0]), float(v[1])) for k, v in self.bounds.items()})
        object.__setattr__(self, "pins", tuple(self.pins))

    @property
    def fps(self) -> float:
        return float(self.rate)

    @property
    def min_frames(self) -> int:
        """The minimum clip length in whole frames (rounded up: a clip of this many frames is at least ``min_clip_seconds`` long)."""
        return max(1, math.ceil(Fraction(self.min_clip_seconds) * self.rate - Fraction(_EPS_FRAMES)))

    @property
    def max_fraction(self) -> Fraction:
        """``max_fraction_of_clip`` as an exact fraction, so a limit such as 0.4 x 45 frames is exactly 18."""
        return Fraction(self.max_fraction_of_clip).limit_denominator(1000000)

    def f(self, seconds: float) -> int:
        """Seconds -> whole frames at ``rate`` (``pipeline.frames.to_frames``)."""
        return to_frames(seconds, self.rate)

    def s(self, frames: int) -> float:
        """Whole frames -> seconds as stored in a timeline (to the microsecond; ``f(s(n)) == n``)."""
        return round(float(Fraction(int(frames)) / self.rate), 6)

    def shot_frames(self, shot: int) -> Tuple[int, int]:
        """The whole frames (lo, hi) inside shot ``shot``: a clip's in-point may be ``lo`` and its out-point ``hi``.  KeyError for an
        unknown shot."""
        lo, hi = self.bounds[shot]
        return (math.ceil(Fraction(lo) * self.rate - Fraction(_EPS_FRAMES)), math.floor(Fraction(hi) * self.rate + Fraction(_EPS_FRAMES)))

    def has_shot(self, shot: int) -> bool:
        return shot in self.bounds


def context_from(cfg: PipelineConfig, rate: Union[Fraction, float], bounds: Mapping[int, Sequence[float]], pins: Sequence[Pin] = (),
                 music: Optional[MusicAnalysis] = None) -> EditContext:
    """The context of a workspace: its config (pacing, transitions and music settings), the source's exact ``rate``, the shot ``bounds``,
    the editor's ``pins`` and the music analysis (None without music)."""
    return EditContext(
        rate=rate if isinstance(rate, Fraction) else snap_rate(float(rate)),
        bounds={int(k): (float(v[0]), float(v[1])) for k, v in bounds.items()},
        min_clip_seconds=float(cfg.pacing.min_clip_seconds),
        max_fraction_of_clip=float(cfg.transitions.max_fraction_of_clip),
        pins=tuple(pins),
        music=music,
        music_start=float(cfg.music.start),
        snap_window=float(cfg.music.snap_window),
        prefer_downbeats=bool(cfg.music.prefer_downbeats_at_beat_changes),
    )


@dataclass(frozen=True)
class Row:
    """One clip of a cut on the frame grid, exactly as ``interchange.common.layout`` lays it out (its ``in_f``, ``out_f``, ``rec``, ``tr``,
    ``kind``, ``head`` and ``tail``), but without refusing a clip that is no longer than its transitions: ``visible`` < 1 says so."""

    in_f: int  # source range, frames
    out_f: int
    tr: int  # incoming transition, frames (0 = cut)
    kind: str  # incoming transition kind ("cut" when tr == 0)
    rec: int  # record frame where the clip starts (for a transition: where it starts to show), as Timeline.record_start
    tail: int  # frames the next clip's transition takes from this clip's visible end (layout's out_off)

    @property
    def length(self) -> int:
        return self.out_f - self.in_f

    @property
    def head(self) -> int:
        return self.tr // 2

    @property
    def visible(self) -> int:
        """Frames an editor shows of this clip once its transitions are centred on their cuts; layout refuses the cut below 1."""
        return self.length - self.head - self.tail

    @property
    def rec_out(self) -> int:
        return self.rec + self.length


def frame_rows(tl: Timeline, rate: Fraction) -> List[Row]:
    """``tl`` on the frame grid at ``rate``: the same arithmetic as ``interchange.common.layout`` (transitions rounded over the running
    total), one ``Row`` per clip."""
    base: List[Tuple[int, int, int, str, int]] = []
    cum, done = 0.0, 0
    for i, c in enumerate(tl.clips):
        in_f = to_frames(c.source_in, rate)
        out_f = max(to_frames(c.source_out, rate), in_f + 1)
        tr = 0
        if i and not c.transition_in.is_cut:
            cum += c.transition_in.duration
            tr = max(to_frames(cum, rate) - done, 0)
            done += tr
        rec = base[-1][4] + (base[-1][1] - base[-1][0]) - tr if base else 0
        base.append((in_f, out_f, tr, c.transition_in.kind if tr else "cut", rec))
    out: List[Row] = []
    for i, (in_f, out_f, tr, kind, rec) in enumerate(base):
        nxt = base[i + 1][2] if i + 1 < len(base) else 0
        out.append(Row(in_f, out_f, tr, kind, rec, nxt - nxt // 2))
    return out


def running_frames(rows: Sequence[Row]) -> int:
    """The cut's length in frames (``interchange.common.record_frames`` of its layout)."""
    return rows[-1].rec_out - rows[-1].tail if rows else 0
