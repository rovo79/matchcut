"""Regression: the first live probe run (DaVinci Resolve 21.0.2.4 free, macOS, from the Py3 console, 2026-10-29).

The fake variant "resolve 21.0.2 free (observed)" reproduces that run exactly: the same cut of the same demo footage (5 clips,
2 dissolves, 462 frames of media), the same read-back and the same 48 null steps.
- GetMarkerByCustomData x16: the lookups before the probe added its own markers, 4 on each of the 3 imports and 4 on build.
- GetLeftOffset, GetRightOffset, GetSourceStartFrame and GetSourceEndFrame x8 each: the transition items, 2 per import on video track 1
  and 2 on import-otio's audio track.
Before the fixes the report called every route unusable and every null a failure.
"""
from __future__ import annotations

from collections import Counter

import pytest

from matchcut.resolve.outcomes import FAILED, NOT_PRESERVED, NOT_TESTED, PASSED, UNSUPPORTED
from matchcut.resolve.probe import console_lines
from matchcut.resolve.report import build_report

from .fake_resolve import VARIANTS
from .test_console import console

# video track 1 as the live run read it back, identically for import-otio, import-fcpxml and import-edl (timeline start 0, 00:00:00:00)
OBSERVED = [
    {"name": "matchcut-demo.mp4", "start": 0, "end": 50, "duration": 50, "source_start_frame": 0, "source_end_frame": 50},
    {"name": "matchcut-demo.mp4", "start": 50, "end": 65, "duration": 15, "source_start_frame": 106, "source_end_frame": 121},
    {"name": "Cross Dissolve", "start": 61, "end": 69, "duration": 8, "source_start_frame": None, "source_end_frame": None},
    {"name": "matchcut-demo.mp4", "start": 65, "end": 114, "duration": 49, "source_start_frame": 167, "source_end_frame": 216},
    {"name": "Cross Dissolve", "start": 105, "end": 123, "duration": 18, "source_start_frame": None, "source_end_frame": None},
    {"name": "matchcut-demo.mp4", "start": 114, "end": 155, "duration": 41, "source_start_frame": 359, "source_end_frame": 400},
    {"name": "matchcut-demo.mp4", "start": 155, "end": 173, "duration": 18, "source_start_frame": 444, "source_end_frame": 461},
]
# the "Failed steps" section of the live run's report, as the user sent it
OBSERVED_NULLS = {"TimelineItem.GetLeftOffset": 8, "TimelineItem.GetRightOffset": 8, "TimelineItem.GetSourceStartFrame": 8,
                  "TimelineItem.GetSourceEndFrame": 8, "Timeline.GetMarkerByCustomData": 16}
IMPORTS = ("import-otio", "import-fcpxml", "import-edl")
ROUTES = IMPORTS + ("build", "build+transitions")
KEYS = ("start", "end", "duration", "source_start_frame", "source_end_frame")


@pytest.fixture
def live(wsp, bundle, monkeypatch):
    _, real = console_lines(bundle.script)
    doc, world = console(bundle, monkeypatch, real, VARIANTS["resolve 21.0.2 free (observed)"])
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    return doc, rep


def test_the_fake_reproduces_the_observed_run(live):
    doc, _ = live
    assert doc["status"] == "complete" and doc["scripting"]["mode"] == "in-app console"
    assert doc["resolve"] == {"product": "DaVinci Resolve", "version": [21, 0, 2, 4, ""], "version_string": "21.0.2.4"}
    assert doc["catalogue"]["files"][0]["version_line"] == "Last Updated: 26 May 2026" and doc["catalogue"]["gated"] == {
        "TimelineItem.AddTransition": False, "TimelineItem.GetType": False}
    for route in IMPORTS:
        rb = doc["timelines"][route]["readback"]
        assert rb["start_frame"] == 0 and rb["start_timecode"] == "00:00:00:00"
        assert [{k: v[k] for k in KEYS} for v in rb["video"][0]] == [{k: o[k] for k in KEYS} for o in OBSERVED], route
        assert [rb["video"][0][k]["name"] for k in (2, 4)] == ["Cross Dissolve", "Cross Dissolve"]
        assert all((v["left_offset"] is None) == (v["source_start_frame"] is None) for v in rb["video"][0])  # None on transition items only
    audio = {r: sum(1 for v in doc["timelines"][r]["readback"]["audio"][0] if v["source_start_frame"] is None) for r in IMPORTS}
    assert audio == {"import-otio": 2, "import-fcpxml": 0, "import-edl": 0}
    for route in ("build", "build+transitions"):
        rb = doc["timelines"][route]["readback"]
        clips = [o for o in OBSERVED if o["source_start_frame"] is not None]
        assert rb["start_frame"] == 90000 and rb["start_timecode"] == "01:00:00:00"
        assert [{k: v[k] for k in KEYS} for v in rb["video"][0]] == [dict({k: o[k] for k in KEYS}, start=o["start"] + 90000, end=o["end"] + 90000)
                                                                      for o in clips]
    for e in doc["timelines"].values():  # FCPXML: Export reported success, and no file came back
        x = e["exports"]["fcpxml"]
        assert x["ok"] is True and x["exists"] is False and e["exports"]["otio"]["exists"] and e["exports"]["edl"]["exists"]
    assert Counter(f["call"] for f in doc["failures"]) == OBSERVED_NULLS and len(doc["failures"]) == 48
    assert all(f["return"] is None and f.get("error") is None and f["called"] for f in doc["failures"])
    assert Counter(f["where"].split(":")[0] for f in doc["failures"] if f["call"] == "Timeline.GetMarkerByCustomData") == {
        "import-otio": 4, "import-fcpxml": 4, "import-edl": 4, "build": 4}
    assert doc["mcp"]["observed"] == "file absent"


def test_the_route_verdicts_and_recommendation(live):
    _, rep = live
    for route in IMPORTS:
        r = rep.routes[route]
        assert (r.verdict, r.frames, r.dissolves, r.navigation_ok, r.transition_items) == ("usable", "exact", "kept", True, 2), route
        assert "dissolves read back: 2 of 2, exact length and centred (transition items)" in r.preserved
        assert "FCPXML export: Timeline.Export returned True but no file is at the path given" in " ".join(r.lost)
    assert rep.routes["import-otio"].own_markers == "yes" and rep.routes["import-otio"].file_markers == "no"
    for route in ("build", "build+transitions"):
        assert (rep.routes[route].verdict, rep.routes[route].frames) == ("usable, dissolves lost", "exact")
    assert rep.recommended == "import-otio" and "junction markers must be added by id after import" in rep.text
    assert "None of the routes is usable" not in rep.text and "Measured once, on one install: DaVinci Resolve 21.0.2.4 (free edition), from Resolve's own console." in rep.text


def test_the_outcomes_of_the_live_run(live):
    _, rep = live
    o = rep.outcomes
    # Failed: the FCPXML export, on each of the 5 routes, and nothing else
    assert [(k, w.split(":")[0], r) for k, w, r, _ in o[FAILED]] == [("finding", "FCPXML export", r) for r in ROUTES]
    assert all("may be edition- or version-specific (unverified)" in ev for _, _, _, ev in o[FAILED])
    # Unsupported: the 32 reads on transition items, with their evidence
    assert Counter(w.split()[0] for k, w, _, _ in o[UNSUPPORTED]) == {c: 8 for c in OBSERVED_NULLS if c.startswith("TimelineItem")}
    assert all(k == "call" and r in IMPORTS and "on transition items (identified by: no source frames, spanning an expected cut)" == ev
               for k, w, r, ev in o[UNSUPPORTED])
    # the 16 null marker lookups: 4 Not preserved (Resolve imported no markers from the OTIO), 8 Not tested (FCPXML markers have no
    # customData field; an EDL carries no markers), 4 Passed (the negative control: a built timeline before the probe's own markers)
    assert [(k, r, ev) for k, w, r, ev in o[NOT_PRESERVED]] == [
        ("finding", "import-otio", "Resolve imported no markers from the OTIO (read-back shows none)")] + [
        ("call", "import-otio", "Resolve imported no markers from the OTIO (read-back shows none)")] * 4
    lookups = lambda cat: Counter(r for k, w, r, ev in o[cat] if k == "call" and w.startswith("Timeline.GetMarkerByCustomData"))  # noqa: E731
    assert lookups(NOT_TESTED) == {"import-fcpxml": 4, "import-edl": 4} and lookups(PASSED) == {"build": 4} and lookups(NOT_PRESERVED) == {"import-otio": 4}
    evid = {r: ev for k, w, r, ev in o[NOT_TESTED] if k == "finding" and w.startswith("junction markers from the file")}
    assert evid == {"import-fcpxml": "FCPXML markers have no customData field (matchcut writes the junction id as the marker's value)",
                    "import-edl": "a CMX3600 EDL carries no markers, only comments"}
    # Not tested: AddTransition and GetType (gated off), the MCP server, the FCPXML files that never came back, the build routes' dissolves
    untested = [w for k, w, r, _ in o[NOT_TESTED]]
    for needle in ("TimelineItem.AddTransition: not documented on this install, so never called",
                   "TimelineItem.GetType: not documented on this install, so never called",
                   "Resolve's MCP server: never started by the probe (binary file absent)"):
        assert needle in untested, needle
    assert untested.count("FCPXML export not re-read by matchcut: no file came back") == 5
    # every one of the 48 null steps is classified exactly once
    assert sum(1 for c in o.values() for k, *_ in c if k == "call") == 48


def test_the_report_text_has_the_five_headings_in_order_and_the_raw_steps(live):
    _, rep = live
    heads = [ln for ln in rep.text.splitlines() if ln.startswith("### ") and "(" in ln and "finding" in ln]
    assert [h.split(" (")[0][4:] for h in heads] == [PASSED, FAILED, UNSUPPORTED, NOT_PRESERVED, NOT_TESTED]
    assert "### Failed (5 findings, 0 calls)" in rep.text and "### Unsupported (0 findings, 32 calls)" in rep.text
    assert "### Not preserved by Resolve (1 finding, 4 calls)" in rep.text and "## Failed steps" not in rep.text
    assert "### Not tested (17 findings, 8 calls)" in rep.text and "### Passed (46 findings, 150 calls)" in rep.text
    assert ("- FCPXML export: Timeline.Export returned True but no file is at the path given "
            "(reason not reported by Resolve; may be edition- or version-specific (unverified)) "
            "(import-otio, import-fcpxml, import-edl, build, build+transitions)") in rep.text
    assert ("- `TimelineItem.GetLeftOffset` returned null: on transition items (identified by: no source frames, spanning an expected cut): "
            "8 times (import-otio, import-fcpxml, import-edl)") in rep.text
    raw = rep.text.split("## Raw steps", 1)[1].splitlines()
    rows = [ln for ln in raw if ln.startswith("- ")]
    assert len(rows) == 48
    assert rows[0] == "- import-otio: readback | `TimelineItem.GetLeftOffset` | args [false] | return null | error null"
    assert "- build: markers | `Timeline.GetMarkerByCustomData` | args [\"s0-s1\"] | return null | error null" in rows


def test_transition_items_starting_at_the_cut_are_still_transitions(wsp, bundle, monkeypatch):
    """Review item 7: an install that places a transition item starting at the cut (start == cut) is still read as a transition."""
    import dataclasses

    v = dataclasses.replace(VARIANTS["resolve 21.0.2 free (observed)"], transition_at_cut=True)
    _, real = console_lines(bundle.script)
    console(bundle, monkeypatch, real, v)
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    for route in IMPORTS:
        r = rep.routes[route]
        assert (r.verdict, r.frames, r.dissolves, r.transition_items) == ("usable", "exact", "kept", 2), route
        assert any("the transition items start at the cut (they sit after it) on this install" in a for a in r.approximated)
    assert rep.recommended == "import-otio" and [w.split(":")[0] for k, w, r, ev in rep.outcomes[FAILED]] == ["FCPXML export"] * 5


@pytest.mark.parametrize("route_kind", ["import-edl", "build", "import-otio"])
def test_a_marker_lookup_that_raised_is_failed_whatever_its_position(wsp, bundle, monkeypatch, route_kind):
    """Review item 1: the before/after notes apply to an empty answer only; an exception is Failed on every route."""
    import dataclasses

    v = dataclasses.replace(VARIANTS["resolve 21.0.2 free (observed)"], raise_on=("Timeline.GetMarkerByCustomData",))
    _, real = console_lines(bundle.script)
    console(bundle, monkeypatch, real, v)
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    raised = lambda cat: [r for k, w, r, ev in rep.outcomes[cat] if k == "call" and w.startswith("Timeline.GetMarkerByCustomData raised")]  # noqa: E731
    assert route_kind in raised(FAILED)
    assert all(route_kind not in raised(c) for c in (PASSED, UNSUPPORTED, NOT_PRESERVED, NOT_TESTED))
