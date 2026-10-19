"""CMX3600 EDL, written and read by hand: the lowest common denominator of editorial interchange.

Export: one video track (reel ``AX``, ``AA/V`` when the source has audio, else ``V``; ``V`` also when ``music.only`` drops the source
audio), ``C`` events and, for a dissolve, the CMX3600 pair of lines with one event number: a zero-length ``C`` line for the outgoing
side and a ``D nnn`` line for the incoming clip.  Source timecode is the media's own (``SourceInfo.start_tc_frames`` + frame); the
record side starts at 00:00:00:00.  Timecode is NON-DROP on the nominal base (24 for 23.976, 30 for 29.97): every frame count is
exact, only the wall-clock reading of a fractional rate drifts, and the file says so.  Media whose own timecode is drop-frame gets
a DROP FRAME file (``;`` separators) so that the source timecodes match the media.

Dissolves need no extra conversion here.  matchcut's overlap (the last ``d`` frames of the outgoing clip are the first ``d`` of the
incoming one, ``d`` in whole frames with error diffusion, from ``common.layout``) *is* the CMX3600 geometry: the transition starts at
the incoming clip's record start and runs ``d`` frames, the outgoing event ends there and its media runs on for ``d`` frames, and the
incoming event begins at its own source in.  The media a dissolve shows is the same as in the OTIO and FCPXML centred form.

Comments carry the provenance: ``* FROM CLIP NAME``, ``* MATCHCUT CLIP <key>``, ``ID``, ``SHOT``, ``BEAT``, ``JUNCTION <id>`` (on the
incoming clip), ``TRANSITION dip_to_black`` and, once at the end, ``STAGE``, ``SOURCE``, ``RATE``, ``FADE_IN``, ``FADE_OUT`` (and ``SOURCE_START_TC``, ``AUDIO none``
when they apply).
Music, the audio mix and every other kind of metadata are not carried.

Import reads any CMX3600 (matchcut's or an editor's): video events of the source footage become clips (an event is another file only on
positive evidence, see ``_is_source``), anything else is listed in the notes and ignored.  Lines of one event number form one event only
as a transition pair on one track; the source's start timecode is subtracted.  A malformed file raises a one-line ``InterchangeError``.
"""
from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from matchcut.pipeline.config import MusicConfig
from matchcut.pipeline.timeline import Timeline

from . import InterchangeError
from .common import (SourceInfo, assemble, clip_key, is_drop_rate, is_valid_timecode, junction_ids, layout, nominal_base, timecode_frames)

MAX_EVENTS = 999
_TC = re.compile(r"^(\d{1,3}):(\d{2}):(\d{2})([:;.,])(\d{2,3})$")
_DOC_KEYS = {"STAGE", "SOURCE", "RATE", "FADE_IN", "FADE_OUT", "TIMECODE", "SOURCE_START_TC", "AUDIO"}
_ONE_LINE = re.compile(r"\s*[\r\n]+\s*")


# -- timecode --------------------------------------------------------------------------------------
def _tc(frames: int, rate: Fraction, drop: bool = False) -> str:
    n = nominal_base(rate)
    frames = max(frames, 0)
    if drop:  # put back the frame numbers drop-frame timecode skips
        d = 2 if n == 30 else 4
        per_min, per_ten = n * 60 - d, n * 600 - 9 * d
        tens, rem = divmod(frames, per_ten)
        frames += 9 * d * tens + (d * ((rem - d) // per_min) if rem >= d else 0)
    sec, f = divmod(frames, n)
    sep = ";" if drop else ":"
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}{sep}{f:0{max(2, len(str(n - 1)))}d}"


def _frames(text: str, rate: Fraction, drop: bool, line: int) -> int:
    m = _TC.match(text)
    if not m:
        raise InterchangeError(f"invalid EDL: line {line}: bad timecode {text!r}")
    h, mi, s, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(5))
    n = nominal_base(rate)
    if f >= n:
        raise InterchangeError(f"invalid EDL: line {line}: frame {f} is out of range at {n} fps")
    if not is_valid_timecode(mi, s, f, rate, drop):
        raise InterchangeError(f"invalid EDL: line {line}: {text} is not a valid {'drop-frame ' if drop else ''}timecode")
    return timecode_frames(h, mi, s, f, rate, drop)


# -- export ----------------------------------------------------------------------------------------
def _one(text: Any) -> str:
    return _ONE_LINE.sub(" ", str(text)).strip()


def _event(num: int, track: str, kind: str, dur: int, tcs: Sequence[str]) -> str:
    head = f"{kind}    {dur:03d} " if dur else f"{kind}        "
    return f"{num:03d}  AX       {track:<5s} {head}{' '.join(tcs)}"


def export_edl(tl: Timeline, stage: str, src: SourceInfo, title: str, music: Optional[MusicConfig] = None) -> str:
    """CMX3600 text for ``tl`` (normally the frame-snapped copy from ``common.export_timeline``) on the source's exact rate.

    ``music`` is only consulted for ``music.only``: the render then drops the source audio, so the events are ``V`` (the music itself
    is not carried by an EDL).
    """
    rate = src.rate
    if not tl.clips:
        raise InterchangeError(f"stage {stage} has no clips to export")
    if len(tl.clips) > MAX_EVENTS:
        raise InterchangeError(f"an EDL holds at most {MAX_EVENTS} events (this cut has {len(tl.clips)} clips)")
    rows = layout(tl, rate)
    ids = junction_ids(tl)
    muted = bool(music is not None and music.path and music.only)  # the render drops the source audio entirely
    track = "AA/V" if src.has_audio and not muted else "V"
    name = Path(src.path).name
    drop = bool(src.start_tc_drop and is_drop_rate(rate))
    tc = lambda n: _tc(n, rate, drop)  # noqa: E731
    t0 = src.start_tc_frames  # source timecode is the media's own; the record side counts from zero
    out: List[str] = [f"TITLE: {_one(title)}", "FCM: DROP FRAME" if drop else "FCM: NON-DROP FRAME", ""]
    for i, (c, row) in enumerate(zip(tl.clips, rows)):
        nxt = rows[i + 1].tr if i + 1 < len(rows) else 0  # the next transition starts where this event ends and eats `nxt` frames of its media
        src_in, src_out = row.in_f, row.out_f - nxt
        rec_in, rec_out = row.rec, row.rec + (row.out_f - row.in_f) - nxt
        if src_out <= src_in:
            raise InterchangeError(f"clip {c.id} is shorter than its transitions")
        num = i + 1
        if row.tr:
            hold = t0 + rows[i - 1].out_f - row.tr  # where the outgoing clip's media is when the dissolve starts
            out.append(_event(num, track, "C", 0, [tc(hold), tc(hold), tc(rec_in), tc(rec_in)]))
            out.append(_event(num, track, "D", row.tr, [tc(t0 + src_in), tc(t0 + src_out), tc(rec_in), tc(rec_out)]))
            out.append(f"* FROM CLIP NAME: {name}")
            out.append(f"* TO CLIP NAME: {name}")
        else:
            out.append(_event(num, track, "C", 0, [tc(t0 + src_in), tc(t0 + src_out), tc(rec_in), tc(rec_out)]))
            out.append(f"* FROM CLIP NAME: {name}")
        out.append(f"* MATCHCUT CLIP {_one(clip_key(c))}")
        out.append(f"* MATCHCUT ID {_one(c.id)}")
        out.append(f"* MATCHCUT SHOT {c.shot_index}")
        if c.beat_id:
            out.append(f"* MATCHCUT BEAT {_one(c.beat_id)}")
        if i:
            out.append(f"* MATCHCUT JUNCTION {ids[i - 1]}")
        if row.tr and row.kind == "dip_to_black":
            out.append("* MATCHCUT TRANSITION dip_to_black")
        out.append("")
    out += [f"* MATCHCUT STAGE {_one(stage)}", f"* MATCHCUT SOURCE {_one(src.path)}", f"* MATCHCUT RATE {rate.numerator}/{rate.denominator}",
            f"* MATCHCUT FADE_IN {tl.fade_in}", f"* MATCHCUT FADE_OUT {tl.fade_out}"]
    if t0:
        out.append(f"* MATCHCUT SOURCE_START_TC {tc(t0)}: source timecodes are the media's own (its start timecode plus the frame)")
    if muted:
        out.append("* MATCHCUT AUDIO none: music.only drops the source audio from the render")
    if rate.denominator != 1 and not drop:
        out.append(f"* MATCHCUT TIMECODE NON-DROP at {rate.numerator}/{rate.denominator} fps: frames are exact, the timecode runs slow against the wall clock")
    return "\n".join(out) + "\n"


# -- import ----------------------------------------------------------------------------------------
def _same_name(a: Optional[str], b: str) -> bool:
    if not a:
        return True
    x, y = Path(a.strip()).name.lower(), Path(b).name.lower()
    return x == y or Path(x).stem == Path(y).stem or x == Path(y).stem


def _same_path(a: str, b: str) -> bool:
    """A ``FROM CLIP`` / ``SOURCE FILE`` value (path or file:// URL) names the source footage?"""
    a = a.strip()
    pa = unquote(urlparse(a).path) if a.startswith("file:") else a
    return pa == b or Path(pa).name.lower() == Path(b).name.lower()


_SHOT_NAME = re.compile(r"shot_?\d+\s*$", re.I)


_FILE_LIKE = re.compile(r"\.[A-Za-z0-9]{1,5}\s*$")


def _is_source(g: Dict[str, Any], reel: str, src_path: str) -> bool:
    """Whether the event of ``g`` is the source footage.  A different file needs positive evidence.  In order:
    a path comment (``FROM CLIP`` / ``SOURCE FILE``) decides; else the clip name: matchcut's own (``... shot<N>``) is the source and a
    file name (it has an extension) must be the source's; else, with no name or one that is no file name, a reel that is (the start of)
    the source's name (reels name the roll, not the clip, so this is never allowed to outvote a file name); else the name, if any."""
    pick = lambda d: d.get("TO") or d.get("FROM") or d.get("SOURCE")  # noqa: E731
    path = pick(g["paths"])
    if path:
        return _same_path(path, src_path)
    name = g["names"].get("TO") or g["names"].get("FROM")
    if name and _SHOT_NAME.search(name):
        return True
    if name and _FILE_LIKE.search(name):
        return _same_name(name, src_path)
    if len(reel) >= 3 and Path(src_path).stem.lower().startswith(reel.lower()):  # the reel may be truncated to 8 characters
        return True
    return _same_name(name, src_path)


def import_edl(text: str, rate: Fraction, src_path: str, bounds: Optional[Dict[int, Sequence[float]]] = None,
               src: Optional[SourceInfo] = None) -> Tuple[Timeline, List[str]]:
    """EDL text -> (Timeline, notes) for the footage ``src_path`` at the exact ``rate``.  Everything not mapped is in ``notes``.

    ``src`` (the probed source footage) gives the media's start timecode, which is subtracted from the source timecodes, and its
    length, against which a clip outside the media is reported.  Without it the media is taken to start at 00:00:00:00.
    """
    try:
        return _import(text, Fraction(rate), src_path, bounds, src)
    except InterchangeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, ZeroDivisionError, OverflowError) as exc:
        raise InterchangeError(f"invalid EDL: {type(exc).__name__}: {exc}".splitlines()[0]) from exc


def _put(cur: List[Dict[str, Any]], table: str, key: str, value: str) -> None:
    """A comment belongs to the latest event of its number; earlier events of that number (the video line before an audio line)
    take it only where they have no value of their own yet."""
    for k, g in enumerate(cur):
        if k == len(cur) - 1:
            g[table][key] = value
        else:
            g[table].setdefault(key, value)


def _parse(text: str, rate: Fraction, notes: List[str]) -> Tuple[str, Dict[str, str], List[Dict[str, Any]]]:
    """(title, document-level comments, event groups).  A group is one event: a single line, or the ``C`` line and the ``D``/``W``/``K``
    line of a transition on the same track and under the same event number, plus the comments that follow.  Lines of other tracks under
    one number (an audio line next to the video line) are separate events."""
    title = ""
    drop_fcm = False
    doc: Dict[str, str] = {}
    groups: List[Dict[str, Any]] = []
    cur: List[Dict[str, Any]] = []  # the groups of the latest event number: its comments belong to all of them
    warned_drop = False
    for n, raw in enumerate(str(text).splitlines(), 1):
        ln = raw.strip()
        if not ln:
            continue
        up = ln.upper()
        toks = ln.split()
        if up.startswith("TITLE:"):
            title = ln[6:].strip()
        elif up.startswith("FCM:"):
            drop_fcm = "DROP" in up and "NON" not in up
        elif ln.startswith("*"):
            body = ln[1:].strip()
            mc = re.match(r"^MATCHCUT\s+([A-Z_]+)\s*(.*)$", body)
            nm = re.match(r"^(FROM|TO) CLIP NAME:\s*(.*)$", body, re.I)
            pt = re.match(r"^(?:(FROM|TO) CLIP|(SOURCE) FILE):\s*(.*)$", body, re.I)
            if mc and mc.group(1) in _DOC_KEYS:
                doc[mc.group(1)] = mc.group(2).strip()
            elif mc:
                _put(cur, "mc", mc.group(1), mc.group(2).strip())
            elif nm:
                _put(cur, "names", nm.group(1).upper(), nm.group(2).strip())
            elif pt:
                _put(cur, "paths", (pt.group(1) or pt.group(2)).upper(), pt.group(3).strip())
        elif toks[0].isdigit() and len(toks) in (8, 9) and all(":" in t or ";" in t for t in toks[-4:]):
            drop = drop_fcm or any(";" in t for t in toks[-4:])
            if drop and not is_drop_rate(rate):
                if not warned_drop:
                    notes.append(f"drop-frame timecode in the EDL but the rate is {float(rate):g} fps: read as non-drop")
                    warned_drop = True
                drop = False
            si, so, ri, ro = (_frames(t, rate, drop, n) for t in toks[-4:])
            if so < si or ro < ri:
                raise InterchangeError(f"invalid EDL: event {toks[0]}: the source out is before its in" if so < si else f"invalid EDL: event {toks[0]}: the record out is before its in")
            ev = {"line": n, "num": toks[0], "reel": toks[1], "track": toks[2].upper(), "kind": toks[3].upper(),
                  "dur": int(toks[4]) if len(toks) == 9 and toks[4].isdigit() else 0, "si": si, "so": so, "ri": ri, "ro": ro}
            if not (cur and cur[0]["lines"][0]["num"] == ev["num"]):
                cur = []
            mate = next((g for g in cur if g["lines"][-1]["track"] == ev["track"] and g["lines"][-1]["kind"] == "C" and len(g["lines"]) == 1
                         and ev["kind"] != "C"), None)
            if mate is not None:
                mate["lines"].append(ev)
            else:
                g = {"lines": [ev], "mc": {}, "names": {}, "paths": {}, "m2": None}
                groups.append(g)
                cur.append(g)
        elif toks[0].upper() == "M2" and groups:
            groups[-1]["m2"] = " ".join(toks)
        elif toks[0].upper() in ("AUD", "SPLIT:", "EFFECTS", "LOC", "NOTE:", "TITLE", "FCM"):
            continue
        else:
            notes.append(f"line {n} ignored: {ln[:40]}")
    return title, doc, groups


def _import(text: str, rate: Fraction, src_path: str, bounds: Optional[Dict[int, Sequence[float]]], src: Optional[SourceInfo]) -> Tuple[Timeline, List[str]]:
    notes: List[str] = []
    title, doc, groups = _parse(text, rate, notes)
    if not groups:
        raise InterchangeError("invalid EDL: no events")
    t0 = src.start_tc_frames if src is not None else 0  # the media's own start timecode: source frame 0
    kept: List[Dict[str, Any]] = []
    prev_out: Optional[int] = None  # record out of the previous video event, whatever became of it
    prev_kept = False
    skipped_media = 0
    for g in groups:
        main = g["lines"][-1]
        first = g["lines"][0] if len(g["lines"]) > 1 else None
        num = main["num"]
        if not ("V" in main["track"] or main["track"] == "B"):
            notes.append(f"event {num}: audio event (track {main['track']}) ignored")
            continue
        gap = main["ri"] - prev_out if prev_out is not None else 0
        prev_out = main["ro"]
        if gap > 0:
            notes.append(f"gap of {gap} frames before event {num} ignored: the following clips close up")
        elif gap < 0:
            notes.append(f"event {num} starts {-gap} frames before the previous video event ends (overlapping record times): read in file order")
        was_kept, prev_kept = prev_kept, False
        if main["reel"].upper() == "BL":
            notes.append(f"event {num}: black (BL) ignored")
            continue
        name = g["names"].get("TO") or g["names"].get("FROM") or g["paths"].get("TO") or g["paths"].get("FROM") or g["paths"].get("SOURCE")
        if not _is_source(g, main["reel"], src_path):
            skipped_media += 1
            notes.append(f"event {num}: other media {name} ignored")
            continue
        kind, tr = main["kind"], 0
        if kind == "D" and main["dur"] > 0:
            tr = main["dur"]
            if first is not None and first["reel"].upper() == "BL":
                notes.append(f"event {num}: dissolve from black ignored")
                tr = 0
        elif kind != "C":
            notes.append(f"event {num}: transition type {kind} ({'wipe' if kind.startswith('W') else 'unsupported'}) read as a cut")
        if g["m2"]:
            notes.append(f"event {num}: speed change ({g['m2']}) ignored, the source range is used as written")
        elif main["so"] - main["si"] != main["ro"] - main["ri"]:
            notes.append(f"event {num}: source length {main['so'] - main['si']} frames differs from record length {main['ro'] - main['ri']} "
                         f"(a speed change?), the source range is used")
        if tr and not was_kept:
            notes.append(f"event {num}: dissolve from an ignored event dropped")
            tr = 0
        mc = g["mc"]
        tkind = "dip_to_black" if mc.get("TRANSITION") == "dip_to_black" else "dissolve"
        kept.append({"num": num, "si": main["si"] - t0, "so": main["so"] - t0, "tr": tr, "kind": tkind, "mc": mc, "name": name or ""})
        prev_kept = True
    if not kept:
        raise InterchangeError(f"no event of the EDL references the source footage {src_path}" if skipped_media else "invalid EDL: no video events")
    items: List[Tuple[str, Dict[str, Any]]] = []
    for k, e in enumerate(kept):
        head = e["tr"] // 2
        if e["tr"]:
            items.append(("tr", {"in": head, "out": e["tr"] - head, "kind": e["kind"]}))
        nxt_tr = kept[k + 1]["tr"] if k + 1 < len(kept) else 0
        out_f = e["so"] + nxt_tr
        vis_in = e["si"] + head
        dur = out_f - (nxt_tr - nxt_tr // 2) - vis_in
        if dur < 1:
            raise InterchangeError(f"invalid EDL: event {e['num']} is shorter than its transitions")
        mc = e["mc"]
        meta: Dict[str, Any] = {"clip_id": mc.get("ID", ""), "clip_key": mc.get("CLIP", ""), "beat_id": mc.get("BEAT", "")}
        if mc.get("SHOT", "").lstrip("-").isdigit():
            meta["shot_index"] = int(mc["SHOT"])
        else:
            m = re.search(r"shot_?(\d+)", e["name"])
            if m:
                meta["shot_index"] = int(m.group(1))
        if not meta["clip_id"]:  # OTIO's cmx_3600 writer keeps matchcut's clip names ("c003 shot2")
            mid = re.match(r"^\s*(\S+)\s+shot_?\d+\s*$", e["name"], re.I)
            meta["clip_id"] = mid.group(1) if mid else ""
        items.append(("clip", {"vis_in": vis_in, "dur": dur, "name": e["name"] or f"event {e['num']}", "meta": meta}))
    if "RATE" in doc:
        try:
            num, _, den = doc["RATE"].partition("/")
            declared = Fraction(int(num), int(den or 1))
            if declared != rate:
                notes.append(f"the EDL was written at {doc['RATE']} fps, read at {rate.numerator}/{rate.denominator}")
        except (ValueError, ZeroDivisionError):
            notes.append(f"RATE comment {doc['RATE']!r} is not a rate, ignored")
    if doc.get("SOURCE") and doc["SOURCE"] != src_path:
        notes.append(f"the EDL names the source {doc['SOURCE']}; the workspace's {src_path} is used")
    meta_doc: Dict[str, Any] = {"stage": doc.get("STAGE", "")}
    for key, field in (("FADE_IN", "fade_in"), ("FADE_OUT", "fade_out")):
        try:
            meta_doc[field] = float(doc.get(key, 0.0))
        except ValueError:
            notes.append(f"{key} comment {doc[key]!r} is not a number, ignored")
    return assemble(items, rate, meta_doc, title or "cut", src_path, bounds, notes, src.duration_frames if src is not None else None), notes
