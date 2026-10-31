"""The five outcome categories of a probe run, and the one table that sorts every finding and every recorded call into them.

A finding is a line the report writes about a route (what ``report._route`` puts in preserved / approximated / lost / notes). A call is
a step the probe recorded: every call that did not simply succeed is classified on its own; successful calls are counted as Passed.
``classify`` is the only place the rules live: the ``FINDING_RULES`` and ``CALL_RULES`` tables, tried in order, first match wins.
``annotate`` prepares what the call rules need to know, from the run's own read-back (which nulls belong to transition items, which
marker lookups came before the probe added its own markers), so that a rule never guesses.
"""
from __future__ import annotations

import json
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

PASSED = "Passed"
FAILED = "Failed"
UNSUPPORTED = "Unsupported"
NOT_PRESERVED = "Not preserved by Resolve"
NOT_TESTED = "Not tested"
CATEGORIES = (PASSED, FAILED, UNSUPPORTED, NOT_PRESERVED, NOT_TESTED)
LABELS = {PASSED: "passed", FAILED: "failed", UNSUPPORTED: "unsupported", NOT_PRESERVED: "not preserved", NOT_TESTED: "not tested"}

NOT_CALLED = "not documented on this install: not called"
FCPXML_NOTE = "reason not reported by Resolve; may be edition- or version-specific (unverified)"
IMPORTS = ("import-otio", "import-fcpxml", "import-edl")
# what each import's file can carry of matchcut's junction ids in a marker Resolve could look up by customData: the OTIO has the markers
# (with the id in their metadata); FCPXML markers have no customData field (matchcut writes the id as the marker's value); a CMX3600 EDL
# carries no markers at all
NO_CUSTOM_DATA = {"import-fcpxml": "FCPXML markers have no customData field (matchcut writes the junction id as the marker's value)",
                  "import-edl": "a CMX3600 EDL carries no markers, only comments"}
BUILDS = ("build", "build+transitions")
ITEM_FIELDS = {"TimelineItem.GetName": "name", "TimelineItem.GetStart": "start", "TimelineItem.GetEnd": "end", "TimelineItem.GetDuration": "duration",
               "TimelineItem.GetLeftOffset": "left_offset", "TimelineItem.GetRightOffset": "right_offset",
               "TimelineItem.GetSourceStartFrame": "source_start_frame", "TimelineItem.GetSourceEndFrame": "source_end_frame"}
OLD_README = ("TimelineItem.GetStart", "TimelineItem.GetEnd", "TimelineItem.GetDuration", "TimelineItem.GetLeftOffset", "TimelineItem.GetRightOffset",
              "TimelineItem.GetSourceStartFrame", "TimelineItem.GetSourceEndFrame")  # signatures that differ from, or are absent in, the 18.5 README


@dataclass
class Item:
    """One thing to classify: a finding (``kind`` "finding": ``group`` is preserved / approximated / lost / notes) or a call ("call": the
    recorded step, with ``note`` set by ``annotate``), on ``route`` ("" for the run as a whole)."""

    kind: str
    route: str
    text: str = ""
    group: str = ""
    step: Dict[str, Any] = field(default_factory=dict)
    note: str = ""  # what explains a call: transition-read, uniform-null, marker-before-file, marker-before-none, marker-after, ...


@dataclass
class Context:
    """What the rules may consult about the whole run."""

    old_version: Optional[str] = None  # the version string, when the Resolve major version is below 20
    transition_called: Dict[str, bool] = field(default_factory=dict)  # route -> AddTransition was called there
    transition_made: Dict[str, bool] = field(default_factory=dict)  # route -> AddTransition returned something for at least one dissolve
    media_found_in_pool: bool = False
    readback_markers: Dict[str, Optional[int]] = field(default_factory=dict)  # route -> markers its read-back shows (None: not read)
    unread: Dict[str, str] = field(default_factory=dict)  # route -> why its timeline, which exists, was never read back


def _call(it: Item) -> str:
    return str(it.step.get("call", ""))


def _ret(it: Item) -> Any:
    return it.step.get("return")


# -- the rules: (what it recognises, test, category, evidence) - tried in order, the first that matches decides ---------------------------
Rule = Tuple[str, Callable[[Item, Context], bool], str, Callable[[Item, Context], str]]

# the findings that come from a timeline's read-back: frames, dissolves, navigation
MEASURED_FROM_READBACK = ("clip count", "record start", "duration", "source in", "source out", "dissolves", "navigation", "2 transition items",
                          "transition items", "GetSourceEndFrame reports")


def _otio_markers(i: Item, c: Context) -> str:
    n = c.readback_markers.get(i.route)
    if not n:
        return "Resolve imported no markers from the OTIO (read-back shows none)"
    return f"the read-back shows {n} marker{'s' if n != 1 else ''}, none with matchcut's junction id as customData"


FINDING_RULES: List[Rule] = [
    ("not made: gated off, never started, or nothing to re-read", lambda i, c: i.group == "untested", NOT_TESTED, lambda i, c: ""),
    ("a timeline that was never read back", lambda i, c: i.route in c.unread and i.group != "untested" and (
        i.text.startswith(MEASURED_FROM_READBACK) or "differs on" in i.text), NOT_TESTED, lambda i, c: f"never read back ({c.unread[i.route]})"),
    ("file markers on a timeline that was never read back", lambda i, c: i.text.startswith("junction markers from the file")
     and c.readback_markers.get(i.route, 0) is None, NOT_TESTED, lambda i, c: "the read-back was not taken"),
    ("what came back as written", lambda i, c: i.group in ("preserved", "approximated"), PASSED, lambda i, c: ""),
    ("an export Resolve did not write", lambda i, c: i.group == "lost" and bool(re.match(r"^\w+ export: Timeline\.Export", i.text)), FAILED,
     lambda i, c: FCPXML_NOTE if i.text.startswith("FCPXML") else "reason not reported by Resolve"),
    ("an export matchcut cannot read", lambda i, c: i.group == "lost" and "export cannot be read back" in i.text, FAILED, lambda i, c: ""),
    ("an export that reads back with other clips or frames", lambda i, c: i.group == "lost" and "export reads back with" in i.text, NOT_PRESERVED,
     lambda i, c: "the timeline Resolve exported differs from the stage"),
    ("markers a file of this kind cannot carry by id", lambda i, c: i.text.startswith("junction markers from the file") and i.route in NO_CUSTOM_DATA,
     NOT_TESTED, lambda i, c: NO_CUSTOM_DATA[i.route]),
    ("the file's junction markers", lambda i, c: i.group == "lost" and i.text.startswith("junction markers from the file"), NOT_PRESERVED,
     _otio_markers),
    ("the probe's own markers", lambda i, c: i.group == "lost" and i.text.startswith("junction markers added by the probe"), FAILED, lambda i, c: ""),
    ("dissolves the build route never adds", lambda i, c: i.text.startswith("dissolves") and i.route == "build", NOT_TESTED,
     lambda i, c: "" if "the build route adds none" in i.text else "the build route adds no dissolves"),
    ("dissolves AddTransition was not asked for", lambda i, c: i.text.startswith("dissolves") and i.route == "build+transitions"
     and not c.transition_called.get(i.route), NOT_TESTED, lambda i, c: "AddTransition was not called on this install"),
    ("dissolves AddTransition did not make", lambda i, c: i.group == "lost" and i.text.startswith("dissolves") and i.route == "build+transitions",
     FAILED, lambda i, c: "AddTransition was called"),
    ("dissolves an imported file carried", lambda i, c: i.group == "lost" and i.text.startswith("dissolves"), NOT_PRESERVED,
     lambda i, c: "the imported file has them"),
    ("AddTransition not made", lambda i, c: i.text.startswith(("AddTransition: not documented", "AddTransition: not called")), NOT_TESTED,
     lambda i, c: "gated off: the installed catalogue does not document it" if NOT_CALLED in i.text else "no usable option or no item"),
    ("frames of an imported timeline", lambda i, c: i.group == "lost" and i.route in IMPORTS and ("differs on" in i.text or i.text.startswith("clip count")),
     NOT_PRESERVED, lambda i, c: "the imported file has the cut's frames"),
    ("reads a version before 20 may not have", lambda i, c: i.group == "notes" and ("not read for" in i.text or "18.5 README" in i.text)
     and c.old_version is not None, UNSUPPORTED, lambda i, c: f"Resolve {c.old_version}: not in the 18.5 README as the probe calls it"),
    ("reads that failed", lambda i, c: i.group == "notes" and "not read for" in i.text, FAILED, lambda i, c: ""),
    ("an experiment with nothing to measure", lambda i, c: i.group == "notes" and i.text.startswith(("the cut has no dissolves", "dissolves: not measured")),
     NOT_TESTED, lambda i, c: ""),
    ("export cuts where the build route made cuts", lambda i, c: i.group == "notes" and "transition changes and no clip" in i.text
     and (i.route == "build" or (i.route == "build+transitions" and not c.transition_made.get(i.route))), NOT_TESTED,
     lambda i, c: "the dissolves never existed on this route"),
    ("export cuts where an imported file had dissolves", lambda i, c: i.group == "notes" and "transition changes and no clip" in i.text, NOT_PRESERVED,
     lambda i, c: "the export lost the dissolves"),
    ("an observation", lambda i, c: i.group == "notes", PASSED, lambda i, c: ""),
    ("anything else lost", lambda i, c: True, FAILED, lambda i, c: ""),
]

CALL_RULES: List[Rule] = [
    ("a write stopped because the open project changed", lambda i, c: i.step.get("called") is False and str(i.step.get("error", "")).startswith(
        "not called: the open project"), FAILED, lambda i, c: "the probe stopped before this write"),
    ("a call gated off", lambda i, c: i.step.get("called") is False, NOT_TESTED, lambda i, c: "not documented on this install"),
    ("a read that does not apply to a transition item", lambda i, c: i.note == "transition-read", UNSUPPORTED,
     lambda i, c: "on transition items (identified by: no source frames, spanning an expected cut)"),
    ("a read that returns nothing on every clip of the run", lambda i, c: i.note == "uniform-null", UNSUPPORTED,
     lambda i, c: "None on every clip item read in this run"),
    ("a read an old version may not have", lambda i, c: c.old_version is not None and _call(i) in OLD_README, UNSUPPORTED,
     lambda i, c: f"Resolve {c.old_version}: not in the 18.5 README as the probe calls it"),
    ("the file's markers, looked up before the probe added its own", lambda i, c: i.note == "marker-before-file" and i.route in NO_CUSTOM_DATA,
     NOT_TESTED, lambda i, c: NO_CUSTOM_DATA[i.route]),
    ("the OTIO's markers, with no read-back to tell", lambda i, c: i.note == "marker-before-file" and c.readback_markers.get(i.route, 0) is None,
     NOT_TESTED, lambda i, c: "the read-back was not taken"),
    ("the OTIO's markers, looked up before the probe added its own", lambda i, c: i.note == "marker-before-file", NOT_PRESERVED, _otio_markers),
    ("the negative control: a built timeline before the probe's markers", lambda i, c: i.note == "marker-before-none", PASSED,
     lambda i, c: "nothing to find yet: a built timeline carries no file markers"),
    ("the probe's own marker, looked up after adding it", lambda i, c: i.note == "marker-after", FAILED,
     lambda i, c: "AddMarker said yes; the marker is not found by id"),
    ("a clip already in the pool", lambda i, c: _call(i) == "MediaPool.ImportMedia" and _ret(i) == [] and c.media_found_in_pool, PASSED,
     lambda i, c: "the clip was already in the media pool; the probe found it there"),
    ("a marker already on the frame", lambda i, c: i.note == "marker-updated", PASSED,
     lambda i, c: "a marker already sat on that frame; UpdateMarkerCustomData gave it the junction id"),
    ("anything else", lambda i, c: True, FAILED, lambda i, c: ""),
]


def classify(it: Item, ctx: Context) -> Tuple[str, str, str]:
    """(category, evidence, the rule's name) for a finding or a call: the first rule of its table that matches."""
    for name, test, category, evidence in (FINDING_RULES if it.kind == "finding" else CALL_RULES):
        if test(it, ctx):
            return category, evidence(it, ctx), name
    raise AssertionError("the last rule matches everything")


# -- what the call rules need to know ------------------------------------------------------------------------------------------------
def _key(step: Dict[str, Any]) -> str:
    return json.dumps([step.get("call"), step.get("args"), step.get("return"), step.get("error")], sort_keys=True, default=str)


def calls(doc: Dict[str, Any], is_transition: Callable[[Dict[str, Any], Dict[str, Any]], bool], uniform_fields: Dict[str, bool]) -> Tuple[List[Item], int]:
    """(every call that did not simply succeed, annotated; how many recorded calls succeeded). A call recorded in a step list is taken
    from there (its route is the list's); a quiet call (read-back, the item under the playhead) only from ``failures``."""
    timelines = doc.get("timelines") or {}
    lists: List[Tuple[str, List[Dict[str, Any]], str]] = [("", doc.get("steps") or [], ""), ("", (doc.get("media") or {}).get("steps") or [], "")]
    for route, e in timelines.items():
        lists.append((route, e.get("steps") or [], ""))
        lists.append((route, (e.get("markers") or {}).get("steps") or [], "markers"))
        lists.append((route, (e.get("navigation") or {}).get("steps") or [], ""))
        for x in (e.get("exports") or {}).values():
            lists.append((route, x.get("steps") or [], ""))
    items: List[Item] = []
    ok = 0
    seen: Counter = Counter()
    for route, steps, part in lists:
        marker_lookups = 0
        # the marker experiment looks every junction up once before adding its own markers, then again after
        n = len((((timelines.get(route) or {}).get("markers") or {}).get("found_before")) or {}) if part == "markers" else 0
        for k, s in enumerate(steps):
            if s.get("call") == "Timeline.GetMarkerByCustomData":
                marker_lookups += 1
            if s.get("ok"):
                ok += 1
                continue
            if s.get("called") is False and s.get("error") == NOT_CALLED:
                continue  # a gated call: one "not tested" finding says so for the whole run
            seen[_key(s)] += 1
            it = Item("call", route, step=s)
            if s.get("call") == "Timeline.GetMarkerByCustomData" and not s.get("error") and s.get("return") in (None, {}):
                # an empty answer only: a lookup that raised is a failure whatever its position
                before = marker_lookups <= n
                it.note = ("marker-before-file" if route in IMPORTS else "marker-before-none") if before else "marker-after"
            elif s.get("call") == "Timeline.AddMarker" and s.get("return") is False:
                nxt = steps[k + 1] if k + 1 < len(steps) else {}
                if nxt.get("call") == "Timeline.UpdateMarkerCustomData" and nxt.get("return") is True:
                    it.note = "marker-updated"
            items.append(it)
    quiet = [f for f in doc.get("failures") or [] if f.get("call") and f.get("call") != "(probe)"]
    tr_left: Dict[Tuple[str, str], int] = {}
    for route, e in timelines.items():  # how many nulls of each item read the transition items of this route's read-back account for
        rb = e.get("readback") or {}
        for track in (rb.get("video") or []) + (rb.get("audio") or []):
            for v in track or []:
                if v and is_transition(v, rb):
                    for call, fld in ITEM_FIELDS.items():
                        if v.get(fld) is None:
                            tr_left[(route, call)] = tr_left.get((route, call), 0) + 1
    for f in quiet:
        if seen[_key(f)]:
            seen[_key(f)] -= 1  # already classified from its step list
            continue
        route = str(f.get("where", "")).split(":")[0]
        route = route if route in timelines else ""
        it = Item("call", route, step=f)
        call = str(f.get("call"))
        if call in ITEM_FIELDS and f.get("return") is None and not f.get("error"):
            if tr_left.get((route, call), 0) > 0:
                tr_left[(route, call)] -= 1
                it.note = "transition-read"
            elif uniform_fields.get(ITEM_FIELDS[call]):
                it.note = "uniform-null"
        items.append(it)
    return items, ok


# -- grouping and presentation -------------------------------------------------------------------------------------------------------
def _routes(rs: List[str]) -> str:
    rs = [r for r in OrderedDict.fromkeys(rs) if r]
    return f" ({', '.join(rs)})" if rs else ""


def _outcome(step: Dict[str, Any]) -> str:
    if step.get("called") is False:
        return f"not called: {step.get('error')}"
    return f"raised {step['error']}" if step.get("error") else f"returned {json.dumps(step.get('return'))}"


def render(findings: List[Tuple[Item, str, str]], call_items: List[Tuple[Item, str, str]], ok_calls: int, headline: str) -> List[str]:
    """The Outcomes section: the five headings in order, each with its counts and grouped lines (identical lines across routes once)."""
    out = ["## Outcomes", "", headline, "Every finding and every recorded call that did not simply succeed is in exactly one category below.", ""]
    for cat in CATEGORIES:
        f_groups: "OrderedDict[str, List[str]]" = OrderedDict()
        for it, c, ev in findings:
            if c == cat:
                f_groups.setdefault(it.text + (f" ({ev})" if ev else ""), []).append(it.route)
        c_groups: "OrderedDict[str, List[str]]" = OrderedDict()
        for it, c, ev in call_items:
            if c == cat:
                c_groups.setdefault(f"`{it.step.get('call')}` {_outcome(it.step)}" + (f": {ev}" if ev else ""), []).append(it.route)
        n_f = sum(len(v) for v in f_groups.values())
        n_c = sum(len(v) for v in c_groups.values()) + (ok_calls if cat == PASSED else 0)
        out.append(f"### {cat} ({n_f} finding{'s' if n_f != 1 else ''}, {n_c} call{'s' if n_c != 1 else ''})")
        out.append("")
        for text, rs in f_groups.items():
            out.append(f"- {text}{_routes(rs)}")
        for text, rs in c_groups.items():
            out.append(f"- {text}: {len(rs)} time{'s' if len(rs) != 1 else ''}{_routes(rs)}")
        if cat == PASSED and ok_calls:
            out.append(f"- {ok_calls} recorded calls returned what was expected")
        if not (f_groups or c_groups or (cat == PASSED and ok_calls)):
            out.append("- none")
        out.append("")
    return out


def raw_steps(doc: Dict[str, Any]) -> List[str]:
    """Appendix: every step the probe recorded as failed or None, exactly as observed (where, call, arguments, return value, error)."""
    lines = ["## Raw steps", "", "Every step the probe recorded as not ok, exactly as observed, in the order it happened.", ""]
    rows = [f for f in doc.get("failures") or []]  # (calls gated off are not failures: Outcomes lists them under Not tested)
    for f in rows:
        lines.append(f"- {f.get('where')} | `{f.get('call')}` | args {json.dumps(f.get('args'), default=str)} | return {json.dumps(f.get('return'), default=str)}"
                     f" | error {json.dumps(f.get('error'))}")
    if not rows:
        lines.append("- none")
    return lines
