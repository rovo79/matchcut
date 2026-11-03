"""Final M3a refinements: a pin the session's base does not hold fails closed (requirement A), a protected-transition mismatch is never
published silently (requirement B), and ``supersede_human`` binds to the very human.json the user approved superseding."""
from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from matchcut.edit import publish as P
from matchcut.edit import session as S
from matchcut.edit import tools as T
from matchcut.edit import view as V
from matchcut.edit.model import Refused
from matchcut.interchange.workspace import export_workspace, import_timeline
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore, apply_pins
from matchcut.pipeline.junctions import stale_human
from matchcut.pipeline.timeline import Timeline, clip_origin

from .test_cli import REPO, run_cli
from .util import click

PENDING = "pin s1->s3 was made or changed after the pipeline's last run, so this edit session does not protect it yet"


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def edit(w: Path, ops) -> str:
    return S.apply(w, S.propose(w, ops)["proposal"])["revision"]


def pin_at(w: Path, j: int, stage: str = "final.json") -> Pin:
    """The pin the Edit view's ``l`` makes on junction ``j`` (0-based) of a stage."""
    tl = Timeline.load(w / stage)
    a, b = tl.clips[j], tl.clips[j + 1]
    return Pin(a.shot_index, b.shot_index, round(a.source_out, 3), round(b.source_in, 3), "locked", "", clip_origin(a), clip_origin(b))


def with_music(w: Path) -> Path:
    cfg = PipelineConfig.load(w / "config.json")
    cfg.music.path = "click.wav"
    cfg.save(w / "config.json")
    click(beat=0.4).save(w / "music.json")  # a beat every 10 frames at 25 fps
    return w


@pytest.fixture
def pending(make_ws) -> Path:
    """A session on final whose pin s1->s3 (c002|c003) was made after the pipeline's last run: final has no decision for it."""
    w = with_music(make_ws())
    PinStore(w).put(pin_at(w, 1))
    out = S.start(w)
    [e] = out["unapplied_pins"]
    assert (e["pin"], e["kind"], e["shots"], e["junction"], e["protected"]) == ("s1->s3", "pending", [1, 3], "s1-s3", False)
    assert e["affected_clips"] == ["c002", "c003"] and e["affected_junctions"] == ["s0-s1", "s1-s3", "s3-s5"]
    return w


# ----------------------------------------------------------------------------------------------- A: a pending pin fails closed
TOUCHING = [
    ("trim", [{"op": "trim", "clip": "c002", "edge": "out", "frames": -2}], "changes c002 (shot 1)"),
    ("roll", [{"op": "roll", "junction": 2, "frames": 2}], "changes c002 (shot 1), changes c003 (shot 3)"),
    ("slip", [{"op": "slip", "clip": "c003", "frames": 2}], "changes c003 (shot 3)"),
    ("replace", [{"op": "replace", "clip": "c002", "shot": 2}], "removes c002 (shot 1)"),
    ("insert", [{"op": "insert", "shot": 2, "in_frame": 210, "out_frame": 240, "after": "c002"}], "separates c002|c003"),
    ("remove", [{"op": "remove", "clip": "c002"}], "removes c002 (shot 1)"),
    ("move", [{"op": "move", "clip": "c003", "before": "c001"}], "changes c003 (shot 3)"),
    ("set_transition", [{"op": "set_transition", "junction": 2, "kind": "dissolve", "frames": 4}], "changes c003 (shot 3)"),
    ("snap_to_beat", [{"op": "snap_to_beat", "junction": 2}], "changes c002 (shot 1), changes c003 (shot 3)"),
    # indirect: no argument names a clip of shot 1 or 3, but the result changes what the pin covers
    ("indirect: remove a neighbour", [{"op": "remove", "clip": "c004"}], "separates c003|c004"),
    ("indirect: a transition by number", [{"op": "set_transition", "junction": 3, "kind": "dissolve", "frames": 4}],
     "changes the transition at c003|c004"),
    ("indirect: move a clip between them", [{"op": "move", "clip": "c005", "after": "c002"}], "separates c002|c003"),
    ("indirect: another piece of shot 3", [{"op": "insert", "shot": 3, "in_frame": 340, "out_frame": 370, "after": "c005"}],
     "adds e001 (shot 3)"),
    ("indirect: insert before a clip of shot 1", [{"op": "insert", "shot": 2, "in_frame": 210, "out_frame": 240, "after": "c001"}],
     "separates c001|c002"),
]


@pytest.mark.parametrize("name, ops, what", TOUCHING, ids=[t[0] for t in TOUCHING])
def test_a_pending_pin_refuses_every_change_to_its_shots_clips_and_junctions(pending, name, ops, what):
    w = pending
    before = sorted(p.name for p in (w / "edits" / "proposals").iterdir())
    with pytest.raises(S.ProposalRefused) as exc:
        S.propose(w, ops)
    line = exc.value.problems[0]
    assert line.startswith(PENDING) and what in line, line
    cmd = S.rerun_command(w)
    src = Path(PipelineConfig.load(w / 'config.json').source).resolve()
    assert cmd == (f"PYTHONPATH={shlex.quote(str(REPO))} python -m matchcut.pipeline refine {shlex.quote(str(src))} -w "
                   f"{shlex.quote(str(w.resolve()))} --keep-config")
    assert line.endswith(f"to protect it, ask the user to re-run the pipeline so the canonical stage applies it ({cmd}, or press R in the TUI "
                         'and continue through its checkpoints), then to restart the edit session (start {"restart": true}); a re-run costs '
                         "model calls and leaves the edit and human stages out as stale, and a restart moves this session aside, so an agent "
                         "never does either on its own")
    assert sorted(p.name for p in (w / "edits" / "proposals").iterdir()) == before and S.load(w).head == "r000"


ELSEWHERE = [
    [{"op": "trim", "clip": "c005", "edge": "out", "frames": -3}],
    [{"op": "trim", "clip": "c001", "edge": "in", "frames": 2}],
    [{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 4}],
    [{"op": "slip", "clip": "c004", "frames": 3}],
    [{"op": "trim", "clip": "c001", "edge": "out", "frames": -2}],  # the cut c001|c002 moves on c001's side only: c002 and the junction kept
]


@pytest.mark.parametrize("ops", ELSEWHERE)
def test_edits_elsewhere_stay_allowed_while_a_pin_is_pending(pending, ops):
    p = S.propose(pending, ops)
    assert S.apply(pending, p["proposal"])["revision"] == "r001"


def test_start_inspect_and_log_list_the_unapplied_pins_and_the_cli_refuses_with_exit_1(pending):
    w = pending
    start_entry = S.unapplied_pins(S.load(w))
    d = V.inspect(w)
    assert d["unapplied_pins"] == start_entry == S.log(w)["unapplied_pins"]
    pin = next(p for p in d["pins"] if p["pin"] == "s1->s3")
    assert pin["state"] == "pending" and pin["protected"] is False and pin["action"].startswith("to protect it, ask the user to re-run the pipeline")
    for command in ("inspect", "log"):
        code, doc, _ = run_cli(str(w), command)
        assert code == 0 and doc["unapplied_pins"] == start_entry
    code, doc, _ = run_cli(str(w), "propose", json.dumps({"ops": [{"op": "trim", "clip": "c003", "edge": "in", "frames": 2}]}))
    assert code == 1 and doc["kind"] == "refused" and doc["command"] == "propose"
    assert doc["error"].startswith("refused: " + PENDING) and 'then to restart the edit session (start {"restart": true})' in doc["error"]
    assert doc["problems"][0].startswith(PENDING) and doc["changes"] == ["c003: in +2f"]


def test_a_pin_stored_after_a_proposal_makes_apply_refuse_and_reproposing_fail_closed(make_ws):
    w = make_ws()
    S.start(w)
    p = S.propose(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -2}])
    PinStore(w).put(pin_at(w, 1))
    with pytest.raises(Refused, match="was made before the workspace changed .*pins.*propose again"):
        S.apply(w, p["proposal"])
    with pytest.raises(S.ProposalRefused, match=PENDING):
        S.propose(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -2}])


def test_a_pin_added_after_apply_makes_publish_refuse(make_ws):
    w = make_ws()
    S.start(w)
    rid = edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    PinStore(w).put(pin_at(w, 1))
    with pytest.raises(Refused) as exc:
        P.publish(w)
    msg = str(exc.value)
    assert msg.startswith(f"{rid} cannot be published: {PENDING}") and "this edit changes c002 (shot 1)" in msg
    assert 'then to restart the edit session (start {"restart": true})' in msg
    assert not (w / "edit.json").exists() and not (w / "edit_publish.json").exists()
    code, doc, _ = run_cli(str(w), "publish")
    assert code == 1 and doc["kind"] == "refused" and doc["error"] == msg


def test_an_untouched_revision_publishes_and_lists_the_pin_as_not_protected(pending):
    w = pending
    rid = edit(w, [{"op": "trim", "clip": "c005", "edge": "out", "frames": -3}])
    out = P.publish(w)
    assert out["revision"] == rid
    [e] = out["unapplied_pins"]
    assert (e["pin"], e["kind"], e["protected"], e["status"]) == ("s1->s3", "pending", False, "not protected by this edit")
    assert json.loads((w / "edit_publish.json").read_text())["unapplied_pins"] == [e]
    [state] = [p for p in out["pins"] if p["pin"] == "s1->s3"]
    assert state["state"] == "not_protected" and state["reason"].startswith("not protected by this edit: " + PENDING)
    line = json.loads((w / "edits" / "published" / "log.jsonl").read_text().splitlines()[-1])
    assert line["unapplied_pins"] == ["s1->s3"]


# ----------------------------------------------------------------------------------------------- A: not placed
def test_a_pin_the_pipeline_could_not_place_fails_closed_with_its_own_wording(make_ws):
    """final's history records pin_unplaced for s2->s4 (neither shot is in the cut): a plain re-run will not help."""
    w = make_ws()
    tl = Timeline.load(w / "final.json")
    ctx = S.workspace_context(w)
    pin = Pin(2, 4, 9.0, 17.0)
    out, decisions = apply_pins(tl, [pin], ctx.bounds, ctx.min_clip_seconds)
    assert [d.action for d in decisions] == ["pin_unplaced"]
    out.save(w / "final.json")
    PinStore(w).put(pin)
    [e] = S.start(w)["unapplied_pins"]
    assert (e["pin"], e["kind"], e["junction"], e["affected_clips"]) == ("s2->s4", "not_placed", None, [])
    assert e["reason"] == ("pin s2->s4 could not be placed by the pipeline's last run (shot 2 is not in this cut), so this edit session "
                           "does not protect it")
    assert e["action"].startswith("a plain re-run will not place it: ask the user to clear it (x) or make it again (l / v) on a junction it "
                                  "can hold in the TUI's Edit view, to re-run the pipeline (PYTHONPATH=")
    assert e["action"].endswith('then to restart the edit session (start {"restart": true}); a re-run costs model calls and leaves the edit '
                                "and human stages out as stale, and a restart moves this session aside, so an agent never does either on "
                                "its own")
    with pytest.raises(S.ProposalRefused, match=r"pin s2->s4 could not be placed .*this edit adds e001 \(shot 4\).*a plain re-run will not"):
        S.propose(w, [{"op": "insert", "shot": 4, "in_frame": 420, "out_frame": 450, "after": "c002"}])
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])  # elsewhere: allowed
    assert [p["state"] for p in P.publish(w)["pins"]] == ["not_protected"]


# ----------------------------------------------------------------------------------------------- A: after a pipeline re-run
def test_after_the_pipeline_re_run_and_a_restart_the_pin_is_held_and_protects(pinned_world_unpinned, tmp_path):
    """The whole route the refusal names: a pin made in the TUI after the last run is pending (the session refuses its junction); the exact
    command it gives re-runs the pipeline, which applies the pin; start with restart holds it, and the usual pin rules protect it."""
    w = tmp_path / "ws"
    shutil.copytree(pinned_world_unpinned.ws, w)
    cfg = PipelineConfig.load(w / "config.json")
    cfg.workspace = str(w)
    cfg.save(w / "config.json")
    S.start(w)
    from matchcut.pipeline.junctions import build_junctions, load_stage_timelines
    found = [x for x in build_junctions(load_stage_timelines(w)) if x.baseline.stage == "roughcut" and not x.current.locked]
    j0 = next((x for x in found if x.beat_boundary), found[0])
    j = j0.index
    pin = Pin(j0.out_shot, j0.in_shot, round(j0.current.out_time, 3), round(j0.current.in_time, 3))  # as the fixture of the pipeline tests
    PinStore(w).put(pin)
    label = f"s{pin.out_shot}->s{pin.in_shot}"
    with pytest.raises(S.ProposalRefused, match=f"pin {label} was made or changed after the pipeline's last run"):
        S.propose(w, [{"op": "set_transition", "junction": j + 1, "kind": "dissolve", "frames": 2}])
    [e] = S.unapplied_pins(S.load(w))
    argv = shlex.split(e["rerun_command"])
    assert argv[:5] == [f"PYTHONPATH={REPO}", "python", "-m", "matchcut.pipeline", "refine"] and argv[-1] == "--keep-config"
    config = (w / "config.json").read_bytes()
    elsewhere = tmp_path / "elsewhere"  # the command runs from any directory: PYTHONPATH names the repository
    elsewhere.mkdir()
    proc = subprocess.run([sys.executable, *argv[2:]], capture_output=True, text=True, cwd=str(elsewhere), timeout=600,
                          env={**__import__("os").environ, "PYTHONPATH": argv[0].split("=", 1)[1]})
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert (w / "config.json").read_bytes() == config  # --keep-config: the workspace's config is never rewritten
    code, doc, _ = run_cli(str(w), "start", '{"restart": true}')
    assert code == 0 and doc["unapplied_pins"] == [] and doc["moved_aside"].startswith("edits.stale-")
    held = next(p for p in V.inspect(w)["pins"] if p["pin"] == label)
    assert held["state"] == "held"
    with pytest.raises(Refused, match="held by an editor pin|breaks the editor pin"):
        S.propose(w, [{"op": "roll", "junction": held["n"], "frames": 1}])
    s = S.load(w)
    longest = max((c for c in s.timeline().clips if c.id not in held["clips"] and not c.locked_out), key=lambda c: c.duration).id
    edit(w, [{"op": "trim", "clip": longest, "edge": "out", "frames": -1}])
    assert [p["state"] for p in P.publish(w)["pins"] if p["pin"] == label] == ["held"]


@pytest.fixture(scope="module")
def pinned_world_unpinned(tmp_path_factory):
    """A finished mock-backend workspace with no pin."""
    from types import SimpleNamespace

    from matchcut.pipeline.demo import make_demo_footage
    from matchcut.pipeline.pipeline import Pipeline

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    root = tmp_path_factory.mktemp("pipeline-pending")
    video = make_demo_footage(root / "demo.mp4")
    ws = root / "ws"
    cfg = PipelineConfig(source=str(video), workspace=str(ws), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()
    return SimpleNamespace(ws=ws, video=video)


# ----------------------------------------------------------------------------------------------- B: protected-transition mismatches
def mismatch(w: Path) -> None:
    """final holds the pin s0->s1 with a cut; the pin protects a dissolve of 4 (set with an explicit kind while no session existed)."""
    tl = Timeline.load(w / "final.json")
    for x in tl.clips:
        x.origin = clip_origin(x)
    ctx = S.workspace_context(w)
    pin = pin_at(w, 0)
    out, decisions = apply_pins(tl, [pin], ctx.bounds, ctx.min_clip_seconds)
    assert decisions[-1].action == "pinned"
    out.save(w / "final.json")
    PinStore(w).put(pin)
    V.pin_transition(w, pin="s0->s1", hold=True, kind="dissolve", frames=4)


@pytest.fixture
def mismatched(make_ws) -> Path:
    w = make_ws()
    mismatch(w)
    assert [m["pin"] for m in S.start(w)["transition_mismatches"]] == ["s0->s1"]
    return w


def test_unrelated_proposals_are_accepted_while_a_mismatch_exists(mismatched):
    for ops in ([{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}], [{"op": "set_transition", "junction": 4, "kind": "dissolve",
                                                                                "frames": 4}]):
        edit(mismatched, ops)


def test_an_inherited_mismatch_is_refused_at_publish_then_a_repair_publishes(mismatched):
    w = mismatched
    edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    with pytest.raises(Refused, match=r'repair it by proposing \{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4\}'):
        P.publish(w)
    code, doc, _ = run_cli(str(w), "publish")
    assert code == 1 and doc["kind"] == "refused" and '{"acknowledge_mismatches": ["s0->s1"]}' in doc["error"]
    edit(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])
    out = P.publish(w)
    assert out["transition_mismatches"] == [] and out["acknowledged_mismatches"] == []
    assert [(p["pin"], p["state"]) for p in out["pins"]] == [("s0->s1", "held")]


@pytest.mark.parametrize("given, why", [
    ([], r"it does not name s0->s1"),
    (["s0->s1", "s0->s1"], r"it names s0->s1 twice"),
    (["s0->s1", "s9->s9"], r"s9->s9 is not a stored pin \(pins: s0->s1\)"),
    (["s5->s6"], r"s5->s6 is not a stored pin .*it does not name s0->s1"),
])
def test_a_partial_extra_or_unknown_acknowledgement_is_refused(mismatched, given, why):
    w = mismatched
    edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    with pytest.raises(Refused, match=(rf"acknowledge_mismatches .* is refused: {why}" if given else "it must name every such pin")):
        P.publish(w, acknowledge_mismatches=given)
    assert not (w / "edit.json").exists()


def test_acknowledging_a_pin_that_is_not_mismatching_is_refused(make_ws):
    w = make_ws()
    mismatch(w)
    PinStore(w).put(pin_at(w, 3))  # s5->s6: stored, but the pipeline never applied it (and it is not mismatching)
    S.start(w)
    edit(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])  # repaired: nothing mismatching now
    with pytest.raises(Refused, match=r"acknowledge_mismatches \[\"s0->s1\"\] is refused: s0->s1 has no transition mismatch and no protection change in r001.*publish "
                                      r"without acknowledge_mismatches"):
        P.publish(w, acknowledge_mismatches=["s0->s1"])
    with pytest.raises(Refused, match=r"s5->s6 has no transition mismatch"):
        P.publish(w, acknowledge_mismatches=["s5->s6"])


def test_the_held_wording_never_appears_for_a_mismatched_pin(mismatched, capsys):
    """inspect, the publish result, the junctions CLI and the TUI's badge: a mismatched pin is never called held as if its protection were
    satisfied."""
    from matchcut.pipeline.cli import main
    from matchcut.pipeline.editorial import protected_transition_note
    w = mismatched
    p = next(x for x in V.inspect(w)["pins"] if x["pin"] == "s0->s1")
    assert p["state"] == "transition_mismatch" and "acknowledge_mismatches" in p["reason"]
    edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    out = T.call(w, "publish", {"acknowledge_mismatches": ["s0->s1"]})
    assert [x["state"] for x in out["pins"]] == ["mismatch_acknowledged"]
    assert main(["junctions", str(w)]) == 0
    printed = capsys.readouterr().out.splitlines()
    footer = [ln for ln in printed if ln.startswith("pin ")]
    assert [ln for ln in printed if "c001->c002" in ln][0].endswith("pinned TRANSITION-MISMATCH")  # the list line says it too
    assert footer == ["pin s0->s1: clips and cut points held by this edit, but its protected transition is NOT kept: the pin holds a "
                      "dissolve of 4 frames, this cut has a cut (mismatch acknowledged at publish)"]
    note = protected_transition_note(Timeline.load(w / "edit.json"), PinStore(w).get(0, 1), True)
    assert note == footer[0].split("but ", 1)[1]
    # the TUI's Edit view: on the edit stage the pin badge is followed by that note (read from the record), never left to read as satisfied
    from types import SimpleNamespace

    from matchcut.pipeline.junctions import Junction, JunctionState
    from matchcut.tui.views.edit import EditView
    from matchcut.tui.widgets.inspector import badges
    state = SimpleNamespace(workspace=w, timeline=lambda stage: Timeline.load(w / f"{stage}.json"))
    pin = PinStore(w).get(0, 1)
    tui_note = EditView._pin_note(SimpleNamespace(_state=state), pin, "edit")
    assert tui_note == note and EditView._pin_note(SimpleNamespace(_state=state), pin, "final") == ""
    j = Junction(0, "c001", "c002", 0, 1, "b1", "b1", 25.0, states=[JunctionState("edit", pin.out_time, pin.in_time)])
    assert badges(j, j.current, pin, None, "edit", tui_note)[-1] == note


# ----------------------------------------------------------------------------------------------- supersede_human, bound to the file
def human_from_final(w: Path) -> None:
    cfg = PipelineConfig.load(w / "config.json")
    export_workspace(w, cfg, stages=("final",), formats=("otio",))
    import_timeline(w, cfg, w / "exports" / "final.otio")
    assert stale_human(w) is None


def test_supersede_human_must_name_the_human_json_it_supersedes(make_ws):
    w = make_ws()
    human_from_final(w)
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    sha12 = sha(w / "human.json")[:12]
    with pytest.raises(Refused, match=rf'human.json is {sha12}: to publish anyway, the user must explicitly approve superseding it, then pass '
                                      rf'supersede_human: "{sha12}"'):
        P.publish(w)
    with pytest.raises(Refused, match=rf'supersede_human: true is no longer accepted: it now names the very human.json it supersedes.*'
                                      rf'supersede_human: "{sha12}"'):
        P.publish(w, supersede_human=True)
    code, doc, _ = run_cli(str(w), "publish", '{"supersede_human": true}')
    assert code == 1 and doc["kind"] == "refused" and "supersede_human: true is no longer accepted" in doc["error"]
    with pytest.raises(Refused, match=rf"supersede_human names 0123456789ab, but human.json is {sha12} now"):
        P.publish(w, supersede_human="0123456789ab")
    code, doc, _ = run_cli(str(w), "publish", '{"supersede_human": "0123"}')
    assert code == 2 and doc["kind"] == "usage"
    assert not (w / "edit.json").exists()
    out = P.publish(w, supersede_human=sha12)
    record = json.loads((w / "edit_publish.json").read_text())
    assert record["superseded_human_sha256"] == sha(w / "human.json") and out["human_left_out"]
    log = json.loads((w / "edits" / "published" / "log.jsonl").read_text().splitlines()[-1])
    assert log["superseded_human"] is True and log["superseded_human_sha256"] == sha(w / "human.json")


def test_a_stale_supersede_value_is_refused(make_ws):
    """The user approved superseding one human.json; the editor's file is re-imported since: the value no longer names it."""
    w = make_ws()
    human_from_final(w)
    old = sha(w / "human.json")[:12]
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    human = json.loads((w / "human.json").read_text())
    human["name"] = "re-imported"
    (w / "human.json").write_text(json.dumps(human))
    assert stale_human(w) is None
    with pytest.raises(Refused, match=rf"supersede_human names {old}, but human.json is {sha(w / 'human.json')[:12]} now .*ask the user again"):
        P.publish(w, supersede_human=old)


def test_supersede_human_with_nothing_to_supersede_is_refused(make_ws):
    w = make_ws()
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    with pytest.raises(Refused, match="there is nothing to supersede; publish without it"):
        P.publish(w, supersede_human="0123456789ab")
    assert P.publish(w, supersede_human=False)["revision"] == "r001"  # (the old form's "no" means nothing is superseded)


# ----------------------------------------------------------------------------------------------- the no-op proposal
def test_a_no_op_proposal_is_refused_with_a_clear_explanation(make_ws):
    w = make_ws()
    S.start(w)
    with pytest.raises(S.ProposalRefused) as exc:
        S.propose(w, [{"op": "set_transition", "junction": 1, "kind": "cut"}])
    assert str(exc.value) == ("refused: the operations change nothing: the cut would be identical to the head r000, so there is nothing to "
                              "apply and nothing was stored (junction 1 (c001|c002) is already cut)")
