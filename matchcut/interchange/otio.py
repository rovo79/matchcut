"""OpenTimelineIO JSON documents, written and read by hand (the OpenTimelineIO library is never imported).

Export: source times are in the media's own timecode (``SourceInfo.start_tc_frames`` + frame; the reference's available range
starts there), ``Timeline.1`` with a video track ``V1`` (``Clip.2`` per matchcut clip, ``Transition.1`` centred on each cut,
a ``Marker.2`` per junction at the cut's record frame), an audio track ``A1`` mirroring the picture when the source has
audio, and an audio track ``A2`` with the music file when music is configured.  The exact rate lives in
``metadata.matchcut.rate`` (``RationalTime.rate`` is only a float).

Import: reads the first video track back into a matchcut ``Timeline`` (overlap semantics restored, ``Clip.origin`` from
the ``clip_key`` metadata) and reports everything it ignored.
"""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from matchcut.pipeline.config import MusicConfig
from matchcut.pipeline.junctions import Junction
from matchcut.pipeline.timeline import Timeline

from . import MATCHCUT_VERSION, InterchangeError
from .common import (BY_NAME_NOTE, DIP_NAME, SHA256, LaidClip, SourceInfo, assemble, clip_key, junction_ids, junction_meta, layout, media_matcher,
                     music_window, record_frames, snap_rate, to_frames, truncate)

UNVERIFIED_DIP = "dip_to_black is UNVERIFIED: OTIO has no such transition type (Custom_Transition + metadata); how an editor treats it is untested"


# -- helpers -------------------------------------------------------------------------------------
def _rt(v: float, rate: float) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": float(v)}


def _range(start: float, dur: float, rate: float) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, rate), "duration": _rt(dur, rate)}


def _ref(src: SourceInfo, name: str) -> Dict[str, Any]:
    r = float(src.rate)
    return {"OTIO_SCHEMA": "ExternalReference.1", "name": name, "target_url": src.url,
            "available_range": _range(src.start_tc_frames, src.duration_frames, r), "available_image_bounds": None,
            "metadata": {"matchcut": {"size": src.size, "mtime_ns": src.mtime_ns}}}


def _clip(name: str, source_range: Dict[str, Any], ref: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "Clip.2", "metadata": {"matchcut": meta}, "name": name, "source_range": source_range, "effects": [], "markers": [],
            "enabled": True, "color": None, "media_references": {"DEFAULT_MEDIA": ref}, "active_media_reference_key": "DEFAULT_MEDIA"}


def _track(kind: str, name: str, children: List[Dict[str, Any]], markers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    return {"OTIO_SCHEMA": "Track.1", "metadata": {}, "name": name, "source_range": None, "effects": [], "markers": markers or [],
            "enabled": True, "color": None, "children": children, "kind": kind}


# -- export --------------------------------------------------------------------------------------
def export_otio(tl: Timeline, stage: str, src: SourceInfo, junctions: Sequence[Junction], music: Optional[MusicConfig] = None,
                music_src: Optional[SourceInfo] = None, stage_sha256: str = "") -> dict:
    """One OTIO document for ``tl`` (normally the frame-snapped copy from ``common.export_timeline``).

    ``stage_sha256`` (the sha256 of the stage file) is written as ``metadata.matchcut.stage_sha256``: a later import finds out which
    version of the stage the editor started from."""
    rate = src.rate
    r = float(rate)
    if not tl.clips:
        raise InterchangeError(f"stage {stage} has no clips to export")
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
            kids.append(_clip(f"{c.id} shot{c.shot_index}", _range(src.start_tc_frames + row.vis_in, row.dur, r), ref,
                              {"clip_id": c.id, "clip_key": clip_key(c), "shot_index": c.shot_index, "beat_id": c.beat_id, "stage": stage}))
        return kids

    markers: List[Dict[str, Any]] = []
    for j in junctions:
        if j.index + 1 < len(rows):
            jid = ids[j.index]
            markers.append({"OTIO_SCHEMA": "Marker.2", "metadata": {"matchcut": junction_meta(j, jid)}, "name": f"J{j.index + 1} s{j.out_shot}->s{j.in_shot}",
                            "color": "RED" if j.pinned else "GREEN", "marked_range": _range(rows[j.index + 1].cut, 1, r), "comment": truncate(j.why())})
    has_music = music is not None and bool(music.path)
    muted = has_music and music.only  # the render drops the source audio entirely
    tracks = [_track("Video", "V1", children(), markers)]
    if src.has_audio:
        a1 = _track("Audio", "A1", children())
        if muted:
            a1["enabled"] = False
            a1["metadata"] = {"matchcut": {"muted": True, "note": "source audio disabled: music.only drops it from the render"}}
        tracks.append(a1)
    if has_music:
        mrate = float(rate)
        start, dur, avail = music_window(music, music_src, rate, record_frames(rows))
        mref = {**_ref(music_src, Path(music_src.path).stem), "available_range": _range(0, avail, mrate)}  # the music's own time starts at 0
        tracks.append(_track("Audio", "A2", [_clip(Path(music_src.path).name, _range(start, dur, mrate), mref,
                                                   {"kind": "music", "gain_db": music.gain_db, "duck": music.duck, "start": music.start,
                                                    "fade_out": music.fade_out, "only": music.only})]))
    tmeta = {"stage": stage, "source": src.path, "rate": [rate.numerator, rate.denominator], "fade_in": tl.fade_in,
             "fade_out": tl.fade_out, "provenance_file": "provenance.json", "matchcut_version": MATCHCUT_VERSION}
    if stage_sha256:
        tmeta["stage_sha256"] = stage_sha256
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "metadata": {"matchcut": tmeta},
        "name": f"{stem} - {stage}", "global_start_time": _rt(0, r),
        "tracks": {"OTIO_SCHEMA": "Stack.1", "metadata": {}, "name": "tracks", "source_range": None, "effects": [], "markers": [],
                   "enabled": True, "color": None, "children": tracks},
    }


# -- import --------------------------------------------------------------------------------------
_SKIP: Tuple[str, Dict[str, Any]] = ("skip", {})


def _schema(item: Any) -> str:
    return str(item.get("OTIO_SCHEMA", "")).split(".")[0] if isinstance(item, dict) else ""


def _path_of(clip: Dict[str, Any]) -> Optional[str]:
    """The media file of a clip, or None when its media reference is not an ExternalReference with a target_url (a GeneratorReference,
    a MissingReference, none)."""
    refs = clip.get("media_references") or {}
    ref = refs.get(clip.get("active_media_reference_key")) or next(iter(refs.values()), None) or {}
    url = ref.get("target_url") if isinstance(ref, dict) and not _schema(ref).startswith(("Generator", "Missing")) else None
    return unquote(urlparse(url).path) if url and url.startswith("file:") else (url or None)


def _available(clip: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    refs = clip.get("media_references") or {}
    ref = refs.get(clip.get("active_media_reference_key")) or next(iter(refs.values()), None) or {}
    return ref.get("available_range")


def _is_time(t: Any) -> bool:
    return isinstance(t, dict) and isinstance(t.get("rate"), (int, float)) and isinstance(t.get("value"), (int, float)) and t["rate"] > 0


def _first_rate(vt: Dict[str, Any], usable: Callable[[Dict[str, Any]], bool] = lambda k: True) -> float:
    """The rate of the first RationalTime in the video track: the source range of a ``usable`` clip, its media's available range, a transition."""
    kids = vt.get("children") or []
    for k in kids:
        if _schema(k) == "Clip" and usable(k):
            for rng in (k.get("source_range"), _available(k)):
                if isinstance(rng, dict) and _is_time(rng.get("start_time")):
                    return float(rng["start_time"]["rate"])
    for k in kids:
        if _schema(k) == "Transition":
            for key in ("in_offset", "out_offset"):
                if _is_time(k.get(key)):
                    return float(k[key]["rate"])
    raise InterchangeError("OTIO video track has no usable time base (no clip or transition with a rate)")


def _fps(rate: Fraction) -> str:
    return f"{rate.numerator}/{rate.denominator}" if rate.denominator != 1 else str(rate.numerator)


def _meta_rate(value: Any, notes: List[str]) -> Optional[Fraction]:
    """metadata.matchcut.rate: [num, den] or a single number; anything else is reported and ignored."""
    try:
        if isinstance(value, (list, tuple)) and len(value) == 2 and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value):
            if value[0] > 0 and value[1] > 0:
                return Fraction(int(value[0]), int(value[1])) if value[0] == int(value[0]) and value[1] == int(value[1]) else snap_rate(value[0] / value[1])
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return snap_rate(float(value))
    except (ValueError, ZeroDivisionError, OverflowError):
        pass
    notes.append(f"metadata.matchcut.rate {value!r} is not a valid rate, read from the time values instead")
    return None


def import_otio(doc: dict, bounds: Optional[Dict[int, Sequence[float]]] = None, src: Optional[SourceInfo] = None) -> Tuple[Timeline, List[str]]:
    """OTIO document (a dict) -> (Timeline, notes).  Everything that cannot be mapped is listed in ``notes``.

    A malformed document raises ``InterchangeError`` (one line), never a bare KeyError or TypeError.  ``src`` (the probed source
    footage) lets a clip outside the media's length be reported.
    """
    try:
        return _import_otio(doc, bounds, src)
    except InterchangeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, ZeroDivisionError, OverflowError) as exc:
        raise InterchangeError(f"invalid OTIO document: {type(exc).__name__}: {exc}".splitlines()[0]) from exc


def placement_errors(doc: dict, rows: Sequence[LaidClip], rate: Fraction) -> List[str]:
    """How long matchcut's own OTIO tracks are against the layout: the picture track and each audio track that mirrors it hold the clips
    end to end, so their clip durations add up to the cut (and an audio clip is as long as its picture clip)."""
    tracks = (doc.get("tracks") or {}).get("children") or []
    total = record_frames(rows)

    def frames(t: Any) -> float:
        return float(t["value"]) / float(t["rate"]) * float(rate)

    def durations(track: Dict[str, Any]) -> List[float]:
        return [frames(k["source_range"]["duration"]) for k in track.get("children") or [] if _schema(k) == "Clip"]

    errs: List[str] = []
    video = next((t for t in tracks if _schema(t) == "Track" and t.get("kind") == "Video"), None)
    if video is None:
        return ["the document has no video track"]
    want = [r.dur for r in rows]
    for t in tracks:
        if _schema(t) != "Track" or t.get("kind") not in ("Video", "Audio"):
            continue
        clips = [k for k in t.get("children") or [] if _schema(k) == "Clip"]
        if t.get("kind") == "Audio" and any(((k.get("metadata") or {}).get("matchcut") or {}).get("kind") == "music" for k in clips):
            continue  # the music clip is a window of the music file, not the picture's mirror
        name = t.get("name") or t.get("kind")
        try:
            got = durations(t)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            errs.append(f"track {name}: a clip has no usable duration")
            continue
        if len(got) != len(rows):
            errs.append(f"track {name} has {len(got)} clips for {len(rows)}")
        elif any(abs(g - w) > 1e-6 for g, w in zip(got, want)):
            k = next(i for i, (g, w) in enumerate(zip(got, want)) if abs(g - w) > 1e-6)
            errs.append(f"track {name}: clip {k + 1} lasts {got[k]:g} frames != {want[k]}")
        if abs(sum(got) - total) > 1e-6:
            errs.append(f"track {name} lasts {sum(got):g} frames != the cut's {total}")
    return errs


def stage_fingerprint(doc: Any) -> str:
    """The ``stage_sha256`` an export wrote into the document's timeline metadata ("" when there is none or it is not a sha256)."""
    meta = ((doc.get("metadata") or {}).get("matchcut") or {}) if isinstance(doc, dict) else {}
    value = meta.get("stage_sha256") if isinstance(meta, dict) else None
    return value if isinstance(value, str) and SHA256.fullmatch(value) else ""


def _import_otio(doc: dict, bounds: Optional[Dict[int, Sequence[float]]], src: Optional[SourceInfo]) -> Tuple[Timeline, List[str]]:
    if not isinstance(doc, dict):
        raise InterchangeError("invalid OTIO document: not a JSON object")
    meta = (doc.get("metadata") or {}).get("matchcut") or {}
    tracks = (doc.get("tracks") or {}).get("children") or []
    notes: List[str] = []
    videos = [t for t in tracks if _schema(t) == "Track" and t.get("kind") == "Video"]
    if not videos:
        raise InterchangeError("OTIO document has no video track")
    vt = videos[0]
    for extra in videos[1:]:
        notes.append(f"video track {extra.get('name') or '?'} ignored (only the first video track is read)")
    for t in tracks:
        if _schema(t) == "Track" and t.get("kind") == "Audio" and any(
                _schema(k) == "Clip" and "matchcut" not in (k.get("metadata") or {}) for k in t.get("children") or []):
            notes.append(f"audio track {t.get('name') or '?'} ignored (matchcut reads the picture only)")
    if not any(_schema(k) == "Clip" for k in vt.get("children") or []):
        raise InterchangeError("OTIO video track has no clips")
    paths = [p for p in (_path_of(k) for k in vt["children"] if _schema(k) == "Clip") if p]
    # the workspace's own footage decides (its exact path first, the same file name only when no clip has it); without it, the file's declared source
    want = src.path if src is not None else (meta.get("source") or next(iter(paths), ""))
    is_source, by_name = media_matcher(paths, want) if want else (lambda p: False, False)
    rate = _meta_rate(meta["rate"], notes) if "rate" in meta else None  # the timeline's rate: its metadata, else the first source clip's time base
    if rate is None:
        try:
            rate = snap_rate(_first_rate(vt, lambda k: (_path_of(k) is not None and is_source(_path_of(k))) if src is not None else True))
        except InterchangeError:
            if src is None:
                raise
    if src is not None:
        # frames are the media's frames: every RationalTime is converted through seconds at the media's own rate, on whatever base it is written
        if rate is not None and rate != src.rate and float(rate) <= 120:
            notes.append(f"the timeline runs at {_fps(rate)} fps and the media at {_fps(src.rate)} fps: times were converted at the media's rate")
        rate = src.rate
    rf = float(rate)

    def fr(t: Any) -> int:
        """RationalTime -> frames at ``rate`` (a time on another base, e.g. a re-export at 29.97, is converted)."""
        if not _is_time(t):
            raise ValueError("RationalTime without a usable rate and value")
        tr_ = float(t["rate"])
        if abs(tr_ - rf) < 1e-6 * max(rf, 1.0):
            return int(round(float(t["value"])))
        return to_frames(float(t["value"]) / float(snap_rate(tr_)), rate)
    items: List[Tuple[str, Dict[str, Any]]] = []
    mapped = 0
    skipped_media = 0
    offsets: Dict[int, int] = {}
    for ch in vt.get("children") or []:
        kind = _schema(ch)
        name = ch.get("name") or kind
        if kind == "Clip":
            path = _path_of(ch)
            if path is None:
                skipped_media += 1
                notes.append(f"clip {name}: generator/missing media ignored")
                items.append(_SKIP)
                continue
            if not is_source(path):
                skipped_media += 1
                notes.append(f"clip {name}: other media {path} ignored")
                items.append(_SKIP)
                continue
            avail = _available(ch)
            sr = ch.get("source_range") or avail
            try:
                vis_in, dur = fr(sr["start_time"]), fr(sr["duration"])
                # source_range is in the media's own time: the media's start (source timecode) is its frame 0
                origin = fr(avail["start_time"]) if isinstance(avail, dict) and _is_time(avail.get("start_time")) else 0
                if not ch.get("source_range"):
                    notes.append(f"clip {name}: no source range, the media's whole available range used")
            except (KeyError, TypeError, ValueError):
                notes.append(f"clip {name}: no usable source range, ignored")
                items.append(_SKIP)
                continue
            if origin:
                offsets[origin] = offsets.get(origin, 0) + 1
            for eff in ch.get("effects") or []:
                notes.append(f"clip {name}: effect {eff.get('name') or _schema(eff)} ignored")
            mapped += 1
            items.append(("clip", {"vis_in": vis_in - origin, "dur": dur, "name": name, "meta": (ch.get("metadata") or {}).get("matchcut") or {}}))
        elif kind == "Transition":
            tm = (ch.get("metadata") or {}).get("matchcut") or {}
            # a dip to black: matchcut's own metadata, else (as in FCPXML) a Custom_Transition that is called one
            dip = tm.get("kind") == "dip_to_black" or (ch.get("transition_type") == "Custom_Transition" and bool(DIP_NAME.search(str(ch.get("name") or ""))))
            tkind = "dip_to_black" if dip else "dissolve"
            try:
                items.append(("tr", {"in": fr(ch.get("in_offset")), "out": fr(ch.get("out_offset")), "kind": tkind}))
            except ValueError:
                notes.append(f"transition {name}: in_offset or out_offset missing or invalid, ignored")
                items.append(_SKIP)
        elif kind == "Gap":
            try:
                length = str(fr(ch["source_range"]["duration"]))
            except (KeyError, TypeError, ValueError):
                length = "?"
            notes.append(f"gap {name} ({length} frames) ignored: the following clips close up")
            items.append(_SKIP)
        else:
            notes.append(f"{kind or 'item'} {name} ignored (nested or unsupported item)")
            items.append(_SKIP)
    if not mapped:
        raise InterchangeError(f"no clip of the OTIO video track references the source footage {want}" if skipped_media and want
                               else "OTIO video track has no clip with a media file" if skipped_media else "OTIO video track has no usable clips")
    if by_name:
        notes.append(BY_NAME_NOTE.format(want=want))
    for origin, n in sorted(offsets.items()):
        notes.append(f"source start timecode of {origin} frames subtracted from the source times of {n} clip{'s' if n != 1 else ''}")
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
    return assemble(cleaned, rate, meta, str(doc.get("name") or "cut"), str(source), bounds, notes, src.duration_frames if src else None), notes
