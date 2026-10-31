"""Proof: an agent edits a matchcut cut iteratively through the editing API, on deterministic fixtures.

    python prototypes/editing_api/proof.py [--work DIR]

Builds synthetic footage and a click track, runs the pipeline with the mock backend, pins junction 1 the way the TUI does and re-runs the
passes, then drives an edit session: inspect, refusals (locks, shot bounds, pins, transition limits, atomic batches, a moved head),
roll / replace / set_transition / snap_to_beat / insert, undo and redo with a branch, compare, render (frame accuracy checked against the
source), publish as the human stage, the same tools over MCP, a stale session, and a second run that must produce identical revisions.
Writes ``proof.json`` and ``proof.md`` into the work directory.  Changes nothing outside it.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from editapi import EditError, EditSession  # noqa: E402
from matchcut.interchange.common import layout  # noqa: E402
from matchcut.pipeline.editorial import Pin, PinStore, pin_states  # noqa: E402
from matchcut.pipeline.junctions import stale_human  # noqa: E402
from matchcut.pipeline.timeline import Timeline, clip_origin  # noqa: E402

PASSED, FAILED = "Passed", "Failed"
CORE = "Failed: matchcut core defect"  # the check failed because of matchcut itself, not the editing API (see the report)
KNOWN = "Known gap (not a pass)"  # the check shows a documented limitation is still there: counted apart from the passes


class Checks:
    def __init__(self) -> None:
        self.rows: List[Dict[str, Any]] = []
        self.timings: Dict[str, float] = {}

    def check(self, step: str, what: str, ok: bool, detail: Any = "", fail: str = FAILED) -> bool:
        outcome = (KNOWN if fail == KNOWN else PASSED) if ok else (FAILED if fail == KNOWN else fail)
        self.rows.append({"step": step, "check": what, "outcome": outcome, "detail": detail if isinstance(detail, str) else json.dumps(detail)})
        print(f"  [{'ok' if ok else 'FAIL'}] {step}: {what}" + (f" -- {self.rows[-1]['detail'][:160]}" if not ok else ""))
        return ok

    def timed(self, key: str, fn, *a, **k):
        t = time.perf_counter()
        out = fn(*a, **k)
        self.timings[key] = round(time.perf_counter() - t, 4)
        return out


def mc(*args: Any) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "matchcut.pipeline", *map(str, args)], cwd=ROOT, capture_output=True, text=True, check=True)


def build_workspace(work: Path) -> Dict[str, Path]:
    from matchcut.pipeline.demo import make_click_track, make_demo_footage
    fx = work / "fixtures"
    demo, click, ws = fx / "demo.mp4", fx / "click.wav", work / "ws"
    if not demo.exists():
        make_demo_footage(demo)
        make_click_track(click)
    if ws.exists():
        shutil.rmtree(ws)
    common = [demo, "-w", ws, "--backend", "mock", "-t", "8", "--music", click]
    mc("run", *common, "--no-render")
    nopin = work / "ws-nopin"  # the same cut before any pin, for the match-cut and first-clip checks
    if nopin.exists():
        shutil.rmtree(nopin)
    shutil.copytree(ws, nopin)
    # pin junction 1 at its current points, as the Edit view's "l" does
    final = Timeline.load(ws / "final.json")
    a, b = final.clips[0], final.clips[1]
    PinStore(ws).put(Pin(a.shot_index, b.shot_index, round(a.source_out, 3), round(b.source_in, 3), "locked", "", clip_origin(a), clip_origin(b)))
    mc("refine", *common, "--no-render")
    return {"demo": demo, "click": click, "ws": ws, "common": common, "nopin": nopin}


def frames_gray(path: Path, indexes: List[int], size=(64, 36)) -> Dict[int, np.ndarray]:
    """Decoded frames (by index) of a video, small and grey, for comparison."""
    sel = "+".join(f"eq(n\\,{i})" for i in indexes)
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select='{sel}',scale={size[0]}:{size[1]},format=gray",
                          "-vsync", "0", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    n = size[0] * size[1]
    got = [np.frombuffer(out[k * n:(k + 1) * n], dtype=np.uint8).astype(np.float32) for k in range(len(out) // n)]
    return dict(zip(sorted(indexes), got))


def probe_frames(path: Path) -> Dict[str, float]:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=codec_type,nb_read_frames,duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    streams = json.loads(out)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    return {"video_frames": int(v["nb_read_frames"]), "video_s": float(v["duration"]), "audio_s": float(a["duration"]) if a else -1.0}


def frame_accuracy(source: Path, render: Path, tl: Timeline, rate) -> List[Dict[str, Any]]:
    """For three frames in the middle of each clip, the render's frame against the source frames at offsets -2..+2, at full size: the best
    match must be offset 0.  Only footage whose neighbouring frames differ can show it; a clip of static frames is reported as not decisive."""
    rows = layout(tl, rate)
    picks = []  # (clip index, source frame, record frame)
    for n, r in enumerate(rows):
        mid = (r.vis_in + r.vis_out) // 2
        picks += [(n, k, r.rec_in + (k - r.vis_in)) for k in (mid - 1, mid, mid + 1)]
    size = (320, 180)
    src = frames_gray(source, sorted({k + d for _, k, _ in picks for d in range(-2, 3)}), size)
    out = frames_gray(render, sorted({ko for _, _, ko in picks}), size)
    result = []
    for n, (c, r) in enumerate(zip(tl.clips, rows)):
        mine = [(k, ko) for m, k, ko in picks if m == n]
        best, decisive, margin = [], True, []
        for k, ko in mine:
            errs = {d: float(np.mean(np.abs(src[k + d] - out[ko]))) for d in range(-2, 3)}
            best.append(min(errs, key=errs.get))
            decisive &= min(float(np.mean(np.abs(src[k] - src[k + d]))) for d in (-1, 1)) > 0.1
            margin.append(round(sorted(errs.values())[1] - errs[0], 2))
        result.append({"clip": c.id, "shot": c.shot_index, "record_frames": [ko for _, ko in mine], "source_frames": [k for k, _ in mine],
                       "best_offsets": best, "decisive": decisive, "margin": margin})
    return result


def session_script(S: EditSession, C: Checks, tag: str = "") -> List[str]:
    """The edits, in order; returns the revision ids made.  ``tag`` prefixes step names (used by the determinism run)."""
    made: List[str] = []

    def step(name: str) -> str:
        return f"{tag}{name}"

    def apply(p: Dict[str, Any], what: str) -> Optional[str]:
        if not C.check(step(what), "proposal accepted", p["ok"], p):
            return None
        r = S.apply(p["proposal"])
        made.append(r["head"])
        return r["head"]

    o3 = S.inspect()["junctions"][2]["beat_offset_frames"]
    p = S.propose([{"op": "roll", "junction": 3, "frames": 3}])
    C.check(step("3 roll"), "a 3-frame roll is refused: c004 would be too short for its 19-frame dissolve", not p["ok"] and any("longer than 40%" in x for x in p["problems"]), p)
    p = C.timed("propose roll", S.propose, [{"op": "roll", "junction": 3, "frames": 1}])
    C.check(step("3 roll"), "a 1-frame roll moves exactly two edges", p["ok"] and sorted(p["changes"]["edits"]) == ["c003: out +1f", "c004: in +1f"], p)
    C.check(step("3 roll"), "a roll keeps the running time", p["ok"] and len(set(p["changes"]["duration_frames"])) == 1, p.get("changes"))
    C.check(step("3 roll"), "clips not named are unchanged", p["untouched_identical"], p)
    t = time.perf_counter()
    apply(p, "3 roll")
    C.timings["apply"] = round(time.perf_counter() - t, 4)

    p = S.propose([{"op": "replace", "clip": "c004", "shot": 4}])
    C.check(step("4 replace"), "c004 is replaced by a new clip of shot 4, same length", p["ok"] and p["changes"]["duration_frames"][0] == p["changes"]["duration_frames"][1]
            and sorted(e.split(" (")[0] for e in p["changes"]["edits"]) == ["added e001", "removed c004"]
            and any("added e001 (shot 4" in e for e in p["changes"]["edits"]), p)
    prev = S.render(None, None, True, p["proposal"])
    C.check(step("4 replace"), "the proposal renders before it is applied (a preview), and the head stays", Path(prev["path"]).exists()
            and prev["frames"] == p["changes"]["duration_frames"][1] and S.inspect()["head"] == made[-1], prev)
    head = apply(p, "4 replace")
    tl = S.timeline(head) if head else None
    C.check(step("4 replace"), "the replacement keeps c004's incoming dissolve", tl is not None and tl.clips[3].id == "e001" and tl.clips[3].transition_in.kind == "dissolve", tl and tl.clips[3].to_dict())

    p = S.propose([{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 40}])
    C.check(step("5 transition"), "a dissolve longer than 40% of the shorter clip is refused", not p["ok"] and "longer than" in p["results"][0].get("reason", ""), p)
    p = S.propose([{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 6}])
    C.check(step("5 transition"), "junction 4 becomes a 6-frame dissolve", p["ok"] and p["changes"]["edits"] == ["e001|c005: cut -> dissolve 6f"], p)
    j4_before = S.inspect()["junctions"][3]["record_frame"]
    apply(p, "5 transition")
    j4_after = S.inspect()["junctions"][3]["record_frame"]
    C.check(step("5 transition"), "the dissolve starts c005 6 frames earlier on the record (a dissolve overlaps its clips): later cuts ripple",
            j4_after == j4_before - 6 and p["changes"]["duration_frames"][1] == p["changes"]["duration_frames"][0] - 6,
            {"record_frame": [j4_before, j4_after], "duration_frames": p["changes"]["duration_frames"]})

    offsets = [j["beat_offset_frames"] for j in S.inspect()["junctions"]]
    C.check(step("6 beat"), "so junction 4, on the beat before, is now off it", offsets[3] != 0, offsets)
    p = S.propose([{"op": "snap_to_beat", "junction": 3}])
    C.check(step("6 beat"), "junction 3 (made a frame later by the roll) is rolled onto its beat", offsets[2] == o3 + 1 and offsets[2] != 0 and p["ok"]
            and f"{-offsets[2]:+d}f onto the beat" in p["results"][0].get("detail", ""), {"offset_before_roll": o3, "offsets_before": offsets, "proposal": p})
    apply(p, "6 beat")
    after = [j["beat_offset_frames"] for j in S.inspect()["junctions"]]
    C.check(step("6 beat"), "only junction 3 moved (a roll ripples nothing)", after == offsets[:2] + [0] + offsets[3:], {"before": offsets, "after": after})
    p = S.propose([{"op": "snap_to_beat", "junction": 2}])
    C.check(step("6 beat"), "snapping a junction already on its beat changes nothing", after[1] == 0 and p["ok"] and p["changes"]["edits"] == []
            and "already on the beat" in p["results"][0]["detail"], p)

    p = S.propose([{"op": "insert", "shot": 5, "in_frame": 325, "out_frame": 350, "after": "c005"},
                   {"op": "set_transition", "junction": 5, "kind": "dip_to_black", "frames": 6}])
    C.check(step("7 batch"), "an insert and a dip to black in one proposal", p["ok"] and p["changes"]["junctions_added"] == 1, p)
    apply(p, "7 batch")
    return made


def run(work: Path) -> Dict[str, Any]:
    C = Checks()
    work.mkdir(parents=True, exist_ok=True)
    print("fixtures and workspace")
    t = time.perf_counter()
    env = build_workspace(work)
    C.timings["workspace (mock pipeline, twice)"] = round(time.perf_counter() - t, 2)
    ws = env["ws"]
    fresh = work / "ws-fresh"  # a copy before any session, for the determinism run
    if fresh.exists():
        shutil.rmtree(fresh)
    shutil.copytree(ws, fresh)
    final = Timeline.load(ws / "final.json")
    C.check("0 fixture", "the pin is applied: c001 out and c002 in are locked", final.clips[0].locked_out and final.clips[1].locked_in,
            [c.to_dict() for c in final.clips[:2]])

    S = EditSession(ws)
    print("session")
    r = C.timed("start", S.start)
    C.check("1 start", "a session starts at r000 from final", r["head"] == "r000" and not r["resumed"], r)
    C.check("1 start", "starting again resumes it", S.start()["resumed"])
    i = C.timed("inspect", S.inspect)
    C.check("2 inspect", "5 clips, 4 junctions, junction 1 shown pinned", len(i["clips"]) == 5 and len(i["junctions"]) == 4 and i["junctions"][0]["pinned"]
            and not any(j["pinned"] for j in i["junctions"][1:]), i["junctions"])
    unused = [s["shot"] for s in i["shots"] if not s["used_by"]]
    C.check("2 inspect", "unused shots are listed with their descriptions", unused == [2, 4, 5] and all("subject" in s for s in i["shots"]), unused)
    C.check("2 inspect", "music beat offsets per junction", all("beat_offset_frames" in j for j in i["junctions"]), [j["beat_offset_frames"] for j in i["junctions"]])
    C.check("2 inspect", "story beats with target and actual length", len(i["story_beats"]) == 3, i["story_beats"])

    # refusals: nothing is written but the proposal record
    revs = lambda: sorted(p.name for p in (ws / "edits" / "revisions").glob("*.json"))  # noqa: E731
    n0 = revs()
    for ops, why, what in [
        ([{"op": "trim", "clip": "c001", "edge": "out", "frames": -2}], "held by an editor pin", "a pinned edge is not trimmed"),
        ([{"op": "roll", "junction": 1, "frames": 2}], "held by an editor pin", "a pinned junction is not rolled"),
        ([{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}], "held by an editor pin", "a pinned junction keeps its transition"),
        ([{"op": "snap_to_beat", "junction": 1}], "held by an editor pin", "a pinned junction is not moved onto a beat"),
        ([{"op": "roll", "junction": 2, "frames": 3}], "would leave shot 1", "a roll past the end of the shot is refused"),
        ([{"op": "trim", "clip": "c003", "edge": "out", "frames": -40}], "below the minimum clip length", "a trim below the minimum length is refused"),
        ([{"op": "remove", "clip": "c002"}], "breaks the editor pin", "removing a pinned clip is refused"),
        ([{"op": "move", "clip": "c002", "after": "c004"}], "breaks the editor pin", "moving a pinned clip away is refused"),
        ([{"op": "roll", "junction": 3, "frames": 2}, {"op": "trim", "clip": "c001", "edge": "out", "frames": -1}], "held by an editor pin",
         "a batch with one refused op is refused whole"),
        ([{"op": "trim", "clip": "c999", "edge": "in", "frames": 1}], "no clip c999", "an unknown clip is named"),
        ([{"op": "explode"}], "unknown op", "an unknown op is named"),
    ]:
        p = S.propose(ops)
        reasons = " ".join([x.get("reason", "") for x in p["results"]] + p["problems"])
        C.check("2b refuse", what, not p["ok"] and why in reasons, reasons)
    C.check("2b refuse", "no revision was made by any refusal", revs() == n0 and S.inspect()["head"] == "r000", revs())
    try:
        S.apply(S.propose([{"op": "remove", "clip": "c002"}])["proposal"])
        C.check("2b refuse", "a refused proposal cannot be applied", False)
    except EditError as exc:
        C.check("2b refuse", "a refused proposal cannot be applied", "was refused" in str(exc), str(exc))

    r0 = S.timeline("r000")
    C.check("1 start", "r000 is on whole frames: every source point and every transition length", all(
        abs(x * 25 - round(x * 25)) < 1e-6 for c in r0.clips for x in (c.source_in, c.source_out, c.transition_in.duration)),
        [(c.id, c.transition_in.duration) for c in r0.clips])
    p = S.propose([{"op": "trim", "clip": "c004", "edge": "in", "frames": 25}])
    C.check("2b refuse", "a trim that leaves a transition longer than 40% of its clip is refused", not p["ok"] and any("longer than 40%" in x for x in p["problems"]), p)
    p = S.propose([{"op": "trim", "clip": "c003", "edge": "out", "frames": 1.9}])
    C.check("2b refuse", "a fractional frame count is refused, not truncated", not p["ok"] and "whole number" in p["results"][0]["reason"], p)
    p = S.propose([{"op": "set_transition", "junction": 2, "kind": "cut"}])
    C.check("2b refuse", "changing one transition moves no other junction (transitions are on whole frames)", p["ok"] and p["changes"]["edits"] == ["c002|c003: dissolve 8f -> cut"]
            and p["changes"]["junctions_changed"] == 1, p)
    for bad, what in ((lambda: S.propose("trim"), "ops that are not a list"), (lambda: S.checkout("../../final"), "a revision id that is a path"),
                      (lambda: S.apply("../session"), "a proposal id that is a path")):
        try:
            bad()
            C.check("2c validate", f"{what} is refused", False)
        except EditError as exc:
            C.check("2c validate", f"{what} is refused", True, str(exc))
    good = S.propose([{"op": "roll", "junction": 3, "frames": 1}])
    path = ws / "edits" / "proposals" / f"{good['proposal']}.json"
    doc = json.loads(path.read_text())
    doc["timeline"]["clips"][0]["source_out"] = 1.0  # a hand-edited proposal file that would cut the pinned clip
    path.write_text(json.dumps(doc))
    try:
        S.apply(good["proposal"])
        C.check("2c validate", "apply re-runs the operations: a hand-edited proposal file is refused", False)
    except EditError as exc:
        C.check("2c validate", "apply re-runs the operations: a hand-edited proposal file is refused", "does not match its operations" in str(exc), str(exc))
    C.check("2c validate", "and the head did not move", S.inspect()["head"] == "r000")

    print("match cuts and the first clip (a copy without the pin)")
    mcws = env["nopin"]
    doc = json.loads((mcws / "final.json").read_text())
    a, b = doc["clips"][3], doc["clips"][4]  # make c004|c005 a match cut, as the match-cut pass marks one
    a["locked_out"], b["locked_in"] = True, True
    a["tags"] = a["tags"] + ["match_out:7"]
    b["tags"] = b["tags"] + ["match_in:6"]
    (mcws / "final.json").write_text(json.dumps(doc, indent=2))
    M = EditSession(mcws)
    M.start()
    for ops, why, what in [
        ([{"op": "insert", "shot": 5, "in_frame": 300, "out_frame": 325, "after": "c004"}], "breaks the match cut c004|c005", "an insert inside a match cut is refused"),
        ([{"op": "remove", "clip": "c005"}], "breaks the match cut c004|c005", "removing half of a match cut is refused"),
        ([{"op": "move", "clip": "c005", "before": "c003"}], "breaks the match cut c004|c005", "moving half of a match cut away is refused"),
        ([{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 4}], "match cuts stay hard cuts", "a match cut stays a hard cut"),
        ([{"op": "trim", "clip": "c004", "edge": "out", "frames": -2}], "held by a match cut", "a match cut's edge is not trimmed"),
    ]:
        p = M.propose(ops)
        reasons = " ".join([x.get("reason", "") for x in p["results"]] + p["problems"])
        C.check("2d match cut", what, not p["ok"] and why in reasons, reasons)
    p = M.propose([{"op": "move", "clip": "c003", "before": "c001"}])
    C.check("2d first clip", "a move that leaves a dissolve next to a clip too short for it is refused (c004's, now after c002)",
            not p["ok"] and any("c002|c004 is longer than 40%" in x for x in p["problems"]), p)
    p = M.propose([{"op": "remove", "clip": "c001"}, {"op": "remove", "clip": "c002"}])
    C.check("2d first clip", "removing the first clips is accepted (the new first clip loses its dissolve)", p["ok"]
            and "c003: incoming dissolve 8f dropped (it is the first clip now)" in p["changes"]["edits"], p)

    print("edits")
    made = session_script(S, C)
    C.check("7 batch", "five revisions r001..r005", made == ["r001", "r002", "r003", "r004", "r005"], made)

    # undo, redo, a branch, a proposal against a moved head
    log0 = S.log()
    C.check("8 undo", "undo moves the head to r004", S.undo()["head"] == "r004")
    C.check("8 undo", "redo comes back to r005", S.redo()["head"] == "r005")
    S.undo()
    p_old = S.propose([{"op": "slip", "clip": "c003", "frames": 1}])
    p_new = S.propose([{"op": "set_transition", "junction": 3, "kind": "cut"}, {"op": "move", "clip": "e001", "before": "c003"},
                       {"op": "remove", "clip": "c005"}, {"op": "slip", "clip": "c003", "frames": 2}])
    C.check("8 branch", "a cut, a move, a remove and a slip in one proposal, each reported", p_new["ok"] and sorted(e.split(" (")[0] for e in p_new["changes"]["edits"])
            == ["c002|e001: dissolve 19f -> cut", "c003: slipped +2f", "order: c001 c002 e001 c003", "removed c005"] and p_new["untouched_identical"], p_new)
    r = S.apply(p_new["proposal"])
    C.check("8 branch", "an edit after an undo makes a branch (r006 from r004); r005 is kept", r["head"] == "r006" and r["parent"] == "r004"
            and (ws / "edits" / "revisions" / "r005.json").exists(), r)
    try:
        S.apply(p_old["proposal"])
        C.check("8 branch", "a proposal made before the head moved is refused", False)
    except EditError as exc:
        C.check("8 branch", "a proposal made before the head moved is refused", "propose again" in str(exc), str(exc))
    S.undo()
    C.check("8 branch", "redo from r004 takes the latest branch", S.redo() == {"head": "r006", "branches": ["r005", "r006"]})
    C.check("8 branch", "checkout moves the head back to the r005 branch", S.checkout("r005") == {"head": "r005"})
    C.check("8 undo", "the log lists every revision with its parent", [x["parent"] for x in S.log()["revisions"]] == [None, "r000", "r001", "r002", "r003", "r004", "r004"],
            S.log()["revisions"])

    cmp = S.compare("r000", "r004")
    C.check("9 compare", "r000 -> r004 shows the net effect: the replace, the new dissolve, and the roll and beat snap combined into one edge move",
            sorted(e.split(" (")[0] for e in cmp["edits"]) == ["added e001", "c003: out -1f", "e001|c005: cut -> dissolve 6f", "removed c004"], cmp)

    print("render")
    head = S.timeline("r005")
    rr = C.timed("render full (r005, source size)", S.render, "r005", None, False)
    meta = probe_frames(Path(rr["path"]))
    C.check("10 render", "the render has exactly the cut's frames", meta["video_frames"] == rr["frames"], {**meta, "expected": rr["frames"]})
    C.check("10 render", "audio and video lengths agree within 2 frames", abs(meta["audio_s"] - meta["video_s"]) <= 2 / 25 + 1e-6, meta, fail=CORE)
    early = S.render("r004", None, False)
    m4 = probe_frames(Path(early["path"]))
    C.check("10 render", "r004 (every clip in source order) has audio as long as its video", abs(m4["audio_s"] - m4["video_s"]) <= 2 / 25 + 1e-6, m4)
    acc = frame_accuracy(env["demo"], Path(rr["path"]), head, S.w.rate)
    decisive = [a for a in acc if a["decisive"]]
    C.check("10 render", "every decisive clip shows its exact source frames (offset 0, three frames each)",
            len(decisive) >= 3 and all(set(a["best_offsets"]) == {0} for a in decisive), acc)
    wj = C.timed("render junction window (r005, j3)", S.render, "r005", 3)
    C.check("10 render", "a junction's review window renders", Path(wj["path"]).exists(), wj)
    w5 = S.render("r005", 5)  # c005 (shot 7) -> e002 (shot 5): out of source order, with music
    m5 = probe_frames(Path(w5["path"]))
    C.check("10 render", "the review window of an out-of-order junction has audio as long as its video", abs(m5["audio_s"] - m5["video_s"]) <= 2 / 25 + 1e-6, m5, fail=CORE)
    t = time.perf_counter()
    S.render("r005", None, False)
    C.timings["render again (cached)"] = round(time.perf_counter() - t, 4)

    print("publish")
    mc("export", ws, "--stage", "final", "--format", "otio", "--out", work / "nle")
    mc("import", ws, work / "nle" / "final.otio")  # an editor's round trip: the human stage now holds an NLE import
    try:
        S.publish()
        C.check("11 publish", "publishing over an editor's import is refused", False)
    except EditError as exc:
        C.check("11 publish", "publishing over an editor's import is refused", "editor's import" in str(exc), str(exc))
    pub = S.publish(replace_import=True)
    kept = list((ws / "edits" / "replaced-imports").glob("*/human_import.json"))
    C.check("11 publish", "with replace_import the editor's import is moved aside, not lost", len(kept) == 1 and json.loads(kept[0].read_text())["format"] == "otio",
            [str(k) for k in kept])
    C.check("11 publish", "the head becomes the human stage, not stale", (ws / "human.json").exists() and stale_human(ws) is None, pub)
    hum = Timeline.load(ws / "human.json")
    states = pin_states(hum, PinStore(ws).load(), "human")
    C.check("11 publish", "the editor pin is still applied in the human stage", [s.state for s in states] == ["applied"], [(s.label, s.state, s.reason) for s in states])
    jl = mc("junctions", ws).stdout
    C.check("11 publish", "matchcut junctions lists the human stage (5 junctions; the new clips renamed h001, h002 by M2a's builder)",
            "5 junctions in human" in jl and "h001" in jl and "h002" in jl, jl)
    C.check("11 publish", "M2a's diff of the published stage finds the clips but reads the two new transitions on c005 as trims (its neighbours changed)",
            sorted(d.split(" (")[0] for d in pub["decisions"]) ==
            sorted(["human removed c004", "human added h001", "human added h002", "human trimmed c003: out -1f", "human trimmed c005: in +3f, out -3f"]), pub["decisions"])
    ex = subprocess.run([sys.executable, "-m", "matchcut.pipeline", "export", ws, "--stage", "human", "--format", "otio", "--out", work / "exports"],
                        cwd=ROOT, capture_output=True, text=True)
    C.check("11 publish", "matchcut export refuses the human stage (M2a's rule: only canonical stages go out), so an edited cut cannot yet go to an NLE",
            ex.returncode != 0 and "human stage is an import result" in (ex.stdout + ex.stderr), (ex.stdout + ex.stderr)[-300:], fail=KNOWN)

    print("mcp")
    proc = subprocess.Popen([sys.executable, str(HERE / "mcp_server.py"), str(ws)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    def call(mid: int, method: str, params: Optional[dict] = None) -> dict:
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())
    t = time.perf_counter()
    init = call(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "proof", "version": "0"}})
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
    listed = call(2, "tools/list")
    C.check("12 mcp", "initialize and tools/list (11 tools)", init["result"]["serverInfo"]["name"] == "matchcut-edit" and len(listed["result"]["tools"]) == 11,
            [x["name"] for x in listed["result"]["tools"]])
    via = json.loads(call(3, "tools/call", {"name": "edit_inspect", "arguments": {}})["result"]["content"][0]["text"])
    C.check("12 mcp", "edit_inspect over MCP equals the Python call", via == S.inspect())
    pr = json.loads(call(4, "tools/call", {"name": "edit_propose", "arguments": {"ops": [{"op": "roll", "junction": 1, "frames": 1}]}})["result"]["content"][0]["text"])
    C.check("12 mcp", "a refusal over MCP carries its reason", not pr["ok"] and "held by an editor pin" in pr["results"][0]["reason"], pr)
    bad = call(5, "tools/call", {"name": "edit_apply", "arguments": {"proposal": "nope"}})
    C.check("12 mcp", "an error is a tool result with isError", bad["result"]["isError"] and "proposal" in bad["result"]["content"][0]["text"], bad)
    junk = call(6, "tools/call", {"name": "edit_propose", "arguments": {"ops": ["trim"]}})
    after_junk = call(7, "tools/call", {"name": "edit_log", "arguments": {}})
    C.check("12 mcp", "a malformed request is an isError result and the server keeps answering", junk["result"]["isError"] and not after_junk["result"]["isError"],
            [junk, after_junk["result"]["isError"]])
    C.timings["mcp: 7 requests incl. start-up"] = round(time.perf_counter() - t, 3)
    proc.stdin.close()
    proc.wait(timeout=10)
    cli = subprocess.run([sys.executable, str(HERE / "cli.py"), str(ws), "edit_log"], capture_output=True, text=True)
    C.check("12 mcp", "the JSON CLI serves the same tools", cli.returncode == 0 and json.loads(cli.stdout)["head"] == "r005", cli.stdout[:200])

    print("stale")
    PinStore(ws).remove(final.clips[0].shot_index, final.clips[1].shot_index)
    mc("refine", *env["common"], "--no-render")  # the pipeline runs again without the pin: final.json changes
    C.check("13 stale", "the session sees that final changed", bool(S.stale()), S.stale())
    try:
        S.propose([{"op": "slip", "clip": "c003", "frames": 1}])
        C.check("13 stale", "a stale session refuses to propose", False)
    except EditError as exc:
        C.check("13 stale", "a stale session refuses to propose", "start a new session" in str(exc), str(exc))
    C.check("13 stale", "the published human stage is stale too (the engine's own rule)", stale_human(ws) is not None, stale_human(ws))
    r = S.start()
    C.check("13 stale", "a new session moves the old one aside (kept) and starts at r000", r["head"] == "r000" and any(p.name.startswith("edits.stale-") for p in ws.iterdir()), r)

    print("determinism")
    S2 = EditSession(fresh)
    S2.start()
    C2 = Checks()
    made2 = session_script(S2, C2, tag="again ")
    C.check("14 determinism", "the same edits on a fresh copy make the same revisions, byte for byte",
            made2 == made and [x["timeline_sha256"] for x in S2.log()["revisions"]] == [x["timeline_sha256"] for x in log0["revisions"]],
            {"first": [x["timeline_sha256"][:12] for x in log0["revisions"]], "again": [x["timeline_sha256"][:12] for x in S2.log()["revisions"]]})
    C.check("14 determinism", "every check of the second run passed", all(x["outcome"] == PASSED for x in C2.rows), [x for x in C2.rows if x["outcome"] != PASSED])

    return {"checks": C.rows, "timings": C.timings, "frame_accuracy": acc,
            "summary": {k: sum(1 for x in C.rows if x["outcome"] == k) for k in (PASSED, KNOWN, CORE, FAILED)}}


def write_md(result: Dict[str, Any], path: Path) -> None:
    lines = ["# Editing API proof: recorded run", "", f"{result['summary']}", "", "| Step | Check | Outcome |", "|---|---|---|"]
    lines += [f"| {r['step']} | {r['check']} | {r['outcome']} |" for r in result["checks"]]
    lines += ["", "## Frame accuracy (r005, rendered at source size)", "", "| Clip | Shot | Record frames | Source frames | Best offsets | Decisive |", "|---|---|---|---|---|---|"]
    lines += [f"| {a['clip']} | {a['shot']} | {a['record_frames']} | {a['source_frames']} | {a['best_offsets']} | {'yes' if a['decisive'] else 'no (static)'} |" for a in result["frame_accuracy"]]
    lines += ["", "## Timings (seconds, this container)", ""] + [f"- {k}: {v}" for k, v in result["timings"].items()]
    path.write_text("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", default=str(Path(tempfile.gettempdir()) / "matchcut-editing-api-proof"))
    args = ap.parse_args()
    work = Path(args.work).resolve()
    result = run(work)
    (work / "proof.json").write_text(json.dumps(result, indent=2) + "\n")
    write_md(result, work / "proof.md")
    print(result["summary"], "->", work / "proof.md")
    return 0 if result["summary"][FAILED] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
