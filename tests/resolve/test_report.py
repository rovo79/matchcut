"""``matchcut.resolve report``: route verdicts and fidelity differences, from resolve_probe.json made against each fake variant."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from matchcut.resolve import ProbeError
from matchcut.resolve.report import build_report

from .conftest import last_run, run_probe
from .fake_resolve import VARIANTS, Variant

ROUTES = ["import-otio", "import-fcpxml", "import-edl", "build", "build+transitions"]
FROM_EXPORTS = "the OTIO, FCPXML and EDL exports; the read-back does not show transitions"


def ws_hashes(ws: Path) -> dict:
    return {str(p.relative_to(ws)): hashlib.sha256(p.read_bytes()).hexdigest() for p in ws.rglob("*") if p.is_file() and "resolve_probe" not in p.parts}


def probe_and_report(wsp, bundle, monkeypatch, variant):
    code, doc, world = run_probe(bundle, monkeypatch, variant)
    assert world.rejected == []
    before = ws_hashes(wsp.ws)
    rep = build_report(wsp.ws, wsp.cfg, last_run(bundle))
    assert ws_hashes(wsp.ws) == before  # the re-imports ran in a scratch copy, never in the workspace
    return rep, doc


def verdicts(rep):
    return {r: rep.routes[r].verdict for r in ROUTES}


def counts(bundle):
    exp = json.loads(bundle.expected.read_text())
    return len(exp["clips"]), len(exp["junctions"]), sum(1 for c in exp["clips"][1:] if c["transition_in"]["frames"])


def test_a_fake_that_keeps_everything(wsp, bundle, monkeypatch):
    rep, doc = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["keeps everything"])
    n, j, d = counts(bundle)
    assert verdicts(rep) == {"import-otio": "usable", "import-fcpxml": "usable", "import-edl": "usable", "build": "usable, dissolves lost",
                             "build+transitions": "usable"}
    assert rep.recommended == "import-otio"
    otio = rep.routes["import-otio"]
    assert any(p.startswith("record start of every clip") for p in otio.preserved)
    assert f"dissolves: {d} of {d} kept, kind and length ({FROM_EXPORTS})" in otio.preserved and otio.dissolves_from == FROM_EXPORTS
    assert any(p.startswith("junction markers found by id") for p in otio.preserved)
    assert otio.lost == [] and rep.routes["build"].lost == [f"dissolves: {d} of {d} are cuts ({FROM_EXPORTS}) (the build route adds none)"]
    text = rep.path.read_text()
    assert rep.path == last_run(bundle).parent / "probe_report.md"
    assert text == rep.text and "Recommended default route for M2c-1: **import-otio** (frames exact and dissolves as expected, as measured)." in text
    assert "not run against a live Resolve" in text and "matchcut fake of the documented Resolve API" in text
    assert "| build+transitions | usable |" in text and f"(dissolves judged from {FROM_EXPORTS})" in text


def test_a_fake_that_loses_dissolves(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["loses dissolves"])
    n, j, d = counts(bundle)
    assert verdicts(rep) == {r: "usable, dissolves lost" for r in ROUTES}
    assert rep.recommended == "build"
    assert f"dissolves: {d} of {d} are cuts ({FROM_EXPORTS})" in rep.routes["import-otio"].lost
    assert f"AddTransition: called for {d} dissolves, returned None {d} times" in rep.routes["build+transitions"].lost
    assert "no route carried the dissolves" in rep.text


def test_a_fake_that_shifts_frames(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["shifts frames by 1"])
    n, j, d = counts(bundle)
    assert verdicts(rep) == {r: "not usable" for r in ROUTES}
    assert rep.recommended is None and "None of the routes is usable on this install" in rep.text
    lost = rep.routes["import-otio"].lost
    assert f"source in differs on {n} of {n} clips: +1 frame ({n})" in lost and f"source out differs on {n} of {n} clips: +1 frame ({n})" in lost
    assert any(x.startswith(f"OTIO export reads back with {n} trims") for x in lost)
    assert any(p.startswith("record start of every clip") for p in rep.routes["import-otio"].preserved)


def test_a_fake_that_drops_markers(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["drops markers"])
    n, j, d = counts(bundle)
    assert rep.recommended == "import-otio" and rep.routes["import-otio"].verdict == "usable"
    assert f"junction markers from the file: 0 of {j} found by id" in rep.routes["import-otio"].lost
    assert f"junction markers added by the probe: 0 of {j} found by id (AddMarker returned True {j} times)" in rep.routes["build"].lost
    assert "Junction markers are not findable by id on any route." in rep.text
    # the recommendation says what that means for M2c-1 instead of recommending the route as if it were complete
    assert ("**import-otio** (frames exact and dissolves as expected, as measured); it needs markers re-added by id: none was found by id on it, "
            "nor on any other route; navigation by timecode lands on the incoming clip.") in rep.text


def test_the_recommendation_prefers_a_route_where_markers_are_found_and_navigation_lands():
    from matchcut.resolve.report import RouteResult, _recommend

    def routes(**spec):
        out = {}
        for name in ROUTES:
            nav, own, verdict = spec.get(name.replace("+", "_").replace("-", "_"), (True, "untested", "not usable"))
            r = RouteResult(name, verdict=verdict)
            r.navigation_ok, r.own_markers, r.markers_by_id = nav, own, own
            out[name] = r
        return out

    # the probe's own markers fail on import-otio but work on build: import-fcpxml (untested, so scored like the install) comes first
    name, text = _recommend(routes(import_otio=(True, "no", "usable"), import_fcpxml=(True, "untested", "usable"), build=(True, "yes", "usable")))
    assert name == "import-fcpxml" and "junction markers were not tested on this route (they are found by id on build)" in text
    # markers work nowhere: untested is no better than failed, and the plan's order decides
    name, text = _recommend(routes(import_otio=(True, "no", "usable"), import_fcpxml=(True, "untested", "usable"), build=(True, "no", "usable")))
    assert name == "import-otio" and "it needs markers re-added by id: none was found by id on it, nor on any other route" in text
    # navigation that does not land counts more than markers
    name, text = _recommend(routes(import_otio=(False, "yes", "usable"), import_fcpxml=(True, "untested", "usable")))
    assert name == "import-fcpxml"
    # a cut without dissolves (every route usable, markers work where tested): the plan's import-first order
    name, _ = _recommend(routes(import_otio=(True, "yes", "usable"), import_fcpxml=(True, "untested", "usable"), import_edl=(True, "untested", "usable"),
                                build=(True, "yes", "usable"), build_transitions=(True, "untested", "usable")))
    assert name == "import-otio"


def test_add_transition_not_documented(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["AddTransition not documented"])
    n, j, d = counts(bundle)
    bt = rep.routes["build+transitions"]
    assert bt.verdict == "usable, dissolves lost" and f"AddTransition: not documented on this install: not called ({d} dissolves)" in bt.lost
    assert rep.recommended == "import-otio"


def test_append_to_timeline_none(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["AppendToTimeline returns [None]"])
    n, j, d = counts(bundle)
    assert rep.routes["build"].verdict == "not usable" and f"clip count 0, expected {n}" in rep.routes["build"].lost
    assert f"AppendToTimeline returned [None] for {n} of {n} clips" in rep.routes["build"].lost
    assert rep.recommended == "import-otio" and "MediaPool.AppendToTimeline" in rep.text


def test_an_inclusive_get_source_end_frame_is_named_not_lost(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["GetSourceEndFrame inclusive"])
    n, j, d = counts(bundle)
    assert verdicts(rep)["import-otio"] == "usable" and rep.recommended == "import-otio"
    for r in ROUTES:
        assert f"source out reads one frame less than expected on every clip ({n}): GetSourceEndFrame is inclusive on this install" in rep.routes[r].approximated
        assert not any(x.startswith("source out differs") for x in rep.routes[r].lost)


def test_an_inclusive_end_frame_is_named_and_the_build_route_stays_usable(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["endFrame inclusive"])
    n, j, d = counts(bundle)
    for r in ("build", "build+transitions"):
        assert rep.routes[r].frames == "exact" and rep.routes[r].verdict.startswith("usable")
        assert any("endFrame is inclusive on this install" in a for a in rep.routes[r].approximated)
        assert not any(x.startswith(("duration differs", "source out differs")) for x in rep.routes[r].lost)
    assert verdicts(rep)["import-otio"] == "usable"  # the imports do not use endFrame


def test_transition_items_are_not_clips_and_judge_the_dissolves(wsp, bundle, monkeypatch):
    rep, doc = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["transition items"])
    n, j, d = counts(bundle)
    items = doc["timelines"]["import-otio"]["readback"]["video"][0]
    assert sum(1 for v in items if v["type"] == "transition") == d and len(items) == n + d
    otio = rep.routes["import-otio"]
    assert otio.verdict == "usable" and otio.transition_items == d and otio.dissolves_from == "the read-back (transition items)"
    assert f"dissolves read back: {d} of {d}, exact length and centred (transition items)" in otio.preserved
    assert f"dissolves: {d} of {d} have no transition item in the read-back (the build route adds none)" in rep.routes["build"].lost
    assert rep.routes["build+transitions"].verdict == "usable"  # AddTransition made them, and the read-back shows them


@pytest.mark.parametrize("variant, status", [("scripting not connected", "not connected"), ("a user project is open", "refused")])
def test_a_probe_that_measured_nothing_says_so(wsp, bundle, monkeypatch, variant, status):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS[variant])
    rep = build_report(wsp.ws, wsp.cfg, last_run(bundle))
    assert rep.recommended is None and all(r.verdict == "not tried" for r in rep.routes.values())
    assert f"the probe stopped before any route: {status}" in rep.text and "not run against a live Resolve" in rep.text


def test_the_navigation_and_export_lines(wsp, bundle, monkeypatch):
    rep, doc = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["keeps everything"])
    k = min(5, counts(bundle)[1])
    assert f"navigation: playhead on the incoming clip for {k} of {k} junctions (start timecode + record frame)" in rep.routes["build"].preserved
    assert any(x.startswith("FCPXML export reads back with no differences") for x in rep.routes["import-otio"].preserved)


def test_frames_that_could_not_be_read_are_not_measured_not_unusable(wsp, bundle, monkeypatch):
    v = Variant(raise_on=("TimelineItem.GetSourceStartFrame",))
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, v)
    assert {r.verdict for r in rep.routes.values()} == {"not measured"} and rep.recommended is None
    n = counts(bundle)[0]
    assert f"source in: not read for {n} of {n} clips" in rep.routes["import-otio"].notes
    assert "TimelineItem.GetSourceStartFrame" in rep.text  # the failed reads are listed
    assert "No route could be judged: the frames of import-otio, import-fcpxml, import-edl, build, build+transitions could not be read back" in rep.text


def test_an_older_resolve_gets_the_version_note_in_the_report(wsp, bundle, monkeypatch):
    rep, doc = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["Resolve 18.5"])
    assert rep.routes["import-otio"].verdict == "not measured"
    assert any("not in the 18.5 README" in x for x in rep.routes["import-otio"].notes)
    assert "- version note: GetStart(subframe_precision), GetSourceStartFrame and GetSourceEndFrame are documented" in rep.text


def test_the_report_takes_the_bundle_or_a_run_directory(wsp, bundle, monkeypatch):
    code, doc, _ = run_probe(bundle, monkeypatch)
    run = bundle.dir / doc["run_dir"]
    run_probe(bundle, monkeypatch, args=["--dry-run"])  # newer, but a dry run: skipped
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    assert rep.path == run / "probe_report.md" and any(n.startswith("the latest run, ") and "was a dry run: reporting" in n for n in rep.notes)
    assert build_report(wsp.ws, wsp.cfg, run).path == run / "probe_report.md"


def test_the_report_skips_runs_that_did_nothing(wsp, bundle, monkeypatch):
    code, done, _ = run_probe(bundle, monkeypatch)
    run_probe(bundle, monkeypatch, VARIANTS["a user project is open"])  # refused
    code, unconfirmed, _ = run_probe(bundle, monkeypatch, answer="no")  # not confirmed, the newest
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    assert rep.path == bundle.dir / done["run_dir"] / "probe_report.md"
    assert f"the latest run, {Path(unconfirmed['run_dir']).name}, was not confirmed: reporting {Path(done['run_dir']).name}" in rep.notes


def test_a_complete_run_wins_over_newer_stopped_unconfirmed_and_dry_runs(wsp, bundle, monkeypatch):
    code, done, _ = run_probe(bundle, monkeypatch)
    code, stopped, _ = run_probe(bundle, monkeypatch, VARIANTS["the open project changes mid-run"])  # aborted, but recorded timelines
    assert stopped["status"] == "aborted" and stopped["timelines"]
    run_probe(bundle, monkeypatch, answer="no")
    run_probe(bundle, monkeypatch, args=["--dry-run"])
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    assert rep.path == bundle.dir / done["run_dir"] / "probe_report.md" and rep.routes["import-otio"].verdict == "usable"


def test_without_a_complete_run_one_that_recorded_timelines_is_reported(wsp, bundle, monkeypatch):
    code, stopped, _ = run_probe(bundle, monkeypatch, VARIANTS["the open project changes mid-run"])
    run_probe(bundle, monkeypatch, answer="no")
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    assert rep.path == bundle.dir / stopped["run_dir"] / "probe_report.md"
    assert any(n.endswith("(aborted; no run is complete)") for n in rep.notes) and "nothing to judge" in rep.text


def test_without_a_complete_run_the_newest_real_run_is_reported_and_named(wsp, bundle, monkeypatch):
    code, refused, _ = run_probe(bundle, monkeypatch, VARIANTS["a user project is open"])
    run_probe(bundle, monkeypatch, args=["--dry-run"])
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    assert rep.path == bundle.dir / refused["run_dir"] / "probe_report.md"
    assert any(n.endswith("(refused; no run is complete)") for n in rep.notes)


def test_a_stopped_run_has_nothing_to_judge(wsp, bundle, monkeypatch):
    rep, doc = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["the open project changes mid-run"])
    assert rep.recommended is None and "The run was stopped (" in rep.text and "); nothing to judge." in rep.text
    assert "None of the routes is usable" not in rep.text


def test_transition_items_offset_alike_are_a_position_convention(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["transition items at the cut"])
    n, j, d = counts(bundle)
    otio = rep.routes["import-otio"]
    assert otio.verdict == "usable" and otio.dissolves == "kept"
    assert (f"dissolves: {d} of {d} read back as transition items of the expected length; the transition items start at the cut "
            "(they sit after it) on this install (matchcut centres them)") in otio.approximated


def test_the_report_names_a_changed_bundle_or_stage(wsp, bundle, monkeypatch):
    run_probe(bundle, monkeypatch)
    stage = wsp.ws / "final.json"
    stage.write_text(stage.read_text().replace('"name"', '"name" ', 1))
    rep = build_report(wsp.ws, wsp.cfg, last_run(bundle))
    assert "the stage file final.json changed since the bundle was made" in rep.text


def test_bad_input_is_one_line(wsp, bundle, tmp_path):
    with pytest.raises(ProbeError) as err:
        build_report(wsp.ws, wsp.cfg, tmp_path / "nope.json")
    assert str(err.value).startswith("cannot read ") and "\n" not in str(err.value)
    with pytest.raises(ProbeError) as err:
        build_report(wsp.ws, wsp.cfg, bundle.dir)
    assert str(err.value).startswith(f"no run in {bundle.dir}: run resolve_probe.py first")
    bad = tmp_path / "resolve_probe.json"
    bad.write_text(json.dumps({"schema": "something else"}))
    with pytest.raises(ProbeError) as err:
        build_report(wsp.ws, wsp.cfg, bad)
    assert "not a matchcut resolve_probe.json" in str(err.value)
    bad.write_text(json.dumps({"schema": "matchcut.resolve_probe/1"}))
    with pytest.raises(ProbeError) as err:
        build_report(wsp.ws, wsp.cfg, bad)
    assert "no expected.json next to" in str(err.value)


def _cats(rep, kind=None):
    return {c: [(k, w, r) for k, w, r, _ in items if kind in (None, k)] for c, items in rep.outcomes.items()}


def test_the_outcomes_of_a_fake_that_keeps_everything(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["keeps everything"])
    o = _cats(rep)
    assert o["Failed"] == [] and o["Unsupported"] == []
    # FCPXML markers have no customData field and an EDL carries no markers: neither is a loss by Resolve
    assert o["Not preserved by Resolve"] == []
    passed_calls = [w for k, w, r in o["Passed"] if k == "call"]
    assert "MediaPool.ImportMedia returned []" in passed_calls and passed_calls.count("Timeline.AddMarker returned false") == 4
    assert [w for k, w, r in o["Not tested"]] == ["Resolve's MCP server: never started by the probe (binary file absent)",
                                                  "junction markers from the file: 0 of 4 found by id",
                                                  "junction markers from the file: 0 of 4 found by id",
                                                  "dissolves: 2 of 2 are cuts (the OTIO, FCPXML and EDL exports; the read-back does not show transitions) "
                                                  "(the build route adds none)",
                                                  "OTIO export reads back with 2 transition changes and no clip or frame differences",
                                                  "FCPXML export reads back with 2 transition changes and no clip or frame differences",
                                                  "EDL export reads back with 2 transition changes and no clip or frame differences"]


@pytest.mark.parametrize("variant, category, expect", [
    ("loses dissolves", "Not preserved by Resolve", [("finding", "dissolves: 2 of 2 are cuts", r) for r in ("import-otio", "import-fcpxml", "import-edl")]),
    ("loses dissolves", "Failed", [("finding", "dissolves: 2 of 2 are cuts", "build+transitions"),
                                   ("finding", "AddTransition: called for 2 dissolves, returned None 2 times", "build+transitions"),
                                   ("call", "TimelineItem.AddTransition returned null", "build+transitions"),
                                   ("call", "TimelineItem.AddTransition returned null", "build+transitions")]),
    ("shifts frames by 1", "Failed", [("finding", "source in differs on 5 of 5 clips", r) for r in ("build", "build+transitions")]
     + [("finding", "source out differs on 5 of 5 clips", r) for r in ("build", "build+transitions")]),
    ("drops markers", "Failed", [("finding", "junction markers added by the probe: 0 of 4 found by id", r) for r in ("import-otio", "build")]),
    ("AddTransition not documented", "Not tested", [("finding", "TimelineItem.AddTransition: not documented on this install, so never called", ""),
                                                    ("finding", "TimelineItem.GetType: not documented on this install, so never called", "")]),
    ("Resolve 18.5", "Unsupported", [("call", "TimelineItem.GetSourceStartFrame raised RuntimeError", r) for r in ROUTES]),
])
def test_each_fake_variant_gets_a_sensible_classification(wsp, bundle, monkeypatch, variant, category, expect):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS[variant])
    got = _cats(rep)[category]
    for kind, start, route in expect:
        assert any(k == kind and w.startswith(start) and r == route for k, w, r in got), (kind, start, route, got[:6])


def test_a_real_exception_is_failed(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, Variant(raise_on=("MediaPool.ImportTimelineFromFile",)))
    failed = _cats(rep, "call")["Failed"]
    assert [(w, r) for k, w, r in failed if w.startswith("MediaPool.ImportTimelineFromFile")] == [
        ("MediaPool.ImportTimelineFromFile raised RuntimeError: fake failure in MediaPool.ImportTimelineFromFile", r) for r in ("import-otio", "import-fcpxml", "import-edl")]
    assert any(w.startswith("no timeline: MediaPool.ImportTimelineFromFile raised") for k, w, r in _cats(rep, "finding")["Failed"])


def test_an_aborted_run_does_not_blame_resolve_for_what_it_never_read(wsp, bundle, monkeypatch):
    """Review item 5: timelines made before the run stopped were never read back: 'clip count 0' there is not measured, not a loss."""
    rep, doc = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["the open project changes mid-run"])
    assert doc["status"] == "aborted" and doc["timelines"]["import-otio"]["readback"] is None
    o = _cats(rep, "finding")
    assert not any(w.startswith("clip count") for k, w, r in o["Not preserved by Resolve"])
    assert [r for k, w, r, ev in rep.outcomes["Not tested"] if w.startswith("clip count") and ev == "never read back (the run stopped)"] == [
        "import-otio", "import-fcpxml", "import-edl"]


def test_when_add_transition_fails_the_export_did_not_lose_dissolves(wsp, bundle, monkeypatch):
    """Review item 6: AddTransition called and failed: the build+transitions exports never had dissolves to lose."""
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["loses dissolves"])
    bt = [(c, ev) for c, items in rep.outcomes.items() for k, w, r, ev in items if r == "build+transitions" and "transition changes and no clip" in w]
    assert bt and all(c == "Not tested" and ev == "the dissolves never existed on this route" for c, ev in bt)


def test_an_fcpxml_bundle_at_the_export_path_is_listed_and_read(wsp, bundle, monkeypatch):
    """Review item 4: Resolve writing a directory (a bundle) at the FCPXML path is recorded, listed and, with an Info.fcpxml, re-read."""
    code, doc, world = run_probe(bundle, monkeypatch, Variant(fcpxml_bundle=True))
    x = doc["timelines"]["import-otio"]["exports"]["fcpxml"]
    assert x["ok"] is True and x["exists"] is False and x["is_dir"] is True and x["dir_entries"] == ["Info.fcpxml"]
    rep = build_report(wsp.ws, wsp.cfg, last_run(bundle))
    r = rep.routes["import-otio"]
    assert "FCPXML export: Timeline.Export wrote a directory at the path, not a file (an FCPXML bundle?): Info.fcpxml" in r.notes
    assert "FCPXML export reads back with no differences from the stage" in r.preserved and not any(x.startswith("FCPXML export") for x in r.lost)


# -- the re-review of 98143cf: failures that must stay visible -------------------------------------------------------------------
def test_a_real_clip_whose_reads_fail_is_not_taken_for_a_transition(wsp, bundle, monkeypatch):
    """Review A: import-otio's clip 3 (65-114) returns None for its source and offset reads. It touches the cuts at 65 and 114 with its
    own edges, but its record range is an expected clip's: it stays a clip, its nulls are Failed and no clip count goes to Not preserved."""
    import dataclasses

    v = dataclasses.replace(VARIANTS["resolve 21.0.2 free (observed)"], null_clip=(".otio", 2))
    _, real = __import__("matchcut.resolve.probe", fromlist=["console_lines"]).console_lines(bundle.script)
    from .test_console import console

    doc, _ = console(bundle, monkeypatch, real, v)
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    clip3 = doc["timelines"]["import-otio"]["readback"]["video"][0][3]
    assert (clip3["start"], clip3["end"], clip3["source_start_frame"]) == (65, 114, None)
    r = rep.routes["import-otio"]
    assert r.transition_items == 2 and r.verdict == "not measured" and not any(x.startswith("clip count") for x in r.lost)
    failed = [(w, rt) for k, w, rt, ev in rep.outcomes["Failed"] if k == "call"]
    for call in ("TimelineItem.GetLeftOffset", "TimelineItem.GetRightOffset", "TimelineItem.GetSourceStartFrame", "TimelineItem.GetSourceEndFrame"):
        assert (f"{call} returned null", "import-otio") in failed, call
    assert not any(w.startswith("clip count") for k, w, rt, ev in rep.outcomes["Not preserved by Resolve"])
    # the other routes keep their transition-item reads Unsupported
    assert sum(1 for k, w, rt, ev in rep.outcomes["Unsupported"] if rt == "import-fcpxml") == 8


@pytest.mark.parametrize("files, ok, wording", [((), True, "wrote a directory with no Info.fcpxml"),
                                                (("junk.txt",), False, "returned False (a directory is at the path)")])
def test_a_directory_that_is_not_a_readable_bundle_stays_failed(wsp, bundle, monkeypatch, files, ok, wording):
    """Review B: only a readable bundle that Export reported as done is a note; anything else at the path stays Failed."""
    run_probe(bundle, monkeypatch, Variant(fcpxml_bundle=True, fcpxml_bundle_files=files, fcpxml_bundle_ok=ok))
    rep = build_report(wsp.ws, wsp.cfg, last_run(bundle))
    failed = [(w, r) for k, w, r, ev in rep.outcomes["Failed"] if k == "finding"]
    assert [r for w, r in failed if w == f"FCPXML export: Timeline.Export {wording}"] == ROUTES
    assert any(x.startswith("FCPXML export: Timeline.Export wrote a directory at the path, not a file") for x in rep.routes["build"].notes)


def test_a_read_back_that_raised_in_a_complete_run_is_never_read_back_not_stopped(wsp, bundle, monkeypatch):
    """Minor: the read-back of import-fcpxml raised (the run itself completed): its frame, dissolve and navigation findings are Not tested
    with that error as the reason, never "the run stopped"; its file markers are Not tested, "the read-back was not taken"."""
    import dataclasses

    v = dataclasses.replace(VARIANTS["resolve 21.0.2 free (observed)"], bad_item_list="import-fcpxml")
    code, doc, _ = run_probe(bundle, monkeypatch, v)
    assert doc["status"] == "complete" and doc["timelines"]["import-fcpxml"]["readback"] is None
    rep = build_report(wsp.ws, wsp.cfg, last_run(bundle))
    mine = {c: [(w, ev) for k, w, r, ev in items if r == "import-fcpxml" and k == "finding"] for c, items in rep.outcomes.items()}
    measured = [(w, ev) for w, ev in mine["Not tested"] if w.startswith(("clip count", "dissolves", "navigation"))]
    assert {w.split(":")[0].split(" ")[0] for w, ev in measured} >= {"clip", "dissolves", "navigation"}
    assert all(ev.startswith("never read back (the read-back raised TypeError") and "the run stopped" not in ev for w, ev in measured)
    assert not any(w.startswith(("clip count", "dissolves", "navigation")) for c in ("Failed", "Not preserved by Resolve") for w, ev in mine[c])


def test_otio_markers_without_a_read_back_are_not_tested(wsp, bundle, monkeypatch):
    """Minor: the OTIO route's read-back is missing (None): its file markers were not read, which is not the same as none."""
    import dataclasses

    v = dataclasses.replace(VARIANTS["resolve 21.0.2 free (observed)"], bad_item_list="import-otio")
    run_probe(bundle, monkeypatch, v)
    rep = build_report(wsp.ws, wsp.cfg, last_run(bundle))
    otio = [(c, k, ev) for c, items in rep.outcomes.items() for k, w, r, ev in items
            if r == "import-otio" and (w.startswith("junction markers from the file") or w.startswith("Timeline.GetMarkerByCustomData returned null"))]
    assert otio and all(c == "Not tested" and ev == "the read-back was not taken" for c, k, ev in otio)
    assert not any(r == "import-otio" for k, w, r, ev in rep.outcomes["Not preserved by Resolve"])
