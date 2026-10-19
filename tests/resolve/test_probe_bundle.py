"""``python -m matchcut.resolve probe``: the bundle (M2a's exports, expected.json, resolve_probe.py) and the CLI around it."""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from matchcut.interchange.common import export_timeline, junction_ids, layout
from matchcut.resolve import ProbeError
from matchcut.resolve.__main__ import main
from matchcut.resolve.probe import build_bundle

REPO = Path(__file__).resolve().parents[2]


def test_the_bundle_holds_the_exports_the_expected_cut_and_the_script(wsp, bundle):
    names = {p.name for p in bundle.dir.iterdir()}
    assert {"final.otio", "matchcut.fcpxml", "final.edl", "expected.json", "resolve_probe.py", "provenance.json", "fidelity.json"} <= names
    exp = json.loads(bundle.expected.read_text())
    w = wsp.world
    snapped, _ = export_timeline(w.by_stage["final"], w.bounds, w.src)
    rows = layout(snapped, w.src.rate)
    assert exp["schema"] == "matchcut.resolve_probe_expected/1" and exp["stage"] == "final" and exp["start_tc"] == "00:00:00:00"
    assert exp["rate"] == [w.src.rate.numerator, w.src.rate.denominator] and exp["source"]["path"] == w.src.path
    assert [(c["vis_in"], c["vis_out"], c["rec_in"], c["dur"]) for c in exp["clips"]] == [(r.vis_in, r.vis_out, r.rec_in, r.dur) for r in rows]
    assert [c["transition_in"]["frames"] for c in exp["clips"]] == [r.tr for r in rows]
    assert [j["id"] for j in exp["junctions"]] == junction_ids(snapped)
    assert [j["record_frame"] for j in exp["junctions"]] == [rows[j["index"] + 1].cut for j in exp["junctions"]]
    assert exp["files"] == {"otio": "final.otio", "fcpxml": "matchcut.fcpxml", "edl": "final.edl"}
    # the OTIO carries the same cut frames as markers
    otio = json.loads((bundle.dir / "final.otio").read_text())
    assert [m["marked_range"]["start_time"]["value"] for m in otio["tracks"]["children"][0]["markers"]] == [j["record_frame"] for j in exp["junctions"]]
    # the script carries exactly expected.json
    tree = ast.parse(bundle.script.read_text(), feature_version=(3, 6))
    (literal,) = [n.value for n in tree.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "EXPECTED"]
    assert ast.literal_eval(literal) == exp
    assert "stage final" in bundle.script.read_text().splitlines()[1]


def test_probe_cli_prints_the_one_command_and_says_it_is_not_live(wsp, capsys):
    out_dir = wsp.tmp / "bundle"
    code = main(["probe", str(wsp.ws), "--stage", "roughcut", "--out", str(out_dir)])
    out = capsys.readouterr().out
    assert code == 0 and (out_dir / "roughcut.otio").exists() and (out_dir / "resolve_probe.py").exists()
    assert f'  python3 "{out_dir / "resolve_probe.py"}"' in out.splitlines()
    assert "not run against a live Resolve" in out and "External scripting using = Local" in out
    assert f'python -m matchcut.resolve report "{wsp.ws}" "{out_dir / "resolve_probe.json"}"' in out
    assert json.loads((out_dir / "expected.json").read_text())["stage"] == "roughcut"


def test_probe_run_without_resolve_says_so_plainly(wsp, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(tmp_path / "none"))
    code = main(["probe", str(wsp.ws), "--run"])
    out = capsys.readouterr().out
    assert code == 2 and "cannot be imported on this machine, so the probe was not run" in out
    assert not (wsp.ws / "resolve_probe" / "resolve_probe.json").exists()


@pytest.mark.parametrize("argv, message", [
    (["probe", "{ws}", "--stage", "human"], "the human stage is an import result, not a canonical stage: probe one of the canonical stages"),
    (["probe", "{ws}", "--stage", "nope"], "no stage nope in {ws}"),
    (["probe", "{tmp}/missing"], "no workspace at {tmp}/missing (config.json missing)"),
    (["report", "{ws}", "{tmp}/nope.json"], "cannot read {tmp}/nope.json: no such file"),
])
def test_bad_input_is_a_one_line_error(wsp, capsys, argv, message):
    argv = [a.format(ws=wsp.ws, tmp=wsp.tmp) for a in argv]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert err == message.format(ws=wsp.ws, tmp=wsp.tmp) + "\n"


def test_report_cli_prints_and_writes_the_report(wsp, bundle, capsys, monkeypatch):
    from .conftest import run_probe

    run_probe(bundle, monkeypatch)
    capsys.readouterr()
    assert main(["report", str(wsp.ws), str(bundle.dir / "resolve_probe.json")]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Resolve probe report") and out.rstrip().endswith(f"wrote {bundle.dir / 'probe_report.md'}")


def test_the_package_stays_out_of_the_tui_review_and_engine():
    code = ("import sys; import matchcut.resolve.probe, matchcut.resolve.report, matchcut.resolve.__main__; "
            "bad = [m for m in sys.modules if m in ('textual', 'DaVinciResolveScript') or m.startswith(('textual.', 'matchcut.tui', 'matchcut.review'))]; "
            "sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=str(REPO)).returncode == 0
    engine = ("import sys; import matchcut.pipeline.pipeline, matchcut.pipeline.cli, matchcut.interchange.workspace; "
              "sys.exit(1 if any(m.startswith('matchcut.resolve') for m in sys.modules) else 0)")
    assert subprocess.run([sys.executable, "-c", engine], cwd=str(REPO)).returncode == 0
    offenders = [p for p in (REPO / "matchcut").rglob("*.py") if "resolve" not in p.parts and "matchcut.resolve" in p.read_text()]
    assert offenders == []


def test_the_template_imports_nothing_but_the_standard_library():
    from matchcut.resolve import _probe_script as t

    tree = ast.parse(Path(t.__file__).read_text(), feature_version=(3, 6))
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names} | {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods == {"argparse", "datetime", "hashlib", "json", "os", "platform", "re", "sys", "traceback", "DaVinciResolveScript"}


def test_a_human_stage_in_the_workspace_is_refused(wsp):
    with pytest.raises(ProbeError):
        build_bundle(wsp.ws, wsp.cfg, "human")
