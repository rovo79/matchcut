"""``provenance.json``: the canonical record of every exported stage, keyed by the ids the interchange files carry.

Interchange files carry a stable id per junction and per clip but are never the only copy of a decision: this
sidecar holds, per stage and junction id, the decisions, history, scores and pin state, and per clip key the
clip's ids and ranges.
"""
from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from matchcut.pipeline.junctions import Junction
from matchcut.pipeline.timeline import Timeline

from .common import SourceInfo, clip_key, junction_ids, junction_meta, plain, to_frames

SCHEMA = "matchcut.provenance/1"


def unique_keys(tl: Timeline) -> List[str]:
    """Clip keys of ``tl``; a key that repeats (the same range twice) gets ``#2``, ``#3`` so a dict can hold both."""
    seen: Dict[str, int] = {}
    out: List[str] = []
    for c in tl.clips:
        k = clip_key(c)
        seen[k] = seen.get(k, 0) + 1
        out.append(k if seen[k] == 1 else f"{k}#{seen[k]}")
    return out


def write_provenance(path: Path, stages: Sequence[Tuple[str, Timeline]], junctions_by_stage: Mapping[str, Sequence[Junction]],
                     src: SourceInfo) -> dict:
    """Write ``path`` and return the document.  ``stages`` are the canonical (unsnapped) stage timelines."""
    rate: Fraction = src.rate
    doc: Dict[str, Any] = {
        "schema": SCHEMA, "source": src.path, "rate": [rate.numerator, rate.denominator],
        "id_scheme": {"junction": "s<out_shot>-s<in_shot> (#n for repeats within a stage)",
                      "clip": "timeline.clip_origin: the clip instance's lineage key (<shot>@<in>-<out> of the rough cut)"},
        "stage_order": [s for s, _ in stages], "stages": {},
    }
    for stage, tl in stages:
        ids = junction_ids(tl)
        jr: Dict[str, Any] = {}
        for j in junctions_by_stage.get(stage, []):
            if j.index >= len(ids):
                continue
            jid = ids[j.index]
            stage_of = list(j.decision_stage)
            level = list(j.clip_level)
            jr[jid] = dict(
                junction_meta(j, jid, scores=True), index=j.index, out_clip=j.out_clip, in_clip=j.in_clip, beat_boundary=j.beat_boundary,
                states=[{"stage": s.stage, "out_time": s.out_time, "in_time": s.in_time, "transition": s.transition,
                         "transition_seconds": s.transition_seconds, "locked_out": s.locked_out, "locked_in": s.locked_in,
                         "combined": s.score.combined if s.score is not None else None} for s in j.states],
                decisions=[dict(d.to_dict(), stage=stage_of[k] if k < len(stage_of) else "", clip_level=bool(level[k]) if k < len(level) else False)
                           for k, d in enumerate(j.decisions)])
        doc["stages"][stage] = {
            "timeline": {"name": tl.name, "duration": round(tl.duration, 4), "fade_in": tl.fade_in, "fade_out": tl.fade_out},
            "clips": {k: dict(c.to_dict(), key=clip_key(c), stage=stage,
                              source_frames=[to_frames(c.source_in, rate), to_frames(c.source_out, rate)])
                      for k, c in zip(unique_keys(tl), tl.clips)},
            "junctions": jr, "history": [d.to_dict() for d in tl.history],
        }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plain(doc), indent=1))
    return doc
