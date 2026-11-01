"""The session store (``matchcut.edit.session``) on small real workspaces: start and r000, transactions, apply re-validation, optimistic
concurrency, two processes under the lock, branches, reserved ids, staleness and the stale move-aside, determinism, id validation and
history."""
from __future__ import annotations

import hashlib
import json
import multiprocessing
from fractions import Fraction
from pathlib import Path
from typing import Dict

import pytest

from matchcut.edit import session as S
from matchcut.edit.model import EditContext, Refused, frame_rows
from matchcut.edit.rules import problems
from matchcut.interchange.common import layout
from matchcut.pipeline.audio import MusicAnalysis
from matchcut.pipeline.editorial import Pin, PinStore
from matchcut.pipeline.timeline import Clip, Timeline, Transition, clip_origin

from .util import click

TRIM = [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}]
TRIM4 = [{"op": "trim", "clip": "c004", "edge": "out", "frames": -4}]
INSERT = [{"op": "insert", "shot": 7, "in_frame": 710, "out_frame": 760, "after": "c005"}]
TOO_SHORT = [{"op": "trim", "clip": "c002", "edge": "out", "frames": -45}]  # 50 -> 5 frames: under the minimum (19 at 25 fps)
OUT_OF_SHOT = [{"op": "trim", "clip": "c002", "edge": "out", "frames": 40}]  # past the end of shot 1 (frame 200)


def tree(ws: Path) -> Dict[str, bytes]:
    """Every file under ``ws`` (the workspace and its session store), by relative path, with its bytes."""
    return {p.relative_to(ws).as_posix(): p.read_bytes() for p in sorted(ws.rglob("*")) if p.is_file()}


def outside_edits(ws: Path) -> Dict[str, bytes]:
    return {k: v for k, v in tree(ws).items() if not k.startswith("edits")}


def sha12(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def state(ws: Path) -> dict:
    return json.loads((ws / "edits" / "session.json").read_text())


def revision(ws: Path, rev: str) -> dict:
    return json.loads((ws / "edits" / "revisions" / f"{rev}.json").read_text())


def touch_final(ws: Path) -> None:
    """The pipeline ran again: final.json is rewritten with another cut."""
    tl = Timeline.load(ws / "final.json")
    tl.clips[0].source_out = round(tl.clips[0].source_out - 0.2, 6)
    tl.save(ws / "final.json")


@pytest.fixture
def ws(make_ws) -> Path:
    w = make_ws()
    S.start(w)
    return w


# ----------------------------------------------------------------------------------------------- start and r000
def test_start_writes_r000_and_the_session_without_touching_the_workspace(make_ws):
    w = make_ws()
    before = tree(w)
    out = S.start(w)
    assert outside_edits(w) == before
    st = state(w)
    assert st["schema"] == S.SESSION_SCHEMA and st["head"] == "r000" and st["next"] == 1
    assert st["base_stage"] == "final" == out["base_stage"] and st["base_file"] == "final.json"
    assert st["base_sha256"] == hashlib.sha256((w / "final.json").read_bytes()).hexdigest()
    assert st["reserved_ids"] == ["c001", "c002", "c003", "c004", "c005"] and st["rate"] == "25"
    assert len(st["session"]) == 36 and out["session"] == st["session"]
    r0 = revision(w, "r000")
    assert r0["parent"] is None and r0["ops"] == [] and r0["base"]["sha256"] == st["base_sha256"]
    base = Timeline.load(w / "final.json")
    tl = Timeline.from_dict(r0["timeline"])
    assert tl.stage == S.EDIT_STAGE and r0["timeline_sha256"] == S.timeline_sha256(tl)
    assert [d.to_dict() for d in tl.history] == [d.to_dict() for d in base.history]
    assert [c.origin for c in tl.clips] == [clip_origin(c) for c in base.clips]  # identity stamped before anything moves
    assert {p.name for p in (w / "edits").iterdir()} == {".lock", "session.json", "revisions", "proposals"}


def test_start_takes_the_latest_stage_or_the_named_one(make_ws):
    w = make_ws()
    assert S.start(w, stage="roughcut")["base_stage"] == "roughcut"
    assert state(w)["base_file"] == "roughcut.json"
    with pytest.raises(S.SessionError, match="no stage 'nope'.*roughcut, tidy, final"):
        S.start(w, stage="nope", restart=True)
    assert state(w)["base_stage"] == "roughcut"  # the refusal changed nothing
    assert S.start(w, restart=True)["base_stage"] == "final"


def test_start_refuses_an_open_session_and_restart_moves_it_aside_intact(ws):
    S.apply(ws, S.propose(ws, TRIM)["proposal"])
    with pytest.raises(S.SessionError, match="a session is open on final \\(head r001\\): pass restart"):
        S.start(ws)
    old = tree(ws / "edits")
    out = S.start(ws, restart=True)
    aside = ws / f"edits.replaced-{sha12(ws / 'final.json')}"  # a fresh session replaced on request; a stale one is edits.stale-...
    assert out["moved_aside"] == aside.name
    assert tree(aside) == {k: v for k, v in old.items() if k != ".lock"}
    assert state(ws)["head"] == "r000" and S.load(ws).revision_ids() == ["r000"]


def test_a_start_that_never_finished_is_moved_aside(make_ws):
    w = make_ws()
    (w / "edits" / "revisions").mkdir(parents=True)
    (w / "edits" / "revisions" / "r000.json").write_text("{}")
    out = S.start(w)
    assert out["moved_aside"] == "edits.stale-incomplete"
    assert (w / "edits.stale-incomplete" / "revisions" / "r000.json").read_text() == "{}"


def test_an_unreadable_session_is_refused_until_restart(ws):
    (ws / "edits" / "session.json").write_text("{not json")
    with pytest.raises(S.SessionError, match="unreadable"):
        S.propose(ws, TRIM)
    with pytest.raises(S.SessionError, match="unreadable: pass restart"):
        S.start(ws)
    assert S.start(ws, restart=True)["moved_aside"] == "edits.stale-unreadable"
    assert (ws / "edits.stale-unreadable" / "session.json").read_text() == "{not json"


def test_r000_puts_an_older_workspace_on_whole_frames_without_rewriting_it(make_ws):
    """29.97 fps, source points off the frame grid and a 0.5 s dissolve (14.985 frames): r000 is on whole frames, the files are not."""
    clips = [("c001", 0, 0.4, 2.1234, "b1"), ("c002", 1, 3.5, 5.2, "b1", ("dissolve", 0.5)), ("c003", 3, 10.5, 12.0, "b2"),
             ("c004", 5, 17.0, 19.0, "b2", ("dip_to_black", 0.3)), ("c005", 6, 20.5, 22.0, "b3", ("dissolve", 0.01))]
    w = make_ws(rate="30000/1001", clips=clips, seconds=True)
    before = outside_edits(w)
    out = S.start(w)
    assert outside_edits(w) == before
    rate = Fraction(30000, 1001)
    tl = S.load(w).timeline("r000")
    for c in tl.clips:
        for x in (c.source_in, c.source_out, c.transition_in.duration):
            assert abs(float(x * rate) - round(float(x * rate))) < 1e-4, (c.id, x)
    rows = frame_rows(tl, rate)
    assert [(r.kind, r.tr) for r in rows] == [("cut", 0), ("dissolve", 15), ("cut", 0), ("dip_to_black", 9), ("cut", 0)]
    assert [(r.in_f, r.out_f) for r in rows][:2] == [(12, 64), (105, 156)]
    assert any("c002: incoming dissolve 0.5 s" in n for n in out["normalised"])
    assert any("c005: incoming dissolve 0.01 s" in n and "a cut" in n for n in out["normalised"])
    # the session works on it: a proposal touching c001 leaves every other clip identical
    p = S.propose(w, [{"op": "trim", "clip": "c001", "edge": "in", "frames": 2}])
    assert p["changes"] == ["c001: in +2f"]


def test_normalise_gives_exactly_the_frames_layout_gives_even_at_a_shot_end_off_the_grid():
    """A point inside its shot keeps the frame M2a's layout rounds it to, though the shot's end is off the frame grid; it is in bounds."""
    ctx = EditContext(Fraction(25), {0: (0.01, 1.99), 1: (1.99, 4.0)})
    tl = Timeline(source="s.mp4", fps=25.0)
    tl.clips = [Clip("c001", 0, 0.012, 1.985), Clip("c002", 1, 2.0, 3.0, transition_in=Transition("dissolve", 0.1))]
    out, notes = S.normalise(tl, ctx)
    assert [(r.in_f, r.out_f, r.tr) for r in frame_rows(out, ctx.rate)] == [(r.in_f, r.out_f, r.tr) for r in layout(tl, ctx.rate)]
    assert [(ctx.f(c.source_in), ctx.f(c.source_out)) for c in out.clips] == [(0, 50), (50, 75)]
    assert ctx.shot_frames(0) == (1, 49)  # what an operation may place; r000's 0 and 50 are the base's own (tolerated) excess
    assert out.clips[0].origin == "0@0.012-1.985" and tl.clips[0].origin == ""  # the input is not changed
    assert notes == ["c001: in 0.012 s -> frame 0 (-0.30f), out 1.985 s -> frame 50 (+0.38f)",
                     "c002: incoming dissolve 0.1 s (2.50f) -> 3 frames"]
    assert problems(out, out, set(), ctx) == []


# ----------------------------------------------------------------------------------------------- proposals and transactions
@pytest.mark.parametrize("ops,match", [
    ([], "ops is empty"),
    ("trim", "ops must be a list"),
    ({"op": "trim"}, "ops must be a list"),
    ([{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}, {"op": "spin"}], "op 2 \\(spin\\): unknown op 'spin'"),
    ([{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}, {"op": "remove", "clip": "c099"}], "op 2 \\(remove\\)"),
    (TRIM + TOO_SHORT, "op 2 \\(trim\\): c002 would have no frames"),
    (OUT_OF_SHOT, "refused: |op 1"),
    ([{"op": "trim", "clip": "c002", "edge": "out", "frames": float("nan")}], "whole number|plain JSON"),
])
def test_a_refused_proposal_writes_nothing(ws, ops, match):
    before = tree(ws)
    with pytest.raises(Refused, match=match):
        S.propose(ws, ops)
    assert tree(ws) == before


def test_a_refusal_by_the_rules_carries_its_problems_and_timing(ws):
    before = tree(ws)
    with pytest.raises(S.ProposalRefused) as err:
        S.propose(ws, TRIM, guards={"max_duration_change_frames": 2})
    assert err.value.problems == ["guard max_duration_change_frames: the running time changes by -5 frames (at most 2 allowed)"]
    assert err.value.timing["duration_change"] == -5 and err.value.changes == ["c002: out -5f"]
    assert tree(ws) == before
    with pytest.raises(S.SessionError, match="guards must be an object"):
        S.propose(ws, TRIM, guards=["max_duration_change_frames"])
    assert tree(ws) == before


def test_a_proposal_that_changes_nothing_is_refused(ws):
    before = tree(ws)
    with pytest.raises(S.ProposalRefused, match="change nothing"):
        S.propose(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}, {"op": "trim", "clip": "c002", "edge": "out", "frames": 5}])
    assert tree(ws) == before


def test_an_accepted_proposal_is_stored_under_its_deterministic_id_and_proposing_again_is_idempotent(ws):
    p = S.propose(ws, TRIM, guards={"max_duration_change_frames": 10})
    assert p["problems"] == [] and p["changes"] == ["c002: out -5f"] and p["timing"]["duration_change"] == -5
    assert p["touched"] == ["c002"] and p["head"] == "r000" and p["details"] == ["trim c002 out -5f"]
    s = S.load(ws)
    head = s.timeline("r000")
    assert p["proposal"] == S.proposal_id("r000", head, TRIM, {"max_duration_change_frames": 10}, s.reserved_ids(),
                                          S.context_sha256(s.context()))
    path = ws / "edits" / "proposals" / f"{p['proposal']}.json"
    doc = json.loads(path.read_text())
    assert doc["head"] == "r000" and doc["ops"] == TRIM and doc["timeline_sha256"] == S.timeline_sha256(Timeline.from_dict(doc["timeline"]))
    data, mtime = path.read_bytes(), path.stat().st_mtime_ns
    assert S.propose(ws, TRIM, guards={"max_duration_change_frames": 10}) == p
    assert path.read_bytes() == data and path.stat().st_mtime_ns == mtime
    assert S.propose(ws, TRIM)["proposal"] != p["proposal"]  # the guards are part of the id
    assert state(ws)["head"] == "r000"  # proposing never moves the head


# ----------------------------------------------------------------------------------------------- apply
def test_apply_writes_the_next_immutable_revision_and_moves_the_head(ws):
    p = S.propose(ws, TRIM)
    out = S.apply(ws, p["proposal"])
    assert out["revision"] == "r001" and out["parent"] == "r000" and out["changes"] == p["changes"]
    st = state(ws)
    assert st["head"] == "r001" and st["next"] == 2
    r1 = revision(ws, "r001")
    stored = json.loads((ws / "edits" / "proposals" / f"{p['proposal']}.json").read_text())
    assert r1["parent"] == "r000" and r1["proposal"] == p["proposal"] and r1["ops"] == TRIM and r1["timing"] == p["timing"]
    assert r1["timeline"] == stored["timeline"] and r1["timeline_sha256"] == stored["timeline_sha256"]
    with pytest.raises(S.SessionError, match="head moved: proposal .* was made at r000, the head is now r001"):
        S.apply(ws, p["proposal"])  # applying twice
    with pytest.raises(S.SessionError, match="no proposal 0123456789ab"):
        S.apply(ws, "0123456789ab")


def tamper_timeline(doc: dict) -> None:
    doc["timeline"]["clips"][1]["source_out"] = round(doc["timeline"]["clips"][1]["source_out"] - 0.04, 6)


def tamper_ops(doc: dict) -> None:
    doc["ops"][0]["frames"] = -6


@pytest.mark.parametrize("how", ["timeline", "ops", "both", "changes", "guards"])
def test_apply_refuses_a_proposal_file_that_was_changed(ws, how):
    p = S.propose(ws, TRIM)
    path = ws / "edits" / "proposals" / f"{p['proposal']}.json"
    doc = json.loads(path.read_text())
    if how == "timeline":
        tamper_timeline(doc)
    elif how == "ops":
        tamper_ops(doc)
    elif how == "both":  # consistently: the ops and everything they give, as a proposal of those ops would store them, under the old id
        other = S.propose(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -6}])
        doc = dict(json.loads((ws / "edits" / "proposals" / f"{other['proposal']}.json").read_text()), id=p["proposal"])
    elif how == "changes":
        doc["changes"] = ["nothing to see"]
    else:
        doc["guards"] = {"max_duration_change_frames": 100}
    path.write_text(json.dumps(doc, indent=2, sort_keys=True))
    before = tree(ws)
    with pytest.raises(S.SessionError, match=f"proposal {p['proposal']} was changed after it was stored"):
        S.apply(ws, p["proposal"])
    assert tree(ws) == before and state(ws)["head"] == "r000"


def test_apply_refuses_a_proposal_made_under_another_context(ws):
    """The workspace's limits changed since the proposal (here: it was made under a higher transition limit): apply says so and writes
    nothing; under the same context it applies."""
    ctx = S.workspace_context(ws)
    lax = EditContext(ctx.rate, ctx.bounds, ctx.min_clip_seconds, 0.5, ctx.pins, ctx.music)
    p = S.propose(ws, [{"op": "set_transition", "junction": "s3-s5", "kind": "dissolve", "frames": 24}], ctx=lax)  # 24 <= 50% of 50
    before = tree(ws)
    with pytest.raises(S.SessionError, match=f"proposal {p['proposal']} was made before the workspace changed .*propose again"):
        S.apply(ws, p["proposal"])
    assert tree(ws) == before
    assert S.apply(ws, p["proposal"], ctx=lax)["revision"] == "r001"


# ----------------------------------------------------------------------------------------------- optimistic concurrency
def test_two_proposals_from_one_head_only_the_first_applied_wins(ws):
    a, b = S.propose(ws, TRIM), S.propose(ws, TRIM4)
    S.apply(ws, a["proposal"])
    before = tree(ws)
    with pytest.raises(S.SessionError, match=f"head moved: proposal {b['proposal']} was made at r000, the head is now r001"):
        S.apply(ws, b["proposal"])
    assert tree(ws) == before


def test_after_undo_a_proposal_from_the_old_head_is_refused(ws):
    S.apply(ws, S.propose(ws, TRIM)["proposal"])
    p = S.propose(ws, TRIM4)  # made at r001
    S.undo(ws)
    with pytest.raises(S.SessionError, match="head moved: .* made at r001, the head is now r000"):
        S.apply(ws, p["proposal"])


def _apply_in_process(ws: str, pid: str, barrier, results) -> None:
    barrier.wait()
    try:
        results.put(("ok", S.apply(Path(ws), pid)["revision"]))
    except Refused as exc:
        results.put(("refused", str(exc)))
    except Exception as exc:  # pragma: no cover - reported to the test
        results.put(("error", repr(exc)))


def _edit_loop(ws: str, clip: str, wins: int, results) -> None:
    """Trim ``clip`` by a frame ``wins`` times, proposing again whenever the other process moved the head first."""
    done, lost = 0, 0
    try:
        while done < wins:
            p = S.propose(Path(ws), [{"op": "trim", "clip": clip, "edge": "out", "frames": -1}])
            try:
                S.apply(Path(ws), p["proposal"])
                done += 1
            except S.SessionError as exc:
                assert str(exc).startswith("head moved"), exc
                lost += 1
        results.put(("ok", clip, lost))
    except Exception as exc:  # pragma: no cover - reported to the test
        results.put(("error", clip, repr(exc)))


def test_two_processes_applying_competing_proposals_under_the_lock(ws):
    mp = multiprocessing.get_context("fork")
    a, b = S.propose(ws, TRIM), S.propose(ws, TRIM4)
    barrier, results = mp.Barrier(2), mp.Queue()
    procs = [mp.Process(target=_apply_in_process, args=(str(ws), pid, barrier, results)) for pid in (a["proposal"], b["proposal"])]
    for p in procs:
        p.start()
    got = sorted(results.get(timeout=60) for _ in procs)
    for p in procs:
        p.join(timeout=60)
    assert [g[0] for g in got] == ["ok", "refused"], got
    assert got[0][1] == "r001" and got[1][1].startswith("head moved")
    st = state(ws)
    assert st["head"] == "r001" and st["next"] == 2 and S.load(ws).revision_ids() == ["r000", "r001"]


def test_two_processes_editing_in_a_loop_leave_a_contiguous_linear_history(ws):
    mp = multiprocessing.get_context("fork")
    results = mp.Queue()
    procs = [mp.Process(target=_edit_loop, args=(str(ws), clip, 3, results)) for clip in ("c002", "c004")]
    for p in procs:
        p.start()
    got = [results.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    assert all(g[0] == "ok" for g in got), got
    s = S.load(ws)
    assert s.revision_ids() == [f"r{n:03d}" for n in range(7)]
    assert s.head == "r006" and s.state["next"] == 7
    assert [s.revision(f"r{n:03d}")["parent"] for n in range(1, 7)] == [f"r{n:03d}" for n in range(6)]
    assert S.compare(ws, "r000", "r006")["changes"] == ["c002: out -3f", "c004: out -3f"]


# ----------------------------------------------------------------------------------------------- undo, redo, checkout, branches
def test_undo_redo_and_checkout_over_branches_move_only_the_head(ws):
    with pytest.raises(S.SessionError, match="nothing to undo: the head is r000"):
        S.undo(ws)
    with pytest.raises(S.SessionError, match="nothing to redo: no revision was made from r000"):
        S.redo(ws)
    S.apply(ws, S.propose(ws, TRIM)["proposal"])  # r001
    S.undo(ws)
    S.apply(ws, S.propose(ws, TRIM4)["proposal"])  # r002, a branch from r000
    assert revision(ws, "r002")["parent"] == "r000"
    revs = {k: v for k, v in tree(ws / "edits").items() if k.startswith(("revisions/", "proposals/"))}
    assert S.undo(ws) == {"head": "r000", "previous": "r002"}
    assert S.redo(ws) == {"head": "r002", "previous": "r000", "branches": ["r001", "r002"]}  # the latest branch
    assert S.checkout(ws, "r001") == {"head": "r001", "previous": "r002"}  # the old branch
    with pytest.raises(S.SessionError, match="nothing to redo"):
        S.redo(ws)
    assert S.undo(ws)["head"] == "r000"
    assert S.checkout(ws, "r000")["head"] == "r000"
    with pytest.raises(S.SessionError, match="no revision r009"):
        S.checkout(ws, "r009")
    after = {k: v for k, v in tree(ws / "edits").items() if k.startswith(("revisions/", "proposals/"))}
    assert after == revs  # nothing deleted, nothing rewritten
    assert S.load(ws).revision_ids() == ["r000", "r001", "r002"]
    # a new edit from r001's branch continues the numbering
    S.checkout(ws, "r001")
    out = S.apply(ws, S.propose(ws, TRIM4)["proposal"])
    assert out["revision"] == "r003" and out["parent"] == "r001"


def test_a_new_clip_id_is_never_issued_twice_across_undo(ws):
    first = S.apply(ws, S.propose(ws, INSERT)["proposal"])
    assert first["changes"] == ["added e001 (shot 7, frames 710-760)"]
    S.undo(ws)
    assert "e001" in state(ws)["reserved_ids"]
    second = S.propose(ws, INSERT)
    assert second["changes"] == ["added e002 (shot 7, frames 710-760)"]
    S.apply(ws, second["proposal"])
    assert [c.id for c in S.load(ws).timeline().clips][-1] == "e002"


def test_an_orphan_revision_a_crash_left_is_never_overwritten(ws):
    S.apply(ws, S.propose(ws, TRIM)["proposal"])
    r1 = (ws / "edits" / "revisions" / "r001.json").read_bytes()
    st = state(ws)
    st.update(head="r000", next=1)  # as if the process died after writing r001 and before session.json
    (ws / "edits" / "session.json").write_text(json.dumps(st))
    out = S.apply(ws, S.propose(ws, TRIM4)["proposal"])
    assert out["revision"] == "r002" and (ws / "edits" / "revisions" / "r001.json").read_bytes() == r1
    assert state(ws)["next"] == 3


# ----------------------------------------------------------------------------------------------- compare, log, load
def test_compare_and_log(ws):
    p1 = S.propose(ws, TRIM)
    S.apply(ws, p1["proposal"])
    S.apply(ws, S.propose(ws, [{"op": "set_transition", "junction": "s3-s5", "kind": "dissolve", "frames": 6}])["proposal"])
    c = S.compare(ws, "r000", "r002")
    assert c["changes"] == ["c002: out -5f", "c003|c004: cut -> dissolve 6f"] and c["timing"]["duration_change"] == -11
    assert c["stale"] is False and S.compare(ws, "r001")["b"] == "r002"
    back = S.compare(ws, "r002", "r000")
    assert back["changes"] == ["c002: out +5f", "c003|c004: dissolve 6f -> cut"]
    lg = S.log(ws)
    assert lg["head"] == "r002" and lg["stale"] is False and lg["base_stage"] == "final"
    assert [(r["id"], r["parent"], r["ops"]) for r in lg["revisions"]] == [("r000", None, []), ("r001", "r000", ["trim"]),
                                                                           ("r002", "r001", ["set_transition"])]
    assert lg["revisions"][1]["summary"] == "trim c002 out -5f" and lg["revisions"][1]["proposal"] == p1["proposal"]
    assert all(r["created"] for r in lg["revisions"])


def test_load_gives_the_head_timeline_and_the_context(ws):
    S.apply(ws, S.propose(ws, TRIM)["proposal"])
    s = S.load(ws)
    assert s.head == "r001" and s.base_stage == "final" and s.stale() is None
    assert s.timeline().to_dict() == revision(ws, "r001")["timeline"]
    ctx = s.context()
    assert ctx.rate == Fraction(25) and ctx.bounds[1] == (4.0, 8.0) and ctx.min_clip_seconds == 0.75 and ctx.music is None
    with pytest.raises(S.SessionError, match="no edit session"):
        S.load(ws.parent / "elsewhere")


def test_without_a_session_every_command_says_run_start(make_ws):
    w = make_ws()
    for call in (lambda: S.propose(w, TRIM), lambda: S.apply(w, "0123456789ab"), lambda: S.undo(w), lambda: S.redo(w),
                 lambda: S.checkout(w, "r000"), lambda: S.compare(w, "r000"), lambda: S.log(w)):
        with pytest.raises(S.SessionError, match="no edit session .*run start first"):
            call()
    assert not (w / "edits").exists()


# ----------------------------------------------------------------------------------------------- staleness
def test_a_stale_session_refuses_every_change_and_reads_say_stale(ws):
    S.apply(ws, S.propose(ws, TRIM)["proposal"])
    p = S.propose(ws, TRIM4)
    touch_final(ws)
    before = tree(ws)
    for call in (lambda: S.propose(ws, TRIM4), lambda: S.apply(ws, p["proposal"]), lambda: S.undo(ws), lambda: S.redo(ws),
                 lambda: S.checkout(ws, "r000")):
        with pytest.raises(S.SessionError, match="the edit session is stale: final.json \\(the base stage final\\) changed"):
            call()
    assert tree(ws) == before
    lg = S.log(ws)
    assert lg["stale"] is True and "final.json" in lg["stale_reason"]
    c = S.compare(ws, "r000", "r001")
    assert c["stale"] is True and c["changes"] == ["c002: out -5f"]
    assert S.load(ws).stale().startswith("final.json")


def test_start_moves_a_stale_session_aside_and_never_over_an_earlier_one(ws):
    original = (ws / "final.json").read_bytes()
    S.apply(ws, S.propose(ws, TRIM)["proposal"])
    first_sha = sha12(ws / "final.json")
    touch_final(ws)
    first = {k: v for k, v in tree(ws / "edits").items() if k != ".lock"}
    out = S.start(ws)  # no restart needed: it is stale
    aside = ws / f"edits.stale-{first_sha}"
    assert out["moved_aside"] == aside.name and tree(aside) == first
    assert state(ws)["base_sha256"][:12] == sha12(ws / "final.json") != first_sha
    # final.json goes back to its first bytes: the second session is stale and moved aside next to the first
    (ws / "final.json").write_bytes(original)
    second_sha = state(ws)["base_sha256"][:12]
    second = {k: v for k, v in tree(ws / "edits").items() if k != ".lock"}
    assert S.start(ws)["moved_aside"] == f"edits.stale-{second_sha}"
    assert tree(ws / f"edits.stale-{second_sha}") == second
    # a third session on the first base goes stale too: its name is taken, so it gets a suffix and the first stays intact
    S.apply(ws, S.propose(ws, TRIM4)["proposal"])
    touch_final(ws)
    third = {k: v for k, v in tree(ws / "edits").items() if k != ".lock"}
    assert S.start(ws)["moved_aside"] == f"edits.stale-{first_sha}-2"
    assert tree(ws / f"edits.stale-{first_sha}-2") == third and tree(aside) == first


def test_a_session_on_the_human_stage_is_stale_once_that_stage_is(ws):
    """A human.json whose canonical stage changed is no longer a stage, so a session on it is stale though human.json is unchanged."""
    final = Timeline.load(ws / "final.json")
    final.stage = "human"
    final.save(ws / "human.json")
    sha = hashlib.sha256((ws / "final.json").read_bytes()).hexdigest()
    (ws / "human_import.json").write_text(json.dumps({"stage": "final", "stage_sha256": sha, "latest_stage": "final", "latest_stage_sha256": sha}))
    assert S.start(ws, restart=True)["base_stage"] == "human"
    S.propose(ws, TRIM)
    touch_final(ws)
    with pytest.raises(S.SessionError, match="human.json \\(the base stage human\\) is stale itself"):
        S.propose(ws, TRIM)


# ----------------------------------------------------------------------------------------------- determinism and ids
def run_script(w: Path) -> dict:
    S.start(w)
    ids = [S.propose(w, TRIM)["proposal"]]
    S.apply(w, ids[-1])
    ids.append(S.propose(w, INSERT)["proposal"])
    S.apply(w, ids[-1])
    S.undo(w)
    ids.append(S.propose(w, [{"op": "set_transition", "junction": 3, "kind": "dissolve", "frames": 4}])["proposal"])
    revs = [S.apply(w, ids[-1])["revision"]]
    docs = {r: {k: v for k, v in revision(w, r).items() if k != "created"} for r in S.load(w).revision_ids()}
    timelines = {r: S.canonical_json(d["timeline"]).encode() for r, d in docs.items()}
    return {"ids": ids, "revs": revs, "docs": docs, "timelines": timelines,
            "proposals": {p.name: p.read_bytes() for p in sorted((w / "edits" / "proposals").iterdir())}}


def test_two_runs_give_byte_identical_revision_timelines_and_the_same_ids(make_ws):
    a, b = run_script(make_ws("one")), run_script(make_ws("two"))
    assert a["ids"] == b["ids"] and a["revs"] == b["revs"] == ["r003"]
    assert a["timelines"] == b["timelines"] and a["docs"] == b["docs"]
    assert a["proposals"] == b["proposals"]  # proposals carry no timestamp


@pytest.mark.parametrize("bad", ["../x", "r1", "R001", "r01", "r001\n", "r001/../r002", "", None, 1, "r٠٠١"])
def test_revision_ids_are_checked_before_any_path_is_built(ws, bad):
    before = tree(ws)
    calls = [lambda: S.checkout(ws, bad)]
    if bad is not None:  # compare and Session.revision read None as the head
        calls += [lambda: S.compare(ws, bad, "r000"), lambda: S.compare(ws, "r000", bad), lambda: S.load(ws).revision(bad)]
    for call in calls:
        with pytest.raises(S.SessionError, match="not a revision id"):
            call()
    assert tree(ws) == before


@pytest.mark.parametrize("bad", ["../x", "ABCDEF123456", "abcdef12345", "abcdef1234567", "abcdef12345g", "abcdef123456\n", "", None])
def test_proposal_ids_are_checked_before_any_path_is_built(ws, bad):
    before = tree(ws)
    with pytest.raises(S.SessionError, match="not a proposal id"):
        S.apply(ws, bad)
    assert tree(ws) == before


# ----------------------------------------------------------------------------------------------- history
def test_applied_revisions_carry_the_base_history_plus_one_agent_decision_per_change(ws):
    base = Timeline.load(ws / "final.json").history
    S.apply(ws, S.propose(ws, TRIM + [{"op": "set_transition", "junction": "s3-s5", "kind": "dissolve", "frames": 6},
                                      {"op": "slip", "clip": "c005", "frames": 3}])["proposal"])
    S.apply(ws, S.propose(ws, INSERT + [{"op": "move", "clip": "c001", "after": "c003"}, {"op": "remove", "clip": "c004"}])["proposal"])
    tl = S.load(ws).timeline()
    assert [d.to_dict() for d in tl.history[:len(base)]] == [d.to_dict() for d in base]
    agent = [(d.pass_name, d.action, d.clip_ids, d.detail) for d in tl.history[len(base):]]
    r1, r2 = revision(ws, "r001"), revision(ws, "r002")
    assert [a[3] for a in agent] == r1["changes"] + r2["changes"]
    assert agent[:3] == [("agent", "trimmed", ["c002"], "c002: out -5f"), ("agent", "transition", ["c003", "c004"], "c003|c004: cut -> dissolve 6f"),
                         ("agent", "slipped", ["c005"], "c005: slipped +3f")]
    by_action = {a[1]: a for a in agent[3:]}
    assert by_action["removed"][2] == ["c004"] and by_action["added"][2] == ["e001"]
    assert set(by_action["moved"][2]) >= {"c001"} and len(by_action["moved"][2]) <= 3
    assert all(d.data["proposal"] == r1["proposal"] for d in tl.history[len(base):len(base) + len(r1["changes"])])


# ----------------------------------------------------------------------------------------------- the workspace's context
def test_the_context_comes_from_the_workspace(ws):
    s = S.load(ws)
    tl = s.timeline()
    a, b = tl.clips[0], tl.clips[1]
    PinStore(ws).put(Pin(a.shot_index, b.shot_index, round(a.source_out, 3), round(b.source_in, 3), "locked", "", clip_origin(a), clip_origin(b)))
    music = click(beat=0.5)
    music.path = str(ws / "score.wav")
    music.save(ws / "music.json")
    cfg = json.loads((ws / "config.json").read_text())
    cfg["music"].update(path=str(ws / "score.wav"), start=1.5)
    cfg["transitions"]["max_fraction_of_clip"] = 0.3
    (ws / "config.json").write_text(json.dumps(cfg))
    ctx = S.workspace_context(ws)
    assert ctx.rate == Fraction(25) and len(ctx.pins) == 1 and ctx.pins[0].out_shot == 0
    assert isinstance(ctx.music, MusicAnalysis) and ctx.music_start == 1.5 and ctx.max_fraction_of_clip == 0.3
    cfg["music"]["path"] = str(ws / "other.wav")  # an analysis of another track is not this cut's music
    (ws / "config.json").write_text(json.dumps(cfg))
    assert S.workspace_context(ws).music is None
    (ws / "probe.json").write_text(json.dumps(dict(json.loads((ws / "probe.json").read_text()), path=str(ws / "gone.mp4"))))
    cfg["source"] = str(ws / "gone.mp4")
    (ws / "config.json").write_text(json.dumps(cfg))
    with pytest.raises(S.SessionError, match="cannot read the source footage"):
        S.workspace_context(ws)
    with pytest.raises(S.SessionError, match="cannot read the source footage"):
        S.propose(ws, TRIM)


def test_the_context_uses_the_exact_rate_of_a_29_97_source(make_ws):
    w = make_ws(rate="30000/1001")
    assert S.workspace_context(w).rate == Fraction(30000, 1001)
    assert S.start(w)["normalised"] == []  # built on whole frames already


# ----------------------------------------------------------------------------------------------- review fixes
def test_one_proposal_id_never_names_two_results(ws):
    """I1: the id covers the reserved clip ids, so the same insert at the same head after apply+undo is another proposal (e002), and
    neither proposal file is rewritten."""
    p1 = S.propose(ws, INSERT)
    f1 = ws / "edits" / "proposals" / f"{p1['proposal']}.json"
    b1 = f1.read_bytes()
    S.apply(ws, p1["proposal"])
    S.undo(ws)
    p2 = S.propose(ws, INSERT)
    assert p2["proposal"] != p1["proposal"] and p2["changes"] == ["added e002 (shot 7, frames 710-760)"]
    assert f1.read_bytes() == b1
    with pytest.raises(S.SessionError, match="head moved|issued other clip ids"):
        S.apply(ws, p1["proposal"])
    r = S.apply(ws, p2["proposal"])["revision"]
    assert revision(ws, "r001")["proposal"] == p1["proposal"] and revision(ws, r)["proposal"] == p2["proposal"]


def test_apply_names_the_reserved_ids_that_changed_since_the_proposal(ws):
    p = S.propose(ws, TRIM)  # at r000, before e001 was issued
    S.apply(ws, S.propose(ws, INSERT)["proposal"])
    S.undo(ws)  # the head is r000 again, but e001 is reserved now
    before = tree(ws)
    with pytest.raises(S.SessionError, match=f"proposal {p['proposal']} was made before the session issued other clip ids"):
        S.apply(ws, p["proposal"])
    assert tree(ws) == before


def test_the_proposal_id_covers_the_context(ws):
    ctx = S.workspace_context(ws)
    ids = {S.propose(ws, TRIM, ctx=c)["proposal"] for c in (
        ctx,
        EditContext(ctx.rate, ctx.bounds, ctx.min_clip_seconds, 0.5, ctx.pins, ctx.music),
        EditContext(ctx.rate, ctx.bounds, ctx.min_clip_seconds, ctx.max_fraction_of_clip, ctx.pins, click(beat=0.5)),
        EditContext(ctx.rate, ctx.bounds, ctx.min_clip_seconds, ctx.max_fraction_of_clip, ctx.pins, click(beat=0.5), 1.0),
        EditContext(ctx.rate, {**ctx.bounds, 7: (28.0, 32.4)}, ctx.min_clip_seconds, ctx.max_fraction_of_clip, ctx.pins, None),
    )}
    assert len(ids) == 5
    assert S.context_sha256(ctx) == S.context_sha256(S.workspace_context(ws))  # deterministic


def test_a_proposal_file_with_other_content_is_never_overwritten(ws):
    p = S.propose(ws, TRIM)
    path = ws / "edits" / "proposals" / f"{p['proposal']}.json"
    doc = json.loads(path.read_text())
    doc["changes"] = ["edited by hand"]
    path.write_text(json.dumps(doc))
    before = tree(ws)
    with pytest.raises(S.SessionError, match="exists with other content"):
        S.propose(ws, TRIM)
    assert tree(ws) == before


def test_every_id_a_batch_issued_is_reserved_even_one_it_removed(ws):
    """I2: insert, remove, insert in one batch issues e001 and e002; both are reserved, so the next insert is e003."""
    batch = INSERT + [{"op": "remove", "clip": "e001"}, {"op": "insert", "shot": 7, "in_frame": 712, "out_frame": 760, "after": "c005"}]
    p = S.propose(ws, batch)
    assert p["issued"] == ["e001", "e002"] and p["changes"] == ["added e002 (shot 7, frames 712-760)"]
    r = S.apply(ws, p["proposal"])["revision"]
    assert revision(ws, r)["issued"] == ["e001", "e002"] and {"e001", "e002"} <= set(state(ws)["reserved_ids"])
    assert S.propose(ws, INSERT)["changes"] == ["added e003 (shot 7, frames 710-760)"]


def test_apply_ops_reports_the_ids_it_issued():
    from matchcut.edit.ops import apply_ops
    from .util import ctx_for, cut
    ctx = ctx_for()
    tl = cut(ctx, [("c001", 0, 10, 60), ("c002", 1, 110, 160)])
    res = apply_ops(tl, [{"op": "insert", "shot": 2, "in_frame": 210, "out_frame": 260, "after": "c002"}, {"op": "remove", "clip": "e001"},
                         {"op": "replace", "clip": "c001", "shot": 3}, {"op": "trim", "clip": "c002", "edge": "out", "frames": -1}], ctx)
    assert res.issued == ("e001", "e002") and [c.id for c in res.timeline.clips] == ["e002", "c002"]


def pinned_at_an_off_grid_shot_end(make_ws, rate: str, out_frames: Fraction) -> Path:
    """Shot 0 ends at frame 60.6; c001's out-point is pinned (apply_pins clamps it to the shot end), then put at ``out_frames``."""
    from matchcut.pipeline.editorial import apply_pins
    from matchcut.pipeline.shots import Shot, load_shots, save_shots
    w = make_ws(rate=rate)
    R = Fraction(rate)
    shots = [Shot(k, float(Fraction(k * 100) / R), float(Fraction((k + 1) * 100) / R)) for k in range(8)]
    shots[0] = Shot(0, 0.0, float(Fraction(606, 10) / R))
    save_shots(shots, w / "shots.json")
    final = Timeline.load(w / "final.json")
    for c in final.clips:
        c.origin = clip_origin(c)
    final.clips[0].source_out = round(float(Fraction(60) / R), 6)
    pin = Pin(0, 1, float(Fraction(61) / R), final.clips[1].source_in, out_origin=final.clips[0].origin, in_origin=final.clips[1].origin)
    pinned, decisions = apply_pins(final, [pin], {s.index: (s.start, s.end) for s in load_shots(w / "shots.json")}, 0.75)
    assert decisions[-1].action == "pinned"
    pinned.clips[0].source_out = round(float(out_frames / R), 6)
    pinned.stage = "final"
    pinned.save(w / "final.json")
    PinStore(w).put(pin)
    return w


@pytest.mark.parametrize("rate", ["25", "24000/1001"])
def test_r000_keeps_a_pinned_out_point_at_an_off_grid_shot_end_on_layouts_frame(make_ws, rate):
    """I3: the pinned out-point sits at the shot's end, frame 60.6: r000 has it on frame 61, as M2a's layout does; the pin is still held
    (so remove/insert/move next to it are refused) and the note names the pin."""
    from matchcut.edit.ops import held_pins
    w = pinned_at_an_off_grid_shot_end(make_ws, rate, Fraction(606, 10))
    out = S.start(w)
    s = S.load(w)
    ctx, base, r0 = s.context(), Timeline.load(w / "final.json"), s.timeline("r000")
    assert [(r.in_f, r.out_f, r.tr) for r in frame_rows(r0, ctx.rate)] == [(r.in_f, r.out_f, r.tr) for r in layout(base, ctx.rate)]
    assert frame_rows(r0, ctx.rate)[0].out_f == 61 and ctx.shot_frames(0) == (0, 60)  # 61: the base's own excess, tolerated
    assert [p.key for p, _ in held_pins(r0, ctx)] == [p.key for p, _ in held_pins(base, ctx)] == [(0, 1)]
    assert any(n.startswith("c001: out") and "pin s0->s1" in n for n in out["normalised"])
    for ops in ([{"op": "remove", "clip": "c002"}], [{"op": "insert", "shot": 7, "in_frame": 710, "out_frame": 760, "after": "c001"}],
                [{"op": "move", "clip": "c002", "after": "c005"}], [{"op": "roll", "junction": 1, "frames": -3}]):
        with pytest.raises(Refused, match="pin"):
            S.propose(w, ops)


def test_start_refuses_rather_than_release_a_pin_when_rounding_moves_its_edge_too_far(make_ws):
    """The base holds the pin with its out-point 0.2 frame from the pinned frame (60.4 vs 60.6); on whole frames it would be 0.6 frame
    away and no longer held: start refuses, naming the pin, and writes nothing."""
    w = pinned_at_an_off_grid_shot_end(make_ws, "25", Fraction(604, 10))
    before = tree(w)
    with pytest.raises(S.SessionError, match="would release s0->s1"):
        S.start(w)
    assert tree(w) == before


def test_ids_in_an_orphan_revision_are_reserved(ws):
    """M1: a crash after writing r001 but before session.json: r001's ids (e001) are still never issued again."""
    S.apply(ws, S.propose(ws, INSERT)["proposal"])
    st = state(ws)
    st.update(head="r000", next=1, reserved_ids=["c001", "c002", "c003", "c004", "c005"])
    (ws / "edits" / "session.json").write_text(json.dumps(st))
    p = S.propose(ws, [{"op": "insert", "shot": 4, "in_frame": 410, "out_frame": 460, "after": "c005"}])
    assert p["changes"] == ["added e002 (shot 4, frames 410-460)"]
    assert S.apply(ws, p["proposal"])["revision"] == "r002"


def test_a_session_whose_source_rate_changed_is_stale(ws):
    """M2: the context's rate differs from the session's: every change refuses, the reads say stale, start moves it aside."""
    S.apply(ws, S.propose(ws, TRIM)["proposal"])
    ctx = S.workspace_context(ws)
    other = EditContext(Fraction(30), ctx.bounds, ctx.min_clip_seconds, ctx.max_fraction_of_clip, ctx.pins, ctx.music)
    before = tree(ws)
    for call in (lambda: S.propose(ws, TRIM4, ctx=other), lambda: S.undo(ws, ctx=other), lambda: S.checkout(ws, "r000", ctx=other)):
        with pytest.raises(S.SessionError, match="stale: the source footage's frame rate is 30 now, the session was made at 25"):
            call()
    assert tree(ws) == before
    assert S.compare(ws, "r000", "r001", ctx=other)["stale"] is True
    assert S.load(ws, other).stale().startswith("the source footage's frame rate")
    assert S.start(ws, ctx=other)["moved_aside"].startswith("edits.stale-")


@pytest.mark.parametrize("field,value", [
    ("base_sha256", "../../../x"), ("base_sha256", "A" * 64), ("base_sha256", None), ("reserved_ids", [1, None]), ("reserved_ids", "c001"),
    ("head", "../r001"), ("head", 1), ("next", "3"), ("next", True), ("next", 10 ** 30), ("next", 0), ("base_file", "/etc/passwd"),
    ("base_file", "../final.json"), ("rate", "zero"), ("rate", "0"), ("session", 5), ("schema", "other/1"), ("base_stage", ""),
])
def test_a_malformed_session_json_is_a_clean_error(ws, field, value):
    """M3: every field is checked before it is used; a malformed one gives one SessionError, and restart moves the file aside."""
    st = state(ws)
    st[field] = value
    (ws / "edits" / "session.json").write_text(json.dumps(st))
    before = tree(ws.parent)
    for call in (lambda: S.propose(ws, TRIM), lambda: S.apply(ws, "0123456789ab"), lambda: S.undo(ws), lambda: S.redo(ws),
                 lambda: S.checkout(ws, "r000"), lambda: S.compare(ws, "r000"), lambda: S.log(ws), lambda: S.load(ws), lambda: S.start(ws)):
        with pytest.raises(S.SessionError, match="edits/session.json is unreadable"):
            call()
    assert tree(ws.parent) == before
    assert S.start(ws, restart=True)["moved_aside"] == "edits.stale-unreadable"


def test_a_changed_revision_file_is_refused(ws):
    """M4: the head revision's timeline was edited by hand: reading it is refused, so nothing is built on it; log lists the error."""
    S.apply(ws, S.propose(ws, TRIM)["proposal"])
    path = ws / "edits" / "revisions" / "r001.json"
    doc = json.loads(path.read_text())
    doc["timeline"]["clips"][0]["source_out"] = 1.0
    path.write_text(json.dumps(doc))
    before = tree(ws)
    with pytest.raises(S.SessionError, match="revision r001 was changed after it was written"):
        S.propose(ws, TRIM4)
    with pytest.raises(S.SessionError, match="revision r001 was changed"):
        S.undo(ws)
    assert tree(ws) == before
    assert [r.get("error", "")[:30] for r in S.log(ws)["revisions"]] == ["", "revision r001 was changed afte"]
    S.checkout(ws, "r000")  # another revision is still usable
    assert S.apply(ws, S.propose(ws, TRIM4)["proposal"])["revision"] == "r002"


@pytest.mark.parametrize("ops,guards,match", [
    ([{"op": "roll", "junction": "²", "frames": 1}], None, "no junction ²"),
    ([{"op": "roll", "junction": "١", "frames": 1}], None, "no junction ١"),
    ([{"op": "roll", "junction": "1" * 5000, "frames": 1}], None, "no junction 111111111111\\.\\.\\."),
    ([{"op": ["trim"]}], None, "unknown op \\['trim'\\]"),
    ([{"op": "trim", "clip": "c002", "edge": "out", "frames": 10 ** 30}], None, "frames is out of range"),
    ([{"op": "trim", "clip": "c002", "edge": "out", "frames": 1e300}], None, "frames is out of range"),
    (TRIM, {"keep_record_frames": ["²"]}, "guard keep_record_frames: no junction ²"),
    (TRIM, {"keep_record_frames": ["9" * 5000]}, "guard keep_record_frames: no junction 999"),
])
def test_malformed_operations_and_guards_are_refused_never_a_traceback(ws, ops, guards, match):
    """M5."""
    before = tree(ws)
    with pytest.raises(Refused, match=match):
        S.propose(ws, ops, guards)
    assert tree(ws) == before


def test_notes_say_one_frame_not_one_frames():
    ctx = EditContext(Fraction(25), {0: (0.0, 4.0), 1: (4.0, 8.0)})
    tl = Timeline(source="s.mp4", fps=25.0)
    tl.clips = [Clip("c001", 0, 0.0, 2.0), Clip("c002", 1, 4.0, 6.0, transition_in=Transition("dip_to_black", 0.05))]
    assert S.normalise(tl, ctx)[1] == ["c002: incoming dip_to_black 0.05 s (1.25f) -> 1 frame"]


@pytest.mark.parametrize("edit", ["parent", "ops", "changes", "proposal", "remove_hash"])
def test_a_revision_whose_parent_ops_or_reports_were_changed_is_refused(ws, edit):
    """The whole revision document but its time is hashed (document_sha256): a hand-edited parent or operations is refused, and log
    shows the revision as an error entry; a changed time alone is accepted."""
    S.apply(ws, S.propose(ws, TRIM)["proposal"])
    path = ws / "edits" / "revisions" / "r001.json"
    original = json.loads(path.read_text())
    assert original["document_sha256"] == S.document_sha256(original)
    doc = dict(original)
    if edit == "remove_hash":
        del doc["document_sha256"]
    else:
        doc[edit] = {"parent": None, "ops": [{"op": "remove", "clip": "c001"}], "changes": [], "proposal": "0123456789ab"}[edit]
    path.write_text(json.dumps(doc))
    before = tree(ws)
    with pytest.raises(S.SessionError, match="revision r001 was changed after it was written"):
        S.undo(ws)
    assert tree(ws) == before
    assert "error" in S.log(ws)["revisions"][1]
    path.write_text(json.dumps(dict(original, created="2000-01-01T00:00:00+00:00")))  # the time is not covered
    assert S.undo(ws)["head"] == "r000"


@pytest.mark.parametrize("ops,guards,match", [
    ([{"op": "trim", "clip": "c002", "edge": "out", "frames": -10 ** 12}], None,
     "frames is out of range: at most 1000000000 frames either way, not -10000000000"),
    ([{"op": "trim", "clip": "c002", "edge": "out", "frames": 1e300}], None, "frames is out of range: at most 1000000000 frames either way, not 1e\\+300"),
    ([{"op": "replace", "clip": "c002", "shot": 10 ** 8}], None, "shot is out of range: at most 1000000 \\(a shot index\\) either way"),
    ([{"op": "insert", "shot": -10 ** 8, "in_frame": 0, "out_frame": 30, "after": "c001"}], None, "shot is out of range"),
    (TRIM, {"max_duration_change_frames": 10 ** 400}, "guard max_duration_change_frames must be a whole number of frames from 0 to 1000000000, "
                                                      "not 10000000000000000000\\.\\.\\."),
])
def test_out_of_range_numbers_are_refused_in_plain_words(ws, ops, guards, match):
    before = tree(ws)
    with pytest.raises(Refused, match=match) as err:
        S.propose(ws, ops, guards)
    assert "-more" not in str(err.value) and len(str(err.value)) < 300
    assert tree(ws) == before
