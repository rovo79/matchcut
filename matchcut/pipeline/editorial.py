"""Editorial pins: junctions the editor has locked or reverted, kept across regenerations.

A pin names a junction by its two shots (clip ids change when a cut is
regenerated; shots do not) and stores the cut points to hold. Pins are applied
to the rough cut before any pass runs: both ends of the junction are locked,
the passes leave locked ends alone, and pacing never drops a pinned clip. The
clips are the clip instances the pin remembers (their ``origin``, which travels through every pass); a pin
without them, or whose instance is gone, takes the piece whose boundary is nearest its point, and is left unplaced
(never guessed) when two pieces fit about equally well. Selection reads the rough cut's ranges as they were before
any pin was applied. When they are not adjacent in the rough cut (a later pass would have joined
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

from .timeline import Clip, Decision, Timeline, clip_origin

log = logging.getLogger(__name__)


@dataclass
class Pin:
    out_shot: int
    in_shot: int
    out_time: float
    in_time: float
    origin: str = "locked"  # "locked": keep the inspected points; "reverted": back to the first cut's points
    note: str = ""
    out_origin: str = ""  # clip_origin of the outgoing / incoming clip when the pin was taken; "" for pins that predate it
    in_origin: str = ""

    @property
    def key(self) -> Tuple[int, int]:
        return (self.out_shot, self.in_shot)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Pin":
        return cls(int(d["out_shot"]), int(d["in_shot"]), float(d["out_time"]), float(d["in_time"]), str(d.get("origin", "locked")), str(d.get("note", "")),
                   str(d.get("out_origin", "")), str(d.get("in_origin", "")))


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


# A rough-cut clip's range as it was before any pin touched it: id -> (source_in, source_out, clip_origin).
Snapshot = Dict[str, Tuple[float, float, str]]

AMBIGUITY_SECONDS = 0.5  # the nearest piece must be at least this much nearer than the runner-up, else the pin is not placed


def _pick(candidates: Sequence[Clip], snap: Snapshot, shot: int, t: float, origin: str, incoming: bool) -> Tuple[Optional[Clip], str]:
    """The candidate a pin side selects, or (None, the reason). Lineage first, then the nearest boundary."""
    if not candidates:
        return None, ""
    if origin:
        same = [c for c in candidates if snap[c.id][2] == origin]
        if len(same) == 1:
            return same[0], ""
    if len(candidates) == 1:
        return candidates[0], ""
    # the outgoing side holds the clip's out-point, the incoming side its in-point
    ranked = sorted(((abs(t - snap[c.id][0 if incoming else 1]), k) for k, c in enumerate(candidates)))
    if ranked[1][0] - ranked[0][0] < AMBIGUITY_SECONDS - 1e-9:
        return None, f"shot {shot} is ambiguous: more than one piece fits the pinned point"
    return candidates[ranked[0][1]], ""


def _choose(out: Timeline, pin: Pin, snap: Snapshot) -> Tuple[Optional[Tuple[Clip, Clip]], str]:
    """The (outgoing, incoming) clips the pin selects, two distinct clips; or (None, reason). Ranges come from ``snap``."""
    outs = [c for c in out.clips if c.shot_index == pin.out_shot]
    if not outs:
        return None, f"shot {pin.out_shot} is not in this cut"
    a, reason = _pick(outs, snap, pin.out_shot, pin.out_time, pin.out_origin, incoming=False)
    if a is None:
        return None, reason
    ins = [c for c in out.clips if c.shot_index == pin.in_shot and c is not a]  # a jump cut: two different pieces of one shot
    if not ins:
        return None, f"shot {pin.in_shot} has only one clip in this cut" if pin.in_shot == pin.out_shot else f"shot {pin.in_shot} is not in this cut"
    b, reason = _pick(ins, snap, pin.in_shot, pin.in_time, pin.in_origin, incoming=True)
    if b is None:
        return None, reason
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


def _place(out: Timeline, pin: Pin, snap: Snapshot, bounds: Dict[int, Tuple[float, float]], min_clip: float, allow_move: bool) -> Optional[List[Decision]]:
    """The decisions for ``pin``; None when it needs a move and ``allow_move`` is false (try it again in the second phase)."""
    chosen, reason = _choose(out, pin, snap)
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
    snap: Snapshot = {c.id: (c.source_in, c.source_out, clip_origin(c)) for c in out.clips}  # phase one clamps ranges; selection must not see that
    decisions: List[Decision] = []
    pending = list(pins)
    for allow_move in (False, True):
        later: List[Pin] = []
        for pin in pending:
            placed = _place(out, pin, snap, bounds, min_clip, allow_move)
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
