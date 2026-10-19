"""The generated resolve_probe.py, run end to end against the fake of the documented Resolve API (``fake_resolve``).

The fake is matchcut's model of the documentation, not evidence about Resolve: these tests prove what the script asks and records, and
that it is safe, not what a real Resolve answers.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

from matchcut.resolve import _probe_script as template

from .conftest import run_probe, tree
from .fake_resolve import DOCUMENTED, GATED, VARIANTS, Variant, World

ROUTES = ["import-otio", "import-fcpxml", "import-edl", "build", "build+transitions"]
TOP_KEYS = {"schema", "probe_version", "created_utc", "status", "dry_run", "bundle", "resolve", "platform", "scripting", "catalogue", "mcp",
            "safety", "database", "project", "media", "steps", "timelines", "failures", "notes"}
ROUTE_KEYS = {"name", "route", "created", "steps", "readback", "markers", "navigation", "exports"}
CREATING = {"ProjectManager.CreateProject", "ProjectManager.CreateFolder", "ProjectManager.GotoRootFolder", "ProjectManager.OpenFolder",
            "ProjectManager.SaveProject", "MediaPool.ImportMedia", "MediaPool.ImportTimelineFromFile", "MediaPool.CreateEmptyTimeline",
            "MediaPool.AppendToTimeline", "Timeline.AddMarker", "Timeline.Export", "Project.SetSetting", "Resolve.OpenPage", "TimelineItem.AddTransition"}


def expected_of(bundle):
    return json.loads(bundle.expected.read_text())


def made(world: World):
    return [c[0] for c in world.log if c[0] in CREATING]


# -- the happy path ---------------------------------------------------------------------------------------------------------------
def test_the_probe_runs_every_route_in_a_project_of_its_own(bundle, monkeypatch, capsys):
    before = tree(bundle.dir)
    code, doc, world = run_probe(bundle, monkeypatch)
    out = capsys.readouterr().out
    exp = expected_of(bundle)
    assert code == 0 and doc["status"] == "complete" and world.rejected == []
    assert {c[0] for c in world.log} <= set(template.README_CALLS) | set(template.GATED_CALLS)  # every call the fake logged is allow-listed
    assert set(doc) == TOP_KEYS | {"edit_page"}
    # its own project, in its own folder, saved; nothing else touched
    name = doc["project"]["name"]
    assert re.fullmatch(r"matchcut-probe-\d{8}T\d{6}Z", name) and doc["project"]["folder"] == "matchcut-probe"
    assert world.projects[("matchcut-probe",)][name] is world.current and world.saved == [name]
    user = world.projects[()]["Untitled Project"]
    assert not user.touched and not user.timelines and not user.pool and user.settings == {"timelineFrameRate": "24"}
    assert not world.projects[("Client work",)]["Client job v2"].touched
    assert doc["project"]["user_current_project"] == "Untitled Project" and doc["safety"]["allowed"] is True
    assert doc["project"]["frame_rate_requested"] == "25" and doc["project"]["frame_rate_setting"] == "25"
    # every route made its own timeline and was read back
    assert list(doc["timelines"]) == ROUTES
    for route in ROUTES:
        entry = doc["timelines"][route]
        assert set(entry) >= ROUTE_KEYS and entry["created"] is True and entry["name"] == "matchcut-probe " + route
        video = entry["readback"]["video"][0]
        assert len(video) == len(exp["clips"])
        assert [v["source_start_frame"] for v in video] == [c["vis_in"] for c in exp["clips"]]
        assert [v["start"] - entry["readback"]["start_frame"] for v in video] == [c["rec_in"] for c in exp["clips"]]
        assert [v["duration"] for v in video] == [c["dur"] for c in exp["clips"]]
        assert entry["readback"]["start_timecode"] == "01:00:00:00" and entry["readback"]["track_counts"]["video"] == 1
        assert set(entry["exports"]) == {"otio", "fcpxml", "edl"} and all(e["ok"] and e["exists"] and e["size"] > 0 for e in entry["exports"].values())
    # the markers: the probe's own are found by junction id on build and import-otio; the OTIO's came across in this fake
    for route in ("build", "import-otio"):
        found = doc["timelines"][route]["markers"]["found_after"]
        assert [found[j["id"]]["frame"] for j in exp["junctions"]] == [float(j["record_frame"]) for j in exp["junctions"]]
    assert all(v is not None for v in doc["timelines"]["import-otio"]["markers"]["found_before"].values())
    assert all(v is None for v in doc["timelines"]["build"]["markers"]["found_before"].values())
    assert "added" in doc["timelines"]["build"]["markers"] and doc["timelines"]["import-fcpxml"]["markers"]["added"] == {}
    # AddTransition: the catalogue documents it, so one call per dissolve with the keys the catalogue names
    dissolves = [j for j in exp["junctions"] if j["frames"]]
    assert dissolves
    calls = world.calls("TimelineItem.AddTransition")
    assert [c[1] for c in calls] == [{"type": "Cross Dissolve", "alignment": "center", "duration": j["frames"]} for j in dissolves]
    attempts = doc["timelines"]["build+transitions"]["transitions"]
    assert [a["called"] for a in attempts] == [True] * len(dissolves) and all(a["documented_in_catalogue"] for a in attempts)
    # navigation: at most 5 junctions, from the start timecode plus the record frame, and from the marker frame where there is one
    nav = doc["timelines"]["build"]["navigation"]["attempts"]
    assert {a["junction"] for a in nav} == {j["id"] for j in exp["junctions"][:5]}
    first = [a for a in nav if a["how"] == "start timecode + record frame"][0]
    assert first["set"] is True and first["current_timecode"] == first["timecode"]
    assert doc["edit_page"] is True and world.page == "edit"
    # the failures in this fake: ImportMedia of a file the OTIO import already put in the pool returns [] (the probe then finds the clip),
    # and AddMarker is False where the OTIO's own marker already sits (the probe then gives that marker the junction id)
    n = len(exp["junctions"])
    assert [(f["where"], f["call"]) for f in doc["failures"]] == [("media", "MediaPool.ImportMedia")] + [("import-otio: markers", "Timeline.AddMarker")] * n
    added = doc["timelines"]["import-otio"]["markers"]["added"]
    assert all(a["add_marker"] is False and a["update_marker_custom_data"] is True for a in added.values())
    assert doc["media"]["found_in_pool_by"] == ["File Path"] and doc["timelines"]["import-edl"]["media_imported_first"] is True
    # files: only inside the bundle (the JSON and exports/)
    new_files = tree(bundle.dir) - before
    assert new_files == {"resolve_probe.json", "exports"} | {f"exports/{r.replace('+', '-')}.{x}" for r in ROUTES for x in ("otio", "fcpxml", "edl")}
    assert "send back resolve_probe.json and the exports folder" in out


def test_the_json_records_the_environment_bundle_catalogue_and_mcp(bundle, monkeypatch):
    code, doc, world = run_probe(bundle, monkeypatch)
    exp = expected_of(bundle)
    assert doc["schema"] == "matchcut.resolve_probe/1" and doc["probe_version"] == 1 and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", doc["created_utc"])
    assert doc["bundle"]["stage"] == "final" and doc["bundle"]["sha256"] == doc["bundle"]["sha256_at_generation"] == exp["sha256"]
    assert doc["resolve"] == {"product": "matchcut fake of the documented Resolve API", "version": [21, 1, 0, 0, ""], "version_string": "21.1.0.0 (fake)"}
    assert doc["platform"]["python"] == ".".join(map(str, sys.version_info[:3])) and doc["platform"]["sys_platform"] == sys.platform
    assert doc["scripting"]["connected"] is True and doc["scripting"]["module_path"].endswith("DaVinciResolveScript.py")
    assert doc["database"] == {"DbType": "Disk", "DbName": "Local Database"}
    cat = doc["catalogue"]
    assert cat["found"] and [Path(f["path"]).name for f in cat["files"]] == ["README.md", "DaVinciResolveScript.pyi"]
    assert cat["files"][0]["version_line"] == "Last Updated: fake 21.1.0"
    assert cat["mentions"]["AddTransition"] is True and cat["mentions"]["AppendToTimeline"] is True and cat["gated"]["TimelineItem.AddTransition"] is True
    assert "Cross Dissolve" in cat["excerpts"]["AddTransition"]
    assert doc["mcp"]["observed"] == "file absent" and doc["mcp"]["found"] == [] and "never starts or connects" in doc["mcp"]["documented"]
    # every step says where its call is documented
    steps = [s for s in doc["steps"]] + [s for e in doc["timelines"].values() for s in e["steps"]]
    assert steps and all(s["documented_in"] in ("README 21.0.4", "installed catalogue") for s in steps)
    assert {s["documented_in"] for s in doc["timelines"]["build+transitions"]["steps"] if s["call"] == "TimelineItem.AddTransition"} == {"installed catalogue"}


# -- safety -----------------------------------------------------------------------------------------------------------------------
def test_the_guard_refuses_a_user_project_and_creates_nothing(bundle, monkeypatch, capsys):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["a user project is open"])
    out = capsys.readouterr().out
    assert code == 3 and doc["status"] == "refused" and doc["safety"]["allowed"] is False
    assert doc["safety"]["current_project"] == doc["project"]["user_current_project"] == "Client job v3"
    assert made(world) == [] and world.saved == [] and world.current.name == "Client job v3" and world.rejected == []
    assert "Save and close your project first" in out and "'Client job v3'" in out
    assert doc["timelines"] == {} and not (bundle.dir / "exports").exists()


def test_an_earlier_probe_project_is_allowed_and_a_rerun_makes_a_new_project(bundle, monkeypatch):
    code, first, world = run_probe(bundle, monkeypatch)
    assert code == 0 and world.current.name == first["project"]["name"]
    code, second, world = run_probe(bundle, monkeypatch, world=world)
    assert code == 0 and second["safety"]["current_project"] == first["project"]["name"] and second["safety"]["allowed"]
    names = sorted(world.projects[("matchcut-probe",)])
    assert len(names) == 2 and second["project"]["name"] != first["project"]["name"] and set(names) == {first["project"]["name"], second["project"]["name"]}
    assert world.calls("ProjectManager.CreateFolder") == [("ProjectManager.CreateFolder", "matchcut-probe")]  # made once, reused
    assert world.saved == [first["project"]["name"], second["project"]["name"]]
    assert not world.projects[()]["Untitled Project"].touched


def test_the_dry_run_connects_and_checks_and_creates_nothing(bundle, monkeypatch, capsys):
    code, doc, world = run_probe(bundle, monkeypatch, args=["--dry-run"])
    assert code == 0 and doc["status"] == "dry-run" and doc["dry_run"] is True and doc["safety"]["allowed"] is True
    assert made(world) == [] and doc["project"]["created"] is False and doc["timelines"] == {}
    assert not (bundle.dir / "resolve_probe.json").exists() and (bundle.dir / "resolve_probe_dry_run.json").exists()
    assert "nothing was created" in capsys.readouterr().out
    # a dry run with a user project open says so too
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["a user project is open"], args=["--dry-run"])
    assert code == 3 and doc["status"] == "refused" and made(world) == []


def test_out_overrides_the_json_path(bundle, monkeypatch, tmp_path):
    target = tmp_path / "elsewhere" / "result.json"
    code, doc, world = run_probe(bundle, monkeypatch, args=["--out", str(target)])
    assert code == 0 and target.exists() and doc["status"] == "complete" and not (bundle.dir / "resolve_probe.json").exists()


def test_scripting_not_connected_says_what_to_set(bundle, monkeypatch, capsys):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["scripting not connected"])
    out = capsys.readouterr().out
    assert code == 2 and doc["status"] == "not connected" and doc["scripting"]["connected"] is False and doc["scripting"]["module_found"] is True
    assert "External scripting using = Local" in out and "Studio" in out
    assert [c[0] for c in world.log] == ["DaVinciResolveScript.scriptapp"]


def test_no_module_prints_the_exact_setup_for_every_os(bundle, monkeypatch, capsys, tmp_path):
    empty = tmp_path / "no-resolve" / "Scripting"
    code, doc, world = run_probe(bundle, monkeypatch, inject=False, catalogue_dir=empty, world=World(Variant(catalogue=False), None))
    out = capsys.readouterr().out
    assert code == 2 and doc["status"] == "module not found" and doc["scripting"]["module_found"] is False
    assert doc["scripting"]["module_dirs_tried"][0] == str(empty / "Modules") and doc["catalogue"]["found"] is False
    for line in ('export RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"',
                 'export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"',
                 r"set RESOLVE_SCRIPT_API=%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
                 r"set RESOLVE_SCRIPT_LIB=C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
                 'export RESOLVE_SCRIPT_LIB="/opt/resolve/libs/Fusion/fusionscript.so"', 'export PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules/"'):
        assert line in out, line


def test_an_early_stop_never_replaces_a_complete_result(bundle, monkeypatch):
    code, done, world = run_probe(bundle, monkeypatch)
    assert code == 0
    code, _, _ = run_probe(bundle, monkeypatch, VARIANTS["scripting not connected"])
    assert code == 2 and json.loads((bundle.dir / "resolve_probe.json").read_text())["status"] == "complete"
    assert json.loads((bundle.dir / "resolve_probe_last_attempt.json").read_text())["status"] == "not connected"


# -- documented calls only --------------------------------------------------------------------------------------------------------
def test_an_undocumented_add_transition_is_never_called(bundle, monkeypatch):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["AddTransition not documented"])
    assert code == 0 and world.rejected == [] and world.calls("TimelineItem.AddTransition") == []
    assert doc["catalogue"]["gated"] == {"TimelineItem.AddTransition": False, "TimelineItem.GetType": False}
    attempts = doc["timelines"]["build+transitions"]["transitions"]
    assert attempts and all(a["called"] is False and a["result"] == "not documented on this install: not called" for a in attempts)
    assert {s["error"] for s in doc["timelines"]["build+transitions"]["steps"] if s["call"] == "TimelineItem.AddTransition"} == {"not documented on this install: not called"}
    assert all(v["type"] == "not documented on this install: not called" for v in doc["timelines"]["build"]["readback"]["video"][0])


def test_without_any_catalogue_only_readme_calls_are_made(bundle, monkeypatch):
    v = Variant(catalogue=False)
    code, doc, world = run_probe(bundle, monkeypatch, v)
    assert code == 0 and world.rejected == [] and doc["catalogue"]["found"] is False
    called = {c[0] for c in world.log}
    assert called and all(name.split(".")[1] in DOCUMENTED[name.split(".")[0]] for name in called)


def test_the_option_keys_come_from_the_catalogue_text_only():
    opts, why = template.transition_options('options: {"type": "Cross Dissolve", "alignment": "center", "duration": <frames>}', 12)
    assert opts == {"type": "Cross Dissolve", "alignment": "center", "duration": 12} and "catalogue" in why
    opts, why = template.transition_options('AddTransition(options) adds a transition. Keys: "duration".', 8)
    assert opts == {"duration": 8}
    opts, why = template.transition_options("AddTransition(TransitionOptions) -> TimelineItem", 8)
    assert opts is None and "names none of the option keys" in why
    opts, _ = template.transition_options('"type", "position", "duration"', 6)  # a key without a value the text names is left out
    assert opts == {"duration": 6}


def test_the_script_calls_only_what_its_allow_list_documents():
    """Every Resolve call goes through Recorder.call with a "Class.Method" literal from README_CALLS or GATED_CALLS; the README part matches
    the fake's independent transcription of the 21.0.4 README, and the gated part is absent from it."""
    tree_ = ast.parse(Path(template.__file__).read_text())
    names = [n.args[3].value for n in ast.walk(tree_) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "call"
             and len(n.args) >= 4 and isinstance(n.args[3], ast.Constant) and isinstance(n.args[3].value, str)]
    item_names = [e.elts[1].value for n in ast.walk(tree_) if isinstance(n, ast.Tuple) for e in n.elts
                  if isinstance(e, ast.Tuple) and len(e.elts) == 3 and isinstance(e.elts[1], ast.Constant) and "." in str(e.elts[1].value)]
    used = set(names) | set(item_names)
    assert used and used <= set(template.README_CALLS) | set(template.GATED_CALLS), used - set(template.README_CALLS) - set(template.GATED_CALLS)
    for name in template.README_CALLS:
        cls, method = name.split(".")
        assert method in DOCUMENTED[cls], name
    for name in template.GATED_CALLS:
        cls, method = name.split(".")
        assert method not in DOCUMENTED[cls] and method in GATED[cls], name
    # no API method is called any other way: capitalised attribute calls appear only on non-API helpers
    direct = {n.func.attr for n in ast.walk(tree_) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr[:1].isupper()}
    assert direct == {"ArgumentParser"}, direct  # argparse's, not Resolve's


def test_the_script_is_python_3_6_and_imports_nothing_from_matchcut(bundle):
    text = bundle.script.read_text()
    tree_ = ast.parse(text, feature_version=(3, 6))
    imported = {a.name.split(".")[0] for n in ast.walk(tree_) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree_) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported == {"argparse", "datetime", "hashlib", "json", "os", "platform", "re", "sys", "traceback", "DaVinciResolveScript"}
    assert "matchcut." not in "".join(ast.unparse(n) for n in ast.walk(tree_) if isinstance(n, (ast.Import, ast.ImportFrom)))
    assert "not run against a live Resolve" in text


# -- failures are recorded and the run goes on -------------------------------------------------------------------------------------
def test_append_to_timeline_returning_none_is_a_recorded_failure(bundle, monkeypatch):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["AppendToTimeline returns [None]"])
    assert code == 0 and doc["status"] == "complete" and world.rejected == []
    fails = [f for f in doc["failures"] if f["call"] == "MediaPool.AppendToTimeline"]
    n = len(expected_of(bundle)["clips"])
    assert [f["where"] for f in fails] == ["build", "build+transitions"] and all(f["return"] == [None] * n and f["ok"] is False for f in fails)
    assert doc["timelines"]["build"]["readback"]["video"] == [[]]
    assert all(a["called"] is False for a in doc["timelines"]["build+transitions"]["transitions"])  # no item to add a transition to
    assert doc["timelines"]["import-otio"]["readback"]["video"][0]  # the other routes still ran


def test_an_exception_is_recorded_with_the_call_and_the_probe_moves_on(bundle, monkeypatch):
    v = Variant(raise_on=("MediaPool.ImportTimelineFromFile", "Timeline.GetMarkers"))
    code, doc, world = run_probe(bundle, monkeypatch, v)
    assert code == 0 and doc["status"] == "complete"
    imports = [f for f in doc["failures"] if f["call"] == "MediaPool.ImportTimelineFromFile"]
    assert [f["where"] for f in imports] == ["import-otio", "import-fcpxml", "import-edl"]
    assert all(f["error"] == "RuntimeError: fake failure in MediaPool.ImportTimelineFromFile" and f["called"] for f in imports)
    assert imports[0]["args"][1] == {"timelineName": "matchcut-probe import-otio", "importSourceClips": True}
    assert [r for r in ROUTES if doc["timelines"][r]["created"]] == ["build", "build+transitions"]
    assert doc["timelines"]["build"]["readback"]["markers"] is None
    assert any(f["call"] == "Timeline.GetMarkers" and f["where"] == "build: readback" for f in doc["failures"])


def test_a_crash_of_the_probe_itself_still_writes_the_json(bundle, monkeypatch, capsys):
    text = bundle.script.read_text().replace("def find_catalogue():", "def find_catalogue():\n    raise ZeroDivisionError('boom')\n\n\ndef _unused():", 1)
    bundle.script.write_text(text)
    code, doc, world = run_probe(bundle, monkeypatch)
    assert code == 1 and doc["status"] == "crashed" and doc["failures"][-1]["error"] == "ZeroDivisionError: boom"
    assert "the probe stopped on an error of its own" in capsys.readouterr().out


@pytest.mark.parametrize("tc, frames", [("01:00:00:00", 90000), ("00:00:10:00", 250), ("01:00:00;00", 90000)])
def test_the_script_timecode_matches_the_exporters_at_25(tc, frames, monkeypatch):
    monkeypatch.setattr(template, "EXPECTED", {"rate": [25, 1]})
    assert template.timecode_to_frames(tc) == (frames, False)
    assert template.frames_to_timecode(frames, False) == tc.replace(";", ":")
    monkeypatch.setattr(template, "EXPECTED", {"rate": [30000, 1001]})
    assert template.timecode_to_frames("01:00:00;00") == (107892, True) and template.frames_to_timecode(107892 + 1800, True) == "01:01:00;02"


def test_the_catalogue_definition_and_its_options_type_are_excerpted(tmp_path, monkeypatch):
    """A README.md that only mentions AddTransition does not hide the .pyi definition; a TypedDict's keys (key: type) count as named."""
    (tmp_path / "README.md").write_text("Last Updated: 21.1\nNew in 21.1: TimelineItem.AddTransition\n")
    (tmp_path / "DaVinciResolveScript.pyi").write_text(
        "class TransitionOptions(TypedDict):\n    type: Literal['Cross Dissolve', 'Dip To Color']\n    alignment: Literal['center', 'left']\n"
        "    durationFrames: int\n\n" + "\n" * 40 +
        "class TimelineItem:\n    def AddTransition(self, options: TransitionOptions) -> TimelineItem:\n        ...\n")
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(tmp_path))
    cat = template.find_catalogue()
    text = cat["excerpts"]["AddTransition"]
    assert "def AddTransition" in text and "class TransitionOptions" in text and cat["gated"]["TimelineItem.AddTransition"]
    opts, why = template.transition_options(text, 10)
    assert opts == {"type": "Cross Dissolve", "alignment": "center"}  # durationFrames is not a key the probe knows: left out, not guessed
