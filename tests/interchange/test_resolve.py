"""The Resolve script generator and ``export --resolve-script``.

The script is tested against a FAKE of the documented scripting API (the Resolve 21.0.4 README): the fake implements only the
documented methods, so a call outside the documented set fails.  No test talks to a real Resolve and matchcut never runs the script.
"""
from __future__ import annotations

import ast
import json
import runpy
import sys
import types
from fractions import Fraction

import pytest

from matchcut.interchange import InterchangeError, resolve
from matchcut.interchange.common import junction_ids, layout, truncate
from matchcut.interchange.edl import _tc
from matchcut.pipeline import cli

from .conftest import exported

# every method name the generated script may call: scripting calls documented in the Resolve 21.0.4 README, plus Python builtins
DOCUMENTED = {
    "scriptapp", "GetProjectManager", "GetCurrentProject", "GetMediaPool", "ImportTimelineFromFile", "SetCurrentTimeline", "OpenPage",
    "AddMarker", "SetCurrentTimecode", "GetTimelineCount", "GetTimelineByIndex", "GetName",
}
MARKER_COLORS = {"Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream"}


# -- a fake of the documented scripting API -----------------------------------------------------------
class FakeTimeline:
    def __init__(self, log: list, name: str):
        self.log, self.name = log, name

    def GetName(self):
        self.log.append(("Timeline.GetName",))
        return self.name

    def AddMarker(self, frameId, color, name, note, duration, customData):
        assert color in MARKER_COLORS and isinstance(frameId, int) and isinstance(customData, str) and isinstance(duration, int)
        self.log.append(("AddMarker", frameId, color, name, note, duration, customData))
        return True

    def SetCurrentTimecode(self, timecode):
        self.log.append(("SetCurrentTimecode", timecode))
        return True


class FakeMediaPool:
    def __init__(self, log: list, project):
        self.log, self.project = log, project

    def ImportTimelineFromFile(self, filePath, importOptions=None):
        assert set(importOptions or {}) <= {"timelineName", "importSourceClips", "sourceClipsPath", "sourceClipsFolders"}
        self.log.append(("ImportTimelineFromFile", filePath, dict(importOptions)))
        tl = FakeTimeline(self.log, importOptions["timelineName"])
        self.project.timelines.append(tl)
        return tl


class FakeProject:
    def __init__(self, log: list):
        self.log, self.timelines, self.current = log, [], None

    def GetMediaPool(self):
        self.log.append(("GetMediaPool",))
        return FakeMediaPool(self.log, self)

    def SetCurrentTimeline(self, timeline):
        self.log.append(("SetCurrentTimeline", timeline.name))
        return True

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, idx):
        return self.timelines[idx - 1]


class FakeProjectManager:
    def __init__(self, log: list):
        self.log = log

    def GetCurrentProject(self):
        self.log.append(("GetCurrentProject",))
        return FakeProject(self.log)


class FakeResolve:
    def __init__(self, log: list):
        self.log = log

    def GetProjectManager(self):
        self.log.append(("GetProjectManager",))
        return FakeProjectManager(self.log)

    def OpenPage(self, pageName):
        assert pageName in {"media", "photo", "cut", "edit", "fusion", "color", "fairlight", "deliver"}
        self.log.append(("OpenPage", pageName))
        return True


def run_against_fake(script, monkeypatch, scripting=None):
    """Run ``script`` (a path) with a fake DaVinciResolveScript module in place; return the call log."""
    log: list = []
    fake = types.ModuleType("DaVinciResolveScript")
    fake.scriptapp = scripting or (lambda name: (log.append(("scriptapp", name)) or FakeResolve(log)))
    monkeypatch.setitem(sys.modules, "DaVinciResolveScript", fake)
    runpy.run_path(str(script), run_name="__main__")
    return [c for c in log if c[0] != "Timeline.GetName"]


def final_inputs(world):
    """(junction frames, notes, ids, snapped final) as the exporters lay the final cut out."""
    snapped, _ = exported(world, "final")
    rows = layout(snapped, world.src.rate)
    js = world.jb["final"]
    ids = junction_ids(snapped)
    frames = {ids[j.index]: rows[j.index + 1].cut for j in js}
    notes = {ids[j.index]: (f"J{j.index + 1} s{j.out_shot}->s{j.in_shot}", truncate(j.why())) for j in js}
    return frames, notes, [ids[j.index] for j in js], snapped


# -- the generator -------------------------------------------------------------------------------------
def test_resolve_script_against_fake_documented_api(world, tmp_path, monkeypatch):
    frames, notes, ids, snapped = final_inputs(world)
    nav = ids[-1]
    text = resolve.resolve_script("/tmp/final.otio", "matchcut final", frames, nav, rate=world.src.rate, notes=notes)
    forbidden = ("AddTransition", "SetFades", "SetClipColor", "SetMetadata", "Export(")
    assert not any(f in text for f in forbidden)
    script = tmp_path / "open_in_resolve.py"
    script.write_text(text)
    compile(text, str(script), "exec")
    calls = run_against_fake(script, monkeypatch)
    assert [c[0] for c in calls[:7]] == ["scriptapp", "GetProjectManager", "GetCurrentProject", "GetMediaPool", "ImportTimelineFromFile",
                                         "SetCurrentTimeline", "OpenPage"]
    assert calls[0] == ("scriptapp", "Resolve") and calls[4][1:] == ("/tmp/final.otio", {"timelineName": "matchcut final", "importSourceClips": True})
    assert calls[5] == ("SetCurrentTimeline", "matchcut final") and calls[6] == ("OpenPage", "edit")
    markers = [c for c in calls if c[0] == "AddMarker"]
    assert [m[6] for m in markers] == ids and [m[1] for m in markers] == [frames[j] for j in ids]
    assert [m[3] for m in markers] == [notes[j][0] for j in ids] and [m[4] for m in markers] == [notes[j][1] for j in ids]
    assert calls[-1] == ("SetCurrentTimecode", _tc(frames[nav], world.src.rate))
    assert [c[0] for c in calls[7:]] == ["AddMarker"] * len(ids) + ["SetCurrentTimecode"]  # the playhead is set last, after the markers


def test_fake_resolve_rejects_undocumented_calls():
    log: list = []
    r = FakeResolve(log)
    with pytest.raises(AttributeError):
        r.GetProjectManager().GetCurrentProject().GetMediaPool().AddTransition  # noqa: B018
    with pytest.raises(AttributeError):
        r.StartRendering  # noqa: B018


def test_the_script_calls_only_documented_methods(world):
    frames, notes, ids, _ = final_inputs(world)
    tree = ast.parse(resolve.resolve_script("/x/final.otio", "t", frames, ids[0], rate=world.src.rate, notes=notes))
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    resolve_calls = {c for c in calls if c[:1].isupper() or c == "scriptapp"}
    assert resolve_calls and resolve_calls <= DOCUMENTED, resolve_calls - DOCUMENTED


def test_the_script_states_what_it_needs_and_assumes(world):
    frames, notes, ids, _ = final_inputs(world)
    text = resolve.resolve_script("/x/final.otio", "t", frames, ids[0], rate=world.src.rate)
    head = text.split("import os")[0]
    for needle in ("Resolve Studio", "External scripting", "never runs", "Resolve 21.0.4", "UNVERIFIED", "start timecode", "00:00:00:00"):
        assert needle in head, needle
    assert "lossless" not in text.lower()


def test_the_playhead_timecode_follows_the_start_timecode():
    frames = {"s1-s2": 100, "s2-s3": 250}
    rate = Fraction(25)
    assert resolve.resolve_script("/x/a.otio", "t", frames, "s2-s3", rate=rate).count("NAVIGATE_TC = '00:00:10:00'") == 1
    assert "NAVIGATE_TC = '01:00:10:00'" in resolve.resolve_script("/x/a.otio", "t", frames, "s2-s3", start_tc="01:00:00:00", rate=rate)
    assert "NAVIGATE_TC = '00:00:04:00'" in resolve.resolve_script("/x/a.otio", "t", frames, "s1-s2", rate=rate)
    # marker frame ids stay record frames: the playhead alone follows the start timecode
    assert "'s2-s3': 250" in resolve.resolve_script("/x/a.otio", "t", frames, "s2-s3", start_tc="01:00:00:00", rate=rate)
    # drop-frame: 01:00:00;00 + 1800 frames at 29.97 is 01:01:00;02
    df = resolve.resolve_script("/x/a.otio", "t", {"a-b": 1800}, "a-b", start_tc="01:00:00;00", rate=Fraction(30000, 1001))
    assert "NAVIGATE_TC = '01:01:00;02'" in df


def test_a_cut_without_junctions_parks_the_playhead_at_the_start():
    text = resolve.resolve_script("/x/a.otio", "t", {}, "", start_tc="01:00:00:00", rate=Fraction(24))
    assert "NAVIGATE_TC = '01:00:00:00'" in text and "JUNCTIONS = {}" in text
    compile(text, "s", "exec")


def test_odd_names_stay_one_literal_each(tmp_path, monkeypatch):
    name = 'Tom\'s "cut"\n\\ end'
    path = "/media/it's here/éè final.otio"
    note = ("shot 3 -> 4", 'why: "x"\nsecond line \\')
    text = resolve.resolve_script(path, name, {"s3-s4": 12}, "s3-s4", rate=Fraction(24), notes={"s3-s4": note})
    script = tmp_path / "odd.py"
    script.write_text(text)
    calls = run_against_fake(script, monkeypatch)
    assert calls[4][1:] == (path, {"timelineName": name, "importSourceClips": True})
    assert [c for c in calls if c[0] == "AddMarker"][0][3:5] == note


@pytest.mark.parametrize("kwargs, message", [
    (dict(navigate_to="nope"), "cannot park the playhead at junction nope: the cut has junctions s1-s2"),
    (dict(start_tc="1:00"), "invalid start timecode 1:00: use HH:MM:SS:FF"),
    (dict(start_tc="00:00:00:25"), "invalid start timecode 00:00:00:25: use HH:MM:SS:FF"),
    (dict(junction_frames={"s1-s2": -1}), "junction s1-s2 sits at record frame -1"),
    (dict(timeline_file=""), "no timeline file to import"),
    (dict(timeline_name=""), "no timeline name"),
])
def test_bad_input_is_a_one_line_error(kwargs, message):
    args = dict(timeline_file="/x/a.otio", timeline_name="t", junction_frames={"s1-s2": 10}, navigate_to="s1-s2", start_tc="00:00:00:00", rate=Fraction(25))
    args.update(kwargs)
    with pytest.raises(InterchangeError) as err:
        resolve.resolve_script(**args)
    assert str(err.value) == message and "\n" not in str(err.value)


def test_the_generator_is_stdlib_only_and_never_imports_the_resolve_module():
    import subprocess
    code = ("import sys; import matchcut.interchange.resolve as r; "
            "bad = [m for m in sys.modules if m == 'DaVinciResolveScript' or m == 'textual' or m.startswith(('textual.', 'matchcut.tui', 'matchcut.review'))]; "
            "r.resolve_script('/x/a.otio', 't', {'a-b': 1}, 'a-b'); sys.exit(1 if bad or 'DaVinciResolveScript' in sys.modules else 0)")
    from pathlib import Path
    assert subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[2])).returncode == 0


# -- export --resolve-script ----------------------------------------------------------------------------
def run(argv, capsys):
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def test_export_resolve_script_writes_the_script_next_to_the_exports(wsx, capsys, monkeypatch):
    code, out, err = run(["export", wsx.ws, "--resolve-script"], capsys)
    assert code == 0 and err == ""
    exports = wsx.ws / "exports"
    script = exports / "open_in_resolve.py"
    assert script.is_file() and f"  {script}" in out.splitlines() and out.splitlines()[0] == f"exported 9 files to {exports}"
    assert "DaVinciResolveScript" not in sys.modules  # matchcut writes the script, it never runs it
    calls = run_against_fake(script, monkeypatch)
    imp = calls[4]
    assert imp[1] == str((exports / "final.otio").resolve()) and imp[2] == {"timelineName": "demo - final", "importSourceClips": True}
    final = wsx.world.by_stage["final"]
    markers = [c for c in calls if c[0] == "AddMarker"]
    assert [m[6] for m in markers] == junction_ids(final) and len(markers) == len(final.clips) - 1
    # the marker frames are the cut frames the OTIO file carries
    doc = json.loads((exports / "final.otio").read_text())
    otio_markers = doc["tracks"]["children"][0]["markers"]
    assert [m[1] for m in markers] == [m["marked_range"]["start_time"]["value"] for m in otio_markers]
    assert [m[3] for m in markers] == [m["name"] for m in otio_markers]
    assert calls[-1][0] == "SetCurrentTimecode"


def test_the_playhead_goes_to_the_first_junction_with_a_transition(wsx, capsys, monkeypatch):
    run(["export", wsx.ws, "--stage", "final", "--format", "otio", "--resolve-script"], capsys)
    snapped, _ = exported(wsx.world, "final")
    rows = layout(snapped, wsx.world.src.rate)
    first = next(r for r in rows[1:] if r.tr)
    calls = run_against_fake(wsx.ws / "exports" / "open_in_resolve.py", monkeypatch)
    assert calls[-1] == ("SetCurrentTimecode", _tc(first.cut, wsx.world.src.rate))


def test_resolve_script_picks_the_final_stage_else_the_last_one_exported(wsx, capsys, monkeypatch):
    run(["export", wsx.ws, "--stage", "roughcut", "--format", "otio", "--resolve-script"], capsys)
    calls = run_against_fake(wsx.ws / "exports" / "open_in_resolve.py", monkeypatch)
    assert calls[4][1].endswith("roughcut.otio") and calls[4][2]["timelineName"] == "demo - roughcut"
    out = wsx.tmp / "elsewhere"
    run(["export", wsx.ws, "--out", out, "--resolve-script"], capsys)
    calls = run_against_fake(out / "open_in_resolve.py", monkeypatch)
    assert calls[4][1] == str((out / "final.otio").resolve())
    assert not (wsx.ws / "exports" / "open_in_resolve.py").read_text() == (out / "open_in_resolve.py").read_text()


def test_resolve_script_without_the_otio_format_is_a_one_line_error(wsx, capsys):
    code, out, err = run(["export", wsx.ws, "--format", "edl", "--resolve-script"], capsys)
    assert (code, out) == (1, "") and err == "--resolve-script needs the otio format: Resolve imports the OTIO file\n"
    assert not (wsx.ws / "exports" / "open_in_resolve.py").exists()


def test_export_without_the_flag_writes_no_script(wsx, capsys):
    run(["export", wsx.ws], capsys)
    assert not (wsx.ws / "exports" / "open_in_resolve.py").exists()


def test_the_script_names_the_start_timecode_the_exporters_write(wsx, capsys):
    """Every exporter starts the record side at zero (OTIO global_start_time, FCPXML tcStart, EDL 00:00:00:00): the script's default."""
    run(["export", wsx.ws, "--resolve-script"], capsys)
    exports = wsx.ws / "exports"
    assert json.loads((exports / "final.otio").read_text())["global_start_time"]["value"] == 0
    assert 'tcStart="0s"' in (exports / "matchcut.fcpxml").read_text()
    first = next(l for l in (exports / "final.edl").read_text().splitlines() if l.startswith("001 "))
    assert first.split()[-2] == "00:00:00:00"  # record in of the first event
    assert "START_TC = '00:00:00:00'" in (exports / "open_in_resolve.py").read_text()
