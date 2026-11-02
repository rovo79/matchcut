"""A transition protection the editor set is the user's to change: ``start`` records every pin's protection in r000 (the session's
baseline), ``publish`` refuses a protection released, replaced, weakened or cleared since, unless the user acknowledges it, and while a
session exists ``pin_transition`` refuses to release or replace a protection without ``user_approved`` naming the pin.  Nothing reads
"held" for such a pin.  Also: the re-run command (absolute, ``--keep-config``) and a relative source path."""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

from matchcut.edit import publish as P
from matchcut.edit import session as S
from matchcut.edit import tools as T
from matchcut.edit import view as V
from matchcut.edit.model import Refused
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import PinStore
from matchcut.pipeline.timeline import Timeline

from .test_cli import REPO, run_cli
from .test_pin_safety import edit, mismatch, pin_at

ELSEWHERE = [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}]


@pytest.fixture
def protected(make_ws) -> Path:
    """The reviewer's m2/m3 world: final holds the pin s0->s1 with a cut, the pin protects a dissolve of 4 (an inherited mismatch); a
    session started and an unrelated edit applied."""
    w = make_ws()
    mismatch(w)
    S.start(w)
    edit(w, ELSEWHERE)
    return w


def footer(w: Path, capsys, *extra: str):
    from matchcut.pipeline.cli import main
    capsys.readouterr()
    assert main(["junctions", str(w), *extra]) == 0
    return capsys.readouterr().out.splitlines()


def never_held(w: Path, capsys, out=None) -> None:
    """inspect, the publish result, the junctions list line and footer, and the TUI's pin note: no word says the pin is held."""
    from matchcut.tui.views.edit import EditView
    for p in V.inspect(w)["pins"]:
        if p["pin"] == "s0->s1":
            assert p["state"] != "held" and p["held"] is False and "held by" not in p["reason"], p
    if out is not None:
        assert [x["state"] for x in out["pins"] if x["pin"] == "s0->s1"] == ["protection_change_acknowledged"]
        lines = [ln for ln in footer(w, capsys) if "s0->s1" in ln]
        assert lines[0].endswith("PROTECTION-CHANGED"), lines
        assert lines[-1].startswith("pin s0->s1: clips and cut points kept by this edit, but the transition protection it had when the "
                                    "edit session started (a dissolve of 4 frames) was ")
        assert "held" not in lines[-1] and "is NOT kept" in lines[-1] and "(protection change acknowledged at publish)" in lines[-1]
        state = SimpleNamespace(workspace=w, timeline=lambda stage: Timeline.load(w / f"{stage}.json"))
        pin = PinStore(w).get(0, 1)
        if pin is not None:
            note = EditView._pin_note(SimpleNamespace(_state=state), pin, "edit")
            assert note == lines[-1].split("but ", 1)[1] and "held" not in note


# ----------------------------------------------------------------------------------------------- the pin_transition guard
@pytest.mark.parametrize("kw, what", [({"hold": False}, "release"), ({"hold": True}, "replace it with a cut")])
def test_releasing_or_replacing_a_protection_during_a_session_needs_user_approved(protected, kw, what):
    w = protected
    before = (w / "editorial.json").read_bytes()
    with pytest.raises(Refused, match=rf"pin s0->s1 protects a dissolve of 4 frames, which the editor chose.*only the user may release or "
                                      rf"change that protection\. Ask the user; with their explicit approval to {what}, run pin_transition "
                                      r'again with user_approved: "s0->s1"'):
        T.call(w, "pin_transition", dict(pin="s0->s1", **kw))
    with pytest.raises(Refused, match=r"user_approved names s1->s3, but this changes pin s0->s1"):
        T.call(w, "pin_transition", dict(pin="s0->s1", user_approved="s1->s3", **kw))
    assert (w / "editorial.json").read_bytes() == before
    out = T.call(w, "pin_transition", dict(pin="s0->s1", user_approved="s0->s1", **kw))
    assert out["changed"] and out["user_approved"] == "s0->s1" and "publish refuses it until the user also acknowledges" in out["note"]


def test_protecting_an_unprotected_pin_or_restating_a_protection_needs_no_approval(make_ws):
    w = make_ws()
    mismatch(w)
    S.start(w)
    assert not V.pin_transition(w, pin="s0->s1", hold=True, kind="dissolve", frames=4)["changed"]  # the same protection: allowed
    PinStore(w).put(pin_at(w, 1))  # s1->s3, unprotected
    out = V.pin_transition(w, pin="s1->s3", hold=True, kind="cut")
    assert out["changed"] and out["user_approved"] is None
    assert V.pin_transition(w, pin="s1->s3", hold=False, user_approved="s1->s3")["changed"]  # an approved release


def test_the_tool_and_its_schema_say_it_needs_the_users_explicit_approval():
    tool = T.get("pin_transition")
    assert "never without the user's explicit approval" in tool.description
    assert tool.schema["properties"]["user_approved"]["pattern"] == T.PIN_LABEL
    assert "explicit approval" in tool.schema["properties"]["user_approved"]["description"]


# ----------------------------------------------------------------------------------------------- publish against the baseline
def test_start_records_the_baseline_in_r000_under_its_hash(make_ws):
    w = make_ws()
    mismatch(w)
    out = S.start(w)
    assert out["pin_protections"] == {"s0->s1": {"hold_transition": True, "held_transition": {"kind": "dissolve", "frames": 4}}}
    doc = json.loads((w / "edits" / "revisions" / "r000.json").read_text())
    assert doc["pin_protections"] == out["pin_protections"]
    doc["pin_protections"] = {}
    (w / "edits" / "revisions" / "r000.json").write_text(json.dumps(doc))
    with pytest.raises(Refused, match="revision r000 was changed after it was written"):
        S.load(w).revision("r000")


def test_m2_an_approved_release_is_refused_at_publish_until_acknowledged_and_never_reads_held(protected, capsys):
    w = protected
    with pytest.raises(Refused, match=r"r001 cannot be published as protected: pin s0->s1 holds a dissolve 4f"):
        P.publish(w)
    V.pin_transition(w, pin="s0->s1", hold=False, user_approved="s0->s1")
    ins = V.inspect(w)
    assert [(c["pin"], c["change"]) for c in ins["protection_changes"]] == [("s0->s1", "released")]
    assert [(p["state"], p["held"], p["clips_held"]) for p in ins["pins"]] == [("protection_changed", False, True)]
    assert ins["junctions"][0]["protection_change"] == "released"
    with pytest.raises(Refused, match=r"r001 cannot be published as protected: the transition protection pin s0->s1 had when this edit "
                                      r"session started \(a dissolve of 4 frames\) was released during the session .*publish "
                                      r'\{"acknowledge_mismatches": \["s0->s1"\]\}.*An agent must never acknowledge'):
        P.publish(w)
    with pytest.raises(Refused, match=r"it does not name s0->s1"):
        P.publish(w, acknowledge_mismatches=["s9->s9"])
    assert not (w / "edit.json").exists()
    out = P.publish(w, acknowledge_mismatches=["s0->s1"])
    [c] = out["acknowledged_protection_changes"]
    assert c["change"] == "released" and c["original"] == {"hold_transition": True, "held_transition": {"kind": "dissolve", "frames": 4}}
    assert c["now"] == {"hold_transition": False, "held_transition": None} and c["transition"] == {"kind": "cut", "frames": 0}
    assert out["transition_mismatches"] == [] and out["pin_protections_at_start"]["s0->s1"]["hold_transition"] is True
    rec = json.loads((w / "edit_publish.json").read_text())
    assert rec["acknowledged_protection_changes"] == out["acknowledged_protection_changes"]
    log = json.loads((w / "edits" / "published" / "log.jsonl").read_text().splitlines()[-1])
    assert log["acknowledged_protection_changes"][0]["change"] == "released"
    never_held(w, capsys, out)


def test_m3_reprotecting_the_current_cut_through_the_cli_is_refused_then_acknowledged(protected, capsys):
    w = protected
    code, doc, _ = run_cli(str(w), "pin_transition", '{"pin": "s0->s1", "hold": true}')
    assert code == 1 and doc["kind"] == "refused"
    assert 'user_approved: "s0->s1"' in doc["error"] and "only the user may release or change" in doc["error"]
    code, doc, _ = run_cli(str(w), "pin_transition", '{"pin": "s0->s1", "hold": true, "user_approved": "s0->s1"}')
    assert code == 0 and doc["held_transition"] == {"kind": "cut", "frames": 0}
    code, doc, _ = run_cli(str(w), "publish")
    assert code == 1 and "was replaced during the session: the pin protects a cut now" in doc["error"]
    code, doc, _ = run_cli(str(w), "publish", '{"acknowledge_mismatches": ["s0->s1"]}')
    assert code == 0 and doc["transition_mismatches"] == [] and doc["acknowledged_protection_changes"][0]["change"] == "replaced"
    assert doc["acknowledged_protection_changes"][0]["now"]["held_transition"] == {"kind": "cut", "frames": 0}
    never_held(w, capsys, doc)


def test_a_pin_cleared_during_the_session_is_a_protection_change(protected, capsys):
    w = protected
    PinStore(w).remove(0, 1)  # x in the TUI
    with pytest.raises(Refused, match=r"was cleared with the pin itself during the session"):
        P.publish(w)
    out = P.publish(w, acknowledge_mismatches=["s0->s1"])
    assert [(p["pin"], p["state"]) for p in out["pins"]] == [("s0->s1", "protection_change_acknowledged")]
    lines = footer(w, capsys)
    assert any(ln.startswith("pin s0->s1: cleared during the edit session; the transition protection") for ln in lines)


def test_restoring_the_protection_publishes_without_acknowledgement(protected):
    w = protected
    V.pin_transition(w, pin="s0->s1", hold=False, user_approved="s0->s1")
    edit(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])
    with pytest.raises(Refused, match="was released during the session"):  # the cut has the old transition, the protection is gone
        P.publish(w)
    V.pin_transition(w, pin="s0->s1", hold=True)  # protecting an unprotected pin: no approval needed; it is the baseline again
    out = P.publish(w)
    assert out["acknowledged_protection_changes"] == [] and [p["state"] for p in out["pins"]] == ["held"]


def test_restart_takes_a_new_baseline(protected):
    w = protected
    V.pin_transition(w, pin="s0->s1", hold=False, user_approved="s0->s1")
    out = S.start(w, restart=True)
    assert out["pin_protections"] == {"s0->s1": {"hold_transition": False, "held_transition": None}}
    edit(w, ELSEWHERE)
    assert P.publish(w)["acknowledged_protection_changes"] == []
    # and the other way round: a protection made before the restart is the new baseline, and releasing it is a change again
    V.pin_transition(w, pin="s0->s1", hold=True)
    S.start(w, restart=True)
    edit(w, ELSEWHERE)
    V.pin_transition(w, pin="s0->s1", hold=False, user_approved="s0->s1")
    with pytest.raises(Refused, match="was released during the session"):
        P.publish(w, replace=True)


def test_a_session_without_a_baseline_cannot_publish(protected):
    w = protected
    path = w / "edits" / "revisions" / "r000.json"
    doc = json.loads(path.read_text())
    del doc["pin_protections"]
    doc["document_sha256"] = S.document_sha256(doc)
    path.write_text(json.dumps(doc))
    with pytest.raises(Refused, match=r"started before matchcut recorded the pins' transition protections.*ask the user to restart"):
        P.publish(w, acknowledge_mismatches=["s0->s1"])


def test_the_mismatch_repair_offers_set_transition_first_and_leaves_the_protection_to_the_user(protected):
    w = protected
    [m] = V.inspect(w, "r000")["transition_mismatches"]
    r = m["repair"]
    assert r.index("set_transition") < r.index("ask the user") and "only they may release or change the protection" in r
    assert "if the editor agrees" not in r


# ----------------------------------------------------------------------------------------------- the re-run command, relative sources
def test_the_rerun_command_is_absolute_keeps_the_config_and_runs_from_anywhere(make_ws, tmp_path, monkeypatch):
    w = make_ws()
    cmd = S.rerun_command(w)
    argv = shlex.split(cmd)
    src = PipelineConfig.load(w / "config.json").source
    assert argv == [f"PYTHONPATH={REPO}", "python", "-m", "matchcut.pipeline", "refine", str(Path(src).resolve()), "-w", str(w.resolve()),
                    "--keep-config"]
    # a relative source found from the current directory: absolute in the command
    monkeypatch.chdir(Path(src).parent)
    cfg = PipelineConfig.load(w / "config.json")
    cfg.source = Path(src).name
    cfg.save(w / "config.json")
    assert shlex.split(S.rerun_command(w))[5] == str(Path(src).resolve()) and S.relative_source_hint(w) == ""
    # ... not found from here: kept as written, and the hint says so
    monkeypatch.chdir(tmp_path)
    assert shlex.split(S.rerun_command(w))[5] == Path(src).name
    hint = S.relative_source_hint(w)
    assert f"relative path {Path(src).name!r}" in hint and str(tmp_path) in hint and "set an absolute source" in hint


def test_a_relative_source_is_found_from_the_directory_the_run_was_made_in(make_ws, tmp_path, monkeypatch):
    w = make_ws()
    src = Path(PipelineConfig.load(w / "config.json").source)
    root = w.parent
    os.link(src, root / "footage.mp4") if not (root / "footage.mp4").exists() else None
    cfg = PipelineConfig.load(w / "config.json")
    cfg.source, cfg.workspace = "footage.mp4", w.name  # as `python -m matchcut.pipeline run footage.mp4` run in root stores them
    cfg.save(w / "config.json")
    monkeypatch.chdir(tmp_path)
    assert S.resolve_source(w) == ((root / "footage.mp4").resolve(), "footage.mp4")


def test_start_refuses_an_unresolved_relative_source_naming_the_path_and_the_directory(make_ws, tmp_path, monkeypatch):
    w = make_ws()
    cfg = PipelineConfig.load(w / "config.json")
    cfg.source = "footage.mp4"
    cfg.save(w / "config.json")
    probe = json.loads((w / "probe.json").read_text())
    probe["path"] = "footage.mp4"
    (w / "probe.json").write_text(json.dumps(probe))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Refused, match=r"cannot read the source footage: .*relative path 'footage.mp4'.* not found from "
                                      + str(tmp_path) + r".*set an absolute source"):
        S.start(w)


def test_unapplied_actions_ask_the_user_to_rerun_or_restart(make_ws):
    w = make_ws()
    PinStore(w).put(pin_at(w, 1))
    [e] = S.start(w)["unapplied_pins"]
    assert e["action"].startswith("to protect it, ask the user to re-run the pipeline") and "then to restart the edit session" in e["action"]
    assert "an agent never does either on its own" in e["action"] and "--keep-config" in e["action"]


def test_a_released_pin_on_a_human_base_offers_clearing_it_and_says_a_rerun_makes_human_stale(make_ws):
    """The ``released`` kind: the pipeline applied the pin, then the editor moved its cut in human.json."""
    from matchcut.interchange.workspace import export_workspace, import_timeline
    from matchcut.pipeline.editorial import apply_pins
    from matchcut.pipeline.timeline import clip_origin
    w = make_ws()
    tl = Timeline.load(w / "final.json")
    for x in tl.clips:
        x.origin = clip_origin(x)
    ctx = S.workspace_context(w)
    pin = pin_at(w, 1)
    out, _ = apply_pins(tl, [pin], ctx.bounds, ctx.min_clip_seconds)
    out.save(w / "final.json")
    PinStore(w).put(pin)
    cfg = PipelineConfig.load(w / "config.json")
    export_workspace(w, cfg, stages=("final",), formats=("otio",))
    import_timeline(w, cfg, w / "exports" / "final.otio")
    h = json.loads((w / "human.json").read_text())
    for c in h["clips"]:
        if c["shot_index"] == 1:
            c["source_out"] = round(c["source_out"] - 0.2, 6)
    (w / "human.json").write_text(json.dumps(h))
    [e] = S.start(w, "human")["unapplied_pins"]
    assert e["kind"] == "released" and e["action"].startswith("ask the user: either clear the pin in the TUI's Edit view (x)")
    assert "a re-run also leaves human.json (the editor's cut) out as stale until it is imported again" in e["action"]


def test_the_tui_session_note_is_left_out_when_relocking_an_applied_pin_at_the_same_points(make_ws):
    """Minor: ``l`` again on a pin the session's base applied, at the same points and clips, changes nothing the session sees."""
    import dataclasses

    from matchcut.pipeline.editorial import apply_pins
    from matchcut.pipeline.timeline import clip_origin
    from matchcut.tui.views.edit import _unchanged_and_applied
    w = make_ws()
    tl = Timeline.load(w / "final.json")
    for x in tl.clips:
        x.origin = clip_origin(x)
    ctx = S.workspace_context(w)
    pin = pin_at(w, 0)
    out, _ = apply_pins(tl, [pin], ctx.bounds, ctx.min_clip_seconds)
    out.save(w / "final.json")
    PinStore(w).put(pin)
    pending = pin_at(w, 1)
    S.start(w)
    assert _unchanged_and_applied(w, pin, dataclasses.replace(pin))  # applied, unchanged: no note
    assert not _unchanged_and_applied(w, pin, dataclasses.replace(pin, out_time=pin.out_time - 0.04))  # moved: the note
    assert not _unchanged_and_applied(w, None, pin)  # new
    assert not _unchanged_and_applied(w, pending, dataclasses.replace(pending))  # unchanged, but the base never applied it
