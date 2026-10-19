"""``matchcut.resolve report``: what ``resolve_probe.json`` says about each route to a Resolve timeline, offline.

The probe script records and never interprets; this module does the interpreting.  For each route (import-otio, import-fcpxml, import-edl,
build, build+transitions) it compares the read-back with ``expected.json`` (record start, duration, source in/out of every clip), the
dissolves with the probe's own exports re-imported by M2a's ``import_timeline`` (in a scratch copy of the workspace, never in the
workspace), the junction markers found by id, and the playhead moves.  It gives each route a verdict, recommends a default route for
M2c-1 (or says none is usable) and writes ``probe_report.md`` next to the JSON.  Every sentence is about what the JSON records.
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
from .probe import EXPECTED, REPORT

SCHEMA = "matchcut.resolve_probe/1"
ROUTES = ("import-otio", "import-fcpxml", "import-edl", "build", "build+transitions")
PREFERRED = ("import-otio", "import-fcpxml", "build+transitions", "build", "import-edl")  # the M2c plan: import if dissolves survive, else build
FALLBACK = ("build", "build+transitions", "import-otio", "import-fcpxml", "import-edl")  # no route keeps the dissolves: the build route
NOT_CALLED = "not documented on this install: not called"
FAKE_PRODUCT = "matchcut fake of the documented Resolve API"
NOT_LIVE = "not run against a live Resolve"
FORMAT_NAMES = {"otio": "OTIO", "fcpxml": "FCPXML", "edl": "EDL"}
ACTIONS = {"trim": ("trim", "trims"), "removed": ("removed clip", "removed clips"), "added": ("added clip", "added clips"),
           "reorder": ("reorder", "reorders"), "transition": ("transition change", "transition changes")}


@dataclass
class RouteResult:
    route: str
    verdict: str = "not tried"  # usable | usable, dissolves lost | usable, dissolves not measured | not measured | not usable | failed | not tried
    frames: str = "not measured"  # exact | differ | not measured
    dissolves: str = "not measured"  # kept | lost | changed | none in the cut | not measured
    markers: str = "-"
    navigation: str = "-"
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
def _frames(r: RouteResult, rb: Dict[str, Any], exp: Dict[str, Any]) -> Optional[bool]:
    clips = exp["clips"]
    video = (rb.get("video") or [[]])[0] if rb.get("video") else []
    if len(video) != len(clips):
        r.lost.append(f"clip count {len(video)}, expected {len(clips)}")
        return False
    start = rb.get("start_frame")
    tc0 = int(exp["source"].get("start_tc_frames") or 0)
    ok: Optional[bool] = True
    fields = (("record start", lambda v: v.get("start") - start if _num(v.get("start")) and _num(start) else None, "rec_in", True),
              ("duration", lambda v: v.get("duration") if _num(v.get("duration")) else None, "dur", True),
              ("source in", lambda v: v.get("source_start_frame") if _num(v.get("source_start_frame")) else None, "vis_in", True),
              ("source out", lambda v: v.get("source_end_frame") if _num(v.get("source_end_frame")) else None, "vis_out", False))
    for label, read, key, essential in fields:
        got = [read(v or {}) for v in video]
        if any(g is None for g in got):
            r.notes.append(f"{label}: not read for {sum(g is None for g in got)} of {len(got)} clips")
            if essential:
                ok = None if ok else ok
            continue
        deltas = [int(round(g - c[key])) for g, c in zip(got, clips)]
        if not any(deltas):
            r.preserved.append(f"{label} of every clip ({len(clips)}) as expected")
        elif tc0 and key in ("vis_in", "vis_out") and all(d == tc0 for d in deltas):
            r.approximated.append(f"{label} is counted from the media's start timecode on every clip (+{tc0} frames)")
        else:
            r.lost.append(f"{label} differs on {sum(1 for d in deltas if d)} of {len(deltas)} clips: {_offsets(deltas)}")
            ok = False
    return ok


def _decision_counts(decisions: List[Dict[str, Any]], skip: Tuple[str, ...] = ()) -> str:
    counts = Counter(d["action"] for d in decisions if d["action"] not in skip)
    return ", ".join(f"{n} {ACTIONS.get(a, (a, a + 's'))[0 if n == 1 else 1]}" for a, n in sorted(counts.items()))


def _dissolves(r: RouteResult, exp: Dict[str, Any], back: Dict[str, Tuple[Optional[List[Dict[str, Any]]], List[str], Optional[str]]]) -> None:
    total = sum(1 for c in exp["clips"][1:] if c["transition_in"]["frames"])
    if not total:
        r.dissolves = "none in the cut"
        r.notes.append("the cut has no dissolves: what Resolve does with them was not measured")
        return
    fmt = next((f for f in ("otio", "fcpxml", "edl") if f in back and back[f][0] is not None), None)
    if fmt is None:
        r.dissolves = "not measured"
        r.notes.append("dissolves: not measured (no export of this timeline could be read back)")
        return
    where = f"the {FORMAT_NAMES[fmt]} export"
    changes = [d for d in back[fmt][0] if d["action"] == "transition" and (d.get("data") or {}).get("before", {}).get("frames")]
    cut = [d for d in changes if not (d.get("data") or {}).get("after", {}).get("frames")]
    other = [d for d in changes if d not in cut]
    if not changes:
        r.dissolves = "kept"
        r.preserved.append(f"dissolves: {total} of {total} kept in {where} (kind and length)")
        return
    if cut:
        r.dissolves = "lost"
        r.lost.append(f"dissolves: {len(cut)} of {total} are cuts in {where}" + (" (the build route adds none)" if r.route == "build" else ""))
    if other:
        r.dissolves = "lost" if cut else "changed"
        r.lost.append(f"dissolves: {len(other)} of {total} changed in {where}: " + "; ".join(d["detail"] for d in other))


def _markers(r: RouteResult, m: Optional[Dict[str, Any]], exp: Dict[str, Any]) -> None:
    if not m:
        return
    n = len(exp["junctions"])
    parts = []
    if r.route.startswith("import-"):
        carried = sum(1 for v in (m.get("found_before") or {}).values() if v)
        line = f"junction markers from the file: {carried} of {n} found by id"
        (r.preserved if carried == n else r.lost).append(line)
        parts.append(f"file {carried}/{n}")
    if m.get("added"):
        own = sum(1 for v in (m.get("found_after") or {}).values() if v)
        yes = sum(1 for a in m["added"].values() if a.get("add_marker") is True or a.get("update_marker_custom_data") is True)
        if own == n:
            r.preserved.append(f"junction markers found by id after the probe added them ({own} of {n})")
        else:
            r.lost.append(f"junction markers added by the probe: {own} of {n} found by id (AddMarker returned True {yes} times)")
        parts.append(f"own {own}/{n}")
    r.markers = ", ".join(parts) or "-"


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
        line = f"navigation: playhead on the incoming clip for {good} of {len(atts)} junctions ({how})"
        (r.preserved if good == len(atts) else r.lost).append(line)
        cells.append(f"{good}/{len(atts)}")
    skipped = [a for a in attempts if a.get("skipped")]
    if skipped:
        r.lost.append(f"navigation: {len(skipped)} junctions skipped: {skipped[0]['skipped']}")
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


def _exports(r: RouteResult, back: Dict[str, Tuple[Optional[List[Dict[str, Any]]], List[str], Optional[str]]], entry: Dict[str, Any]) -> None:
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


def _failed_route(r: RouteResult, entry: Dict[str, Any]) -> None:
    r.verdict = "failed"
    bad = next((s for s in entry.get("steps") or [] if not s.get("ok")), None)
    if entry.get("error"):
        r.lost.append(f"no timeline: the probe stopped on {entry['error']}")
    elif bad:
        r.lost.append(f"no timeline: {bad['call']} " + (f"raised {bad['error']}" if bad.get("error") else f"returned {json.dumps(bad.get('return'))}"))
    else:
        r.lost.append("no timeline: " + (entry.get("note") or "not made"))


def _route(route: str, doc: Dict[str, Any], exp: Dict[str, Any], back: Dict[str, Any]) -> RouteResult:
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
    _dissolves(r, exp, back)
    _transitions(r, entry.get("transitions"))
    _markers(r, entry.get("markers"), exp)
    _navigation(r, entry.get("navigation"), rb, exp)
    _exports(r, back, entry)
    rate_set = rb.get("frame_rate_setting")
    want = doc.get("project", {}).get("frame_rate_requested")
    if want and rate_set and str(rate_set) != str(want):
        r.lost.append(f"timeline frame rate setting {rate_set}, the cut is {want}")
    if ok is None:
        r.verdict = "not measured"
    elif ok is False:
        r.verdict = "not usable"
    elif r.dissolves in ("kept", "none in the cut"):
        r.verdict = "usable"
    elif r.dissolves in ("lost", "changed"):
        r.verdict = "usable, dissolves lost"
    else:
        r.verdict = "usable, dissolves not measured"
    return r


# -- the whole report ---------------------------------------------------------------------------------------------------------
def _recommend(routes: Dict[str, RouteResult]) -> Tuple[Optional[str], str]:
    for name in PREFERRED:
        if routes[name].verdict == "usable":
            return name, f"Recommended default route for M2c-1: **{name}** (frames exact and dissolves as expected, as measured)."
    for name in FALLBACK:
        if routes[name].verdict == "usable, dissolves lost":
            return name, (f"Recommended default route for M2c-1: **{name}**, but no route carried the dissolves on this install: "
                          "they would need another way (or stay cuts) in M2c-1.")
    for name in FALLBACK:
        if routes[name].verdict == "usable, dissolves not measured":
            return name, f"Recommended default route for M2c-1: **{name}**, with the dissolves not measured (no export could be read back)."
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
    lines = ["# Resolve probe report", "", _source_line(doc), "",
             f"- probe JSON: `{json_path}` (status {doc.get('status')}, {doc.get('created_utc')}, probe version {doc.get('probe_version')})",
             f"- stage `{exp['stage']}`: {len(exp['clips'])} clips, {len(exp['junctions'])} junctions, "
             f"{sum(1 for c in exp['clips'][1:] if c['transition_in']['frames'])} dissolves, {exp['rate'][0]}/{exp['rate'][1]} fps",
             f"- Resolve: {(str(res.get('product')) + ' ' + str(res.get('version_string'))) if res.get('product') else 'not reported'}; "
             f"{plat.get('system')} {plat.get('release')}; Python {plat.get('python')}",
             f"- scripting: connected {(doc.get('scripting') or {}).get('connected')}; module {(doc.get('scripting') or {}).get('module_path')}"]
    files = cat.get("files") or []
    if files:
        lines.append("- API catalogue: " + "; ".join(f"`{f.get('path')}` ({f.get('version_line') or 'no version line'})" for f in files))
    else:
        lines.append("- API catalogue: not found" + (" (so only README 21.0.4 calls were made)" if doc.get("timelines") else ""))
    gated = cat.get("gated") or {}
    if gated:
        lines.append("- calls newer than the 21.0.4 README: " + ", ".join(f"{k} {'documented, called' if v else 'not documented, not called'}" for k, v in gated.items()))
    lines.append(f"- Resolve MCP binary: {mcp.get('observed', 'not checked')} (observed only; never started)")
    lines.append(f"- projects: the open project at the start was {proj.get('user_current_project')!r}; the probe's project: {proj.get('name')!r} "
                 f"in the folder {proj.get('folder')!r}")
    lines += ["", "## Recommendation", "", recommendation]
    findable = [n for n in ROUTES if any(p.startswith(("junction markers found by id", "junction markers from the file")) for p in routes[n].preserved)]
    tried = [n for n in ROUTES if routes[n].markers != "-"]
    if tried:
        lines.append("")
        lines.append(f"Junction markers are findable by id on: {', '.join(findable)}." if findable else "Junction markers are not findable by id on any route.")
    lines += ["", "## Routes", ""] + _table(routes)
    for name in ROUTES:
        r = routes[name]
        lines += ["", f"### {name}: {r.verdict}", ""]
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
    """Read ``json_path`` and the bundle next to it, compare, write ``probe_report.md`` there; a one-line ``ProbeError`` on bad input."""
    ws, json_path = Path(ws), Path(json_path)
    doc = _load(json_path)
    if doc.get("schema") != SCHEMA:
        raise ProbeError(f"{json_path} is not a matchcut resolve_probe.json (schema {doc.get('schema')!r})")
    bundle = json_path.parent
    if not (bundle / EXPECTED).is_file() and (doc.get("bundle") or {}).get("dir"):
        bundle = Path(doc["bundle"]["dir"])
    if not (bundle / EXPECTED).is_file():
        raise ProbeError(f"no expected.json next to {json_path}: give the resolve_probe.json inside its probe bundle")
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
    back: Dict[str, Dict[str, Any]] = {}
    if any((e.get("exports") or {}) for e in timelines.values()):
        with tempfile.TemporaryDirectory(prefix="matchcut-probe-report-") as tmp:
            scratch, scfg = _scratch(ws, bundle, Path(tmp), cfg)
            for route, entry in timelines.items():
                back[route] = {}
                for fmt, e in (entry.get("exports") or {}).items():
                    path = bundle / e.get("file", "")
                    if e.get("ok") and path.is_file():
                        back[route][fmt] = _reimport(scratch, scfg, path, stage)
    routes = {name: _route(name, doc, exp, back.get(name, {})) for name in ROUTES}
    recommended, recommendation = _recommend(routes) if timelines else (None, "No route was tried, so there is nothing to recommend.")
    text = _render(doc, exp, json_path, routes, recommendation, notes)
    out = json_path.parent / REPORT
    try:
        out.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ProbeError(f"cannot write {out}: {exc.strerror or exc}") from exc
    return Report(text, out, routes, recommended, notes)
