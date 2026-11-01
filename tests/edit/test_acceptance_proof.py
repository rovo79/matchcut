"""M3a automated acceptance: the editing-API prototype's 91-check proof (``prototypes/editing_api/proof.py``; the latest recorded run is
``prototypes/editing_api/recorded-run-renderer-fix/proof.json``: 90 passed, 1 known gap), re-expressed as tests against ``matchcut.edit``.

The prototype is evidence, not code: nothing here imports it.  The fixture is built the way the proof built its own: synthetic demo
footage and a click track, the pipeline run with the mock backend (``python -m matchcut.pipeline run``), junction 1 pinned as the Edit
view's ``l`` does it and the passes re-run (``refine``); a second copy, without the pin, has c004|c005 marked a match cut.  The edit
session is then driven through the real API (``matchcut.edit.session``, ``view``, ``publish``), the JSON CLI (``python -m matchcut.edit``)
and, where the prototype used it, the pipeline CLI (``python -m matchcut.pipeline``: refine, export, import, junctions).

``MAPPING`` (below) lists every prototype check, by id (``P01`` ... ``P91``: its position in the recorded run), with the test(s) that
cover it and a classification:

- ``covered``: the same behaviour is asserted;
- ``superseded``: M3a deliberately changed the behaviour; the note names the decision, and the test asserts the new behaviour;
- ``closed``: the prototype's known gap, which M3a closes; the test asserts it is closed;
- ``known gap``: M3a does not do it; its test is ``xfail(strict=True)`` and never counts as a pass;
- ``out of scope``: not part of M3a (the MCP transport is M3b); its test is skipped with the reason.

``test_every_prototype_check_is_mapped`` reads the recorded run and fails if any of the 91 checks is missing, renamed or reordered, and
``test_the_mapping_names_real_tests_with_marks_that_match_their_class`` checks each mapped test exists and carries the marks its class
demands (no known gap or out-of-scope check can pass silently; no covered one is skipped).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pytest

from matchcut.edit import publish as P
from matchcut.edit import session as S
from matchcut.edit import view as V
from matchcut.edit.model import Refused
from matchcut.interchange.common import export_timeline, layout, source_info
from matchcut.interchange.fidelity import differences
from matchcut.interchange.otio import export_otio
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore, pin_states
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines, stage_timeline_paths, stale_edit, stale_human
from matchcut.pipeline.shots import load_shots
from matchcut.pipeline.timeline import Timeline, clip_origin

REPO = Path(__file__).resolve().parents[2]
PROTOTYPE = REPO / "prototypes" / "editing_api"
RECORDED = PROTOTYPE / "recorded-run-renderer-fix" / "proof.json"
FPS = 25

COVERED, SUPERSEDED, CLOSED, KNOWN_GAP, OUT_OF_SCOPE = "covered", "superseded", "closed", "known gap", "out of scope"
CLASSES = (COVERED, SUPERSEDED, CLOSED, KNOWN_GAP, OUT_OF_SCOPE)

D1 = "decision 1 (published agent edits get their own stage, edit; human is reserved for editor imports)"
D2 = "decision 2 (pinned junctions may change their transition, unless the pin protects it)"
SESSION_MODEL = ("the approved spec's session model (EDIT.md, start: a fresh session is refused unless restart; the session lives in "
                 "WS/edits/ and every command continues it)")
NO_OP = ("M3a's proposal rule (EDIT.md, Proposals: a proposal whose operations change nothing is refused, with the operations' details; "
         "nothing is stored)")
MCP = "the MCP transport is M3b (spec: Out of scope); the same tool table is served by the JSON CLI, asserted under P85"


@dataclass(frozen=True)
class Check:
    id: str
    step: str
    check: str
    classification: str
    tests: Tuple[str, ...]
    note: str = ""


def _c(id_: str, step: str, check: str, classification: str, tests: Sequence[str], note: str = "") -> Check:
    return Check(id_, step, check, classification, tuple(tests), note)


# Every check of the recorded run, in its order; ``tests`` are names in this module (``name[param id]`` for a parametrized case).
MAPPING: List[Check] = [
    _c("P01", "0 fixture", "the pin is applied: c001 out and c002 in are locked", COVERED, ["test_p01_the_pin_is_applied"]),
    _c("P02", "1 start", "a session starts at r000 from final", COVERED, ["test_p02_a_session_starts_at_r000_from_final"]),
    _c("P03", "1 start", "starting again resumes it", SUPERSEDED, ["test_p03_starting_again_is_refused_and_the_session_continues"],
       SESSION_MODEL),
    _c("P04", "2 inspect", "5 clips, 4 junctions, junction 1 shown pinned", COVERED, ["test_p04_inspect_clips_junctions_and_the_pin"]),
    _c("P05", "2 inspect", "unused shots are listed with their descriptions", COVERED, ["test_p05_unused_shots_with_their_descriptions"],
       "M3a's inspect lacked the descriptions; fixed with this test (view.inspect: shots[].description)"),
    _c("P06", "2 inspect", "music beat offsets per junction", COVERED, ["test_p06_music_beat_offsets_per_junction"]),
    _c("P07", "2 inspect", "story beats with target and actual length", COVERED, ["test_p07_story_beats_with_target_and_actual_length"],
       "M3a's inspect lacked the target length; fixed with this test (view.inspect: story_beats[].target_frames)"),
    _c("P08", "2b refuse", "a pinned edge is not trimmed", COVERED, ["test_refusals[P08]"]),
    _c("P09", "2b refuse", "a pinned junction is not rolled", COVERED, ["test_refusals[P09]"]),
    _c("P10", "2b refuse", "a pinned junction keeps its transition", SUPERSEDED,
       ["test_p10_a_pinned_junction_may_change_its_transition_unless_protected"], D2),
    _c("P11", "2b refuse", "a pinned junction is not moved onto a beat", COVERED, ["test_refusals[P11]"]),
    _c("P12", "2b refuse", "a roll past the end of the shot is refused", COVERED, ["test_refusals[P12]"]),
    _c("P13", "2b refuse", "a trim below the minimum length is refused", COVERED, ["test_refusals[P13]"]),
    _c("P14", "2b refuse", "removing a pinned clip is refused", COVERED, ["test_refusals[P14]"]),
    _c("P15", "2b refuse", "moving a pinned clip away is refused", COVERED, ["test_refusals[P15]"]),
    _c("P16", "2b refuse", "a batch with one refused op is refused whole", COVERED, ["test_refusals[P16]"]),
    _c("P17", "2b refuse", "an unknown clip is named", COVERED, ["test_refusals[P17]"]),
    _c("P18", "2b refuse", "an unknown op is named", COVERED, ["test_refusals[P18]"]),
    _c("P19", "2b refuse", "no revision was made by any refusal", COVERED, ["test_p19_no_refusal_writes_a_revision_or_a_proposal"]),
    _c("P20", "2b refuse", "a refused proposal cannot be applied", COVERED, ["test_p20_a_refused_proposal_cannot_be_applied"],
       "M3a never stores a refused proposal: its would-be id is refused by apply"),
    _c("P21", "1 start", "r000 is on whole frames: every source point and every transition length", COVERED,
       ["test_p21_r000_is_on_whole_frames"]),
    _c("P22", "2b refuse", "a trim that leaves a transition longer than 40% of its clip is refused", COVERED,
       ["test_p22_a_trim_that_leaves_a_transition_over_the_limit_is_refused"]),
    _c("P23", "2b refuse", "a fractional frame count is refused, not truncated", COVERED,
       ["test_p23_a_fractional_frame_count_is_refused"]),
    _c("P24", "2b refuse", "changing one transition moves no other junction (transitions are on whole frames)", COVERED,
       ["test_p24_changing_one_transition_moves_no_other_junction"]),
    _c("P25", "2c validate", "ops that are not a list is refused", COVERED, ["test_p25_ops_that_are_not_a_list_are_refused"]),
    _c("P26", "2c validate", "a revision id that is a path is refused", COVERED, ["test_p26_a_revision_id_that_is_a_path_is_refused"]),
    _c("P27", "2c validate", "a proposal id that is a path is refused", COVERED, ["test_p27_a_proposal_id_that_is_a_path_is_refused"]),
    _c("P28", "2c validate", "apply re-runs the operations: a hand-edited proposal file is refused", COVERED,
       ["test_p28_p29_a_hand_edited_proposal_is_refused_and_the_head_stays"]),
    _c("P29", "2c validate", "and the head did not move", COVERED, ["test_p28_p29_a_hand_edited_proposal_is_refused_and_the_head_stays"]),
    _c("P30", "2d match cut", "an insert inside a match cut is refused", COVERED, ["test_match_cut_refusals[P30]"]),
    _c("P31", "2d match cut", "removing half of a match cut is refused", COVERED, ["test_match_cut_refusals[P31]"]),
    _c("P32", "2d match cut", "moving half of a match cut away is refused", COVERED, ["test_match_cut_refusals[P32]"]),
    _c("P33", "2d match cut", "a match cut stays a hard cut", COVERED, ["test_match_cut_refusals[P33]"]),
    _c("P34", "2d match cut", "a match cut's edge is not trimmed", COVERED, ["test_match_cut_refusals[P34]"]),
    _c("P35", "2d first clip", "a move that leaves a dissolve next to a clip too short for it is refused (c004's, now after c002)", COVERED,
       ["test_p35_a_move_leaving_a_dissolve_beside_a_too_short_clip_is_refused"]),
    _c("P36", "2d first clip", "removing the first clips is accepted (the new first clip loses its dissolve)", COVERED,
       ["test_p36_removing_the_first_clips_drops_the_new_first_clips_dissolve"]),
    _c("P37", "3 roll", "a 3-frame roll is refused: c004 would be too short for its 19-frame dissolve", COVERED,
       ["test_p37_a_3_frame_roll_is_refused"]),
    _c("P38", "3 roll", "a 1-frame roll moves exactly two edges", COVERED, ["test_p38_a_1_frame_roll_moves_exactly_two_edges"]),
    _c("P39", "3 roll", "a roll keeps the running time", COVERED, ["test_p39_a_roll_keeps_the_running_time"]),
    _c("P40", "3 roll", "clips not named are unchanged", COVERED, ["test_p40_clips_not_named_are_unchanged"]),
    _c("P41", "3 roll", "proposal accepted", COVERED, ["test_p41_the_roll_is_accepted_and_applied"]),
    _c("P42", "4 replace", "c004 is replaced by a new clip of shot 4, same length", COVERED,
       ["test_p42_c004_is_replaced_by_a_new_clip_of_shot_4"]),
    _c("P43", "4 replace", "the proposal renders before it is applied (a preview), and the head stays", COVERED,
       ["test_p43_the_proposal_previews_before_it_is_applied"]),
    _c("P44", "4 replace", "proposal accepted", COVERED, ["test_p44_the_replace_is_accepted_and_applied"]),
    _c("P45", "4 replace", "the replacement keeps c004's incoming dissolve", COVERED, ["test_p45_the_replacement_keeps_the_dissolve"]),
    _c("P46", "5 transition", "a dissolve longer than 40% of the shorter clip is refused", COVERED,
       ["test_p46_a_dissolve_over_the_limit_is_refused"]),
    _c("P47", "5 transition", "junction 4 becomes a 6-frame dissolve", COVERED, ["test_p47_junction_4_becomes_a_6_frame_dissolve"]),
    _c("P48", "5 transition", "proposal accepted", COVERED, ["test_p48_the_transition_is_accepted_and_applied"]),
    _c("P49", "5 transition", "the dissolve starts c005 6 frames earlier on the record (a dissolve overlaps its clips): later cuts ripple",
       COVERED, ["test_p49_the_dissolve_ripples_later_cuts"], "decision 3 keeps this timing; M3a also reports it (timing.moved)"),
    _c("P50", "6 beat", "so junction 4, on the beat before, is now off it", COVERED, ["test_p50_junction_4_left_its_beat"]),
    _c("P51", "6 beat", "junction 3 (made a frame later by the roll) is rolled onto its beat", COVERED,
       ["test_p51_junction_3_is_rolled_onto_its_beat"]),
    _c("P52", "6 beat", "proposal accepted", COVERED, ["test_p52_the_snap_is_accepted_and_applied"]),
    _c("P53", "6 beat", "only junction 3 moved (a roll ripples nothing)", COVERED, ["test_p53_only_junction_3_moved"]),
    _c("P54", "6 beat", "snapping a junction already on its beat changes nothing", SUPERSEDED,
       ["test_p54_snapping_a_junction_already_on_its_beat_changes_nothing"], NO_OP),
    _c("P55", "7 batch", "an insert and a dip to black in one proposal", COVERED, ["test_p55_an_insert_and_a_dip_to_black_in_one_proposal"]),
    _c("P56", "7 batch", "proposal accepted", COVERED, ["test_p56_the_batch_is_accepted_and_applied"]),
    _c("P57", "7 batch", "five revisions r001..r005", COVERED, ["test_p57_five_revisions"]),
    _c("P58", "8 undo", "undo moves the head to r004", COVERED, ["test_p58_undo_moves_the_head_to_r004"]),
    _c("P59", "8 undo", "redo comes back to r005", COVERED, ["test_p59_redo_comes_back_to_r005"]),
    _c("P60", "8 branch", "a cut, a move, a remove and a slip in one proposal, each reported", COVERED,
       ["test_p60_a_cut_a_move_a_remove_and_a_slip_each_reported"]),
    _c("P61", "8 branch", "an edit after an undo makes a branch (r006 from r004); r005 is kept", COVERED,
       ["test_p61_an_edit_after_an_undo_makes_a_branch"]),
    _c("P62", "8 branch", "a proposal made before the head moved is refused", COVERED,
       ["test_p62_a_proposal_made_before_the_head_moved_is_refused"]),
    _c("P63", "8 branch", "redo from r004 takes the latest branch", COVERED, ["test_p63_redo_takes_the_latest_branch"]),
    _c("P64", "8 branch", "checkout moves the head back to the r005 branch", COVERED, ["test_p64_checkout_moves_the_head_to_r005"]),
    _c("P65", "8 undo", "the log lists every revision with its parent", COVERED, ["test_p65_the_log_lists_every_revision_with_its_parent"]),
    _c("P66", "9 compare", "r000 -> r004 shows the net effect: the replace, the new dissolve, and the roll and beat snap combined into one "
       "edge move", COVERED, ["test_p66_compare_shows_the_net_effect"],
       "on M3a's whole-frame grid junction 3 starts on its beat, so the roll (+1f) and the snap (-1f) combine to no c003 change, and e001's "
       "in-point carries the snap: the net effect is still one combined edge"),
    _c("P67", "10 render", "the render has exactly the cut's frames", COVERED, ["test_p67_the_render_has_exactly_the_cuts_frames"]),
    _c("P68", "10 render", "audio and video lengths agree within 2 frames", COVERED, ["test_p68_audio_and_video_lengths_agree"]),
    _c("P69", "10 render", "r004 (every clip in source order) has audio as long as its video", COVERED,
       ["test_p69_r004_has_audio_as_long_as_its_video"]),
    _c("P70", "10 render", "every decisive clip shows its exact source frames (offset 0, three frames each)", COVERED,
       ["test_p70_every_decisive_clip_shows_its_exact_source_frames"]),
    _c("P71", "10 render", "a junction's review window renders", COVERED, ["test_p71_a_junctions_review_window_renders"]),
    _c("P72", "10 render", "the review window of an out-of-order junction has audio as long as its video", COVERED,
       ["test_p72_an_out_of_order_window_has_audio_as_long_as_its_video"]),
    _c("P73", "11 publish", "publishing over an editor's import is refused", SUPERSEDED,
       ["test_p73_publishing_over_a_fresh_editor_import_is_refused"], D1),
    _c("P74", "11 publish", "with replace_import the editor's import is moved aside, not lost", SUPERSEDED,
       ["test_p74_the_editors_import_is_never_touched"], D1 + ": human.json is never moved or changed; supersede_human leaves it out as "
       "stale"),
    _c("P75", "11 publish", "the head becomes the human stage, not stale", SUPERSEDED, ["test_p75_the_head_becomes_the_edit_stage"], D1),
    _c("P76", "11 publish", "the editor pin is still applied in the human stage", SUPERSEDED,
       ["test_p76_the_editor_pin_is_held_in_the_edit_stage"], D1 + ": asserted on the edit stage"),
    _c("P77", "11 publish", "matchcut junctions lists the human stage (5 junctions; the new clips renamed h001, h002 by M2a's builder)",
       SUPERSEDED, ["test_p77_junctions_lists_the_edit_stage_with_the_sessions_clip_ids"], D1 + ": the edit stage keeps e001, e002"),
    _c("P78", "11 publish", "M2a's diff of the published stage finds the clips but reads the two new transitions on c005 as trims (its "
       "neighbours changed)", SUPERSEDED, ["test_p78_the_edit_stage_history_names_each_change"],
       D1 + ": the history is the canonical history plus one agent decision per change, transitions named as transitions"),
    _c("P79", "11 publish", "matchcut export refuses the human stage (M2a's rule: only canonical stages go out), so an edited cut cannot "
       "yet go to an NLE", CLOSED,
       ["test_p79_the_edit_stage_exports_to_otio_fcpxml_and_edl_and_reimports",
        "test_p79_a_reimport_compares_with_the_exact_snapshot_after_a_later_publish"], D1 + ": the edit stage is exportable through M2a"),
    _c("P80", "12 mcp", "initialize and tools/list (11 tools)", OUT_OF_SCOPE, ["test_p80_mcp_initialize_and_tools_list"], MCP),
    _c("P81", "12 mcp", "edit_inspect over MCP equals the Python call", OUT_OF_SCOPE, ["test_p81_mcp_inspect_equals_the_python_call"], MCP),
    _c("P82", "12 mcp", "a refusal over MCP carries its reason", OUT_OF_SCOPE, ["test_p82_mcp_refusal_carries_its_reason"], MCP),
    _c("P83", "12 mcp", "an error is a tool result with isError", OUT_OF_SCOPE, ["test_p83_mcp_error_is_an_is_error_result"], MCP),
    _c("P84", "12 mcp", "a malformed request is an isError result and the server keeps answering", OUT_OF_SCOPE,
       ["test_p84_mcp_malformed_request"], MCP),
    _c("P85", "12 mcp", "the JSON CLI serves the same tools", COVERED, ["test_p85_the_json_cli_serves_the_same_tools"],
       "also asserts the CLI analogues of P81-P84 (inspect equals the Python call, a refusal's reason, errors, malformed input)"),
    _c("P86", "13 stale", "the session sees that final changed", COVERED, ["test_p86_the_session_sees_that_final_changed"]),
    _c("P87", "13 stale", "a stale session refuses to propose", COVERED, ["test_p87_a_stale_session_refuses_to_propose"]),
    _c("P88", "13 stale", "the published human stage is stale too (the engine's own rule)", SUPERSEDED,
       ["test_p88_the_published_edit_stage_is_stale_too"], D1 + ": the published stage is edit (junctions.stale_edit)"),
    _c("P89", "13 stale", "a new session moves the old one aside (kept) and starts at r000", COVERED,
       ["test_p89_a_new_session_moves_the_old_one_aside"]),
    _c("P90", "14 determinism", "the same edits on a fresh copy make the same revisions, byte for byte", COVERED,
       ["test_p90_the_same_edits_make_the_same_revisions"]),
    _c("P91", "14 determinism", "every check of the second run passed", COVERED, ["test_p91_every_check_of_the_second_run_passes"]),
]


# ----------------------------------------------------------------------------------------------- helpers
def mc(*args: Any, check: bool = True) -> subprocess.CompletedProcess:
    """The pipeline CLI, as the prototype ran it."""
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.run([sys.executable, "-m", "matchcut.pipeline", *map(str, args)], cwd=REPO, capture_output=True, text=True,
                          check=check, env=env, timeout=600)


def edit_cli(ws: Path, command: str, args: Optional[Any] = None) -> Tuple[int, Dict[str, Any]]:
    """The edit JSON CLI: (exit code, the one JSON object it printed)."""
    env = dict(os.environ, PYTHONPATH=str(REPO))
    env.pop("MATCHCUT_DEBUG", None)
    argv = [sys.executable, "-m", "matchcut.edit", str(ws), command] + ([] if args is None else [args if isinstance(args, str) else
                                                                                                  json.dumps(args)])
    proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, env=env, timeout=600)
    lines = proc.stdout.splitlines()
    assert len(lines) == 1 and "Traceback" not in proc.stderr, (proc.stdout[:1000], proc.stderr[-1000:])
    return proc.returncode, json.loads(lines[0])


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def attempt(ws: Path, ops: Any, guards: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A proposal, as the prototype's propose returned it: accepted (``ok`` and the summary) or refused (``ok`` False and why: the
    message, the rules' ``problems`` and the operations' ``details``)."""
    try:
        return dict(S.propose(ws, ops, guards), ok=True)
    except Refused as exc:
        return {"ok": False, "error": str(exc), "problems": list(getattr(exc, "problems", [])), "details": list(getattr(exc, "details", [])),
                "changes": getattr(exc, "changes", None), "index": getattr(exc, "index", None)}


def apply(ws: Path, p: Dict[str, Any]) -> str:
    assert p["ok"], p
    return S.apply(ws, p["proposal"])["revision"]


def revision_files(ws: Path) -> List[str]:
    return sorted(p.name for p in (ws / "edits" / "revisions").glob("*.json"))


def proposal_files(ws: Path) -> List[str]:
    return sorted(p.name for p in (ws / "edits" / "proposals").glob("*.json"))


def copy_ws(src: Path, dst: Path) -> Path:
    shutil.copytree(src, dst)
    return dst


def probe_frames(path: Path) -> Dict[str, float]:
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_entries", "stream=codec_type,nb_read_frames,duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    streams = json.loads(out)["streams"]
    v = next(s for s in streams if s["codec_type"] == "video")
    a = next((s for s in streams if s["codec_type"] == "audio"), None)
    return {"video_frames": int(v["nb_read_frames"]), "video_s": float(v["duration"]), "audio_s": float(a["duration"]) if a else -1.0}


def frames_gray(path: Path, indexes: Sequence[int], size=(320, 180)) -> Dict[int, np.ndarray]:
    """Decoded frames (by index) of a video, grey, for comparison."""
    sel = "+".join(f"eq(n\\,{i})" for i in indexes)
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"select='{sel}',scale={size[0]}:{size[1]},format=gray",
                          "-vsync", "0", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
    n = size[0] * size[1]
    got = [np.frombuffer(out[k * n:(k + 1) * n], dtype=np.uint8).astype(np.float32) for k in range(len(out) // n)]
    return dict(zip(sorted(indexes), got))


def frame_accuracy(source: Path, render: Path, tl: Timeline, rate: Fraction) -> List[Dict[str, Any]]:
    """For three frames in the middle of each clip, the render's frame against the source frames at offsets -2..+2: the best match must be
    offset 0.  Only footage whose neighbouring frames differ can show it; a clip of static frames is reported as not decisive."""
    rows = layout(tl, rate)
    picks = []  # (clip index, source frame, record frame)
    for n, r in enumerate(rows):
        mid = (r.vis_in + r.vis_out) // 2
        picks += [(n, k, r.cut + (k - r.vis_in)) for k in (mid - 1, mid, mid + 1)]
    src = frames_gray(source, sorted({k + d for _, k, _ in picks for d in range(-2, 3)}))
    out = frames_gray(render, sorted({ko for _, _, ko in picks}))
    result = []
    for n, c in enumerate(tl.clips):
        mine = [(k, ko) for m, k, ko in picks if m == n]
        best, decisive = [], True
        for k, ko in mine:
            errs = {d: float(np.mean(np.abs(src[k + d] - out[ko]))) for d in range(-2, 3)}
            best.append(min(errs, key=errs.get))
            decisive &= min(float(np.mean(np.abs(src[k] - src[k + d]))) for d in (-1, 1)) > 0.1
        result.append({"clip": c.id, "best_offsets": best, "decisive": decisive})
    return result


# ----------------------------------------------------------------------------------------------- the fixture workspace
@pytest.fixture(scope="module")
def built(tmp_path_factory) -> SimpleNamespace:
    """The proof's fixture: demo footage and a click track, the pipeline run with the mock backend, a copy without the pin (``nopin``),
    then junction 1 pinned as the Edit view's ``l`` does and the passes re-run.  Tests copy ``ws`` / ``nopin``; nothing changes them."""
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg and ffprobe are needed")
    from matchcut.pipeline.demo import make_click_track, make_demo_footage
    root = tmp_path_factory.mktemp("proof")
    demo, click, ws = root / "demo.mp4", root / "click.wav", root / "ws"
    make_demo_footage(demo)
    make_click_track(click)
    common = [demo, "-w", ws, "--backend", "mock", "-t", "8", "--music", click]
    mc("run", *common, "--no-render")
    nopin = copy_ws(ws, root / "ws-nopin")
    final = Timeline.load(ws / "final.json")
    a, b = final.clips[0], final.clips[1]
    PinStore(ws).put(Pin(a.shot_index, b.shot_index, round(a.source_out, 3), round(b.source_in, 3), "locked", "", clip_origin(a),
                         clip_origin(b)))
    mc("refine", *common, "--no-render")
    return SimpleNamespace(root=root, demo=demo, click=click, ws=ws, nopin=nopin, common=common)


@pytest.fixture
def ws(built, tmp_path) -> Path:
    """A copy of the pinned workspace with a session started at r000."""
    w = copy_ws(built.ws, tmp_path / "ws")
    S.start(w)
    return w


@pytest.fixture
def match_ws(built, tmp_path) -> Path:
    """A copy of the cut before the pin, with c004|c005 marked a match cut as the match-cut pass marks one, and a session on it."""
    w = copy_ws(built.nopin, tmp_path / "ws-nopin")
    doc = json.loads((w / "final.json").read_text())
    a, b = doc["clips"][3], doc["clips"][4]
    a["locked_out"], b["locked_in"] = True, True
    a["tags"] = a["tags"] + ["match_out:7"]
    b["tags"] = b["tags"] + ["match_in:6"]
    (w / "final.json").write_text(json.dumps(doc, indent=2))
    S.start(w)
    return w


# ----------------------------------------------------------------------------------------------- 0, 1, 2: fixture, start, inspect
def test_p01_the_pin_is_applied(built):
    final = Timeline.load(built.ws / "final.json")
    assert final.clips[0].locked_out and final.clips[1].locked_in
    [state] = pin_states(final, PinStore(built.ws).load(), "final")
    assert state.state == "applied"


def test_p02_a_session_starts_at_r000_from_final(built, tmp_path):
    w = copy_ws(built.ws, tmp_path / "ws")
    out = S.start(w)
    assert out["head"] == "r000" and out["base_stage"] == "final" and out["moved_aside"] is None and out["can_publish"]
    assert out["base_sha256"] == sha(w / "final.json") and revision_files(w) == ["r000.json"]


def test_p03_starting_again_is_refused_and_the_session_continues(ws):
    """The prototype's start resumed an open session; M3a's start refuses over a fresh session (``restart`` replaces it) and every other
    command continues the session in WS/edits/."""
    before = S.log(ws)
    with pytest.raises(S.SessionError, match=r"a session is open on final \(head r000\): pass restart"):
        S.start(ws)
    after = S.log(ws)
    assert (after["session"], after["head"]) == (before["session"], "r000")
    apply(ws, attempt(ws, [{"op": "slip", "clip": "c003", "frames": 1}]))
    code, doc = edit_cli(ws, "log")
    assert code == 0 and doc["session"] == before["session"] and doc["head"] == "r001"


def test_p04_inspect_clips_junctions_and_the_pin(ws):
    d = V.inspect(ws)
    assert len(d["clips"]) == 5 and len(d["junctions"]) == 4
    assert d["junctions"][0]["pinned"] and not any(j["pinned"] for j in d["junctions"][1:])
    assert d["junctions"][0]["pin"] == "s0->s1" and d["junctions"][0]["protection"]


def test_p05_unused_shots_with_their_descriptions(ws):
    d = V.inspect(ws)
    assert d["unused_shots"] == [2, 4, 5]
    described = json.loads((ws / "descriptions.json").read_text())
    for s in d["shots"]:
        assert s["description"] is not None and s["description"]["subject"] == described[str(s["shot"])]["subject"]
        assert s["description"]["description"] == described[str(s["shot"])]["description"]
    assert [s["used_by"] for s in d["shots"] if s["shot"] in (2, 4, 5)] == [[], [], []]


def test_p06_music_beat_offsets_per_junction(ws):
    d = V.inspect(ws)
    assert all(isinstance(j["beat_offset_frames"], int) for j in d["junctions"])
    assert [j["offset_frames"] for j in d["music"]["junctions"]] == [j["beat_offset_frames"] for j in d["junctions"]]


def test_p07_story_beats_with_target_and_actual_length(ws):
    d = V.inspect(ws)
    beats = d["story_beats"]
    narrative = {b["id"]: b for b in json.loads((ws / "narrative.json").read_text())["beats"]}
    assert len(beats) == 3 and [b["beat"] for b in beats] == ["b1", "b2", "b3"]
    for b in beats:
        target = narrative[b["beat"]]["target_duration_s"]
        assert b["target_seconds"] == target and b["target_frames"] == round(target * FPS)
        assert b["frames"] == b["record_frames"][1] - b["record_frames"][0] > 0
    assert sum(b["frames"] for b in beats) <= d["duration_frames"]


# ----------------------------------------------------------------------------------------------- 2b: refusals
REFUSALS = [
    pytest.param([{"op": "trim", "clip": "c001", "edge": "out", "frames": -2}], "held by an editor pin", id="P08"),
    pytest.param([{"op": "roll", "junction": 1, "frames": 2}], "held by an editor pin", id="P09"),
    pytest.param([{"op": "snap_to_beat", "junction": 1}], "held by an editor pin", id="P11"),
    pytest.param([{"op": "roll", "junction": 2, "frames": 3}], "would leave shot 1", id="P12"),
    pytest.param([{"op": "trim", "clip": "c003", "edge": "out", "frames": -40}], "below the minimum clip length", id="P13"),
    pytest.param([{"op": "remove", "clip": "c002"}], "breaks the editor pin", id="P14"),
    pytest.param([{"op": "move", "clip": "c002", "after": "c004"}], "breaks the editor pin", id="P15"),
    pytest.param([{"op": "roll", "junction": 3, "frames": 2}, {"op": "trim", "clip": "c001", "edge": "out", "frames": -1}],
                 "held by an editor pin", id="P16"),
    pytest.param([{"op": "trim", "clip": "c999", "edge": "in", "frames": 1}], "no clip c999", id="P17"),
    pytest.param([{"op": "explode"}], "unknown op", id="P18"),
]


@pytest.mark.parametrize("ops, why", REFUSALS)
def test_refusals(ws, ops, why):
    p = attempt(ws, ops)
    assert not p["ok"] and why in " ".join([p["error"]] + p["problems"]), p
    assert revision_files(ws) == ["r000.json"] and proposal_files(ws) == [] and S.log(ws)["head"] == "r000"
    if len(ops) > 1:  # the batch: the first op alone is fine; the whole is refused at the second
        assert p["index"] == 1 and attempt(ws, ops[:1])["ok"]


def test_p10_a_pinned_junction_may_change_its_transition_unless_protected(ws):
    """The prototype refused any transition change at a pin; decision 2 allows it unless the pin protects the transition."""
    p = attempt(ws, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])
    assert p["ok"] and p["changes"] == ["c001|c002: cut -> dissolve 4f"]
    out = V.pin_transition(ws, pin="s0->s1", hold=True)
    assert out["held_transition"] == {"kind": "cut", "frames": 0}
    p = attempt(ws, [{"op": "set_transition", "junction": 1, "kind": "dissolve", "frames": 4}])
    assert not p["ok"] and "protected by the editor pin s0->s1" in p["error"]
    # the cut points stay held whatever the protection
    assert "held by an editor pin" in attempt(ws, [{"op": "roll", "junction": 1, "frames": 1}])["error"]


def test_p19_no_refusal_writes_a_revision_or_a_proposal(ws):
    for param in REFUSALS:
        assert not attempt(ws, param.values[0])["ok"]
    assert revision_files(ws) == ["r000.json"] and proposal_files(ws) == [] and V.inspect(ws)["session"]["head"] == "r000"


def test_p20_a_refused_proposal_cannot_be_applied(ws):
    ops = [{"op": "remove", "clip": "c002"}]
    assert not attempt(ws, ops)["ok"] and proposal_files(ws) == []  # a refusal is never stored, so there is nothing to apply
    s = S.load(ws)
    would_be = S.proposal_id("r000", s.timeline("r000"), ops, None, s.reserved_ids(), S.context_sha256(s.context()))
    with pytest.raises(S.SessionError, match=f"no proposal {would_be}"):
        S.apply(ws, would_be)
    code, doc = edit_cli(ws, "apply", {"proposal": would_be})
    assert code == 1 and doc["kind"] == "refused" and f"no proposal {would_be}" in doc["error"]
    assert revision_files(ws) == ["r000.json"]


def test_p21_r000_is_on_whole_frames(ws):
    r0 = S.load(ws).timeline("r000")
    for c in r0.clips:
        for x in (c.source_in, c.source_out, c.transition_in.duration):
            assert abs(x * FPS - round(x * FPS)) < 1e-6, (c.id, x)


def test_p22_a_trim_that_leaves_a_transition_over_the_limit_is_refused(ws):
    p = attempt(ws, [{"op": "trim", "clip": "c004", "edge": "in", "frames": 25}])
    assert not p["ok"] and any("longer than 40%" in x for x in p["problems"]), p


def test_p23_a_fractional_frame_count_is_refused(ws):
    p = attempt(ws, [{"op": "trim", "clip": "c003", "edge": "out", "frames": 1.9}])
    assert not p["ok"] and "frames must be a whole number, not 1.9" in p["error"]
    code, doc = edit_cli(ws, "propose", {"ops": [{"op": "trim", "clip": "c003", "edge": "out", "frames": 1.9}]})
    assert code == 2 and doc["kind"] == "usage" and any("1.9" in e for e in doc["errors"]), doc
    assert proposal_files(ws) == []


def test_p24_changing_one_transition_moves_no_other_junction(ws):
    before = V.inspect(ws)["junctions"]
    p = attempt(ws, [{"op": "set_transition", "junction": 2, "kind": "cut"}])
    assert p["ok"] and p["changes"] == ["c002|c003: dissolve 8f -> cut"]
    assert p["timing"]["duration_change"] == 8
    # the 8 frames the dissolve overlapped come back exactly (whole frames, no rounding drift): junction 2 (where c003 starts to show)
    # and every later junction move by exactly 8 frames, junction 1 not at all
    assert [(m["id"], m["delta"]) for m in p["timing"]["moved"]] == [(j["id"], 8) for j in before[1:]]
    r0 = V.inspect(ws)["clips"]
    after = V.inspect(ws, apply(ws, p))
    assert [j["record_frame"] for j in after["junctions"]] == [before[0]["record_frame"]] + [j["record_frame"] + 8 for j in before[1:]]
    # no other junction changes: every other transition, and every clip's source frames, stay as they were
    assert [j["transition"] for j in after["junctions"] if j["n"] != 2] == [j["transition"] for j in before if j["n"] != 2]
    assert [c["source_frames"] for c in after["clips"]] == [c["source_frames"] for c in r0]


# ----------------------------------------------------------------------------------------------- 2c: validation
def test_p25_ops_that_are_not_a_list_are_refused(ws):
    with pytest.raises(S.SessionError, match="ops must be a list of operations"):
        S.propose(ws, "trim")
    code, doc = edit_cli(ws, "propose", {"ops": "trim"})
    assert code == 2 and doc["kind"] == "usage"


def test_p26_a_revision_id_that_is_a_path_is_refused(ws):
    with pytest.raises(S.SessionError, match="not a revision id"):
        S.checkout(ws, "../../final")
    code, doc = edit_cli(ws, "checkout", {"revision": "../../final"})
    assert code == 2 and doc["kind"] == "usage"
    assert S.log(ws)["head"] == "r000"


def test_p27_a_proposal_id_that_is_a_path_is_refused(ws):
    with pytest.raises(S.SessionError, match="not a proposal id"):
        S.apply(ws, "../session")
    code, doc = edit_cli(ws, "apply", {"proposal": "../session"})
    assert code == 2 and doc["kind"] == "usage"


def test_p28_p29_a_hand_edited_proposal_is_refused_and_the_head_stays(ws):
    good = attempt(ws, [{"op": "roll", "junction": 3, "frames": 1}])
    path = ws / "edits" / "proposals" / f"{good['proposal']}.json"
    doc = json.loads(path.read_text())
    doc["timeline"]["clips"][0]["source_out"] = 1.0  # a hand-edited proposal file that would cut the pinned clip
    path.write_text(json.dumps(doc))
    with pytest.raises(S.SessionError, match="does not match what its operations give; propose again"):
        S.apply(ws, good["proposal"])
    assert V.inspect(ws)["session"]["head"] == "r000" and revision_files(ws) == ["r000.json"]


# ----------------------------------------------------------------------------------------------- 2d: match cuts and the first clip
@pytest.mark.parametrize("ops, why", [
    pytest.param([{"op": "insert", "shot": 5, "in_frame": 300, "out_frame": 325, "after": "c004"}], "breaks the match cut c004|c005",
                 id="P30"),
    pytest.param([{"op": "remove", "clip": "c005"}], "breaks the match cut c004|c005", id="P31"),
    pytest.param([{"op": "move", "clip": "c005", "before": "c003"}], "breaks the match cut c004|c005", id="P32"),
    pytest.param([{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 4}], "match cuts stay hard cuts", id="P33"),
    pytest.param([{"op": "trim", "clip": "c004", "edge": "out", "frames": -2}], "held by a match cut", id="P34"),
])
def test_match_cut_refusals(match_ws, ops, why):
    assert V.inspect(match_ws)["junctions"][3]["match_cut"]
    p = attempt(match_ws, ops)
    assert not p["ok"] and why in " ".join([p["error"]] + p["problems"]), p
    assert revision_files(match_ws) == ["r000.json"]


def test_p35_a_move_leaving_a_dissolve_beside_a_too_short_clip_is_refused(match_ws):
    p = attempt(match_ws, [{"op": "move", "clip": "c003", "before": "c001"}])
    assert not p["ok"] and any("dissolve at c002|c004 is longer than 40%" in x for x in p["problems"]), p


def test_p36_removing_the_first_clips_drops_the_new_first_clips_dissolve(match_ws):
    p = attempt(match_ws, [{"op": "remove", "clip": "c001"}, {"op": "remove", "clip": "c002"}])
    assert p["ok"] and "c003: incoming dissolve 8f dropped (it is the first clip now)" in p["changes"], p
    tl = S.load(match_ws).timeline(apply(match_ws, p))
    assert tl.clips[0].id == "c003" and tl.clips[0].transition_in.is_cut


# ----------------------------------------------------------------------------------------------- 3-7: the edits (the proof's script)
def session_script(ws: Path) -> Dict[str, Any]:
    """The proof's edits, in order, through the API; returns what each step observed (nothing is asserted here: ``SCRIPT_CHECKS`` do)."""
    o: Dict[str, Any] = {"made": []}

    def offsets() -> List[int]:
        return [j["beat_offset_frames"] for j in V.inspect(ws)["junctions"]]

    o["r000"] = V.inspect(ws, "r000")
    o["o3"] = o["r000"]["junctions"][2]["beat_offset_frames"]
    # 3 roll
    o["roll3"] = attempt(ws, [{"op": "roll", "junction": 3, "frames": 3}])
    o["roll1"] = attempt(ws, [{"op": "roll", "junction": 3, "frames": 1}])
    if o["roll1"]["ok"]:
        o["made"].append(apply(ws, o["roll1"]))
        o["r001"] = V.inspect(ws, o["made"][-1])
    # 4 replace
    o["replace"] = attempt(ws, [{"op": "replace", "clip": "c004", "shot": 4}])
    if o["replace"]["ok"]:
        o["preview"] = V.preview(ws, o["replace"]["proposal"])
        o["head_after_preview"] = V.inspect(ws)["session"]["head"]
        o["made"].append(apply(ws, o["replace"]))
        o["r002_clips"] = S.load(ws).timeline(o["made"][-1]).clips
    # 5 transition
    o["dissolve40"] = attempt(ws, [{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 40}])
    o["dissolve6"] = attempt(ws, [{"op": "set_transition", "junction": 4, "kind": "dissolve", "frames": 6}])
    o["j4_before"] = V.inspect(ws)["junctions"][3]["record_frame"]
    if o["dissolve6"]["ok"]:
        o["made"].append(apply(ws, o["dissolve6"]))
    o["j4_after"] = V.inspect(ws)["junctions"][3]["record_frame"]
    # 6 beat
    o["offsets"] = offsets()
    o["snap3"] = attempt(ws, [{"op": "snap_to_beat", "junction": 3}])
    if o["snap3"]["ok"]:
        o["made"].append(apply(ws, o["snap3"]))
    o["offsets_after"] = offsets()
    o["revisions_before_snap2"] = revision_files(ws)
    o["proposals_before_snap2"] = proposal_files(ws)
    o["snap2"] = attempt(ws, [{"op": "snap_to_beat", "junction": 2}])
    o["revisions_after_snap2"] = revision_files(ws)
    o["proposals_after_snap2"] = proposal_files(ws)
    # 7 batch
    o["batch"] = attempt(ws, [{"op": "insert", "shot": 5, "in_frame": 325, "out_frame": 350, "after": "c005"},
                              {"op": "set_transition", "junction": 5, "kind": "dip_to_black", "frames": 6}])
    if o["batch"]["ok"]:
        o["made"].append(apply(ws, o["batch"]))
        o["batch_frames"] = V.inspect(ws)["clips"][-1]
    o["log"] = S.log(ws)
    o["revision_sha256"] = [S.load(ws).revision(r)["timeline_sha256"] for r in S.load(ws).revision_ids()]
    o["revision_timelines"] = [S.load(ws).revision(r)["timeline"] for r in S.load(ws).revision_ids()]
    o["proposal_ids"] = [o[k]["proposal"] for k in ("roll1", "replace", "dissolve6", "snap3", "batch") if o[k]["ok"]]
    return o


def check_p37(o):
    assert not o["roll3"]["ok"] and any("the 19-frame dissolve at c003|c004 is longer than 40%" in x for x in o["roll3"]["problems"])


def check_p38(o):
    p = o["roll1"]
    assert p["ok"] and sorted(p["changes"]) == ["c003: out +1f", "c004: in +1f"] and sorted(p["touched"]) == ["c003", "c004"]


def check_p39(o):
    t = o["roll1"]["timing"]
    assert t["duration_change"] == 0 and len(set(t["duration_frames"])) == 1


def check_p40(o):
    """Every clip the roll does not name is identical in frames (source and record), and only junction 3 moved, by its frame."""
    before, after = o["r000"], o["r001"]
    assert [c["id"] for c in after["clips"]] == [c["id"] for c in before["clips"]]
    for b, a in zip(before["clips"], after["clips"]):
        if b["id"] not in ("c003", "c004"):
            assert {k: b[k] for k in ("source_frames", "record_frames", "transition_in")} == {
                k: a[k] for k in ("source_frames", "record_frames", "transition_in")}, b["id"]
    moved = [(jb["n"], ja["record_frame"] - jb["record_frame"]) for jb, ja in zip(before["junctions"], after["junctions"])
             if ja["record_frame"] != jb["record_frame"]]
    assert moved == [(3, 1)] and [m["id"] for m in o["roll1"]["timing"]["moved"]] == ["s3-s6"]


def check_p41(o):
    assert o["roll1"]["ok"] and o["made"][0] == "r001"


def check_p42(o):
    p = o["replace"]
    assert p["ok"] and p["timing"]["duration_frames"][0] == p["timing"]["duration_frames"][1]
    assert sorted(e.split(" (")[0] for e in p["changes"]) == ["added e001", "removed c004"]
    assert any(e.startswith("added e001 (shot 4, ") for e in p["changes"])


def check_p43(o):
    prev = o["preview"]
    assert Path(prev["path"]).exists() and prev["frames"] == o["replace"]["timing"]["duration_frames"][1]
    assert prev["proposal"] == o["replace"]["proposal"] and o["head_after_preview"] == "r001"
    assert probe_frames(Path(prev["path"]))["video_frames"] == prev["frames"]


def check_p44(o):
    assert o["replace"]["ok"] and o["made"][1] == "r002"


def check_p45(o):
    c = o["r002_clips"][3]
    assert c.id == "e001" and c.shot_index == 4 and c.transition_in.kind == "dissolve"
    assert round(c.transition_in.duration * FPS) == 19


def check_p46(o):
    p = o["dissolve40"]
    assert not p["ok"] and "is longer than 40% of the shorter clip" in p["error"]


def check_p47(o):
    p = o["dissolve6"]
    assert p["ok"] and p["changes"] == ["e001|c005: cut -> dissolve 6f"]


def check_p48(o):
    assert o["dissolve6"]["ok"] and o["made"][2] == "r003"


def check_p49(o):
    t = o["dissolve6"]["timing"]
    assert o["j4_after"] == o["j4_before"] - 6
    assert t["duration_frames"][1] == t["duration_frames"][0] - 6 and t["duration_change"] == -6
    assert t["moved"] == [{"id": "s4-s7", "clips": ["e001", "c005"], "before": o["j4_before"], "after": o["j4_before"] - 6, "delta": -6}]


def check_p50(o):
    assert o["offsets"][3] != 0 and "s4-s7" in o["dissolve6"]["timing"]["left_beat"]


def check_p51(o):
    p = o["snap3"]
    assert o["offsets"][2] == o["o3"] + 1 and o["offsets"][2] != 0
    assert p["ok"] and f"{-o['offsets'][2]:+d}f onto the " in p["details"][0], p
    assert p["timing"]["reached_beat"] == ["s3-s4"]


def check_p52(o):
    assert o["snap3"]["ok"] and o["made"][3] == "r004"


def check_p53(o):
    before, after = o["offsets"], o["offsets_after"]
    assert after == before[:2] + [0] + before[3:]
    assert [m["id"] for m in o["snap3"]["timing"]["moved"]] == ["s3-s4"] and o["snap3"]["timing"]["duration_change"] == 0


def check_p54(o):
    """Superseded form: the prototype accepted an empty proposal; M3a refuses a proposal that changes nothing, says why, and stores
    nothing.  The behaviour checked (nothing changes) is the same."""
    p = o["snap2"]
    assert o["offsets_after"][1] == 0
    assert not p["ok"] and p["error"] == "refused: the operations change nothing" and p["changes"] == []
    assert "is already on the " in p["details"][0], p
    assert o["revisions_after_snap2"] == o["revisions_before_snap2"] and o["proposals_after_snap2"] == o["proposals_before_snap2"]


def check_p55(o):
    p = o["batch"]
    assert p["ok"] and len(p["timing"]["added"]) == 1
    assert p["changes"] == ["added e002 (shot 5, frames 325-350, incoming dip_to_black 6f)"]


def check_p56(o):
    c = o["batch_frames"]
    assert o["batch"]["ok"] and o["made"][4] == "r005"
    assert (c["id"], c["shot"], c["source_frames"], c["transition_in"]) == ("e002", 5, [325, 350], {"kind": "dip_to_black", "frames": 6})


def check_p57(o):
    assert o["made"] == ["r001", "r002", "r003", "r004", "r005"]
    assert [r["id"] for r in o["log"]["revisions"]] == ["r000", "r001", "r002", "r003", "r004", "r005"] and o["log"]["head"] == "r005"


SCRIPT_CHECKS: List[Callable[[Dict[str, Any]], None]] = [
    check_p37, check_p38, check_p39, check_p40, check_p41, check_p42, check_p43, check_p44, check_p45, check_p46, check_p47, check_p48,
    check_p49, check_p50, check_p51, check_p52, check_p53, check_p54, check_p55, check_p56, check_p57]


@pytest.fixture(scope="module")
def scripted(built) -> SimpleNamespace:
    """The proof's edits on a copy of the workspace (r001..r005), and, for the determinism check, the same edits on a second, fresh copy."""
    root = built.root / "scripted"
    first = copy_ws(built.ws, root / "ws")
    second = copy_ws(built.ws, root / "ws-again")
    S.start(first)
    o = session_script(first)
    S.start(second)
    o2 = session_script(second)
    return SimpleNamespace(ws=first, o=o, again=second, o2=o2, root=root)


def test_p37_a_3_frame_roll_is_refused(scripted):
    check_p37(scripted.o)


def test_p38_a_1_frame_roll_moves_exactly_two_edges(scripted):
    check_p38(scripted.o)


def test_p39_a_roll_keeps_the_running_time(scripted):
    check_p39(scripted.o)


def test_p40_clips_not_named_are_unchanged(scripted):
    check_p40(scripted.o)


def test_p41_the_roll_is_accepted_and_applied(scripted):
    check_p41(scripted.o)


def test_p42_c004_is_replaced_by_a_new_clip_of_shot_4(scripted):
    check_p42(scripted.o)


def test_p43_the_proposal_previews_before_it_is_applied(scripted):
    check_p43(scripted.o)


def test_p44_the_replace_is_accepted_and_applied(scripted):
    check_p44(scripted.o)


def test_p45_the_replacement_keeps_the_dissolve(scripted):
    check_p45(scripted.o)


def test_p46_a_dissolve_over_the_limit_is_refused(scripted):
    check_p46(scripted.o)


def test_p47_junction_4_becomes_a_6_frame_dissolve(scripted):
    check_p47(scripted.o)


def test_p48_the_transition_is_accepted_and_applied(scripted):
    check_p48(scripted.o)


def test_p49_the_dissolve_ripples_later_cuts(scripted):
    check_p49(scripted.o)


def test_p50_junction_4_left_its_beat(scripted):
    check_p50(scripted.o)


def test_p51_junction_3_is_rolled_onto_its_beat(scripted):
    check_p51(scripted.o)


def test_p52_the_snap_is_accepted_and_applied(scripted):
    check_p52(scripted.o)


def test_p53_only_junction_3_moved(scripted):
    check_p53(scripted.o)


def test_p54_snapping_a_junction_already_on_its_beat_changes_nothing(scripted):
    check_p54(scripted.o)


def test_p55_an_insert_and_a_dip_to_black_in_one_proposal(scripted):
    check_p55(scripted.o)


def test_p56_the_batch_is_accepted_and_applied(scripted):
    check_p56(scripted.o)


def test_p57_five_revisions(scripted):
    check_p57(scripted.o)


# ----------------------------------------------------------------------------------------------- 8, 9: undo, redo, a branch, compare
@pytest.fixture(scope="module")
def branched(scripted) -> SimpleNamespace:
    w = copy_ws(scripted.ws, scripted.root / "ws-branch")
    o: Dict[str, Any] = {}
    o["undo"] = S.undo(w)
    o["redo"] = S.redo(w)
    S.undo(w)
    o["old"] = attempt(w, [{"op": "slip", "clip": "c003", "frames": 1}])
    o["new"] = attempt(w, [{"op": "set_transition", "junction": 3, "kind": "cut"}, {"op": "move", "clip": "e001", "before": "c003"},
                           {"op": "remove", "clip": "c005"}, {"op": "slip", "clip": "c003", "frames": 2}])
    o["r004"] = V.inspect(w, "r004")
    o["applied"] = S.apply(w, o["new"]["proposal"]) if o["new"]["ok"] else None
    o["r006"] = V.inspect(w, "r006") if o["applied"] else None
    try:
        S.apply(w, o["old"]["proposal"])
        o["old_applied"] = None
    except Refused as exc:
        o["old_applied"] = str(exc)
    S.undo(w)
    o["redo_branch"] = S.redo(w)
    o["checkout"] = S.checkout(w, "r005")
    o["log"] = S.log(w)
    o["compare"] = S.compare(w, "r000", "r004")
    return SimpleNamespace(ws=w, o=o)


def test_p58_undo_moves_the_head_to_r004(branched):
    assert branched.o["undo"] == {"head": "r004", "previous": "r005"}


def test_p59_redo_comes_back_to_r005(branched):
    assert branched.o["redo"] == {"head": "r005", "previous": "r004", "branches": ["r005"]}


def test_p60_a_cut_a_move_a_remove_and_a_slip_each_reported(branched):
    p = branched.o["new"]
    assert p["ok"] and sorted(e.split(" (")[0] for e in p["changes"]) == [
        "c002|e001: dissolve 19f -> cut", "c003: slipped +2f", "order: c001 c002 e001 c003", "removed c005"]
    assert len(p["details"]) == 4, p["details"]  # one line per operation
    # untouched clips identical in frames: c001's and c002's source ranges
    r4 = {c["id"]: c for c in branched.o["r004"]["clips"]}
    r6 = {c["id"]: c for c in branched.o["r006"]["clips"]}
    assert all(r6[k]["source_frames"] == r4[k]["source_frames"] for k in ("c001", "c002", "e001"))


def test_p61_an_edit_after_an_undo_makes_a_branch(branched):
    a = branched.o["applied"]
    assert a["revision"] == "r006" and a["parent"] == "r004"
    assert (branched.ws / "edits" / "revisions" / "r005.json").exists()


def test_p62_a_proposal_made_before_the_head_moved_is_refused(branched):
    msg = branched.o["old_applied"]
    assert msg is not None and "head moved" in msg and "was made at r004, the head is now r006: propose again" in msg


def test_p63_redo_takes_the_latest_branch(branched):
    assert branched.o["redo_branch"] == {"head": "r006", "previous": "r004", "branches": ["r005", "r006"]}


def test_p64_checkout_moves_the_head_to_r005(branched):
    assert branched.o["checkout"] == {"head": "r005", "previous": "r006"}


def test_p65_the_log_lists_every_revision_with_its_parent(branched):
    log = branched.o["log"]
    assert [x["parent"] for x in log["revisions"]] == [None, "r000", "r001", "r002", "r003", "r004", "r004"]
    assert [x["id"] for x in log["revisions"]] == ["r000", "r001", "r002", "r003", "r004", "r005", "r006"] and log["head"] == "r005"


def test_p66_compare_shows_the_net_effect(branched, scripted):
    cmp = branched.o["compare"]
    assert sorted(e.split(" (")[0] for e in cmp["changes"]) == ["added e001", "e001|c005: cut -> dissolve 6f", "removed c004"], cmp
    # the roll (+1f on c003 out and the replacement's in) and the snap (-1f) combine: c003 is back where r000 had it, and e001 starts one
    # frame earlier than the replace put it
    replaced = next(e for e in scripted.o["replace"]["changes"] if e.startswith("added e001"))
    net = next(e for e in cmp["changes"] if e.startswith("added e001"))
    first = [int(x) for x in replaced.split("frames ")[1].split(",")[0].split("-")]
    now = [int(x) for x in net.split("frames ")[1].split(",")[0].split("-")]
    assert now == [first[0] - 1, first[1]]
    assert cmp["timing"]["duration_change"] == -6


# ----------------------------------------------------------------------------------------------- 10: render
@pytest.fixture(scope="module")
def rendered(scripted) -> SimpleNamespace:
    w = scripted.ws
    full = V.render(w, "r005", None, "full")
    r4 = V.render(w, "r004", None, "full")
    j3 = V.render(w, "r005", 3)
    j5 = V.render(w, "r005", 5)  # c005 (shot 7) -> e002 (shot 5): out of source order, with music
    return SimpleNamespace(ws=w, full=full, r4=r4, j3=j3, j5=j5)


def test_p67_the_render_has_exactly_the_cuts_frames(rendered):
    meta = probe_frames(Path(rendered.full["path"]))
    assert meta["video_frames"] == rendered.full["frames"] == V.inspect(rendered.ws, "r005")["duration_frames"]
    assert rendered.full["quality"] == "full" and rendered.full["music"]


def test_p68_audio_and_video_lengths_agree(rendered):
    meta = probe_frames(Path(rendered.full["path"]))
    assert abs(meta["audio_s"] - meta["video_s"]) <= 2 / FPS + 1e-6, meta


def test_p69_r004_has_audio_as_long_as_its_video(rendered):
    meta = probe_frames(Path(rendered.r4["path"]))
    assert meta["video_frames"] == rendered.r4["frames"] and abs(meta["audio_s"] - meta["video_s"]) <= 2 / FPS + 1e-6, meta


def test_p70_every_decisive_clip_shows_its_exact_source_frames(rendered, built):
    tl = S.load(rendered.ws).timeline("r005")
    acc = frame_accuracy(built.demo, Path(rendered.full["path"]), tl, Fraction(FPS))
    decisive = [a for a in acc if a["decisive"]]
    assert len(decisive) >= 3 and all(set(a["best_offsets"]) == {0} for a in decisive), acc


def test_p71_a_junctions_review_window_renders(rendered):
    j = rendered.j3
    assert Path(j["path"]).exists() and j["kind"] == "window" and j["junction"] == {"n": 3, "id": "s3-s4"}
    assert probe_frames(Path(j["path"]))["video_frames"] == j["frames"] == j["record_frames"][1] - j["record_frames"][0]


def test_p72_an_out_of_order_window_has_audio_as_long_as_its_video(rendered):
    meta = probe_frames(Path(rendered.j5["path"]))
    assert rendered.j5["music"] and meta["video_frames"] == rendered.j5["frames"]
    assert abs(meta["audio_s"] - meta["video_s"]) <= 2 / FPS + 1e-6, meta


# ----------------------------------------------------------------------------------------------- 11: publish (decision 1)
@pytest.fixture(scope="module")
def published(scripted) -> SimpleNamespace:
    """As the proof: an editor's round trip first (final exported to OTIO and imported back: human.json now holds an NLE import), then
    publishing r005."""
    w = copy_ws(scripted.ws, scripted.root / "ws-publish")
    nle = scripted.root / "nle"
    mc("export", w, "--stage", "final", "--format", "otio", "--out", nle)
    mc("import", w, nle / "final.otio")
    o: Dict[str, Any] = {"human_before": {n: (w / n).read_bytes() for n in ("human.json", "human_import.json")},
                         "human_fresh_before": stale_human(w) is None}
    try:
        P.publish(w)
        o["refused"] = None
    except Refused as exc:
        o["refused"] = str(exc)
    o["edit_after_refusal"] = (w / "edit.json").exists()
    o["pub"] = P.publish(w, supersede_human=True)
    o["human_after"] = {n: (w / n).read_bytes() for n in o["human_before"]}
    o["junctions"] = mc("junctions", w).stdout
    return SimpleNamespace(ws=w, o=o)


def test_p73_publishing_over_a_fresh_editor_import_is_refused(published):
    o = published.o
    assert o["human_fresh_before"]
    assert o["refused"] is not None and "human.json (an editor's re-export, imported against final) is the latest stage" in o["refused"]
    assert "pass supersede_human to publish anyway" in o["refused"] and not o["edit_after_refusal"]


def test_p74_the_editors_import_is_never_touched(published):
    o = published.o
    assert o["human_after"] == o["human_before"]  # never moved or changed: re-importing the editor's file brings it back
    assert o["pub"]["human_left_out"] == stale_human(published.ws) and "edit.json was published after the import" in o["pub"]["human_left_out"]
    assert not (published.ws / "edits" / "replaced-imports").exists()


def test_p75_the_head_becomes_the_edit_stage(published):
    w, pub = published.ws, published.o["pub"]
    assert pub["revision"] == "r005" and stale_edit(w) is None
    assert (w / "edit.json").read_bytes() == (w / pub["published"]).read_bytes() and pub["edit_sha256"] == sha(w / "edit.json")
    edit = Timeline.load(w / "edit.json")
    assert edit.stage == "edit" and [c.to_dict() for c in edit.clips] == [c.to_dict() for c in S.load(w).timeline("r005").clips]
    assert [name for name, _ in stage_timeline_paths(w)][-1] == "edit"


def test_p76_the_editor_pin_is_held_in_the_edit_stage(published):
    w = published.ws
    states = pin_states(Timeline.load(w / "edit.json"), PinStore(w).load(), "edit")
    assert [(s.label, s.state) for s in states] == [("s0->s1", "applied")]
    assert [(p["pin"], p["state"], p["transition_mismatch"]) for p in published.o["pub"]["pins"]] == [("s0->s1", "held", False)]


def test_p77_junctions_lists_the_edit_stage_with_the_sessions_clip_ids(published):
    out = published.o["junctions"]
    assert "5 junctions in edit" in out and "c003->e001" in out and "c005->e002" in out and "h001" not in out
    assert "locked pinned" in out.splitlines()[1] and "pin s0->s1: held by this edit" in out


def test_p78_the_edit_stage_history_names_each_change(published):
    w = published.ws
    edit, final = Timeline.load(w / "edit.json"), Timeline.load(w / "final.json")
    assert [d.to_dict() for d in edit.history[:len(final.history)]] == [d.to_dict() for d in final.history]
    agent = [(d.action, d.clip_ids) for d in edit.history[len(final.history):]]
    assert all(d.pass_name == "agent" for d in edit.history[len(final.history):])
    assert ("transition", ["e001", "c005"]) in agent and ("added", ["e002"]) in agent and ("removed", ["c004"]) in agent
    assert ("added", ["e001"]) in agent and not any(a == "trimmed" and ids == ["c005"] for a, ids in agent)
    assert published.o["pub"]["decisions"] == len(agent)


def editor_otio(w: Path, out: Path, fingerprint: str) -> Path:
    """An editor's re-export of the exported edit stage: the last clip's out-point 5 frames earlier, carrying the stage and fingerprint."""
    cfg = PipelineConfig.load(w / "config.json")
    src = source_info(cfg.source)
    bounds = {s.index: (s.start, s.end) for s in load_shots(w / "shots.json")}
    snapped, _ = export_timeline(Timeline.load(w / "edit.json"), bounds, src)
    ed = snapped.copy()
    ed.clips[-1].source_out = round(float(Fraction(round(ed.clips[-1].source_out * FPS) - 5) / FPS), 6)
    js = build_junctions(load_stage_timelines(w))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(export_otio(ed, "edit", src, js, stage_sha256=fingerprint)))
    return out


def test_p79_the_edit_stage_exports_to_otio_fcpxml_and_edl_and_reimports(scripted, tmp_path):
    """The prototype's known gap, closed: an edited cut is published (the edit CLI), exported to OTIO, FCPXML and EDL (the pipeline CLI),
    with fidelity, and an editor's files come back against that exact export."""
    w = copy_ws(scripted.ws, tmp_path / "ws")
    code, pub = edit_cli(w, "publish")
    assert code == 0 and pub["revision"] == "r005"
    ex = mc("export", w, "--stage", "edit", check=False)
    assert ex.returncode == 0, ex.stderr
    assert "fidelity: 3 checks, 0 errors" in ex.stdout
    out = w / "exports"
    digest = sha(w / "edit.json")
    files = {"otio": out / "edit.otio", "fcpxml": out / "matchcut.fcpxml", "edl": out / "edit.edl"}
    assert all(f.exists() for f in files.values())
    # fidelity: the report covers the edit stage, every format ok
    report = json.loads((out / "fidelity.json").read_text())
    assert report["ok"] is True and set(report["stages"]) == {"edit"}
    assert all(r["ok"] and not r["errors"] for r in report["stages"]["edit"].values()), report["stages"]["edit"]
    # each file carries the stage and its fingerprint, the sha256 of edit.json; the snapshot is kept
    assert json.loads(files["otio"].read_text())["metadata"]["matchcut"]["stage_sha256"] == digest
    assert digest in files["fcpxml"].read_text()
    assert {"* MATCHCUT STAGE edit", f"* MATCHCUT STAGE_SHA256 {digest}"} <= set(files["edl"].read_text().splitlines())
    snapshot = out / ".stages" / f"edit-{digest[:12]}.json"
    assert snapshot.read_bytes() == (w / "edit.json").read_bytes()
    # re-import: each unchanged file comes back against the edit snapshot with no decision, frame for frame the edit stage
    cfg = PipelineConfig.load(w / "config.json")
    src = source_info(cfg.source)
    bounds = {s.index: (s.start, s.end) for s in load_shots(w / "shots.json")}
    snapped, _ = export_timeline(Timeline.load(w / "edit.json"), bounds, src)
    for fmt, f in files.items():
        imp = mc("import", w, f, check=False)
        assert imp.returncode == 0 and "against edit: 0 human decisions" in imp.stdout, (fmt, imp.stdout, imp.stderr)
        record = json.loads((w / "human_import.json").read_text())
        assert record["stage"] == "edit" and record["decisions"] == 0, (fmt, record)
        assert not differences(snapped, Timeline.load(w / "human.json"), src.rate, 0), fmt
        assert stale_human(w) is None
    # an editor's change comes back as exactly that change
    edited = editor_otio(w, tmp_path / "nle" / "edited.otio", digest)
    imp = mc("import", w, edited, check=False)
    assert imp.returncode == 0 and "against edit: 1 human decision" in imp.stdout, (imp.stdout, imp.stderr)
    human = Timeline.load(w / "human.json")
    trims = [(d.action, d.clip_ids, d.data) for d in human.history if d.pass_name == "human"]
    assert trims == [("trim", ["e002"], {"in_frames": 0, "out_frames": -5})], trims


def test_p79_a_reimport_compares_with_the_exact_snapshot_after_a_later_publish(scripted, tmp_path):
    w = copy_ws(scripted.ws, tmp_path / "ws")
    assert edit_cli(w, "publish")[0] == 0
    assert mc("export", w, "--stage", "edit", "--format", "otio").returncode == 0
    first = sha(w / "edit.json")
    exported = w / "exports" / "edit.otio"
    kept = tmp_path / "kept.otio"
    shutil.copy(exported, kept)
    # the session edits on and publishes again: edit.json changes
    apply(w, attempt(w, [{"op": "trim", "clip": "c003", "edge": "out", "frames": -2}]))
    code, again = edit_cli(w, "publish")
    assert code == 0 and again["previous"] == "r005" and sha(w / "edit.json") != first
    imp = mc("import", w, kept, check=False)
    assert imp.returncode == 0 and "against edit: 0 human decisions" in imp.stdout, (imp.stdout, imp.stderr)
    record = json.loads((w / "human_import.json").read_text())
    assert first[:12] in json.dumps(record) and record["decisions"] == 0, record


# ----------------------------------------------------------------------------------------------- 12: MCP (M3b) and the JSON CLI
@pytest.mark.skip(reason=MCP)
def test_p80_mcp_initialize_and_tools_list():
    raise AssertionError("not part of M3a")


@pytest.mark.skip(reason=MCP)
def test_p81_mcp_inspect_equals_the_python_call():
    raise AssertionError("not part of M3a")


@pytest.mark.skip(reason=MCP)
def test_p82_mcp_refusal_carries_its_reason():
    raise AssertionError("not part of M3a")


@pytest.mark.skip(reason=MCP)
def test_p83_mcp_error_is_an_is_error_result():
    raise AssertionError("not part of M3a")


@pytest.mark.skip(reason=MCP)
def test_p84_mcp_malformed_request():
    raise AssertionError("not part of M3a")


def test_p85_the_json_cli_serves_the_same_tools(scripted, tmp_path):
    from matchcut.edit.tools import TOOLS
    w = copy_ws(scripted.ws, tmp_path / "ws")
    code, log = edit_cli(w, "log")
    assert code == 0 and log["head"] == "r005" and log == json.loads(json.dumps(S.log(w)))
    code, doc = edit_cli(w, "inspect")
    assert code == 0 and doc == json.loads(json.dumps(V.inspect(w)))
    code, ref = edit_cli(w, "propose", {"ops": [{"op": "roll", "junction": 1, "frames": 1}]})
    assert code == 1 and ref["kind"] == "refused" and "held by an editor pin" in ref["error"]
    code, err = edit_cli(w, "apply", {"proposal": "0123456789ab"})
    assert code == 1 and "no proposal 0123456789ab" in err["error"]
    code, bad = edit_cli(w, "propose", {"ops": ["trim"]})
    assert code == 2 and bad["kind"] == "usage"
    code, after = edit_cli(w, "log")
    assert code == 0 and after["head"] == "r005"
    # one tool table: the CLI's --help lists exactly its commands and their schemas
    env = dict(os.environ, PYTHONPATH=str(REPO))
    helped = json.loads(subprocess.run([sys.executable, "-m", "matchcut.edit", "--help"], cwd=REPO, capture_output=True, text=True, env=env,
                                       check=True).stdout)
    assert [(c["name"], c["arguments"]) for c in helped["commands"]] == [(t.name, t.schema) for t in TOOLS]
    assert {t.name for t in TOOLS} == {"start", "inspect", "log", "propose", "apply", "undo", "redo", "checkout", "compare", "preview",
                                       "render", "frames", "publish", "pin_transition"}


# ----------------------------------------------------------------------------------------------- 13: stale
@pytest.fixture(scope="module")
def staled(built, scripted) -> SimpleNamespace:
    """A published workspace whose pipeline is run again without the pin: final.json changes."""
    w = copy_ws(scripted.ws, scripted.root / "ws-stale")
    P.publish(w)
    o: Dict[str, Any] = {"edit_fresh_before": stale_edit(w) is None}
    final = Timeline.load(w / "final.json")
    PinStore(w).remove(final.clips[0].shot_index, final.clips[1].shot_index)
    common = [built.demo, "-w", w, "--backend", "mock", "-t", "8", "--music", built.click]
    mc("refine", *common, "--no-render")
    o["final_changed"] = sha(w / "final.json") != S.load(w).state["base_sha256"]
    o["stale"] = S.load(w).stale()
    o["inspect_stale"] = V.inspect(w)["session"]["stale"]
    try:
        S.propose(w, [{"op": "slip", "clip": "c003", "frames": 1}])
        o["propose"] = None
    except Refused as exc:
        o["propose"] = str(exc)
    o["stale_edit"] = stale_edit(w)
    o["stages"] = [name for name, _ in stage_timeline_paths(w)]
    o["edit_kept"] = (w / "edit.json").exists()
    o["session_before"] = S.load(w).session_id
    o["start"] = S.start(w)
    o["dirs"] = sorted(p.name for p in w.iterdir() if p.name.startswith("edits"))
    return SimpleNamespace(ws=w, o=o)


def test_p86_the_session_sees_that_final_changed(staled):
    o = staled.o
    assert o["final_changed"] and o["stale"] and "final.json (the base stage final) changed since this session started" in o["stale"]
    assert o["inspect_stale"] is True


def test_p87_a_stale_session_refuses_to_propose(staled):
    assert staled.o["propose"] is not None and "the edit session is stale" in staled.o["propose"]
    assert "run start to begin a new session" in staled.o["propose"]


def test_p88_the_published_edit_stage_is_stale_too(staled):
    o = staled.o
    assert o["edit_fresh_before"] and o["stale_edit"] and "final.json changed since it was published" in o["stale_edit"]
    assert "edit" not in o["stages"] and o["edit_kept"]  # left out of the stage order, never deleted


def test_p89_a_new_session_moves_the_old_one_aside(staled):
    o = staled.o
    start = o["start"]
    assert start["head"] == "r000" and start["moved_aside"].startswith("edits.stale-") and start["session"] != o["session_before"]
    assert start["moved_aside"] in o["dirs"]
    kept = staled.ws / start["moved_aside"]
    assert sorted(p.name for p in (kept / "revisions").glob("*.json")) == [f"r00{k}.json" for k in range(6)]


# ----------------------------------------------------------------------------------------------- 14: determinism
def test_p90_the_same_edits_make_the_same_revisions(scripted):
    o, o2 = scripted.o, scripted.o2
    assert o2["made"] == o["made"] == ["r001", "r002", "r003", "r004", "r005"]
    assert o2["revision_sha256"] == o["revision_sha256"] and o2["proposal_ids"] == o["proposal_ids"]
    assert o2["revision_timelines"] == o["revision_timelines"]
    for rid in o["made"]:
        a = json.loads((scripted.ws / "edits" / "revisions" / f"{rid}.json").read_text())
        b = json.loads((scripted.again / "edits" / "revisions" / f"{rid}.json").read_text())
        assert {k: v for k, v in a.items() if k != "created"} == {k: v for k, v in b.items() if k != "created"}, rid


def test_p91_every_check_of_the_second_run_passes(scripted):
    for check in SCRIPT_CHECKS:
        check(scripted.o2)


# ----------------------------------------------------------------------------------------------- the mapping
def recorded_checks() -> List[Dict[str, Any]]:
    return json.loads(RECORDED.read_text())["checks"]


def test_every_prototype_check_is_mapped():
    """Every one of the recorded run's 91 checks has its id, step and text in MAPPING, in the recorded order: none is silently dropped."""
    doc = json.loads(RECORDED.read_text())
    rows = doc["checks"]
    assert len(rows) == 91 and doc["summary"]["Passed"] == 90 and doc["summary"]["Known gap (not a pass)"] == 1
    assert [(f"P{n:02d}", r["step"], r["check"]) for n, r in enumerate(rows, 1)] == [(m.id, m.step, m.check) for m in MAPPING]
    assert len({m.id for m in MAPPING}) == 91
    # the prototype's one known gap is the check this milestone closes
    [gap] = [(f"P{n:02d}", r) for n, r in enumerate(rows, 1) if r["outcome"] != "Passed"]
    assert gap[0] == "P79" and next(m for m in MAPPING if m.id == "P79").classification == CLOSED
    # each check text appears in the proof script itself (the f-string ones by their stem)
    source = (PROTOTYPE / "proof.py").read_text()
    for m in MAPPING:
        stem = m.check[:-len(" is refused")] if m.step == "2c validate" and m.check.endswith(" is refused") and m.id in (
            "P25", "P26", "P27") else m.check
        assert stem in source, m.id


def _marks(test: str) -> Tuple[Any, List[Any]]:
    """The test function a MAPPING name refers to, and the marks on that test (and on its parameter set, for ``name[id]``)."""
    name, _, param = test.partition("[")
    fn = globals().get(name)
    assert callable(fn) and name.startswith("test_"), f"{test} is not a test in this module"
    marks = list(getattr(fn, "pytestmark", []))
    if param:
        param = param.rstrip("]")
        sets = [p for m in marks if m.name == "parametrize" for p in m.args[1]]
        found = [p for p in sets if getattr(p, "id", None) == param]
        assert found, f"{test}: no parameter set {param}"
        marks = [m for m in marks if m.name != "parametrize"] + list(found[0].marks)
    return fn, [m for m in marks if m.name != "parametrize"]


def test_the_mapping_names_real_tests_with_marks_that_match_their_class():
    counts = {c: 0 for c in CLASSES}
    for m in MAPPING:
        assert m.classification in CLASSES and m.tests, m.id
        counts[m.classification] += 1
        if m.classification in (SUPERSEDED, OUT_OF_SCOPE):
            assert m.note, f"{m.id}: a {m.classification} check names its reason"
        for test in m.tests:
            _, marks = _marks(test)
            names = {x.name for x in marks}
            if m.classification == KNOWN_GAP:
                xfail = [x for x in marks if x.name == "xfail"]
                assert ("skip" in names) or (xfail and xfail[0].kwargs.get("strict") is True), f"{m.id}: a known gap is strict xfail or skip"
            elif m.classification == OUT_OF_SCOPE:
                assert "skip" in names, f"{m.id}: out of scope is skipped"
            else:
                assert not names & {"skip", "skipif", "xfail"}, f"{m.id}: a {m.classification} check must run and pass"
    assert sum(counts.values()) == 91
    assert counts == {COVERED: 75, SUPERSEDED: 10, CLOSED: 1, KNOWN_GAP: 0, OUT_OF_SCOPE: 5}, counts
