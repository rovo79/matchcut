"""CMX3600 EDL, written and read by hand: the lowest common denominator of editorial interchange.

Export: one video track (reel ``AX``, ``AA/V`` when the source has audio, else ``V``), ``C`` events and, for a dissolve, the
CMX3600 pair of lines with one event number: a zero-length ``C`` line for the outgoing side and a ``D nnn`` line for the incoming
clip.  Timecode is NON-DROP on the nominal base (24 for 23.976, 30 for 29.97): every frame count is exact, only the wall-clock
reading of a fractional rate drifts, and the file says so.

Dissolves need no extra conversion here.  matchcut's overlap (the last ``d`` frames of the outgoing clip are the first ``d`` of the
incoming one, ``d`` in whole frames with error diffusion, from ``common.layout``) *is* the CMX3600 geometry: the transition starts at
the incoming clip's record start and runs ``d`` frames, the outgoing event ends there and its media runs on for ``d`` frames, and the
incoming event begins at its own source in.  The media a dissolve shows is the same as in the OTIO and FCPXML centred form.

Comments carry the provenance: ``* FROM CLIP NAME``, ``* MATCHCUT CLIP <key>``, ``ID``, ``SHOT``, ``BEAT``, ``JUNCTION <id>`` (on the
incoming clip), ``TRANSITION dip_to_black`` and, once at the end, ``STAGE``, ``SOURCE``, ``RATE``, ``FADE_IN``, ``FADE_OUT``.
Music, the audio mix and every other kind of metadata are not carried.

Import reads any CMX3600 (matchcut's or an editor's): video events of the source footage become clips, anything else is listed in
the notes and ignored.  A malformed file raises a one-line ``InterchangeError``.
"""
from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from matchcut.pipeline.timeline import Timeline

from . import InterchangeError
from .common import SourceInfo, assemble, clip_key, junction_ids, layout

MAX_EVENTS = 999
_TC = re.compile(r"^(\d{1,3}):(\d{2}):(\d{2})([:;.,])(\d{2,3})$")
_DOC_KEYS = {"STAGE", "SOURCE", "RATE", "FADE_IN", "FADE_OUT", "TIMECODE"}
_ONE_LINE = re.compile(r"\s*[\r\n]+\s*")


# -- timecode --------------------------------------------------------------------------------------
def _nominal(rate: Fraction) -> int:
    return max(1, int(round(float(rate))))


def _tc(frames: int, rate: Fraction) -> str:
    n = _nominal(rate)
    sec, f = divmod(max(frames, 0), n)
    return f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}:{f:0{max(2, len(str(n - 1)))}d}"


def _is_drop_rate(rate: Fraction) -> bool:
    return rate in (Fraction(30000, 1001), Fraction(60000, 1001))


def _frames(text: str, rate: Fraction, drop: bool, line: int) -> int:
    m = _TC.match(text)
    if not m:
        raise InterchangeError(f"invalid EDL: line {line}: bad timecode {text!r}")
    h, mi, s, f = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(5))
    n = _nominal(rate)
    if f >= n or mi >= 60 or s >= 60:
        raise InterchangeError(f"invalid EDL: line {line}: frame {f} is out of range at {n} fps" if f >= n else f"invalid EDL: line {line}: bad timecode {text!r}")
    total = ((h * 60 + mi) * 60 + s) * n + f
    if drop:  # 29.97 / 59.94 drop-frame: 2 (4) frame numbers skipped at each minute except every tenth
        minutes = 60 * h + mi
        total -= (2 if n == 30 else 4) * (minutes - minutes // 10)
    return total


# -- export ----------------------------------------------------------------------------------------
def _one(text: Any) -> str:
    return _ONE_LINE.sub(" ", str(text)).strip()


def _event(num: int, track: str, kind: str, dur: int, tcs: Sequence[str]) -> str:
    head = f"{kind}    {dur:03d} " if dur else f"{kind}        "
    return f"{num:03d}  AX       {track:<5s} {head}{' '.join(tcs)}"


def export_edl(tl: Timeline, stage: str, src: SourceInfo, title: str) -> str:
    """CMX3600 text for ``tl`` (normally the frame-snapped copy from ``common.export_timeline``) on the source's exact rate."""
    rate = src.rate
    if not tl.clips:
        raise InterchangeError(f"stage {stage} has no clips to export")
    if len(tl.clips) > MAX_EVENTS:
        raise InterchangeError(f"an EDL holds at most {MAX_EVENTS} events (this cut has {len(tl.clips)} clips)")
    rows = layout(tl, rate)
    ids = junction_ids(tl)
    track = "AA/V" if src.has_audio else "V"
    name = Path(src.path).name
    tc = lambda n: _tc(n, rate)  # noqa: E731
    out: List[str] = [f"TITLE: {_one(title)}", "FCM: NON-DROP FRAME", ""]
    for i, (c, row) in enumerate(zip(tl.clips, rows)):
        nxt = rows[i + 1].tr if i + 1 < len(rows) else 0  # the next transition starts where this event ends and eats `nxt` frames of its media
        src_in, src_out = row.in_f, row.out_f - nxt
        rec_in, rec_out = row.rec, row.rec + (row.out_f - row.in_f) - nxt
        if src_out <= src_in:
            raise InterchangeError(f"clip {c.id} is shorter than its transitions")
        num = i + 1
        if row.tr:
            hold = rows[i - 1].out_f - row.tr  # where the outgoing clip's media is when the dissolve starts
            out.append(_event(num, track, "C", 0, [tc(hold), tc(hold), tc(rec_in), tc(rec_in)]))
            out.append(_event(num, track, "D", row.tr, [tc(src_in), tc(src_out), tc(rec_in), tc(rec_out)]))
            out.append(f"* FROM CLIP NAME: {name}")
            out.append(f"* TO CLIP NAME: {name}")
        else:
            out.append(_event(num, track, "C", 0, [tc(src_in), tc(src_out), tc(rec_in), tc(rec_out)]))
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
    if rate.denominator != 1:
        out.append(f"* MATCHCUT TIMECODE NON-DROP at {rate.numerator}/{rate.denominator} fps: frames are exact, the timecode runs slow against the wall clock")
    return "\n".join(out) + "\n"


# -- import ----------------------------------------------------------------------------------------
def _same_name(a: Optional[str], b: str) -> bool:
    if not a:
        return True
    x, y = Path(a.strip()).name.lower(), Path(b).name.lower()
    return x == y or Path(x).stem == Path(y).stem or x == Path(y).stem


def import_edl(text: str, rate: Fraction, src_path: str, bounds: Optional[Dict[int, Sequence[float]]] = None) -> Tuple[Timeline, List[str]]:
    """EDL text -> (Timeline, notes) for the footage ``src_path`` at the exact ``rate``.  Everything not mapped is in ``notes``."""
    try:
        return _import(text, Fraction(rate), src_path, bounds)
    except InterchangeError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, ZeroDivisionError, OverflowError) as exc:
        raise InterchangeError(f"invalid EDL: {type(exc).__name__}: {exc}".splitlines()[0]) from exc


def _parse(text: str, rate: Fraction, notes: List[str]) -> Tuple[str, Dict[str, str], List[Dict[str, Any]]]:
    """(title, document-level comments, event groups).  A group is the lines of one event number plus its comments."""
    title = ""
    drop_fcm = False
    doc: Dict[str, str] = {}
    groups: List[Dict[str, Any]] = []
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
            fm = re.match(r"^(FROM|TO) CLIP NAME:\s*(.*)$", body, re.I)
            if mc and mc.group(1) in _DOC_KEYS:
                doc[mc.group(1)] = mc.group(2).strip()
            elif groups and mc:
                groups[-1]["mc"][mc.group(1)] = mc.group(2).strip()
            elif groups and fm:
                groups[-1]["names"][fm.group(1).upper()] = fm.group(2).strip()
        elif toks[0].isdigit() and len(toks) in (8, 9) and all(":" in t or ";" in t for t in toks[-4:]):
            drop = drop_fcm or any(";" in t for t in toks[-4:])
            if drop and not _is_drop_rate(rate):
                if not warned_drop:
                    notes.append(f"drop-frame timecode in the EDL but the rate is {float(rate):g} fps: read as non-drop")
                    warned_drop = True
                drop = False
            si, so, ri, ro = (_frames(t, rate, drop, n) for t in toks[-4:])
            if so < si or ro < ri:
                raise InterchangeError(f"invalid EDL: event {toks[0]}: the source out is before its in" if so < si else f"invalid EDL: event {toks[0]}: the record out is before its in")
            ev = {"line": n, "num": toks[0], "reel": toks[1], "track": toks[2].upper(), "kind": toks[3].upper(),
                  "dur": int(toks[4]) if len(toks) == 9 and toks[4].isdigit() else 0, "si": si, "so": so, "ri": ri, "ro": ro}
            if groups and groups[-1]["lines"][0]["num"] == ev["num"]:
                groups[-1]["lines"].append(ev)
            else:
                groups.append({"lines": [ev], "mc": {}, "names": {}, "m2": None})
        elif toks[0].upper() == "M2" and groups:
            groups[-1]["m2"] = " ".join(toks)
        elif toks[0].upper() in ("AUD", "SPLIT:", "EFFECTS", "LOC", "NOTE:", "TITLE", "FCM"):
            continue
        else:
            notes.append(f"line {n} ignored: {ln[:40]}")
    return title, doc, groups


def _import(text: str, rate: Fraction, src_path: str, bounds: Optional[Dict[int, Sequence[float]]]) -> Tuple[Timeline, List[str]]:
    notes: List[str] = []
    title, doc, groups = _parse(text, rate, notes)
    if not groups:
        raise InterchangeError("invalid EDL: no events")
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
        was_kept, prev_kept = prev_kept, False
        if main["reel"].upper() == "BL":
            notes.append(f"event {num}: black (BL) ignored")
            continue
        name = g["names"].get("TO") or g["names"].get("FROM")
        if not _same_name(name, src_path):
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
        kept.append({"num": num, "si": main["si"], "so": main["so"], "tr": tr, "kind": tkind, "mc": mc, "name": name or ""})
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
        items.append(("clip", {"vis_in": vis_in, "dur": dur, "name": e["name"] or f"event {e['num']}", "meta": meta}))
    if "RATE" in doc:
        try:
            num, _, den = doc["RATE"].partition("/")
            declared = Fraction(int(num), int(den or 1))
            if declared != rate:
                notes.append(f"the EDL was written at {doc['RATE']} fps, read at {rate.numerator}/{rate.denominator}")
        except (ValueError, ZeroDivisionError):
            notes.append(f"RATE comment {doc['RATE']!r} is not a rate, ignored")
    meta_doc: Dict[str, Any] = {"stage": doc.get("STAGE", "")}
    for key, field in (("FADE_IN", "fade_in"), ("FADE_OUT", "fade_out")):
        try:
            meta_doc[field] = float(doc.get(key, 0.0))
        except ValueError:
            notes.append(f"{key} comment {doc[key]!r} is not a number, ignored")
    return assemble(items, rate, meta_doc, title or "cut", doc.get("SOURCE") or src_path, bounds), notes
