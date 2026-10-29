"""The probe run from inside Resolve's own console (Workspace > Console, Py3), as the free edition allows.

The console hands the script Resolve's app object (``resolve``); nothing is imported from outside, ``input()`` is never called (the real run
needs --yes), and no SystemExit reaches Resolve. The tests exec the exact lines ``python -m matchcut.resolve probe`` prints, with a
``resolve`` global that is the fake, and with no DaVinciResolveScript importable.
"""
from __future__ import annotations

import builtins
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from matchcut.interchange.common import source_info
from matchcut.resolve.__main__ import main
from matchcut.resolve.probe import console_lines
from matchcut.resolve.report import build_report

from .conftest import last_run
from .fake_resolve import VARIANTS, FakeResolve, Variant, World


def console(bundle, monkeypatch, line: str, variant: Variant = None):
    """Exec ``line`` as Resolve's Py3 console would, with ``resolve`` = the fake: (the JSON it wrote, the world, what it printed)."""
    expected = json.loads(bundle.expected.read_text())
    world = World(variant or VARIANTS["keeps everything"], source_info(expected["source"]["path"]))
    cat = bundle.dir.parent / "fake-install" / "Scripting"
    world.write_catalogue(cat)
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(cat))
    monkeypatch.delitem(sys.modules, "DaVinciResolveScript", raising=False)
    monkeypatch.setattr(sys, "argv", [])  # the console has no command line

    def no_input(prompt=""):
        raise AssertionError("input() called in console mode")

    monkeypatch.setattr(builtins, "input", no_input)
    assert importlib.util.find_spec("DaVinciResolveScript") is None
    before = last_run(bundle)
    exec(compile(line, "<Resolve console>", "single"), {"resolve": FakeResolve(world)})
    run = last_run(bundle)
    doc = json.loads(run.read_text()) if run is not None and run != before else None
    return doc, world


def test_the_dry_run_line_runs_in_the_console_and_creates_nothing(bundle, monkeypatch, capsys):
    dry, _ = console_lines(bundle.script)
    doc, world = console(bundle, monkeypatch, dry)
    out = capsys.readouterr().out
    assert out.rstrip().endswith("matchcut probe exit 0") and doc["status"] == "dry-run"
    assert doc["scripting"]["mode"] == "in-app console" and doc["scripting"]["connected"] is True and doc["scripting"]["module_found"] is None
    assert [c for c in world.log if c[0] != "input"] == [] and "DaVinciResolveScript" not in sys.modules  # nothing created, nothing imported


def test_the_real_run_line_runs_end_to_end_without_asking(bundle, monkeypatch, capsys):
    _, real = console_lines(bundle.script)
    doc, world = console(bundle, monkeypatch, real)
    out = capsys.readouterr().out
    assert out.rstrip().endswith("matchcut probe exit 0") and doc["status"] == "complete" and world.rejected == []
    assert doc["scripting"]["mode"] == "in-app console" and doc["safety"]["confirmed"] == "--yes"
    assert doc["safety_after_confirm"]["current_project"] == "Untitled Project"  # the re-check runs even with nothing to wait for
    assert world.saved == [doc["project"]["name"]] and not world.user.touched and world.calls("input") == []
    assert all(doc["timelines"][r]["created"] for r in doc["timelines"]) and len(doc["timelines"]) == 5
    assert (bundle.dir / doc["run_dir"] / "exports" / "build.otio").is_file()
    assert "DaVinciResolveScript.scriptapp" not in {c[0] for c in world.log}  # the app object was handed in


def test_in_the_console_the_real_run_needs_yes(bundle, monkeypatch, capsys):
    line = console_lines(bundle.script)[1].replace("['--yes']", "[]")
    doc, world = console(bundle, monkeypatch, line)
    out = capsys.readouterr().out
    assert out.rstrip().endswith("matchcut probe exit 5") and doc["status"] == "not confirmed" and doc["safety"]["confirmed"] is False
    assert "In Resolve's console the probe cannot ask: use the real-run line, which passes --yes." in out
    assert world.calls("ProjectManager.CreateProject") == [] and world.calls("ProjectManager.GotoRootFolder") == []


def test_no_system_exit_escapes_into_resolve(bundle, monkeypatch, capsys):
    line = console_lines(bundle.script)[1].replace("['--yes']", "['--bogus']")
    doc, world = console(bundle, monkeypatch, line)  # argparse exits on an unknown argument: the console gets the code, not SystemExit
    assert capsys.readouterr().out.rstrip().endswith("matchcut probe exit 2") and doc is None


@pytest.mark.parametrize("variant, code, status", [("a user project is open", 3, "refused"), ("the open project changes mid-run", 4, "aborted"),
                                                   ("the open project changes before CreateProject", 3, "refused")])
def test_every_guard_holds_in_the_console(bundle, monkeypatch, capsys, variant, code, status):
    _, real = console_lines(bundle.script)
    doc, world = console(bundle, monkeypatch, real, VARIANTS[variant])
    assert capsys.readouterr().out.rstrip().endswith(f"matchcut probe exit {code}") and doc["status"] == status
    assert world.calls("ProjectManager.SaveProject") == [] and not world.user.touched


def test_the_report_names_the_free_edition_and_the_console(wsp, bundle, monkeypatch):
    _, real = console_lines(bundle.script)
    console(bundle, monkeypatch, real, Variant(name="free 21.0.2", product="DaVinci Resolve", version=(21, 0, 2, 4, "")))
    rep = build_report(wsp.ws, wsp.cfg, bundle.dir)
    assert "Status: measured by resolve_probe.py from Resolve's own console on DaVinci Resolve 21.0.2.4 (fake) (free edition)" in rep.text
    assert '- edition: free edition (GetProductName() does not contain "Studio")' in rep.text and "- scripting: in-app console;" in rep.text
    rep_studio = Variant(name="studio", product="DaVinci Resolve Studio")
    console(bundle, monkeypatch, real, rep_studio)
    assert "(Studio)" in build_report(wsp.ws, wsp.cfg, bundle.dir).text


def test_the_probe_command_prints_the_two_console_lines(wsp, capsys):
    out_dir = wsp.tmp / "bundle"
    assert main(["probe", str(wsp.ws), "--out", str(out_dir)]) == 0
    lines = capsys.readouterr().out.splitlines()
    dry, real = console_lines(out_dir / "resolve_probe.py")
    assert lines[lines.index("# dry run (creates nothing):") + 1] == dry
    assert lines[lines.index("# real run (this switches to a new probe project):") + 1] == real
    assert "['--yes']" in real and "['--dry-run']" in dry and not dry.startswith(" ") and "runpy" not in real
    for line in (dry, real):
        compile(line, "<Resolve console>", "single")


def test_the_console_lines_quote_any_path():
    dry, real = console_lines(Path("/Users/rob/Movies/matchcut-demo_edit/resolve_probe/resolve_probe.py"))
    assert dry.startswith("_mc_p = '/Users/rob/Movies/matchcut-demo_edit/resolve_probe/resolve_probe.py'; ")
    assert dry.endswith('print("matchcut probe exit", _mc_g["console"](resolve, [\'--dry-run\']))')
    odd = console_lines(Path("/Users/o'neil/My \"cut\"/resolve_probe.py"))[1]
    compile(odd, "<Resolve console>", "single")


def test_the_terminal_mode_is_unchanged(bundle, monkeypatch):
    from .conftest import run_probe

    code, doc, world = run_probe(bundle, monkeypatch)
    assert code == 0 and doc["scripting"]["mode"] == "external" and "DaVinciResolveScript.scriptapp" in {c[0] for c in world.log}
