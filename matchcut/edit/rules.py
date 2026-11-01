"""Proposal-wide rules, the timing report, the change report and ``describe``: pure functions of timelines and an ``EditContext``.

The rules look at the whole result of a proposal, whichever operation caused a change, and count only what the ``before`` timeline did not
already have (a problem the pipeline left is reported by ``describe``, never refused; a proposal may not add one or make one worse):

- shot bounds and the minimum clip length, in frames; ``Timeline.validate`` (duplicate ids, unknown shots and transition kinds);
- transitions: whole frames, at most ``max_fraction_of_clip`` of the shorter of their two clips, and a cut M2a's ``layout`` accepts (every
  clip longer than its transitions);
- every editor pin ``before`` holds is still held (clip instances, adjacency, both cut points), and a transition it protects
  (``hold_transition``) is unchanged;
- every match cut of ``before`` keeps its two clips adjacent, its cut points and its hard cut;
- clips no operation touched are identical, and so is every junction between two of them, in frames;
- the proposal's guards (``max_duration_change_frames``, ``keep_record_frames``, ``keep_on_beat``).

Transitions overlap their two clips (a new dissolve moves every later record position earlier); ``timing`` reports it, nothing absorbs it.
"""
from __future__ import annotations

from collections import Counter
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from matchcut.interchange import InterchangeError
from matchcut.interchange.common import junction_ids, layout
from matchcut.pipeline.editorial import Pin, pin_outcome
from matchcut.pipeline.passes import is_match_cut
from matchcut.pipeline.timeline import HUMAN_STAGE, TRANSITION_KINDS, Clip, Timeline, clip_origin

from .model import EditContext, Refused, Row, frame_rows, running_frames
from .ops import MAX_FRAMES, beat_frame, find_junction, held_pins, lock_reason, pin_label, transition_limit

GUARDS = ("max_duration_change_frames", "keep_record_frames", "keep_on_beat")

Pair = Tuple[str, str]


# ----------------------------------------------------------------------------------------------- helpers
def _label(kind: str, frames: int) -> str:
    return "cut" if not frames else f"{kind} {frames}f"


def _junctions(tl: Timeline) -> Dict[Pair, int]:
    """(outgoing id, incoming id) -> junction index."""
    return {(tl.clips[i].id, tl.clips[i + 1].id): i for i in range(len(tl.clips) - 1)}


def _cut(rows: Sequence[Row], i: int) -> Tuple[int, int, str, int]:
    """Junction i in frames: (outgoing out-point, incoming in-point, transition kind, transition frames)."""
    return rows[i].out_f, rows[i + 1].in_f, rows[i + 1].kind, rows[i + 1].tr


def _shot_excess(ctx: EditContext, c: Clip, r: Row) -> Optional[Tuple[int, int, int]]:
    """(frames outside the shot, lo, hi), or None for an unknown shot."""
    if not ctx.has_shot(c.shot_index):
        return None
    lo, hi = ctx.shot_frames(c.shot_index)
    return max(0, lo - r.in_f) + max(0, r.out_f - hi), lo, hi


def _off_whole(ctx: EditContext, seconds: float) -> bool:
    x = Fraction(float(seconds)) * ctx.rate
    return abs(x - round(x)) > Fraction(1, 1000)


def beat_offset(tl: Timeline, rows: Sequence[Row], j: int, ctx: EditContext) -> Optional[int]:
    """How many frames junction ``j`` is after (+) or before (-) the nearest music beat, on the frame grid (0: its record frame is the beat's
    frame, ``ops.beat_frame``); None without music."""
    if ctx.music is None:
        return None
    rec = rows[j + 1].rec
    beat = ctx.music.nearest_beat(float(Fraction(rec) / ctx.rate) + ctx.music_start)
    return None if beat is None else rec - beat_frame(beat, ctx)


def _dedupe(items: Iterable[str]) -> List[str]:
    seen: set = set()
    return [x for x in items if not (x in seen or seen.add(x))]


# ----------------------------------------------------------------------------------------------- the rules
def pin_problems(before: Timeline, after: Timeline, ctx: EditContext) -> List[Tuple[Pin, str]]:
    """(pin, one line) for every editor pin of ``ctx`` that ``before`` holds and ``after`` no longer holds as it was: its two clips
    (within a session clip ids are stable, so the very two clips the pin holds in ``before``), their adjacency and both cut points, and
    the transition the pin protects (``hold_transition``).  ``pin_outcome`` alone would accept another clip of the same shot at the same
    point for a pin taken without clip instances."""
    out: List[Tuple[Pin, str]] = []
    rb, ra = frame_rows(before, ctx.rate), frame_rows(after, ctx.rate)
    ja = _junctions(after)
    present = {c.id for c in after.clips}
    for pin, site in held_pins(before, ctx):
        a, b = before.clips[site].id, before.clips[site + 1].id
        k = ja.get((a, b))
        if k is None:
            gone = [x for x in (a, b) if x not in present]
            why = f"{' and '.join(gone)} removed" if gone else f"its clips {a}|{b} are no longer adjacent"
            out.append((pin, f"breaks the editor pin {pin_label(pin)}: {why}"))
            continue
        if _cut(ra, k)[:2] != _cut(rb, site)[:2]:
            out.append((pin, f"breaks the editor pin {pin_label(pin)}: the cut {a}|{b} moved"))
            continue
        d = pin_outcome(after, pin, HUMAN_STAGE)
        if d is None or d.action != "pinned":
            out.append((pin, f"breaks the editor pin {pin_label(pin)}: {d.detail if d is not None and d.detail else 'the pin is lost'}"))
            continue
        if pin.hold_transition:
            t0 = (rb[site + 1].kind, rb[site + 1].tr)
            t1 = (ra[k + 1].kind, ra[k + 1].tr)
            if t0 != t1:
                out.append((pin, f"changes the transition the editor pin {pin_label(pin)} protects ({_label(*t0)} -> {_label(*t1)})"))
    return out


def problems(before: Timeline, after: Timeline, touched: Iterable[str], ctx: EditContext, guards: Optional[Dict[str, Any]] = None) -> List[str]:
    """What ``after`` (the result of a proposal on ``before``) breaks, one line each; [] when it may be applied.  ``touched`` are the ids of
    the clips the operations changed (``OpResult.touched``, united over the proposal)."""
    touched = set(touched)
    out: List[str] = []
    rb, ra = frame_rows(before, ctx.rate), frame_rows(after, ctx.rate)
    old = {c.id: (c, r) for c, r in zip(before.clips, rb)}

    # what Timeline.validate checks, compared clip by clip: duplicate ids, unknown shots, unknown transition kinds, shot bounds (in frames)
    # and the minimum length; only a problem the clip did not have before, or a worse one, counts
    count_b, count_a = Counter(c.id for c in before.clips), Counter(c.id for c in after.clips)
    out += [f"duplicate clip id {cid}" for cid, n in count_a.items() if n > 1 and n > count_b.get(cid, 0)]
    for c, r in zip(after.clips, ra):
        prev = old.get(c.id)
        if not ctx.has_shot(c.shot_index) and (prev is None or prev[0].shot_index != c.shot_index):
            out.append(f"{c.id}: unknown shot {c.shot_index}")
        kind = c.transition_in.kind
        if kind not in TRANSITION_KINDS and (prev is None or prev[0].transition_in.kind != kind):
            out.append(f"{c.id}: unknown transition {kind}")
        ex = _shot_excess(ctx, c, r)
        if ex is not None and ex[0]:
            was = _shot_excess(ctx, prev[0], prev[1]) if prev is not None and prev[0].shot_index == c.shot_index else None
            if was is None or ex[0] > was[0]:
                out.append(f"{c.id} lies outside shot {c.shot_index} (frames {r.in_f}-{r.out_f}; the shot is frames {ex[1]}-{ex[2]})")
        if r.length < ctx.min_frames and (prev is None or r.length < prev[1].length):
            out.append(f"{c.id} is {r.length} frames, below the minimum clip length ({ctx.min_frames} frames)")

    # transitions: the first clip has none; whole frames; at most max_fraction_of_clip of the shorter clip; the layout accepts the cut
    if after.clips and not after.clips[0].transition_in.is_cut and not (before.clips and before.clips[0].id == after.clips[0].id
                                                                         and not before.clips[0].transition_in.is_cut):
        out.append(f"the first clip {after.clips[0].id} has an incoming {after.clips[0].transition_in.kind}")
    for c in after.clips[1:]:
        t = c.transition_in
        if not t.is_cut and _off_whole(ctx, t.duration):
            prev = old.get(c.id)
            if prev is None or prev[0].transition_in != t:
                out.append(f"the {t.kind} into {c.id} is not a whole number of frames ({t.duration}s)")
    jb, ja = _junctions(before), _junctions(after)
    for (a, b), i in ja.items():
        tr = ra[i + 1].tr
        if not tr:
            continue
        limit = transition_limit(ctx, ra[i].length, ra[i + 1].length)
        excess = tr - limit
        if excess <= 0:
            continue
        k = jb.get((a, b))
        was = (rb[k + 1].tr - transition_limit(ctx, rb[k].length, rb[k + 1].length)) if k is not None else Fraction(0)
        if excess > max(was, Fraction(0)):
            out.append(f"the {tr}-frame {ra[i + 1].kind} at {a}|{b} is longer than {float(ctx.max_fraction):.0%} of the shorter clip "
                       f"({int(limit)} frames)")
    short = False
    for c, r in zip(after.clips, ra):
        if r.visible >= 1:
            continue
        prev = old.get(c.id)
        if prev is None or prev[1].visible >= 1 or r.visible < prev[1].visible:
            short = True
            out.append(f"{c.id} is no longer than its transitions ({r.length} frames, {r.head + r.tail} of them in transitions): "
                       "the cut cannot be laid out")
    if not short:
        try:
            layout(after, ctx.rate)
        except InterchangeError as exc:
            try:
                layout(before, ctx.rate)
                out.append(f"the cut cannot be laid out: {exc}")
            except InterchangeError:
                pass

    out += [line for _, line in pin_problems(before, after, ctx)]

    # match cuts
    for i in range(len(before.clips) - 1):
        a, b = before.clips[i], before.clips[i + 1]
        if not is_match_cut(a, b):
            continue
        k = ja.get((a.id, b.id))
        was = _cut(rb, i)
        ok = k is not None and _cut(ra, k)[:2] == was[:2] and (ra[k + 1].tr == 0 or _cut(ra, k)[2:] == was[2:])
        if not ok:
            out.append(f"breaks the match cut {a.id}|{b.id}: its clips must stay adjacent, with the same cut points and a hard cut")

    # untouched clips and the junctions between them
    for c in after.clips:
        prev = old.get(c.id)
        if prev is not None and c.id not in touched and c.to_dict() != prev[0].to_dict():
            out.append(f"{c.id} changed but no operation touched it")
    for (a, b), k in ja.items():
        i = jb.get((a, b))
        if i is not None and a not in touched and b not in touched and _cut(ra, k) != _cut(rb, i):
            out.append(f"junction {a}|{b} changed in frames but no operation touched it")

    out += guard_problems(before, after, ctx, guards)
    return _dedupe(out)


def guard_problems(before: Timeline, after: Timeline, ctx: EditContext, guards: Optional[Dict[str, Any]]) -> List[str]:
    """The guards a proposal carries (decision 3), each violation naming the guard and the junction.  Junctions are named as in ``before``
    (number or id) and followed to ``after`` by their two clips."""
    if guards is None:
        return []
    if not isinstance(guards, dict):
        return [f"guards must be an object with any of {', '.join(GUARDS)}, not {guards!r}"]
    out: List[str] = [f"unknown guard {k!r} (guards: {', '.join(GUARDS)})" for k in guards if k not in GUARDS]
    rb, ra = frame_rows(before, ctx.rate), frame_rows(after, ctx.rate)
    ja = _junctions(after)
    ids_a = junction_ids(after)

    if "max_duration_change_frames" in guards:
        limit = guards["max_duration_change_frames"]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= MAX_FRAMES:
            shown = repr(limit) if len(repr(limit)) <= 24 else repr(limit)[:20] + "..."
            out.append(f"guard max_duration_change_frames must be a whole number of frames from 0 to {MAX_FRAMES}, not {shown}")
        else:
            d = running_frames(ra) - running_frames(rb)
            if abs(d) > limit:
                out.append(f"guard max_duration_change_frames: the running time changes by {d:+d} frames (at most {limit} allowed)")

    def followed(name: str) -> List[Tuple[Any, str, Optional[int]]]:
        """(the reference, its id in before, its index in after or None) for each junction a guard names."""
        refs = guards[name]
        if isinstance(refs, (str, int)) and not isinstance(refs, bool):
            refs = [refs]
        if not isinstance(refs, list):
            out.append(f"guard {name} must be a list of junctions, not {refs!r}")
            return []
        ids_b = junction_ids(before)
        found = []
        for ref in refs:
            try:
                i = find_junction(before, ref)
            except Refused as exc:
                out.append(f"guard {name}: {exc}")
                continue
            found.append((ref, ids_b[i], ja.get((before.clips[i].id, before.clips[i + 1].id))))
        return found

    if "keep_record_frames" in guards:
        for ref, jid, k in followed("keep_record_frames"):
            i = find_junction(before, ref)
            a, b = before.clips[i].id, before.clips[i + 1].id
            if k is None:
                out.append(f"guard keep_record_frames: junction {jid} ({a}|{b}) is no longer a junction")
            elif ra[k + 1].rec != rb[i + 1].rec:
                d = ra[k + 1].rec - rb[i + 1].rec
                out.append(f"guard keep_record_frames: junction {jid} ({a}|{b}) moves from record frame {rb[i + 1].rec} to {ra[k + 1].rec} ({d:+d}f)")
    if "keep_on_beat" in guards:
        if ctx.music is None:
            out.append("guard keep_on_beat: this workspace has no music track")
        else:
            for ref, jid, k in followed("keep_on_beat"):
                i = find_junction(before, ref)
                a, b = before.clips[i].id, before.clips[i + 1].id
                if beat_offset(before, rb, i, ctx) != 0:
                    continue  # it was off the beat already: the timing report shows it, the guard does not refuse an unrelated edit
                if k is None:
                    out.append(f"guard keep_on_beat: junction {jid} ({a}|{b}) was on the beat and is no longer a junction")
                    continue
                off = beat_offset(after, ra, k, ctx)
                if off != 0:
                    where = f"{abs(off)} frame{'s' if abs(off) != 1 else ''} {'after' if off > 0 else 'before'} the beat" if off is not None else "with no beat"
                    out.append(f"guard keep_on_beat: junction {jid} ({a}|{b}, {ids_a[k]} after the edit) was on the beat and would be {where}")
    return out


# ----------------------------------------------------------------------------------------------- reports
def timing(before: Timeline, after: Timeline, ctx: EditContext) -> Dict[str, Any]:
    """The timing of a proposal (decision 3), in frames: the running time before and after; every junction (by its two clip ids, named by
    its id in ``after``) whose record frame changed; junctions added and removed; with music, every junction's beat offset before and after
    and the junctions that left or reached a beat."""
    rb, ra = frame_rows(before, ctx.rate), frame_rows(after, ctx.rate)
    jb, ja = _junctions(before), _junctions(after)
    ids_b, ids_a = junction_ids(before), junction_ids(after)
    db, da = running_frames(rb), running_frames(ra)
    moved, added, offsets, left, reached = [], [], [], [], []
    for (a, b), k in ja.items():
        i = jb.get((a, b))
        if i is None:
            added.append({"id": ids_a[k], "clips": [a, b], "record_frame": ra[k + 1].rec})
        elif ra[k + 1].rec != rb[i + 1].rec:
            moved.append({"id": ids_a[k], "clips": [a, b], "before": rb[i + 1].rec, "after": ra[k + 1].rec, "delta": ra[k + 1].rec - rb[i + 1].rec})
        if ctx.music is not None:
            o0 = beat_offset(before, rb, i, ctx) if i is not None else None
            o1 = beat_offset(after, ra, k, ctx)
            offsets.append({"id": ids_a[k], "clips": [a, b], "before": o0, "after": o1})
            if i is not None and o0 == 0 and o1 != 0:
                left.append(ids_a[k])
            if i is not None and o0 != 0 and o1 == 0:
                reached.append(ids_a[k])
    removed = [{"id": ids_b[i], "clips": [a, b], "record_frame": rb[i + 1].rec} for (a, b), i in jb.items() if (a, b) not in ja]
    return {"duration_frames": [db, da], "duration_change": da - db, "moved": moved, "added": added, "removed": removed,
            "beat_offsets": offsets if ctx.music is not None else None, "left_beat": left, "reached_beat": reached}


def same_clip(x: Clip, y: Clip) -> bool:
    """Whether two clips with the same id are the same clip instance: the same shot and the same stamped ``origin`` (an unstamped origin is
    derived from the clip's range by ``clip_origin``, so it would change with every trim; two unstamped clips of one shot are the same)."""
    return x.shot_index == y.shot_index and (x.origin == y.origin or not (x.origin or y.origin))


def changes(before: Timeline, after: Timeline, ctx: EditContext) -> List[str]:
    """What ``after`` changes relative to ``before``, by clip id (ids are stable within a session, so nothing is matched): removed and
    added clips, edge moves in source frames, incoming transitions, and the new order of the clips both have.  A clip whose id is the same
    but whose shot or instance (``same_clip``) is not is reported as removed and added, never as an edge move."""
    rb, ra = frame_rows(before, ctx.rate), frame_rows(after, ctx.rate)
    b = {c.id: (c, r) for c, r in zip(before.clips, rb)}
    a = {c.id: (c, r) for c, r in zip(after.clips, ra)}
    kept = {cid for cid in a if cid in b and same_clip(b[cid][0], a[cid][0])}
    out: List[str] = []
    for cid, (c, r) in b.items():
        if cid not in kept:
            out.append(f"removed {cid} (shot {c.shot_index}, frames {r.in_f}-{r.out_f})")
    prev_a = {c.id: (after.clips[i - 1].id if i else None) for i, c in enumerate(after.clips)}
    for cid, (c, r) in a.items():
        if cid not in kept:
            tr = f", incoming {_label(r.kind, r.tr)}" if r.tr else ""
            out.append(f"added {cid} (shot {c.shot_index}, frames {r.in_f}-{r.out_f}{tr})")
            continue
        r0 = b[cid][1]
        d_in, d_out = r.in_f - r0.in_f, r.out_f - r0.out_f
        if d_in and d_in == d_out:
            out.append(f"{cid}: slipped {d_in:+d}f")
        elif d_in or d_out:
            out.append(f"{cid}: " + ", ".join(([f"in {d_in:+d}f"] if d_in else []) + ([f"out {d_out:+d}f"] if d_out else [])))
        t0, t1 = (r0.kind, r0.tr), (r.kind, r.tr)
        if t0 != t1:
            if prev_a[cid] is not None:
                out.append(f"{prev_a[cid]}|{cid}: {_label(*t0)} -> {_label(*t1)}")
            else:
                out.append(f"{cid}: incoming {_label(*t0)} dropped (it is the first clip now)")
    common_b = [c.id for c in before.clips if c.id in kept]
    common_a = [c.id for c in after.clips if c.id in kept]
    if common_a != common_b:
        out.append("order: " + " ".join(common_a))
    return out


def describe(tl: Timeline, ctx: EditContext) -> Dict[str, Any]:
    """The cut for inspection, in frames: its clips (id, key, shot, story beat, source and record frames, incoming transition, locked
    edges, purpose) and its junctions (number, id, clips, record frame, transition, story-beat change, match cut, editor pin and whether it
    protects the transition, music-beat offset)."""
    rows = frame_rows(tl, ctx.rate)
    ids = junction_ids(tl)
    pins = {site: pin for pin, site in held_pins(tl, ctx)}
    clips = []
    for c, r in zip(tl.clips, rows):
        locks = {edge: why for edge in ("in", "out") for why in [lock_reason(c, edge)] if why}
        clips.append({"id": c.id, "key": clip_origin(c), "shot": c.shot_index, "beat": c.beat_id, "source_frames": [r.in_f, r.out_f],
                      "frames": r.length, "record_frames": [r.rec, r.rec_out], "transition_in": {"kind": r.kind, "frames": r.tr},
                      "locks": locks, "purpose": c.purpose})
    junctions = []
    for i, jid in enumerate(ids):
        a, b = tl.clips[i], tl.clips[i + 1]
        pin = pins.get(i)
        junctions.append({"n": i + 1, "id": jid, "out": a.id, "in": b.id, "record_frame": rows[i + 1].rec,
                          "transition": {"kind": rows[i + 1].kind, "frames": rows[i + 1].tr}, "beat_change": a.beat_id != b.beat_id,
                          "match_cut": is_match_cut(a, b), "pinned": pin is not None, "pin": pin_label(pin) if pin is not None else None,
                          "hold_transition": bool(pin is not None and pin.hold_transition),
                          "beat_offset_frames": beat_offset(tl, rows, i, ctx)})
    return {"rate": str(ctx.rate), "fps": round(ctx.fps, 6), "duration_frames": running_frames(rows), "clips": clips, "junctions": junctions}
