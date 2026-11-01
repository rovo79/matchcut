"""Frame arithmetic shared by the pipeline, the renderer and the interchange exporters (which re-export these names), so that every
place that turns seconds into frames rounds the same way."""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Tuple

# the rates a probe that only reports a float (or an approximate rational) is snapped to
COMMON_RATES: Tuple[Fraction, ...] = (
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30), Fraction(48000, 1001), Fraction(48),
    Fraction(50), Fraction(60000, 1001), Fraction(60), Fraction(120000, 1001), Fraction(120),
)
SNAP_TOLERANCE = 0.002  # relative, for a float rate: 23.976 and 29.97 are 0.0001 off their rationals


def nearest_common(rate: float) -> Tuple[Fraction, float]:
    best = min(COMMON_RATES, key=lambda r: abs(float(r) - rate))
    return best, abs(float(best) - rate) / rate


def snap_rate(rate: float) -> Fraction:
    """The common rate nearest to a float rate (24000/1001 for 23.976); an unusual rate becomes a small rational.  ``rate`` must be > 0."""
    if rate <= 0:
        raise ValueError(f"invalid frame rate {rate}")
    best, err = nearest_common(rate)
    return best if err <= SNAP_TOLERANCE else Fraction(rate).limit_denominator(10000)


def to_frames(t: float, rate: Fraction) -> int:
    """Seconds -> whole frames at ``rate``, ties rounded away from zero.

    A millionth of a frame of slack makes exact halves robust against the binary float ``t`` (1/48 s at 24 fps is 1 frame).
    """
    x = Fraction(float(t)) * Fraction(rate)
    n = math.floor(abs(x) + Fraction(1, 2) + Fraction(1, 1000000))
    return -n if x < 0 else n
