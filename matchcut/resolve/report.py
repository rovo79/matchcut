"""``matchcut.resolve report``: what a run's ``resolve_probe.json`` says about each route to a Resolve timeline, offline.

The probe script records and never interprets; this module does the interpreting.  For each route (import-otio, import-fcpxml, import-edl,
build, build+transitions) it compares the read-back with ``expected.json`` (record start, duration, source in/out of every clip; transition
items, which 21.1 is reported to list among a track's items, are counted apart).  It names the end-frame conventions it can tell from a
uniform one-frame offset (``GetSourceEndFrame`` inclusive, ``AppendToTimeline``'s ``endFrame`` inclusive) instead of calling them lost frames.
Dissolves are judged from the read-back when the install lists transition items, else from the probe's own exports re-imported by M2a's
``import_timeline`` (in a scratch copy of the workspace, never in the workspace), every format separately, and the report says which.
It also counts the junction markers found by id and the playhead moves, gives each route a verdict, recommends a default route for M2c-1
(preferring one where markers are found by id and navigation lands, and saying so when none does) or says none is usable, and writes
``probe_report.md`` next to the JSON.  Every sentence is about what the JSON records.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from matchcut.interchange import InterchangeError
from matchcut.interchange.workspace import import_timeline
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.junctions import stage_timeline_paths

from . import ProbeError
from .probe import EXPECTED, REPORT, RESULT, RUNS

SCHEMA = "matchcut.resolve_probe/1"
ROUTES = ("import-otio", "import-fcpxml", "import-edl", "build", "build+transitions")
PREFERRED = ("import-otio", "import-fcpxml", "build+transitions", "build", "import-edl")  # the M2c plan: import if dissolves survive, else build
FALLBACK = ("build", "build+transitions", "import-otio", "import-fcpxml", "import-edl")  # no route keeps the dissolves: the build route
BUILD_ROUTES = ("build", "build+transitions")
NOT_CALLED = "not documented on this install: not called"
FAKE_PRODUCT = "matchcut fake of the documented Resolve API"
NOT_LIVE = "not run against a live Resolve"
FORMAT_NAMES = {"otio": "OTIO", "fcpxml": "FCPXML", "edl": "EDL"}
ACTIONS = {"trim": ("trim", "trims"), "removed": ("removed clip", "removed clips"), "added": ("added clip", "added clips"),
           "reorder": ("reorder", "reorders"), "transition": ("transition change", "transition changes")}
SRC_END_INCLUSIVE = "GetSourceEndFrame is inclusive on this install"
END_FRAME_INCLUSIVE = "endFrame is inclusive on this install"
Back = Dict[str, Tuple[Optional[List[Dict[str, Any]]], List[str], Optional[str]]]


@dataclass
class RouteResult:
    route: str
    # usable | usable, dissolves lost | usable, dissolves uncertain | usable, dissolves not measured | not measured | not usable | failed | not tried
    verdict: str = "not tried"
    frames: str = "not measured"  # exact | differ | not measured
    dissolves: str = "not measured"  # kept | lost | changed | uncertain | none in the cut | not measured
    dissolves_from: str = ""  # the read-back, or which exports
    markers: str = "-"
    markers_by_id: str = "untested"  # yes | no | untested
    navigation: str = "-"
    navigation_ok: Optional[bool] = None
    transition_items: int = 0
    preserved: List[str] = field(default_factory=list)
    approximated: List[str] = field(default_factory=list)
    lost: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


@dataclass
class Report:
    text: str
    path: Path
    routes: Dict[str, RouteResult]
    recommended: Optional[str]
    notes: List[str] = field(default_factory=list)


def _load(path: Path) -> Dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ProbeError(f"cannot read {path}: no such file") from None
    except (OSError, ValueError) as exc:
        raise ProbeError(f"cannot read {path}: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}") from exc
    if not isinstance(doc, dict):
        raise ProbeError(f"cannot read {path}: not a JSON object")
    return doc


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _offsets(deltas: List[int]) -> str:
    counts = Counter(d for d in deltas if d)
    return ", ".join(f"{d:+d} frame{'s' if abs(d) != 1 else ''} ({n})" for d, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])))


def resolve_input(path: Path) -> Path:
    """A run's resolve_probe.json from what the person gives: the file, a run directory, or the bundle (its latest run by name)."""
    path = Path(path)
    if path.is_dir():
        if (path / RESULT).is_file():
            return path / RESULT
        runs = sorted(p for p in (path / RUNS).glob(f"*/{RESULT}")) if (path / RUNS).is_dir() else []
        real = [p for p in runs if "dry-run" not in p.parent.name]  # a dry run only when there is nothing else
        if real or runs:
            return (real or runs)[-1]
        raise ProbeError(f"no run in {path}: run resolve_probe.py first (each run writes {RUNS}/<UTC time>/{RESULT})")
    return path


def _bundle_of(json_path: Path, doc: Dict[str, Any]) -> Optional[Path]:
    for folder in (json_path.parent, json_path.parent.parent.parent):  # next to it, or <bundle>/runs/<run>/
        if (folder / EXPECTED).is_file():
            return folder
    recorded = (doc.get("bundle") or {}).get("dir")
    return Path(recorded) if recorded and (Path(recorded) / EXPECTED).is_file() else None


# -- the workspace copy the probe's exports are re-imported into -------------------------------------------------------------------
def _scratch(ws: Path, bundle: Path, into: Path, cfg: PipelineConfig) -> Tuple[Path, PipelineConfig]:
    """A copy of what ``import_timeline`` reads (stage files, shots, probe, config) plus the bundle's stage snapshots."""
    scratch = into / "ws"
    scratch.mkdir()
    for p in Path(ws).iterdir():
        if p.is_file() and p.suffix == ".json" and p.name not in ("human.json", "human_import.json"):
            shutil.copy2(p, scratch / p.name)
    if (Path(ws) / "passes").is_dir():
        shutil.copytree(Path(ws) / "passes", scratch / "passes")
    if (bundle / ".stages").is_dir():
        shutil.copytree(bundle / ".stages", scratch / "exports" / ".stages")
    try:
        scfg = PipelineConfig.load(scratch / "config.json")
    except Exception:
        scfg = cfg
    scfg.workspace = str(scratch)
    return scratch, scfg


def _reimport(scratch: Path, cfg: PipelineConfig, path: Path, stage: str) -> Tuple[Optional[List[Dict[str, Any]]], List[str], Optional[str]]:
    try:
        res = import_timeline(scratch, cfg, path, None, stage)
        return [d.to_dict() for d in res.decisions], list(res.notes), None
    except InterchangeError as exc:
        return None, [], str(exc)
    finally:
        for name in ("human.json", "human_import.json"):
            (scratch / name).unlink(missing_ok=True)


# -- one route -----------------------------------------------------------------------------------------------------------------
def _track(rb: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(clips, transition items) of video track 1: an item whose recorded type is "transition" is not a clip."""
    items = [v or {} for v in ((rb.get("video") or [[]])[0] if rb.get("video") else [])]
    return [v for v in items if v.get("type") != "transition"], [v for v in items if v.get("type") == "transition"]


def _uniform(deltas: Optional[List[int]], value: int) -> bool:
    return bool(deltas) and all(d == value for d in deltas)


def _frames(r: RouteResult, rb: Dict[str, Any], exp: Dict[str, Any]) -> Optional[bool]:
    clips = exp["clips"]
    video, transitions = _track(rb)
    r.transition_items = len(transitions)
    if transitions:
        r.notes.append(f"{len(transitions)} transition items read back (type \"transition\"), counted apart from the clips")
    if len(video) != len(clips):
        r.lost.append(f"clip count {len(video)}, expected {len(clips)}")
        return False
    start = rb.get("start_frame")
    tc0 = int(exp["source"].get("start_tc_frames") or 0)
    fields = (("record start", lambda v: v.get("start") - start if _num(v.get("start")) and _num(start) else None, "rec_in", True),
              ("duration", lambda v: v.get("duration") if _num(v.get("duration")) else None, "dur", True),
              ("source in", lambda v: v.get("source_start_frame") if _num(v.get("source_start_frame")) else None, "vis_in", True),
              ("source out", lambda v: v.get("source_end_frame") if _num(v.get("source_end_frame")) else None, "vis_out", False))
    deltas: Dict[str, Optional[List[int]]] = {}
    ok: Optional[bool] = True
    for label, read, key, essential in fields:
        got = [read(v) for v in video]
        if any(g is None for g in got):
            r.notes.append(f"{label}: not read for {sum(g is None for g in got)} of {len(got)} clips")
            deltas[label] = None
            if essential and ok:
                ok = None
            continue
        deltas[label] = [int(round(g - c[key])) for g, c in zip(got, clips)]
    exact = lambda label: deltas[label] is not None and not any(deltas[label])  # noqa: E731
    explained: set = set()
    n = len(clips)
    # AppendToTimeline's endFrame taken as inclusive: every built clip one frame longer, with the source in and the record start exact
    if r.route in BUILD_ROUTES and exact("record start") and exact("source in") and _uniform(deltas["duration"], 1) and \
            (deltas["source out"] is None or _uniform(deltas["source out"], 1) or _uniform(deltas["source out"], 0)):
        r.approximated.append(f"duration is one frame longer than expected on every clip ({n}) with the source in exact: {END_FRAME_INCLUSIVE} "
                              "(AppendToTimeline; M2c-1 passes endFrame = source out - 1)")
        explained |= {"duration", "source out"}
    # GetSourceEndFrame taken as inclusive: only the source out reads one frame less, everything else exact
    elif exact("record start") and exact("duration") and exact("source in") and _uniform(deltas["source out"], -1):
        r.approximated.append(f"source out reads one frame less than expected on every clip ({n}): {SRC_END_INCLUSIVE}")
        explained.add("source out")
    for label, _, key, _ in fields:
        d = deltas[label]
        if d is None or label in explained:
            continue
        if not any(d):
            r.preserved.append(f"{label} of every clip ({n}) as expected")
        elif tc0 and key in ("vis_in", "vis_out") and all(x == tc0 for x in d):
            r.approximated.append(f"{label} is counted from the media's start timecode on every clip (+{tc0} frames)")
        else:
            r.lost.append(f"{label} differs on {sum(1 for x in d if x)} of {len(d)} clips: {_offsets(d)}")
            ok = False
    return ok


def _export_dissolves(back: Back, exp: Dict[str, Any], total: int) -> Dict[str, Tuple[str, int, List[str]]]:
    """Per readable export: (kept | lost | changed, how many dissolves differ, details)."""
    out: Dict[str, Tuple[str, int, List[str]]] = {}
    for fmt in ("otio", "fcpxml", "edl"):
        if fmt not in back or back[fmt][0] is None:
            continue
        changes = [d for d in back[fmt][0] if d["action"] == "transition" and (d.get("data") or {}).get("before", {}).get("frames")]
        cut = [d for d in changes if not (d.get("data") or {}).get("after", {}).get("frames")]
        other = [d for d in changes if d not in cut]
        state = "kept" if not changes else "lost" if cut else "changed"
        out[fmt] = (state, len(cut) or len(other), [d["detail"] for d in other])
    return out


def _dissolves(r: RouteResult, rb: Dict[str, Any], exp: Dict[str, Any], back: Back, install_lists: bool) -> None:
    wanted = [(c["rec_in"] - c["transition_in"]["head"], c["transition_in"]["frames"]) for c in exp["clips"][1:] if c["transition_in"]["frames"]]
    total = len(wanted)
    suffix = " (the build route adds none)" if r.route == "build" else ""
    if not total:
        r.dissolves = "none in the cut"
        r.notes.append("the cut has no dissolves: what Resolve does with them was not measured")
        return
    _, transitions = _track(rb)
    start = rb.get("start_frame")
    if transitions or install_lists:  # the read-back shows transitions on this install: judge from it
        r.dissolves_from = "the read-back (transition items)"
        found = {(t.get("start") - start, t.get("duration")) for t in transitions if _num(t.get("start")) and _num(start)}
        hit = sum(1 for w in wanted if w in found)
        if hit == total and len(transitions) == total:
            r.dissolves = "kept"
            r.preserved.append(f"dissolves: {total} of {total} read back as transition items at the expected frames and length")
        elif not transitions:
            r.dissolves = "lost"
            r.lost.append(f"dissolves: {total} of {total} have no transition item in the read-back{suffix}")
        else:
            r.dissolves = "changed"
            r.lost.append(f"dissolves: {total - hit} of {total} do not match a transition item in the read-back "
                          f"({len(transitions)} items: {sorted(found)}; expected {wanted})")
        return
    per = _export_dissolves(back, exp, total)
    if not per:
        r.notes.append("dissolves: not measured (the read-back does not show transitions and no export could be read back)")
        return
    states = {s for s, _, _ in per.values()}
    names = [FORMAT_NAMES[f] for f in per]
    where = ("the " + ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1] + " export" + ("s" if len(names) > 1 else "")
             + "; the read-back does not show transitions")
    r.dissolves_from = where
    if len(states) > 1:
        r.dissolves = "uncertain"
        r.lost.append("dissolves: the exports disagree: " + ", ".join(f"{FORMAT_NAMES[f]} {s}" + (f" ({k} of {total})" if s != "kept" else "")
                                                               for f, (s, k, _) in per.items()))
        return
    state = states.pop()
    k = max(n for _, n, _ in per.values())
    r.dissolves = state
    if state == "kept":
        r.preserved.append(f"dissolves: {total} of {total} kept, kind and length ({where})")
    elif state == "lost":
        r.lost.append(f"dissolves: {k} of {total} are cuts ({where}){suffix}")
    else:
        r.lost.append(f"dissolves: {k} of {total} changed ({where}): " + "; ".join(d for _, _, ds in per.values() for d in ds)[:300])


def _markers(r: RouteResult, m: Optional[Dict[str, Any]], exp: Dict[str, Any]) -> None:
    if not m:
        return
    n = len(exp["junctions"])
    parts, states = [], []
    if r.route.startswith("import-"):
        carried = sum(1 for v in (m.get("found_before") or {}).values() if v)
        (r.preserved if carried == n else r.lost).append(f"junction markers from the file: {carried} of {n} found by id")
        parts.append(f"file {carried}/{n}")
        states.append(carried == n)
    if m.get("added"):
        own = sum(1 for v in (m.get("found_after") or {}).values() if v)
        yes = sum(1 for a in m["added"].values() if a.get("add_marker") is True or a.get("update_marker_custom_data") is True)
        if own == n:
            r.preserved.append(f"junction markers found by id after the probe added them ({own} of {n})")
        else:
            r.lost.append(f"junction markers added by the probe: {own} of {n} found by id (AddMarker returned True {yes} times)")
        parts.append(f"own {own}/{n}")
        states.append(own == n)
        frameless = sum(1 for v in (m.get("found_after") or {}).values() if v and v.get("frame") is None)
        if frameless:
            r.notes.append(f"GetMarkerByCustomData answered without a frame for {frameless} of {n} junctions: it returned the marker's information only")
    r.markers = ", ".join(parts) or "-"
    r.markers_by_id = "yes" if states and any(states) else "no" if states else "untested"


def _navigation(r: RouteResult, nav: Optional[Dict[str, Any]], rb: Dict[str, Any], exp: Dict[str, Any]) -> None:
    attempts = (nav or {}).get("attempts") or []
    by_id = {j["id"]: j for j in exp["junctions"]}
    start = rb.get("start_frame")
    cells = []
    for how in ("start timecode + record frame", "start timecode + marker frame"):
        atts = [a for a in attempts if a.get("how") == how]
        if not atts:
            continue
        good = 0
        for a in atts:
            j = by_id.get(a.get("junction"))
            item = a.get("current_item") or {}
            want = (start + exp["clips"][j["index"] + 1]["rec_in"]) if j and _num(start) else None
            if a.get("set") is True and a.get("current_timecode") == a.get("timecode") and want is not None and item.get("start") == want:
                good += 1
        (r.preserved if good == len(atts) else r.lost).append(f"navigation: playhead on the incoming clip for {good} of {len(atts)} junctions ({how})")
        cells.append(f"{good}/{len(atts)}")
        if how == "start timecode + record frame":
            r.navigation_ok = good == len(atts)
    skipped = [a for a in attempts if a.get("skipped")]
    if skipped:
        r.lost.append(f"navigation: {len(skipped)} junctions skipped: {skipped[0]['skipped']}")
        r.navigation_ok = False
    r.navigation = " + ".join(cells) or "-"


def _transitions(r: RouteResult, attempts: Optional[List[Dict[str, Any]]]) -> None:
    atts = [a for a in (attempts or []) if "junction" in a]
    if not atts:
        return
    if all(a.get("result") == NOT_CALLED for a in atts):
        r.lost.append(f"AddTransition: {NOT_CALLED} ({len(atts)} dissolves)")
        return
    called = [a for a in atts if a.get("called")]
    skipped = [a for a in atts if not a.get("called")]
    if skipped:
        r.lost.append(f"AddTransition: not called for {len(skipped)} dissolves: {skipped[0].get('result')}")
    if called:
        nones = sum(1 for a in called if a.get("result") in (None, False))
        line = f"AddTransition: called for {len(called)} dissolves, returned None {nones} times" if nones else \
            f"AddTransition: called for {len(called)} dissolves with {json.dumps(called[0].get('options'))}, returned a value each time"
        (r.lost if nones else r.notes).append(line)
        if called[0].get("options_source"):
            r.notes.append(f"AddTransition options: {called[0]['options_source']}")


def _exports(r: RouteResult, back: Back, entry: Dict[str, Any]) -> None:
    for fmt, e in (entry.get("exports") or {}).items():
        name = FORMAT_NAMES.get(fmt, fmt)
        if not e.get("ok") or not e.get("exists"):
            r.lost.append(f"{name} export: Timeline.Export failed" + (f" ({e['error']})" if e.get("error") else ""))
            continue
        decisions, notes, error = back.get(fmt, (None, [], "not read back"))
        if decisions is None:
            r.lost.append(f"{name} export cannot be read back by matchcut: {error}")
        elif not decisions:
            r.preserved.append(f"{name} export reads back with no differences from the stage")
        elif not _decision_counts(decisions, ("transition",)):
            r.notes.append(f"{name} export reads back with {_decision_counts(decisions)} and no clip or frame differences")
        else:
            r.lost.append(f"{name} export reads back with {_decision_counts(decisions, ('transition',))}: "
                          + "; ".join(d["detail"] for d in decisions if d["action"] != "transition")[:400])


def _decision_counts(decisions: List[Dict[str, Any]], skip: Tuple[str, ...] = ()) -> str:
    counts = Counter(d["action"] for d in decisions if d["action"] not in skip)
    return ", ".join(f"{n} {ACTIONS.get(a, (a, a + 's'))[0 if n == 1 else 1]}" for a, n in sorted(counts.items()))


def _failed_route(r: RouteResult, entry: Dict[str, Any]) -> None:
    r.verdict = "failed"
    bad = next((s for s in entry.get("steps") or [] if not s.get("ok")), None)
    if entry.get("error"):
        r.lost.append(f"no timeline: the probe stopped on {entry['error']}")
    elif bad:
        r.lost.append(f"no timeline: {bad['call']} " + (f"raised {bad['error']}" if bad.get("error") else f"returned {json.dumps(bad.get('return'))}"))
    else:
        r.lost.append("no timeline: " + (entry.get("note") or "not made"))


def _route(route: str, doc: Dict[str, Any], exp: Dict[str, Any], back: Back, install_lists: bool) -> RouteResult:
    r = RouteResult(route)
    entry = (doc.get("timelines") or {}).get(route)
    if entry is None:
        return r
    for s in entry.get("steps") or []:
        if s.get("call") == "MediaPool.AppendToTimeline" and isinstance(s.get("return"), list) and None in s["return"]:
            r.lost.append(f"AppendToTimeline returned [None] for {s['return'].count(None)} of {len(s['return'])} clips")
    if not entry.get("created"):
        _failed_route(r, entry)
        return r
    rb = entry.get("readback") or {}
    ok = _frames(r, rb, exp)
    r.frames = "exact" if ok else "differ" if ok is False else "not measured"
    if ok is None:
        r.notes += list(doc.get("version_notes") or [])
    _dissolves(r, rb, exp, back, install_lists)
    _transitions(r, entry.get("transitions"))
    _markers(r, entry.get("markers"), exp)
    _navigation(r, entry.get("navigation"), rb, exp)
    _exports(r, back, entry)
    if ok is None:
        r.verdict = "not measured"
    elif ok is False:
        r.verdict = "not usable"
    elif r.dissolves in ("kept", "none in the cut"):
        r.verdict = "usable"
    elif r.dissolves in ("lost", "changed"):
        r.verdict = "usable, dissolves lost"
    elif r.dissolves == "uncertain":
        r.verdict = "usable, dissolves uncertain"
    else:
        r.verdict = "usable, dissolves not measured"
    return r


# -- the whole report ---------------------------------------------------------------------------------------------------------
def _pick(routes: Dict[str, RouteResult], order: Tuple[str, ...], verdicts: Tuple[str, ...]) -> Optional[str]:
    """The route of ``order`` with one of ``verdicts`` where navigation lands and markers are found by id, else the best of the rest."""
    cands = [n for n in order if routes[n].verdict in verdicts]
    if not cands:
        return None
    rank = lambda n: (routes[n].navigation_ok is True) * 2 + (routes[n].markers_by_id == "yes")  # noqa: E731
    best = max(rank(n) for n in cands)
    return next(n for n in cands if rank(n) == best)


def _qualify(name: str, routes: Dict[str, RouteResult]) -> List[str]:
    r = routes[name]
    out = []
    if r.markers_by_id != "yes":
        others = [n for n in ROUTES if routes[n].markers_by_id == "yes"]
        if r.markers_by_id == "untested":
            out.append("junction markers were not tested on this route" + (f" (they are found by id on {', '.join(others)})" if others else ""))
        else:
            out.append("it needs markers re-added by id: none was found by id on it"
                       + (f" (they are on {', '.join(others)})" if others else ", nor on any other route"))
    if r.navigation_ok is False:
        out.append("navigation by timecode did not land on the incoming clip every time")
    elif r.navigation_ok is True and r.markers_by_id != "yes":
        out.append("navigation by timecode lands on the incoming clip")
    return out


def _recommend(routes: Dict[str, RouteResult]) -> Tuple[Optional[str], str]:
    tiers = ((PREFERRED, ("usable",), "frames exact and dissolves as expected, as measured"),
             (PREFERRED, ("usable, dissolves uncertain", "usable, dissolves not measured"), "frames exact; the dissolves are uncertain or not measured"),
             (FALLBACK, ("usable, dissolves lost",), "frames exact, but no route carried the dissolves on this install: they would need another way "
                                                     "(or stay cuts) in M2c-1"))
    for order, verdicts, why in tiers:
        name = _pick(routes, order, verdicts)
        if name:
            extra = _qualify(name, routes)
            return name, f"Recommended default route for M2c-1: **{name}** ({why})" + ("; " + "; ".join(extra) if extra else "") + "."
    unread = [n for n in ROUTES if routes[n].verdict == "not measured"]
    if unread:
        return None, ("No route could be judged: the frames of " + ", ".join(unread) + " could not be read back (see Failed steps); "
                      "none of the others is usable on this install.")
    return None, "None of the routes is usable on this install: no timeline came back with the cut's frames."


def _source_line(doc: Dict[str, Any]) -> str:
    product = (doc.get("resolve") or {}).get("product")
    if product == FAKE_PRODUCT:
        return f"Status: {NOT_LIVE}. This JSON comes from {FAKE_PRODUCT} (a test of matchcut's side), not from Resolve."
    if not (doc.get("scripting") or {}).get("connected"):
        return f"Status: {NOT_LIVE}: the probe did not connect to Resolve ({doc.get('status')})."
    version = (doc.get("resolve") or {}).get("version_string")
    return (f"Status: measured by resolve_probe.py on {product} {version}, as Resolve reported itself. matchcut's own tests ran the probe "
            "only against a fake of the documented API.")


def _table(routes: Dict[str, RouteResult]) -> List[str]:
    out = ["| route | verdict | frames | dissolves | markers by id | navigation |", "|---|---|---|---|---|---|"]
    for name in ROUTES:
        r = routes[name]
        out.append(f"| {name} | {r.verdict} | {r.frames} | {r.dissolves} | {r.markers} | {r.navigation} |")
    return out


def _render(doc: Dict[str, Any], exp: Dict[str, Any], json_path: Path, routes: Dict[str, RouteResult], recommendation: str,
            notes: List[str]) -> str:
    plat = doc.get("platform") or {}
    res = doc.get("resolve") or {}
    cat = doc.get("catalogue") or {}
    mcp = doc.get("mcp") or {}
    proj = doc.get("project") or {}
    safety = doc.get("safety") or {}
    product = (str(res.get("product")) + " " + str(res.get("version_string"))) if res.get("product") else "not reported"
    lines = ["# Resolve probe report", "", _source_line(doc), "",
             f"- probe JSON: `{json_path}` (status {doc.get('status')}, {doc.get('created_utc')}, probe version {doc.get('probe_version')})",
             f"- stage `{exp['stage']}`: {len(exp['clips'])} clips, {len(exp['junctions'])} junctions, "
             f"{sum(1 for c in exp['clips'][1:] if c['transition_in']['frames'])} dissolves, {exp['rate'][0]}/{exp['rate'][1]} fps",
             f"- Resolve: {product}; {plat.get('system')} {plat.get('release')}; Python {plat.get('python')}",
             f"- scripting: connected {(doc.get('scripting') or {}).get('connected')}; module {(doc.get('scripting') or {}).get('module_path')}"]
    for e in (doc.get("scripting") or {}).get("module_errors") or []:
        lines.append(f"- found {e.get('file') or e.get('where')} but it failed to import: {e.get('error')}")
    files = cat.get("files") or []
    if files:
        lines.append("- API catalogue: " + "; ".join(f"`{f.get('path')}` ({f.get('version_line') or 'no version line'})" for f in files))
    else:
        lines.append("- API catalogue: not found" + (" (so only README 21.0.4 calls were made)" if doc.get("timelines") else ""))
    gated = cat.get("gated") or {}
    if gated:
        lines.append("- calls newer than the 21.0.4 README: " + ", ".join(f"{k} {'documented, called' if v else 'not documented, not called'}" for k, v in gated.items()))
    for note in doc.get("version_notes") or []:
        lines.append(f"- version note: {note}")
    lines.append(f"- Resolve MCP binary: {mcp.get('observed', 'not checked')} (observed only; never started)")
    counts = safety.get("counts") or {}
    lines.append(f"- projects: the open project at the start was {proj.get('user_current_project')!r}"
                 + (f" ({counts.get('timelines')} timelines, {counts.get('clips')} clips, {counts.get('subfolders')} media pool folders)" if counts else "")
                 + f"; the probe's project: {proj.get('name')!r} in the folder {proj.get('folder')!r}"
                 + (f"; frame rate setting {proj.get('frame_rate_setting')!r} (asked {proj.get('frame_rate_requested')!r})" if proj.get("frame_rate_requested") else ""))
    lines += ["", "## Recommendation", "", recommendation]
    findable = [n for n in ROUTES if routes[n].markers_by_id == "yes"]
    if any(routes[n].markers_by_id != "untested" for n in ROUTES):
        lines.append("")
        lines.append(f"Junction markers are findable by id on: {', '.join(findable)}." if findable else "Junction markers are not findable by id on any route.")
    lines += ["", "## Routes", ""] + _table(routes)
    for name in ROUTES:
        r = routes[name]
        lines += ["", f"### {name}: {r.verdict}", ""]
        if r.dissolves_from:
            lines.append(f"- dissolves judged from {r.dissolves_from}")
        for title, items in (("preserved", r.preserved), ("approximated", r.approximated), ("lost", r.lost), ("notes", r.notes)):
            for item in items:
                lines.append(f"- {title}: {item}")
        if not (r.preserved or r.approximated or r.lost or r.notes):
            lines.append("- not tried")
    fails = doc.get("failures") or []
    lines += ["", f"## Failed steps ({len(fails)})", ""]
    for f in fails:
        what = f"raised {f['error']}" if f.get("error") else f"returned {json.dumps(f.get('return'))}"
        lines.append(f"- {f.get('where')}: `{f.get('call')}` {what}")
    if not fails:
        lines.append("- none")
    if notes or doc.get("notes"):
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes + list(doc.get("notes") or [])]
    return "\n".join(lines) + "\n"


def build_report(ws: Path, cfg: PipelineConfig, json_path: Path) -> Report:
    """Read a run's JSON (``json_path``: the file, its run directory, or the bundle for its latest run) and the bundle, compare, write
    ``probe_report.md`` next to the JSON; a one-line ``ProbeError`` on bad input."""
    ws = Path(ws)
    json_path = resolve_input(Path(json_path))
    doc = _load(json_path)
    if doc.get("schema") != SCHEMA:
        raise ProbeError(f"{json_path} is not a matchcut resolve_probe.json (schema {doc.get('schema')!r})")
    bundle = _bundle_of(json_path, doc)
    if bundle is None:
        raise ProbeError(f"no expected.json next to {json_path} or in its bundle: give a resolve_probe.json from inside its probe bundle")
    exp = _load(bundle / EXPECTED)
    notes: List[str] = []
    stage = exp["stage"]
    stage_file = dict(stage_timeline_paths(ws)).get(stage)
    if stage_file is None or not stage_file.is_file():
        notes.append(f"the workspace has no stage {stage}: compared with expected.json only")
    elif hashlib.sha256(stage_file.read_bytes()).hexdigest() != exp.get("stage_sha256"):
        notes.append(f"the stage file {stage_file.name} changed since the bundle was made: the frames are compared with expected.json, "
                     "the exports with the stage as it was exported (its snapshot)")
    ran_with = (doc.get("bundle") or {}).get("sha256") or {}
    changed = sorted(k for k, v in ran_with.items() if v != exp.get("sha256", {}).get(k))
    if changed:
        notes.append(f"the bundle's {', '.join(changed)} file(s) differ from what expected.json describes: the probe ran on other files")
    timelines = doc.get("timelines") or {}
    if not timelines:
        notes.append(f"the probe stopped before any route: {doc.get('status')}")
    back: Dict[str, Back] = {}
    if any((e.get("exports") or {}) for e in timelines.values()):
        with tempfile.TemporaryDirectory(prefix="matchcut-probe-report-") as tmp:
            scratch, scfg = _scratch(ws, bundle, Path(tmp), cfg)
            for route, entry in timelines.items():
                back[route] = {}
                for fmt, e in (entry.get("exports") or {}).items():
                    path = bundle / e.get("file", "")
                    if e.get("ok") and path.is_file():
                        back[route][fmt] = _reimport(scratch, scfg, path, stage)
    install_lists = any(_track(e.get("readback") or {})[1] for e in timelines.values())
    routes = {name: _route(name, doc, exp, back.get(name, {}), install_lists) for name in ROUTES}
    asked, got = (doc.get("project") or {}).get("frame_rate_requested"), (doc.get("project") or {}).get("frame_rate_setting")
    if asked and got is not None and str(got) != str(asked):
        notes.append(f"the probe's project frame rate setting is {got!r}, the cut's is {asked!r} (Project.GetSetting('timelineFrameRate'))")
    recommended, recommendation = _recommend(routes) if timelines else (None, "No route was tried, so there is nothing to recommend.")
    text = _render(doc, exp, json_path, routes, recommendation, notes)
    out = json_path.parent / REPORT
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ProbeError(f"cannot write {out}: {exc.strerror or exc}") from exc
    return Report(text, out, routes, recommended, notes)
