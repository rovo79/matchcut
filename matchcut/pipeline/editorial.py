"""Editorial pins: junctions the editor has locked or reverted, kept across regenerations.

A pin names a junction by its two shots (clip ids change when a cut is
regenerated; shots do not) and stores the cut points to hold. Pins are applied
to the rough cut before any pass runs: both ends of the junction are locked,
the passes leave locked ends alone, and pacing never drops a pinned clip. The
clips are chosen by the pinned points (the piece of each shot that holds them).
When they are not adjacent in the rough cut (a later pass would have joined
them), one clip is moved next to the other, never splitting or reordering the
beats and never moving a clip another pin holds; pins already in place are
applied before any pin that needs a move.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .timeline import Clip, Decision, Timeline

log = logging.getLogger(__name__)


@dataclass
class Pin:
    out_shot: int
    in_shot: int
    out_time: float
    in_time: float
    origin: str = "locked"  # "locked": keep the inspected points; "reverted": back to the first cut's points
    note: str = ""

    @property
    def key(self) -> Tuple[int, int]:
        return (self.out_shot, self.in_shot)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Pin":
        return cls(int(d["out_shot"]), int(d["in_shot"]), float(d["out_time"]), float(d["in_time"]), str(d.get("origin", "locked")), str(d.get("note", "")))


class PinStore:
    """``workspace/editorial.json``: {"pins": [...]}, written atomically."""

    def __init__(self, ws: Path):
        self.path = Path(ws) / "editorial.json"

    def load(self) -> List[Pin]:
        try:
            entries = json.loads(self.path.read_text()).get("pins", [])
            iter(entries)
        except (OSError, ValueError, TypeError, AttributeError, OverflowError):
            return []
        pins: List[Pin] = []
        for entry in entries:  # one malformed entry must not make a later put/remove drop the others
            try:
                pins.append(Pin.from_dict(entry))
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                log.warning("%s: skipping a malformed pin entry: %r", self.path, entry)
        return pins

    def _save(self, pins: Sequence[Pin]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"pins": [p.to_dict() for p in pins]}, indent=2))
        os.replace(tmp, self.path)

    def get(self, out_shot: int, in_shot: int) -> Optional[Pin]:
        return next((p for p in self.load() if p.key == (out_shot, in_shot)), None)

    def put(self, pin: Pin) -> None:
        pins = self.load()
        for i, p in enumerate(pins):
            if p.key == pin.key:
                pins[i] = pin
                break
        else:
            pins.append(pin)
        self._save(pins)

    def remove(self, out_shot: int, in_shot: int) -> bool:
        pins = self.load()
        kept = [p for p in pins if p.key != (out_shot, in_shot)]
        if len(kept) == len(pins):
            return False
        self._save(kept)
        return True


def _snap(t: float, fps: float) -> float:
    return round(round(t * fps) / fps, 3) if fps > 0 else round(t, 3)


def _beat_runs(clips: Sequence[Clip]) -> List[str]:
    runs: List[str] = []
    for c in clips:
        if not runs or runs[-1] != c.beat_id:
            runs.append(c.beat_id)
    return runs


def _index(clips: Sequence[Clip], clip: Clip) -> int:
    return next(i for i, c in enumerate(clips) if c is clip)


def _distance(c: Clip, t: float) -> float:
    """0 when ``t`` lies in the clip's source range, else the gap to the nearer end."""
    return max(c.source_in - t, t - c.source_out, 0.0)


def _nearest(clips: Sequence[Clip], shot: int, t: float, skip: Optional[Clip] = None) -> Optional[Clip]:
    """The clip of ``shot`` whose source range holds ``t`` (else the nearest one); the earlier clip on a tie."""
    best: Optional[Clip] = None
    for c in clips:
        if c.shot_index != shot or c is skip:
            continue
        if best is None or _distance(c, t) < _distance(best, t) - 1e-9:
            best = c
    return best


def _choose(out: Timeline, pin: Pin) -> Tuple[Optional[Tuple[Clip, Clip]], str]:
    """The (outgoing, incoming) clips the pinned points select, two distinct clips; or (None, reason)."""
    a = _nearest(out.clips, pin.out_shot, pin.out_time)
    if a is None:
        return None, f"shot {pin.out_shot} is not in this cut"
    if pin.in_shot == pin.out_shot:  # a jump cut: two different pieces of one shot
        b = _nearest(out.clips, pin.in_shot, pin.in_time, skip=a)
        if b is None:
            return None, f"shot {pin.in_shot} has only one clip in this cut"
    else:
        b = _nearest(out.clips, pin.in_shot, pin.in_time)
        if b is None:
            return None, f"shot {pin.in_shot} is not in this cut"
    return (a, b), ""


def _move_next_to(out: Timeline, a: Clip, b: Clip, pin: Pin) -> Tuple[Optional[Decision], str]:
    """Move ``b`` just after ``a``, or else ``a`` just before ``b``; (the pin_moved decision, "") or (None, the first reason)."""
    runs = _beat_runs(out.clips)
    reason = ""
    for moved, anchor, after in ((b, a, True), (a, b, False)):
        if moved.locked_in or moved.locked_out:
            reason = reason or f"shot {moved.shot_index} is held by another pin"
            continue
        if (after and anchor.locked_out) or (not after and anchor.locked_in):
            reason = reason or f"shot {anchor.shot_index} is held by another pin"
            continue
        order = [c for c in out.clips if c is not moved]
        order.insert(_index(order, anchor) + (1 if after else 0), moved)
        if _beat_runs(order) != runs:
            reason = reason or f"moving shot {moved.shot_index} would split or reorder the beats"
            continue
        out.clips = order
        detail = f"moved shot {moved.shot_index} next to shot {anchor.shot_index} to hold the editor's pin"
        return Decision("editor", "pin_moved", [moved.id, anchor.id], detail, None, pin.to_dict()), ""
    return None, reason


def _place(out: Timeline, pin: Pin, bounds: Dict[int, Tuple[float, float]], min_clip: float, allow_move: bool) -> Optional[List[Decision]]:
    """The decisions for ``pin``; None when it needs a move and ``allow_move`` is false (try it again in the second phase)."""
    chosen, reason = _choose(out, pin)
    if chosen is None:
        return [Decision("editor", "pin_unplaced", [], reason, None, pin.to_dict())]
    a, b = chosen
    adjacent = _index(out.clips, b) == _index(out.clips, a) + 1
    if not adjacent and not allow_move:
        return None
    # The room check comes before any move: a pin that cannot be held must leave the order alone.
    a_lo = a.source_in + min_clip
    a_hi = bounds.get(a.shot_index, (a.source_in, a.source_out))[1]
    b_lo = bounds.get(b.shot_index, (b.source_in, b.source_out))[0]
    b_hi = b.source_out - min_clip
    if a_lo > a_hi or b_lo > b_hi:
        return [Decision("editor", "pin_unplaced", [a.id, b.id], "not enough room in these clips for the pinned points", None, pin.to_dict())]
    decisions: List[Decision] = []
    if not adjacent:
        moved, reason = _move_next_to(out, a, b, pin)
        if moved is None:
            return [Decision("editor", "pin_unplaced", [], reason, None, pin.to_dict())]
        decisions.append(moved)
    out_t = min(max(_snap(pin.out_time, out.fps), a_lo), a_hi)
    in_t = min(max(_snap(pin.in_time, out.fps), b_lo), b_hi)
    a.source_out, b.source_in = round(out_t, 3), round(in_t, 3)
    a.locked_out = b.locked_in = True
    for c in (a, b):
        if "pinned" not in c.tags:
            c.tags.append("pinned")
    detail = f"{pin.origin} by the editor: out {a.source_out:.2f}s, in {b.source_in:.2f}s"
    decisions.append(Decision("editor", "pinned", [a.id, b.id], detail, None, dict(pin.to_dict(), out=a.source_out, in_=b.source_in)))
    return decisions


def apply_pins(tl: Timeline, pins: Sequence[Pin], bounds: Dict[int, Tuple[float, float]], min_clip: float) -> Tuple[Timeline, List[Decision]]:
    """A copy of ``tl`` with every placeable pin applied and locked, plus the decisions taken.

    Two phases, each in store order: first every pin whose chosen clips are already adjacent (so nothing a later pin
    moves can break them), then the pins that need a move.
    """
    out = tl.copy()
    decisions: List[Decision] = []
    pending = list(pins)
    for allow_move in (False, True):
        later: List[Pin] = []
        for pin in pending:
            placed = _place(out, pin, bounds, min_clip, allow_move)
            if placed is None:
                later.append(pin)
            else:
                decisions.extend(placed)
        pending = later
    out.history.extend(decisions)
    return out, decisions


def pin_outcome(tl: Timeline, pin: Pin) -> Optional[Decision]:
    """What the latest run did with ``pin``: the editor's latest decision in ``tl`` for the same two shots at the same points."""
    for d in reversed(tl.history):
        if d.pass_name != "editor" or d.data.get("out_shot") != pin.out_shot or d.data.get("in_shot") != pin.in_shot:
            continue
        try:
            same = round(float(d.data["out_time"]), 3) == round(pin.out_time, 3) and round(float(d.data["in_time"]), 3) == round(pin.in_time, 3)
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if same:
            return d
    return None


def pin_constraints(pins: Sequence[Pin]) -> List[str]:
    return [f"keep shot {p.out_shot} immediately followed by shot {p.in_shot} (an editor-locked junction)" for p in pins]
