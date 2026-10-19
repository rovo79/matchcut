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
    assert "| build+transitions | usable |" in text and f"- dissolves judged from {FROM_EXPORTS}" in text


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

    def route(name, nav, markers, verdict="usable"):
        r = RouteResult(name, verdict=verdict)
        r.navigation_ok, r.markers_by_id = nav, markers
        return r

    routes = {"import-otio": route("import-otio", False, "no"), "import-fcpxml": route("import-fcpxml", True, "no"),
              "import-edl": route("import-edl", True, "yes"), "build": route("build", True, "yes", "usable, dissolves lost"),
              "build+transitions": route("build+transitions", True, "untested", "not usable")}
    name, text = _recommend(routes)
    assert name == "import-edl" and "**import-edl**" in text  # the only usable route where both navigation and markers work
    routes["import-edl"].verdict = "not usable"
    name, text = _recommend(routes)
    assert name == "import-fcpxml"  # navigation lands there; no usable route finds markers by id
    assert "it needs markers re-added by id: none was found by id on it (they are on import-edl, build)" in text
    routes["import-fcpxml"].navigation_ok = False
    name, text = _recommend(routes)
    assert name == "import-otio" and "navigation by timecode did not land on the incoming clip every time" in text


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
    assert f"dissolves: {d} of {d} read back as transition items at the expected frames and length" in otio.preserved
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
    run_probe(bundle, monkeypatch, args=["--dry-run"])
    code, doc, _ = run_probe(bundle, monkeypatch)
    run = bundle.dir / doc["run_dir"]
    assert build_report(wsp.ws, wsp.cfg, bundle.dir).path == run / "probe_report.md"  # the latest real run, not the dry run
    assert build_report(wsp.ws, wsp.cfg, run).path == run / "probe_report.md"


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
