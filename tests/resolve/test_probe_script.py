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
from fractions import Fraction

from matchcut.interchange.common import source_info

from .fake_resolve import DOCUMENTED, GATED, VARIANTS, Media, ProjectModel, TimelineModel, Variant, World

ROUTES = ["import-otio", "import-fcpxml", "import-edl", "build", "build+transitions"]
TOP_KEYS = {"schema", "probe_version", "created_utc", "status", "dry_run", "bundle", "resolve", "platform", "scripting", "catalogue", "mcp",
            "safety", "database", "project", "media", "steps", "timelines", "failures", "notes", "run_dir", "version_notes"}
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
    assert set(doc) == TOP_KEYS | {"edit_page", "safety_after_confirm"}
    # its own project, in its own folder, saved; nothing else touched
    name = doc["project"]["name"]
    assert re.fullmatch(r"matchcut-probe-\d{8}T\d{6}Z", name) and doc["project"]["folder"] == "matchcut-probe"
    assert world.projects[("matchcut-probe",)][name] is world.current and world.saved == [name]
    user = world.projects[()]["Untitled Project"]
    assert not user.touched and not user.timelines and not user.pool and user.settings == {"timelineFrameRate": "24"}
    assert not world.projects[("Client work",)]["Client job v2"].touched
    assert doc["project"]["user_current_project"] == "Untitled Project" and doc["safety"]["allowed"] is True
    assert doc["safety"]["counts"] == {"timelines": 0, "clips": 0, "folders": 0, "depth": 0} and doc["safety"]["empty"] is True
    assert doc["safety_after_confirm"]["counts"] == doc["safety"]["counts"]  # looked again right before the switch
    assert doc["safety"]["confirmed"] == "--yes" and doc["version_notes"] == []
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
    # files: only inside the bundle, in this run's own directory (the JSON and exports/)
    run = doc["run_dir"]
    assert re.fullmatch(r"runs/\d{8}T\d{6}Z", run)
    new_files = tree(bundle.dir) - before
    assert new_files == {"runs", run, f"{run}/resolve_probe.json", f"{run}/exports"} | {
        f"{run}/exports/{r.replace('+', '-')}.{x}" for r in ROUTES for x in ("otio", "fcpxml", "edl")}
    assert doc["timelines"]["build"]["exports"]["otio"]["file"] == f"{run}/exports/build.otio"
    assert "send back that folder" in out and str(bundle.dir / run) in out


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
    assert "open a new, empty project (or quit and reopen Resolve) and run the probe again" in out and "'Client job v3'" in out
    assert "File > Close Project" not in out
    assert doc["timelines"] == {} and not list(bundle.dir.glob("runs/*/exports"))


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
    code, doc, world = run_probe(bundle, monkeypatch, args=["--dry-run"], answer=EOFError)
    assert code == 0 and doc["status"] == "dry-run" and doc["dry_run"] is True and doc["safety"]["allowed"] is True
    assert made(world) == [] and doc["project"]["created"] is False and doc["timelines"] == {}
    assert world.calls("input") == []  # a dry run never asks and never switches
    assert doc["run_dir"].endswith("-dry-run") and (bundle.dir / doc["run_dir"] / "resolve_probe.json").exists()
    assert not (bundle.dir / doc["run_dir"] / "exports").exists()
    assert "nothing was created" in capsys.readouterr().out
    # a dry run with a user project open says so too
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["a user project is open"], args=["--dry-run"])
    assert code == 3 and doc["status"] == "refused" and made(world) == []


def test_out_overrides_the_json_path(bundle, monkeypatch, tmp_path):
    target = tmp_path / "elsewhere" / "result.json"
    code, doc, world = run_probe(bundle, monkeypatch, args=["--out", str(target)])
    assert code == 0 and target.exists() and doc["status"] == "complete"
    assert not (bundle.dir / doc["run_dir"] / "resolve_probe.json").exists() and (bundle.dir / doc["run_dir"] / "exports" / "build.otio").exists()


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


def test_every_run_writes_its_own_directory_and_never_replaces_another(bundle, monkeypatch):
    code, done, world = run_probe(bundle, monkeypatch)
    assert code == 0
    first = bundle.dir / done["run_dir"]
    before = {p: p.read_bytes() for p in first.rglob("*") if p.is_file()}
    code, again, _ = run_probe(bundle, monkeypatch, VARIANTS["the open project changes mid-run"])  # a second real run that fails part-way
    assert code == 4 and again["status"] == "aborted" and again["run_dir"] != done["run_dir"]
    assert {p: p.read_bytes() for p in first.rglob("*") if p.is_file()} == before  # the earlier complete run is untouched
    code, third, _ = run_probe(bundle, monkeypatch, VARIANTS["scripting not connected"])
    assert code == 2 and third["run_dir"] not in (done["run_dir"], again["run_dir"])
    assert json.loads((first / "resolve_probe.json").read_text())["status"] == "complete"


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
    opts, why = template.transition_options('AddTransition(options) adds a transition. Keys: "duration" (in frames).', 8)
    assert opts == {"duration": 8}
    # the unit of duration is not stated: duration is not passed, and with nothing else named AddTransition is not called
    opts, why = template.transition_options('AddTransition(options) adds a transition. Keys: "duration".', 8)
    assert opts is None and "does not say the unit of duration" in why
    opts, why = template.transition_options('Keys: "duration", "alignment" ("center")', 8)
    assert opts == {"alignment": "center"} and "duration not passed" in why
    opts, why = template.transition_options("AddTransition(TransitionOptions) -> TimelineItem", 8)
    assert opts is None and "names none of the option keys" in why
    opts, _ = template.transition_options('"type", "position", "duration" in frames', 6)  # a key without a value the text names is left out
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
    assert text.lstrip().startswith("def AddTransition") and cat["excerpt_files"]["AddTransition"].endswith(".pyi")
    assert cat["gated"]["TimelineItem.AddTransition"] and list(cat["option_types"]) == ["TransitionOptions"]
    opts, why = template.transition_options(text + "\n" + cat["option_types"]["TransitionOptions"], 10)
    assert opts == {"type": "Cross Dissolve", "alignment": "center"}  # durationFrames is not a key the probe knows: left out, not guessed


# -- the review's safety and catalogue fixes -----------------------------------------------------------------------------------
@pytest.mark.parametrize("variant, counts", [("untitled with a timeline", {"timelines": 1, "clips": 0, "folders": 0, "depth": 0}),
                                             ("untitled with a pool clip", {"timelines": 0, "clips": 1, "folders": 0, "depth": 0}),
                                             ("a clip in a nested bin", {"timelines": 0, "clips": 1, "folders": 2, "depth": 2})])
def test_a_project_with_anything_in_it_is_refused(bundle, monkeypatch, capsys, variant, counts):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS[variant])
    out = capsys.readouterr().out
    assert code == 3 and doc["status"] == "refused" and doc["safety"]["allowed"] is False and doc["safety"]["empty"] is False
    assert doc["safety"]["counts"] == counts and doc["safety"]["current_project"] == "Untitled Project"
    assert made(world) == [] and world.saved == [] and world.current is world.user and world.rejected == []
    assert f"(it has {counts['timelines']} timelines, and {counts['clips']} clips in {counts['folders'] + 1} media pool folders)" in out
    assert "Save your work, then open a new, empty project (or quit and reopen Resolve) and run the probe again" in out


@pytest.mark.parametrize("variant, why", [("timeline count unreadable", "the timeline count could not be read"),
                                          ("root folder unreadable", "the media pool's root folder could not be read"),
                                          ("bins past the depth cap", "the media pool has folders more than 3 levels deep"),
                                          ("bins past the count cap", "the media pool has more than 50 folders")])
def test_a_project_whose_emptiness_cannot_be_confirmed_is_refused(bundle, monkeypatch, capsys, variant, why):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS[variant])
    out = capsys.readouterr().out
    assert code == 3 and doc["status"] == "refused" and made(world) == [] and world.current is world.user
    assert f"(the probe could not confirm that it is empty: {why})" in out


@pytest.mark.parametrize("variant, folders", [("an empty named project", 0), ("empty nested bins", 3), ("untitled with a pool folder", 1)])
def test_an_empty_project_is_allowed_whatever_its_name_and_empty_bins_are_fine(bundle, monkeypatch, variant, folders):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS[variant])
    assert code == 0 and doc["status"] == "complete" and doc["safety"]["empty"] is True and doc["safety"]["counts"]["folders"] == folders
    assert doc["safety"]["current_project"] == VARIANTS[variant].current_project and not world.user.touched


def test_a_project_opened_while_the_prompt_waits_is_refused_after_the_yes(bundle, monkeypatch, capsys):
    """N-C1: the guard runs again after the answer; the person opened a client project meanwhile and then typed yes."""
    world = World(VARIANTS["keeps everything"], source_info(expected_of(bundle)["source"]["path"]))
    client = world.projects[("Client work",)]["Client job v2"]
    client.timelines.append(TimelineModel("Edit", 86400, "01:00:00:00", Fraction(24)))

    def ask(prompt=""):
        world.log.append(("input", prompt))
        world.current = client  # the client project is opened in Resolve while the question waits
        return "yes"

    code, doc, world = run_probe(bundle, monkeypatch, world=world, answer=ask)
    out = capsys.readouterr().out
    assert code == 3 and doc["status"] == "refused" and doc["safety"]["confirmed"] is True
    assert doc["safety_after_confirm"]["current_project"] == "Client job v2" and doc["safety_after_confirm"]["counts"]["timelines"] == 1
    assert "The open project changed while the probe waited for your answer" in out and "Nothing was created" in out
    assert made(world) == [] and world.current is client and not client.touched and world.saved == []


@pytest.mark.parametrize("answer", ["no", "", "y", EOFError])
def test_without_a_yes_nothing_is_switched_or_created(bundle, monkeypatch, capsys, answer):
    code, doc, world = run_probe(bundle, monkeypatch, answer=answer)
    out = capsys.readouterr().out
    assert code == 5 and doc["status"] == "not confirmed" and doc["safety"]["confirmed"] is False
    assert made(world) == [] and world.current is world.user and doc["timelines"] == {}
    (prompt,) = [c[1] for c in world.calls("input")]
    assert "'Untitled Project'" in prompt and "matchcut-probe-" in prompt and "'matchcut-probe'" in prompt and prompt.endswith("Type yes to continue: ")
    assert "(or with a -2 suffix if that name is taken)" in prompt
    assert "nothing was created and the open project was not switched" in out


def test_typing_yes_proceeds_and_yes_flag_skips_the_question(bundle, monkeypatch):
    code, doc, world = run_probe(bundle, monkeypatch, answer="yes")
    assert code == 0 and doc["status"] == "complete" and doc["safety"]["confirmed"] is True and len(world.calls("input")) == 1
    code, doc, world = run_probe(bundle, monkeypatch)  # --yes
    assert code == 0 and doc["safety"]["confirmed"] == "--yes" and world.calls("input") == []


def test_an_earlier_probe_project_is_allowed_even_with_timelines(bundle, monkeypatch):
    code, first, world = run_probe(bundle, monkeypatch)
    code, second, world = run_probe(bundle, monkeypatch, world=world)
    s = second["safety"]
    assert code == 0 and s["earlier_probe_project"] is True and s["allowed"] is True and s["counts"]["timelines"] == 5


def test_a_module_that_is_found_but_fails_to_import_is_named(bundle, monkeypatch, capsys, tmp_path):
    api = tmp_path / "Scripting"
    (api / "Modules").mkdir(parents=True)
    (api / "Modules" / "DaVinciResolveScript.py").write_text("raise ImportError('cannot load fusionscript: wrong architecture')\n")
    monkeypatch.setattr(sys, "path", list(sys.path))
    code, doc, world = run_probe(bundle, monkeypatch, inject=False, catalogue_dir=api, world=World(Variant(catalogue=False), None))
    out = capsys.readouterr().out
    path = str(api / "Modules" / "DaVinciResolveScript.py")
    assert code == 2 and doc["status"] == "module failed to import" and doc["scripting"]["module_found"] is False
    assert doc["scripting"]["module_errors"] == [{"where": str(api / "Modules"), "file": path,
                                                  "error": "ImportError: cannot load fusionscript: wrong architecture"}]
    assert f"found {path} but it failed to import: ImportError: cannot load fusionscript: wrong architecture" in out
    assert "a Python that does not match Resolve's" in out and "export RESOLVE_SCRIPT_API=" in out
    assert "was not found" not in out and "If Resolve is installed elsewhere than its default folders, set these" in out
    monkeypatch.setattr(sys, "argv", [str(bundle.script), "--check-module"])
    import runpy
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(bundle.script), run_name="__main__")
    assert exc.value.code == 2 and f"found {path} but it failed to import" in capsys.readouterr().out


def test_create_project_that_does_not_switch_stops_before_anything_else(bundle, monkeypatch):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["CreateProject does not switch"])
    assert code == 4 and doc["status"] == "aborted" and world.calls("ProjectManager.SaveProject") == []
    assert [c for c in made(world) if c not in ("ProjectManager.GotoRootFolder", "ProjectManager.CreateFolder", "ProjectManager.OpenFolder",
                                                 "ProjectManager.CreateProject")] == []
    assert any("stopped before touching anything" in n for n in doc["notes"]) and not world.user.touched


def test_a_project_switch_mid_run_stops_before_the_next_write_and_nothing_is_saved(bundle, monkeypatch):
    """N-I1: the switch happens between two calls of one part (after CreateEmptyTimeline, before SetCurrentTimeline/AppendToTimeline)."""
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["the open project changes mid-run"])
    assert code == 4 and doc["status"] == "aborted" and doc["project"]["saved"] is False and doc["stopped"] is True
    assert world.calls("ProjectManager.SaveProject") == [] and not world.user.touched
    switch = world.trace.index("MediaPool.CreateEmptyTimeline")
    after = world.trace[switch + 1:]
    assert not [c for c in after if c in template.WRITES], after  # no further write lands
    assert any(n.startswith("before Project.SetCurrentTimeline the open project is 'Untitled Project', not the probe's") for n in doc["notes"])
    assert any(n.startswith("not saved: the open project is no longer the probe's") for n in doc["notes"])
    assert doc["failures"][-1]["error"] == "not called: the open project is 'Untitled Project', not the probe's"


PYI_NEIGHBOUR = '''class Timeline:
    def AddTransition(self, opts) -> bool:
        """not TimelineItem's: {"type": "Cross Dissolve", "duration": 5 frames}"""

class TimelineItem:
    def AddTransition(self, options) -> TimelineItem:
        """Adds a transition."""
    def SetFades(self, fades: dict) -> bool:
        """fades: {"position": "end", "duration": 12 frames, "alignment": "center"}"""
'''


def test_add_transition_keys_never_come_from_a_neighbouring_def(tmp_path, monkeypatch):
    (tmp_path / "DaVinciResolveScript.pyi").write_text(PYI_NEIGHBOUR)
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(tmp_path))
    cat = template.find_catalogue()
    text = cat["excerpts"]["AddTransition"]
    assert "SetFades" not in text and "Timeline:" not in text and text.strip().endswith('"""Adds a transition."""')
    assert template.transition_options(text, 12)[0] is None  # nothing named: not called
    assert cat["gated"]["TimelineItem.AddTransition"] is True and cat["excerpt_files"]["AddTransition"].endswith(".pyi")


def test_gating_is_per_class(tmp_path, monkeypatch):
    (tmp_path / "DaVinciResolveScript.pyi").write_text("class Timeline:\n    def AddTransition(self): ...\n    def GetType(self): ...\n")
    (tmp_path / "README.md").write_text("Timeline\n  AddTransition() on a timeline\n\nTimelineItem\n  GetName()\n")
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(tmp_path))
    cat = template.find_catalogue()
    assert cat["gated"] == {"TimelineItem.AddTransition": False, "TimelineItem.GetType": False}
    (tmp_path / "README.md").write_text("Timeline\n  GetName()\n\nTimelineItem\n  GetType() --> string  # new in 21.1\n")
    assert template.find_catalogue()["gated"] == {"TimelineItem.AddTransition": False, "TimelineItem.GetType": True}


def test_marker_answers_without_a_frame_are_recorded_raw(bundle, monkeypatch):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["marker info without frame"])
    exp = expected_of(bundle)
    found = doc["timelines"]["build"]["markers"]["found_after"]
    assert code == 0 and all(v is not None and v["frame"] is None and v["raw"]["customData"] == j["id"] for j, v in zip(exp["junctions"], found.values()))
    nav = doc["timelines"]["build"]["navigation"]["attempts"]
    assert {a["how"] for a in nav} == {"start timecode + record frame"}  # no frame to navigate to from the marker


def test_the_frame_rate_setting_and_the_timeline_settings_snapshot(bundle, monkeypatch):
    code, doc, world = run_probe(bundle, monkeypatch)
    rb = doc["timelines"]["build"]["readback"]
    assert rb["settings"] == {"timelineFrameRate": "25"} and rb["frame_rate_setting"] == "25" and rb["frame_rate_setting_from"] == "Timeline.GetSetting() snapshot"
    monkeypatch.setattr(template, "EXPECTED", {"rate": [30000, 1001], "source": {"start_tc_drop": True}})
    assert template.rate_setting() == "29.97 DF"
    monkeypatch.setattr(template, "EXPECTED", {"rate": [30000, 1001], "source": {"start_tc_drop": False}})
    assert template.rate_setting() == "29.97"
    monkeypatch.setattr(template, "EXPECTED", {"rate": [24000, 1001], "source": {"start_tc_drop": True}})
    assert template.rate_setting() == "23.976"


def test_an_older_resolve_gets_the_version_note(bundle, monkeypatch):
    code, doc, world = run_probe(bundle, monkeypatch, VARIANTS["Resolve 18.5"])
    assert code == 0 and len(doc["version_notes"]) == 1 and "18.5.0.0" in doc["version_notes"][0]
    assert "not in the 18.5 README" in doc["version_notes"][0]


def test_the_linux_lib_defaults_follow_the_readme():
    assert template.LIB_DEFAULTS["linux"] == ["/opt/resolve/libs/Fusion/fusionscript.so", "/home/resolve/libs/Fusion/fusionscript.so"]


@pytest.mark.parametrize("change", ["another empty project", "a clip added to the open project"])
def test_any_change_while_the_prompt_waits_is_refused_even_if_the_new_state_would_pass(bundle, monkeypatch, capsys, change):
    """The re-check compares with what the person saw when they answered, not only with the rule."""
    world = World(VARIANTS["keeps everything"], source_info(expected_of(bundle)["source"]["path"]))
    other = ProjectModel("Other empty project", ())
    world.projects[()][other.name] = other

    def ask(prompt=""):
        world.log.append(("input", prompt))
        if change == "another empty project":
            world.current = other  # also empty: it would pass the guard on its own
        else:
            world.user.pool.append(Media(world.src.path, "late.mp4", world.src))
        return "yes"

    code, doc, world = run_probe(bundle, monkeypatch, world=world, answer=ask)
    assert code == 3 and doc["status"] == "refused" and made(world) == []
    assert "The open project changed while the probe waited for your answer" in capsys.readouterr().out
