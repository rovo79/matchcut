"""Review items: the playable files for one junction (and its baseline), cached under ``workspace/review``.

A hard-cut window is an mpv EDL over the source (exact, instant); a window with a transition, a fade or
music is rendered. Files are named by a content key and written through ``<dir>/.tmp``, so an existing
non-empty file is always complete and is reused.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple

from ..pipeline.config import PipelineConfig
from ..pipeline.junctions import load_stage_timelines, locate_junction
from ..pipeline.media import MediaError, MediaInfo, probe
from ..pipeline.render import snap_to_frames
from ..pipeline.shots import load_shots
from ..pipeline.timeline import Timeline
from .render import _discard, _ffmpeg_error, _tmp, render_ab, render_window
from .window import ReviewError, Window, needs_render, review_window, sub_timeline, window_edl

REVIEW_VERSION = 1


@dataclass
class ReviewItem:
    stage: str
    index: int  # 0-based junction index in that stage
    label: str
    path: Path
    kind: str  # "edl" or "render"
    cut: float  # seconds from the item's start to the incoming clip's start
    duration: float
    transition: str  # "cut", "dissolve 0.50s", "dip to black 0.40s"
    cached: bool


@dataclass
class ReviewPair:
    b: ReviewItem
    a: Optional[ReviewItem] = None
    note: str = ""


def _stat(path: str) -> list:
    try:
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return [None, None]


def review_key(sub: Timeline, w: Window, source: str, cfg: PipelineConfig, kind: str) -> str:
    music = asdict(cfg.music)
    if cfg.music.path:
        music["file"] = _stat(str(cfg.music.path))
    doc = {
        "version": REVIEW_VERSION,
        "kind": kind,
        "clips": [c.to_dict() for c in sub.clips],
        "fades": [sub.fade_in, sub.fade_out],
        "fps": sub.fps,
        "window": asdict(w),
        "source": [str(Path(source).resolve()), *_stat(source)],
        "audio_bitrate": cfg.render.audio_bitrate,
        "review": asdict(cfg.review),
        "music": music,
    }
    text = json.dumps(doc, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _cached(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _media(ws: Path, cfg: PipelineConfig) -> MediaInfo:
    probe_path = ws / "probe.json"
    if probe_path.exists():
        try:
            media = MediaInfo.from_dict(json.loads(probe_path.read_text()))
        except (OSError, ValueError, TypeError) as e:
            raise ReviewError(f"cannot read {probe_path}: {e}") from e
    elif cfg.source and Path(cfg.source).exists():
        try:
            media = probe(cfg.source)
        except MediaError as e:
            raise _ffmpeg_error(e) from e
    else:
        raise ReviewError(f"source footage not found: {cfg.source or '(no source configured)'}")
    if not Path(media.path).exists():
        raise ReviewError(f"source footage not found: {media.path}")
    return media


def _bounds(ws: Path) -> Optional[Dict[int, Sequence[float]]]:
    try:
        return {s.index: (s.start, s.end) for s in load_shots(ws / "shots.json")}
    except Exception:
        return None


def _transition_text(tl: Timeline, junction: int) -> str:
    t = tl.clips[junction + 1].transition_in
    if t.is_cut:
        return "cut"
    return f"{t.kind.replace('_', ' ')} {t.duration:.2f}s"


def _write_edl(text: str, path: Path) -> None:
    tmp = _tmp(path.parent, ".edl")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        _discard(tmp)


def _item(
    tl: Timeline,
    stage: str,
    junction: int,
    media: MediaInfo,
    cfg: PipelineConfig,
    out_dir: Path,
    prefix: str,
    force_render: bool,
    bounds: Optional[Dict[int, Sequence[float]]],
) -> ReviewItem:
    w = review_window(tl, junction, cfg.review.pre_seconds, cfg.review.post_seconds)
    if w.duration <= 0:
        raise ReviewError("the review window is empty: raise review.pre_seconds or review.post_seconds")
    kind = "render" if force_render or needs_render(tl, w, cfg) else "edl"
    sub = sub_timeline(tl, w)
    if kind == "render":  # a render snaps to frames; an EDL plays the unsnapped window, so it must be keyed on that
        sub = snap_to_frames(sub, bounds)
    key = review_key(sub, w, media.path, cfg, kind)
    a, b = tl.clips[junction], tl.clips[junction + 1]
    path = out_dir / f"{stage}_s{a.shot_index}-s{b.shot_index}_{key}.{'edl' if kind == 'edl' else 'mp4'}"
    transition = _transition_text(tl, junction)
    label = f"{prefix}{stage}  shot {a.shot_index} -> shot {b.shot_index}  {transition}"
    cached = _cached(path)
    if not cached:
        out_dir.mkdir(parents=True, exist_ok=True)
        if kind == "edl":
            _write_edl(window_edl(tl, w, str(Path(media.path).resolve())), path)
        else:
            render_window(tl, w, media, cfg, path, bounds=bounds)
    return ReviewItem(stage, junction, label, path, kind, round(w.cut - w.start, 4), w.duration, transition, cached)


def prepare_review(
    ws: Path,
    cfg: PipelineConfig,
    stage: Optional[str] = None,
    index: int = 0,
    ab: bool = False,
    force_render: bool = False,
) -> ReviewPair:
    """The playable item(s) for junction ``index`` (0-based) of ``stage`` (default: the last stage)."""
    ws = Path(ws)
    stages = load_stage_timelines(ws)
    if not stages:
        raise ReviewError(f"no timelines in {ws}: run the pipeline first")
    names = [s for s, _ in stages]
    stage = stage or names[-1]
    if stage not in names:
        raise ReviewError(f"no stage {stage} in {ws}")
    k = names.index(stage)
    tl = stages[k][1]
    n = len(tl.clips) - 1
    if not 0 <= index < n:
        raise ReviewError(f"junction {index + 1} is out of range 1..{max(n, 0)}")
    media = _media(ws, cfg)
    bounds = _bounds(ws)
    out_dir = ws / "review"
    if not ab:
        return ReviewPair(_item(tl, stage, index, media, cfg, out_dir, "", force_render, bounds))
    a_ref, b_ref = tl.clips[index], tl.clips[index + 1]
    base: Optional[Tuple[str, Timeline, int]] = None
    for base_stage, timeline in stages[:k]:
        found = locate_junction(timeline, a_ref, b_ref)
        if found is not None:
            base = (base_stage, timeline, found)
            break
    if base is None:
        note = f"nothing earlier to compare: this junction first appears at {stage}"
        return ReviewPair(_item(tl, stage, index, media, cfg, out_dir, "", force_render, bounds), None, note)
    b = _item(tl, stage, index, media, cfg, out_dir, "B  ", force_render, bounds)
    a = _item(base[1], base[0], base[2], media, cfg, out_dir, "A  ", force_render, bounds)
    return ReviewPair(b, a)


def _rendered(item: ReviewItem, ws: Path, cfg: PipelineConfig) -> ReviewItem:
    if item.kind == "render":
        return item
    again = prepare_review(ws, cfg, item.stage, item.index, force_render=True).b
    again.label = item.label  # keep the A/B prefix
    return again


def ab_file(pair: ReviewPair, ws: Path, cfg: PipelineConfig) -> Path:
    """One mp4 for players without IPC: A, a black gap, B (both rendered; cached under ``review``)."""
    if pair.a is None:
        raise ReviewError(pair.note or "there is nothing to compare")
    ws = Path(ws)
    a, b = _rendered(pair.a, ws, cfg), _rendered(pair.b, ws, cfg)
    out = ws / "review" / f"ab_{a.path.stem.rsplit('_', 1)[-1]}_{b.path.stem.rsplit('_', 1)[-1]}.mp4"
    if not _cached(out):
        media = _media(ws, cfg)
        render_ab(a, b, out, media.has_audio or bool(cfg.music.path), cfg)
    return out
