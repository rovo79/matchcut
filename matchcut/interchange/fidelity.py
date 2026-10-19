"""Validation of an export against the canonical stage, and the report of what each format keeps, approximates and loses.

Every export is re-imported with matchcut's own reader and compared with the frame-snapped copy that was written: clip count and
order, shot, source in/out frames (exact), transition kind and frames, the clip keys, and the record duration (within half a frame
plus a millisecond, ``common.record_tolerance_frames``).  A difference is an ``errors`` entry.  What cannot survive by design is the
``KNOWN`` table below, which every result and every report includes; what the export measured (clips moved by the frame snap,
dissolve lengths rounded to whole frames) is added to ``approximated``.  No format here is a complete copy of a matchcut cut.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from matchcut.pipeline.timeline import Timeline

from . import InterchangeError
from .common import clip_key, layout, record_frames, record_tolerance_frames, to_frames

FORMATS = ("otio", "fcpxml", "edl")
FORMAT_NAMES = {"otio": "OpenTimelineIO", "fcpxml": "FCPXML 1.10", "edl": "CMX3600 EDL"}

_ALL_LOST = [
    "ducking: the render ducks the music under speech, no format carries that automation",
    "loudness normalisation: applied by the render only",
]
_ALL_APPROX = [
    "dissolve lengths are whole frames and a cut's record time is a whole number of frames (measured below)",
    "source times are written in the media's embedded start timecode (frame 0 = that timecode, 0 when the file has none); UNVERIFIED that "
    "Resolve and Final Cut Pro conform timecoded camera media from it (human gate)",
]

# What each format cannot carry or carries only approximately, whatever the stage.  Entries marked UNVERIFIED have not been
# checked in an editor.
KNOWN: Dict[str, Dict[str, List[str]]] = {
    "otio": {
        "approximated": _ALL_APPROX + [
            "dip to black: a Custom_Transition with matchcut metadata (OTIO has no such transition); UNVERIFIED how an editor treats it",
        ],
        "lost": _ALL_LOST + [
            "fade in/out of the whole cut: timeline metadata only, not rendered by the receiving editor",
            "music mix: the music file, its start and gain are carried (track A2 and metadata), the render's mix is not",
            "UNVERIFIED: whether a third-party editor keeps the matchcut metadata (clips, markers, timeline) when it saves the file again",
        ],
    },
    "fcpxml": {
        "approximated": _ALL_APPROX + [
            "dip to black: the Fade To Color transition, its uid taken from secondary sources; UNVERIFIED in Final Cut Pro",
        ],
        "lost": _ALL_LOST + [
            "fade in/out of the whole cut: metadata only, not rendered by the receiving editor",
            "music mix: the music file (lane -1), its start and gain are carried, the render's mix is not",
            "UNVERIFIED: whether the matchcut metadata (md keys, keywords, marker values) survives a round trip through a third-party application",
            "UNVERIFIED: that srcEnable=\"video\" mutes the source audio under music.only in every application",
        ],
    },
    "edl": {
        "approximated": _ALL_APPROX + [
            "dip to black: written as a dissolve plus a MATCHCUT TRANSITION comment; an editor shows a plain dissolve",
            "timecode is NON-DROP: at 23.976 and 29.97 fps the frame counts are exact but the timecode runs slow against the wall clock",
        ],
        "lost": _ALL_LOST + [
            "music: an EDL has no second track, so the music file is not carried",
            "metadata other than comments: only the MATCHCUT comments (clip key, junction id, shot, stage, fades) are written; editors may drop them",
            "fade in/out of the whole cut: a comment only, not rendered by the receiving editor",
            "tracks: one video track only; the source audio rides on the video events (AA/V; V when music.only drops it)",
        ],
    },
}


@dataclass
class FidelityResult:
    fmt: str
    stage: str
    preserved: List[str] = field(default_factory=list)
    approximated: List[str] = field(default_factory=list)
    lost: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    canonical_sha256: str = ""  # sha256 of the stage file the export was made from (lets a later, narrower export keep this entry only while it holds)
    stale: bool = False  # the stage file changed since this export, whose files are still in the directory: kept, but no longer a statement about the stage

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, Any]:
        d = {"fmt": self.fmt, "stage": self.stage, "ok": self.ok, "preserved": list(self.preserved),
             "approximated": list(self.approximated), "lost": list(self.lost), "errors": list(self.errors)}
        if self.canonical_sha256:
            d["canonical_sha256"] = self.canonical_sha256
        if self.stale:
            d["stale"] = True
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "FidelityResult":
        return cls(str(d["fmt"]), str(d["stage"]), list(d.get("preserved", [])), list(d.get("approximated", [])), list(d.get("lost", [])),
                   list(d.get("errors", [])), str(d.get("canonical_sha256", "")), bool(d.get("stale", False)))


# -- comparison ------------------------------------------------------------------------------------
def _compare(a: Timeline, b: Timeline, rate: Fraction, tol: float = 0, rec_tol: Optional[float] = None,
             keys: bool = False) -> List[Tuple[str, str]]:
    """(category, message) for every difference between ``a`` and ``b`` on the frame grid of ``rate``.

    Source frames and transition frames may differ by ``tol`` frames (0 = exact); record positions and the record duration by
    ``rec_tol`` (default ``tol``).  ``keys`` also compares the clip keys.
    """
    rec_tol = tol if rec_tol is None else rec_tol
    if len(a.clips) != len(b.clips):
        return [("clips", f"clip count {len(b.clips)} != {len(a.clips)} (reimported vs exported)")]
    if not a.clips:
        return []
    la, lb = layout(a, rate), layout(b, rate)
    out: List[Tuple[str, str]] = []
    for i, (ca, cb, x, y) in enumerate(zip(a.clips, b.clips, la, lb)):
        who = f"clip {i + 1} ({ca.id}, shot {ca.shot_index})"
        if ca.shot_index != cb.shot_index:
            out.append(("shots", f"{who}: shot {cb.shot_index} != {ca.shot_index}"))
        if abs(x.in_f - y.in_f) > tol:
            out.append(("source", f"{who}: source in {y.in_f} != {x.in_f} frames"))
        if abs(x.out_f - y.out_f) > tol:
            out.append(("source", f"{who}: source out {y.out_f} != {x.out_f} frames"))
        if x.transition[0] != y.transition[0]:
            out.append(("transitions", f"{who}: transition {y.transition[0]} != {x.transition[0]}"))
        elif abs(x.tr - y.tr) > tol:
            out.append(("transitions", f"{who}: transition length {y.tr} != {x.tr} frames"))
        if abs(x.rec - y.rec) > rec_tol:
            out.append(("record", f"{who}: record start {y.rec} != {x.rec} frames"))
        if abs(x.cut - y.cut) > rec_tol:
            out.append(("record", f"{who}: cut at record frame {y.cut} != {x.cut}"))
        if keys and clip_key(ca) != cb.origin:
            out.append(("keys", f"{who}: clip key {cb.origin or '(missing)'} != {clip_key(ca)}"))
    end = abs(la[-1].rec_out - lb[-1].rec_out)
    if end > rec_tol:
        out.append(("record", f"record duration {lb[-1].rec_out} != {la[-1].rec_out} frames"))
    secs = abs(a.duration - b.duration) * float(rate)
    if secs > max(rec_tol, record_tolerance_frames(rate)) + 1e-6 and not end > rec_tol:
        out.append(("record", f"record duration differs by {secs:.2f} frames"))
    return out


def differences(a: Timeline, b: Timeline, rate: Fraction, tol: float = 0, rec_tol: Optional[float] = None, keys: bool = False) -> List[str]:
    """One message per difference between two timelines (see ``_compare``); empty when they are equivalent."""
    return [m for _, m in _compare(a, b, rate, tol, rec_tol, keys)]


# -- measured approximations -----------------------------------------------------------------------
# What the frame snap (``Timeline.snap_to_frames``) can do to one edge: round to the nearest frame (at most half a frame, plus 4-decimal
# storage), then pull an edge that left its shot back inside (at most one frame in total for a clip that was inside its shot).  A clip
# shorter than two frames gets its out edge pushed to two frames.  A larger move is not snapping: it is an error.
MAX_SNAP_FRAMES = 1.0
_SNAP_SLACK = 0.01  # frames; plus 1 ms of time (see _snap_errors)


def _snap_errors(canonical: Timeline, exported: Timeline, rate: Fraction) -> List[str]:
    """Differences between the stage and the copy that was written that snapping cannot explain."""
    if len(canonical.clips) != len(exported.clips):
        return [f"export drops or adds clips: {len(exported.clips)} written for {len(canonical.clips)} in the stage"]
    fps = float(rate)
    # Timeline.validate lets a clip sit up to 1 ms outside its shot and passes store ranges to 3 decimals, so a valid stage can be
    # pulled back by the snap a little more than a frame.  The slack is therefore 0.01 frame + 1 ms (chosen over re-snapping with the
    # shot bounds, which the gate does not have).
    slack = _SNAP_SLACK + 0.001 * fps
    errs: List[str] = []
    for i, (a, b) in enumerate(zip(canonical.clips, exported.clips)):
        who = f"clip {i + 1} ({a.id}, shot {a.shot_index})"
        if a.shot_index != b.shot_index:
            errs.append(f"{who}: exported as shot {b.shot_index}")
        if clip_key(a) != clip_key(b):
            errs.append(f"{who}: exported clip key {clip_key(b)} != {clip_key(a)}")
        if i and (a.transition_in.is_cut != b.transition_in.is_cut or (not a.transition_in.is_cut and a.transition_in.kind != b.transition_in.kind)):
            errs.append(f"{who}: exported transition {b.transition_in.kind} != {a.transition_in.kind}")
        elif i and not a.transition_in.is_cut and abs(a.transition_in.duration - b.transition_in.duration) > 1e-3:
            errs.append(f"{who}: exported transition length {b.transition_in.duration:g} s != {a.transition_in.duration:g} s")
        short = (a.source_out - a.source_in) * fps < 2
        for label, x, y, limit in (("in", a.source_in, b.source_in, MAX_SNAP_FRAMES),
                                   ("out", a.source_out, b.source_out, 2.0 if short else MAX_SNAP_FRAMES)):
            d = (y - x) * fps
            if abs(d) > limit + slack:
                errs.append(f"{who}: source {label} moved {d:+.2f} frames by the snap; snapping moves an edge by at most {limit:g} frame{'s' if limit > 1 else ''}")
    return errs


def _snap_note(canonical: Timeline, exported: Timeline, rate: Fraction) -> Optional[str]:
    fps = float(rate)
    moved, worst, where = 0, 0.0, ""
    for a, b in zip(canonical.clips, exported.clips):
        d = max(abs(b.source_in - a.source_in), abs(b.source_out - a.source_out)) * fps
        if d > 0.01:
            moved += 1
            if d > worst:
                worst, where = d, a.id
    if not moved:
        return None
    return (f"snapped to the frame grid: {moved} of {len(canonical.clips)} clips moved, largest move {worst:.2f} frame{'s' if worst > 1.005 else ''}"
            f" ({where}); the exported ranges are the snapped ones")


def _dissolve_note(canonical: Timeline, rate: Fraction) -> Optional[str]:
    fps = float(rate)
    cum, done, worst, n = 0.0, 0, 0.0, 0
    for c in canonical.clips[1:]:
        if c.transition_in.is_cut:
            continue
        n += 1
        cum += c.transition_in.duration
        tr = max(to_frames(cum, rate) - done, 0)
        done += tr
        worst = max(worst, abs(tr - c.transition_in.duration * fps))
    if not n or worst < 0.005:
        return None
    return f"dissolve lengths rounded to whole frames: {n} transition{'s' if n != 1 else ''}, largest error {worst:.2f} frame{'s' if worst > 1.005 else ''}"


def _dip_note(canonical: Timeline) -> str:
    trs = [c for c in canonical.clips[1:] if not c.transition_in.is_cut]
    dips = sum(1 for c in trs if c.transition_in.kind == "dip_to_black")
    return f"dip to black: {dips} of {len(trs)} transition{'s' if len(trs) != 1 else ''} in this cut" + (" (approximated as described above)" if dips else "")


def _fmt_rate(rate: Fraction) -> str:
    return f"{rate.numerator}/{rate.denominator} ({float(rate):.3f})" if rate.denominator != 1 else str(rate.numerator)


def check(canonical: Timeline, reimported: Timeline, fmt: str, rate: Fraction, exported: Optional[Timeline] = None) -> FidelityResult:
    """Validate one export.

    ``canonical`` is the stage as matchcut holds it; ``exported`` is the frame-snapped copy that was written (``canonical`` itself
    when it is already on the frame grid); ``reimported`` is what matchcut's own reader made of the file.  ``reimported`` must equal
    ``exported`` frame for frame; how far ``exported`` is from ``canonical`` is reported as approximated, not as an error.
    """
    if fmt not in KNOWN:
        raise InterchangeError(f"unknown interchange format {fmt!r}")
    exp = exported if exported is not None else canonical
    res = FidelityResult(fmt, canonical.stage or exp.stage)
    snap_errors = _snap_errors(canonical, exp, rate) if exp is not canonical else []
    found = _compare(exp, reimported, rate, tol=0, rec_tol=record_tolerance_frames(rate), keys=True)
    res.errors = snap_errors + [m for _, m in found]
    bad = {cat for cat, _ in found}
    if snap_errors:  # what was written is not the stage: nothing below is a statement about the stage
        bad |= {"clips", "shots", "source", "transitions", "keys"}
    if "clips" not in bad:
        n = len(exp.clips)
        trs = sum(1 for c in exp.clips[1:] if not c.transition_in.is_cut)
        if "shots" not in bad:
            res.preserved.append(f"clip count and order ({n} clips) and the shot of every clip")
        if "source" not in bad:
            res.preserved.append(f"source in/out frames of every clip, exact at {_fmt_rate(rate)} fps")
        if "transitions" not in bad:
            res.preserved.append(f"transition kind and length in frames ({trs} transition{'s' if trs != 1 else ''}, {n - 1 - trs} cuts)")
        if "record" not in bad:
            res.preserved.append(f"record duration {record_frames(layout(exp, rate))} frames (within {record_tolerance_frames(rate):.2f} frame)")
        if "keys" not in bad:
            res.preserved.append("clip keys (the lineage ids of the clips)")
    res.approximated = list(KNOWN[fmt]["approximated"])
    for note in (_snap_note(canonical, exp, rate) if not snap_errors else None, _dissolve_note(canonical, rate), _dip_note(canonical)):
        if note:
            res.approximated.append(note)
    res.lost = list(KNOWN[fmt]["lost"])
    if abs(exp.fade_in - reimported.fade_in) > 1e-3 or abs(exp.fade_out - reimported.fade_out) > 1e-3:
        res.errors.append(f"fade metadata {reimported.fade_in:g}/{reimported.fade_out:g} != {exp.fade_in:g}/{exp.fade_out:g} (in/out seconds)")
    return res


# -- the report ------------------------------------------------------------------------------------
def _cell(items: Sequence[str]) -> str:
    return "<br>".join("- " + i.replace("|", "\\|") for i in items) if items else "-"


def write_report(results: Sequence[FidelityResult], out_dir: Any) -> Tuple[Path, Path]:
    """Write ``fidelity.json`` and ``fidelity.md`` into ``out_dir``: a table per stage, a row per format."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stages: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in results:
        stages.setdefault(r.stage, {})[r.fmt] = r.to_dict()
    jp, mp = out / "fidelity.json", out / "fidelity.md"
    # a stale entry describes an earlier export: it neither fails nor passes the current one
    jp.write_text(json.dumps({"stages": stages, "ok": all(r.ok for r in results if not r.stale)}, indent=2, ensure_ascii=False) + "\n")
    md = ["# Interchange fidelity report", "",
          "Each export was read back with matchcut's own reader and compared with the stage it was written from. "
          "**Preserved** items were compared and match. **Approximated** items carry a documented difference. "
          "**Lost** items cannot be carried by that format. **Errors** are differences nothing explains; any error fails the export. "
          "None of these formats is a complete copy of a matchcut cut.", ""]
    if any(r.stale for r in results):
        md += ["Rows marked **STALE** describe an earlier export whose stage file has changed since: its files are still in this directory "
               "but are no longer checked against the stage. Export that stage again to refresh it.", ""]
    for stage, by_fmt in stages.items():
        md += [f"## Stage: {stage or '(unnamed)'}", "", "| format | preserved | approximated | lost | errors |", "|---|---|---|---|---|"]
        for fmt in FORMATS:
            if fmt in by_fmt:
                d = by_fmt[fmt]
                label = FORMAT_NAMES.get(fmt, fmt) + (" **STALE**" if d.get("stale") else "")
                md.append(f"| {label} | {_cell(d['preserved'])} | {_cell(d['approximated'])} | {_cell(d['lost'])} | {_cell(d['errors'])} |")
        md.append("")
    mp.write_text("\n".join(md))
    return jp, mp
