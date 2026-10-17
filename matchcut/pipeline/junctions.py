"""Junctions: the cut between two consecutive clips, followed through every stage of a cut.

A junction is identified by its two clip ids. Passes keep clip ids, so the same
junction can be traced from the rough cut through each pass to the final cut.
Scores come from the same MatchScorer the match-cut pass uses, rebuilt from the
workspace's cached analysis, so the numbers shown are the numbers the pass compared.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .config import PipelineConfig
from .features import Analysis
from .matching import MatchScore, MatchScorer
from .timeline import Decision, Timeline

SCORE_ROWS: Tuple[str, ...] = ("structure", "composition", "luminance", "color", "frame", "motion", "combined")


def stage_timeline_paths(ws: Path) -> List[Tuple[str, Path]]:
    """(stage, path) for every place a stage timeline can live, in stage order, present or not."""
    entries = [("roughcut", ws / "roughcut.json")]
    entries += [(p.stem.split("_", 1)[-1], p) for p in sorted((ws / "passes").glob("*.json"))]
    entries.append(("final", ws / "final.json"))
    return entries


def load_stage_timelines(ws: Path) -> List[Tuple[str, Timeline]]:
    stages: List[Tuple[str, Timeline]] = []
    for stage, path in stage_timeline_paths(ws):
        if not path.exists():
            continue
        try:
            stages.append((stage, Timeline.load(path)))
        except Exception:  # a half-written or hand-edited file: leave that stage out
            continue
    return stages


def scorer_for_workspace(ws: Path, cfg: PipelineConfig) -> Optional[MatchScorer]:
    path = ws / "analysis.npz"
    if not path.exists():
        return None
    try:
        return MatchScorer(Analysis.load(path).cells, cfg.matchcut)
    except Exception:
        return None


def frames_delta(seconds: float, fps: float) -> str:
    frames = int(round(seconds * fps)) if fps > 0 else 0
    return f"{seconds:+.2f}s ({frames:+d}f)"


@dataclass
class JunctionState:
    stage: str
    out_time: float
    in_time: float
    transition: str = "cut"
    transition_seconds: float = 0.0
    locked_out: bool = False
    locked_in: bool = False
    score: Optional[MatchScore] = None

    @property
    def locked(self) -> bool:
        return self.locked_out and self.locked_in

    def change_from(self, prev: "JunctionState", fps: float) -> str:
        parts: List[str] = []
        if abs(self.out_time - prev.out_time) > 1e-4:
            parts.append(f"out {frames_delta(self.out_time - prev.out_time, fps)}")
        if abs(self.in_time - prev.in_time) > 1e-4:
            parts.append(f"in {frames_delta(self.in_time - prev.in_time, fps)}")
        if (self.transition, round(self.transition_seconds, 3)) != (prev.transition, round(prev.transition_seconds, 3)):
            parts.append(self.transition if self.transition == "cut" else f"{self.transition} {self.transition_seconds:.2f}s")
        if self.locked and not prev.locked:
            parts.append("locked")
        return ", ".join(parts)


@dataclass
class Junction:
    index: int  # junction j sits between clip j and clip j+1 of the stage it was built from
    out_clip: str
    in_clip: str
    out_shot: int
    in_shot: int
    out_beat: str
    in_beat: str
    fps: float
    states: List[JunctionState] = field(default_factory=list)  # stage order; only stages where the clips are adjacent
    decisions: List[Decision] = field(default_factory=list)  # every decision naming both clips

    @property
    def beat_boundary(self) -> bool:
        return self.out_beat != self.in_beat

    @property
    def baseline(self) -> JunctionState:
        return self.states[0]

    @property
    def current(self) -> JunctionState:
        return self.states[-1]

    def state_at(self, stage: str) -> Optional[JunctionState]:
        return next((s for s in self.states if s.stage == stage), None)

    def latest(self, *actions: str) -> Optional[Decision]:
        for d in reversed(self.decisions):
            if not actions or d.action in actions:
                return d
        return None

    @property
    def match_type(self) -> str:
        d = self.latest("match_cut")
        return str(d.data.get("match_type") or "") if d is not None else ""

    @property
    def pinned(self) -> bool:
        return any(d.pass_name == "editor" and d.action == "pinned" for d in self.decisions)

    def why(self) -> str:
        d = (
            next((x for x in reversed(self.decisions) if x.pass_name == "editor"), None)
            or self.latest("match_cut", "rejected", "jump_cut_risk")
            or self.latest()
        )
        return f"{d.pass_name}: {d.detail}" if d is not None else "no pass changed this junction"

    def history(self) -> List[str]:
        b = self.baseline
        lines = [f"{b.stage}: {b.transition} at out {b.out_time:.2f}s / in {b.in_time:.2f}s"]
        for prev, cur in zip(self.states, self.states[1:]):
            change = cur.change_from(prev, self.fps)
            if change:
                lines.append(f"{cur.stage}: {change}")
        return lines


def _state(stage: str, timeline: Timeline, i: int, scorer: Optional[MatchScorer]) -> JunctionState:
    a, b = timeline.clips[i], timeline.clips[i + 1]
    score: Optional[MatchScore] = None
    if scorer is not None:
        try:
            score = scorer.score_cut(a.source_out, b.source_in)
        except Exception:
            score = None
    return JunctionState(stage, a.source_out, b.source_in, b.transition_in.kind, b.transition_in.duration, a.locked_out, b.locked_in, score)


def build_junctions(stages: Sequence[Tuple[str, Timeline]], scorer: Optional[MatchScorer] = None, upto: Optional[str] = None) -> List[Junction]:
    """Junctions of stage ``upto`` (default: the last stage), each followed through the stages up to it."""
    if not stages:
        return []
    names = [s for s, _ in stages]
    end = names.index(upto) if upto in names else len(stages) - 1
    considered = list(stages[: end + 1])
    ref = considered[-1][1]
    out: List[Junction] = []
    for i in range(len(ref.clips) - 1):
        a, b = ref.clips[i], ref.clips[i + 1]
        j = Junction(i, a.id, b.id, a.shot_index, b.shot_index, a.beat_id, b.beat_id, ref.fps)
        for stage, timeline in considered:
            ids = [c.id for c in timeline.clips]
            if a.id in ids and b.id in ids and ids.index(b.id) == ids.index(a.id) + 1:
                j.states.append(_state(stage, timeline, ids.index(a.id), scorer))
        j.decisions = [d for d in ref.history if a.id in d.clip_ids and b.id in d.clip_ids]
        out.append(j)
    return out


def junction_line(j: Junction) -> str:
    cur = j.current
    score = f"{cur.score.combined:.2f}" if cur.score is not None else "  - "
    tags = ([f"match:{j.match_type}"] if j.match_type else []) + (["locked"] if cur.locked else []) + (["pinned"] if j.pinned else []) + (["beat"] if j.beat_boundary else [])
    return (
        f"{j.index + 1:>3}  {j.out_clip}->{j.in_clip}  s{j.out_shot}->s{j.in_shot}  "
        f"out {cur.out_time:7.2f}  in {cur.in_time:7.2f}  {cur.transition:<12} score {score}  {' '.join(tags)}"
    ).rstrip()


def junction_detail(j: Junction) -> str:
    lines = [junction_line(j), f"why: {j.why()}", "history:"] + [f"  {h}" for h in j.history()]
    b, c = j.baseline, j.current
    if b.score is not None and c.score is not None:
        lines.append(f"scores ({b.stage} -> {c.stage}):")
        lines += [f"  {name:<12} {getattr(b.score, name):.3f} -> {getattr(c.score, name):.3f}" for name in SCORE_ROWS]
    lines.append("decisions:")
    lines += [f"  {d.pass_name}:{d.action}  {d.detail}" for d in j.decisions] or ["  (none)"]
    return "\n".join(lines)
