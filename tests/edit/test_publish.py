"""M3a task 4: publishing a session as the ``edit`` stage, its staleness, the stage order, the session base, and the junctions of the
edit stage (attribution of the agent's decisions, pin badges)."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from matchcut.edit import publish as P
from matchcut.edit import session as S
from matchcut.edit.model import Refused
from matchcut.pipeline.editorial import Pin, PinStore, apply_pins, pin_states
from matchcut.pipeline.junctions import (EDIT_STAGE, build_junctions, junction_line, latest_canonical, load_stage_timelines, stage_timeline_paths,
                                         stale_edit, stale_human)
from matchcut.pipeline.timeline import AGENT_PASS, Decision, Timeline, clip_origin


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def protected(w: Path) -> dict:
    """Every file publishing must never touch: final, the pass files, the rough cut, human*.json."""
    files = [w / "final.json", w / "roughcut.json", w / "human.json", w / "human_import.json"] + sorted((w / "passes").glob("*.json"))
    return {p.name: p.read_bytes() for p in files if p.exists()}


def edit(w: Path, ops) -> str:
    p = S.propose(w, ops)
    return S.apply(w, p["proposal"])["revision"]


@pytest.fixture
def ws(make_ws) -> Path:
    w = make_ws()
    S.start(w)
    return w


def stages(w: Path):
    return [s for s, p in stage_timeline_paths(w) if p.exists()]


# ----------------------------------------------------------------------------------------------- publish
def test_r000_is_never_published(ws):
    with pytest.raises(Refused, match="nothing to publish: r000 is the base stage on whole frames, with no agent edit"):
        P.publish(ws)
    with pytest.raises(Refused, match="nothing to publish"):
        P.publish(ws, "r000")
    edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    S.undo(ws)  # the head is r000 again
    with pytest.raises(Refused, match="nothing to publish"):
        P.publish(ws)
    assert not (ws / "edit.json").exists() and not (ws / "edit_publish.json").exists()


def test_publish_writes_the_edit_stage_its_record_and_an_immutable_copy(ws):
    before = protected(ws)
    final = Timeline.load(ws / "final.json")
    rid = edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5},
                    {"op": "set_transition", "junction": 3, "kind": "dissolve", "frames": 6}])
    out = P.publish(ws)
    s = S.load(ws)
    data = (ws / "edit.json").read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    rec = json.loads((ws / "edit_publish.json").read_text())
    assert {k: v for k, v in rec.items() if k != "published_at"} == {
        "schema": "matchcut.edit_publish/1", "session": s.session_id, "revision": rid, "timeline_sha256": s.revision(rid)["timeline_sha256"],
        "edit_file": "edit.json", "edit_sha256": digest, "base_stage": "final", "base_file": "final.json", "base_sha256": sha(ws / "final.json"),
        "latest_stage": "final", "latest_stage_file": "final.json", "latest_stage_sha256": sha(ws / "final.json"), "decisions": 2,
        "published": f"edits/published/{rid}-{digest[:12]}.json"}
    assert rec["published_at"] and out["replaced"] is None and out["previous"] is None and out["edit_sha256"] == digest
    assert (ws / rec["published"]).read_bytes() == data
    tl = Timeline.load(ws / "edit.json")
    assert tl.stage == EDIT_STAGE and S.timeline_sha256(tl) == rec["timeline_sha256"]
    # the history is the latest canonical stage's plus one agent decision per change, each naming its proposal
    assert [d.to_dict() for d in tl.history[: len(final.history)]] == [d.to_dict() for d in final.history]
    new = tl.history[len(final.history):]
    assert [d.pass_name for d in new] == [AGENT_PASS] * 2 and all(d.data.get("proposal") for d in new)
    assert stale_edit(ws) is None and stages(ws) == ["roughcut", "tidy", "final", "edit"]
    assert [s for s, _ in load_stage_timelines(ws)][-1] == "edit"
    assert protected(ws) == before  # final, the pass files and the rough cut are untouched
    assert not list(ws.glob(".*.tmp")) and not list((ws / "edits" / "published").glob(".*"))


def test_publishing_again_from_the_same_session_replaces_edit_json_and_keeps_the_earlier_copy(ws):
    r1 = edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    first = P.publish(ws)
    r2 = edit(ws, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    second = P.publish(ws)
    assert (first["revision"], second["revision"], second["previous"], second["replaced"]) == (r1, r2, r1, None)
    copies = sorted(p.name for p in (ws / "edits" / "published").glob("r*.json"))
    assert copies == sorted([Path(first["published"]).name, Path(second["published"]).name])
    assert (ws / first["published"]).read_bytes() != (ws / "edit.json").read_bytes() == (ws / second["published"]).read_bytes()
    assert not (ws / "edits" / "replaced").exists()
    # an earlier revision may be published again: the same session, so it replaces edit.json too
    again = P.publish(ws, r1)
    assert again["revision"] == r1 and (ws / "edit.json").read_bytes() == (ws / first["published"]).read_bytes()
    assert len(list((ws / "edits" / "published").glob("r*.json"))) == 2  # the same bytes: the immutable copy is reused
    assert json.loads((ws / "edit_publish.json").read_text())["decisions"] == 1


def test_publishing_from_another_session_is_refused_then_moves_the_old_pair_aside_with_replace(ws):
    edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    first = P.publish(ws)
    old_pair = {n: (ws / n).read_bytes() for n in ("edit.json", "edit_publish.json")}
    S.start(ws, restart=True)  # another session, on final (never on the edit stage)
    assert S.load(ws).base_stage == "final" and S.load(ws).session_id != first["session"]
    assert (ws / first["published"]).exists()  # the archive stays in edits/ when the session is moved aside
    edit(ws, [{"op": "remove", "clip": "c003"}])
    with pytest.raises(Refused, match=r"edit.json was published by another session .*: publishing would replace it; pass replace"):
        P.publish(ws)
    assert {n: (ws / n).read_bytes() for n in old_pair} == old_pair
    out = P.publish(ws, replace=True)
    aside = ws / "edits" / "replaced" / first["edit_sha256"][:12]
    assert out["replaced"] == f"edits/replaced/{first['edit_sha256'][:12]}" and json.loads((ws / "edit_publish.json").read_text())["replaced"] == out["replaced"]
    assert {n: (aside / n).read_bytes() for n in old_pair} == old_pair
    assert json.loads((ws / "edit_publish.json").read_text())["session"] == S.load(ws).session_id
    # a second move-aside of an edit.json with the same hash (here: the first one, put back by hand without its record) never overwrites
    shutil.copy(aside / "edit.json", ws / "edit.json")
    with pytest.raises(Refused, match="edit.json is not the file edit_publish.json describes"):
        P.publish(ws)
    (ws / "edit_publish.json").unlink()
    with pytest.raises(Refused, match="edit.json has no readable edit_publish.json"):
        P.publish(ws)
    out2 = P.publish(ws, replace=True)
    assert out2["replaced"] == f"edits/replaced/{first['edit_sha256'][:12]}-2"
    assert {n: (aside / n).read_bytes() for n in old_pair} == old_pair  # the first move-aside is untouched
    assert sorted(p.name for p in (ws / out2["replaced"]).iterdir()) == ["edit.json"]


def test_a_corrupt_record_is_refused_without_replace(ws):
    """Another session meets an edit.json whose record is unreadable: refused, naming a possibly interrupted publish; replace moves it."""
    edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    P.publish(ws)
    (ws / "edit_publish.json").write_text("{not json")
    S.start(ws, restart=True)
    edit(ws, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    with pytest.raises(Refused, match=r"no readable edit_publish.json \(an interrupted publish\?\)"):
        P.publish(ws)
    assert P.publish(ws, replace=True)["replaced"]


def test_publishing_never_touches_final_the_passes_or_the_human_stage(ws, tmp_path):
    rec = {"schema": "matchcut.human_import/1", "stage": "final", "stage_file": "final.json", "stage_sha256": sha(ws / "final.json"),
           "latest_stage": "final", "latest_stage_file": "final.json", "latest_stage_sha256": sha(ws / "final.json")}
    human = Timeline.load(ws / "final.json")
    human.stage = "human"
    human.save(ws / "human.json")
    (ws / "human_import.json").write_text(json.dumps(rec))
    assert stale_human(ws) is None
    before = protected(ws)
    assert set(before) >= {"final.json", "01_tidy.json", "roughcut.json", "human.json", "human_import.json"}
    edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    P.publish(ws, supersede_human=True)
    edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -2}])
    P.publish(ws)  # (the human stage is stale already: nothing more to supersede)
    assert protected(ws) == before
    # the human stage, imported before the publish, continues final's history, not the edit stage's: it is left out now (never deleted)
    assert stale_human(ws) == ("human.json is stale: edit.json was published after the import, and the human history does not continue it "
                               "(re-export, or re-import the editor's file: matchcut diffs it against the stage it was exported from)")
    assert stages(ws)[-1] == "edit"


def test_publish_takes_a_named_revision_and_refuses_an_unknown_one(ws):
    r1 = edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    assert P.publish(ws, r1)["revision"] == r1
    with pytest.raises(Refused, match="no revision r009"):
        P.publish(ws, "r009")
    with pytest.raises(Refused, match="not a revision id"):
        P.publish(ws, "../x")


def test_a_stale_session_cannot_publish(ws):
    edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    final = Timeline.load(ws / "final.json")
    final.clips[0].source_in += 0.04
    final.save(ws / "final.json")
    with pytest.raises(Refused, match="the edit session is stale"):
        P.publish(ws)
    assert not (ws / "edit.json").exists()


@pytest.mark.parametrize("base", ["roughcut", "tidy", "human"])
def test_only_a_session_on_the_latest_canonical_stage_can_publish(make_ws, base):
    """The edit stage continues the latest canonical stage's history (decision 1): a session on an earlier stage, or on human, would put
    its base's history (and cut) where final's belongs."""
    w = make_ws()
    if base == "human":
        human = Timeline.load(w / "final.json")
        human.stage = "human"
        human.save(w / "human.json")
        (w / "human_import.json").write_text(json.dumps({"stage": "final", "stage_sha256": sha(w / "final.json"), "latest_stage": "final",
                                                         "latest_stage_sha256": sha(w / "final.json")}))
    S.start(w, stage=base)
    edit(w, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    with pytest.raises(Refused, match=rf"the session is based on {base}, not on the latest canonical stage final: .*\(start "
                                      r'\{"stage": "final", "restart": true\}'):
        P.publish(w)
    assert not (w / "edit.json").exists()


# ----------------------------------------------------------------------------------------------- the session base
def test_the_edit_stage_is_never_a_session_base(ws):
    edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    P.publish(ws)
    assert stages(ws)[-1] == "edit"
    out = S.start(ws, restart=True)  # the default base: the latest canonical stage
    assert out["base_stage"] == "final"
    with pytest.raises(Refused, match="the edit stage cannot be a session's base"):
        S.start(ws, stage="edit", restart=True)
    # and the session on final stays fresh after it publishes (publishing replaces edit.json, never final.json)
    edit(ws, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    P.publish(ws, replace=True)
    assert S.load(ws).stale() is None
    edit(ws, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 1}])
    assert P.publish(ws)["previous"] == "r001"


# ----------------------------------------------------------------------------------------------- staleness
def published(ws) -> bytes:
    edit(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}])
    P.publish(ws)
    assert stale_edit(ws) is None
    return (ws / "edit.json").read_bytes()


def left_out(ws, kept: bytes, why: str) -> None:
    assert stale_edit(ws) == why
    assert "edit" not in [s for s, _ in stage_timeline_paths(ws)] and "edit" not in [s for s, _ in load_stage_timelines(ws)]
    assert (ws / "edit.json").read_bytes() == kept  # left out, never deleted


HINT = "(start an edit session on the current stage and publish again: matchcut never rewrites or deletes edit.json)"


def test_edit_is_stale_when_the_latest_canonical_stage_changes(ws):
    kept = published(ws)
    final = Timeline.load(ws / "final.json")
    final.clips[0].source_in += 0.04
    final.save(ws / "final.json")
    left_out(ws, kept, f"edit.json is stale: final.json changed since it was published {HINT}")
    (ws / "final.json").unlink()  # gone: the latest canonical stage is another one now
    left_out(ws, kept, f"edit.json is stale: final.json changed since it was published {HINT}")


def test_edit_is_stale_when_its_base_stage_changes(ws):
    """The base is the latest canonical stage (publish refuses any other); a record naming another base is checked against that file."""
    kept = published(ws)
    rec = json.loads((ws / "edit_publish.json").read_text())
    rec.update(base_stage="tidy", base_file="passes/01_tidy.json", base_sha256=sha(ws / "passes" / "01_tidy.json"))
    (ws / "edit_publish.json").write_text(json.dumps(rec))
    assert stale_edit(ws) is None
    tidy = Timeline.load(ws / "passes" / "01_tidy.json")
    tidy.history.append(Decision("tidy", "note", [], "x"))
    tidy.save(ws / "passes" / "01_tidy.json")
    left_out(ws, kept, f"edit.json is stale: 01_tidy.json changed since it was published {HINT}")


def test_edit_is_stale_when_a_later_canonical_stage_appears(ws):
    kept = published(ws)
    rec = json.loads((ws / "edit_publish.json").read_text())
    rec.update(latest_stage="tidy", latest_stage_file="passes/01_tidy.json", latest_stage_sha256=sha(ws / "passes" / "01_tidy.json"))
    (ws / "edit_publish.json").write_text(json.dumps(rec))
    left_out(ws, kept, f"edit.json is stale: tidy is no longer the latest canonical stage {HINT}")


@pytest.mark.parametrize("damage", ["missing", "not json", "a list", "another schema", "no hash", "another edit.json"])
def test_edit_is_stale_without_a_readable_record_that_describes_it(ws, damage):
    kept = published(ws)
    rec_path = ws / "edit_publish.json"
    rec = json.loads(rec_path.read_text())
    if damage == "missing":
        rec_path.unlink()
    elif damage == "not json":
        rec_path.write_text("{not json")
    elif damage == "a list":
        rec_path.write_text("[]")
    elif damage == "another schema":
        rec_path.write_text(json.dumps(dict(rec, schema="matchcut.edit_publish/0")))
    elif damage == "no hash":
        rec_path.write_text(json.dumps({k: v for k, v in rec.items() if k != "edit_sha256"}))
    else:
        tl = Timeline.load(ws / "edit.json")
        tl.name = "edited by hand"
        tl.save(ws / "edit.json")
        kept = (ws / "edit.json").read_bytes()
    why = (f"edit.json is stale: it is not the file edit_publish.json describes (changed since it was published) {HINT}" if damage == "another edit.json"
           else f"edit.json is stale: edit_publish.json is missing or unreadable {HINT}")
    left_out(ws, kept, why)


def test_no_edit_json_is_not_stale(make_ws):
    w = make_ws()
    (w / "edit_publish.json").write_text("{}")
    assert stale_edit(w) is None and "edit" not in [s for s, _ in stage_timeline_paths(w)]
    assert latest_canonical(w) == ("final", w / "final.json")


# ----------------------------------------------------------------------------------------------- junctions of the edit stage
def test_agent_decisions_on_junctions_the_edit_created_are_attributed(ws):
    edit(ws, [{"op": "remove", "clip": "c003"}, {"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    edit(ws, [{"op": "insert", "shot": 7, "in_frame": 710, "out_frame": 760, "after": "c004"}])
    edit(ws, [{"op": "move", "clip": "c001", "after": "c002"}])
    P.publish(ws)
    js = build_junctions(load_stage_timelines(ws))
    by = {(j.out_clip, j.in_clip): j for j in js}
    assert list(by) == [("c002", "c001"), ("c001", "c004"), ("c004", "e001"), ("e001", "c005")]
    # removal (and the move) joined c001 and c004: the agent's own trim of c004 is attributed to it
    j = by[("c001", "c004")]
    assert [(d.pass_name, d.action) for d in j.decisions] == [("agent", "trimmed"), ("agent", "moved")]
    assert [s.stage for s in j.states] == ["edit"]
    # an insertion: both junctions of the new clip say the agent added it
    for pair in (("c004", "e001"), ("e001", "c005")):
        assert by[pair].why() == "made adjacent by edit: added e001 (shot 7, frames 710-760)"
        assert [(d.pass_name, d.action) for d in by[pair].decisions] == [("agent", "added")]
    # a move: the junction says so
    assert by[("c002", "c001")].why() == "agent: order: c002 c001 c004 e001 c005"
    # e001 overlaps no canonical clip; a clip inserted over a canonical clip's range is a new instance all the same (no inherited history)
    S.start(ws, restart=True)
    edit(ws, [{"op": "insert", "shot": 3, "in_frame": 320, "out_frame": 380, "after": "c005"}])
    P.publish(ws, replace=True)
    j = build_junctions(load_stage_timelines(ws))[-1]
    assert (j.out_clip, j.in_clip) == ("c005", "e001") and [s.stage for s in j.states] == ["edit"]
    assert all(d.pass_name == "agent" for d in j.decisions)


def test_a_junction_the_edit_joined_by_removal_says_so_and_keeps_the_agents_trim(ws):
    edit(ws, [{"op": "remove", "clip": "c003"}])
    edit(ws, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    P.publish(ws)
    j = next(j for j in build_junctions(load_stage_timelines(ws)) if (j.out_clip, j.in_clip) == ("c002", "c004"))
    assert j.why() == "made adjacent by edit: removed c003 (shot 3, frames 320-380)"
    assert [(d.action, j.decision_stage[k]) for k, d in enumerate(j.decisions)] == [("trimmed", "edit")]
    # an untouched junction keeps its canonical history: the tidy pass's trim of c002 still explains c001|c002
    j1 = build_junctions(load_stage_timelines(ws))[0]
    assert (j1.out_clip, j1.in_clip) == ("c001", "c002") and [s.stage for s in j1.states] == ["roughcut", "tidy", "final", "edit"]
    assert j1.why() == "tidy: tightened"


def pinned_final(w: Path) -> Pin:
    """final holds the editor pin s0->s1 (c001|c002), as the pipeline applies it."""
    tl = Timeline.load(w / "final.json")
    for x in tl.clips:
        x.origin = clip_origin(x)
    ctx = S.workspace_context(w)
    a, b = tl.clips[0], tl.clips[1]
    pin = Pin(a.shot_index, b.shot_index, round(a.source_out, 3), round(b.source_in, 3), "locked", "", clip_origin(a), clip_origin(b))
    out, decisions = apply_pins(tl, [pin], ctx.bounds, ctx.min_clip_seconds)
    assert decisions[-1].action == "pinned"
    out.save(w / "final.json")
    PinStore(w).put(pin)
    return pin


def test_the_edit_stage_keeps_the_pin_badge(make_ws):
    w = make_ws()
    pin = pinned_final(w)
    S.start(w)
    edit(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4},  # a pin's transition may change (decision 2)
             {"op": "trim", "clip": "c004", "edge": "in", "frames": 3}])
    P.publish(w)
    stages_ = load_stage_timelines(w)
    assert stages_[-1][0] == "edit"
    js = build_junctions(stages_, pins=PinStore(w).load())
    assert js[0].pinned and "pinned" in junction_line(js[0]) and js[0].pin_lost == ""
    assert not any(j.pinned for j in js[1:])
    assert [st.state for st in pin_states(stages_[-1][1], [pin], "edit")] == ["applied"]
    assert js[0].current.transition == "dissolve"


def test_the_junctions_cli_lists_the_edit_stage_and_says_when_it_is_stale(ws, capsys):
    from matchcut.pipeline.cli import main
    edit(ws, [{"op": "remove", "clip": "c003"}])
    P.publish(ws)
    assert main(["junctions", str(ws)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "3 junctions in edit (4 stages)" and "c002->c004" in out
    assert main(["junctions", str(ws), "--index", "2"]) == 0
    assert "why: made adjacent by edit: removed c003" in capsys.readouterr().out
    final = Timeline.load(ws / "final.json")
    final.clips[0].source_in += 0.04
    final.save(ws / "final.json")
    assert main(["junctions", str(ws)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "4 junctions in final (3 stages)" and "note: edit.json is stale: final.json changed since it was published" in out


def test_a_junction_the_edit_joined_by_a_move_says_so(ws):
    edit(ws, [{"op": "move", "clip": "c003", "after": "c005"}])  # c002 and c004 are joined by moving c003 out from between them
    P.publish(ws)
    j = next(j for j in build_junctions(load_stage_timelines(ws)) if (j.out_clip, j.in_clip) == ("c002", "c004"))
    assert j.decisions == [] and j.why() == "made adjacent by edit: order: c001 c002 c004 c005 c003"
