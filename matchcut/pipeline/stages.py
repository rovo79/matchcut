"""The forward stages: ingest -> describe -> narrative -> rough cut.

Each stage reads what it needs from the :class:`Context`, writes its result to
the workspace, and returns it. The orchestrator in ``pipeline.py`` decides
when a cached result can be reused.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import prompts, schemas
from .context import Context
from .llm import LLMRequest
from .sampling import chunked, shot_sheet
from .timeline import Clip, Decision, Timeline

log = logging.getLogger("matchcut.pipeline")


# --------------------------------------------------------------------------
# describe
# --------------------------------------------------------------------------
def describe_shots(ctx: Context) -> Dict[int, Dict[str, Any]]:
    """Vision pass: the model describes every shot from its contact-sheet row."""
    cfg = ctx.cfg.sampling
    sheets_dir = ctx.workspace / "sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    descriptions: Dict[int, Dict[str, Any]] = {}
    sheets = list(chunked(ctx.shots, cfg.shots_per_sheet))
    sheet_no = 0
    for group in chunked(sheets, cfg.sheets_per_request):
        images = []
        shot_ids: List[int] = []
        for shots in group:
            img = shot_sheet(ctx.frames, shots, cfg)
            img.save(sheets_dir / f"describe_{sheet_no:03d}.jpg", quality=cfg.jpeg_quality)
            sheet_no += 1
            images.append(img)
            shot_ids.extend(s.index for s in shots)
        stats = [ctx.shot_stats(ctx.shot(i)) for i in shot_ids]
        text = prompts.DESCRIBE.format(
            shot_ids=shot_ids,
            machine_notes=json.dumps(stats, indent=0),
        )
        response = ctx.llm.structured(
            LLMRequest(
                name="describe",
                system=prompts.EDITOR_SYSTEM,
                text=text,
                images=images,
                schema=schemas.DescribeResponse,
                payload={"shots": stats},
            )
        )
        assert isinstance(response, schemas.DescribeResponse)
        for d in response.shots:
            if d.shot_id in shot_ids:
                descriptions[d.shot_id] = d.model_dump()
        missing = [i for i in shot_ids if i not in descriptions]
        for i in missing:  # the model skipped a row: keep a neutral stub so later stages still see it
            log.warning("no description returned for shot %d; using stub", i)
            descriptions[i] = _stub_description(i)
    return descriptions


def _stub_description(shot_id: int) -> Dict[str, Any]:
    return schemas.ShotDescription(
        shot_id=shot_id,
        description="(no description returned)",
        subject="unknown",
        setting="unknown",
        shot_size="unknown",
        camera_movement="unknown",
        subject_motion="none",
        dominant_shapes="unknown",
        mood="unknown",
        tags=[],
        key_action="none",
        quality=0.5,
        usable=True,
    ).model_dump()


# --------------------------------------------------------------------------
# narrative
# --------------------------------------------------------------------------
def find_narrative(ctx: Context) -> Dict[str, Any]:
    catalog = ctx.catalog()
    text = prompts.NARRATIVE.format(
        brief=ctx.cfg.brief or "(none)",
        target=ctx.target_text(),
        music=ctx.music_text(),
        n_shots=len(ctx.shots),
        total_duration=ctx.media.duration,
        catalog=json.dumps(catalog, indent=0),
    )
    response = ctx.llm.structured(
        LLMRequest(
            name="narrative",
            system=prompts.EDITOR_SYSTEM,
            text=text,
            schema=schemas.NarrativeResponse,
            payload={"shots": catalog, "target_duration": ctx.cfg.target_duration},
        )
    )
    assert isinstance(response, schemas.NarrativeResponse)
    narrative = response.model_dump()
    valid_ids = {s.index for s in ctx.shots}
    seen: set = set()
    for beat in narrative["beats"]:
        cleaned = []
        for sid in beat["shot_ids"]:
            if sid in valid_ids and sid not in seen:
                cleaned.append(sid)
                seen.add(sid)
        beat["shot_ids"] = cleaned
    narrative["beats"] = [b for b in narrative["beats"] if b["shot_ids"]]
    if not narrative["beats"]:
        raise RuntimeError("narrative stage produced no beats with valid shots")
    if ctx.cfg.target_duration:
        total = sum(b["target_duration_s"] for b in narrative["beats"]) or 1.0
        for b in narrative["beats"]:  # normalise the model's budget to the requested running time
            b["target_duration_s"] = round(b["target_duration_s"] / total * ctx.cfg.target_duration, 2)
    return narrative


# --------------------------------------------------------------------------
# rough cut
# --------------------------------------------------------------------------
def rough_cut(ctx: Context) -> Timeline:
    pacing = ctx.cfg.pacing
    beats = ctx.narrative["beats"]
    beat_shot_ids = [sid for b in beats for sid in b["shot_ids"]]
    catalog = ctx.catalog(beat_shot_ids)
    text = prompts.ROUGH_CUT.format(
        brief=ctx.cfg.brief or "(none)",
        target=ctx.target_text(),
        music=ctx.music_text(),
        narrative=ctx.narrative_text(),
        catalog=json.dumps(catalog, indent=0),
        min_clip=pacing.min_clip_seconds,
        max_clip=pacing.max_clip_seconds,
    )
    response = ctx.llm.structured(
        LLMRequest(
            name="rough_cut",
            system=prompts.EDITOR_SYSTEM,
            text=text,
            schema=schemas.RoughCutResponse,
            payload={
                "shots": catalog,
                "beats": beats,
                "min_clip": pacing.min_clip_seconds,
                "max_clip": pacing.max_clip_seconds,
            },
        )
    )
    assert isinstance(response, schemas.RoughCutResponse)
    timeline = Timeline(source=ctx.media.path, fps=ctx.media.fps, name="rough cut", stage="roughcut")
    decisions: List[Decision] = []
    beat_ids = {b["id"] for b in beats}
    used: set = set()
    for rc in response.clips:
        if rc.shot_id not in ctx.shot_bounds or rc.shot_id in used:
            decisions.append(Decision("roughcut", "reject", [], f"shot {rc.shot_id} unknown or already used"))
            continue
        s, e = ctx.shot_bounds[rc.shot_id]
        in_s, out_s = clamp_clip(rc.in_s, rc.out_s, s, e, pacing.min_clip_seconds, pacing.max_clip_seconds)
        if out_s - in_s < pacing.min_clip_seconds - 1e-6:
            decisions.append(Decision("roughcut", "reject", [], f"shot {rc.shot_id}: too short after clamping"))
            continue
        used.add(rc.shot_id)
        timeline.clips.append(
            Clip(
                id="",
                shot_index=rc.shot_id,
                source_in=round(in_s, 3),
                source_out=round(out_s, 3),
                beat_id=rc.beat_id if rc.beat_id in beat_ids else beats[0]["id"],
                purpose=rc.purpose,
            )
        )
    if not timeline.clips:
        raise RuntimeError("rough cut produced no valid clips")
    # keep beats in narrative order even if the model interleaved them
    order = {b["id"]: i for i, b in enumerate(beats)}
    timeline.clips.sort(key=lambda c: order.get(c.beat_id, 0))
    timeline.renumber()
    decisions.append(Decision("roughcut", "notes", [c.id for c in timeline.clips], response.notes))
    timeline.history.extend(decisions)
    return timeline


def clamp_clip(in_s: float, out_s: float, start: float, end: float, min_len: float, max_len: float) -> Tuple[float, float]:
    """Keep [in_s, out_s] inside the shot and within the length limits."""
    in_s, out_s = sorted((in_s, out_s))
    in_s = max(start, min(in_s, end))
    out_s = max(start, min(out_s, end))
    if out_s - in_s > max_len:
        out_s = in_s + max_len
    if out_s - in_s < min_len:
        out_s = min(end, in_s + min_len)
        in_s = max(start, out_s - min_len)
    return in_s, out_s
