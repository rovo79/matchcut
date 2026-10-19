"""OpenTimelineIO JSON documents, written and read by hand (the OpenTimelineIO library is never imported).

Export: ``Timeline.1`` with a video track ``V1`` (``Clip.2`` per matchcut clip, ``Transition.1`` centred on each cut,
a ``Marker.2`` per junction at the cut's record frame), an audio track ``A1`` mirroring the picture when the source has
audio, and an audio track ``A2`` with the music file when music is configured.  The exact rate lives in
``metadata.matchcut.rate`` (``RationalTime.rate`` is only a float).

Import: reads the first video track back into a matchcut ``Timeline`` (overlap semantics restored, ``Clip.origin`` from
the ``clip_key`` metadata) and reports everything it ignored.
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from matchcut.pipeline.config import MusicConfig
from matchcut.pipeline.junctions import Junction
from matchcut.pipeline.timeline import Timeline

from . import MATCHCUT_VERSION, InterchangeError
from .common import (SourceInfo, assemble, clip_key, junction_ids, junction_meta, layout, record_frames, snap_rate, to_frames,
                     truncate)

UNVERIFIED_DIP = "dip_to_black is UNVERIFIED: OTIO has no such transition type (Custom_Transition + metadata); how an editor treats it is untested"


# -- helpers -------------------------------------------------------------------------------------
def _rt(v: float, rate: float) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": float(v)}


def _range(start: float, dur: float, rate: float) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, rate), "duration": _rt(dur, rate)}


def _ref(src: SourceInfo, name: str) -> Dict[str, Any]:
    r = float(src.rate)
    return {"OTIO_SCHEMA": "ExternalReference.1", "name": name, "target_url": src.url,
            "available_range": _range(0, src.duration_frames, r), "available_image_bounds": None,
            "metadata": {"matchcut": {"size": src.size, "mtime_ns": src.mtime_ns}}}


def _clip(name: str, source_range: Dict[str, Any], ref: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "Clip.2", "metadata": {"matchcut": meta}, "name": name, "source_range": source_range, "effects": [], "markers": [],
            "enabled": True, "color": None, "media_references": {"DEFAULT_MEDIA": ref}, "active_media_reference_key": "DEFAULT_MEDIA"}


def _track(kind: str, name: str, children: List[Dict[str, Any]], markers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "Track.1", "metadata": {}, "name": name, "source_range": None, "effects": [], "markers": markers or [],
            "enabled": True, "color": None, "children": children, "kind": kind}


# -- export --------------------------------------------------------------------------------------
def export_otio(tl: Timeline, stage: str, src: SourceInfo, junctions: Sequence[Junction], music: Optional[MusicConfig] = None,
                music_src: Optional[SourceInfo] = None) -> dict:
    """One OTIO document for ``tl`` (normally the frame-snapped copy from ``common.export_timeline``)."""
    rate = src.rate
    r = float(rate)
    rows = layout(tl, rate)
    ids = junction_ids(tl)
    jid_in = {i + 1: jid for i, jid in enumerate(ids)}  # incoming-clip index -> junction id
    stem = Path(src.path).stem
    ref = _ref(src, stem)

    def children() -> List[Dict[str, Any]]:
        kids: List[Dict[str, Any]] = []
        for i, (c, row) in enumerate(zip(tl.clips, rows)):
            if row.tr:
                kind, in_off, out_off = row.transition
                dip = kind == "dip_to_black"
                tmeta: Dict[str, Any] = {"kind": kind, "duration": c.transition_in.duration}
                if i in jid_in:
                    tmeta["junction_id"] = jid_in[i]
                if dip:
                    tmeta["unverified"] = UNVERIFIED_DIP
                kids.append({"OTIO_SCHEMA": "Transition.1", "metadata": {"matchcut": tmeta}, "name": "Dip To Black" if dip else "Cross Dissolve",
                             "in_offset": _rt(in_off, r), "out_offset": _rt(out_off, r),
                             "transition_type": "Custom_Transition" if dip else "SMPTE_Dissolve"})
            kids.append(_clip(f"{c.id} shot{c.shot_index}", _range(row.vis_in, row.dur, r), ref,
                              {"clip_id": c.id, "clip_key": clip_key(c), "shot_index": c.shot_index, "beat_id": c.beat_id, "stage": stage}))
        return kids

    markers: List[Dict[str, Any]] = []
    for j in junctions:
        if j.index + 1 < len(rows):
            jid = ids[j.index]
            markers.append({"OTIO_SCHEMA": "Marker.2", "metadata": {"matchcut": junction_meta(j, jid)}, "name": f"J{j.index + 1} s{j.out_shot}->s{j.in_shot}",
                            "color": "RED" if j.pinned else "GREEN", "marked_range": _range(rows[j.index + 1].cut, 1, r), "comment": truncate(j.why())})
    tracks = [_track("Video", "V1", children(), markers)]
    if src.has_audio:
        tracks.append(_track("Audio", "A1", children()))
    if music is not None and music.path:
        if music_src is None:
            raise InterchangeError(f"music file not probed: {music.path}")
        mrate = float(rate)
        start = to_frames(music.start, rate)
        mref = {**_ref(music_src, Path(music_src.path).stem),
                "available_range": _range(0, to_frames(music_src.seconds, rate), mrate)}
        tracks.append(_track("Audio", "A2", [_clip(Path(music_src.path).name, _range(start, record_frames(rows), mrate), mref,
                                                   {"kind": "music", "gain_db": music.gain_db, "duck": music.duck, "start": music.start})]))
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "metadata": {"matchcut": {"stage": stage, "source": src.path, "rate": [rate.numerator, rate.denominator], "fade_in": tl.fade_in,
                                  "fade_out": tl.fade_out, "provenance_file": "provenance.json", "matchcut_version": MATCHCUT_VERSION}},
        "name": f"{stem} - {stage}", "global_start_time": _rt(0, r),
        "tracks": {"OTIO_SCHEMA": "Stack.1", "metadata": {}, "name": "tracks", "source_range": None, "effects": [], "markers": [],
                   "enabled": True, "color": None, "children": tracks},
    }


# -- import --------------------------------------------------------------------------------------
def _schema(item: Dict[str, Any]) -> str:
    return str(item.get("OTIO_SCHEMA", "")).split(".")[0]


def _path_of(clip: Dict[str, Any]) -> Optional[str]:
    refs = clip.get("media_references") or {}
    ref = refs.get(clip.get("active_media_reference_key")) or next(iter(refs.values()), None) or {}
    url = ref.get("target_url")
    return unquote(urlparse(url).path) if url and url.startswith("file:") else (url or None)


def _same_media(a: Optional[str], b: Optional[str]) -> bool:
    return a is None or b is None or a == b or Path(a).name == Path(b).name


def import_otio(doc: dict, bounds: Optional[Dict[int, Sequence[float]]] = None) -> Tuple[Timeline, List[str]]:
    """OTIO document (a dict) -> (Timeline, notes).  Everything that cannot be mapped is listed in ``notes``."""
    meta = (doc.get("metadata") or {}).get("matchcut", {})
    tracks = (doc.get("tracks") or {}).get("children", [])
    notes: List[str] = []
    videos = [t for t in tracks if _schema(t) == "Track" and t.get("kind") == "Video"]
    if not videos:
        raise InterchangeError("OTIO document has no video track")
    vt = videos[0]
    for extra in videos[1:]:
        notes.append(f"video track {extra.get('name') or '?'} ignored (only the first video track is read)")
    for t in tracks:
        if t.get("kind") == "Audio" and any(_schema(k) == "Clip" and "matchcut" not in (k.get("metadata") or {}) for k in t.get("children", [])):
            notes.append(f"audio track {t.get('name') or '?'} ignored (matchcut reads the picture only)")
    first = next((k for k in vt.get("children", []) if _schema(k) == "Clip"), None)
    if first is None:
        raise InterchangeError("OTIO video track has no clips")
    if meta.get("rate"):
        rate = Fraction(int(meta["rate"][0]), int(meta["rate"][1]))
    else:
        rate = snap_rate(float(first["source_range"]["start_time"]["rate"]))
    rf = float(rate)

    def fr(t: Dict[str, Any]) -> int:
        """RationalTime -> frames at ``rate`` (a time on another base, e.g. a re-export at 29.97, is converted)."""
        tr_ = float(t["rate"])
        if abs(tr_ - rf) < 1e-6 * max(rf, 1.0):
            return int(round(float(t["value"])))
        return to_frames(float(t["value"]) / float(snap_rate(tr_)), rate)

    want = meta.get("source") or _path_of(first)
    items: List[Tuple[str, Dict[str, Any]]] = []
    mapped = 0
    skipped_media = 0
    for ch in vt.get("children", []):
        kind = _schema(ch)
        name = ch.get("name") or kind
        if kind == "Clip":
            if not _same_media(_path_of(ch), want):
                skipped_media += 1
                notes.append(f"clip {name}: other media {_path_of(ch)} ignored")
                items.append(("skip", {}))
                continue
            sr = ch.get("source_range") or (ch.get("media_references", {}).get(ch.get("active_media_reference_key"), {}) or {}).get("available_range")
            if not sr:
                notes.append(f"clip {name}: no source range, ignored")
                items.append(("skip", {}))
                continue
            for eff in ch.get("effects") or []:
                notes.append(f"clip {name}: effect {eff.get('name') or _schema(eff)} ignored")
            mapped += 1
            items.append(("clip", {"vis_in": fr(sr["start_time"]), "dur": fr(sr["duration"]), "name": name, "meta": (ch.get("metadata") or {}).get("matchcut", {})}))
        elif kind == "Transition":
            tm = (ch.get("metadata") or {}).get("matchcut", {})
            tkind = "dip_to_black" if tm.get("kind") == "dip_to_black" else "dissolve"
            items.append(("tr", {"in": fr(ch["in_offset"]), "out": fr(ch["out_offset"]), "kind": tkind}))
        elif kind == "Gap":
            notes.append(f"gap {name} ({fr(ch['source_range']['duration']) if ch.get('source_range') else '?'} frames) ignored: the following clips close up")
            items.append(("skip", {}))
        else:
            notes.append(f"{kind} {name} ignored (nested or unsupported item)")
            items.append(("skip", {}))
    if not mapped:
        raise InterchangeError(f"no clip of the OTIO video track references the source footage {want}" if skipped_media else "OTIO video track has no usable clips")
    # a transition only means something between two mapped clips
    cleaned: List[Tuple[str, Dict[str, Any]]] = []
    for k, (kind, d) in enumerate(items):
        if kind == "skip":
            continue
        if kind == "tr" and not (0 < k < len(items) - 1 and items[k - 1][0] == "clip" and items[k + 1][0] == "clip"):
            notes.append("transition next to an ignored item dropped")
            continue
        cleaned.append((kind, d))
    source = meta.get("source") or want or ""
    return assemble(cleaned, rate, meta, str(doc.get("name") or "cut"), str(source), bounds), notes
