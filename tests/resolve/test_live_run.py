"""Regression: the first live probe run (DaVinci Resolve 21.0.2.4 free, macOS, from the Py3 console, 2026-10-29).

The fake variant "resolve 21.0.2 free (observed)" reproduces that run's read-back exactly (it is the same cut of the same demo footage:
5 clips, 2 dissolves, 462 frames of media). Before the fix, the report called every route unusable. The read-back proves the imports
frame-exact, with the dissolves as transition items: there was no GetType on that install, and the last clip's GetSourceEndFrame is the
media's last frame.
"""
from __future__ import annotations

import pytest

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
IMPORTS = ("import-otio", "import-fcpxml", "import-edl")
KEYS = ("start", "end", "duration", "source_start_frame", "source_end_frame")


@pytest.fixture
def live(wsp, bundle, monkeypatch):
    _, real = console_lines(bundle.script)
    doc, world = console(bundle, monkeypatch, real, VARIANTS["resolve 21.0.2 free (observed)"])
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    return doc, rep


def test_the_fake_reproduces_the_observed_read_back(live):
    doc, _ = live
    assert doc["status"] == "complete" and doc["scripting"]["mode"] == "in-app console"
    assert doc["resolve"] == {"product": "DaVinci Resolve", "version": [21, 0, 2, 4, ""], "version_string": "21.0.2.4"}
    assert doc["catalogue"]["files"][0]["version_line"] == "Last Updated: 26 May 2026" and doc["catalogue"]["gated"] == {
        "TimelineItem.AddTransition": False, "TimelineItem.GetType": False}
    for route in IMPORTS:
        rb = doc["timelines"][route]["readback"]
        assert rb["start_frame"] == 0 and rb["start_timecode"] == "00:00:00:00"
        got = [{k: v[k] for k in KEYS} for v in rb["video"][0]]
        assert got == [{k: o[k] for k in KEYS} for o in OBSERVED], route
        assert [rb["video"][0][k]["name"] for k in (2, 4)] == ["Cross Dissolve", "Cross Dissolve"]
        assert all(v["left_offset"] is None and v["right_offset"] is None for v in rb["video"][0])
    for route in ("build", "build+transitions"):
        rb = doc["timelines"][route]["readback"]
        clips = [o for o in OBSERVED if o["source_start_frame"] is not None]
        assert rb["start_frame"] == 90000 and rb["start_timecode"] == "01:00:00:00"
        assert [{k: v[k] for k in KEYS} for v in rb["video"][0]] == [dict({k: o[k] for k in KEYS}, start=o["start"] + 90000, end=o["end"] + 90000)
                                                                      for o in clips]
    assert all(not e["exports"]["fcpxml"]["ok"] and e["exports"]["otio"]["ok"] and e["exports"]["edl"]["ok"] for e in doc["timelines"].values())
    assert doc["mcp"]["observed"] == "file absent"


def test_the_report_reads_the_live_run_correctly(live):
    _, rep = live
    for route in IMPORTS:
        r = rep.routes[route]
        assert (r.verdict, r.frames, r.dissolves, r.navigation_ok, r.transition_items) == ("usable", "exact", "kept", True, 2), route
        assert "dissolves read back: 2 of 2, exact length and centred (transition items)" in r.preserved
        assert any(n.startswith("2 transition items read back, counted apart from the clips; identified by: no source frames, spans the cut at 65; "
                                "name 'Cross Dissolve'; no source frames, spans the cut at 114") for n in r.notes)
        assert any(n.startswith("GetSourceEndFrame reports the media's last frame for a clip that ends at the media's end (exclusive elsewhere): clip 5")
                   for n in r.notes)
        assert not any(x.startswith(("source out differs", "clip count")) for x in r.lost)
        assert "FCPXML export: Timeline.Export failed" in r.lost  # a lost item, not a route failure
        assert r.navigation.startswith("4/4")
    otio = rep.routes["import-otio"]
    assert otio.own_markers == "yes" and otio.file_markers == "no"
    for route in ("build", "build+transitions"):
        r = rep.routes[route]
        assert (r.verdict, r.frames, r.dissolves) == ("usable, dissolves lost", "exact", "lost"), route
    assert "dissolves: 2 of 2 have no transition item in the read-back (the build route adds none)" in rep.routes["build"].lost
    assert "AddTransition: not documented on this install: not called (2 dissolves)" in rep.routes["build+transitions"].lost
    assert rep.recommended == "import-otio"
    assert ("Recommended default route for M2c-1: **import-otio** (frames exact and dissolves as expected, as measured); junction markers must be "
            "added by id after import: the markers the file carries are not found by id, the probe's own are.") in rep.text
    assert "None of the routes is usable" not in rep.text
    assert "(free edition)" in rep.text and "from Resolve's own console" in rep.text
