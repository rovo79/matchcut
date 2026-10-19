"""FCPXML 1.10, written and read by hand (``xml.etree`` only).

Export: one ``library/event`` with one ``project`` per exported stage.  The source is one ``format`` (the exact
``frameDuration`` of the footage) and one ``asset``; the spine holds an ``asset-clip`` per matchcut clip and a
``transition`` centred on each cut (``Cross Dissolve``; ``Fade To Color`` for a dip to black, unverified).  Every junction
leaves a ``marker`` (value: the junction id and its shots, note: why it was chosen, cut to 240 characters) and a
``keyword`` ``mc:<junction id>`` on the incoming clip; clips and sequences carry ``com.matchcut.*`` ``md`` keys.

Music is an ``asset-clip`` connected to the first spine clip on ``lane="-1"`` with ``<adjust-volume amount="<gain>dB"/>``.
``music.only`` (the render drops the source audio) is written as ``srcEnable="video"`` on every spine clip: the spine then
plays the picture only.  Its window follows the renderer exactly like the OTIO ``A2`` track (``common.music_window``).

Import: any time base (rational seconds on any grid), renumbered resource ids and lost metadata are accepted; everything
that cannot be mapped (connected items, other media, unknown assets, gaps, effects, retimes, nested items) is listed in the
notes, and every parse failure is a one-line ``InterchangeError``.
"""
from __future__ import annotations

import hashlib
import math
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from matchcut.pipeline.config import MusicConfig
from matchcut.pipeline.junctions import Junction
from matchcut.pipeline.timeline import Timeline

from . import MATCHCUT_VERSION, InterchangeError
from .common import SourceInfo, assemble, clip_key, exact_rate, junction_ids, layout, music_window, record_frames, rt, truncate

FCPXML_VERSION = "1.10"
CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"  # seen in real FCP exports (research: secondary source)
FADE_TO_COLOR_UID = "FxPlug:F779C565-486D-4633-8035-0374B4DB8F5C"  # UNVERIFIED in FCP and Resolve
UNVERIFIED_DIP = "dip_to_black is UNVERIFIED: FCPXML 'Fade To Color' uid and colour parameter are untested in Final Cut Pro and Resolve"
MD = "com.matchcut."
_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")  # characters XML 1.0 cannot carry


# -- export --------------------------------------------------------------------------------------
def _x(value: Any) -> str:
    return _XML_BAD.sub("", str(value))


def _md(parent: ET.Element, **kv: Any) -> None:
    m = parent.find("metadata")
    if m is None:
        m = ET.SubElement(parent, "metadata")
    for k, v in kv.items():
        ET.SubElement(m, "md", key=MD + k, value=_x(v), type="string", editable="1", source="custom")


def _db(gain: float) -> str:
    """A gain as FCPXML writes it: ``-6dB``, ``-7.5dB``, ``0dB``."""
    text = ("%.4f" % gain).rstrip("0").rstrip(".")
    return f"{'0' if text in ('', '-0') else text}dB"


def _uid(src: SourceInfo) -> str:
    """A stable asset uid from the file's content key (path, size, mtime)."""
    return "com.matchcut.source." + hashlib.sha1(f"{src.path}|{src.size}|{src.mtime_ns}".encode("utf-8")).hexdigest()[:16]


def export_fcpxml(stages: Sequence[Tuple[str, Timeline, Sequence[Junction]]], src: SourceInfo, music: Optional[MusicConfig] = None,
                  music_src: Optional[SourceInfo] = None, name: str = "matchcut") -> str:
    """One FCPXML 1.10 document: an event ``name`` with a project ``"<name> - <stage>"`` per stage (timelines normally frame-snapped)."""
    rate = src.rate
    has_music = music is not None and bool(music.path)
    muted = bool(has_music and music.only and src.has_audio)  # the render drops the source audio entirely
    root = ET.Element("fcpxml", version=FCPXML_VERSION)
    res = ET.SubElement(root, "resources")
    ET.SubElement(res, "format", id="r1", frameDuration=rt(1, rate), width=str(src.width), height=str(src.height))
    asset = ET.SubElement(res, "asset", id="r2", name=_x(Path(src.path).stem), uid=_uid(src), start="0s", duration=rt(src.duration_frames, rate),
                          hasVideo="1", hasAudio="1" if src.has_audio else "0", format="r1")
    ET.SubElement(asset, "media-rep", kind="original-media", src=src.url)
    ET.SubElement(res, "effect", id="r3", name="Cross Dissolve", uid=CROSS_DISSOLVE_UID)
    laid = [layout(tl, rate) for _, tl, _ in stages]
    if any(r.kind == "dip_to_black" for rows in laid for r in rows):
        ET.SubElement(res, "effect", id="r4", name="Fade To Color", uid=FADE_TO_COLOR_UID)  # UNVERIFIED; only declared when a dip is used
    if has_music:
        if music_src is None:
            raise InterchangeError(f"music file not probed: {music.path}")
        masset = ET.SubElement(res, "asset", id="r5", name=_x(Path(music_src.path).stem), uid=_uid(music_src), start="0s",
                               duration=rt(music_window(music, music_src, rate, 10 ** 9)[2], rate), hasVideo="0", hasAudio="1")
        ET.SubElement(masset, "media-rep", kind="original-media", src=music_src.url)
    event = ET.SubElement(ET.SubElement(root, "library"), "event", name=_x(name))
    for (stage, tl, junctions), rows in zip(stages, laid):
        ids = junction_ids(tl)
        by_in = {j.index + 1: (j, ids[j.index]) for j in junctions if 0 <= j.index < len(ids)}  # incoming-clip index -> junction
        seq = ET.SubElement(ET.SubElement(event, "project", name=_x(f"{name} - {stage}")), "sequence", format="r1",
                            duration=rt(record_frames(rows), rate), tcStart="0s", tcFormat="NDF")
        spine = ET.SubElement(seq, "spine")
        for i, (c, row) in enumerate(zip(tl.clips, rows)):
            if row.tr:
                kind, in_off, _ = row.transition
                dip = kind == "dip_to_black"
                tr = ET.SubElement(spine, "transition", name="Dip To Black" if dip else "Cross Dissolve", offset=rt(row.cut - in_off, rate),
                                   duration=rt(row.tr, rate))
                fv = ET.SubElement(tr, "filter-video", ref="r4" if dip else "r3", name="Fade To Color" if dip else "Cross Dissolve")
                if dip:
                    ET.SubElement(fv, "param", name="color", key="3", value="0 0 0 1")
                _md(tr, kind=kind, **({"junction_id": by_in[i][1]} if i in by_in else {}), **({"unverified": UNVERIFIED_DIP} if dip else {}))
            ac = ET.SubElement(spine, "asset-clip", name=_x(f"{c.id} shot{c.shot_index}"), ref="r2", offset=rt(row.cut, rate), start=rt(row.vis_in, rate),
                               duration=rt(row.dur, rate), tcFormat="NDF")
            if muted:
                ac.set("srcEnable", "video")
            if i == 0 and has_music:  # connected to the first clip: its offset is in that clip's own time (where the cut starts)
                start, dur, _ = music_window(music, music_src, rate, record_frames(rows))
                mc = ET.SubElement(ac, "asset-clip", name=_x(Path(music_src.path).name), ref="r5", lane="-1", offset=rt(row.vis_in, rate),
                                   start=rt(start, rate), duration=rt(dur, rate))
                ET.SubElement(mc, "adjust-volume", amount=_db(music.gain_db))
                _md(mc, kind="music", gain_db=music.gain_db, duck=music.duck, start=music.start, fade_out=music.fade_out, only=music.only)
            if i in by_in:  # marker + keyword (the likeliest to survive an editor) on the incoming clip
                j, jid = by_in[i]
                ET.SubElement(ac, "marker", start=rt(row.vis_in, rate), duration=rt(1, rate), value=_x(f"{jid} (s{j.out_shot} -> s{j.in_shot})"),
                              note=_x(truncate(j.why())))
                ET.SubElement(ac, "keyword", start=rt(row.vis_in, rate), duration=rt(row.dur, rate), value=_x(f"mc:{jid}"))
            _md(ac, clip_key=clip_key(c), clip_id=c.id, shot_index=c.shot_index, beat_id=c.beat_id, stage=stage)
        smd: Dict[str, Any] = {"stage": stage, "fade_in": tl.fade_in, "fade_out": tl.fade_out, "provenance_file": "provenance.json",
                               "matchcut_version": MATCHCUT_VERSION}
        if muted:
            smd["source_audio"] = "disabled (srcEnable=video on every clip): music.only drops the source audio from the render"
        _md(seq, **smd)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(root, encoding="unicode") + "\n"


# -- import --------------------------------------------------------------------------------------
_SKIP: Tuple[str, Dict[str, Any]] = ("skip", {})
_DIP = re.compile(r"dip|fade to (color|colour|black)", re.I)


def _time(text: Optional[str]) -> Fraction:
    """FCPXML time -> seconds: ``"12s"``, ``"0s"``, ``"1001/3000s"``, ``"2.5s"``."""
    s = (text or "").strip()
    if not s.endswith("s"):
        raise ValueError(f"invalid time {text!r}")
    try:
        return Fraction(s[:-1])
    except ZeroDivisionError as exc:
        raise ValueError(f"invalid time {text!r}") from exc


def _md_of(el: Optional[ET.Element]) -> Dict[str, str]:
    if el is None:
        return {}
    return {m.get("key", "")[len(MD):]: m.get("value", "") for m in el.findall("metadata/md") if m.get("key", "").startswith(MD)}


def _path_of(asset: Optional[ET.Element]) -> Optional[str]:
    if asset is None:
        return None
    reps = asset.findall("media-rep")
    rep = next((r for r in reps if r.get("kind", "original-media") == "original-media"), reps[0] if reps else None)
    url = rep.get("src") if rep is not None else asset.get("src")
    if not url:
        return None
    return unquote(urlparse(url).path) if url.startswith("file:") else url


def _same_media(a: Optional[str], b: Optional[str]) -> bool:
    return a is None or b is None or a == b or Path(a).name == Path(b).name


def _is_music(el: ET.Element) -> bool:
    return _md_of(el).get("kind") == "music"


def _stage_of(project: ET.Element) -> str:
    return _md_of(project.find("sequence")).get("stage", "")


def _select(root: ET.Element, stage: Optional[str]) -> Tuple[ET.Element, List[ET.Element]]:
    projects = [p for p in root.iter("project") if p.find("sequence") is not None]
    if not projects:
        raise InterchangeError("FCPXML has no project with a sequence")
    if stage is None:
        return projects[0], projects[1:]
    for p in projects:
        pname = p.get("name", "")
        if _stage_of(p) == stage or (not _stage_of(p) and (pname == stage or pname.endswith(f" - {stage}"))):
            return p, [q for q in projects if q is not p]
    have = ", ".join(_stage_of(p) or p.get("name", "?") for p in projects)
    raise InterchangeError(f"no project for stage {stage!r} (the file has: {have})")


def _parse(xml: Any) -> ET.Element:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise InterchangeError(f"invalid FCPXML: {exc}".splitlines()[0]) from exc
    if root.tag != "fcpxml":
        raise InterchangeError("not an FCPXML document (the root element is not <fcpxml>)")
    return root


def import_fcpxml(xml: Any, stage: Optional[str] = None, bounds: Optional[Dict[int, Sequence[float]]] = None) -> Tuple[Timeline, List[str]]:
    """FCPXML text -> (Timeline, notes) for the project of ``stage`` (the first project when None).  Anything not mapped is in ``notes``.

    A malformed file raises ``InterchangeError`` (one line), never a bare KeyError or ValueError.
    """
    try:
        return _import(xml, stage, bounds)
    except InterchangeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, ZeroDivisionError, OverflowError, ET.ParseError) as exc:
        raise InterchangeError(f"invalid FCPXML: {type(exc).__name__}: {exc}".splitlines()[0]) from exc


def _import(xml: Any, stage: Optional[str], bounds: Optional[Dict[int, Sequence[float]]]) -> Tuple[Timeline, List[str]]:
    root = _parse(xml)
    proj, others = _select(root, stage)
    seq = proj.find("sequence")
    notes: List[str] = []
    if stage is None and others:
        notes.append(f"{len(others)} other project{'s' if len(others) != 1 else ''} ignored (only the first is read; name a stage to read another)")
    resources = root.find("resources")
    formats = {f.get("id"): f for f in (resources.findall("format") if resources is not None else [])}
    assets = {a.get("id"): a for a in (resources.findall("asset") if resources is not None else [])}
    effects = {e.get("id"): e for e in (resources.findall("effect") if resources is not None else [])}
    fmt = formats.get(seq.get("format"))
    if fmt is None:
        fmt = next((f for f in formats.values() if f.get("frameDuration")), None)
    try:
        frame = _time(fmt.get("frameDuration")) if fmt is not None else Fraction(0)
    except ValueError:
        frame = Fraction(0)
    if frame <= 0:
        raise InterchangeError("FCPXML sequence has no usable frame duration (its format has none)")
    rate = exact_rate(1 / frame)

    def fr(text: Optional[str]) -> int:
        """Time -> whole frames at ``rate`` (halves rounded up): editors write times on any common grid."""
        return math.floor(_time(text) * rate + Fraction(1, 2))

    spine = seq.find("spine")
    kids = list(spine) if spine is not None else []
    if not any(k.tag == "asset-clip" for k in kids):
        raise InterchangeError("FCPXML spine has no clips")
    want: Optional[str] = None
    for k in kids:
        a = assets.get(k.get("ref")) if k.tag == "asset-clip" else None
        if a is not None:
            want = _path_of(a)
            break
    items: List[Tuple[str, Dict[str, Any]]] = []
    mapped = 0
    skipped_media = 0
    offsets: Dict[int, int] = {}
    pending: Optional[Dict[str, Any]] = None  # the transition just before the element being read
    pos = 0  # record frame after the previous clip: the offset of a clip that has none
    source = ""
    for el in kids:
        tag = el.tag
        name = el.get("name") or tag
        before, pending = pending, None  # a transition belongs to the element right after it
        if tag == "transition":
            fv = el.find("filter-video")
            eff = effects.get(fv.get("ref")) if fv is not None else None
            label = " ".join([name, fv.get("name", "") if fv is not None else "", eff.get("name", "") if eff is not None else ""])
            dip = _md_of(el).get("kind") == "dip_to_black" or bool(_DIP.search(label)) or (eff is not None and eff.get("uid") == FADE_TO_COLOR_UID)
            try:
                dur = fr(el.get("duration"))
                off: Optional[int] = fr(el.get("offset")) if el.get("offset") else None
            except ValueError:
                notes.append(f"transition {name}: duration or offset missing or invalid, ignored")
                items.append(_SKIP)
                continue
            if dur <= 0:
                notes.append(f"transition {name}: zero length, ignored")
                items.append(_SKIP)
                continue
            if off is None:
                notes.append(f"transition {name}: no offset, read as centred on the cut")
            pending = {"off": off, "dur": dur, "kind": "dip_to_black" if dip else "dissolve"}
            items.append(("trpending", pending))
        elif tag == "asset-clip":
            asset = assets.get(el.get("ref"))
            if asset is None:
                notes.append(f"clip {name}: unknown asset {el.get('ref') or '?'} ignored")
                items.append(_SKIP)
                continue
            path = _path_of(asset)
            if not _same_media(path, want):
                skipped_media += 1
                notes.append(f"clip {name}: other media {path} ignored")
                items.append(_SKIP)
                continue
            try:
                origin = fr(asset.get("start")) if asset.get("start") else 0
                vis_in = fr(el.get("start")) if el.get("start") else origin
                if el.get("duration"):
                    dur = fr(el.get("duration"))
                elif asset.get("duration"):
                    dur = fr(asset.get("duration")) + origin - vis_in
                else:
                    raise ValueError("no duration")
                cut = fr(el.get("offset")) if el.get("offset") else pos
                if dur < 1:
                    raise ValueError("empty")
            except ValueError:
                notes.append(f"clip {name}: no usable start, duration or offset, ignored")
                items.append(_SKIP)
                continue
            if origin:
                offsets[origin] = offsets.get(origin, 0) + 1
            if el.get("enabled") == "0":
                notes.append(f"clip {name}: disabled in the file, read as enabled")
            for ch in el:
                lane = ch.get("lane")
                if lane not in (None, "", "0"):
                    if not _is_music(ch):
                        label = " ".join(x for x in (lane and f"lane {lane}", ch.tag, ch.get("name") or ch.get("ref")) if x)
                        notes.append(f"{label} ignored (connected items are not read)")
                elif ch.tag.startswith(("adjust-", "filter-")) or ch.tag in ("timeMap", "mute", "audio-role-source"):
                    notes.append(f"clip {name}: {ch.tag} ignored")
            pos = cut + dur
            mapped += 1
            source = source or (path or "")
            if before is not None:
                before["cut"] = cut
            items.append(("clip", {"vis_in": vis_in - origin, "dur": dur, "name": el.get("name", ""), "meta": _md_of(el)}))
        elif tag == "gap":
            try:
                length = str(fr(el.get("duration")))
            except ValueError:
                length = "?"
            notes.append(f"gap {name} ({length} frames) ignored: the following clips close up")
            items.append(_SKIP)
        else:
            notes.append(f"{tag} {name} ignored (nested or unsupported item)")
            items.append(_SKIP)
    if not mapped:
        raise InterchangeError(f"no clip of the FCPXML spine references the source footage {want}" if skipped_media else "FCPXML spine has no usable clips")
    for origin, n in sorted(offsets.items()):
        notes.append(f"source start timecode of {origin} frames subtracted from the source times of {n} clip{'s' if n != 1 else ''}")
    # a transition only means something between two mapped clips; its in/out split comes from where it starts relative to the cut
    cleaned: List[Tuple[str, Dict[str, Any]]] = []
    for k, (kind, d) in enumerate(items):
        if kind == "skip":
            continue
        if kind == "trpending":
            if not (0 < k < len(items) - 1 and items[k - 1][0] == "clip" and items[k + 1][0] == "clip"):
                notes.append("transition next to an ignored item dropped")
                continue
            dur = d["dur"]
            in_off = dur // 2 if d["off"] is None else min(max(d["cut"] - d["off"], 0), dur)  # the transition starts in_off before the cut
            cleaned.append(("tr", {"in": in_off, "out": dur - in_off, "kind": d["kind"]}))
            continue
        cleaned.append((kind, d))
    smd = _md_of(seq)
    meta: Dict[str, Any] = {"stage": smd.get("stage") or (stage or "")}
    for key in ("fade_in", "fade_out"):
        try:
            meta[key] = float(smd.get(key, 0.0) or 0.0)
        except ValueError:
            notes.append(f"{MD}{key} {smd.get(key)!r} is not a number, read as 0")
    for _, d in cleaned:
        if d.get("meta") is not None:
            idx = d["meta"].get("shot_index")
            if idx is not None:
                try:
                    d["meta"]["shot_index"] = int(idx)
                except ValueError:
                    del d["meta"]["shot_index"]
    return assemble(cleaned, rate, meta, proj.get("name") or "cut", source, bounds), notes


def fcpxml_junction_ids(xml: Any, stage: Optional[str] = None) -> List[str]:
    """The junction ids that survive in an (edited) file: per clip, its marker value (``s3-s5 (...)``), else its ``mc:`` keyword."""
    root = _parse(xml)
    out: List[str] = []
    for p in root.iter("project"):
        if stage is not None and _stage_of(p) != stage:
            continue
        for clip in p.iter("asset-clip"):
            found = None
            for m in clip.findall("marker"):
                mm = re.match(r"(s-?\d+-s-?\d+(?:#\d+)?)", m.get("value", ""))
                if mm:
                    found = mm.group(1)
                    break
            if found is None:
                for k in clip.findall("keyword"):
                    mm = re.fullmatch(r"mc:(s-?\d+-s-?\d+(?:#\d+)?)", k.get("value", ""))
                    if mm:
                        found = mm.group(1)
                        break
            if found:
                out.append(found)
    return out
