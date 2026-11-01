"""M3a task 4: the ``edit`` stage through M2a: export (OTIO, FCPXML, EDL) with fidelity, re-import against the exact snapshot after a later
publish, the refusals (unresolvable, missing or ambiguous fingerprint) and ``--stage``/``--base``, and the human stage made from an edit
import (its record and its staleness)."""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from matchcut.edit import publish as P
from matchcut.edit import session as S
from matchcut.interchange import InterchangeError
from matchcut.interchange.common import export_timeline, source_info
from matchcut.interchange.edl import export_edl
from matchcut.interchange.fcpxml import export_fcpxml, fcpxml_stages
from matchcut.interchange.fidelity import differences
from matchcut.interchange.otio import export_otio
from matchcut.interchange.workspace import DEFAULT_STAGES, export_workspace, import_timeline
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines, stale_edit, stale_human
from matchcut.pipeline.shots import load_shots
from matchcut.pipeline.timeline import Timeline

RATES = ["25", "30000/1001"]
FORMATS = ["otio", "fcpxml", "edl"]


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def edit(w: Path, ops) -> str:
    return S.apply(w, S.propose(w, ops)["proposal"])["revision"]


def published(make_ws, rate: str = "25") -> SimpleNamespace:
    """A workspace whose edit stage is published: c002 trimmed, junction 3 a 6-frame dissolve."""
    w = make_ws(rate=rate)
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}, {"op": "set_transition", "junction": 3, "kind": "dissolve", "frames": 6}])
    P.publish(w)
    cfg = PipelineConfig.load(w / "config.json")
    src = source_info(cfg.source)
    bounds = {s.index: (s.start, s.end) for s in load_shots(w / "shots.json")}
    return SimpleNamespace(ws=w, cfg=cfg, src=src, bounds=bounds, rate=Fraction(rate))


def editor_files(x: SimpleNamespace, out: Path, fingerprint: str, stage: str = "edit", title: str = "") -> dict:
    """What an editor's re-export of the exported edit stage looks like: the last clip's out-point 5 frames earlier, one file per format,
    each naming ``stage`` and carrying ``fingerprint`` (none when empty)."""
    tl = Timeline.load(x.ws / "edit.json")
    snapped, _ = export_timeline(tl, x.bounds, x.src)
    ed = snapped.copy()
    ed.clips[-1].source_out = round(float(Fraction(round(ed.clips[-1].source_out * x.rate) - 5) / x.rate), 6)
    js = build_junctions(load_stage_timelines(x.ws))
    out.mkdir(parents=True, exist_ok=True)
    files = {"otio": out / "edited.otio", "fcpxml": out / "edited.fcpxml", "edl": out / "edited.edl"}
    files["otio"].write_text(json.dumps(export_otio(ed, stage, x.src, js, stage_sha256=fingerprint)))
    files["fcpxml"].write_text(export_fcpxml([(stage, ed, js)], x.src, stage_sha256={stage: fingerprint} if fingerprint else None))
    files["edl"].write_text(export_edl(ed, stage, x.src, title or f"src - {stage}", stage_sha256=fingerprint))
    return files


def trim_of_the_last_clip(decisions) -> list:
    return [(d.action, d.clip_ids, d.data) for d in decisions]


EXPECTED = [("trim", ["c005"], {"in_frames": 0, "out_frames": -5})]


def unchanged_on_refusal(ws: Path, call) -> str:
    before = {p.name: p.read_bytes() for p in ws.glob("human*.json")}
    with pytest.raises(InterchangeError) as exc:
        call()
    assert "\n" not in str(exc.value)
    assert {p.name: p.read_bytes() for p in ws.glob("human*.json")} == before
    return str(exc.value)


# ----------------------------------------------------------------------------------------------- export
@pytest.mark.parametrize("rate", RATES)
def test_the_edit_stage_exports_to_otio_fcpxml_and_edl_with_fidelity(make_ws, rate):
    x = published(make_ws, rate)
    res = export_workspace(x.ws, x.cfg, stages=("edit",))
    out = x.ws / "exports"
    digest = sha(x.ws / "edit.json")
    assert {p.name for p in res.files} >= {"edit.otio", "matchcut.fcpxml", "edit.edl", "provenance.json", "fidelity.json", "fidelity.md"}
    assert [(r.stage, r.fmt) for r in res.results] == [("edit", f) for f in FORMATS]
    assert all(r.ok and not r.errors and r.preserved for r in res.results), [r.errors for r in res.results]
    # each file carries the stage fingerprint, the sha256 of edit.json, and a snapshot of edit.json is kept
    assert (out / ".stages" / f"edit-{digest[:12]}.json").read_bytes() == (x.ws / "edit.json").read_bytes()
    assert json.loads((out / "edit.otio").read_text())["metadata"]["matchcut"]["stage_sha256"] == digest
    assert f"* MATCHCUT STAGE_SHA256 {digest}" in (out / "edit.edl").read_text().splitlines()
    assert "* MATCHCUT STAGE edit" in (out / "edit.edl").read_text().splitlines()
    assert fcpxml_stages((out / "matchcut.fcpxml").read_text()) == ["edit"] and digest in (out / "matchcut.fcpxml").read_text()
    report = json.loads((out / "fidelity.json").read_text())
    assert set(report["stages"]) == {"edit"} and report["ok"] is True
    prov = json.loads((out / "provenance.json").read_text())
    assert prov["stages"]["edit"]["canonical_sha256"] == digest
    # what an editor reads from each file is the edit stage, frame for frame
    snapped, _ = export_timeline(Timeline.load(x.ws / "edit.json"), x.bounds, x.src)
    for fmt, f in {"otio": out / "edit.otio", "fcpxml": out / "matchcut.fcpxml", "edl": out / "edit.edl"}.items():
        res_i = import_timeline(x.ws, x.cfg, f)
        assert res_i.stage == "edit" and res_i.decisions == [], (fmt, res_i.notes)
    assert not differences(snapped, Timeline.load(x.ws / "human.json"), x.src.rate, 0)


def test_edit_is_exported_with_the_canonical_stages_and_never_by_default(make_ws):
    x = published(make_ws)
    assert "edit" not in DEFAULT_STAGES  # like human, which is never exported, the edit stage is exported when named
    export_workspace(x.ws, x.cfg)
    assert not (x.ws / "exports" / "edit.otio").exists()
    res = export_workspace(x.ws, x.cfg, stages=("roughcut", "final", "edit"))
    assert [r.stage for r in res.results] == ["roughcut"] * 3 + ["final"] * 3 + ["edit"] * 3 and all(r.ok for r in res.results)
    prov = json.loads((x.ws / "exports" / "provenance.json").read_text())
    assert prov["stage_order"] == ["roughcut", "final", "edit"]
    assert fcpxml_stages((x.ws / "exports" / "matchcut.fcpxml").read_text()) == ["roughcut", "final", "edit"]
    # an export of final alone keeps the edit stage's records while edit.json is unchanged
    res = export_workspace(x.ws, x.cfg, stages=("final",), formats=("otio",))
    assert any("provenance edit" in n for n in res.notes)
    fid = json.loads((x.ws / "exports" / "fidelity.json").read_text())
    assert "edit" in fid["stages"] and not any(d.get("stale") for d in fid["stages"]["edit"].values())


def test_exporting_a_missing_or_stale_edit_stage_is_refused(make_ws):
    w = make_ws()
    cfg = PipelineConfig.load(w / "config.json")
    with pytest.raises(InterchangeError, match=r"cannot export the edit stage: no edit.json in .*publish an edit session first"):
        export_workspace(w, cfg, stages=("edit",))
    S.start(w)
    edit(w, [{"op": "remove", "clip": "c003"}])
    P.publish(w)
    final = Timeline.load(w / "final.json")
    final.clips[0].source_in += 0.04
    final.save(w / "final.json")
    with pytest.raises(InterchangeError, match="cannot export the edit stage: edit.json is stale: final.json changed since it was published"):
        export_workspace(w, cfg, stages=("edit",))
    res = export_workspace(w, cfg)  # the other stages export, and the note says why edit is left out
    assert any(n.startswith("edit.json is stale") for n in res.notes)


# ----------------------------------------------------------------------------------------------- import against the exact snapshot
@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("rate", RATES)
def test_a_reimport_is_diffed_against_the_exact_snapshot_after_a_later_publish(make_ws, tmp_path, rate, fmt):
    x = published(make_ws, rate)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    exported_sha = sha(x.ws / "edit.json")
    files = editor_files(x, tmp_path / "editor", exported_sha)
    # the agent publishes again: edit.json changes after the export
    edit(x.ws, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    P.publish(x.ws)
    assert sha(x.ws / "edit.json") != exported_sha
    res = import_timeline(x.ws, x.cfg, files[fmt])
    assert res.stage == "edit" and trim_of_the_last_clip(res.decisions) == EXPECTED, [d.detail for d in res.decisions]  # not the agent's later trim
    assert any("was exported from an earlier edit (edit.json was published again since)" in n for n in res.notes), res.notes
    rec = json.loads((x.ws / "human_import.json").read_text())
    snap = x.ws / "exports" / ".stages" / f"edit-{exported_sha[:12]}.json"
    assert (rec["stage"], rec["stage_file"], rec["stage_sha256"]) == ("edit", "edit.json", sha(x.ws / "edit.json"))
    assert (rec["base_snapshot"], rec["base_snapshot_sha256"]) == (str(snap), exported_sha)
    assert (rec["latest_stage"], rec["latest_stage_file"], rec["latest_stage_sha256"]) == ("final", "final.json", sha(x.ws / "final.json"))
    assert (rec["history_stage"], rec["history_stage_sha256"]) == ("edit", sha(x.ws / "edit.json"))
    # the human stage is listed after the edit stage and continues its (current) history
    assert stale_human(x.ws) is None and [s for s, _ in load_stage_timelines(x.ws)][-3:] == ["final", "edit", "human"]
    now = Timeline.load(x.ws / "edit.json")
    human = Timeline.load(x.ws / "human.json")
    assert [d.to_dict() for d in human.history[: len(now.history)]] == [d.to_dict() for d in now.history]
    assert [(d.pass_name, d.action) for d in human.history[len(now.history):]] == [("human", "trim")]


def test_the_human_stage_from_an_edit_import_is_stale_when_edit_json_changes(make_ws, tmp_path):
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    files = editor_files(x, tmp_path / "editor", sha(x.ws / "edit.json"))
    res = import_timeline(x.ws, x.cfg, files["otio"])
    assert trim_of_the_last_clip(res.decisions) == EXPECTED and not any("earlier" in n for n in res.notes)
    rec = json.loads((x.ws / "human_import.json").read_text())
    assert rec["stage"] == "edit" and "base_snapshot" not in rec  # edit.json is still the exported version
    kept = (x.ws / "human.json").read_bytes()
    edit(x.ws, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    P.publish(x.ws, supersede_human=True)  # (the human stage continues the old edit.json)
    assert stale_human(x.ws) == ("human.json is stale: edit.json changed since the import (re-export, or re-import the editor's file: matchcut "
                                 "diffs it against the stage it was exported from)")
    assert [s for s, _ in load_stage_timelines(x.ws)][-1] == "edit" and (x.ws / "human.json").read_bytes() == kept


def test_the_human_stage_from_an_edit_import_is_stale_when_the_latest_canonical_stage_changes(make_ws, tmp_path):
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    import_timeline(x.ws, x.cfg, editor_files(x, tmp_path / "editor", sha(x.ws / "edit.json"))["edl"])
    assert stale_human(x.ws) is None
    final = Timeline.load(x.ws / "final.json")
    final.clips[0].source_in += 0.04
    final.save(x.ws / "final.json")
    assert stale_human(x.ws).startswith("human.json is stale: final.json changed since the import")
    assert stale_edit(x.ws).startswith("edit.json is stale: final.json changed")
    assert [s for s, _ in load_stage_timelines(x.ws)][-1] == "final"


def test_an_edit_import_while_the_edit_stage_is_stale_is_refused(make_ws, tmp_path):
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    files = editor_files(x, tmp_path / "editor", sha(x.ws / "edit.json"))
    (x.ws / "edit_publish.json").unlink()
    msg = unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, files["otio"]))
    assert msg.startswith("cannot import against the edit stage: edit.json is stale: edit_publish.json is missing or unreadable")
    assert msg.endswith("the human stage would be stale at once")


def test_a_final_import_in_a_workspace_with_an_edit_stage_continues_the_edit_history(make_ws, tmp_path):
    """The human stage is listed after the edit stage: its history must continue the edit stage's, whichever stage it was compared with."""
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("final",))
    out = x.ws / "exports" / "final.otio"
    res = import_timeline(x.ws, x.cfg, out)
    assert res.stage == "final" and res.decisions == []
    assert "human stage is based on final, not edit: junction histories compare it with edit, so changes the later stages made show as differences" in res.notes
    rec = json.loads((x.ws / "human_import.json").read_text())
    assert (rec["stage"], rec["history_stage"], rec["history_stage_sha256"]) == ("final", "edit", sha(x.ws / "edit.json"))
    edit_tl = Timeline.load(x.ws / "edit.json")
    assert [d.to_dict() for d in Timeline.load(x.ws / "human.json").history] == [d.to_dict() for d in edit_tl.history]
    assert stale_human(x.ws) is None and [s for s, _ in load_stage_timelines(x.ws)][-2:] == ["edit", "human"]


# ----------------------------------------------------------------------------------------------- refusals, and --stage / --base
def test_an_unresolvable_edit_fingerprint_is_refused_never_guessed(make_ws, tmp_path):
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    bogus = hashlib.sha256(b"an edit stage nobody exported").hexdigest()
    files = editor_files(x, tmp_path / "editor", bogus)
    for fmt in FORMATS:
        msg = unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, files[fmt]))
        assert msg.startswith(f"edited.{fmt} was exported from an edit stage (sha {bogus[:12]}) that matches no snapshot (edit-{bogus[:12]}.json is not in "
                              "exports/.stages or "), msg
        assert "matchcut does not guess the base" in msg and "--stage and --base SNAPSHOT" in msg


@pytest.mark.parametrize("fmt", FORMATS)
def test_a_file_without_a_fingerprint_is_refused_once_there_is_an_edit_stage(make_ws, tmp_path, fmt):
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    exported_sha = sha(x.ws / "edit.json")
    # the editor renamed the timeline: no stage name, no fingerprint (an EDL whose title changed)
    files = editor_files(x, tmp_path / "editor", "", stage="my cut", title="my cut")
    msg = unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, files[fmt]))
    assert msg == ("edited.{} carries no stage fingerprint, and this workspace has an edit stage as well as final: the base is ambiguous; "
                   "name the base with --stage and --base SNAPSHOT (a file in exports/.stages)").format(fmt)
    # --stage alone does not name the version
    unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, files[fmt], stage="edit"))
    # --stage and --base do, even after a later publish
    edit(x.ws, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    P.publish(x.ws)
    res = import_timeline(x.ws, x.cfg, files[fmt], stage="edit", base=f"edit-{exported_sha[:12]}")
    assert res.stage == "edit" and trim_of_the_last_clip(res.decisions) == EXPECTED
    rec = json.loads((x.ws / "human_import.json").read_text())
    assert rec["base_snapshot"] == str(x.ws / "exports" / ".stages" / f"edit-{exported_sha[:12]}.json") and rec["base_snapshot_sha256"] == exported_sha


def test_an_ambiguous_base_is_refused_unless_named(make_ws, tmp_path):
    """A file that names edit but carries final's fingerprint, or the reverse: matchcut does not pick one."""
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("final", "edit"))
    final_sha, edit_sha = sha(x.ws / "final.json"), sha(x.ws / "edit.json")
    names_edit = editor_files(x, tmp_path / "a", final_sha, stage="edit")["otio"]
    msg = unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, names_edit))
    assert msg == (f"edited.otio names stage edit, but its fingerprint (sha {final_sha[:12]}) is that of stage final: the base is ambiguous; "
                   "name the base with --stage and --base SNAPSHOT (a file in exports/.stages)")
    names_final = editor_files(x, tmp_path / "b", edit_sha, stage="final")["edl"]
    msg = unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, names_final))
    assert msg.startswith(f"edited.edl names stage final, but its fingerprint (sha {edit_sha[:12]}) is that of stage edit: the base is ambiguous")
    # --stage against the fingerprint is refused too: only --base names the version
    unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, names_final, stage="final"))
    res = import_timeline(x.ws, x.cfg, names_final, stage="edit", base=str(x.ws / "exports" / ".stages" / f"edit-{edit_sha[:12]}.json"))
    assert res.stage == "edit" and trim_of_the_last_clip(res.decisions) == EXPECTED
    assert "the file says it was exported from stage final; compared with edit as asked" in res.notes
    # a base of another stage than --stage is refused
    msg = unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, names_edit, stage="edit", base=f"final-{final_sha[:12]}.json"))
    assert msg.endswith("is a final stage timeline, not edit: name the base the file was exported from")
    with pytest.raises(InterchangeError, match="--base names the version of a stage: give the stage too"):
        import_timeline(x.ws, x.cfg, names_edit, base=f"edit-{edit_sha[:12]}")
    with pytest.raises(InterchangeError, match="--base nothing-here: no such file"):
        import_timeline(x.ws, x.cfg, names_edit, stage="edit", base="nothing-here")


def test_a_canonical_import_with_its_own_fingerprint_still_works_with_an_edit_stage(make_ws):
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("final",))
    res = import_timeline(x.ws, x.cfg, x.ws / "exports" / "final.edl")
    assert res.stage == "final" and res.decisions == []
    assert [s for s, _ in load_stage_timelines(x.ws)][-3:] == ["final", "edit", "human"] and stale_human(x.ws) is None


def test_a_fingerprint_names_the_base_when_the_file_names_no_stage(make_ws, tmp_path):
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    files = editor_files(x, tmp_path / "editor", sha(x.ws / "edit.json"), stage="my cut", title="my cut")
    res = import_timeline(x.ws, x.cfg, files["otio"])
    assert res.stage == "edit" and trim_of_the_last_clip(res.decisions) == EXPECTED
    assert "the file does not say which stage it was exported from; its fingerprint is that of a edit file" not in res.notes
    assert "the file does not say which stage it was exported from; its fingerprint is that of stage edit" in res.notes


# ----------------------------------------------------------------------------------------------- end to end, through the CLIs only
def test_an_agent_session_through_the_clis_start_propose_apply_publish_export_and_reimport(make_ws, tmp_path):
    import os
    import subprocess
    import sys

    from .test_cli import REPO, run_cli
    w = make_ws()
    assert run_cli(str(w), "start")[1]["base_stage"] == "final"
    code, p, _ = run_cli(str(w), "propose", json.dumps({"ops": [{"op": "remove", "clip": "c003"}, {"op": "trim", "clip": "c004", "edge": "in", "frames": 3}]}))
    assert code == 0 and p["problems"] == []
    assert run_cli(str(w), "apply", json.dumps({"proposal": p["proposal"]}))[1]["revision"] == "r001"
    code, pub, _ = run_cli(str(w), "publish")
    assert code == 0 and pub["revision"] == "r001" and pub["decisions"] == 2 and (w / pub["published"]).exists()
    code, again, _ = run_cli(str(w), "publish", '{"revision": "r000"}')
    assert code == 1 and again["kind"] == "refused" and again["error"].startswith("nothing to publish")
    code, bad, _ = run_cli(str(w), "publish", '{"replace": "yes"}')
    assert code == 2 and bad["kind"] == "usage"

    def pipeline_cli(*argv: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "matchcut.pipeline.cli", *argv], capture_output=True, text=True, timeout=300,
                              env=dict(os.environ, PYTHONPATH=str(REPO)), cwd=str(REPO))

    out = pipeline_cli("junctions", str(w))
    assert out.returncode == 0 and out.stdout.splitlines()[0] == "3 junctions in edit (4 stages)", out.stdout + out.stderr
    out = pipeline_cli("export", str(w), "--stage", "edit")
    assert out.returncode == 0, out.stderr
    assert "edit  otio  ok" in out.stdout and "edit  fcpxml  ok" in out.stdout and "edit  edl  ok" in out.stdout
    exported_sha = sha(w / "edit.json")
    # the editor trims the last clip in the exported EDL (5 frames off its source and record out); the agent publishes again meanwhile
    x = SimpleNamespace(ws=w, cfg=PipelineConfig.load(w / "config.json"), rate=Fraction(25))
    x.src = source_info(x.cfg.source)
    x.bounds = {s.index: (s.start, s.end) for s in load_shots(w / "shots.json")}
    files = editor_files(x, tmp_path / "editor", exported_sha)
    p = run_cli(str(w), "propose", json.dumps({"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": -4}]}))[1]
    run_cli(str(w), "apply", json.dumps({"proposal": p["proposal"]}))
    assert run_cli(str(w), "publish")[1]["previous"] == "r001"
    out = pipeline_cli("import", str(w), str(files["edl"]))
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[0].startswith("imported edited.edl (edl) against edit: 1 human decision")
    assert "trim c005" in out.stdout and "exported from an earlier edit" in out.stdout
    # a renamed EDL (no fingerprint) is refused, then imported with --stage and --base
    renamed = editor_files(x, tmp_path / "renamed", "", stage="my cut", title="my cut")["edl"]
    out = pipeline_cli("import", str(w), str(renamed))
    assert out.returncode == 1 and "carries no stage fingerprint" in out.stderr and "--base SNAPSHOT" in out.stderr
    out = pipeline_cli("import", str(w), str(renamed), "--stage", "edit", "--base", f"edit-{exported_sha[:12]}")
    assert out.returncode == 0, out.stderr
    assert json.loads((w / "human_import.json").read_text())["base_snapshot_sha256"] == exported_sha
    out = pipeline_cli("junctions", str(w))
    assert out.stdout.splitlines()[0] == "3 junctions in human (5 stages)"


# ----------------------------------------------------------------------------------------------- review fixes
def test_an_edl_whose_title_alone_was_renamed_still_imports_by_its_matchcut_comments(make_ws, tmp_path):
    """The EDL's stage and fingerprint are its ``* MATCHCUT`` comment lines, not its title: a renamed title alone still imports."""
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    edl = (x.ws / "exports" / "edit.edl").read_text()
    renamed = tmp_path / "renamed.edl"
    renamed.write_text("\n".join(("TITLE: my cut" if ln.startswith("TITLE:") else ln) for ln in edl.splitlines()) + "\n")
    res = import_timeline(x.ws, x.cfg, renamed)
    assert res.stage == "edit" and res.decisions == []


def test_an_edl_without_its_matchcut_comments_is_refused_once_there_is_an_edit_stage(make_ws, tmp_path):
    """An editor that drops comments: no stage, no fingerprint; refused until --stage/--base name the base."""
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    edl = (x.ws / "exports" / "edit.edl").read_text()
    stripped = tmp_path / "stripped.edl"
    stripped.write_text("\n".join(ln for ln in edl.splitlines() if not ln.lstrip().startswith("* MATCHCUT")) + "\n")
    msg = unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, stripped))
    assert msg.startswith("stripped.edl carries no stage fingerprint, and this workspace has an edit stage as well as final")
    res = import_timeline(x.ws, x.cfg, stripped, stage="edit", base=f"edit-{sha(x.ws / 'edit.json')[:12]}")
    assert res.stage == "edit" and res.decisions == []


def test_a_file_without_a_fingerprint_beside_a_stale_edit_stage_is_refused_with_a_working_example(make_ws, tmp_path, monkeypatch):
    """Only a stale edit.json: the refusal says the edit stage is stale (why the base is ambiguous), gives a --base that works, with the
    workspace's real path, and says how a relative --base is resolved."""
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("final",))
    edl = (x.ws / "exports" / "final.edl").read_text()
    stripped = tmp_path / "stripped.edl"
    stripped.write_text("\n".join(ln for ln in edl.splitlines() if not ln.lstrip().startswith("* MATCHCUT")) + "\n")
    (x.ws / "edit_publish.json").write_text("[]")  # the edit stage is stale now
    msg = unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, stripped))
    assert msg.startswith("stripped.edl carries no stage fingerprint, and this workspace has an edit stage that is stale (edit.json is "
                          "stale: edit_publish.json is missing or unreadable) as well as final: the file may have been exported from either, "
                          "so the base is ambiguous")
    example = f"--stage final --base {x.ws / 'final.json'}"
    assert example in msg and "current directory, then in exports/.stages" in msg
    res = import_timeline(x.ws, x.cfg, stripped, stage="final", base=str(x.ws / "final.json"))  # the example works
    assert res.stage == "final" and res.decisions == []


@pytest.mark.parametrize("title", ["Interview - edit", "edit"])
def test_without_an_edit_stage_a_title_naming_edit_imports_as_before(make_ws, tmp_path, title):
    """A workspace that never had an edit stage: an EDL titled "... - edit" with its comments dropped is read against final, with the note,
    exactly as before M3a."""
    w = make_ws()
    cfg = PipelineConfig.load(w / "config.json")
    export_workspace(w, cfg)
    edl = (w / "exports" / "final.edl").read_text()
    f = tmp_path / "cut.edl"
    f.write_text("\n".join((f"TITLE: {title}" if ln.startswith("TITLE:") else ln) for ln in edl.splitlines()
                           if not ln.lstrip().startswith("* MATCHCUT STAGE")) + "\n")
    res = import_timeline(w, cfg, f)
    assert res.stage == "final" and any("carries no stage fingerprint" in n for n in res.notes)
    assert "history_stage" not in json.loads((w / "human_import.json").read_text())
    # an OTIO named "... - edit" with final's fingerprint is final's, not ambiguous
    doc = json.loads((w / "exports" / "final.otio").read_text())
    doc["name"] = "Interview - edit"
    doc["metadata"]["matchcut"].pop("stage", None)
    (tmp_path / "cut.otio").write_text(json.dumps(doc))
    assert import_timeline(w, cfg, tmp_path / "cut.otio").stage == "final"
    # and --stage edit is unknown there, as before
    with pytest.raises(InterchangeError, match="no stage edit in"):
        import_timeline(w, cfg, f, stage="edit")


def test_base_names_a_snapshot_in_exports_stages_before_the_current_directory(make_ws, tmp_path, monkeypatch):
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    name = f"edit-{sha(x.ws / 'edit.json')[:12]}"
    decoy = Timeline.load(x.ws / "edit.json")
    decoy.clips[-1].source_out -= 0.4
    (tmp_path / name).write_text(json.dumps(decoy.to_dict()))
    (tmp_path / f"{name}.json").write_text(json.dumps(decoy.to_dict()))
    monkeypatch.chdir(tmp_path)
    for base in (name, f"{name}.json"):
        res = import_timeline(x.ws, x.cfg, x.ws / "exports" / "edit.edl", stage="edit", base=base)
        assert res.decisions == []
        rec = json.loads((x.ws / "human_import.json").read_text())
        assert rec["base_snapshot"] == str((x.ws / "exports" / ".stages" / f"{name}.json").resolve())
    # a relative path that is not a snapshot's name is resolved and recorded absolute
    res = import_timeline(x.ws, x.cfg, x.ws / "exports" / "edit.edl", stage="edit", base=f"./{name}.json")
    assert json.loads((x.ws / "human_import.json").read_text())["base_snapshot"] == str((tmp_path / f"{name}.json").resolve())
    assert res.decisions  # the decoy is another cut: the editor's file differs from it


def test_a_snapshot_whose_content_does_not_match_its_name_is_refused_as_a_base(make_ws):
    x = published(make_ws)
    export_workspace(x.ws, x.cfg, stages=("edit",))
    snap = x.ws / "exports" / ".stages" / f"edit-{sha(x.ws / 'edit.json')[:12]}.json"
    tl = Timeline.load(snap)
    tl.name = "changed by hand"
    snap.write_text(json.dumps(tl.to_dict()))
    msg = unchanged_on_refusal(x.ws, lambda: import_timeline(x.ws, x.cfg, x.ws / "exports" / "edit.edl", stage="edit", base=snap.stem))
    assert "is damaged (its content does not hash to the" in msg
