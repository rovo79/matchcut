"""Edit decision data model: clips, transitions, timelines, decisions."""
from __future__ import annotations

import json
import math
from fractions import Fraction
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .frames import snap_rate, to_frames

TRANSITION_KINDS = ("cut", "dissolve", "dip_to_black")
HUMAN_STAGE = "human"  # the stage an editor's re-export is imported as (matchcut.interchange writes it; the engine only reads it)
EDIT_STAGE = "edit"  # the stage an agent's published edit session is (matchcut.edit writes it; the engine only reads it)
AGENT_PASS = "agent"  # the pass name of the decisions an edit session appends, one per change


@dataclass
class Transition:
    kind: str = "cut"
    duration: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Transition":
        return cls(kind=d.get("kind", "cut"), duration=float(d.get("duration", 0.0)))

    @property
    def is_cut(self) -> bool:
        return self.kind == "cut" or self.duration <= 0


@dataclass
class Clip:
    id: str
    shot_index: int
    source_in: float
    source_out: float
    beat_id: str = ""
    purpose: str = ""
    transition_in: Transition = field(default_factory=Transition)
    locked_in: bool = False  # in-point tuned for a match cut; later passes keep it
    locked_out: bool = False
    tags: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    origin: str = ""  # the clip instance this one descends from (see clip_origin); stamped when the pins are applied

    @property
    def duration(self) -> float:
        return self.source_out - self.source_in

    def to_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = round(self.duration, 3)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Clip":
        return cls(
            id=str(d["id"]),
            shot_index=int(d["shot_index"]),
            source_in=float(d["source_in"]),
            source_out=float(d["source_out"]),
            beat_id=str(d.get("beat_id", "")),
            purpose=str(d.get("purpose", "")),
            transition_in=Transition.from_dict(d.get("transition_in", {})),
            locked_in=bool(d.get("locked_in", False)),
            locked_out=bool(d.get("locked_out", False)),
            tags=list(d.get("tags", [])),
            notes=list(d.get("notes", [])),
            origin=str(d.get("origin", "")),
        )


def clip_origin(c: Clip) -> str:
    """The identity of a clip instance: its stamped ``origin``, else the rough-cut key built from its current range."""
    return c.origin or f"{c.shot_index}@{c.source_in:.3f}-{c.source_out:.3f}"


@dataclass
class Decision:
    """A record of something a pass did (or deliberately did not do)."""

    pass_name: str
    action: str
    clip_ids: List[str]
    detail: str
    score: Optional[float] = None
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Decision":
        return cls(
            pass_name=d["pass_name"],
            action=d["action"],
            clip_ids=list(d.get("clip_ids", [])),
            detail=d.get("detail", ""),
            score=d.get("score"),
            data=dict(d.get("data", {})),
        )


@dataclass
class Timeline:
    source: str
    fps: float
    clips: List[Clip] = field(default_factory=list)
    fade_in: float = 0.0
    fade_out: float = 0.0
    name: str = "cut"
    stage: str = ""
    history: List[Decision] = field(default_factory=list)

    # -- basics ----------------------------------------------------------
    @property
    def duration(self) -> float:
        total = sum(c.duration for c in self.clips)
        for c in self.clips[1:]:
            if not c.transition_in.is_cut:
                total -= c.transition_in.duration
        return max(total, 0.0)

    def clip_by_id(self, clip_id: str) -> Clip:
        for c in self.clips:
            if c.id == clip_id:
                return c
        raise KeyError(clip_id)

    def index_of(self, clip_id: str) -> int:
        for i, c in enumerate(self.clips):
            if c.id == clip_id:
                return i
        raise KeyError(clip_id)

    def copy(self) -> "Timeline":
        return Timeline.from_dict(self.to_dict())

    def snap_to_frames(self, bounds: Optional[Dict[int, Sequence[float]]] = None) -> "Timeline":
        """Copy with in/out points on the frame grid (at least two frames per clip).

        With ``bounds`` (shot index -> (start, end)), a rounding that would leave the
        shot is pulled back in: in-points round up, out-points round down.
        """
        out = self.copy()
        fps = out.fps
        for c in out.clips:
            lo, hi = (bounds or {}).get(c.shot_index, (None, None))
            in_f = int(round(c.source_in * fps))
            out_f = int(round(c.source_out * fps))
            if lo is not None and in_f / fps < lo - 1e-9:
                in_f = int(math.ceil(lo * fps - 1e-9))
            if hi is not None and out_f / fps > hi + 1e-9:
                out_f = int(math.floor(hi * fps + 1e-9))
            if out_f - in_f < 2:
                out_f = in_f + 2
            c.source_in = round(in_f / fps, 4)
            c.source_out = round(out_f / fps, 4)
        return out

    def snap_transitions(self) -> "Timeline":
        """Copy with every transition a whole number of frames, laid out exactly as the interchange exporters and the renderer lay them out
        (``interchange.common.layout``): rounded over the running total at the source's exact rate, so the cut's length stays within half
        a frame.  Lengths are stored to the microsecond, so a long run of transitions re-laid out from the stored values gives the same
        frames.  A transition that rounds to no frame (or has no length) becomes a cut.  Source points are left as they are."""
        out = self.copy()
        if out.fps <= 0:
            return out
        rate = snap_rate(out.fps)
        cum, done = 0.0, 0
        for c in out.clips[1:]:
            if c.transition_in.is_cut:
                c.transition_in = Transition()
                continue
            cum += c.transition_in.duration
            frames = max(to_frames(cum, rate) - done, 0)
            done += frames
            c.transition_in = Transition(c.transition_in.kind, round(float(frames / rate), 6)) if frames else Transition()
        if out.clips:
            out.clips[0].transition_in = Transition()
        return out

    def transition_rounding_problems(self, rounded: "Timeline") -> List[Decision]:
        """What rounding the transitions to whole frames (``rounded`` = ``self.snap_transitions()``) did that changes the cut, as decisions to
        record: a transition that rounded to no frame (it is a cut now), or one that takes all of a clip's frames.  Ordinary rounding of a
        fraction of a frame is not reported (the exports and the render always rounded it so)."""
        out: List[Decision] = []
        if self.fps <= 0:
            return out
        rate = snap_rate(self.fps)
        for i in range(1, min(len(self.clips), len(rounded.clips))):
            before, after = self.clips[i].transition_in, rounded.clips[i].transition_in
            prev, cur = rounded.clips[i - 1], rounded.clips[i]
            ids = [prev.id, cur.id]
            if not before.is_cut and after.is_cut:
                out.append(Decision("final", "transition_dropped", ids,
                                    f"{before.kind} of {before.duration:.3f} s between {prev.id} and {cur.id} rounds to no frame: it is a cut now",
                                    data={"kind": before.kind, "seconds": before.duration}))
                continue
            if after.is_cut:
                continue
            tr = to_frames(after.duration, rate)
            shortest = min(to_frames(prev.source_out, rate) - to_frames(prev.source_in, rate), to_frames(cur.source_out, rate) - to_frames(cur.source_in, rate))
            if tr >= shortest:
                out.append(Decision("final", "transition_too_long", ids,
                                    f"{after.kind} of {tr} frames between {prev.id} and {cur.id} is as long as one of its clips ({shortest} frames): "
                                    "the editor and the exports cannot lay it out", data={"frames": tr, "clip_frames": shortest}))
        return out

    def renumber(self) -> None:
        for i, c in enumerate(self.clips):
            c.id = f"c{i + 1:03d}"

    def record_start(self, clip_index: int) -> float:
        """Timeline time at which clip ``clip_index`` starts playing."""
        t = 0.0
        for i, c in enumerate(self.clips[: clip_index + 1]):
            if i == 0:
                continue
            t += self.clips[i - 1].duration
            if not c.transition_in.is_cut:
                t -= c.transition_in.duration
        return t

    def validate(self, shot_bounds: Optional[Dict[int, Sequence[float]]] = None, min_clip: float = 0.1) -> List[str]:
        problems: List[str] = []
        seen = set()
        for c in self.clips:
            if c.id in seen:
                problems.append(f"duplicate clip id {c.id}")
            seen.add(c.id)
            if c.duration < min_clip:
                problems.append(f"{c.id}: duration {c.duration:.3f}s below {min_clip}s")
            if shot_bounds is not None:
                if c.shot_index not in shot_bounds:
                    problems.append(f"{c.id}: unknown shot {c.shot_index}")
                else:
                    s, e = shot_bounds[c.shot_index]
                    if c.source_in < s - 1e-3 or c.source_out > e + 1e-3:
                        problems.append(
                            f"{c.id}: [{c.source_in:.2f},{c.source_out:.2f}] outside shot {c.shot_index} [{s:.2f},{e:.2f}]"
                        )
            if c.transition_in.kind not in TRANSITION_KINDS:
                problems.append(f"{c.id}: unknown transition {c.transition_in.kind}")
        return problems

    # -- serialisation ---------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "stage": self.stage,
            "source": self.source,
            "fps": self.fps,
            "fade_in": self.fade_in,
            "fade_out": self.fade_out,
            "duration": round(self.duration, 3),
            "clips": [c.to_dict() for c in self.clips],
            "history": [d.to_dict() for d in self.history],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Timeline":
        return cls(
            source=d["source"],
            fps=float(d["fps"]),
            clips=[Clip.from_dict(c) for c in d.get("clips", [])],
            fade_in=float(d.get("fade_in", 0.0)),
            fade_out=float(d.get("fade_out", 0.0)),
            name=d.get("name", "cut"),
            stage=d.get("stage", ""),
            history=[Decision.from_dict(x) for x in d.get("history", [])],
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path) -> "Timeline":
        return cls.from_dict(json.loads(Path(path).read_text()))

    # -- exports ---------------------------------------------------------
    def to_edl(self, title: Optional[str] = None) -> str:
        """CMX3600-style EDL (single video track, source reel AX), laid out exactly as the M2a EDL exporter lays it out
        (``interchange.edl.export_edl`` on ``interchange.common.layout``): every number a whole frame at the source's exact rate, source
        points rounded half up, transitions over the running total; an event before a dissolve ends where the dissolve starts, and the
        dissolve's hold line holds the outgoing media there.  So this EDL, the M2a exports and the render agree to the frame, and it reads
        back through M2a's EDL reader as the same cut."""
        lines = [f"TITLE: {title or self.name}", "FCM: NON-DROP FRAME", ""]
        if not self.clips:
            return "\n".join(lines)
        rate = snap_rate(self.fps) if self.fps > 0 else Fraction(25)
        base = int(round(float(rate))) or 1

        def tc(n: int) -> str:
            secs, f = divmod(max(n, 0), base)
            return f"{secs // 3600:02d}:{(secs % 3600) // 60:02d}:{secs % 60:02d}:{f:02d}"

        rows = []  # (in_f, out_f, tr, rec) per clip, as layout() computes them
        cum, done = 0.0, 0
        for i, c in enumerate(self.clips):
            in_f = to_frames(c.source_in, rate)
            out_f = max(to_frames(c.source_out, rate), in_f + 1)
            tr = 0
            if i and not c.transition_in.is_cut:
                cum += c.transition_in.duration
                tr = max(to_frames(cum, rate) - done, 0)
                done += tr
            rec = rows[-1][3] + (rows[-1][1] - rows[-1][0]) - tr if rows else 0
            rows.append((in_f, out_f, tr, rec))
        event = 0
        for i, (c, (in_f, out_f, tr, rec)) in enumerate(zip(self.clips, rows)):
            nxt = rows[i + 1][2] if i + 1 < len(rows) else 0  # the next transition starts where this event ends
            src_out, rec_out = out_f - nxt, rec + (out_f - in_f) - nxt
            event += 1
            if tr:
                prev = self.clips[i - 1]
                hold = rows[i - 1][1] - tr  # where the outgoing clip's media is when the dissolve starts
                lines.append(f"{event:03d}  AX       V     C        {tc(hold)} {tc(hold)} {tc(rec)} {tc(rec)}")
                lines.append(f"{event:03d}  AX       V     D    {tr:03d} {tc(in_f)} {tc(src_out)} {tc(rec)} {tc(rec_out)}")
                lines.append(f"* FROM CLIP NAME: shot_{prev.shot_index:04d}")
                lines.append(f"* TO CLIP NAME: shot_{c.shot_index:04d}")
                lines.append(f"* TRANSITION: {c.transition_in.kind}")
            else:
                lines.append(f"{event:03d}  AX       V     C        {tc(in_f)} {tc(src_out)} {tc(rec)} {tc(rec_out)}")
                lines.append(f"* FROM CLIP NAME: shot_{c.shot_index:04d}")
            if src_out <= in_f:  # the transitions take all of this clip (the final stage records transition_too_long)
                lines.append(f"* COMMENT: shot_{c.shot_index:04d} is no longer than its transitions: this event has no frames of its own")
            if c.purpose:
                lines.append(f"* COMMENT: {c.beat_id} {c.purpose}".strip())
            lines.append("")
        return "\n".join(lines)

    def to_csv(self) -> str:
        rows = ["clip_id,shot_index,beat_id,source_in,source_out,duration,record_in,transition_in,transition_duration,purpose"]
        for i, c in enumerate(self.clips):
            rows.append(
                ",".join(
                    [
                        c.id,
                        str(c.shot_index),
                        c.beat_id,
                        f"{c.source_in:.3f}",
                        f"{c.source_out:.3f}",
                        f"{c.duration:.3f}",
                        f"{self.record_start(i):.3f}",
                        c.transition_in.kind,
                        f"{c.transition_in.duration:.3f}",
                        '"' + c.purpose.replace('"', "'") + '"',
                    ]
                )
            )
        return "\n".join(rows)


@dataclass
class PassResult:
    timeline: Timeline
    decisions: List[Decision]
