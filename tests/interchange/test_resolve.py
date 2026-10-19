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
from matchcut.interchange.common import junction_ids, layout, parse_timecode, truncate
from matchcut.interchange.edl import _tc
from matchcut.pipeline import cli

from .conftest import exported

# every method name the generated script may call: scripting calls documented in the Resolve 21.0.4 README, plus Python builtins
DOCUMENTED = {
    "scriptapp", "GetProjectManager", "GetCurrentProject", "GetMediaPool", "ImportTimelineFromFile", "SetCurrentTimeline", "OpenPage",
    "AddMarker", "UpdateMarkerCustomData", "SetCurrentTimecode", "GetTimelineCount", "GetTimelineByIndex", "GetName",
}
MARKER_COLORS = {"Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream"}


# -- a fake of the documented scripting API -----------------------------------------------------------
class Behaviour:
    """What the fake Resolve does: the happy path by default, each field switches one thing to its failure."""

    def __init__(self, **kw):
        self.no_resolve = False  # scriptapp returns None
        self.no_project = False  # GetCurrentProject returns None
        self.import_none = False  # ImportTimelineFromFile returns None (and creates nothing)
        self.existing = []  # names of timelines the project already holds
        self.markers_on = []  # frames that already carry a marker (the OTIO import brought them): AddMarker is False there
        self.update_fails = False  # UpdateMarkerCustomData returns False
        self.timecode_fails = False  # SetCurrentTimecode returns False
        self.set_timeline_fails = False
        self.page_fails = False
        self.__dict__.update(kw)


class FakeTimeline:
    def __init__(self, log: list, name: str, how: Behaviour):
        self.log, self.name, self.how = log, name, how
        self.markers = {f: None for f in how.markers_on}  # frame -> customData

    def GetName(self):
        self.log.append(("Timeline.GetName",))
        return self.name

    def AddMarker(self, frameId, color, name, note, duration, customData):
        assert color in MARKER_COLORS and isinstance(frameId, int) and isinstance(customData, str) and isinstance(duration, int)
        if frameId in self.markers:
            self.log.append(("AddMarker", frameId, color, name, note, duration, customData, False))
            return False
        self.markers[frameId] = customData
        self.log.append(("AddMarker", frameId, color, name, note, duration, customData))
        return True

    def UpdateMarkerCustomData(self, frameId, customData):
        assert isinstance(frameId, int) and isinstance(customData, str)
        self.log.append(("UpdateMarkerCustomData", frameId, customData))
        if self.how.update_fails or frameId not in self.markers:
            return False
        self.markers[frameId] = customData
        return True

    def SetCurrentTimecode(self, timecode):
        self.log.append(("SetCurrentTimecode", timecode))
        return not self.how.timecode_fails


class FakeMediaPool:
    def __init__(self, log: list, project):
        self.log, self.project = log, project

    def ImportTimelineFromFile(self, filePath, importOptions=None):
        assert set(importOptions or {}) <= {"timelineName", "importSourceClips", "sourceClipsPath", "sourceClipsFolders"}
        self.log.append(("ImportTimelineFromFile", filePath, dict(importOptions)))
        if self.project.how.import_none:
            return None
        tl = FakeTimeline(self.log, importOptions["timelineName"], self.project.how)
        self.project.timelines.append(tl)
        return tl


class FakeProject:
    def __init__(self, log: list, how: Behaviour):
        self.log, self.how, self.current = log, how, None
        self.timelines = [FakeTimeline(log, n, how) for n in how.existing]

    def GetMediaPool(self):
        self.log.append(("GetMediaPool",))
        return FakeMediaPool(self.log, self)

    def SetCurrentTimeline(self, timeline):
        self.log.append(("SetCurrentTimeline", timeline.name))
        return not self.how.set_timeline_fails

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, idx):
        return self.timelines[idx - 1]


class FakeProjectManager:
    def __init__(self, log: list, how: Behaviour):
        self.log, self.how = log, how

    def GetCurrentProject(self):
        self.log.append(("GetCurrentProject",))
        return None if self.how.no_project else FakeProject(self.log, self.how)


class FakeResolve:
    def __init__(self, log: list, how: Behaviour = None):
        self.log, self.how = log, how or Behaviour()

    def GetProjectManager(self):
        self.log.append(("GetProjectManager",))
        return FakeProjectManager(self.log, self.how)

    def OpenPage(self, pageName):
        assert pageName in {"media", "photo", "cut", "edit", "fusion", "color", "fairlight", "deliver"}
        self.log.append(("OpenPage", pageName))
        return not self.how.page_fails


def run_script(script, monkeypatch, how: Behaviour = None):
    """Run ``script`` (a path) with a fake DaVinciResolveScript module in place; return (SystemExit argument or None, the call log)."""
    log: list = []
    how = how or Behaviour()
    fake = types.ModuleType("DaVinciResolveScript")
    fake.scriptapp = lambda name: (log.append(("scriptapp", name)) or (None if how.no_resolve else FakeResolve(log, how)))
    monkeypatch.setitem(sys.modules, "DaVinciResolveScript", fake)
    code = None
    try:
        runpy.run_path(str(script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
    return code, [c for c in log if c[0] != "Timeline.GetName"]


def run_against_fake(script, monkeypatch, how: Behaviour = None):
    """Run a script that is expected to succeed; return the call log."""
    code, calls = run_script(script, monkeypatch, how)
    assert code is None, code
    return calls


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


def playhead(text: str, tmp_path, monkeypatch) -> str:
    """The timecode the generated script gives SetCurrentTimecode when it runs against the fake Resolve."""
    script = tmp_path / "playhead.py"
    script.write_text(text)
    calls = run_against_fake(script, monkeypatch)
    (tc,) = [c[1] for c in calls if c[0] == "SetCurrentTimecode"]
    return tc


def test_the_playhead_timecode_follows_the_start_timecode(tmp_path, monkeypatch):
    frames = {"s1-s2": 100, "s2-s3": 250}
    rate = Fraction(25)
    gen = lambda nav, **kw: resolve.resolve_script("/x/a.otio", "t", frames, nav, rate=rate, **kw)  # noqa: E731
    assert playhead(gen("s2-s3"), tmp_path, monkeypatch) == "00:00:10:00"
    assert playhead(gen("s2-s3", start_tc="01:00:00:00"), tmp_path, monkeypatch) == "01:00:10:00"
    assert playhead(gen("s1-s2"), tmp_path, monkeypatch) == "00:00:04:00"
    assert "NAVIGATE_TC" not in gen("s1-s2")  # worked out when the script runs, not baked in
    # marker frame ids stay record frames: the playhead alone follows the start timecode
    assert "'s2-s3': 250" in gen("s2-s3", start_tc="01:00:00:00")
    # drop-frame: 01:00:00;00 + 1800 frames at 29.97 is 01:01:00;02
    df = resolve.resolve_script("/x/a.otio", "t", {"a-b": 1800}, "a-b", start_tc="01:00:00;00", rate=Fraction(30000, 1001))
    assert playhead(df, tmp_path, monkeypatch) == "01:01:00;02"
    # the formatter in the script is the exporters': same text for a spread of frames at every kind of rate
    for r, start in ((Fraction(24000, 1001), "00:00:00:00"), (Fraction(30000, 1001), "01:00:00;00"), (Fraction(60000, 1001), "00:00:00;00"),
                     (Fraction(24), "10:00:00:00"), (Fraction(25), "00:59:59:24")):
        for frame in (0, 1, 1799, 1800, 17981, 17982, 107892, 215785):
            text = resolve.resolve_script("/x/a.otio", "t", {"j": frame}, "j", start_tc=start, rate=r)
            base, drop = parse_timecode(start, r)
            assert playhead(text, tmp_path, monkeypatch) == _tc(base + frame, r, drop), (r, start, frame)


def test_editing_start_tc_in_the_script_moves_the_playhead(world, tmp_path, monkeypatch):
    """The playhead hint says "edit START_TC": it used to change nothing, because NAVIGATE_TC was written into the script (final review T5-a)."""
    frames, notes, ids, _ = final_inputs(world)
    text = resolve.resolve_script("/x/final.otio", "t", frames, ids[1], rate=world.src.rate, notes=notes)
    assert "START_TC = '00:00:00:00'" in text
    base = playhead(text, tmp_path, monkeypatch)
    assert base == _tc(frames[ids[1]], world.src.rate)
    moved = playhead(text.replace("START_TC = '00:00:00:00'", "START_TC = '01:00:00:00'"), tmp_path, monkeypatch)
    assert moved == _tc(frames[ids[1]] + 3600 * 25, world.src.rate) and moved.startswith("01:")
    # a START_TC that is no timecode is one line and no call to Resolve for the playhead
    bad = tmp_path / "bad.py"
    bad.write_text(text.replace("START_TC = '00:00:00:00'", "START_TC = 'one o clock'"))
    code, calls = run_script(bad, monkeypatch)
    assert code == "START_TC 'one o clock' is not a timecode: use HH:MM:SS:FF" and not any(c[0] == "SetCurrentTimecode" for c in calls)


def test_a_cut_without_junctions_parks_the_playhead_at_the_start(tmp_path, monkeypatch):
    text = resolve.resolve_script("/x/a.otio", "t", {}, "", start_tc="01:00:00:00", rate=Fraction(24))
    assert "JUNCTIONS = {}" in text
    compile(text, "s", "exec")
    assert playhead(text, tmp_path, monkeypatch) == "01:00:00:00"


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


def write_script(tmp_path, world, **kw):
    frames, notes, ids, _ = final_inputs(world)
    path = tmp_path / "s.py"
    path.write_text(resolve.resolve_script("/x/final.otio", "matchcut final", frames, ids[1], rate=world.src.rate, notes=notes, **kw))
    return path, frames, ids


def test_the_happy_path_prints_what_it_did_and_exits_cleanly(world, tmp_path, monkeypatch, capsys):
    script, frames, ids = write_script(tmp_path, world)
    code, calls = run_script(script, monkeypatch)
    out = capsys.readouterr().out.splitlines()
    assert code is None and f"{len(ids)} of {len(ids)} junctions carry their id as marker customData" in out
    assert out[-1] == f"playhead at {_tc(frames[ids[1]], world.src.rate)} (junction {ids[1]})"
    assert not any(c[0] == "UpdateMarkerCustomData" for c in calls)


def test_no_resolve_or_no_project_exits_with_one_line(world, tmp_path, monkeypatch):
    script, _, _ = write_script(tmp_path, world)
    code, calls = run_script(script, monkeypatch, Behaviour(no_resolve=True))
    assert code == "cannot reach Resolve (is it running, Studio, external scripting = Local?)" and [c[0] for c in calls] == ["scriptapp"]
    code, calls = run_script(script, monkeypatch, Behaviour(no_project=True))
    assert code == "no project is open in Resolve" and calls[-1] == ("GetCurrentProject",)


def test_a_failed_import_with_no_timeline_of_that_name_exits(world, tmp_path, monkeypatch):
    script, _, _ = write_script(tmp_path, world)
    code, calls = run_script(script, monkeypatch, Behaviour(import_none=True, existing=["something else"]))
    assert code == "timeline import failed: /x/final.otio" and not any(c[0] in ("AddMarker", "SetCurrentTimeline") for c in calls)


def test_a_failed_import_falls_back_to_the_existing_timeline_and_says_so(world, tmp_path, monkeypatch, capsys):
    script, frames, ids = write_script(tmp_path, world)
    code, calls = run_script(script, monkeypatch, Behaviour(import_none=True, existing=["matchcut final"]))
    out = capsys.readouterr().out.splitlines()
    assert code is None and calls[5] == ("SetCurrentTimeline", "matchcut final")
    assert out[0] == "timeline matchcut final already existed: markers added to it; delete it and re-run to import the new export"
    assert [c[6] for c in calls if c[0] == "AddMarker"] == ids
    # a successful import never prints that line
    run_script(script, monkeypatch)
    assert "already existed" not in capsys.readouterr().out


def test_markers_that_came_with_the_import_get_the_junction_id_through_update_marker_custom_data(world, tmp_path, monkeypatch, capsys):
    script, frames, ids = write_script(tmp_path, world)
    code, calls = run_script(script, monkeypatch, Behaviour(markers_on=list(frames.values())))
    assert code is None
    assert [(c[1], c[2]) for c in calls if c[0] == "UpdateMarkerCustomData"] == [(frames[j], j) for j in ids]
    assert f"{len(ids)} of {len(ids)} junctions carry their id as marker customData" in capsys.readouterr().out
    # only the frames that were taken are updated
    taken = [frames[ids[0]]]
    code, calls = run_script(script, monkeypatch, Behaviour(markers_on=taken))
    assert code is None and [c[1] for c in calls if c[0] == "UpdateMarkerCustomData"] == taken


def test_a_junction_that_ends_up_without_its_id_is_counted_and_the_exit_is_non_zero(world, tmp_path, monkeypatch, capsys):
    script, frames, ids = write_script(tmp_path, world)
    code, calls = run_script(script, monkeypatch, Behaviour(markers_on=list(frames.values()), update_fails=True))
    out = capsys.readouterr().out
    assert code.startswith(f"{len(ids)} junctions have no marker with their id")
    assert f"0 of {len(ids)} junctions carry their id as marker customData; without it: {', '.join(ids)}" in out
    # one of them only
    code, calls = run_script(script, monkeypatch, Behaviour(markers_on=[frames[ids[0]]], update_fails=True))
    assert code == "1 junctions have no marker with their id"
    assert f"{len(ids) - 1} of {len(ids)} junctions carry their id as marker customData; without it: {ids[0]}" in capsys.readouterr().out


def test_a_failed_playhead_prints_the_start_timecode_hint_and_exits(world, tmp_path, monkeypatch, capsys):
    script, frames, ids = write_script(tmp_path, world, start_tc="01:00:00:00")
    code, calls = run_script(script, monkeypatch, Behaviour(timecode_fails=True))
    out = capsys.readouterr().out
    assert code == "playhead not set" and "playhead at" not in out
    assert "could not set the playhead to " in out and "edit START_TC at the top of this script to the timeline's start timecode and run it again" in out
    assert "START_TC 01:00:00:00 + the junction's record frame" in out
    assert calls[-1][0] == "SetCurrentTimecode"


def test_the_timeline_that_cannot_be_made_current_exits_and_a_failed_page_only_warns(world, tmp_path, monkeypatch, capsys):
    script, _, _ = write_script(tmp_path, world)
    code, calls = run_script(script, monkeypatch, Behaviour(set_timeline_fails=True))
    assert code == "cannot make the timeline matchcut final current" and not any(c[0] == "AddMarker" for c in calls)
    code, calls = run_script(script, monkeypatch, Behaviour(page_fails=True))
    assert code is None and "warning: could not open the Edit page" in capsys.readouterr().out


def test_the_script_no_longer_calls_the_markers_harmless():
    text = resolve.resolve_script("/x/a.otio", "t", {"a-b": 1}, "a-b")
    assert "harmless" not in text and "UpdateMarkerCustomData" in text


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
