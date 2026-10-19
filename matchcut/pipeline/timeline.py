"""Edit decision data model: clips, transitions, timelines, decisions."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .media import seconds_to_timecode

TRANSITION_KINDS = ("cut", "dissolve", "dip_to_black")


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
        """CMX3600-style EDL (single video track, source reel AX)."""
        lines = [f"TITLE: {title or self.name}", "FCM: NON-DROP FRAME", ""]
        tc = lambda t: seconds_to_timecode(t, self.fps)  # noqa: E731
        rec = 0.0
        event = 0
        for i, c in enumerate(self.clips):
            tr = c.transition_in
            if i > 0 and not tr.is_cut:
                prev = self.clips[i - 1]
                rec -= tr.duration
                frames = int(round(tr.duration * self.fps))
                event += 1
                lines.append(
                    f"{event:03d}  AX       V     C        "
                    f"{tc(prev.source_out)} {tc(prev.source_out)} {tc(rec)} {tc(rec)}"
                )
                lines.append(
                    f"{event:03d}  AX       V     D    {frames:03d} "
                    f"{tc(c.source_in)} {tc(c.source_out)} {tc(rec)} {tc(rec + c.duration)}"
                )
                lines.append(f"* FROM CLIP NAME: shot_{prev.shot_index:04d}")
                lines.append(f"* TO CLIP NAME: shot_{c.shot_index:04d}")
                lines.append(f"* TRANSITION: {tr.kind}")
            else:
                event += 1
                lines.append(
                    f"{event:03d}  AX       V     C        "
                    f"{tc(c.source_in)} {tc(c.source_out)} {tc(rec)} {tc(rec + c.duration)}"
                )
                lines.append(f"* FROM CLIP NAME: shot_{c.shot_index:04d}")
            if c.purpose:
                lines.append(f"* COMMENT: {c.beat_id} {c.purpose}".strip())
            rec += c.duration
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
