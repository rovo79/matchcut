"""M3a task 4 review fixes: a publish over a fresh human stage (supersede_human), pins stored since a revision was made, the default
session base and can_publish, crash recovery and the publish archive (records, log), the stage lock, and the stale-human hint."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from matchcut.edit import publish as P
from matchcut.edit import session as S
from matchcut.edit import tools as T
from matchcut.edit import view as V
from matchcut.edit.model import Refused
from matchcut.interchange.workspace import export_workspace, import_timeline, stage_lock
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore, apply_pins
from matchcut.pipeline.junctions import load_stage_timelines, stale_edit, stale_human
from matchcut.pipeline.timeline import Timeline, clip_origin

HINT = "(re-export, or re-import the editor's file: matchcut diffs it against the stage it was exported from)"


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def edit(w: Path, ops) -> str:
    return S.apply(w, S.propose(w, ops)["proposal"])["revision"]


def human_from_final(w: Path) -> None:
    """A fresh human stage imported against final (what matchcut.interchange writes)."""
    cfg = PipelineConfig.load(w / "config.json")
    export_workspace(w, cfg, stages=("final",), formats=("otio",))
    import_timeline(w, cfg, w / "exports" / "final.otio")
    assert stale_human(w) is None and load_stage_timelines(w)[-1][0] == "human"


def human_files(w: Path) -> dict:
    return {n: (w / n).read_bytes() for n in ("human.json", "human_import.json")}


# ----------------------------------------------------------------------------------------------- I2: a fresh human stage
def test_publish_over_a_fresh_canonical_human_stage_is_refused_unless_superseded(make_ws):
    w = make_ws()
    human_from_final(w)
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    kept = human_files(w)
    with pytest.raises(Refused, match=r"human.json \(an editor's re-export, imported against final\) is the latest stage .* leave it out of the "
                                      r"stage order as stale; human.json and human_import.json are never changed .*pass supersede_human"):
        P.publish(w)
    assert not (w / "edit.json").exists() and stale_human(w) is None
    out = T.call(w, "publish", {"supersede_human": True})
    assert out["human_left_out"] == stale_human(w) and out["human_left_out"].startswith("human.json is stale: edit.json was published")
    assert json.loads((w / "edit_publish.json").read_text())["superseded_human"].startswith("human.json (an editor's re-export")
    assert human_files(w) == kept and load_stage_timelines(w)[-1][0] == "edit"
    log = [json.loads(x) for x in (w / "edits" / "published" / "log.jsonl").read_text().splitlines()]
    assert [e["superseded_human"] for e in log] == [True]


def test_republishing_over_a_human_stage_imported_against_the_edit_is_refused_unless_superseded(make_ws):
    w = make_ws()
    S.start(w)
    r1 = edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    assert P.publish(w)["human_left_out"] is None
    cfg = PipelineConfig.load(w / "config.json")
    export_workspace(w, cfg, stages=("edit",), formats=("otio",))
    import_timeline(w, cfg, w / "exports" / "edit.otio")
    assert stale_human(w) is None
    edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    with pytest.raises(Refused, match=r"human.json \(an editor's re-export, imported against edit\) continues the edit stage that is published now"):
        P.publish(w)
    assert P.publish(w, r1)["human_left_out"] is None  # the same edit.json again: the human stage still continues it
    assert stale_human(w) is None
    out = P.publish(w, supersede_human=True)
    assert out["human_left_out"].startswith("human.json is stale: edit.json changed since the import")


# ----------------------------------------------------------------------------------------------- I3: pins stored since
def pin_of(tl: Timeline, j: int) -> Pin:
    a, b = tl.clips[j], tl.clips[j + 1]
    return Pin(a.shot_index, b.shot_index, round(a.source_out, 3), round(b.source_in, 3), "locked", "", clip_origin(a), clip_origin(b))


def pinned_final(w: Path, j: int = 0) -> Pin:
    tl = Timeline.load(w / "final.json")
    for x in tl.clips:
        x.origin = clip_origin(x)
    ctx = S.workspace_context(w)
    pin = pin_of(tl, j)
    out, decisions = apply_pins(tl, [pin], ctx.bounds, ctx.min_clip_seconds)
    assert decisions[-1].action == "pinned"
    out.save(w / "final.json")
    return pin


def test_a_pin_the_base_holds_but_stored_after_the_revision_is_checked_at_publish(make_ws):
    """final holds the pin s0->s1 (the pipeline applied it), but editorial.json lacked it when the agent removed c002: the revision no longer
    holds the pin; once the pin is stored again, publishing that revision is refused."""
    w = make_ws()
    pin = pinned_final(w)
    S.start(w)
    rid = edit(w, [{"op": "remove", "clip": "c002"}])  # no pin stored: allowed
    PinStore(w).put(pin)
    with pytest.raises(Refused, match=rf"pin s0->s1 was added or changed since {rid} was made, and {rid} breaks the editor pin s0->s1: c002 "
                                      r"removed: propose the edits again \(or release the pin\)"):
        P.publish(w)
    assert not (w / "edit.json").exists()
    PinStore(w).remove(pin.out_shot, pin.in_shot)
    assert P.publish(w)["revision"] == rid


def test_a_transition_protected_after_the_revision_is_checked_at_publish(make_ws):
    w = make_ws()
    pin = pinned_final(w)
    PinStore(w).put(pin)
    S.start(w)
    edit(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])  # an unprotected pin's transition may change
    PinStore(w).put(Pin(**{**pin.__dict__, "hold_transition": True, "held_transition": {"kind": "cut", "frames": 0}}))
    with pytest.raises(Refused, match=r"pin s0->s1 was added or changed since r001 was made, and r001 changes the transition the editor pin "
                                      r"s0->s1 protects \(cut -> dissolve 4f\)"):
        P.publish(w)
    # protecting the head's own transition (pin_transition without kind/frames) is consistent with the revision: it publishes
    V.pin_transition(w, pin="s0->s1", hold=True)
    out = P.publish(w)
    assert [(p["pin"], p["state"], p["hold_transition"]) for p in out["pins"]] == [("s0->s1", "held", True)]


def test_publish_reports_the_pins_and_words_a_pin_the_edit_does_not_hold_for_the_edit_stage(make_ws, capsys):
    from matchcut.pipeline.cli import main
    w = make_ws()
    held = pinned_final(w)
    PinStore(w).put(held)
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    later = pin_of(Timeline.load(w / "final.json"), 2)  # stored after the pipeline's last run: neither final nor the edit holds it
    PinStore(w).put(later)
    out = P.publish(w)
    states = {p["pin"]: (p["state"], p["reason"]) for p in out["pins"]}
    assert states["s0->s1"] == ("held", "")
    assert states["s3->s5"][0] == "not_held" and states["s3->s5"][1].startswith("not held by this edit")
    assert "re-run" not in states["s3->s5"][1] and "pending" not in json.dumps(out["pins"])
    assert main(["junctions", str(w)]) == 0
    footer = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("pin ")]
    assert footer == ["pin s0->s1: held by this edit",
                      "pin s3->s5: not held by this edit (the pin was stored after the pipeline's last run; a run would apply it, and leave "
                      "the edit stage out as stale)"]
    assert "re-run the pipeline" not in json.dumps(V.inspect(w)["pins"])


def test_the_tui_badge_on_the_edit_stage_does_not_say_pending():
    from matchcut.pipeline.junctions import Junction, JunctionState
    from matchcut.tui.widgets.inspector import EDIT_NOT_HELD, badges
    j = Junction(0, "c001", "c002", 0, 1, "b1", "b1", 25.0, states=[JunctionState("edit", 2.0, 4.0)])
    pin = Pin(0, 1, 2.0, 4.0)
    assert badges(j, j.current, pin, None, "edit") == [EDIT_NOT_HELD]
    assert badges(j, j.current, pin, None, "final") == ["pin pending: run again (R) to apply"]


# ----------------------------------------------------------------------------------------------- I4: the default base, can_publish
def test_the_default_base_is_the_latest_canonical_stage_even_with_a_fresh_human_stage(make_ws):
    w = make_ws()
    human_from_final(w)
    out = S.start(w)
    assert (out["base_stage"], out["can_publish"], out["publish_blocker"]) == ("final", True, None)
    assert S.log(w)["can_publish"] is True and V.inspect(w)["session"]["can_publish"] is True


@pytest.mark.parametrize("base", ["human", "roughcut"])
def test_a_session_on_human_or_an_earlier_stage_says_it_cannot_publish(make_ws, base):
    w = make_ws()
    human_from_final(w)
    out = S.start(w, stage=base)
    assert out["base_stage"] == base and out["can_publish"] is False
    assert out["publish_blocker"].startswith(f"the session is based on {base}, not on the latest canonical stage final")
    for doc in (S.log(w), V.inspect(w)["session"], T.call(w, "start", {"stage": base, "restart": True})):
        assert doc["can_publish"] is False and doc["publish_blocker"] == out["publish_blocker"]


# ----------------------------------------------------------------------------------------------- M2, M3: crashes and the archive
def flaky(monkeypatch, fn: str, when):
    real = getattr(os, fn)

    def call(a, b, *rest):
        if when(str(a), str(b)):
            raise OSError(28, "No space left on device (injected)")
        return real(a, b, *rest)
    monkeypatch.setattr(os, fn, call)
    return real


def test_a_publish_cut_short_before_its_record_recovers_from_the_same_session(make_ws, monkeypatch):
    w = make_ws()
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    P.publish(w)
    r2 = edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    real = flaky(monkeypatch, "replace", lambda a, b: b.endswith("/edit_publish.json"))
    with pytest.raises(OSError):
        P.publish(w)
    monkeypatch.setattr(os, "replace", real)
    assert stale_edit(w).startswith("edit.json is stale: it is not the file edit_publish.json describes")
    out = P.publish(w)  # the same session: edit.json is byte for byte its publish of r002
    # previous: the revision the old record names (r001), not the interrupted one, which is "recovered"
    assert out["previous"] == "r001" and out["recovered"] == r2 and out["replaced"] is None and stale_edit(w) is None
    log = [json.loads(x) for x in (w / "edits" / "published" / "log.jsonl").read_text().splitlines()]
    assert [(e["revision"], e["previous"], e["recovered"]) for e in log] == [("r001", None, None), (r2, "r001", None), (r2, "r001", r2)]
    # another session meeting such an edit.json is told it may be an interrupted publish
    real = flaky(monkeypatch, "replace", lambda a, b: b.endswith("/edit_publish.json"))
    edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 1}])
    with pytest.raises(OSError):
        P.publish(w)
    monkeypatch.setattr(os, "replace", real)
    S.start(w, restart=True)
    edit(w, [{"op": "remove", "clip": "c003"}])
    with pytest.raises(Refused, match=r"edit.json is not the file edit_publish.json describes \(changed since it was published, or an "
                                      r"interrupted publish\?\)"):
        P.publish(w)


def test_every_record_is_kept_and_a_record_left_without_its_edit_json_is_archived(make_ws, monkeypatch):
    w = make_ws()
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    P.publish(w)
    first = (w / "edit_publish.json").read_bytes()
    edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    P.publish(w)  # the same session overwrites the record: its copy stays
    records = w / "edits" / "published" / "records"
    assert first in {p.read_bytes() for p in records.iterdir()} and len(list(records.iterdir())) == 2
    # a replace whose move-aside is cut short after edit.json moved: the record is left behind, and archived by the next publish
    S.start(w, restart=True)
    edit(w, [{"op": "remove", "clip": "c003"}])
    old = (w / "edit_publish.json").read_bytes()
    real = flaky(monkeypatch, "rename", lambda a, b: a.endswith("/edit_publish.json") and "/replaced/" in b and "record-" not in b)
    with pytest.raises(OSError):
        P.publish(w, replace=True)
    monkeypatch.setattr(os, "rename", real)
    assert not (w / "edit.json").exists() and (w / "edit_publish.json").read_bytes() == old
    P.publish(w)
    orphans = list((w / "edits" / "replaced").glob("record-*/edit_publish.json"))
    assert [p.read_bytes() for p in orphans] == [old]
    log = [json.loads(x) for x in (w / "edits" / "published" / "log.jsonl").read_text().splitlines()]
    assert [e["revision"] for e in log] == ["r001", "r002", "r001"] and log[-1]["orphan_record"] == orphans[0].parent.relative_to(w).as_posix()
    assert {"published_at", "session", "revision", "edit_sha256", "published", "replaced", "superseded_human"} <= set(log[0])


# ----------------------------------------------------------------------------------------------- M4: the lock, the hint
def test_an_import_waits_for_the_stage_lock_a_publish_holds(make_ws):
    w = make_ws()
    cfg = PipelineConfig.load(w / "config.json")
    export_workspace(w, cfg, stages=("final",), formats=("otio",))
    done = []
    with stage_lock(w):
        t = threading.Thread(target=lambda: done.append(import_timeline(w, cfg, w / "exports" / "final.otio")))
        t.start()
        time.sleep(1.0)
        assert not done and not (w / "human.json").exists()
    t.join(60)
    assert done and (w / "human.json").exists()


def test_the_stale_human_message_has_one_hint(make_ws):
    w = make_ws()
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    P.publish(w)
    cfg = PipelineConfig.load(w / "config.json")
    export_workspace(w, cfg, stages=("edit",), formats=("otio",))
    import_timeline(w, cfg, w / "exports" / "edit.otio")
    (w / "edit_publish.json").write_text("[]")
    msg = stale_human(w)
    assert msg == ("human.json is stale: it was imported against the edit stage, and edit.json is stale: edit_publish.json is missing or "
                   f"unreadable {HINT}")
    assert msg.count("(") == 1


def test_record_copies_made_in_the_same_second_sort_in_the_order_they_were_written(make_ws, monkeypatch):
    w = make_ws()
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    r2 = edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    monkeypatch.setattr(S, "_now", lambda: "2026-11-01T12:00:00+00:00")
    order = [P.publish(w, r)["revision"] for r in (r2, "r001", r2)]
    names = sorted(p.name for p in (w / "edits" / "published" / "records").iterdir())
    assert [n.split("-")[2] for n in names] == order and [n.split("-")[1] for n in names] == ["001", "002", "003"]
    assert all(n.startswith("20261101T120000Z-") for n in names)


# ----------------------------------------------------------------------------------------------- the stage lock: bounded, shared for export
HOLD = """
import sys, time
sys.path.insert(0, sys.argv[1])
from matchcut.interchange.workspace import stage_lock
with stage_lock(sys.argv[2], shared=sys.argv[3] == "shared"):
    print("held", flush=True)
    time.sleep(float(sys.argv[4]))
"""


@pytest.fixture
def held_by_another_process():
    import subprocess
    import sys
    procs = []

    def hold(w: Path, mode: str = "exclusive", seconds: float = 20.0):
        repo = str(Path(__file__).resolve().parents[2])
        pr = subprocess.Popen([sys.executable, "-c", HOLD, repo, str(w), mode, str(seconds)], stdout=subprocess.PIPE, text=True)
        assert pr.stdout.readline().strip() == "held"
        procs.append(pr)
        return pr
    yield hold
    for pr in procs:
        pr.terminate()
        pr.wait(30)


def test_a_publish_gives_up_on_a_stage_lock_held_too_long_with_a_clear_refusal(make_ws, held_by_another_process, monkeypatch):
    from matchcut.interchange import workspace as WSP
    w = make_ws()
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    held_by_another_process(w)
    monkeypatch.setattr(WSP, "STAGE_LOCK_TIMEOUT", 0.5)
    t0 = time.monotonic()
    with pytest.raises(Refused, match=r"another import or publish \(or an export\) is running in .*\.stage\.lock was still held after "
                                      r"0\.5 s; try again once it has finished"):
        P.publish(w)
    assert 0.4 < time.monotonic() - t0 < 10 and not (w / "edit.json").exists()
    # an import too, as an InterchangeError
    from matchcut.interchange import InterchangeError
    cfg = PipelineConfig.load(w / "config.json")
    with pytest.raises(InterchangeError, match="another import or publish"):
        with stage_lock(w, timeout=0.2):
            pass
    assert not (w / "human.json").exists()
    with pytest.raises(InterchangeError, match="another import or publish"):
        export_workspace(w, cfg, stages=("final",), formats=("otio",))  # a shared lock waits for an exclusive holder too


def test_exports_share_the_stage_lock_and_a_publish_waits_for_them(make_ws, held_by_another_process, monkeypatch):
    from matchcut.interchange import workspace as WSP
    w = make_ws()
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    cfg = PipelineConfig.load(w / "config.json")
    held_by_another_process(w, "shared")  # another export reading the stages
    monkeypatch.setattr(WSP, "STAGE_LOCK_TIMEOUT", 0.5)
    export_workspace(w, cfg, stages=("final",), formats=("otio",))  # shared with it: no wait
    with pytest.raises(Refused, match="another import or publish"):
        P.publish(w)  # exclusive: waits for the export, then gives up
    assert not (w / "edit.json").exists()


def test_a_publish_waits_for_an_export_then_goes_ahead(make_ws, held_by_another_process):
    w = make_ws()
    S.start(w)
    edit(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    held_by_another_process(w, "shared", 1.5)
    t0 = time.monotonic()
    assert P.publish(w)["revision"] == "r001"
    assert time.monotonic() - t0 > 1.0


# ----------------------------------------------------------------------------------------------- I3b: a protected transition r000 lacks
def mismatched_by_explicit_kind(w: Path) -> Pin:
    """A protection set with an explicit transition while no session exists: final has a cut there, the pin holds a dissolve of 4."""
    pin = pinned_final(w)
    PinStore(w).put(pin)
    out = V.pin_transition(w, pin="s0->s1", hold=True, kind="dissolve", frames=4)
    assert out["held_transition"] == {"kind": "dissolve", "frames": 4} and out["junction"] is None
    return pin


def mismatched_by_restart(w: Path) -> Pin:
    """The agent's own dissolve protected, then start --restart: r000 comes from final, built by the pipeline (which ignores the
    protection), with a cut there."""
    pin = pinned_final(w)
    PinStore(w).put(pin)
    S.start(w)
    edit(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])
    assert V.pin_transition(w, pin="s0->s1", hold=True)["held_transition"] == {"kind": "dissolve", "frames": 4}
    return pin


MISMATCHES = [mismatched_by_explicit_kind, mismatched_by_restart]


@pytest.mark.parametrize("make_mismatch", MISMATCHES)
def test_start_and_inspect_flag_a_protected_transition_r000_lacks_and_say_how_to_resolve_it(make_ws, make_mismatch):
    w = make_ws()
    make_mismatch(w)
    out = S.start(w, restart=True)
    [m] = out["transition_mismatches"]
    assert (m["pin"], m["n"], m["junction"], m["transition"], m["held_transition"]) == (
        "s0->s1", 1, "s0-s1", {"kind": "cut", "frames": 0}, {"kind": "dissolve", "frames": 4})
    assert '{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}' in m["repair"]
    assert 'pin_transition {"pin": "s0->s1", "hold": true}' in m["repair"]
    d = V.inspect(w)
    assert d["transition_mismatches"] == [m] and d["junctions"][0]["transition_mismatch"]
    assert next(p for p in d["pins"] if p["pin"] == "s0->s1")["reason"] == m["repair"]
    # the tool's result carries it too
    assert T.call(w, "inspect", {})["transition_mismatches"] == [m]


@pytest.mark.parametrize("make_mismatch", MISMATCHES)
def test_the_held_transition_can_be_reached_and_published(make_ws, make_mismatch):
    w = make_ws()
    make_mismatch(w)
    S.start(w, restart=True)
    rid = edit(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])  # the repair: the held transition
    assert V.inspect(w)["transition_mismatches"] == []
    out = P.publish(w)
    assert out["revision"] == rid and [(p["pin"], p["state"]) for p in out["pins"]] == [("s0->s1", "held")]
    assert Timeline.load(w / "edit.json").clips[1].transition_in.kind == "dissolve"
    # once there, it stays: the protection holds the dissolve 4
    with pytest.raises(Refused, match=r"protected by the editor pin s0->s1: it holds the dissolve 4f there now"):
        S.propose(w, [{"op": "set_transition", "junction": 1, "kind": "cut"}])


@pytest.mark.parametrize("make_mismatch", MISMATCHES)
def test_an_untouched_mismatch_publishes(make_ws, make_mismatch):
    w = make_ws()
    make_mismatch(w)
    S.start(w, restart=True)
    rid = edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    assert P.publish(w)["revision"] == rid
    assert Timeline.load(w / "edit.json").clips[1].transition_in.is_cut


@pytest.mark.parametrize("make_mismatch", MISMATCHES)
def test_another_transition_at_a_mismatched_protected_pin_is_refused_when_proposed(make_ws, make_mismatch):
    w = make_ws()
    make_mismatch(w)
    S.start(w, restart=True)
    for kind, frames in (("dissolve", 6), ("dip_to_black", 4)):
        with pytest.raises(Refused, match=r"the transition at junction 1 \(c001\|c002\) is protected by the editor pin s0->s1: it holds a "
                                          r'dissolve 4f, so only set_transition to that \(kind "dissolve", frames 4\) is allowed there'):
            S.propose(w, [{"op": "set_transition", "junction": 1, "kind": kind, "frames": frames}])


def test_the_rules_allow_only_the_held_transition_at_a_mismatched_protected_pin(make_ws):
    """The rule on the whole proposal (whatever operation caused it), and the one publish applies from r000, are one function."""
    from matchcut.edit import rules
    from matchcut.pipeline.timeline import Transition
    w = make_ws()
    mismatched_by_explicit_kind(w)
    S.start(w)
    s = S.load(w)
    base, ctx = s.timeline("r000"), s.context()
    rate = ctx.rate

    def at(kind, frames):
        tl = base.copy()
        tl.clips[1].transition_in = Transition(kind, float(frames / rate)) if frames else Transition()
        return tl
    assert rules.problems(base, at("dissolve", 4), {"c002"}, ctx) == []
    assert rules.problems(base, at("dissolve", 6), {"c002"}, ctx) == [
        "changes the transition the editor pin s0->s1 protects (cut -> dissolve 6f; the pin holds a dissolve 4f, the only other transition "
        "allowed there)"]
    assert rules.problems(at("dissolve", 4), at("dissolve", 6), {"c002"}, ctx) == [
        "changes the transition the editor pin s0->s1 protects (dissolve 4f -> dissolve 6f)"]
    assert P._pin_problems(base, at("dissolve", 6), ctx) == [("s0->s1", rules.pin_problems(base, at("dissolve", 6), ctx)[0][1])]
    assert P._pin_problems(base, at("dissolve", 4), ctx) == [] and P._pin_problems(base, base, ctx) == []


def test_a_revision_with_a_third_transition_is_refused_at_publish(make_ws):
    """The protection is taken after the revisions were made: r001 has a dissolve of 4, the pin holds r002's dissolve of 6, r000 a cut."""
    w = make_ws()
    PinStore(w).put(pinned_final(w))
    S.start(w)
    r1 = edit(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])
    edit(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 6}])
    V.pin_transition(w, pin="s0->s1", hold=True)
    with pytest.raises(Refused, match=rf"pin s0->s1 was added or changed since {r1} was made, and {r1} changes the transition the editor pin "
                                      r"s0->s1 protects \(cut -> dissolve 4f; the pin holds a dissolve 6f, the only other transition allowed "
                                      r"there\)"):
        P.publish(w, r1)
    assert not (w / "edit.json").exists()
    assert P.publish(w)["revision"] == "r002"
