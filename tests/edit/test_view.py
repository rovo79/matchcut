"""``matchcut.edit.view`` without rendering: ``inspect`` (clips, junctions, shots and their use, story and music beats, pins and
protections, staleness), ``pin_transition`` and the refusals of ``preview`` (a refused, tampered, stale or outdated proposal is never
rendered)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import pytest

from matchcut.edit import session as S
from matchcut.edit import view as V
from matchcut.edit.model import Refused
from matchcut.edit.rules import describe
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore, apply_pins
from matchcut.pipeline.timeline import Timeline, clip_origin

from .test_session import TRIM, TRIM4, touch_final
from .util import click


@pytest.fixture
def ws(make_ws) -> Path:
    w = make_ws()
    S.start(w)
    return w


def pinned(make_ws, name: str = "ws") -> Path:
    """A workspace whose final cut holds the pin s0->s1 (c001|c002), released the pin s5->s6 (c004|c005: the editor trimmed c005's
    in-point by 4 frames after the pipeline applied it) and has never applied the pin s3->s6 (added since the last run)."""
    w = make_ws(name)
    tl = Timeline.load(w / "final.json")
    for x in tl.clips:
        x.origin = clip_origin(x)  # as the pipeline stamps them before it applies the pins
    ctx = S.workspace_context(w)
    c = {x.id: x for x in tl.clips}

    def pin_of(a: str, b: str) -> Pin:
        x, y = c[a], c[b]
        return Pin(x.shot_index, y.shot_index, round(x.source_out, 3), round(y.source_in, 3), "locked", "", clip_origin(x), clip_origin(y))

    p1, p2 = pin_of("c001", "c002"), pin_of("c004", "c005")
    p3 = Pin(3, 6, 15.0, 25.0)
    out, decisions = apply_pins(tl, [p1, p2], ctx.bounds, ctx.min_clip_seconds)
    assert [d.action for d in decisions] == ["pinned", "pinned"]
    c005 = out.clips[4]
    c005.source_in = round(c005.source_in + 4 / 25, 6)
    out.save(w / "final.json")
    for p in (p1, p2, p3):
        PinStore(w).put(p)
    return w


# ----------------------------------------------------------------------------------------------- inspect
def test_inspect_lists_clips_junctions_shots_and_story_beats(ws):
    p = S.propose(ws, [{"op": "set_transition", "junction": 3, "kind": "dissolve", "frames": 6}])
    S.apply(ws, p["proposal"])
    d = V.inspect(ws)
    assert d["session"]["head"] == "r001" == d["session"]["revision"] and d["session"]["is_head"] and d["session"]["revisions"] == 2
    assert d["session"]["stale"] is False and d["session"]["stale_reason"] is None and d["session"]["base_stage"] == "final"
    assert (d["rate"], d["fps"]) == ("25", 25.0)
    assert [c["id"] for c in d["clips"]] == ["c001", "c002", "c003", "c004", "c005"]
    c3 = d["clips"][2]
    assert (c3["shot"], c3["source_frames"], c3["frames"], c3["beat"]) == (3, [320, 380], 60, "b2")
    assert c3["transition_in"] == {"kind": "dissolve", "frames": 8} and c3["origin"] == c3["key"] and c3["origin"]
    assert c3["record_frames"] == [92, 152]
    j = d["junctions"]
    assert [x["id"] for x in j] == ["s0-s1", "s1-s3", "s3-s5", "s5-s6"] and [x["n"] for x in j] == [1, 2, 3, 4]
    assert j[2]["transition"] == {"kind": "dissolve", "frames": 6} and j[2]["record_frame"] == 146
    assert all(x["protection"] == [] and not x["pinned"] for x in j)
    assert d["duration_frames"] == 50 + 50 + 60 + 50 + 60 - 8 - 6 and d["duration_seconds"] == pytest.approx(d["duration_frames"] / 25)
    used = {s["shot"]: s["used_by"] for s in d["shots"]}
    assert used == {0: ["c001"], 1: ["c002"], 2: [], 3: ["c003"], 4: [], 5: ["c004"], 6: ["c005"], 7: []}
    assert d["unused_shots"] == [2, 4, 7] and d["unknown_shots"] == []
    assert d["shots"][3] == {"shot": 3, "frames": [300, 400], "length": 100, "used_by": ["c003"]}
    beats = d["story_beats"]
    assert [(b["beat"], b["clips"]) for b in beats] == [("b1", ["c001", "c002"]), ("b2", ["c003", "c004"]), ("b3", ["c005"])]
    assert beats[0]["record_frames"] == [0, 96] and beats[1]["record_frames"] == [96, 196] and beats[2]["record_frames"] == [196, 256]
    assert [b["frames"] for b in beats] == [96, 100, 60] and [b["n"] for b in beats] == [1, 2, 3]
    assert d["music"] is None and d["pins"] == [] and d["protections"] == []
    json.dumps(d)  # plain JSON


def test_inspect_an_older_revision_and_story_titles(ws):
    (ws / "narrative.json").write_text(json.dumps({"beats": [{"id": "b1", "title": "Arrival", "intent": "set the place"}]}))
    p = S.propose(ws, [{"op": "remove", "clip": "c004"}])
    S.apply(ws, p["proposal"])
    now, then = V.inspect(ws), V.inspect(ws, "r000")
    assert [c["id"] for c in now["clips"]] == ["c001", "c002", "c003", "c005"] and now["unused_shots"] == [2, 4, 5, 7]
    assert then["session"]["revision"] == "r000" and not then["session"]["is_head"] and len(then["clips"]) == 5
    assert now["story_beats"][0]["title"] == "Arrival" and now["story_beats"][1]["title"] is None
    with pytest.raises(S.SessionError, match="not a revision id"):
        V.inspect(ws, "../r000")
    with pytest.raises(S.SessionError, match="no revision r007"):
        V.inspect(ws, "r007")


def test_inspect_music_beats_and_offsets(make_ws):
    w = make_ws()
    cfg = PipelineConfig.load(w / "config.json")
    cfg.music.path = "click.wav"
    cfg.save(w / "config.json")
    click(beat=0.4).save(w / "music.json")
    S.start(w)
    d = V.inspect(w)
    m = d["music"]
    assert m["track"] == "click.wav" and m["tempo_bpm"] == 150.0 and m["beats_per_bar"] == 4 and m["start"] == 0.0
    assert m["beat_frames"][:4] == [0, 10, 20, 30] and m["downbeat_frames"][:3] == [0, 40, 80]
    assert m["beats_in_cut"] == len([f for f in range(0, d["duration_frames"] + 1, 10)])
    ctx = S.workspace_context(w)
    want = [x["beat_offset_frames"] for x in describe(S.load(w).timeline(), ctx)["junctions"]]
    assert [x["offset_frames"] for x in m["junctions"]] == want == [x["beat_offset_frames"] for x in d["junctions"]]
    assert m["junctions"][0] == {"n": 1, "id": "s0-s1", "record_frame": 50, "nearest_beat_frame": 50, "offset_frames": 0, "downbeat": False}
    assert m["junctions"][1]["record_frame"] == 92 and m["junctions"][1]["nearest_beat_frame"] == 90 and m["junctions"][1]["offset_frames"] == 2


def test_inspect_pins_protections_and_released_pins(make_ws):
    w = pinned(make_ws)
    S.start(w)
    d = V.inspect(w)
    pins = {p["pin"]: p for p in d["pins"]}
    assert set(pins) == {"s0->s1", "s5->s6", "s3->s6"}
    held = pins["s0->s1"]
    assert held["held"] and held["state"] == "held" and held["junction"] == "s0-s1" and held["n"] == 1 and held["clips"] == ["c001", "c002"]
    assert held["transition"] == {"kind": "cut", "frames": 0} and not held["hold_transition"] and not held["transition_mismatch"]
    released = pins["s5->s6"]
    assert not released["held"] and released["state"] == "released" and "trimmed by 4 frames (in)" in released["reason"]
    assert released["junction"] is None
    pending = pins["s3->s6"]
    assert pending["state"] == "pending" and "not applied" in pending["reason"]
    j = d["junctions"]
    assert j[0]["pinned"] and j[0]["pin"] == "s0->s1" and j[0]["protection"] == ["editor pin s0->s1: its clips stay adjacent with both cut points"]
    assert not j[3]["pinned"] and j[3]["protection"] == []
    assert d["protections"] == [{"n": 1, "junction": "s0-s1", "kind": "pin", "pin": "s0->s1", "transition": False, "held_transition": None,
                                 "transition_mismatch": False}]
    assert d["clips"][0]["locks"] == {"out": "the out-point of c001 is held by an editor pin"}


def test_inspect_shows_a_held_transition_that_differs_from_the_junctions(make_ws):
    """The protection is taken at r001 (a dissolve of 6); checking out r000 (a cut there) shows the mismatch."""
    w = pinned(make_ws)
    S.start(w)
    p = S.propose(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 6}])
    S.apply(w, p["proposal"])
    out = V.pin_transition(w, pin="s0->s1")
    assert out["held_transition"] == {"kind": "dissolve", "frames": 6} and not out["transition_mismatch"]
    S.checkout(w, "r000")
    d = V.inspect(w)
    p = next(x for x in d["pins"] if x["pin"] == "s0->s1")
    assert p["hold_transition"] and p["held_transition"] == {"kind": "dissolve", "frames": 6} and p["transition"] == {"kind": "cut", "frames": 0}
    assert p["transition_mismatch"] and "holds a dissolve of 6 frames" in p["reason"]
    j = d["junctions"][0]
    assert j["hold_transition"] and j["held_transition"] == {"kind": "dissolve", "frames": 6} and j["transition_mismatch"]
    assert j["protection"][1] == "editor pin s0->s1: its transition stays as it is (cut)"
    assert d["protections"][0]["transition"] and d["protections"][0]["transition_mismatch"]
    assert not V.inspect(w, "r001")["junctions"][0]["transition_mismatch"]
    V.pin_transition(w, pin="s0->s1")  # the head's transition: no mismatch any more
    assert not V.inspect(w)["junctions"][0]["transition_mismatch"]


def test_inspect_a_match_cut_is_a_protection(make_ws):
    w = make_ws()
    tl = Timeline.load(w / "final.json")
    a, b = tl.clips[3], tl.clips[4]
    a.locked_out = b.locked_in = True
    a.tags.append("match_out:motion")
    b.tags.append("match_in:motion")
    tl.save(w / "final.json")
    S.start(w)
    d = V.inspect(w)
    assert d["junctions"][3]["match_cut"] and d["junctions"][3]["protection"] == [
        "match cut: its clips stay adjacent with both cut points and a hard cut"]
    assert d["protections"] == [{"n": 4, "junction": "s5-s6", "kind": "match_cut", "pin": None, "transition": True, "held_transition": None,
                                 "transition_mismatch": False}]


def test_inspect_reports_a_stale_session(ws):
    touch_final(ws)
    d = V.inspect(ws)
    assert d["session"]["stale"] is True and "changed since this session started" in d["session"]["stale_reason"]
    assert [c["id"] for c in d["clips"]] == ["c001", "c002", "c003", "c004", "c005"]  # still readable


def test_inspect_without_a_session(make_ws):
    with pytest.raises(S.SessionError, match="no edit session"):
        V.inspect(make_ws())


# ----------------------------------------------------------------------------------------------- pin_transition
def editorial(w: Path) -> dict:
    return json.loads((w / "editorial.json").read_text())


def test_pin_transition_sets_and_clears_the_protection_touching_nothing_else(make_ws):
    w = pinned(make_ws)
    S.start(w)
    before = editorial(w)
    out = V.pin_transition(w, pin="s0->s1")
    assert out["changed"] and out["context_changed"] and out["hold_transition"]
    assert out["held_transition"] == {"kind": "cut", "frames": 0} and out["junction"] == {"n": 1, "id": "s0-s1"}
    assert out["previous"] == {"hold_transition": False, "held_transition": None} and "proposed again" in out["note"]
    after = editorial(w)
    assert after["pins"][1:] == before["pins"][1:] and set(after) == {"pins"}
    assert after["pins"][0] == dict(before["pins"][0], hold_transition=True, held_transition={"kind": "cut", "frames": 0})
    again = V.pin_transition(w, pin="s0->s1")  # the same: nothing is written
    assert not again["changed"] and "not written" in again["note"] and editorial(w) == after
    cleared = V.pin_transition(w, pin="s0->s1", hold=False)
    assert cleared["changed"] and not cleared["hold_transition"] and cleared["held_transition"] is None
    assert editorial(w) == before  # written as before the fields existed


def test_pin_transition_by_junction_and_explicit_transitions(make_ws):
    w = pinned(make_ws)
    S.start(w)
    out = V.pin_transition(w, junction="s0-s1", kind="cut", frames=0)  # explicit, and the head's: allowed
    assert out["pin"] == "s0->s1" and out["held_transition"] == {"kind": "cut", "frames": 0}
    assert V.pin_transition(w, junction=1.0, kind="cut")["held_transition"] == {"kind": "cut", "frames": 0}
    with pytest.raises(S.SessionError, match="no editor pin holds junction s1-s3"):
        V.pin_transition(w, junction=2)
    with pytest.raises(S.SessionError, match="pin s3->s6: it is not held in the head revision r000.*give kind and frames"):
        V.pin_transition(w, pin="s3->s6")
    out = V.pin_transition(w, pin="s3->s6", kind="dissolve", frames=5.0)  # not held at the head: an explicit transition is allowed
    assert out["junction"] is None and out["held_transition"] == {"kind": "dissolve", "frames": 5}


def test_pin_transition_refuses_a_transition_the_head_does_not_have_at_a_held_pin(make_ws):
    """Review fix: the rules keep a protected junction's transition as it is, so holding another one there could never be satisfied."""
    w = pinned(make_ws)
    S.start(w)
    before = (w / "editorial.json").read_bytes()
    for kind, frames in (("dissolve", 6), ("dip_to_black", 4)):
        with pytest.raises(S.SessionError, match=r"pin s0->s1 is held at junction s0-s1 of the head revision r000, where the transition is "
                                                 r"a cut: .*could never be reached there.*Omit kind and frames"):
            V.pin_transition(w, pin="s0->s1", kind=kind, frames=frames)
    assert (w / "editorial.json").read_bytes() == before
    p = S.propose(w, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 6}])
    S.apply(w, p["proposal"])
    assert V.pin_transition(w, pin="s0->s1", kind="dissolve", frames=6)["held_transition"] == {"kind": "dissolve", "frames": 6}


def test_pin_transition_keeps_what_pinstore_does_not_understand(make_ws):
    """Review fix: a malformed pin entry, unknown top-level keys and unknown pin fields survive a pin_transition (``PinStore.put``
    dropped them)."""
    w = pinned(make_ws)
    S.start(w)
    raw = editorial(w)
    raw["pins"].insert(1, {"out_shot": "x", "in_shot": 2})
    raw["pins"][0]["future_field"] = "keep me"
    raw["version"] = 3
    raw["notes"] = "the editor's note"
    (w / "editorial.json").write_text(json.dumps(raw, indent=2))
    V.pin_transition(w, pin="s0->s1")
    after = editorial(w)
    assert after["version"] == 3 and after["notes"] == "the editor's note" and after["pins"][1] == {"out_shot": "x", "in_shot": 2}
    assert after["pins"][0] == dict(raw["pins"][0], hold_transition=True, held_transition={"kind": "cut", "frames": 0})
    assert after["pins"][2:] == raw["pins"][2:]
    V.pin_transition(w, pin="s0->s1", hold=False)
    assert editorial(w) == raw
    assert [pin_label_of(p) for p in PinStore(w).load()] == ["s0->s1", "s5->s6", "s3->s6"]


def pin_label_of(p: Pin) -> str:
    return f"s{p.out_shot}->s{p.in_shot}"


def test_pin_transition_lists_only_proposals_it_makes_stale(make_ws):
    w = pinned(make_ws)
    S.start(w)
    old = S.propose(w, TRIM)
    V.pin_transition(w, pin="s3->s6", kind="dissolve", frames=4)  # old is stale now
    live = S.propose(w, TRIM)
    out = V.pin_transition(w, pin="s0->s1")
    assert out["proposals_to_propose_again"] == [live["proposal"]] and old["proposal"] != live["proposal"]


@pytest.mark.parametrize("kw, message", [
    ({"pin": "s9->s9"}, r"no pin s9->s9 in editorial.json \(pins: s0->s1, s5->s6, s3->s6\)"),
    ({"pin": "../s0->s1"}, "a pin is named by its label"),
    ({"pin": "s0->s1", "junction": 1}, "one of them"),
    ({}, "one of them"),
    ({"pin": "s0->s1", "kind": "wipe", "frames": 3}, "unknown transition 'wipe'"),
    ({"pin": "s0->s1", "kind": "dissolve", "frames": 1.5}, "whole number of frames"),
    ({"pin": "s0->s1", "kind": "dissolve", "frames": True}, "whole number of frames"),
    ({"pin": "s0->s1", "kind": "dissolve", "frames": "3"}, "whole number of frames"),
    ({"pin": "s0->s1", "kind": "dissolve", "frames": -2}, "from 0 to"),
    ({"pin": "s0->s1", "kind": "dissolve", "frames": 10 ** 400}, "from 0 to 1000000000, not out of range"),
    ({"pin": "s0->s1", "kind": "dissolve", "frames": 0}, "needs at least 1 frame, not 0"),
    ({"pin": "s0->s1", "kind": "dissolve"}, "needs at least 1 frame"),
    ({"pin": "s0->s1", "kind": "cut", "frames": 3}, "a cut has no frames"),
    ({"pin": "s0->s1", "frames": 3}, "frames needs kind"),
    ({"pin": "s0->s1", "hold": False, "kind": "cut"}, "takes no kind or frames"),
    ({"pin": "s0->s1", "hold": "yes"}, "hold must be true or false"),
])
def test_pin_transition_refusals_write_nothing(make_ws, kw, message):
    w = pinned(make_ws)
    S.start(w)
    before = (w / "editorial.json").read_bytes()
    with pytest.raises(S.SessionError, match=message):
        V.pin_transition(w, **kw)
    assert (w / "editorial.json").read_bytes() == before


def test_an_older_pin_without_the_fields_loads_and_can_be_protected(make_ws):
    w = pinned(make_ws)
    raw = editorial(w)
    assert all("hold_transition" not in p for p in raw["pins"])  # written as before the fields existed
    S.start(w)
    assert not V.inspect(w)["pins"][0]["hold_transition"]
    V.pin_transition(w, pin="s0->s1")
    assert V.inspect(w)["pins"][0]["hold_transition"]


def test_a_protected_transition_refuses_set_transition_naming_the_pin(make_ws):
    w = pinned(make_ws)
    S.start(w)
    op = [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 6}]
    S.propose(w, op)  # unprotected: a pinned junction may change its transition
    V.pin_transition(w, pin="s0->s1")
    with pytest.raises(Refused, match="protected by the editor pin s0->s1"):
        S.propose(w, op)
    V.pin_transition(w, pin="s0->s1", hold=False)
    S.propose(w, op)


def test_pin_transition_makes_earlier_proposals_stale(make_ws):
    w = pinned(make_ws)
    S.start(w)
    p = S.propose(w, TRIM)
    out = V.pin_transition(w, pin="s0->s1")
    assert out["proposals_to_propose_again"] == [p["proposal"]]
    with pytest.raises(S.SessionError, match="before the workspace changed"):
        S.apply(w, p["proposal"])
    with pytest.raises(S.SessionError, match="before the workspace changed"):
        V.preview(w, p["proposal"])
    q = S.propose(w, TRIM)
    assert q["proposal"] != p["proposal"]
    S.apply(w, q["proposal"])


def test_pin_transition_without_a_session(make_ws):
    w = pinned(make_ws)
    with pytest.raises(S.SessionError, match="no edit session"):
        V.pin_transition(w, junction=1)
    with pytest.raises(S.SessionError, match="there is no edit session.*give kind and frames"):
        V.pin_transition(w, pin="s0->s1")
    out = V.pin_transition(w, pin="s0->s1", kind="dissolve", frames=4)
    assert out["changed"] and out["proposals_to_propose_again"] == [] and not (w / "edits").exists()


# ----------------------------------------------------------------------------------------------- preview refusals
def renders(w: Path) -> list:
    d = w / "edits" / "renders"
    return sorted(p.name for p in d.rglob("*")) if d.exists() else []


def test_preview_refuses_a_refused_unknown_or_tampered_proposal(ws):
    with pytest.raises(Refused):
        S.propose(ws, [{"op": "trim", "clip": "c002", "edge": "out", "frames": -45}])
    with pytest.raises(S.SessionError, match="no proposal 0123456789ab"):
        V.preview(ws, "0123456789ab")
    with pytest.raises(S.SessionError, match="not a proposal id"):
        V.preview(ws, "../../final")
    p = S.propose(ws, TRIM)
    path = ws / "edits" / "proposals" / f"{p['proposal']}.json"
    doc = json.loads(path.read_text())
    doc["timeline"]["clips"][0]["source_in"] += 0.04
    path.write_text(json.dumps(doc))
    with pytest.raises(S.SessionError, match="changed after it was stored"):
        V.preview(ws, p["proposal"])
    assert renders(ws) == [] and S.load(ws).revision_ids() == ["r000"]


def test_preview_refuses_a_stale_session_or_a_moved_head(ws):
    a, b = S.propose(ws, TRIM), S.propose(ws, TRIM4)
    S.apply(ws, a["proposal"])
    with pytest.raises(S.SessionError, match="head moved"):
        V.preview(ws, b["proposal"])
    c = S.propose(ws, TRIM4)
    touch_final(ws)
    with pytest.raises(S.SessionError, match="the edit session is stale"):
        V.preview(ws, c["proposal"])
    assert renders(ws) == [] and S.load(ws).revision_ids() == ["r000", "r001"]


def test_check_proposal_writes_nothing(ws):
    p = S.propose(ws, TRIM)
    files = sorted(x.relative_to(ws).as_posix() for x in ws.rglob("*") if x.is_file())
    doc = S.check_proposal(ws, p["proposal"])
    assert doc["id"] == p["proposal"] and doc["head"] == "r000" and Timeline.from_dict(doc["timeline"]).clips[1].source_out == pytest.approx(
        165 / 25)
    assert sorted(x.relative_to(ws).as_posix() for x in ws.rglob("*") if x.is_file()) == files


def test_render_quality_is_checked_before_anything(ws):
    with pytest.raises(S.SessionError, match="quality must be one of proxy, full"):
        V.render(ws, quality="draft")
    assert renders(ws) == []


def test_exact_window_frames_at_29_97():
    """The window is whole frames at the exact rate: pre/post rounded to frames, the first clip never starting inside a transition."""
    from tests.edit.util import R2997, ctx_for, cut
    ctx = ctx_for(R2997)
    tl = cut(ctx, [("c001", 0, 10, 60), ("c002", 1, 120, 170), ("c003", 3, 320, 380, "b2", ("dissolve", 8)), ("c004", 5, 520, 570)])
    exact = V._exact(tl, ctx.rate)
    w, info = V.review_window_frames(exact, 1, ctx.rate, 2.0, 2.0)
    assert info["cut_frame"] == 92 and info["record_frames"] == [92 - 60, 92 + 8 + 60] and info["cut_offset_frames"] == 60
    assert (w.first, w.last) == (0, 3) and info["clips"] == ["c001", "c002", "c003", "c004"]
    assert w.offset == pytest.approx(32 / float(Fraction(30000, 1001)) - 0.0005)
    assert w.duration == pytest.approx(round(128 / float(Fraction(30000, 1001)) - 0.0005, 4))
    w0, info0 = V.review_window_frames(exact, 0, ctx.rate, 2.0, 0.5)
    assert w0.offset == 0.0 and info0["record_frames"] == [0, 50 + 15]
    w2, info2 = V.review_window_frames(exact, 2, ctx.rate, 0.1, 0.1)  # 3 frames either side of the cut c003|c004 at 152
    assert info2["record_frames"] == [149, 155] and (w2.first, w2.last) == (2, 3)
    w3, info3 = V.review_window_frames(exact, 2, ctx.rate, 2.0, 0.1)
    assert info3["record_frames"] == [92, 155] and (w3.first, w3.last) == (1, 3)  # 92 starts c003's dissolve (92..100): from c002



@pytest.mark.parametrize("rate", [Fraction(25), Fraction(30000, 1001), Fraction(24000, 1001), Fraction(60000, 1001)])
def test_window_trims_select_exactly_the_windows_frames(rate):
    """``render_window`` trims the rendered sub-cut with 4-decimal seconds (``trim=start=%.4f:duration=%.4f``).  The window's offset and
    length are whole frames placed ``TRIM_EPSILON_SECONDS`` early, so those trims keep exactly frames [start, end) at any rate: a frame is
    kept when its time is at or after the start, and less than the duration after the first kept frame (ffmpeg's trim)."""
    from matchcut.pipeline.render import TRIM_EPSILON_SECONDS as eps
    for start in list(range(0, 400)) + list(range(100000, 100400)):
        offset = 0.0 if start == 0 else float(Fraction(start) / rate) - eps
        trim = Fraction(f"{offset:.4f}")
        first = next(k for k in range(max(0, start - 2), start + 3) if Fraction(k) / rate >= trim)
        assert first == start
        for n in (1, 59, 120, 121):
            d = Fraction(f"{float(Fraction(n) / rate) - eps:.4f}")
            kept = [k for k in range(first, first + n + 3) if Fraction(k - first) / rate < d]
            assert len(kept) == n, (start, n)


# ----------------------------------------------------------------------------------------------- review fixes: render names, .tmp sweep
@pytest.fixture
def fake_renders(monkeypatch):
    """Renders that only write a small file: the names are what is under test."""
    import matchcut.review.render as RR

    def cut(tl, media, cfg, out, **kw):
        Path(out).write_bytes(b"x" * 10)
        return out

    def window(tl, w, media, cfg, out, label="", bounds=None):
        Path(out).write_bytes(b"x" * 10)
    monkeypatch.setattr(V.pipeline_render, "render", cut)
    monkeypatch.setattr(RR, "render_window", window)


def names(w: Path, rev=None):
    return tuple(Path(V.render(w, rev, junction=j)["path"]).name if j else Path(V.render(w, rev)["path"]).name for j in (None, 2, 4))


def test_a_window_is_named_by_what_it_renders(ws, fake_renders):
    base = names(ws)
    p = S.propose(ws, [{"op": "trim", "clip": "c005", "edge": "out", "frames": -5}])  # far from window 2, inside window 4
    S.apply(ws, p["proposal"])
    now = names(ws)
    assert now[0] != base[0] and now[1] == base[1] and now[2] != base[2]
    S.undo(ws)
    # the same frames under new clip ids (c004 removed, e001 inserted with its range): every name stays
    p = S.propose(ws, [{"op": "remove", "clip": "c004"}, {"op": "insert", "shot": 5, "in_frame": 520, "out_frame": 570, "after": "c003"}])
    S.apply(ws, p["proposal"])
    assert [c.id for c in S.load(ws).timeline().clips] == ["c001", "c002", "c003", "e001", "c005"]
    assert names(ws) == base


def test_a_window_name_follows_the_musics_position(make_ws, fake_renders, tmp_path):
    w = make_ws()
    music = tmp_path / "music.wav"
    music.write_bytes(b"RIFF")
    cfg = PipelineConfig.load(w / "config.json")
    cfg.music.path = str(music)
    cfg.save(w / "config.json")
    import matchcut.review.render as RR
    S.start(w)
    old = RR._audio_duration
    RR._audio_duration = lambda path: 60.0
    try:
        base = names(w)
        p = S.propose(w, [{"op": "trim", "clip": "c001", "edge": "in", "frames": 5}])  # every later window starts 5 frames earlier in the music
        S.apply(w, p["proposal"])
        now = names(w)
        assert now[2] != base[2]
        S.undo(w)
        p = S.propose(w, [{"op": "trim", "clip": "c005", "edge": "out", "frames": -5}])  # window 2 ends nowhere near: same music position
        S.apply(w, p["proposal"])
        assert names(w)[1] == base[1]
    finally:
        RR._audio_duration = old


def test_a_source_replaced_with_the_same_size_and_mtime_renames_the_renders(make_ws, fake_renders, tmp_path):
    import shutil
    src = tmp_path / "copy.mp4"
    w0 = make_ws()
    shutil.copy(PipelineConfig.load(w0 / "config.json").source, src)
    from tests.edit.conftest import write_workspace
    w = write_workspace(tmp_path / "w2", src, Fraction(25))
    S.start(w)
    base = names(w)
    st = os.stat(src)
    data = bytearray(src.read_bytes())
    data[len(data) // 2] ^= 0xFF
    src.write_bytes(bytes(data))
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(src).st_size == st.st_size and os.stat(src).st_mtime_ns == st.st_mtime_ns
    assert all(a != b for a, b in zip(names(w), base))


def test_stale_temporary_files_are_swept(ws, fake_renders):
    tmp = ws / "edits" / "renders" / ".tmp"
    tmp.mkdir(parents=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    gone, old, live, fresh_gone = tmp / f"{dead.pid}-abc.mp4", tmp / "tmpk3j.full.mp4", tmp / f"{os.getpid()}-def.mp4", tmp / f"{dead.pid}-new.mp4"
    for f in (gone, old, live, fresh_gone):
        f.write_bytes(b"partial")
    an_hour_ago = time.time() - 3700
    os.utime(old, (an_hour_ago, an_hour_ago))
    os.utime(gone, (time.time() - 120, time.time() - 120))
    V.render(ws)
    assert sorted(p.name for p in tmp.iterdir()) == sorted([live.name, fresh_gone.name])  # a dead process's file written just now waits


def test_render_version_is_bumped_past_the_frames_fix():
    assert V.RENDER_VERSION >= 2  # frame-*.jpg files grabbed without the source audio (wrong on MPEG-TS) are never reused


LOCKER = """
import fcntl, os, sys
fds = []
for name in sys.argv[1:]:
    fd = os.open(name, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fds.append(fd)
print("locked", flush=True)
sys.stdin.read()
"""


def test_a_file_another_process_holds_locked_is_never_swept(ws, fake_renders):
    """A writer in another pid namespace looks dead to ``os.kill``: its lock, not its pid, keeps its file.  Old and named for a dead pid,
    a locked file stays; once the lock is gone it is swept."""
    import sys
    tmp = ws / "edits" / "renders" / ".tmp"
    tmp.mkdir(parents=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    old, foreign = tmp / "tmpzz9.full.mp4", tmp / f"{dead.pid}-0123456789ab.mp4"
    for f in (old, foreign):
        f.write_bytes(b"partial")
        os.utime(f, (time.time() - 7200, time.time() - 7200))
    locker = subprocess.Popen([sys.executable, "-c", LOCKER, str(old), str(foreign)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert locker.stdout.readline().strip() == "locked"
        assert V.held(old) and V.held(foreign)
        V.render(ws)
        V._sweep_tmp(tmp, max_age=0.0, quiet=0.0)
        assert old.exists() and foreign.exists()
    finally:
        locker.communicate("")
    assert not V.held(old)
    V._sweep_tmp(tmp)
    assert not old.exists() and not foreign.exists()


def test_a_render_holds_its_temporary_file_locked_while_it_is_written(ws, monkeypatch):
    seen = {}

    def cut(tl, media, cfg, out, **kw):
        Path(out).write_bytes(b"x" * 10)  # (ffmpeg opens the same path: the same inode, so the lock holds)
        os.utime(out, (time.time() - 7200, time.time() - 7200))
        seen["held"] = V.held(Path(out))
        V._sweep_tmp(Path(out).parent, max_age=0.0, quiet=0.0)
        seen["kept"] = Path(out).exists()
        return out
    monkeypatch.setattr(V.pipeline_render, "render", cut)
    V.render(ws)
    assert seen == {"held": True, "kept": True}
    assert list((ws / "edits" / "renders" / ".tmp").iterdir()) == []


def test_a_review_window_temp_file_is_locked_until_discarded(tmp_path):
    import matchcut.review.render as RR
    f = RR._tmp(tmp_path, ".mp4")
    try:
        assert RR.held(f)
    finally:
        RR._discard(f)
    assert not f.exists() and str(f) not in RR._HELD
