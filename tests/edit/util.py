"""Builders for the edit tests: small cuts in memory, laid out in frames, with pins, match cuts and a click-track music analysis."""
from __future__ import annotations

from fractions import Fraction
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

from matchcut.edit.model import EditContext, frame_rows
from matchcut.pipeline.audio import MusicAnalysis
from matchcut.pipeline.editorial import Pin, apply_pins
from matchcut.pipeline.timeline import Clip, Timeline, Transition, clip_origin

R25 = Fraction(25)
R2997 = Fraction(30000, 1001)
R23976 = Fraction(24000, 1001)
R5994 = Fraction(60000, 1001)


def bounds_for(n_shots: int, frames_per_shot: int, rate: Fraction) -> Dict[int, Tuple[float, float]]:
    """Shot k covers frames [k * frames_per_shot, (k + 1) * frames_per_shot], as seconds."""
    return {k: (float(Fraction(k * frames_per_shot) / rate), float(Fraction((k + 1) * frames_per_shot) / rate)) for k in range(n_shots)}


def ctx_for(rate: Fraction = R25, n_shots: int = 8, frames_per_shot: int = 100, *, min_clip: float = 0.75, frac: float = 0.4,
            pins: Sequence[Pin] = (), music: Optional[MusicAnalysis] = None, music_start: float = 0.0, window: float = 0.35,
            downbeats: bool = True) -> EditContext:
    return EditContext(rate, bounds_for(n_shots, frames_per_shot, rate), min_clip, frac, tuple(pins), music, music_start, window, downbeats)


def cut(ctx: EditContext, spec: Sequence[tuple]) -> Timeline:
    """A cut from (id, shot, in frame, out frame[, beat[, (kind, frames)]]) rows; every clip carries a stamped origin, as r000 does."""
    tl = Timeline(source="s.mp4", fps=float(ctx.rate))
    for row in spec:
        cid, shot, i, o = row[:4]
        beat = row[4] if len(row) > 4 else "b1"
        kind, n = row[5] if len(row) > 5 else ("cut", 0)
        c = Clip(cid, shot, ctx.s(i), ctx.s(o), beat, transition_in=Transition(kind, ctx.s(n)) if n else Transition())
        c.origin = clip_origin(c)
        tl.clips.append(c)
    return tl


def frames_of(tl: Timeline, ctx: EditContext) -> list:
    return [(c.id, r.in_f, r.out_f, r.kind, r.tr) for c, r in zip(tl.clips, frame_rows(tl, ctx.rate))]


def pin(tl: Timeline, ctx: EditContext, j: int, **kw) -> Tuple[Timeline, Pin]:
    """Pin junction ``j`` (0-based) at its current points, as the Edit view's ``l`` does, and apply it as the pipeline does."""
    a, b = tl.clips[j], tl.clips[j + 1]
    p = Pin(a.shot_index, b.shot_index, round(a.source_out, 3), round(b.source_in, 3), "locked", "", clip_origin(a), clip_origin(b), **kw)
    out, decisions = apply_pins(tl, [p], ctx.bounds, ctx.min_clip_seconds)
    assert decisions[-1].action == "pinned", decisions
    return out, p


def with_pins(ctx: EditContext, pins: Sequence[Pin]) -> EditContext:
    return EditContext(ctx.rate, ctx.bounds, ctx.min_clip_seconds, ctx.max_fraction_of_clip, tuple(pins), ctx.music, ctx.music_start,
                       ctx.snap_window, ctx.prefer_downbeats)


def match_cut(tl: Timeline, j: int, kind: str = "motion") -> Timeline:
    """Mark junction ``j`` (0-based) a match cut, as the match-cut pass marks one."""
    out = tl.copy()
    a, b = out.clips[j], out.clips[j + 1]
    a.locked_out, b.locked_in = True, True
    a.tags.append(f"match_out:{kind}")
    b.tags.append(f"match_in:{kind}")
    return out


def click(beat: float = 0.5, per_bar: int = 4, seconds: float = 120.0) -> MusicAnalysis:
    beats = np.arange(0.0, seconds, beat)
    return MusicAnalysis(path="click.wav", duration=seconds, sample_rate=22050, tempo_bpm=60.0 / beat, beats_per_bar=per_bar, beats=beats,
                         downbeats=beats[::per_bar], energy=np.ones(8, dtype=np.float32), hop_seconds=0.5)
