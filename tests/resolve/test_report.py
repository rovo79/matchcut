"""``matchcut.resolve report``: route verdicts and fidelity differences, from resolve_probe.json made against each fake variant."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from matchcut.resolve import ProbeError
from matchcut.resolve.report import build_report

from .conftest import run_probe
from .fake_resolve import VARIANTS, Variant

ROUTES = ["import-otio", "import-fcpxml", "import-edl", "build", "build+transitions"]


def ws_hashes(ws: Path) -> dict:
    return {str(p.relative_to(ws)): hashlib.sha256(p.read_bytes()).hexdigest() for p in ws.rglob("*") if p.is_file() and "resolve_probe" not in p.parts}


def probe_and_report(wsp, bundle, monkeypatch, variant):
    code, doc, world = run_probe(bundle, monkeypatch, variant)
    assert world.rejected == []
    before = ws_hashes(wsp.ws)
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir / "resolve_probe.json")
    assert ws_hashes(wsp.ws) == before  # the re-imports ran in a scratch copy, never in the workspace
    return rep, doc


def verdicts(rep):
    return {r: rep.routes[r].verdict for r in ROUTES}


def test_a_fake_that_keeps_everything(wsp, bundle, monkeypatch):
    rep, doc = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["keeps everything"])
    assert verdicts(rep) == {"import-otio": "usable", "import-fcpxml": "usable", "import-edl": "usable", "build": "usable, dissolves lost",
                             "build+transitions": "usable"}
    assert rep.recommended == "import-otio"
    otio = rep.routes["import-otio"]
    assert any(p.startswith("record start of every clip") for p in otio.preserved) and any(p.startswith("dissolves:") for p in otio.preserved)
    assert any(p.startswith("junction markers found by id") for p in otio.preserved)
    assert otio.lost == [] and rep.routes["build"].lost == ["dissolves: 2 of 2 are cuts in the OTIO export (the build route adds none)"]
    text = (bundle.dir / "probe_report.md").read_text()
    assert text == rep.text and "Recommended default route for M2c-1: **import-otio**" in text
    assert "not run against a live Resolve" in text and "matchcut fake of the documented Resolve API" in text
    assert "| build+transitions | usable |" in text


def test_a_fake_that_loses_dissolves(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["loses dissolves"])
    assert verdicts(rep) == {r: "usable, dissolves lost" for r in ROUTES}
    assert rep.recommended == "build"
    assert "dissolves: 2 of 2 are cuts in the OTIO export" in rep.routes["import-otio"].lost
    assert "AddTransition: called for 2 dissolves, returned None 2 times" in rep.routes["build+transitions"].lost
    assert "no route carried the dissolves" in rep.text


def test_a_fake_that_shifts_frames(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["shifts frames by 1"])
    assert verdicts(rep) == {r: "not usable" for r in ROUTES}
    assert rep.recommended is None and "None of the routes is usable on this install" in rep.text
    n = len(json.loads(bundle.expected.read_text())["clips"])
    lost = rep.routes["import-otio"].lost
    assert f"source in differs on {n} of {n} clips: +1 frame ({n})" in lost and f"source out differs on {n} of {n} clips: +1 frame ({n})" in lost
    assert any(x.startswith(f"OTIO export reads back with {n} trims") for x in lost)
    assert any(p.startswith("record start of every clip") for p in rep.routes["import-otio"].preserved)


def test_a_fake_that_drops_markers(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["drops markers"])
    assert rep.recommended == "import-otio" and rep.routes["import-otio"].verdict == "usable"
    n = len(json.loads(bundle.expected.read_text())["junctions"])
    assert f"junction markers from the file: 0 of {n} found by id" in rep.routes["import-otio"].lost
    assert f"junction markers added by the probe: 0 of {n} found by id (AddMarker returned True {n} times)" in rep.routes["build"].lost
    assert "Junction markers are not findable by id on any route." in rep.text


def test_add_transition_not_documented(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["AddTransition not documented"])
    bt = rep.routes["build+transitions"]
    assert bt.verdict == "usable, dissolves lost" and "AddTransition: not documented on this install: not called (2 dissolves)" in bt.lost
    assert rep.recommended == "import-otio"


def test_append_to_timeline_none(wsp, bundle, monkeypatch):
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["AppendToTimeline returns [None]"])
    n = len(json.loads(bundle.expected.read_text())["clips"])
    assert rep.routes["build"].verdict == "not usable" and f"clip count 0, expected {n}" in rep.routes["build"].lost
    assert f"AppendToTimeline returned [None] for {n} of {n} clips" in rep.routes["build"].lost
    assert rep.recommended == "import-otio" and "MediaPool.AppendToTimeline" in rep.text


@pytest.mark.parametrize("variant, status", [("scripting not connected", "not connected"), ("a user project is open", "refused")])
def test_a_probe_that_measured_nothing_says_so(wsp, bundle, monkeypatch, variant, status):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS[variant])
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir / "resolve_probe.json")
    assert rep.recommended is None and all(r.verdict == "not tried" for r in rep.routes.values())
    assert f"the probe stopped before any route: {status}" in rep.text and "not run against a live Resolve" in rep.text


def test_the_navigation_and_export_lines(wsp, bundle, monkeypatch):
    rep, doc = probe_and_report(wsp, bundle, monkeypatch, VARIANTS["keeps everything"])
    k = min(5, len(json.loads(bundle.expected.read_text())["junctions"]))
    assert f"navigation: playhead on the incoming clip for {k} of {k} junctions (start timecode + record frame)" in rep.routes["build"].preserved
    assert any(x.startswith("FCPXML export reads back with no differences") for x in rep.routes["import-otio"].preserved)


def test_the_report_names_a_changed_bundle_or_stage(wsp, bundle, monkeypatch):
    run_probe(bundle, monkeypatch)
    stage = wsp.ws / "final.json"
    stage.write_text(stage.read_text().replace('"name"', '"name" ', 1))
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir / "resolve_probe.json")
    assert "the stage file final.json changed since the bundle was made" in rep.text


def test_bad_input_is_one_line(wsp, tmp_path):
    with pytest.raises(ProbeError) as err:
        build_report(wsp.ws, wsp.cfg, tmp_path / "nope.json")
    assert str(err.value).startswith("cannot read ") and "\n" not in str(err.value)
    bad = tmp_path / "resolve_probe.json"
    bad.write_text(json.dumps({"schema": "something else"}))
    with pytest.raises(ProbeError) as err:
        build_report(wsp.ws, wsp.cfg, bad)
    assert "not a matchcut resolve_probe.json" in str(err.value)
    bad.write_text(json.dumps({"schema": "matchcut.resolve_probe/1"}))
    with pytest.raises(ProbeError) as err:
        build_report(wsp.ws, wsp.cfg, bad)
    assert "no expected.json next to" in str(err.value)


def test_frames_that_could_not_be_read_are_not_measured_not_unusable(wsp, bundle, monkeypatch):
    v = Variant(raise_on=("TimelineItem.GetSourceStartFrame",))
    rep, _ = probe_and_report(wsp, bundle, monkeypatch, v)
    assert {r.verdict for r in rep.routes.values()} == {"not measured"} and rep.recommended is None
    n = len(json.loads(bundle.expected.read_text())["clips"])
    assert f"source in: not read for {n} of {n} clips" in rep.routes["import-otio"].notes
    assert "TimelineItem.GetSourceStartFrame" in rep.text  # the failed reads are listed
    assert "No route could be judged: the frames of import-otio, import-fcpxml, import-edl, build, build+transitions could not be read back" in rep.text
