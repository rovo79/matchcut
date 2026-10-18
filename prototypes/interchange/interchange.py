"""FEASIBILITY PROOF, not a product feature: matchcut timelines <-> OTIO / FCPXML 1.10 / mpv / Resolve.

Not wired into the pipeline or the TUI. Stdlib plus matchcut's own modules only (OpenTimelineIO is
never imported here; the tests use a throwaway venv for validation). See README.md for what is
verified offline and what needs a live application.

Dissolve semantics. matchcut's dissolve of d seconds OVERLAPS the last d s of the outgoing clip with
the first d s of the incoming clip (record duration = sum - d). OTIO and FCPXML centre a transition on
the cut point. Converting exactly (d in whole frames, in_off + out_off = d, in_off = d // 2):
    outgoing visible range ends at   source_out - out_off      incoming visible range starts at source_in + in_off
    transition starts in_off before the cut and ends out_off after it; cut = matchcut record start + in_off.
Everything the transition shows lies inside [source_in, source_out] of the two clips, so no media beyond
matchcut's own ranges is needed. Transition durations are rounded to frames with error diffusion, so
the total record duration stays within half a frame of Timeline.duration.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from matchcut.pipeline.junctions import Junction
from matchcut.pipeline.media import seconds_to_timecode
from matchcut.pipeline.timeline import Clip, Decision, Timeline, Transition

LABEL = "matchcut interchange PROTOTYPE (feasibility proof, not wired into matchcut)"
CROSS_DISSOLVE_UID = "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"  # seen in real FCP exports (research: secondary source)
FADE_TO_COLOR_UID = "FxPlug:F779C565-486D-4633-8035-0374B4DB8F5C"  # UNVERIFIED in FCP and Resolve
UNVERIFIED_DIP = "dip_to_black is UNVERIFIED: no OTIO type (Custom_Transition + metadata); FCPXML 'Fade To Color' uid/colour param untested"
FCPXML_VERSION = "1.10"
Stages = Sequence[Tuple[str, Timeline]]


# -- small helpers -------------------------------------------------------------------------------
def rational_fps(fps: float) -> Tuple[int, int]:
    """Exact (num, den) for a float rate: integer rates and NTSC (24000/1001 ...) snap, others limit_denominator."""
    n = int(round(fps))
    if abs(fps - n) < 1e-3:
        return (n, 1)
    if n and abs(fps - n * 1000 / 1001) < 0.01:
        return (n * 1000, 1001)
    f = Fraction(fps).limit_denominator(10000)
    return (f.numerator, f.denominator)


def parse_time(s: Optional[str]) -> Fraction:
    """FCPXML time string -> seconds: "12s", "0s", "1001/3000s", "2.5s"; None -> 0."""
    return Fraction(s.strip()[:-1]) if s else Fraction(0)


def _plain(x: Any) -> Any:
    return json.loads(json.dumps(x, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


def _trunc(s: str, n: int = 240) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _url(path: str) -> str:
    return Path(path).resolve().as_uri()


def _from_url(url: str) -> str:
    return unquote(urlparse(url).path)


def junction_ids(js: Sequence[Junction]) -> List[str]:
    """Stable junction ids "s<out>-s<in>" (same shot pair twice in a stage: "#2", "#3" ...)."""
    seen: Dict[str, int] = {}
    out = []
    for j in js:
        base = f"s{j.out_shot}-s{j.in_shot}"
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return out


def clip_keys(tl: Timeline) -> List[str]:
    """Stable clip keys "shot<idx>@<source_in>" (a repeat gets "#2")."""
    seen: Dict[str, int] = {}
    out = []
    for c in tl.clips:
        base = f"shot{c.shot_index}@{c.source_in:g}"
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return out


def _jmeta(j: Junction, jid: str) -> dict:
    cur = j.current
    return {
        "junction_id": jid,
        "why": j.why(),
        "history": list(j.history()),
        "pinned": j.pinned,
        "pin_origin": j.pin_origin,
        "scores": _plain(cur.score.to_dict()) if cur.score is not None else None,
        "transition": {"kind": cur.transition, "duration": round(cur.transition_seconds, 4)},
    }


def _layout(tl: Timeline, fps: float) -> List[dict]:
    """Frame-grid geometry shared by every exporter and by the diff.

    Per clip: in_f/out_f (matchcut source range), tr (incoming transition, frames), rec (matchcut record start),
    in_off/out_off (centred-transition halves), vis_in/vis_out/dur (NLE-visible range), cut (NLE cut point = spine offset).
    """
    rows: List[dict] = []
    cum, done, rec = 0.0, 0, 0
    for i, c in enumerate(tl.clips):
        in_f = int(round(c.source_in * fps))
        out_f = max(int(round(c.source_out * fps)), in_f + 1)
        tr = 0
        if i and not c.transition_in.is_cut:
            cum += c.transition_in.duration
            tr = max(int(round(cum * fps)) - done, 0)  # error diffusion keeps the total within half a frame
            done += tr
        if i:
            rec = rows[-1]["rec"] + (rows[-1]["out_f"] - rows[-1]["in_f"]) - tr
        rows.append({"in_f": in_f, "out_f": out_f, "tr": tr, "kind": c.transition_in.kind if tr else "cut", "rec": rec, "in_off": tr // 2})
    for i, r in enumerate(rows):
        nxt = rows[i + 1] if i + 1 < len(rows) else None
        r["out_off"] = nxt["tr"] - nxt["in_off"] if nxt else 0
        r["vis_in"], r["vis_out"] = r["in_f"] + r["in_off"], r["out_f"] - r["out_off"]
        r["dur"], r["cut"] = r["vis_out"] - r["vis_in"], r["rec"] + r["in_off"]
        if r["dur"] < 1:
            raise ValueError(f"clip {tl.clips[i].id} is shorter than its transitions")
    return rows


def _assemble(items: List[Tuple[str, dict]], fps: float, meta: dict, name: str, source: str, bounds: Optional[Dict[int, Sequence[float]]]) -> Timeline:
    """Shared back end of both importers: [("clip", {vis_in, dur, name, meta}), ("tr", {in, out, kind})] -> Timeline.

    Converts centred transitions back to matchcut overlap semantics (source_in = vis_in - in_off, source_out = vis_end + out_off).
    """
    clips: List[Clip] = []
    seen: set = set()
    pend: Optional[dict] = None
    fresh = 0
    for k, (kind, d) in enumerate(items):
        if kind == "tr":
            pend = d
            continue
        nxt = items[k + 1][1] if k + 1 < len(items) and items[k + 1][0] == "tr" else None
        tin = pend if (pend and clips) else None
        pend = None
        in_f = d["vis_in"] - (tin["in"] if tin else 0)
        out_f = d["vis_in"] + d["dur"] + (nxt["out"] if nxt else 0)
        cm = d["meta"]
        cid = str(cm.get("clip_id") or "")
        while not cid or cid in seen:
            fresh += 1
            cid = f"h{fresh:03d}"
        seen.add(cid)
        shot = cm.get("shot_index")
        if shot is None:
            m = re.search(r"shot(\d+)", d["name"])
            shot = int(m.group(1)) if m else None
        if shot is None:  # last resort: the shot whose bounds overlap the source range most
            best = max(((max(0.0, min(b[1], out_f / fps) - max(b[0], in_f / fps)), s) for s, b in (bounds or {}).items()), default=(0.0, -1))
            shot = best[1] if best[0] > 0 else -1
        clips.append(Clip(cid, int(shot), round(in_f / fps, 4), round(out_f / fps, 4), str(cm.get("beat_id", "")),
                          transition_in=Transition(tin["kind"], round((tin["in"] + tin["out"]) / fps, 4)) if tin else Transition()))
    return Timeline(source=source, fps=fps, clips=clips, fade_in=float(meta.get("fade_in", 0.0)), fade_out=float(meta.get("fade_out", 0.0)),
                    name=name, stage=str(meta.get("stage", "")))


# -- OpenTimelineIO (JSON documents written by hand; schema names as in interchange.md) -------------
def _rt(v: float, rate: float) -> dict:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": float(v)}


def _range(start: float, dur: float, rate: float) -> dict:
    return {"OTIO_SCHEMA": "TimeRange.1", "start_time": _rt(start, rate), "duration": _rt(dur, rate)}


def export_otio(stages: Stages, media_path: str, fps_rational: Tuple[int, int], junctions_by_stage: Dict[str, List[Junction]],
                audio: bool = True, media_duration: Optional[float] = None, provenance_file: str = "provenance.json") -> Dict[str, dict]:
    """One OTIO document per stage: V1 (+ A1 mirror), transitions centred on cuts, a Marker per junction on the V1 track."""
    num, den = fps_rational
    rate = num / den
    stem = Path(media_path).stem
    docs: Dict[str, dict] = {}
    for stage, tl in stages:
        rows = _layout(tl, rate)
        js = junctions_by_stage.get(stage, [])
        jids = junction_ids(js)
        by_in = {j.index + 1: (j, jid) for j, jid in zip(js, jids)}
        ref = {"OTIO_SCHEMA": "ExternalReference.1", "metadata": {}, "name": stem,
               "available_range": _range(0, round(media_duration * rate), rate) if media_duration else None,
               "available_image_bounds": None, "target_url": _url(media_path)}

        def track(kind: str, tname: str, markers: List[dict]) -> dict:
            kids: List[dict] = []
            for i, (c, r) in enumerate(zip(tl.clips, rows)):
                if r["tr"]:
                    dip = r["kind"] == "dip_to_black"
                    tmeta = {"kind": r["kind"], "duration": c.transition_in.duration}
                    if i in by_in:
                        tmeta["junction_id"] = by_in[i][1]
                    if dip:
                        tmeta["unverified"] = UNVERIFIED_DIP
                    kids.append({"OTIO_SCHEMA": "Transition.1", "metadata": {"matchcut": tmeta}, "name": "Dip To Black" if dip else "Cross Dissolve",
                                 "in_offset": _rt(r["in_off"], rate), "out_offset": _rt(r["tr"] - r["in_off"], rate),
                                 "transition_type": "Custom_Transition" if dip else "SMPTE_Dissolve"})
                kids.append({"OTIO_SCHEMA": "Clip.2", "metadata": {"matchcut": {"clip_id": c.id, "shot_index": c.shot_index, "beat_id": c.beat_id, "stage": stage}},
                             "name": f"{c.id} shot{c.shot_index}", "source_range": _range(r["vis_in"], r["dur"], rate), "effects": [], "markers": [],
                             "enabled": True, "color": None, "media_references": {"DEFAULT_MEDIA": ref}, "active_media_reference_key": "DEFAULT_MEDIA"})
            return {"OTIO_SCHEMA": "Track.1", "metadata": {}, "name": tname, "source_range": None, "effects": [], "markers": markers,
                    "enabled": True, "color": None, "children": kids, "kind": kind}

        markers = []
        for j, jid in zip(js, jids):
            if j.index + 1 < len(rows):
                markers.append({"OTIO_SCHEMA": "Marker.2", "metadata": {"matchcut": _jmeta(j, jid)}, "name": f"J{j.index + 1} s{j.out_shot}->s{j.in_shot}",
                                "color": "RED" if j.pinned else "GREEN", "marked_range": _range(rows[j.index + 1]["cut"], 1, rate), "comment": _trunc(j.why())})
        kids = [track("Video", "V1", markers)] + ([track("Audio", "A1", [])] if audio else [])
        docs[stage] = {
            "OTIO_SCHEMA": "Timeline.1",
            "metadata": {"matchcut": {"stage": stage, "source": tl.source, "fps": [num, den], "fade_in": tl.fade_in, "fade_out": tl.fade_out,
                                      "provenance_file": provenance_file, "label": LABEL}},
            "name": f"{stem} - {stage}", "global_start_time": _rt(0, rate),
            "tracks": {"OTIO_SCHEMA": "Stack.1", "metadata": {}, "name": "tracks", "source_range": None, "effects": [], "markers": [],
                       "enabled": True, "color": None, "children": kids},
        }
    return docs


def import_otio(doc: dict, shot_bounds: Optional[Dict[int, Sequence[float]]] = None) -> Timeline:
    """OTIO document (dict) -> matchcut Timeline from the first Video track. Gaps and other tracks are ignored."""
    meta = doc.get("metadata", {}).get("matchcut", {})
    vt = next(t for t in doc["tracks"]["children"] if t.get("kind") == "Video")
    first = next(c for c in vt["children"] if c["OTIO_SCHEMA"].startswith("Clip"))
    fps = meta["fps"][0] / meta["fps"][1] if meta.get("fps") else first["source_range"]["start_time"]["rate"]
    fr = lambda rt: int(round(rt["value"] / rt["rate"] * fps))  # noqa: E731
    items: List[Tuple[str, dict]] = []
    for ch in vt["children"]:
        schema = ch["OTIO_SCHEMA"].split(".")[0]
        if schema == "Clip":
            sr = ch["source_range"]
            items.append(("clip", {"vis_in": fr(sr["start_time"]), "dur": fr(sr["duration"]), "name": ch.get("name", ""), "meta": ch.get("metadata", {}).get("matchcut", {})}))
        elif schema == "Transition":
            tm = ch.get("metadata", {}).get("matchcut", {})
            kind = "dip_to_black" if tm.get("kind") == "dip_to_black" else "dissolve"
            items.append(("tr", {"in": fr(ch["in_offset"]), "out": fr(ch["out_offset"]), "kind": kind}))
    ref = next(iter(first.get("media_references", {}).values()), {})
    source = meta.get("source") or (_from_url(ref["target_url"]) if ref.get("target_url") else "")
    return _assemble(items, fps, meta, doc.get("name", "cut"), source, shot_bounds)


def otio_junction_ids(doc: dict) -> List[str]:
    """Junction ids of the markers that survive in an (edited) OTIO document: metadata first, marker name as fallback."""
    vt = next(t for t in doc["tracks"]["children"] if t.get("kind") == "Video")
    out = []
    for m in vt.get("markers", []):
        jid = m.get("metadata", {}).get("matchcut", {}).get("junction_id")
        mm = re.search(r"(s\d+)->(s\d+)", m.get("name", ""))
        out.append(jid or (f"{mm.group(1)}-{mm.group(2)}" if mm else ""))
    return out


# -- FCPXML 1.10 ---------------------------------------------------------------------------------
def export_fcpxml(stages: Stages, media_path: str, fps_rational: Tuple[int, int], junctions_by_stage: Dict[str, List[Junction]],
                  size: Tuple[int, int] = (1920, 1080), has_audio: bool = True, media_duration: Optional[float] = None,
                  provenance_file: str = "provenance.json") -> str:
    """FCPXML 1.10: one event, one project per stage, spine of asset-clips, centred transitions, a marker per junction."""
    num, den = fps_rational
    rate = num / den
    stem = Path(media_path).stem

    def T(f: int) -> str:  # frames -> rational seconds string on the frame grid ("1001/24000s", "2s")
        v = f * den
        return f"{v // num}s" if v % num == 0 else f"{v}/{num}s"

    def md(parent: ET.Element, **kv: Any) -> None:
        m = parent.find("metadata")
        m = m if m is not None else ET.SubElement(parent, "metadata")
        for k, v in kv.items():
            ET.SubElement(m, "md", key=f"com.matchcut.{k}", value=str(v), type="string", editable="1", source="custom")

    root = ET.Element("fcpxml", version=FCPXML_VERSION)
    res = ET.SubElement(root, "resources")
    ET.SubElement(res, "format", id="r1", frameDuration=T(1), width=str(size[0]), height=str(size[1]))
    asset = ET.SubElement(res, "asset", id="r2", name=stem, uid="com.matchcut.source." + hashlib.sha1(str(Path(media_path).resolve()).encode()).hexdigest()[:12],
                          start="0s", hasVideo="1", hasAudio="1" if has_audio else "0", format="r1")
    if media_duration:
        asset.set("duration", T(round(media_duration * rate)))
    ET.SubElement(asset, "media-rep", kind="original-media", src=_url(media_path))
    ET.SubElement(res, "effect", id="r3", name="Cross Dissolve", uid=CROSS_DISSOLVE_UID)
    if any(r["kind"] == "dip_to_black" for _, tl in stages for r in _layout(tl, rate)):
        ET.SubElement(res, "effect", id="r4", name="Fade To Color", uid=FADE_TO_COLOR_UID)  # UNVERIFIED; only declared when a dip is used
    event = ET.SubElement(ET.SubElement(root, "library"), "event", name="matchcut")
    for stage, tl in stages:
        rows = _layout(tl, rate)
        js = junctions_by_stage.get(stage, [])
        by_in = {j.index + 1: (j, jid) for j, jid in zip(js, junction_ids(js))}
        seq = ET.SubElement(ET.SubElement(event, "project", name=f"{stem} - {stage}"), "sequence", format="r1",
                            duration=T(rows[-1]["cut"] + rows[-1]["dur"] if rows else 0), tcStart="0s", tcFormat="NDF")
        spine = ET.SubElement(seq, "spine")
        for i, (c, r) in enumerate(zip(tl.clips, rows)):
            if r["tr"]:
                dip = r["kind"] == "dip_to_black"
                tr = ET.SubElement(spine, "transition", name="Dip To Black" if dip else "Cross Dissolve", offset=T(r["cut"] - r["in_off"]), duration=T(r["tr"]))
                fv = ET.SubElement(tr, "filter-video", ref="r4" if dip else "r3", name="Transition" if dip else "Cross Dissolve")
                if dip:
                    ET.SubElement(fv, "param", name="color", key="3", value="0 0 0 1")
                    md(tr, unverified=UNVERIFIED_DIP)
                else:
                    md(tr, kind="dissolve")
            ac = ET.SubElement(spine, "asset-clip", name=f"{c.id} shot{c.shot_index}", ref="r2", offset=T(r["cut"]), start=T(r["vis_in"]), duration=T(r["dur"]), tcFormat="NDF")
            if i in by_in:  # marker + keyword (the likeliest to survive an editor) + md on the incoming clip
                j, jid = by_in[i]
                ET.SubElement(ac, "marker", start=T(r["vis_in"]), duration=T(1), value=f"J{i} s{j.out_shot}->s{j.in_shot}", note=_trunc(j.why()))
                ET.SubElement(ac, "keyword", start=T(r["vis_in"]), duration=T(r["dur"]), value=f"mc:{jid}")
            md(ac, clip_id=c.id, shot_index=c.shot_index, beat_id=c.beat_id, stage=stage, **({"junction": by_in[i][1]} if i in by_in else {}))
        md(seq, stage=stage, fade_in=tl.fade_in, fade_out=tl.fade_out, provenance_file=provenance_file, label=LABEL)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n' + ET.tostring(root, encoding="unicode") + "\n"


def import_fcpxml(xml: str, stage: Optional[str] = None, shot_bounds: Optional[Dict[int, Sequence[float]]] = None) -> Timeline:
    """FCPXML -> Timeline for the project whose sequence md says ``stage`` (first project if None). Any rational timebase is accepted."""
    root = ET.fromstring(xml)
    res = root.find("resources")
    frame = parse_time(res.find("format").get("frameDuration"))
    fps_frac = 1 / frame
    fps = float(fps_frac)
    fr = lambda s: int(round(parse_time(s) * fps_frac))  # noqa: E731
    effects = {e.get("id"): e.get("name", "") for e in res.findall("effect")}
    assets = {a.get("id"): a for a in res.findall("asset")}
    mdd = lambda el: {m.get("key")[len("com.matchcut."):]: m.get("value") for m in el.findall("metadata/md") if m.get("key", "").startswith("com.matchcut.")}  # noqa: E731
    projects = root.findall(".//project")
    proj = next((p for p in projects if stage is None or mdd(p.find("sequence")).get("stage") == stage), None)
    if proj is None:
        raise KeyError(f"no project for stage {stage!r}")
    seq = proj.find("sequence")
    items: List[Tuple[str, dict]] = []
    pending: Optional[Tuple[int, int, str]] = None  # (offset, duration, kind) of the transition awaiting its incoming clip
    src = ""
    for el in seq.find("spine"):
        if el.tag == "transition":
            fv = el.find("filter-video")
            name = el.get("name", "") + " " + (effects.get(fv.get("ref"), "") if fv is not None else "")
            pending = (fr(el.get("offset")), fr(el.get("duration")), "dip_to_black" if re.search(r"dip|fade to color", name, re.I) else "dissolve")
        elif el.tag == "asset-clip":
            cut = fr(el.get("offset"))
            if pending:
                tr_off, dur, kind = pending
                in_off = min(max(cut - tr_off, 0), dur)  # the transition starts in_off before the cut
                items.append(("tr", {"in": in_off, "out": dur - in_off, "kind": kind}))
                pending = None
            items.append(("clip", {"vis_in": fr(el.get("start")), "dur": fr(el.get("duration")), "name": el.get("name", ""), "meta": mdd(el)}))
            a = assets.get(el.get("ref"))
            if a is not None and not src:
                rep = a.find("media-rep")
                src = _from_url(rep.get("src")) if rep is not None else ""
    meta = mdd(seq)
    return _assemble(items, fps, meta, proj.get("name", "cut"), src, shot_bounds)


def fcpxml_junction_ids(xml: str, stage: Optional[str] = None) -> List[str]:
    """Junction ids of the markers that survive in an (edited) FCPXML: marker value "J<n> s<o>->s<i>"."""
    out = []
    for p in ET.fromstring(xml).findall(".//project"):
        if stage is None or any(m.get("key") == "com.matchcut.stage" and m.get("value") == stage for m in p.findall("sequence/metadata/md")):
            for m in p.iter("marker"):
                mm = re.search(r"(s\d+)->(s\d+)", m.get("value", ""))
                if mm:
                    out.append(f"{mm.group(1)}-{mm.group(2)}")
    return out


# -- provenance sidecar --------------------------------------------------------------------------
def write_provenance(path: Path, stages: Stages, junctions_by_stage: Dict[str, List[Junction]], media_path: str, fps_rational: Tuple[int, int]) -> dict:
    """provenance.json: the canonical, full decision history per junction for every stage, keyed by stable ids."""
    doc: Dict[str, Any] = {"schema": "matchcut.provenance/1", "label": LABEL, "source": str(media_path), "fps": list(fps_rational),
                           "id_scheme": {"junction": "s<out_shot>-s<in_shot> (#n for repeats)", "clip": "shot<idx>@<source_in>"},
                           "stage_order": [s for s, _ in stages], "stages": {}}
    for stage, tl in stages:
        js = junctions_by_stage.get(stage, [])
        jr: Dict[str, Any] = {}
        for j, jid in zip(js, junction_ids(js)):
            stage_of = list(getattr(j, "decision_stage", []))
            level = list(getattr(j, "clip_level", []))
            jr[jid] = dict(_jmeta(j, jid), index=j.index, out_clip=j.out_clip, in_clip=j.in_clip, beat_boundary=j.beat_boundary,
                           states=[{"stage": s.stage, "out_time": s.out_time, "in_time": s.in_time, "transition": s.transition,
                                    "transition_seconds": s.transition_seconds, "locked_out": s.locked_out, "locked_in": s.locked_in,
                                    "combined": s.score.combined if s.score is not None else None} for s in j.states],
                           decisions=[dict(d.to_dict(), stage=stage_of[k] if k < len(stage_of) else "", clip_level=bool(level[k]) if k < len(level) else False)
                                      for k, d in enumerate(j.decisions)])
        doc["stages"][stage] = {
            "timeline": {"name": tl.name, "duration": round(tl.duration, 4), "fade_in": tl.fade_in, "fade_out": tl.fade_out},
            "clips": {k: dict(c.to_dict(), key=k) for k, c in zip(clip_keys(tl), tl.clips)},
            "junctions": jr, "history": [d.to_dict() for d in tl.history]}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_plain(doc), indent=1))
    return doc


# -- human edits back ----------------------------------------------------------------------------
def _match(canon: Timeline, edited: Timeline) -> Dict[int, int]:
    """canonical clip index -> edited clip index by lineage (same shot, overlapping source range), best overlap first, one to one."""
    cf, ef = _layout(canon, canon.fps), _layout(edited, edited.fps)
    pairs = []
    for i, (c, r) in enumerate(zip(canon.clips, cf)):
        for j, (e, q) in enumerate(zip(edited.clips, ef)):
            ov = min(r["out_f"] / canon.fps, q["out_f"] / edited.fps) - max(r["in_f"] / canon.fps, q["in_f"] / edited.fps)
            if c.shot_index == e.shot_index and ov > 0:
                pairs.append((-ov, abs(i - j), i, j))
    out: Dict[int, int] = {}
    used: set = set()
    for _, _, i, j in sorted(pairs):
        if i not in out and j not in used:
            out[i] = j
            used.add(j)
    return out


def _ids(canon: Timeline, edited: Timeline, match: Dict[int, int]) -> List[str]:
    """Edited clip ids: the canonical id for a matched clip (so junctions line up), a fresh "h<n>" id for an added one."""
    ids: List[Optional[str]] = [None] * len(edited.clips)
    for i, j in match.items():
        ids[j] = canon.clips[i].id
    taken = {c.id for c in canon.clips}
    n = 0
    for j in range(len(ids)):
        if ids[j] is None:
            n += 1
            while f"h{n:03d}" in taken:
                n += 1
            ids[j] = f"h{n:03d}"
            taken.add(ids[j])
    return [str(x) for x in ids]


def diff_human_edits(canonical: Timeline, edited: Timeline) -> List[Decision]:
    """Human decisions: trim (visible cut point moved), transition, removed, added, reorder. Neither timeline is modified.

    Trims compare the NLE-visible edges (source_in + in_off, source_out - out_off), so turning a dissolve into a cut
    is a 'transition' decision and not also a trim. Clip ids in decisions are the canonical ids (new clips: h<n>).
    """
    match = _match(canonical, edited)
    ids = _ids(canonical, edited, match)
    cf, ef = _layout(canonical, canonical.fps), _layout(edited, edited.fps)
    inv = {j: i for i, j in match.items()}
    out: List[Decision] = []

    def D(action: str, cids: List[str], detail: str, **data: Any) -> None:
        out.append(Decision("human", action, cids, detail, data=data))

    for i, c in enumerate(canonical.clips):
        if i not in match:
            D("removed", [c.id], f"human removed {c.id} (shot {c.shot_index}, {c.source_in:.2f}-{c.source_out:.2f}s)", shot_index=c.shot_index, source_in=c.source_in, source_out=c.source_out)
    for j, e in enumerate(edited.clips):
        if j not in inv:
            D("added", [ids[j]], f"human added {ids[j]} (shot {e.shot_index}, {e.source_in:.2f}-{e.source_out:.2f}s)", shot_index=e.shot_index, source_in=e.source_in, source_out=e.source_out)
    order = [inv[j] for j in range(len(edited.clips)) if j in inv]
    if order != sorted(order):
        D("reorder", [canonical.clips[i].id for i in order], "human reordered clips: " + " ".join(canonical.clips[i].id for i in order))
    for j in range(len(edited.clips)):
        if j not in inv:
            continue
        i, cid = inv[j], ids[j]
        d_in, d_out = ef[j]["vis_in"] - cf[i]["vis_in"], ef[j]["vis_out"] - cf[i]["vis_out"]
        if d_in or d_out:
            parts = ([f"in {d_in:+d}f"] if d_in else []) + ([f"out {d_out:+d}f"] if d_out else [])
            D("trim", [cid], f"human trimmed {cid}: " + ", ".join(parts), in_frames=d_in, out_frames=d_out)
        if j and inv.get(j - 1) == i - 1 and i > 0:  # same neighbour as before: compare the junction's transition
            before, after = (cf[i]["kind"], cf[i]["tr"]), (ef[j]["kind"], ef[j]["tr"])
            if before != after:
                fmt = lambda t: "cut" if not t[1] else f"{t[0]} {t[1]}f"  # noqa: E731
                D("transition", [ids[j - 1], cid], f"human changed transition {fmt(before)} -> {fmt(after)}",
                  before={"kind": before[0], "frames": before[1]}, after={"kind": after[0], "frames": after[1]})
    return out


def human_stage(canonical: Timeline, edited: Timeline) -> Timeline:
    """The edited timeline as a new stage "human": canonical ids, canonical history + the human decisions. Canonical is untouched."""
    ids = _ids(canonical, edited, _match(canonical, edited))
    decisions = diff_human_edits(canonical, edited)
    out = edited.copy()
    for c, cid in zip(out.clips, ids):
        c.id = cid
    out.stage, out.name, out.source, out.fps = "human", "human edit", canonical.source, canonical.fps
    out.history = [Decision.from_dict(d.to_dict()) for d in canonical.history] + decisions
    return out


# -- navigation: mpv and Resolve -----------------------------------------------------------------
def junction_time(tl: Timeline, index: int, edl: bool = False) -> Tuple[float, int, str]:
    """(seconds, frame, timecode) of junction ``index`` (between clip index and index+1).

    Default: the NLE cut point on the exported timeline (for a dissolve its centre = matchcut record start + in_off).
    edl=True: the same junction in the mpv EDL, which has no transitions (clips simply concatenated).
    """
    rows = _layout(tl, tl.fps)
    frame = sum(r["out_f"] - r["in_f"] for r in rows[: index + 1]) if edl else rows[index + 1]["cut"]
    return frame / tl.fps, frame, seconds_to_timecode(frame / tl.fps, tl.fps)


def mpv_edl(tl: Timeline, media_path: str) -> str:
    """mpv EDL v0: one segment per clip with title= (junction label on the incoming clip). No transitions, hard cuts only."""
    p = str(Path(media_path).resolve())
    p = f"%{len(p.encode())}%{p}" if re.search(r"[,;\n!%]", p) else p  # EDL length-escape for awkward file names
    lines = ["# mpv EDL v0"]
    for i, (c, r) in enumerate(zip(tl.clips, _layout(tl, tl.fps))):
        title = f"J{i} s{tl.clips[i - 1].shot_index}->s{c.shot_index} {c.id}" if i else f"{c.id} s{c.shot_index}"
        lines.append(f"{p},{r['in_f'] / tl.fps:.6f},{(r['out_f'] - r['in_f']) / tl.fps:.6f},title={title}")
    return "\n".join(lines) + "\n"


def mpv_ipc_commands(tl: Timeline, index: int, pre: float = 1.0, post: float = 1.0, why: str = "") -> List[dict]:
    """mpv JSON IPC: loop [junction - pre, junction + post] on the EDL timeline, seek there, show the why line."""
    t = junction_time(tl, index, edl=True)[0]
    total = sum(r["out_f"] - r["in_f"] for r in _layout(tl, tl.fps)) / tl.fps
    a, b = max(0.0, t - pre), min(total, t + post)
    label = f"J{index + 1} s{tl.clips[index].shot_index}->s{tl.clips[index + 1].shot_index}" + (f": {why}" if why else "")
    return [{"command": ["set_property", "ab-loop-a", a]}, {"command": ["set_property", "ab-loop-b", b]},
            {"command": ["seek", a, "absolute+exact"]}, {"command": ["show-text", label, 4000]}]


_RESOLVE_TEMPLATE = '''#!/usr/bin/env python3
"""GENERATED by matchcut prototypes/interchange (feasibility proof). NEVER run against a live Resolve by its authors.

Uses only scripting-API calls listed as verified in the Resolve 21.0.4 README copy: scriptapp, GetProjectManager,
GetCurrentProject, GetMediaPool, ImportTimelineFromFile, SetCurrentTimeline, OpenPage, Timeline.AddMarker,
Timeline.SetCurrentTimecode, GetTimelineCount, GetTimelineByIndex, GetName. Needs DaVinci Resolve Studio running with
Preferences > System > General > External scripting using = Local. No call that adds transitions (21.1-only, option keys unverified).
Assumption (unverified): the imported timeline starts at 00:00:00:00, so marker frame ids and timecodes are record frames.
"""
import os
import sys

TIMELINE_FILE = @FILE@
TIMELINE_NAME = @NAME@
JUNCTIONS = @JUNCTIONS@   # junction id -> record frame of the cut point
NOTES = @NOTES@           # junction id -> (marker name, note)
NAVIGATE_TO = @NAV@
NAVIGATE_TC = @TC@


def load_api():
    try:
        import DaVinciResolveScript as dvr
    except ImportError:
        api = os.environ.get("RESOLVE_SCRIPT_API") or {
            "darwin": "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
            "win32": os.path.expandvars(r"%PROGRAMDATA%\\Blackmagic Design\\DaVinci Resolve\\Support\\Developer\\Scripting"),
        }.get(sys.platform, "/opt/resolve/Developer/Scripting")
        sys.path.append(os.path.join(api, "Modules"))
        import DaVinciResolveScript as dvr
    return dvr


def main():
    resolve = load_api().scriptapp("Resolve")
    if resolve is None:
        sys.exit("cannot reach Resolve (is it running, Studio, external scripting = Local?)")
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        sys.exit("no project is open in Resolve")
    pool = project.GetMediaPool()
    timeline = pool.ImportTimelineFromFile(TIMELINE_FILE, {"timelineName": TIMELINE_NAME, "importSourceClips": True})
    if not timeline:  # fall back to a lookup by name
        for i in range(1, project.GetTimelineCount() + 1):
            if project.GetTimelineByIndex(i).GetName() == TIMELINE_NAME:
                timeline = project.GetTimelineByIndex(i)
    if not timeline:
        sys.exit("timeline import failed: " + TIMELINE_FILE)
    project.SetCurrentTimeline(timeline)
    resolve.OpenPage("edit")
    for jid, frame in JUNCTIONS.items():
        name, note = NOTES.get(jid, (jid, ""))
        timeline.AddMarker(frame, "Green", name, note, 1, jid)  # customData = junction id; False if a marker is already there (OTIO may have brought one)
    timeline.SetCurrentTimecode(NAVIGATE_TC)
    print("playhead at %s (junction %s)" % (NAVIGATE_TC, NAVIGATE_TO))


if __name__ == "__main__":
    main()
'''


def resolve_script(timeline_file: str, timeline_name: str, junction_frames: Dict[str, int], navigate_to: str,
                   fps: float = 24.0, notes: Optional[Dict[str, Tuple[str, str]]] = None) -> str:
    """A Resolve (Studio) Python script: import the timeline file, add junction markers (customData = id), park the playhead."""
    subs = {"@FILE@": str(timeline_file), "@NAME@": timeline_name, "@JUNCTIONS@": dict(junction_frames), "@NOTES@": dict(notes or {}),
            "@NAV@": navigate_to, "@TC@": seconds_to_timecode(junction_frames[navigate_to] / fps, fps)}
    text = _RESOLVE_TEMPLATE
    for k, v in subs.items():
        text = text.replace(k, repr(v))
    return text
