"""A small, deterministic editing API over matchcut's canonical timeline (feasibility prototype).

Nothing here changes matchcut: it reads a workspace through matchcut's own modules and keeps its own files under
``WS/edits/``.  Its one write outside that folder is ``publish``, which writes the human stage (``human.json`` and
``human_import.json``) exactly as ``matchcut import`` does, through the same builder.

Model
- A **session** starts from one canonical stage (``final`` by default), frame-snapped as M2a exports it, as revision ``r000``.
- An agent **proposes** a list of operations.  The proposal is computed on a copy, checked, and saved with a deterministic id; nothing
  else changes.  **apply** commits a proposal as a new immutable revision, only if the head has not moved and the canonical stage has
  not changed since the session started.
- **undo** / **redo** move the head along the revision tree; nothing is deleted.  **compare** diffs any two revisions with M2a's diff.
- **render** uses matchcut's ffmpeg renderer: the whole cut, or one junction's review window.
- Every operation is in **frames**, on the source's exact rate.  Clips are named by their id (stable for the whole session: new clips get
  ``e<n>``, nothing is renumbered) or their instance key (``clip_origin``).  Junctions by number (1-based) or id (``s1-s3``).

Rules every proposal must pass (all or nothing)
- source ranges stay inside the clip's shot and at least ``pacing.min_clip_seconds`` long; transitions fit (M2a's ``layout``);
- an edge locked by an editor pin or a match cut is never moved, and a held editor pin is never broken (``pin_outcome``);
- a match cut stays a hard cut, and a pinned junction keeps its transition;
- clips the operations do not name are left exactly as they were (checked, not assumed).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from matchcut.interchange import InterchangeError
from matchcut.interchange.common import export_timeline, junction_ids, layout, record_frames, snap_rate, to_frames
from matchcut.interchange.workspace import IMPORT_SCHEMA, human_stage
from matchcut.pipeline.audio import MusicAnalysis
from matchcut.pipeline.config import PipelineConfig, RenderConfig
from matchcut.pipeline.editorial import Pin, PinStore, pin_outcome, pin_site
from matchcut.pipeline.junctions import stage_timeline_paths
from matchcut.pipeline.media import MediaInfo
from matchcut.pipeline.passes import is_match_cut
from matchcut.pipeline.render import render as ffmpeg_render
from matchcut.pipeline.shots import load_shots
from matchcut.pipeline.timeline import HUMAN_STAGE, TRANSITION_KINDS, Clip, Decision, Timeline, Transition, clip_origin

SCHEMA = "matchcut.edit/0"
OPS = ("trim", "roll", "slip", "replace", "insert", "remove", "move", "set_transition", "snap_to_beat")


class EditError(Exception):
    """A request that cannot be served at all (unknown revision, stale session, ...); one line."""


class Refused(Exception):
    """One operation cannot be applied; the message says why, in one line."""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> Optional[str]:
    try:
        return _sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _tl_sha(tl: Timeline) -> str:
    return _sha256_bytes(_canon(tl.to_dict()).encode())


# ----------------------------------------------------------------------------------------------- the workspace, read only
@dataclass
class Workspace:
    ws: Path
    cfg: PipelineConfig
    media: MediaInfo
    rate: Fraction
    bounds: Dict[int, Tuple[float, float]]
    descriptions: Dict[int, Dict[str, Any]]
    narrative: Dict[str, Any]
    music: Optional[MusicAnalysis]
    pins: List[Pin] = field(default_factory=list)

    @classmethod
    def open(cls, ws: Path) -> "Workspace":
        ws = Path(ws)
        try:
            cfg = PipelineConfig.load(ws / "config.json")
            media = MediaInfo.from_dict(json.loads((ws / "probe.json").read_text()))
            shots = load_shots(ws / "shots.json")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EditError(f"not a matchcut workspace with an analysed source: {ws} ({exc})") from exc
        def _json(name: str) -> Any:
            try:
                return json.loads((ws / name).read_text())
            except (OSError, ValueError):
                return {}
        music = None
        if cfg.music.path and (ws / "music.json").exists():
            music = MusicAnalysis.load(ws / "music.json")
        return cls(ws, cfg, media, snap_rate(media.fps), {s.index: (s.start, s.end) for s in shots},
                   {int(k): v for k, v in _json("descriptions.json").items()}, _json("narrative.json") or {}, music, PinStore(ws).load())

    @property
    def fps(self) -> float:
        return float(self.rate)

    def f(self, seconds: float) -> int:
        return to_frames(seconds, self.rate)

    def s(self, frames: int) -> float:
        return round(frames / self.fps, 4)

    def shot_frames(self, shot: int) -> Tuple[int, int]:
        lo, hi = self.bounds[shot]
        return int(math.ceil(lo * self.fps - 1e-6)), int(math.floor(hi * self.fps + 1e-6))

    @property
    def min_frames(self) -> int:
        return int(math.ceil(self.cfg.pacing.min_clip_seconds * self.fps - 1e-6))


# ----------------------------------------------------------------------------------------------- addressing
def find_clip(tl: Timeline, ref: Any) -> Clip:
    ref = str(ref)
    hits = [c for c in tl.clips if c.id == ref] or [c for c in tl.clips if clip_origin(c) == ref]
    if len(hits) != 1:
        raise Refused(f"no clip {ref}" if not hits else f"clip {ref} is ambiguous ({', '.join(c.id for c in hits)})")
    return hits[0]


def find_junction(tl: Timeline, ref: Any) -> int:
    """0-based index i of the cut between clips i and i+1, from a 1-based number or a junction id (``s1-s3``, ``s1-s3#2``)."""
    n = len(tl.clips) - 1
    if isinstance(ref, int) or (isinstance(ref, str) and ref.isdigit()):
        k = int(ref)
        if not 1 <= k <= n:
            raise Refused(f"junction {k} is out of range 1..{n}")
        return k - 1
    ids = junction_ids(tl)
    if ref in ids:
        return ids.index(ref)
    raise Refused(f"no junction {ref} (junctions: {', '.join(ids) or 'none'})")


def lock_reason(c: Clip, edge: str) -> str:
    """Why the ``edge`` (in/out) of ``c`` may not move, or ""."""
    if not (c.locked_in if edge == "in" else c.locked_out):
        return ""
    if any(t.startswith("match_in:" if edge == "in" else "match_out:") for t in c.tags):
        return f"{c.id} {edge}-point is held by a match cut"
    if "pinned" in c.tags:
        return f"{c.id} {edge}-point is held by an editor pin"
    return f"{c.id} {edge}-point is locked"


# ----------------------------------------------------------------------------------------------- operations
class Ops:
    """Each operation edits ``tl`` in place, returns (ids of the clips it changed, one line), or raises ``Refused``."""

    def __init__(self, w: Workspace, tl: Timeline):
        self.w, self.tl = w, tl

    # -- helpers
    def _fr(self, c: Clip) -> Tuple[int, int]:
        return self.w.f(c.source_in), self.w.f(c.source_out)

    def _set(self, c: Clip, in_f: int, out_f: int, new: bool = False) -> None:
        """Give ``c`` the frames ``in_f``-``out_f``: inside its shot, and never shorter than the minimum clip length unless it already
        was (the pipeline can leave a clip a frame or two under it; an edit may lengthen such a clip, never shorten it further)."""
        lo, hi = self.w.shot_frames(c.shot_index)
        if in_f < lo or out_f > hi:
            raise Refused(f"{c.id} would leave shot {c.shot_index} (frames {in_f}-{out_f}, shot is {lo}-{hi})")
        floor = self.w.min_frames if new else min(self.w.min_frames, self.w.f(c.source_out) - self.w.f(c.source_in))
        if out_f - in_f < floor:
            raise Refused(f"{c.id} would be {out_f - in_f} frames, below the minimum clip length ({self.w.min_frames})")
        c.source_in, c.source_out = self.w.s(in_f), self.w.s(out_f)

    def _edge(self, c: Clip, edge: str) -> None:
        why = lock_reason(c, edge)
        if why:
            raise Refused(why)

    def _new_id(self) -> str:
        taken = {c.id for c in self.tl.clips}
        n = 1
        while f"e{n:03d}" in taken:
            n += 1
        return f"e{n:03d}"

    def _new_clip(self, shot: int, in_f: int, out_f: int, beat_id: str, purpose: str) -> Clip:
        if shot not in self.w.bounds:
            raise Refused(f"no shot {shot}")
        c = Clip(id=self._new_id(), shot_index=shot, source_in=0.0, source_out=0.0, beat_id=beat_id, purpose=purpose)
        self._set(c, in_f, out_f, new=True)
        key = f"edit:{shot}@{in_f}-{out_f}"
        taken = {clip_origin(x) for x in self.tl.clips}
        k = 2
        origin = key
        while origin in taken:
            origin, k = f"{key}#{k}", k + 1
        c.origin = origin
        return c

    def _place(self, c: Clip, op: Dict[str, Any]) -> None:
        if "before" in op:
            self.tl.clips.insert(self.tl.clips.index(find_clip(self.tl, op["before"])), c)
        elif "after" in op:
            self.tl.clips.insert(self.tl.clips.index(find_clip(self.tl, op["after"])) + 1, c)
        else:
            raise Refused("say where: before or after a clip")

    def _first_is_cut(self) -> None:
        if self.tl.clips and not self.tl.clips[0].transition_in.is_cut:
            self.tl.clips[0].transition_in = Transition()

    # -- the operations
    def trim(self, op: Dict[str, Any]) -> Tuple[Set[str], str]:
        """Move one edge of a clip by ``frames`` (positive = later in the source); later clips ripple."""
        c, edge, n = find_clip(self.tl, op["clip"]), op["edge"], int(op["frames"])
        if edge not in ("in", "out"):
            raise Refused("edge must be in or out")
        self._edge(c, edge)
        in_f, out_f = self._fr(c)
        self._set(c, in_f + n if edge == "in" else in_f, out_f + n if edge == "out" else out_f)
        return {c.id}, f"trim {c.id} {edge} {n:+d}f"

    def roll(self, op: Dict[str, Any]) -> Tuple[Set[str], str]:
        """Move the cut between two clips by ``frames``: the outgoing out-point and the incoming in-point together; nothing ripples."""
        j = find_junction(self.tl, op["junction"])
        a, b = self.tl.clips[j], self.tl.clips[j + 1]
        n = int(op["frames"])
        self._edge(a, "out")
        self._edge(b, "in")
        ai, ao = self._fr(a)
        bi, bo = self._fr(b)
        self._set(a, ai, ao + n)
        self._set(b, bi + n, bo)
        return {a.id, b.id}, f"roll junction {j + 1} ({a.id}|{b.id}) {n:+d}f"

    def slip(self, op: Dict[str, Any]) -> Tuple[Set[str], str]:
        """Show other frames of the same shot for the same length."""
        c, n = find_clip(self.tl, op["clip"]), int(op["frames"])
        self._edge(c, "in")
        self._edge(c, "out")
        i, o = self._fr(c)
        self._set(c, i + n, o + n)
        return {c.id}, f"slip {c.id} {n:+d}f"

    def replace(self, op: Dict[str, Any]) -> Tuple[Set[str], str]:
        """Put footage of another shot in a clip's place, for the same length unless ``frames`` is given; it keeps the incoming transition."""
        old = find_clip(self.tl, op["clip"])
        self._edge(old, "in")
        self._edge(old, "out")
        shot = int(op["shot"])
        if shot not in self.w.bounds:
            raise Refused(f"no shot {shot}")
        oi, oo = self._fr(old)
        length = int(op.get("frames", oo - oi))
        lo, hi = self.w.shot_frames(shot)
        if "in_frame" in op:
            start = int(op["in_frame"])
        else:  # the middle of the shot
            if hi - lo < length:
                raise Refused(f"shot {shot} has {hi - lo} frames, fewer than the {length} needed")
            start = lo + (hi - lo - length) // 2
        new = self._new_clip(shot, start, start + length, old.beat_id, f"replaces {old.id}")
        new.transition_in = Transition.from_dict(old.transition_in.to_dict())
        k = self.tl.clips.index(old)
        self.tl.clips[k] = new
        return {old.id, new.id}, f"replace {old.id} (shot {old.shot_index}) with {new.id} (shot {shot} frames {start}-{start + length})"

    def insert(self, op: Dict[str, Any]) -> Tuple[Set[str], str]:
        shot, i, o = int(op["shot"]), int(op["in_frame"]), int(op["out_frame"])
        anchor = find_clip(self.tl, op.get("before") or op.get("after"))
        new = self._new_clip(shot, i, o, anchor.beat_id, "inserted")
        self._place(new, op)
        self._first_is_cut()
        return {new.id}, f"insert {new.id} (shot {shot} frames {i}-{o}) {'before' if 'before' in op else 'after'} {anchor.id}"

    def remove(self, op: Dict[str, Any]) -> Tuple[Set[str], str]:
        c = find_clip(self.tl, op["clip"])
        if len(self.tl.clips) <= 1:
            raise Refused("cannot remove the only clip")
        self.tl.clips.remove(c)
        self._first_is_cut()
        return {c.id}, f"remove {c.id} (shot {c.shot_index})"

    def move(self, op: Dict[str, Any]) -> Tuple[Set[str], str]:
        c = find_clip(self.tl, op["clip"])
        target = find_clip(self.tl, op.get("before") or op.get("after"))
        if target is c:
            raise Refused("cannot move a clip next to itself")
        self.tl.clips.remove(c)
        self._place(c, op)
        self._first_is_cut()
        return {c.id}, f"move {c.id} {'before' if 'before' in op else 'after'} {target.id}"

    def set_transition(self, op: Dict[str, Any]) -> Tuple[Set[str], str]:
        j = find_junction(self.tl, op["junction"])
        a, b = self.tl.clips[j], self.tl.clips[j + 1]
        kind, n = op["kind"], int(op.get("frames", 0))
        if kind not in TRANSITION_KINDS:
            raise Refused(f"unknown transition {kind} ({', '.join(TRANSITION_KINDS)})")
        if kind == "cut":
            n = 0
        if kind != "cut" and n < 1:
            raise Refused(f"a {kind} needs at least 1 frame")
        if kind != "cut" and is_match_cut(a, b):
            raise Refused(f"junction {j + 1} is a match cut: match cuts stay hard cuts")
        if a.locked_out and b.locked_in and "pinned" in a.tags and "pinned" in b.tags:
            raise Refused(f"junction {j + 1} is held by an editor pin: its transition stays")
        frac = self.w.cfg.transitions.max_fraction_of_clip
        limit = int(min(self.w.f(a.duration), self.w.f(b.duration)) * frac)
        if n > limit:
            raise Refused(f"a {n}-frame {kind} is longer than {frac:.0%} of the shorter clip ({limit} frames)")
        b.transition_in = Transition(kind, self.w.s(n)) if n else Transition()
        return {b.id}, f"junction {j + 1} ({a.id}|{b.id}) -> {kind}{f' {n}f' if n else ''}"

    def snap_to_beat(self, op: Dict[str, Any]) -> Tuple[Set[str], str]:
        """Put the cut on the nearest music beat within ``window_s``: by a roll (default; nothing else moves) or a ripple trim."""
        music = self.w.music
        if music is None:
            raise Refused("this workspace has no music track")
        j = find_junction(self.tl, op["junction"])
        mode = op.get("mode", "roll")
        window = float(op.get("window_s", self.w.cfg.music.snap_window))
        track_t = self.tl.record_start(j + 1) + self.w.cfg.music.start
        beat = music.nearest_beat(track_t)
        if beat is None or abs(beat - track_t) > window:
            raise Refused(f"no beat within {window:.2f}s of junction {j + 1}")
        n = int(round((beat - track_t) * self.w.fps))
        if n == 0:
            return set(), f"junction {j + 1} already on the beat at {beat:.3f}s"
        if mode == "roll":
            ids, _ = self.roll({"junction": j + 1, "frames": n})
        elif mode == "ripple":
            ids, _ = self.trim({"clip": self.tl.clips[j].id, "edge": "out", "frames": n})
        else:
            raise Refused("mode must be roll or ripple")
        return ids, f"junction {j + 1} {n:+d}f onto the beat at {beat:.3f}s ({mode})"


# ----------------------------------------------------------------------------------------------- reading a timeline
def held_pins(tl: Timeline, pins: Sequence[Pin]) -> List[Pin]:
    return [p for p in pins if (lambda d: d is not None and d.action == "pinned")(pin_outcome(tl, p, HUMAN_STAGE))]


def describe(w: Workspace, tl: Timeline) -> Dict[str, Any]:
    rows = layout(tl, w.rate)
    ids = junction_ids(tl)
    pins = held_pins(tl, w.pins)
    pinned_at = {pin_site(tl, p) for p in pins}
    clips = []
    for c, r in zip(tl.clips, rows):
        clips.append({"id": c.id, "key": clip_origin(c), "shot": c.shot_index, "beat": c.beat_id, "source_frames": [r.in_f, r.out_f],
                      "record_frames": [r.rec_in, r.rec_out], "transition_in": {"kind": r.kind, "frames": r.tr},
                      "locked": [e for e, v in (("in", c.locked_in), ("out", c.locked_out)) if v], "purpose": c.purpose})
    junctions = []
    for i, jid in enumerate(ids):
        a, b = tl.clips[i], tl.clips[i + 1]
        entry: Dict[str, Any] = {"n": i + 1, "id": jid, "out": a.id, "in": b.id, "record_frame": rows[i + 1].rec,
                                 "transition": {"kind": rows[i + 1].kind, "frames": rows[i + 1].tr}, "beat_change": a.beat_id != b.beat_id,
                                 "match_cut": is_match_cut(a, b),
                                 "pinned": i in pinned_at}
        if w.music is not None:
            t = tl.record_start(i + 1) + w.cfg.music.start
            beat = w.music.nearest_beat(t)
            entry["beat_offset_frames"] = None if beat is None else int(round((t - beat) * w.fps))
        junctions.append(entry)
    used: Dict[int, List[str]] = {}
    for c in tl.clips:
        used.setdefault(c.shot_index, []).append(c.id)
    shots = []
    for k in sorted(w.bounds):
        d = w.descriptions.get(k, {})
        lo, hi = w.shot_frames(k)
        shots.append({"shot": k, "frames": [lo, hi], "used_by": used.get(k, []), "subject": d.get("subject", ""), "tags": d.get("tags", []),
                      "quality": d.get("quality"), "usable": d.get("usable", True)})
    beats = []
    for b in w.narrative.get("beats", []):
        actual = sum(r.dur for c, r in zip(tl.clips, rows) if c.beat_id == b["id"])
        beats.append({"id": b["id"], "title": b.get("title", ""), "pace": b.get("pace", ""), "target_s": b.get("target_duration_s"),
                      "actual_frames": actual})
    return {"fps": str(w.rate), "duration_frames": record_frames(rows), "clips": clips, "junctions": junctions, "shots": shots,
            "story_beats": beats, "pins": [{"out_shot": p.out_shot, "in_shot": p.in_shot, "origin": p.origin} for p in pins],
            "music": None if w.music is None else {"tempo_bpm": round(w.music.tempo_bpm, 2), "beats": len(w.music.beats), "start_s": w.cfg.music.start}}


def _junction_table(tl: Timeline, rate: Fraction) -> Dict[Tuple[str, str], Tuple[int, int, str, int]]:
    rows = layout(tl, rate)
    return {(clip_origin(tl.clips[i]), clip_origin(tl.clips[i + 1])): (rows[i].out_f, rows[i + 1].in_f, rows[i + 1].kind, rows[i + 1].tr)
            for i in range(len(tl.clips) - 1)}


def _fields(c: Clip) -> Any:
    return (c.shot_index, round(c.source_in, 4), round(c.source_out, 4), c.transition_in.kind, round(c.transition_in.duration, 4),
            c.locked_in, c.locked_out, tuple(c.tags), clip_origin(c), c.beat_id)


def changes(w: Workspace, before: Timeline, after: Timeline) -> Dict[str, Any]:
    """What ``after`` changes relative to ``before``, by clip id (ids are stable within a session, so nothing has to be matched):
    removed and added clips, moved edges in source frames, incoming transitions, order; junction counts and the running time in frames."""
    rb, ra = layout(before, w.rate), layout(after, w.rate)
    b = {c.id: (c, r) for c, r in zip(before.clips, rb)}
    a = {c.id: (c, r) for c, r in zip(after.clips, ra)}
    edits: List[str] = []
    for cid, (c, r) in b.items():
        if cid not in a:
            edits.append(f"removed {cid} (shot {c.shot_index}, frames {r.in_f}-{r.out_f})")
    for cid, (c, r) in a.items():
        if cid not in b:
            edits.append(f"added {cid} (shot {c.shot_index}, frames {r.in_f}-{r.out_f})")
    prev_b = {c.id: (before.clips[i - 1].id if i else None) for i, c in enumerate(before.clips)}
    prev_a = {c.id: (after.clips[i - 1].id if i else None) for i, c in enumerate(after.clips)}
    for cid, (c, r) in a.items():
        if cid not in b:
            continue
        r0 = b[cid][1]
        d_in, d_out = r.in_f - r0.in_f, r.out_f - r0.out_f
        if d_in and d_in == d_out:
            edits.append(f"{cid}: slipped {d_in:+d}f")
        elif d_in or d_out:
            edits.append(f"{cid}: " + ", ".join(([f"in {d_in:+d}f"] if d_in else []) + ([f"out {d_out:+d}f"] if d_out else [])))
        t0, t1 = (r0.kind, r0.tr), (r.kind, r.tr)
        if t0 != t1 and prev_a[cid] is not None:
            label = lambda t: "cut" if not t[1] else f"{t[0]} {t[1]}f"  # noqa: E731
            edits.append(f"{prev_a[cid]}|{cid}: {label(t0)} -> {label(t1)}")
    common_b = [c.id for c in before.clips if c.id in a]
    common_a = [c.id for c in after.clips if c.id in b]
    if common_a != common_b:
        edits.append("order: " + " ".join(common_a))
    jb, ja = _junction_table(before, w.rate), _junction_table(after, w.rate)
    return {"edits": edits, "junctions_removed": len([k for k in jb if k not in ja]), "junctions_added": len([k for k in ja if k not in jb]),
            "junctions_changed": len([k for k in jb if k in ja and jb[k] != ja[k]]), "duration_frames": [record_frames(rb), record_frames(ra)]}


# ----------------------------------------------------------------------------------------------- the session
class EditSession:
    """``WS/edits/``: ``session.json`` (base, head), ``revisions/rNNN.json`` (immutable), ``proposals/<id>.json``, ``renders/``."""

    def __init__(self, ws: Path):
        self.w = Workspace.open(ws)
        self.dir = self.w.ws / "edits"
        self.state_path = self.dir / "session.json"

    # -- state
    def _state(self) -> Dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError) as exc:
            raise EditError("no edit session: run start first") from exc

    def _save_state(self, st: Dict[str, Any]) -> None:
        _atomic_write(self.state_path, json.dumps(st, indent=2) + "\n")

    def _stage_path(self, stage: str) -> Path:
        paths = dict(stage_timeline_paths(self.w.ws))
        if stage not in paths or stage == HUMAN_STAGE or not paths[stage].exists():
            raise EditError(f"no canonical stage {stage} in {self.w.ws}")
        return paths[stage]

    def stale(self) -> str:
        st = self._state()
        now = _sha256(self._stage_path(st["base_stage"]))
        return "" if now == st["base_sha256"] else f"{st['base_stage']}.json changed since this session started (the pipeline ran again): start a new session"

    def _check_fresh(self) -> Dict[str, Any]:
        st = self._state()
        why = self.stale()
        if why:
            raise EditError(why)
        return st

    def revision(self, rev: str) -> Dict[str, Any]:
        try:
            return json.loads((self.dir / "revisions" / f"{rev}.json").read_text())
        except (OSError, ValueError) as exc:
            raise EditError(f"no revision {rev}") from exc

    def timeline(self, rev: Optional[str] = None) -> Timeline:
        return Timeline.from_dict(self.revision(rev or self._state()["head"])["timeline"])

    def _write_revision(self, rev: Dict[str, Any]) -> None:
        path = self.dir / "revisions" / f"{rev['id']}.json"
        if path.exists():
            raise EditError(f"revision {rev['id']} exists")  # revisions are immutable
        _atomic_write(path, json.dumps(rev, indent=2) + "\n")

    # -- commands
    def start(self, stage: str = "final") -> Dict[str, Any]:
        path = self._stage_path(stage)
        sha = _sha256(path)
        if self.state_path.exists():
            st = self._state()
            if st["base_stage"] == stage and st["base_sha256"] == sha:
                return {"resumed": True, "head": st["head"], "base_stage": stage}
            stale_dir = self.dir.with_name(f"edits.stale-{st['base_sha256'][:12]}")
            if stale_dir.exists():
                raise EditError(f"{stale_dir.name} exists: move it away first")
            shutil.move(str(self.dir), str(stale_dir))  # kept, never deleted
        canonical = Timeline.load(path)
        snapped, notes = export_timeline(canonical, self.w.bounds, self.w.rate)  # the stage as M2a exports it: on the frame grid, keys stamped
        snapped.stage, snapped.name = "edit", "edit r000"
        self._write_revision({"schema": SCHEMA, "id": "r000", "parent": None, "proposal": None, "ops": [], "results": [],
                              "timeline_sha256": _tl_sha(snapped), "timeline": snapped.to_dict()})
        self._save_state({"schema": SCHEMA, "base_stage": stage, "base_sha256": sha, "head": "r000", "next": 1})
        return {"resumed": False, "head": "r000", "base_stage": stage, "snap_notes": notes}

    def inspect(self, rev: Optional[str] = None) -> Dict[str, Any]:
        st = self._state()
        rev = rev or st["head"]
        out = describe(self.w, self.timeline(rev))
        out.update({"revision": rev, "head": st["head"], "base_stage": st["base_stage"], "stale": self.stale()})
        return out

    def propose(self, ops: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        st = self._check_fresh()
        head = st["head"]
        before = self.timeline(head)
        tl = before.copy()
        engine = Ops(self.w, tl)
        results: List[Dict[str, Any]] = []
        touched: Set[str] = set()
        ok = True
        for op in ops:
            name = op.get("op")
            if name not in OPS:
                results.append({"op": op, "ok": False, "reason": f"unknown op {name} ({', '.join(OPS)})"})
                ok = False
                continue
            snapshot = tl.copy()
            try:
                ids, detail = getattr(engine, name)(op)
                touched |= ids
                results.append({"op": op, "ok": True, "detail": detail})
            except Refused as exc:
                tl.clips = snapshot.clips  # the op left nothing behind
                results.append({"op": op, "ok": False, "reason": str(exc)})
                ok = False
            except (KeyError, ValueError, TypeError) as exc:
                tl.clips = snapshot.clips
                results.append({"op": op, "ok": False, "reason": f"malformed op: {exc!r}"})
                ok = False
        problems: List[str] = []
        if ok:
            min_s = self.w.cfg.pacing.min_clip_seconds
            known = set(before.validate(self.w.bounds, min_s))  # only new problems count (see Ops._set)
            problems += [p for p in tl.validate(self.w.bounds, min_s) if p not in known and "below" not in p]
            try:
                layout(tl, self.w.rate)
            except InterchangeError as exc:
                problems.append(str(exc))
            for p in held_pins(before, self.w.pins):
                d = pin_outcome(tl, p, HUMAN_STAGE)
                if d is None or d.action != "pinned":
                    problems.append(f"breaks the editor pin s{p.out_shot}->s{p.in_shot}: {d.detail if d else 'pin lost'}")
            old = {c.id: c for c in before.clips}
            for c in tl.clips:
                if c.id in old and c.id not in touched and _fields(c) != _fields(old[c.id]):
                    problems.append(f"{c.id} changed but no operation named it")
            ok = not problems
        for r in results:
            if r["ok"]:
                tl.history.append(Decision("agent", r["op"]["op"], [], r["detail"], data=dict(r["op"])))
        proposal_id = _sha256_bytes(f"{head}:{_tl_sha(before)}:{_canon(list(ops))}".encode())[:12]
        out = {"proposal": proposal_id, "base": head, "ok": ok, "results": results, "problems": problems,
               "untouched_identical": not any("no operation named it" in p for p in problems),
               "changes": changes(self.w, before, tl) if ok else None}
        _atomic_write(self.dir / "proposals" / f"{proposal_id}.json",
                      json.dumps(dict(out, ops=list(ops), timeline=tl.to_dict() if ok else None), indent=2) + "\n")
        return out

    def apply(self, proposal_id: str) -> Dict[str, Any]:
        st = self._check_fresh()
        try:
            prop = json.loads((self.dir / "proposals" / f"{proposal_id}.json").read_text())
        except (OSError, ValueError) as exc:
            raise EditError(f"no proposal {proposal_id}") from exc
        if not prop["ok"]:
            raise EditError(f"proposal {proposal_id} was refused: " + "; ".join([r["reason"] for r in prop["results"] if not r["ok"]] + prop["problems"]))
        if prop["base"] != st["head"]:
            raise EditError(f"proposal {proposal_id} was made at {prop['base']}, but the head is now {st['head']}: propose again")
        rid = f"r{st['next']:03d}"
        tl = Timeline.from_dict(prop["timeline"])
        tl.name = f"edit {rid}"
        self._write_revision({"schema": SCHEMA, "id": rid, "parent": st["head"], "proposal": proposal_id, "ops": prop["ops"],
                              "results": prop["results"], "changes": prop["changes"], "timeline_sha256": _tl_sha(tl), "timeline": tl.to_dict()})
        st.update(head=rid, next=st["next"] + 1)
        self._save_state(st)
        return {"head": rid, "parent": prop["base"], "changes": prop["changes"]}

    def _children(self, rev: str) -> List[str]:
        out = []
        for p in sorted((self.dir / "revisions").glob("r*.json")):
            if json.loads(p.read_text()).get("parent") == rev:
                out.append(p.stem)
        return out

    def undo(self) -> Dict[str, Any]:
        st = self._check_fresh()
        parent = self.revision(st["head"])["parent"]
        if parent is None:
            raise EditError("nothing to undo: the head is the session's first revision")
        st["head"] = parent
        self._save_state(st)
        return {"head": parent}

    def redo(self) -> Dict[str, Any]:
        st = self._check_fresh()
        kids = self._children(st["head"])
        if not kids:
            raise EditError("nothing to redo")
        st["head"] = kids[-1]  # the latest branch
        self._save_state(st)
        return {"head": kids[-1], "branches": kids}

    def checkout(self, rev: str) -> Dict[str, Any]:
        """Move the head to any revision of the session (e.g. back to another branch)."""
        st = self._check_fresh()
        self.revision(rev)
        st["head"] = rev
        self._save_state(st)
        return {"head": rev}

    def log(self) -> Dict[str, Any]:
        st = self._state()
        revs = []
        for p in sorted((self.dir / "revisions").glob("r*.json")):
            r = json.loads(p.read_text())
            revs.append({"id": r["id"], "parent": r["parent"], "ops": [x.get("detail") for x in r["results"]], "timeline_sha256": r["timeline_sha256"]})
        return {"head": st["head"], "base_stage": st["base_stage"], "stale": self.stale(), "revisions": revs}

    def compare(self, a: str, b: str) -> Dict[str, Any]:
        return dict(changes(self.w, self.timeline(a), self.timeline(b)), a=a, b=b)

    def render(self, rev: Optional[str] = None, junction: Any = None, proxy: bool = True) -> Dict[str, Any]:
        rev = rev or self._state()["head"]
        tl = self.timeline(rev)
        cfg = self.w.cfg
        music_duration = self.w.music.duration if self.w.music is not None else None
        out_dir = self.dir / "renders"
        if junction is None:
            out = out_dir / f"{rev}{'-proxy' if proxy else ''}.mp4"
            if not out.exists():
                rcfg = RenderConfig(**{**cfg.render.__dict__, "enabled": True, "proxy": proxy})
                ffmpeg_render(tl, self.w.media, rcfg, out, music=cfg.music if cfg.music.path else None, music_duration=music_duration)
            return {"path": str(out), "frames": record_frames(layout(tl, self.w.rate))}
        from matchcut.review.render import render_window  # lazy: the review package is optional here
        from matchcut.review.window import review_window
        j = find_junction(tl, junction)
        win = review_window(tl, j, cfg.review.pre_seconds, cfg.review.post_seconds)
        out = out_dir / f"{rev}-j{j + 1}.mp4"
        if not out.exists():
            render_window(tl, win, self.w.media, cfg, out, label="", bounds=self.w.bounds)
        return {"path": str(out), "window_s": [win.start, win.end], "cut_s": win.cut}

    def publish(self) -> Dict[str, Any]:
        """Write the head as the workspace's human stage, through M2a's builder, so the TUI, ``junctions`` and ``export`` see it."""
        st = self._check_fresh()
        stage_path = self._stage_path(st["base_stage"])
        canonical = Timeline.load(stage_path)
        snapped = self.timeline("r000")
        head = self.timeline(st["head"])
        human = human_stage(canonical, head, self.w.rate, snapped=snapped, bounds=self.w.bounds)
        latest_name, latest_path = [(s, p) for s, p in stage_timeline_paths(self.w.ws) if s != HUMAN_STAGE and p.exists()][-1]
        decisions = [d for d in human.history if d.pass_name == "human"]
        record = {"schema": IMPORT_SCHEMA, "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "source_file": str((self.dir / "revisions" / f"{st['head']}.json").resolve()), "format": "matchcut-edit",
                  "stage": st["base_stage"], "stage_file": stage_path.name, "stage_sha256": st["base_sha256"],
                  "latest_stage": latest_name, "latest_stage_file": latest_path.name, "latest_stage_sha256": _sha256(latest_path),
                  "decisions": len(decisions), "notes": [f"published from the edit session at revision {st['head']}"]}
        _atomic_write(self.w.ws / "human.json", json.dumps(human.to_dict(), indent=2))
        _atomic_write(self.w.ws / "human_import.json", json.dumps(record, indent=2) + "\n")
        return {"human": str(self.w.ws / "human.json"), "revision": st["head"], "decisions": [d.detail for d in decisions]}
