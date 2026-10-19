"""The ``export`` and ``import`` commands of the pipeline CLI: output lines, exit codes, one-line errors, lazy import."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from matchcut.interchange import workspace
from matchcut.interchange.common import export_timeline
from matchcut.interchange.otio import export_otio
from matchcut.pipeline import cli

from .test_workspace import patched_otio, write_edited_files, final_of


def run(argv, capsys):
    code = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return code, out.out, out.err


# -- export ------------------------------------------------------------------------------------------
def test_export_prints_the_files_and_the_fidelity_summary(wsx, capsys):
    code, out, err = run(["export", wsx.ws], capsys)
    assert code == 0 and err == ""
    exports = wsx.ws / "exports"
    lines = out.splitlines()
    assert lines[0] == f"exported 8 files to {exports}"
    for name in ("roughcut.otio", "final.otio", "matchcut.fcpxml", "roughcut.edl", "final.edl", "provenance.json", "fidelity.json", "fidelity.md"):
        assert f"  {exports / name}" in lines
    assert f"fidelity: 6 checks, 0 errors ({exports / 'fidelity.md'})" in lines
    results = [l for l in lines if l.startswith(("  roughcut  ", "  final  "))]
    assert len(results) == 6 and results[0].startswith("  roughcut  otio  ok: ") and results[-1].startswith("  final  edl  ok: ")
    assert all(" preserved, " in l and " approximated, " in l and l.endswith(" lost") for l in results)


def test_export_options(wsx, capsys):
    out_dir = wsx.tmp / "elsewhere"
    code, out, err = run(["export", wsx.ws, "--stage", "final", "--format", "edl", "--format", "otio", "--out", out_dir], capsys)
    assert code == 0 and err == "" and out.splitlines()[0] == f"exported 5 files to {out_dir}"
    assert sorted(p.name for p in out_dir.iterdir()) == ["fidelity.json", "fidelity.md", "final.edl", "final.otio", "provenance.json"]
    assert "fidelity: 2 checks, 0 errors" in out
    code, out, _ = run(["export", wsx.ws, "--stage", "roughcut", "--stage", "polish", "--format", "otio", "--out", out_dir], capsys)
    assert code == 0 and "polish.otio" in out and "roughcut.otio" in out


def test_export_prints_the_notes(wsx, music_file, capsys):
    cfg = json.loads((wsx.ws / "config.json").read_text())
    cfg["music"]["path"], cfg["music"]["only"] = str(music_file), True
    (wsx.ws / "config.json").write_text(json.dumps(cfg))
    code, out, err = run(["export", wsx.ws, "--stage", "final"], capsys)
    assert code == 0 and err == ""
    assert any(l.startswith("note: music.only:") and "source audio" in l for l in out.splitlines()), out


def test_export_errors_go_to_stderr_with_exit_1(wsx, capsys, monkeypatch):
    code, out, err = run(["export", wsx.tmp / "nowhere"], capsys)
    assert (code, out) == (1, "") and err == f"no workspace at {wsx.tmp / 'nowhere'} (config.json missing)\n"
    code, out, err = run(["export", wsx.ws, "--stage", "nope"], capsys)
    assert (code, out) == (1, "") and err == f"no stage nope in {wsx.ws}\n"
    real = workspace.fidelity.check

    def broken(canonical, back, fmt, rate, exported=None):
        r = real(canonical, back, fmt, rate, exported)
        if fmt == "otio" and r.stage == "final":
            r.errors.append("record duration 99 != 100 frames")
        return r

    monkeypatch.setattr(workspace.fidelity, "check", broken)
    code, out, err = run(["export", wsx.ws], capsys)
    assert (code, out) == (1, "") and err == "export of otio/final does not match the canonical timeline: record duration 99 != 100 frames\n"
    (wsx.ws / "config.json").write_text("{broken")
    code, out, err = run(["export", wsx.ws], capsys)
    assert code == 1 and err.startswith(f"cannot read {wsx.ws / 'config.json'}") and err.count("\n") == 1


def test_an_unknown_format_is_a_one_line_error_with_exit_1(wsx, capsys):
    code, out, err = run(["export", wsx.ws, "--format", "aaf"], capsys)
    assert (code, out, err) == (1, "", "unknown format aaf: use otio, fcpxml or edl\n")
    code, out, err = run(["import", wsx.ws, wsx.tmp / "x.otio", "--format", "aaf"], capsys)
    assert (code, out, err) == (1, "", "unknown format aaf: use otio, fcpxml or edl\n")
    assert not (wsx.ws / "exports").exists()


# -- import ------------------------------------------------------------------------------------------
def test_import_prints_the_human_decisions_and_the_report(wsx, capsys):
    edited = patched_otio(wsx)
    code, out, err = run(["import", wsx.ws, edited], capsys)
    assert code == 0 and err == ""
    lines = out.splitlines()
    assert lines[0] == f"imported {edited.name} (otio) against final: 2 human decisions -> {wsx.ws / 'human.json'}"
    assert lines[1] == "  trim c002  human trimmed c002: in +5f"
    assert lines[2].startswith("  transition c002 c003  human changed transition dissolve ") and lines[2].endswith("f -> cut")
    assert any(l.startswith("import report: ") for l in lines) or len(lines) == 3
    assert (wsx.ws / "human.json").exists() and (wsx.ws / "human_import.json").exists()


def test_import_with_no_changes_and_with_notes(wsx, capsys):
    final, snapped = final_of(wsx)
    files = write_edited_files(wsx, snapped, wsx.tmp, rename=False)
    code, out, err = run(["import", wsx.ws, files["edl"]], capsys)
    assert code == 0 and out.splitlines()[0] == f"imported {files['edl'].name} (edl) against final: 0 human decisions -> {wsx.ws / 'human.json'}"
    assert "no changes" in out
    doc = json.loads(files["otio"].read_text())
    doc["tracks"]["children"][0]["children"][0]["effects"] = [{"OTIO_SCHEMA": "Effect.1", "name": "Blur", "effect_name": "Blur", "metadata": {}}]
    files["otio"].write_text(json.dumps(doc))
    code, out, err = run(["import", wsx.ws, files["otio"], "--format", "otio"], capsys)
    assert code == 0 and "import report: 1 note" in out and "  - clip c001 shot0: effect Blur ignored" in out.splitlines()


def test_import_stage_option_and_stale_warning(wsx, capsys):
    final, snapped = final_of(wsx)
    files = write_edited_files(wsx, snapped, wsx.tmp, rename=False)
    code, out, _ = run(["import", wsx.ws, files["otio"], "--stage", "roughcut"], capsys)
    assert code == 0 and "against roughcut" in out
    stale = json.loads((wsx.ws / "roughcut.json").read_text())
    stale["name"] = "changed after the import"
    (wsx.ws / "roughcut.json").write_text(json.dumps(stale))
    code, out, err = run(["export", wsx.ws, "--stage", "roughcut", "--format", "edl"], capsys)
    assert code == 0 and "note: human.json is stale: roughcut.json changed since the import (re-import the editor's file to see it)" in out.splitlines()


def test_import_errors_go_to_stderr_with_exit_1(wsx, capsys):
    code, out, err = run(["import", wsx.ws, wsx.tmp / "missing.otio"], capsys)
    assert (code, out) == (1, "") and err.startswith("cannot read ") and err.count("\n") == 1
    bad = wsx.tmp / "bad.edl"
    bad.write_text("this is not an edl\n")
    code, out, err = run(["import", wsx.ws, bad], capsys)
    assert (code, out) == (1, "") and err.startswith("invalid EDL") and err.count("\n") == 1
    code, out, err = run(["import", wsx.tmp / "nowhere", bad], capsys)
    assert (code, out) == (1, "") and err.startswith("no workspace at ")
    assert not (wsx.ws / "human.json").exists()


def test_junctions_says_when_the_human_stage_is_stale_and_leaves_it_out(wsx, capsys):
    code, out, _ = run(["import", wsx.ws, patched_otio(wsx)], capsys)
    code, out, err = run(["junctions", wsx.ws], capsys)
    assert code == 0 and out.splitlines()[0].startswith("4 junctions in human (") and "note:" not in out
    final = json.loads((wsx.ws / "final.json").read_text())
    final["name"] = "re-run"
    (wsx.ws / "final.json").write_text(json.dumps(final, indent=2))
    msg = "human.json is stale: final.json changed since the import (re-import the editor's file to see it)"
    code, out, err = run(["junctions", wsx.ws], capsys)
    assert code == 0 and err == "" and out.splitlines()[0].startswith("4 junctions in final (") and out.splitlines()[-1] == f"note: {msg}"
    code, out, err = run(["junctions", wsx.ws, "--index", "1"], capsys)
    assert code == 0 and out.splitlines()[-1] == f"note: {msg}"
    assert (wsx.ws / "human.json").exists()
    code, out, _ = run(["export", wsx.ws, "--stage", "roughcut", "--format", "edl"], capsys)
    assert f"note: {msg}" in out.splitlines()
    code, out, _ = run(["import", wsx.ws, patched_otio(wsx)], capsys)
    assert code == 0 and any(l.startswith(f"  - {msg}; this import replaces it") for l in out.splitlines())


def test_import_against_an_earlier_stage_prints_the_warning(wsx, capsys):
    w = wsx.world
    snapped, _ = export_timeline(w.by_stage["roughcut"], w.bounds, w.src)
    path = wsx.tmp / "rough.otio"
    path.write_text(json.dumps(export_otio(snapped, "roughcut", w.src, w.jb["roughcut"])))
    code, out, err = run(["import", wsx.ws, path], capsys)
    assert code == 0 and "against roughcut: 0 human decisions" in out
    assert "  - human stage is based on roughcut, not final: junction histories compare it with final, so changes the later stages made show as differences" in out.splitlines()


# -- structure ---------------------------------------------------------------------------------------
def test_the_interchange_package_is_imported_lazily_by_the_cli():
    code = "import sys; import matchcut.pipeline.cli as c; c.parse_args(['junctions', 'x']); c.parse_args(['export', 'x']); sys.exit(1 if any(m == 'matchcut.interchange' or m.startswith('matchcut.interchange.') for m in sys.modules) else 0)"
    assert subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[2])).returncode == 0


def test_the_interchange_package_imports_neither_the_tui_nor_the_review_package():
    code = ("import sys; import matchcut.interchange.workspace; "
            "bad = [m for m in sys.modules if m == 'textual' or m.startswith(('textual.', 'matchcut.tui', 'matchcut.review'))]; "
            "print(bad); sys.exit(1 if bad else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(Path(__file__).resolve().parents[2]), capture_output=True, text=True)
    assert done.returncode == 0, done.stdout
