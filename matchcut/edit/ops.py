"""The nine edit operations: pure, frame-native functions of a timeline and one operation.

Every operation works on a copy of the timeline (``apply_op`` never changes its input) and either returns the new timeline, the ids of the
clips it changed (``touched``) and one line saying what it did, or raises ``Refused`` with one line saying why not.  Counts are whole
frames at the source's exact rate; points are stored with ``EditContext.s``.

Addressing:
- a clip by its id (stable for a session: new clips are ``e<n>``, nothing is renumbered) or its instance key (``clip_origin``);
- a junction (the cut between clips i and i+1) by its 1-based number or its id (``s1-s3``; a repeated shot pair ``s1-s3#2``).

Each operation checks what it can see locally: locked edges (an editor pin or a match cut), shot bounds, the minimum clip length (a clip the
pipeline left under it may grow, never shrink further), and, for ``set_transition``, the transition rules.  The proposal-wide rules
(``rules.problems``) check the whole result, whichever operation caused a change.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import AbstractSet, Any, Callable, Collection, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from matchcut.interchange.common import junction_ids
from matchcut.pipeline.editorial import Pin, pin_outcome, pin_site
from matchcut.pipeline.passes import is_match_cut
from matchcut.pipeline.timeline import HUMAN_STAGE, TRANSITION_KINDS, Clip, Timeline, Transition, clip_origin

from .model import EditContext, Refused, frame_rows

OPS = ("trim", "roll", "slip", "replace", "insert", "remove", "move", "set_transition", "snap_to_beat")

# the arguments each operation takes (besides "op"); anything else is refused, so a misspelt optional argument is never silently ignored
ARGS: Dict[str, Tuple[str, ...]] = {
    "trim": ("clip", "edge", "frames"),
    "roll": ("junction", "frames"),
    "slip": ("clip", "frames"),
    "replace": ("clip", "shot", "in_frame", "frames"),
    "insert": ("shot", "in_frame", "out_frame", "before", "after"),
    "remove": ("clip",),
    "move": ("clip", "before", "after"),
    "set_transition": ("junction", "kind", "frames"),
    "snap_to_beat": ("junction", "mode", "window_s"),
}

_REQUIRED = object()


@dataclass(frozen=True)
class OpResult:
    timeline: Timeline
    touched: FrozenSet[str]  # ids of the clips the operation changed, added or removed
    detail: str  # one line
    issued: Tuple[str, ...] = ()  # ids of the new clips the operation made (replace, insert), sorted


@dataclass(frozen=True)
class OpsResult:
    timeline: Timeline
    touched: FrozenSet[str]
    details: Tuple[str, ...]  # one line per operation
    issued: Tuple[str, ...] = ()  # every id a new clip took in the batch, sorted (also one a later operation removed again)


MAX_FRAMES = 10 ** 9  # a frame count beyond this either way is refused (no cut is that long; it keeps the arithmetic finite)
MAX_SHOT = 10 ** 6  # a shot index beyond this either way is refused (no workspace has that many shots)


# ----------------------------------------------------------------------------------------------- arguments and addressing
def _shown(v: Any) -> str:
    """A number for an out-of-range message: in full when short, else in powers of ten (a huge int cannot be formatted as a float, and
    Python refuses to write an int of more than 4300 digits)."""
    if isinstance(v, int) and abs(v) >= 10 ** 15:
        return f"~{'-' if v < 0 else ''}1e{int(math.log10(abs(v)))}"
    return str(int(v)) if abs(v) < 1e15 else f"{v:g}"


def whole(op: Dict[str, Any], key: str, default: Any = _REQUIRED, limit: int = MAX_FRAMES, unit: str = "frames") -> int:
    """A whole-number argument: an int, or a float with no fraction, of at most ``limit`` (``unit``) either way.  A bool, a fractional or
    non-numeric value is refused, never truncated; a missing one is refused unless there is a ``default``."""
    if key not in op:
        if default is _REQUIRED:
            raise Refused(f"{op.get('op', 'the operation')} needs {key}")
        return default
    v = op[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise Refused(f"{key} must be a whole number, not {v!r}")
    if isinstance(v, float) and not (math.isfinite(v) and v.is_integer()):
        raise Refused(f"{key} must be a whole number, not {v!r}")
    if abs(v) > limit:
        shown = _shown(v)
        raise Refused(f"{key} is out of range: at most {limit} {unit} either way, not {shown[:12]}{'...' if len(shown) > 12 else ''}")
    return int(v)


def _ref(op: Dict[str, Any], key: str) -> Any:
    if key not in op or op[key] is None:
        raise Refused(f"{op.get('op', 'the operation')} needs {key}")
    return op[key]


def find_clip(tl: Timeline, ref: Any) -> Clip:
    """The clip whose id is ``ref``, else the one clip whose ``clip_origin`` is ``ref``; Refused when there is none or more than one."""
    if not isinstance(ref, str) or not ref:
        raise Refused(f"a clip is named by its id or its key (a string), not {ref!r}")
    hits = [c for c in tl.clips if c.id == ref] or [c for c in tl.clips if clip_origin(c) == ref]
    if not hits:
        raise Refused(f"no clip {ref}")
    if len(hits) > 1:
        raise Refused(f"clip {ref} is ambiguous ({', '.join(c.id for c in hits)}): name it by its id")
    return hits[0]


def find_junction(tl: Timeline, ref: Any) -> int:
    """The 0-based index i of the cut between clips i and i+1, from a 1-based number (an int, an integral float or a digit string) or a junction id
    (``s1-s3``, ``s1-s3#2`` as ``junction_ids`` gives them); Refused when there is no such junction."""
    n = len(tl.clips) - 1
    if isinstance(ref, bool):
        raise Refused(f"a junction is named by its number or its id, not {ref!r}")
    if isinstance(ref, float) and math.isfinite(ref) and ref.is_integer() and abs(ref) <= MAX_FRAMES:
        ref = int(ref)  # 2.0 names junction 2, as ``whole`` reads frame counts (JSON Schema's integers include it)
    if isinstance(ref, str) and ref.isascii() and ref.isdigit() and len(ref) > 9:
        raise Refused(f"no junction {ref[:12]}{'...' if len(ref) > 12 else ''}: this cut has {n} junction{'s' if n != 1 else ''}")
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isascii() and ref.isdigit()):
        k = int(ref)
        if not 1 <= k <= n:
            raise Refused(f"no junction {k}: this cut has {n} junction{'s' if n != 1 else ''}" + (f" (1..{n})" if n > 0 else ""))
        return k - 1
    if not isinstance(ref, str):
        raise Refused(f"a junction is named by its number or its id, not {ref!r}")
    ids = junction_ids(tl)
    if ref in ids:
        return ids.index(ref)
    shown = ", ".join(ids[:12]) + (", ..." if len(ids) > 12 else "")
    raise Refused(f"no junction {ref} (junctions: {shown or 'none'})")


# ----------------------------------------------------------------------------------------------- locks and pins
def lock_reason(c: Clip, edge: str) -> str:
    """Why the ``edge`` ("in" / "out") of ``c`` may not move, in one line, or "" when it may."""
    if not (c.locked_in if edge == "in" else c.locked_out):
        return ""
    if any(t.startswith("match_in:" if edge == "in" else "match_out:") for t in c.tags):
        return f"the {edge}-point of {c.id} is held by a match cut"
    if "pinned" in c.tags:
        return f"the {edge}-point of {c.id} is held by an editor pin"
    return f"the {edge}-point of {c.id} is locked"


def _unlocked(c: Clip, *edges: str) -> None:
    for edge in edges:
        why = lock_reason(c, edge)
        if why:
            raise Refused(why)


def held_pins(tl: Timeline, ctx: EditContext) -> List[Tuple[Pin, int]]:
    """The editor pins ``tl`` holds (``pin_outcome`` on the human stage's terms: the pin was applied and the cut still keeps its clip
    instances, their adjacency and both cut points), each with its junction index (``pin_site``)."""
    out: List[Tuple[Pin, int]] = []
    for pin in ctx.pins:
        d = pin_outcome(tl, pin, HUMAN_STAGE)
        if d is not None and d.action == "pinned":
            site = pin_site(tl, pin)
            if site is not None:
                out.append((pin, site))
    return out


def pin_label(pin: Pin) -> str:
    return f"s{pin.out_shot}->s{pin.in_shot}"


Tr = Tuple[str, int]  # a junction's transition: (kind, frames), ("cut", 0) for a cut


def as_transition(kind: Any, frames: Any) -> Tr:
    """(kind, whole frames), with a transition of no frames read as ("cut", 0)."""
    n = int(frames or 0)
    return ("cut", 0) if not n else (str(kind), n)


def held_transition(pin: Pin, current: Tr) -> Tr:
    """The transition a protecting pin holds: its stored ``held_transition``, else (none stored) ``current``, the junction's transition in
    the timeline the pin is checked against."""
    h = pin.held_transition
    return as_transition(h.get("kind"), h.get("frames")) if isinstance(h, dict) else as_transition(*current)


def protected_transition_change(pin: Pin, was: Tr, now: Tr) -> Optional[str]:
    """Whether the change of a junction's transition from ``was`` to ``now`` is refused at an editor pin that holds that junction; one line
    when it is, None when it is allowed.  The one rule ``ops`` (``set_transition``), ``rules.pin_problems`` (every proposal) and ``publish``
    (``was`` is then r000's) share.

    Only a new or worse violation counts (decision 2): an unprotected pin allows any change; a protected one (``hold_transition``) allows
    the transition to stay as it was, or to become exactly the transition the pin holds (a repair, when the two differ, e.g. a protection
    set with an explicit transition, or a session restarted on a stage the pipeline built ignoring the protection).  Any other change is
    refused."""
    was, now = as_transition(*was), as_transition(*now)
    if not pin.hold_transition or now == was:
        return None
    held = held_transition(pin, was)
    if now == held:
        return None
    line = f"changes the transition the editor pin {pin_label(pin)} protects ({_label(*was)} -> {_label(*now)})"
    return line if held == was else line[:-1] + f"; the pin holds a {_label(*held)}, the only other transition allowed there)"


# ----------------------------------------------------------------------------------------------- helpers
def _frames(ctx: EditContext, c: Clip) -> Tuple[int, int]:
    return ctx.f(c.source_in), ctx.f(c.source_out)


def _set(ctx: EditContext, c: Clip, in_f: int, out_f: int, new: bool = False) -> None:
    """Give ``c`` the frames ``in_f``-``out_f``.

    The range must lie inside the clip's shot (``EditContext.shot_frames``, so M2a's export never moves a point an operation placed), and be
    at least the minimum clip length.  A clip the pipeline already left a little outside its shot, or under the minimum, is tolerated as
    it is: an edge that stays where it was may stay outside, an edge that moves must land inside the shot, and the clip may grow but not
    shrink further.  A new clip (``new``) is held to the rules strictly.  Only an edge that moves is written."""
    if not ctx.has_shot(c.shot_index):
        raise Refused(f"{c.id}: no shot {c.shot_index}")
    lo, hi = ctx.shot_frames(c.shot_index)
    old_in, old_out = (in_f, out_f) if new else _frames(ctx, c)
    for edge, frame, was, bad in (("in", in_f, old_in, in_f < lo), ("out", out_f, old_out, out_f > hi)):
        if bad and not new and frame != was and (was < lo if edge == "in" else was > hi) and \
                (frame > was if edge == "in" else frame < was):
            raise Refused(f"{c.id}'s {edge}-point would still lie outside shot {c.shot_index} at frame {frame} (the shot is frames {lo}-{hi}): "
                          f"an edge that moves must land inside it, at frame {lo if edge == 'in' else hi} or {'later' if edge == 'in' else 'earlier'}")
    if (in_f < lo and (new or in_f != old_in)) or (out_f > hi and (new or out_f != old_out)):
        raise Refused(f"{c.id} would leave shot {c.shot_index} (frames {in_f}-{out_f}; the shot is frames {lo}-{hi})")
    if out_f <= in_f:
        raise Refused(f"{c.id} would have no frames ({in_f}-{out_f})")
    floor = ctx.min_frames if new else min(ctx.min_frames, old_out - old_in)
    if out_f - in_f < floor:
        raise Refused(f"{c.id} would be {out_f - in_f} frames, below the minimum clip length ({ctx.min_frames} frames)")
    if new or in_f != old_in:
        c.source_in = ctx.s(in_f)
    if new or out_f != old_out:
        c.source_out = ctx.s(out_f)


def new_clip_id(tl: Timeline, reserved: Collection[str] = ()) -> str:
    """The first ``e<n>`` (``e001``, ``e002`` ...) that no clip of ``tl`` has, that no decision of its history names, and that is not in
    ``reserved`` (every id the session has ever issued or used): an id is never given to a second clip."""
    taken = {c.id for c in tl.clips} | set(reserved)
    for d in tl.history:
        taken.update(d.clip_ids)
    n = 1
    while f"e{n:03d}" in taken:
        n += 1
    return f"e{n:03d}"


def _new_clip(tl: Timeline, ctx: EditContext, shot: int, in_f: int, out_f: int, beat_id: str, purpose: str,
              reserved: Collection[str] = ()) -> Clip:
    if not ctx.has_shot(shot):
        raise Refused(f"no shot {shot}")
    c = Clip(id=new_clip_id(tl, reserved), shot_index=shot, source_in=0.0, source_out=0.0, beat_id=beat_id, purpose=purpose)
    _set(ctx, c, in_f, out_f, new=True)
    key = f"edit:{shot}@{in_f}-{out_f}"
    taken = {clip_origin(x) for x in tl.clips}
    origin, k = key, 2
    while origin in taken:
        origin, k = f"{key}#{k}", k + 1
    c.origin = origin
    return c


def _where(tl: Timeline, op: Dict[str, Any]) -> Tuple[str, Clip]:
    """("before" | "after", the anchor clip) from exactly one of the two arguments."""
    given = [k for k in ("before", "after") if op.get(k) is not None]
    if len(given) != 1:
        raise Refused(f"{op.get('op')} needs exactly one of before / after (a clip)")
    return given[0], find_clip(tl, op[given[0]])


def _put(tl: Timeline, c: Clip, side: str, anchor: Clip) -> None:
    k = tl.clips.index(anchor)
    tl.clips.insert(k + (1 if side == "after" else 0), c)


def _first_is_cut(tl: Timeline) -> Set[str]:
    """The first clip of a cut has no incoming transition: one a reorder left there becomes a cut.  The ids this changed."""
    if tl.clips and tl.clips[0].transition_in != Transition():
        tl.clips[0].transition_in = Transition()
        return {tl.clips[0].id}
    return set()


def _label(kind: str, frames: int) -> str:
    return "cut" if not frames else f"{kind} {frames}f"


def _repair_args(t: Tr) -> str:
    return 'kind "cut"' if not t[1] else f'kind "{t[0]}", frames {t[1]}'


def transition_limit(ctx: EditContext, a_frames: int, b_frames: int) -> Fraction:
    """The longest transition (in frames, exact) allowed between clips of these lengths: ``max_fraction_of_clip`` of the shorter."""
    return ctx.max_fraction * min(a_frames, b_frames)


# ----------------------------------------------------------------------------------------------- the operations
# each edits ``tl`` (a copy) in place and returns (ids of the clips it changed, one line), or raises Refused

def _trim(tl: Timeline, op: Dict[str, Any], ctx: EditContext) -> Tuple[Set[str], str]:
    """Move one edge of a clip by ``frames`` (positive = later in the source); later clips ripple."""
    c = find_clip(tl, _ref(op, "clip"))
    edge = _ref(op, "edge")
    if edge not in ("in", "out"):
        raise Refused(f"edge must be in or out, not {edge!r}")
    n = whole(op, "frames")
    _unlocked(c, edge)
    i, o = _frames(ctx, c)
    _set(ctx, c, i + n if edge == "in" else i, o + n if edge == "out" else o)
    return {c.id}, f"trim {c.id} {edge} {n:+d}f"


def _roll(tl: Timeline, op: Dict[str, Any], ctx: EditContext) -> Tuple[Set[str], str]:
    """Move a cut point by ``frames``: the outgoing clip's out-point and the incoming clip's in-point together; nothing ripples."""
    j = find_junction(tl, _ref(op, "junction"))
    n = whole(op, "frames")
    a, b = tl.clips[j], tl.clips[j + 1]
    _unlocked(a, "out")
    _unlocked(b, "in")
    ai, ao = _frames(ctx, a)
    bi, bo = _frames(ctx, b)
    _set(ctx, a, ai, ao + n)
    _set(ctx, b, bi + n, bo)
    return {a.id, b.id}, f"roll junction {j + 1} ({a.id}|{b.id}) {n:+d}f"


def _slip(tl: Timeline, op: Dict[str, Any], ctx: EditContext) -> Tuple[Set[str], str]:
    """Show other frames of the same shot for the same length: both edges move by ``frames``."""
    c = find_clip(tl, _ref(op, "clip"))
    n = whole(op, "frames")
    _unlocked(c, "in", "out")
    i, o = _frames(ctx, c)
    _set(ctx, c, i + n, o + n)
    return {c.id}, f"slip {c.id} {n:+d}f"


def _replace(tl: Timeline, op: Dict[str, Any], ctx: EditContext, reserved: AbstractSet[str] = frozenset()) -> Tuple[Set[str], str]:
    """Put footage of another shot in a clip's place: ``frames`` long (default: the same length), from ``in_frame`` (default: the middle of
    the shot).  The new clip keeps the old one's incoming transition and story beat."""
    old = find_clip(tl, _ref(op, "clip"))
    shot = whole(op, "shot", limit=MAX_SHOT, unit="(a shot index)")
    _unlocked(old, "in", "out")
    if not ctx.has_shot(shot):
        raise Refused(f"no shot {shot}")
    oi, oo = _frames(ctx, old)
    length = whole(op, "frames", oo - oi)
    if length < 1:
        raise Refused(f"frames must be at least 1, not {length}")
    lo, hi = ctx.shot_frames(shot)
    if "in_frame" in op:
        start = whole(op, "in_frame")
    else:
        if hi - lo < length:
            raise Refused(f"shot {shot} has {hi - lo} frames, fewer than the {length} needed")
        start = lo + (hi - lo - length) // 2
    new = _new_clip(tl, ctx, shot, start, start + length, old.beat_id, f"replaces {old.id}", reserved)
    new.transition_in = Transition(old.transition_in.kind, old.transition_in.duration)
    tl.clips[tl.clips.index(old)] = new
    touched = {old.id, new.id} | _first_is_cut(tl)
    return touched, f"replace {old.id} (shot {old.shot_index}) with {new.id} (shot {shot}, frames {start}-{start + length})"


def _insert(tl: Timeline, op: Dict[str, Any], ctx: EditContext, reserved: AbstractSet[str] = frozenset()) -> Tuple[Set[str], str]:
    """A new clip of ``shot`` (frames ``in_frame``-``out_frame``) before or after a clip, with a cut.  The clip that follows it keeps its own
    incoming transition."""
    shot, i, o = whole(op, "shot", limit=MAX_SHOT, unit="(a shot index)"), whole(op, "in_frame"), whole(op, "out_frame")
    side, anchor = _where(tl, op)
    new = _new_clip(tl, ctx, shot, i, o, anchor.beat_id, "inserted", reserved)
    _put(tl, new, side, anchor)
    return {new.id} | _first_is_cut(tl), f"insert {new.id} (shot {shot}, frames {i}-{o}) {side} {anchor.id}"


def _remove(tl: Timeline, op: Dict[str, Any], ctx: EditContext) -> Tuple[Set[str], str]:
    """Take a clip out; later clips ripple.  The clip that followed it keeps its own incoming transition."""
    c = find_clip(tl, _ref(op, "clip"))
    if len(tl.clips) <= 1:
        raise Refused("cannot remove the only clip")
    tl.clips.remove(c)
    return {c.id} | _first_is_cut(tl), f"remove {c.id} (shot {c.shot_index})"


def _move(tl: Timeline, op: Dict[str, Any], ctx: EditContext) -> Tuple[Set[str], str]:
    """Move a clip before or after another; it keeps its incoming transition (a cut when it becomes the first clip)."""
    c = find_clip(tl, _ref(op, "clip"))
    side, anchor = _where(tl, op)
    if anchor is c:
        raise Refused(f"cannot move {c.id} {side} itself")
    order = [x.id for x in tl.clips]
    tl.clips.remove(c)
    _put(tl, c, side, anchor)
    same = [x.id for x in tl.clips] == order
    return {c.id} | _first_is_cut(tl), f"move {c.id} {side} {anchor.id}" + (" (it is there already)" if same else "")


def _set_transition(tl: Timeline, op: Dict[str, Any], ctx: EditContext) -> Tuple[Set[str], str]:
    """Make a junction a cut, or a dissolve / dip to black of ``frames``.  A match cut stays a hard cut; a transition an editor pin protects
    (``hold_transition``) stays, or becomes exactly the one the pin holds (``protected_transition_change``); the length is at most
    ``max_fraction_of_clip`` of the shorter clip (one already over it may only get shorter)."""
    j = find_junction(tl, _ref(op, "junction"))
    kind = _ref(op, "kind")
    if kind not in TRANSITION_KINDS:
        raise Refused(f"unknown transition {kind!r} (one of: {', '.join(TRANSITION_KINDS)})")
    if kind == "cut":
        n = whole(op, "frames", 0)
        if n:
            raise Refused(f"a cut has no frames (frames {n})")
    else:
        n = whole(op, "frames")
        if n < 1:
            raise Refused(f"a {kind} needs at least 1 frame, not {n}")
    a, b = tl.clips[j], tl.clips[j + 1]
    rows = frame_rows(tl, ctx.rate)
    cur = (rows[j + 1].kind, rows[j + 1].tr)
    new = (kind if n else "cut", n)
    if new == cur:
        return set(), f"junction {j + 1} ({a.id}|{b.id}) is already {_label(*cur)}"
    if n and is_match_cut(a, b):
        raise Refused(f"junction {j + 1} ({a.id}|{b.id}) is a match cut: match cuts stay hard cuts")
    for pin, site in held_pins(tl, ctx):
        if site == j and protected_transition_change(pin, cur, new) is not None:
            held = held_transition(pin, cur)
            only = (f": it holds the {_label(*held)} there now" if held == cur else
                    f": it holds a {_label(*held)}, so only set_transition to that ({_repair_args(held)}) is allowed there")
            raise Refused(f"the transition at junction {j + 1} ({a.id}|{b.id}) is protected by the editor pin {pin_label(pin)}{only}")
    limit = transition_limit(ctx, rows[j].length, rows[j + 1].length)
    if n > limit and n > cur[1]:
        raise Refused(f"a {n}-frame {kind} at junction {j + 1} ({a.id}|{b.id}) is longer than {float(ctx.max_fraction):.0%} of the shorter clip "
                      f"({math.floor(limit)} frames)")
    b.transition_in = Transition(kind, ctx.s(n)) if n else Transition()
    return {b.id}, f"junction {j + 1} ({a.id}|{b.id}): {_label(*cur)} -> {_label(*new)}"


def beat_frame(beat: float, ctx: EditContext) -> int:
    """The record frame a music beat (seconds into the track) falls on: the frame grid's nearest frame to ``beat - music_start``.  A junction
    is on the beat when its record frame is this frame; snapping puts it there, so a snapped junction is on its beat and stays put."""
    return ctx.f(beat - ctx.music_start)


def beat_target(tl: Timeline, j: int, ctx: EditContext, window: Optional[float] = None) -> Tuple[Optional[float], bool, int]:
    """(the beat junction ``j`` would snap to, in seconds of the track, or None; whether it is a downbeat; the junction's record frame).

    As the beat-sync pass: at a story-beat change, with ``prefer_downbeats``, the nearest downbeat within the window wins; otherwise the
    nearest beat within the window."""
    rec = frame_rows(tl, ctx.rate)[j + 1].rec
    music = ctx.music
    if music is None:
        return None, False, rec
    window = ctx.snap_window if window is None else window
    track_t = float(Fraction(rec) / ctx.rate) + ctx.music_start
    if ctx.prefer_downbeats and tl.clips[j].beat_id != tl.clips[j + 1].beat_id:
        db = music.nearest_beat(track_t, downbeats=True)
        if db is not None and abs(db - track_t) <= window + 1e-9:
            return db, True, rec
    b = music.nearest_beat(track_t)
    if b is not None and abs(b - track_t) <= window + 1e-9:
        return b, False, rec
    return None, False, rec


def _snap_to_beat(tl: Timeline, op: Dict[str, Any], ctx: EditContext) -> Tuple[Set[str], str]:
    """Put a junction on the nearest music beat within ``window_s`` (default ``music.snap_window``): by a roll (default; nothing else moves)
    or, with ``mode: ripple``, by trimming the outgoing clip's out-point (later clips ripple)."""
    if ctx.music is None:
        raise Refused("this workspace has no music track")
    j = find_junction(tl, _ref(op, "junction"))
    mode = op.get("mode", "roll")
    if mode not in ("roll", "ripple"):
        raise Refused(f"mode must be roll or ripple, not {mode!r}")
    window = op.get("window_s", ctx.snap_window)
    if isinstance(window, bool) or not isinstance(window, (int, float)) or not math.isfinite(window) or window < 0:
        raise Refused(f"window_s must be a number of seconds, not {window!r}")
    target, downbeat, rec = beat_target(tl, j, ctx, float(window))
    what = "downbeat" if downbeat else "beat"
    if target is None:
        raise Refused(f"no beat within {float(window):.2f}s of junction {j + 1}")
    n = beat_frame(target, ctx) - rec
    a, b = tl.clips[j], tl.clips[j + 1]
    if n == 0:
        return set(), f"junction {j + 1} ({a.id}|{b.id}) is already on the {what} at {target:.3f}s"
    if mode == "roll":
        ids, _ = _roll(tl, {"op": "roll", "junction": j + 1, "frames": n}, ctx)
    else:
        ids, _ = _trim(tl, {"op": "trim", "clip": a.id, "edge": "out", "frames": n}, ctx)
    return ids, f"junction {j + 1} ({a.id}|{b.id}) {n:+d}f onto the {what} at {target:.3f}s ({mode})"


_OPS: Dict[str, Callable[[Timeline, Dict[str, Any], EditContext], Tuple[Set[str], str]]] = {
    "trim": _trim, "roll": _roll, "slip": _slip, "replace": _replace, "insert": _insert, "remove": _remove, "move": _move,
    "set_transition": _set_transition, "snap_to_beat": _snap_to_beat,
}


_NEW_CLIPS = ("replace", "insert")  # the operations that issue a new clip id


def apply_op(tl: Timeline, op: Any, ctx: EditContext, reserved_ids: Collection[str] = ()) -> OpResult:
    """Apply one operation to a copy of ``tl``; ``tl`` is never changed.  Raises ``Refused`` (one line) when it cannot be applied.

    ``reserved_ids`` are ids a new clip may not take: every id the session has ever issued or used (the session keeps them), so an id never
    names two different clips across revisions."""
    if not isinstance(op, dict):
        raise Refused(f"an operation is an object with an \"op\", not {op!r}")
    name = op.get("op")
    if not isinstance(name, str) or name not in _OPS:
        raise Refused(f"unknown op {name!r} (one of: {', '.join(OPS)})")
    extra = sorted(str(k) for k in op if k != "op" and k not in ARGS[name])
    if extra:
        raise Refused(f"{name} does not take {', '.join(extra)} (it takes {', '.join(ARGS[name])})")
    out = tl.copy()
    if name in _NEW_CLIPS:
        touched, detail = _OPS[name](out, op, ctx, frozenset(reserved_ids))  # type: ignore[call-arg]
    else:
        touched, detail = _OPS[name](out, op, ctx)
    issued = tuple(sorted({c.id for c in out.clips} - {c.id for c in tl.clips})) if name in _NEW_CLIPS else ()
    return OpResult(out, frozenset(touched), detail, issued)


def apply_ops(tl: Timeline, ops: Sequence[Any], ctx: EditContext, reserved_ids: Collection[str] = ()) -> OpsResult:
    """Apply ``ops`` in order to a copy of ``tl``, all or nothing: the first refusal is raised with its position (``Refused.index``) and
    nothing is returned.  The touched ids are the union over the operations.  An id used anywhere in the batch (or in ``reserved_ids``) is
    never issued again within it."""
    if isinstance(ops, (str, bytes, dict)) or not isinstance(ops, Sequence):
        raise Refused("ops must be a list of operations")
    reserved = set(reserved_ids) | {c.id for c in tl.clips}
    cur, touched, details, issued = tl, set(), [], set()  # type: Timeline, Set[str], List[str], Set[str]
    for k, op in enumerate(ops):
        name = op.get("op") if isinstance(op, dict) else None
        where = f"op {k + 1}{f' ({name[:40]})' if isinstance(name, str) else ''}"
        try:
            res = apply_op(cur, op, ctx, reserved)
        except Refused as exc:
            raise Refused(f"{where}: {exc}", k) from None
        except (TypeError, ValueError, OverflowError, ZeroDivisionError) as exc:  # malformed input no check above caught: a refusal
            raise Refused(f"{where}: malformed operation ({type(exc).__name__}: {str(exc)[:120]})", k) from None
        cur, details = res.timeline, details + [res.detail]
        touched |= res.touched
        issued |= set(res.issued)
        reserved |= {c.id for c in cur.clips}
    return OpsResult(cur if cur is not tl else tl.copy(), frozenset(touched), tuple(details), tuple(sorted(issued)))
