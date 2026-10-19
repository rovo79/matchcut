"""Junctions followed across stages, rescored with the match-cut scorer, and the CLI listing."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from matchcut.pipeline import cli
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.junctions import (
    _locate,
    JunctionState,
    build_junctions,
    frame_delta,
    frames_delta,
    load_stage_timelines,
    scorer_for_workspace,
    signed,
    stage_timeline_paths,
)
from matchcut.pipeline.passes import run_passes
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Decision, Timeline, Transition


def clip(cid, shot, a, b, beat="b1", **attrs):
    c = Clip(cid, shot, a, b, beat)
    for k, v in attrs.items():
        setattr(c, k, v)
    return c


def tl(stage, clips, history=()):
    t = Timeline(source="src.mp4", fps=25.0, clips=list(clips), stage=stage)
    t.history = list(history)
    return t


MATCH = Decision("matchcut", "match_cut", ["c001", "c002"], "graphic match cut (trimmed to best junction): accepted on feature score alone", 0.74, {"match_type": "graphic"})
DISSOLVE = Decision("transitions", "dissolve", ["c002", "c003"], "beat b1 -> b2: dissolve 0.30s")


def three_stages():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0), clip("c003", 2, 6.0, 8.0, "b2")])
    mc = tl("matchcut", [clip("c001", 0, 0.0, 1.88, locked_out=True), clip("c002", 1, 3.12, 5.0, locked_in=True), clip("c003", 2, 6.0, 8.0, "b2")], [MATCH])
    final = tl(
        "final",
        [clip("c001", 0, 0.0, 1.88, locked_out=True), clip("c002", 1, 3.12, 5.0, locked_in=True), clip("c003", 2, 6.0, 8.0, "b2", transition_in=Transition("dissolve", 0.3))],
        [MATCH, DISSOLVE],
    )
    return [("roughcut", rough), ("matchcut", mc), ("final", final)]


def test_a_junction_is_followed_through_every_stage():
    first, second = build_junctions(three_stages())
    assert (first.out_clip, first.in_clip, second.out_clip, second.in_clip) == ("c001", "c002", "c002", "c003")
    assert [s.stage for s in first.states] == ["roughcut", "matchcut", "final"]
    assert first.baseline.out_time == 2.0 and first.current.out_time == 1.88
    assert first.current.locked and not first.baseline.locked
    assert first.history() == ["roughcut: cut at out 2.00s / in 3.00s", "matchcut: out -0.12s (-3f), in +0.12s (+3f), locked"]
    assert first.match_type == "graphic"
    assert first.why().startswith("matchcut: graphic match cut")
    assert not first.beat_boundary and second.beat_boundary
    assert second.history()[-1] == "final: dissolve 0.30s"
    assert second.why() == "transitions: beat b1 -> b2: dissolve 0.30s"
    assert first.state_at("matchcut").out_time == 1.88 and first.state_at("nope") is None


def test_a_junction_made_by_a_reorder_starts_where_it_first_exists():
    rough = tl("roughcut", [clip("c001", 0, 0, 2), clip("c002", 1, 3, 5), clip("c003", 2, 6, 8)])
    final = tl("final", [clip("c001", 0, 0, 2), clip("c003", 2, 6, 8), clip("c002", 1, 3, 5)])
    j = build_junctions([("roughcut", rough), ("final", final)])[0]
    assert [s.stage for s in j.states] == ["final"]
    assert j.history() == ["final: cut at out 2.00s / in 6.00s"]
    assert j.why() == "made adjacent by final"


def test_upto_builds_the_junctions_of_an_earlier_stage():
    js = build_junctions(three_stages(), upto="roughcut")
    assert [(j.out_clip, j.in_clip) for j in js] == [("c001", "c002"), ("c002", "c003")]
    assert all([s.stage for s in j.states] == ["roughcut"] for j in js)
    assert build_junctions([]) == []


def test_frames_delta_formats_seconds_and_frames():
    assert frames_delta(-0.12, 25.0) == "-0.12s (-3f)"
    assert frames_delta(0.04, 25.0) == "+0.04s (+1f)"
    assert frames_delta(0.5, 0.0) == "+0.50s (+0f)"


def test_stage_layout_and_loading_skip_missing_and_corrupt_files(tmp_path):
    (tmp_path / "passes").mkdir()
    tl("roughcut", [clip("c001", 0, 0, 2)]).save(tmp_path / "roughcut.json")
    tl("matchcut", [clip("c001", 0, 0, 2)]).save(tmp_path / "passes" / "01_matchcut.json")
    (tmp_path / "passes" / "02_continuity.json").write_text("{not json")
    assert [s for s, _ in stage_timeline_paths(tmp_path)] == ["roughcut", "matchcut", "continuity", "final", "human"]
    assert dict(stage_timeline_paths(tmp_path))["human"] == tmp_path / "human.json"
    assert [s for s, _ in load_stage_timelines(tmp_path)] == ["roughcut", "matchcut"]
    tl("final", [clip("c001", 0, 0, 2)]).save(tmp_path / "final.json")
    tl("human", [clip("c001", 0, 0, 2)]).save(tmp_path / "human.json")
    assert [s for s, _ in load_stage_timelines(tmp_path)] == ["roughcut", "matchcut", "final"]  # a human.json with no import record is stale: left out
    assert scorer_for_workspace(tmp_path, PipelineConfig()) is None


def human_workspace(tmp_path):
    """roughcut, a pass, final and an imported human stage with a matching import record (what matchcut.interchange writes)."""
    (tmp_path / "passes").mkdir(exist_ok=True)
    tl("roughcut", [clip("c001", 0, 0, 2), clip("c002", 1, 4, 6)]).save(tmp_path / "roughcut.json")
    tl("pacing", [clip("c001", 0, 0, 2), clip("c002", 1, 4, 6)]).save(tmp_path / "passes" / "01_pacing.json")
    tl("final", [clip("c001", 0, 0, 2), clip("c002", 1, 4, 6)]).save(tmp_path / "final.json")
    tl("human", [clip("c001", 0, 0, 2), clip("c002", 1, 4.2, 6)]).save(tmp_path / "human.json")
    sha = lambda f: hashlib.sha256((tmp_path / f).read_bytes()).hexdigest()  # noqa: E731
    record = {"stage": "roughcut", "stage_sha256": sha("roughcut.json"), "latest_stage": "final", "latest_stage_sha256": sha("final.json")}
    (tmp_path / "human_import.json").write_text(json.dumps(record))
    return record


def test_a_human_stage_is_listed_last_until_either_stage_file_it_was_built_from_changes(tmp_path):
    from matchcut.pipeline.junctions import stale_human

    assert stale_human(tmp_path) is None  # no human.json
    human_workspace(tmp_path)
    assert stale_human(tmp_path) is None
    assert [s for s, _ in load_stage_timelines(tmp_path)] == ["roughcut", "pacing", "final", "human"]
    kept = (tmp_path / "human.json").read_bytes()
    # the latest stage (the one whose history it copies) changes, the compared one does not
    tl("final", [clip("c001", 0, 0, 2.5), clip("c002", 1, 4, 6)]).save(tmp_path / "final.json")
    assert stale_human(tmp_path) == "human.json is stale: final.json changed since the import (re-import the editor's file to see it)"
    assert [s for s, _ in stage_timeline_paths(tmp_path)][-1] == "final" and [s for s, _ in load_stage_timelines(tmp_path)] == ["roughcut", "pacing", "final"]
    assert (tmp_path / "human.json").read_bytes() == kept  # left out, never deleted
    # the compared stage changes
    human_workspace(tmp_path)
    tl("roughcut", [clip("c001", 0, 0, 2.5), clip("c002", 1, 4, 6)]).save(tmp_path / "roughcut.json")
    assert stale_human(tmp_path).startswith("human.json is stale: roughcut.json changed since the import")
    # a stage file that is gone
    human_workspace(tmp_path)
    (tmp_path / "final.json").unlink()
    assert "final.json changed since the import" in stale_human(tmp_path)


def test_a_human_json_without_a_readable_import_record_is_stale(tmp_path):
    from matchcut.pipeline.junctions import stale_human

    human_workspace(tmp_path)
    for text in (None, "{not json", "[]", json.dumps({"stage": "final"})):
        if text is None:
            (tmp_path / "human_import.json").unlink()
        else:
            (tmp_path / "human_import.json").write_text(text)
        assert stale_human(tmp_path) == "human.json is stale: human_import.json is missing or unreadable (re-import the editor's file to see it)", text
        assert [s for s, _ in load_stage_timelines(tmp_path)] == ["roughcut", "pacing", "final"]


def test_a_junction_made_adjacent_by_a_removal_names_it():
    from matchcut.pipeline.timeline import Decision as D

    three = tl("final", [clip("c001", 0, 0, 2), clip("c002", 1, 4, 6), clip("c003", 2, 8, 10)])
    human = tl("human", [clip("c001", 0, 0, 2), clip("c003", 2, 8, 10)], history=[D("human", "removed", ["c002"], "human removed c002 (shot 1, 4.00-6.00s)")])
    j = build_junctions([("final", three), ("human", human)])[0]
    assert j.why() == "made adjacent by human: removed c002 (shot 1, 4.00-6.00s)"


# -- real footage: the scores shown are the scores the pass compared ---------------------------
@pytest.fixture(scope="module")
def mc_ctx(matchcut_video, tmp_path_factory):
    cfg = PipelineConfig(source=str(matchcut_video), workspace=str(tmp_path_factory.mktemp("ws")), target_duration=6.0)
    cfg.llm.backend = "mock"
    ctx = Pipeline(cfg).narrative()
    ctx.narrative = {
        "beats": [
            {"id": "b1", "title": "A", "pace": "medium", "target_duration_s": 3.0, "shot_ids": [0, 2], "transition_in": "cut"},
            {"id": "b2", "title": "B", "pace": "fast", "target_duration_s": 3.0, "shot_ids": [3, 4, 1], "transition_in": "dissolve"},
        ]
    }
    return ctx


def designed_rough_cut(ctx) -> Timeline:
    """Same layout as tests/pipeline/test_passes.py: c001 -> c002 is the designed match cut."""
    t = Timeline(source=ctx.media.path, fps=ctx.media.fps, stage="roughcut")
    t.clips = [
        Clip("c001", 0, 0.5, 2.5, "b1"),
        Clip("c002", 2, 4.5, 6.0, "b1"),
        Clip("c003", 3, 6.0, 8.0, "b2"),
        Clip("c004", 4, 8.0, 10.0, "b2"),
        Clip("c005", 1, 2.5, 4.5, "b2"),
    ]
    return t


def test_rescored_junctions_equal_what_the_matchcut_pass_recorded(mc_ctx, tmp_path):
    rough = designed_rough_cut(mc_ctx)
    run_passes(rough, mc_ctx, ["matchcut"], save_dir=tmp_path)
    stages = [("roughcut", rough), ("matchcut", Timeline.load(tmp_path / "01_matchcut.json"))]
    scorer = scorer_for_workspace(Path(mc_ctx.cfg.workspace), mc_ctx.cfg)
    assert scorer is not None
    matched = [j for j in build_junctions(stages, scorer) if j.latest("match_cut") is not None]
    assert matched, "the fixture is designed to produce a match cut at c001 -> c002"
    for j in matched:
        recorded = j.latest("match_cut")
        assert j.current.score.combined == pytest.approx(recorded.score, abs=1e-3)
        assert j.baseline.score.combined == pytest.approx(recorded.data["baseline"], abs=1e-3)


def test_cli_lists_and_details_junctions(mc_ctx, tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "passes").mkdir(parents=True)
    shutil.copy(Path(mc_ctx.cfg.workspace) / "analysis.npz", ws / "analysis.npz")
    mc_ctx.cfg.save(ws / "config.json")
    rough = designed_rough_cut(mc_ctx)
    rough.save(ws / "roughcut.json")
    run_passes(rough, mc_ctx, ["matchcut"], save_dir=ws / "passes")
    assert cli.main(["junctions", str(ws)]) == 0
    listing = capsys.readouterr().out
    assert "4 junctions in matchcut (2 stages)" in listing and "match:" in listing
    assert cli.main(["junctions", str(ws), "--index", "1"]) == 0
    detail = capsys.readouterr().out
    assert "why: matchcut:" in detail and "combined" in detail and "history:" in detail
    assert cli.main(["junctions", str(ws), "--index", "9"]) == 1
    assert cli.main(["junctions", str(tmp_path / "nope")]) == 1


# -- passes renumber, drop and trim clips: junctions follow lineage, not ids -------------------
def test_a_renumbering_pass_does_not_attach_the_wrong_earlier_clips():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 3.4), clip("c003", 2, 6.0, 8.0)])
    drop = Decision("continuity", "drop", ["c002"], "too short")
    cont = tl("continuity", [clip("c001", 0, 0.0, 2.0), clip("c002", 2, 6.0, 8.0)], list(rough.history) + [drop])
    js = build_junctions([("roughcut", rough), ("continuity", cont)])
    assert len(js) == 1
    j = js[0]
    assert (j.out_clip, j.in_clip, j.out_shot, j.in_shot) == ("c001", "c002", 0, 2)
    assert [s.stage for s in j.states] == ["continuity"]
    assert j.baseline.in_time == 6.0 and j.history() == ["continuity: cut at out 2.00s / in 6.00s"]
    assert drop not in j.decisions  # c002 in the drop is the rough cut's shot-1 clip, not this junction's in-clip
    assert j.why() == "made adjacent by continuity: too short"  # but it is the clip that sat between the two


def test_a_junction_survives_renumbering_and_keeps_its_own_decisions():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 3.4), clip("c003", 2, 6.0, 8.0), clip("c004", 3, 9.0, 11.0)])
    drop = Decision("continuity", "drop", ["c002"], "too short")
    fix = Decision("continuity", "match_cut", ["c003", "c004"], "kept", 0.5)
    cont = tl("continuity", [clip("c001", 0, 0.0, 2.0), clip("c002", 2, 6.0, 8.0), clip("c003", 3, 9.0, 11.0)], [drop, fix])
    js = build_junctions([("roughcut", rough), ("continuity", cont)])
    last = js[1]  # shot 2 -> shot 3, now c002 -> c003
    assert (last.out_clip, last.in_clip) == ("c002", "c003")
    assert [s.stage for s in last.states] == ["roughcut", "continuity"]
    assert last.baseline.out_time == 8.0 and last.baseline.in_time == 9.0
    assert last.decisions == [fix]  # recorded in rough-cut numbering, attributed through it
    assert drop not in js[0].decisions


def test_a_single_clip_trim_is_clip_level_and_explains_the_junction_when_nothing_else_does():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    trim = Decision("pacing", "trim", ["c001"], "trimmed 0.20s of dead air")
    paced = tl("pacing", [clip("c001", 0, 0.0, 1.8), clip("c002", 1, 3.0, 5.0)], [trim])
    j = build_junctions([("roughcut", rough), ("pacing", paced)])[0]
    assert j.decisions == [trim] and j.clip_level == [True]
    assert j.why() == "pacing: trimmed 0.20s of dead air"
    assert j.history()[-1] == "pacing: out -0.20s (-5f)"


def test_a_junction_level_decision_outranks_a_clip_level_one():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    both = [Decision("transitions", "dissolve", ["c001", "c002"], "dissolve 0.30s"), Decision("pacing", "trim", ["c001"], "trimmed")]
    final = tl("final", [clip("c001", 0, 0.0, 1.8), clip("c002", 1, 3.0, 5.0)], both)
    j = build_junctions([("roughcut", rough), ("final", final)])[0]
    assert j.clip_level == [False, True]
    assert j.why() == "transitions: dissolve 0.30s"


def test_whole_timeline_decisions_are_not_attributed_to_a_junction():
    clips = lambda: [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0), clip("c003", 2, 6.0, 8.0), clip("c004", 3, 9.0, 11.0)]
    rough = tl("roughcut", clips())
    polished = tl("polish", clips(), [Decision("polish", "assessment", ["c001", "c002", "c003", "c004"], "overall: fine")])
    js = build_junctions([("roughcut", rough), ("polish", polished)])
    assert len(js) == 3
    assert all(j.decisions == [] and j.why() == "no pass changed this junction" for j in js)


def test_a_beat_fit_is_attributed_to_none_of_the_beats_junctions():
    clips = lambda: [clip("c%03d" % n, n, n * 3.0, n * 3.0 + 2.0) for n in range(1, 6)]
    rough = tl("roughcut", clips())
    fit = Decision("pacing", "fit", ["c003", "c004", "c005"], "beat b2 fitted to 5.0s")
    paced = tl("pacing", clips(), [fit])
    js = build_junctions([("roughcut", rough), ("pacing", paced)])
    assert [(j.out_clip, j.in_clip) for j in js][2:] == [("c003", "c004"), ("c004", "c005")]
    assert all(fit not in j.decisions for j in js)


def test_a_polish_refusal_on_a_clip_does_not_outrank_the_match_cut_decision():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    mc = tl("matchcut", [clip("c001", 0, 0.0, 1.88, locked_out=True), clip("c002", 1, 3.12, 5.0, locked_in=True)], [MATCH])
    refusal = Decision("polish", "rejected", ["c001"], "trim_tail: out-point locked by a match cut")
    polished = tl("polish", [clip("c001", 0, 0.0, 1.88, locked_out=True), clip("c002", 1, 3.12, 5.0, locked_in=True)], [MATCH, refusal])
    j = build_junctions([("roughcut", rough), ("matchcut", mc), ("polish", polished)])[0]
    assert j.why().startswith("matchcut:")
    assert refusal in j.decisions and j.clip_level == [False, True]


def test_cli_reports_a_corrupt_config(tmp_path, capsys):
    (tmp_path / "config.json").write_text("{not json")
    assert cli.main(["junctions", str(tmp_path)]) == 1
    assert "config.json" in capsys.readouterr().err


def test_junctions_through_every_default_pass_keep_their_lineage(mc_ctx, tmp_path):
    rough = designed_rough_cut(mc_ctx)
    run_passes(rough, mc_ctx, ["matchcut", "continuity", "pacing", "transitions", "polish"], save_dir=tmp_path)
    stages = [("roughcut", designed_rough_cut(mc_ctx))] + [(p.stem.split("_", 1)[-1], Timeline.load(p)) for p in sorted(tmp_path.glob("*.json"))]
    assert len(stages) >= 4
    by_stage = dict(stages)
    final = stages[-1][1]
    js = build_junctions(stages)
    assert len(js) == len(final.clips) - 1
    for j in js:
        assert j.states and j.current.stage == stages[-1][0]
        for state in j.states:
            t = by_stage[state.stage]
            i = _locate(t, final.clips[j.index], final.clips[j.index + 1])
            assert i is not None
            assert (state.out_time, state.in_time) == (t.clips[i].source_out, t.clips[i + 1].source_in)
            for got, ref in ((t.clips[i], final.clips[j.index]), (t.clips[i + 1], final.clips[j.index + 1])):
                assert got.shot_index == ref.shot_index
                assert max(got.source_in, ref.source_in) < min(got.source_out, ref.source_out)
    match = next(j for j in js if (j.out_shot, j.in_shot) == (0, 2))
    assert match.latest("match_cut") is not None
    assert match.why().startswith("matchcut:")


# -- C1: why() never claims "no pass changed this junction" when one did --------------------------
NOTHING = "no pass changed this junction"


def run_stages(ctx, tmp_path, beat_targets=(3.0, 3.0), total=6.0, monkeypatch=None):
    """The default passes over the designed rough cut with a mock backend, as (stage, timeline) pairs."""
    monkeypatch.setattr(ctx.cfg, "target_duration", total)
    narrative = {"beats": [dict(b, target_duration_s=t) for b, t in zip(ctx.narrative["beats"], beat_targets)]}
    monkeypatch.setattr(ctx, "narrative", narrative)
    rough = designed_rough_cut(ctx)
    run_passes(rough, ctx, ["matchcut", "continuity", "pacing", "transitions", "polish"], save_dir=tmp_path)
    return [("roughcut", designed_rough_cut(ctx))] + [(p.stem.split("_", 1)[-1], Timeline.load(p)) for p in sorted(tmp_path.glob("*.json"))]


def test_real_default_passes_never_say_nothing_changed_for_a_moved_junction(mc_ctx, tmp_path, monkeypatch):
    js = build_junctions(run_stages(mc_ctx, tmp_path, monkeypatch=monkeypatch))
    moved = [j for j in js if len(j.history()) > 1]
    assert moved
    assert all(NOTHING not in j.why() for j in moved)
    pacing_moved = [j for j in js if any(line.startswith("pacing:") for line in j.history())]
    assert any("pacing" in j.why() for j in pacing_moved)
    # a junction only pacing touched (no match cut, no transition) is explained by pacing
    only_pacing = [j for j in pacing_moved if not any(h.startswith(("matchcut:", "transitions:")) for h in j.history())]
    assert only_pacing and all(j.why().startswith("pacing:") for j in only_pacing)


def test_a_junction_made_by_a_drop_names_the_stage_that_made_it_adjacent(mc_ctx, tmp_path, monkeypatch):
    stages = run_stages(mc_ctx, tmp_path, beat_targets=(1.5, 2.0), total=3.5, monkeypatch=monkeypatch)
    js = build_junctions(stages)
    made = [j for j in js if j.baseline.stage != "roughcut"]
    assert made, "tightening the target should drop a clip and join its neighbours"
    for j in made:
        assert NOTHING not in j.why()
    assert all(j.first_stage == "roughcut" for j in js)
    assert any(j.baseline.stage == "pacing" for j in made)


def test_why_for_a_junction_with_history_but_no_attributed_decision_names_the_last_moving_stage():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    paced = tl("pacing", [clip("c001", 0, 0.0, 1.08), clip("c002", 1, 3.0, 5.0)], [Decision("pacing", "fit", ["c001", "c002"], "beat b1 fitted")])
    final = tl("final", [clip("c001", 0, 0.0, 1.08), clip("c002", 1, 3.0, 5.0)])
    j = build_junctions([("roughcut", rough), ("pacing", paced), ("final", final)])[0]
    assert j.decisions == []
    assert j.why() == "pacing moved this junction: out -0.92s (-23f)"


def test_why_for_a_junction_a_drop_made_adjacent_names_the_dropping_stage_and_its_detail():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0), clip("c003", 2, 6.0, 8.0)])
    drop = Decision("pacing", "drop", ["c002"], "total: over target, dropped lowest-value clip")
    paced = tl("pacing", [clip("c001", 0, 0.0, 2.0), clip("c002", 2, 6.0, 8.0)], [drop])
    j = build_junctions([("roughcut", rough), ("pacing", paced)])[0]
    assert j.baseline.stage == "pacing" and j.first_stage == "roughcut"
    assert j.why() == "made adjacent by pacing: total: over target, dropped lowest-value clip"
    bare = tl("pacing", [clip("c001", 0, 0.0, 2.0), clip("c002", 2, 6.0, 8.0)])
    assert build_junctions([("roughcut", rough), ("pacing", bare)])[0].why() == "made adjacent by pacing"


def test_a_junction_made_adjacent_by_the_editors_pin_move_names_the_move():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0), clip("c003", 2, 6.0, 8.0)])
    move = Decision("editor", "pin_moved", ["c003", "c001"], "moved shot 2 next to shot 0 to hold the editor's pin")
    moved = tl("matchcut", [clip("c001", 0, 0.0, 2.0), clip("c003", 2, 6.0, 8.0), clip("c002", 1, 3.0, 5.0)], [move])
    j = build_junctions([("roughcut", rough), ("matchcut", moved)])[0]
    assert j.adjacent_detail == "moved shot 2 next to shot 0 to hold the editor's pin"
    assert j.why() == "editor: moved shot 2 next to shot 0 to hold the editor's pin"  # the move is the editor's decision about this junction


def test_nothing_changed_is_only_said_for_one_history_line_from_the_first_stage():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    j = build_junctions([("roughcut", rough), ("final", tl("final", rough.clips))])[0]
    assert len(j.history()) == 1 and j.why() == NOTHING


def test_pacing_records_a_trim_for_every_clip_the_proportional_fit_shortened(mc_ctx, tmp_path, monkeypatch):
    stages = run_stages(mc_ctx, tmp_path, monkeypatch=monkeypatch)
    paced = dict(stages)["pacing"]
    fits = [d for d in paced.history if d.pass_name == "pacing" and d.action == "trim" and "fitted to target" in d.detail]
    assert fits and all(len(d.clip_ids) == 1 and d.detail.split(", -")[-1].endswith("s") for d in fits)


# -- I2: snapping noise below half a frame is not a change ---------------------------------------
def state(out, inn, **kw):
    return JunctionState("x", out, inn, **kw)


def test_frame_delta_counts_frames_on_the_grid_not_the_rounded_seconds():
    assert frame_delta(2.0, 1.88, 25.0) == "-0.12s (-3f)"
    assert frame_delta(4.25, 4.24, 25.0) == "-0.01s (+0f)"  # both ends round to frame 106
    assert frame_delta(1.019, 1.021, 25.0) == "+0.00s (+1f)"  # a hair apart, but across a frame boundary
    assert frame_delta(1.03, 1.05, 25.0) == "+0.02s (+0f)"  # frames 26 and 26: the grid, not the rounded seconds, decides
    assert frame_delta(0.0, 0.5, 0.0) == "+0.50s (+0f)"


def test_change_from_ignores_differences_under_half_a_frame():
    assert state(2.004, 4.2).change_from(state(2.0, 4.2), 25.0) == ""
    assert state(2.0, 4.19).change_from(state(2.0, 4.2), 25.0) == ""  # 0.01s < 0.02s
    assert state(2.0, 4.18).change_from(state(2.0, 4.2), 25.0) == "in -0.02s (-1f)"  # half a frame is a change
    assert state(2.0, 4.2).change_from(state(2.0, 4.21), 0.0) == ""  # no fps: 0.02s threshold
    assert state(2.0, 4.2, transition="dissolve", transition_seconds=0.3).change_from(state(2.004, 4.2), 25.0) == "dissolve 0.30s"


def test_a_real_run_has_no_history_line_that_only_snaps_to_the_grid(mc_ctx, tmp_path, monkeypatch):
    js = build_junctions(run_stages(mc_ctx, tmp_path, monkeypatch=monkeypatch))
    lines = [line for j in js for line in j.history()[1:]]
    assert lines and not any("(+0f)" in line for line in lines)


def test_a_real_workspace_run_has_no_snapping_noise_in_its_histories(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()
    js = build_junctions(load_stage_timelines(Path(cfg.workspace)))
    assert js and not any("(+0f)" in line for j in js for line in j.history()[1:])


# -- I5: the pin's effect is the editor's, not the first pass's ----------------------------------
def pinned_stages():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    pinned = Decision("editor", "pinned", ["c001", "c002"], "locked by the editor: out 1.88s, in 3.12s", None, {"out_time": 1.88, "in_time": 3.12})
    mc = tl("matchcut", [clip("c001", 0, 0.0, 1.88, locked_out=True), clip("c002", 1, 3.12, 5.0, locked_in=True)], [pinned])
    final = tl("final", [clip("c001", 0, 0.0, 1.88, locked_out=True), clip("c002", 1, 3.12, 5.0, locked_in=True)], [pinned])
    return [("roughcut", rough), ("matchcut", mc), ("final", final)]


def test_the_pins_effect_is_labelled_as_the_editors_not_the_first_passes():
    j = build_junctions(pinned_stages())[0]
    assert j.history() == ["roughcut: cut at out 2.00s / in 3.00s", "editor (before matchcut): out -0.12s (-3f), in +0.12s (+3f), locked"]
    assert j.why().startswith("editor:")


def test_a_pass_that_moves_a_pinned_junction_later_keeps_its_own_label():
    stages = pinned_stages()
    moved = tl("final", [clip("c001", 0, 0.0, 1.5, locked_out=True), clip("c002", 1, 3.12, 5.0, locked_in=True)], stages[2][1].history)
    j = build_junctions(stages[:2] + [("final", moved)])[0]
    assert j.history()[-1].startswith("final: out -0.38s")
    assert j.history()[1].startswith("editor (before matchcut):")


def test_signed_never_prints_negative_zero():
    assert signed(-0.001) == "+0.00"
    assert signed(-0.004) == "+0.00"
    assert signed(0.0) == "+0.00"
    assert signed(-0.006) == "-0.01"
    assert signed(0.25) == "+0.25"
    assert signed(-1.5, 1) == "-1.5"


def test_frame_delta_of_a_tiny_negative_move_is_plus_zero():
    assert frame_delta(16.21, 16.2099, 25.0) == "+0.00s (+0f)"
    assert frames_delta(-0.0001, 25.0) == "+0.00s (+0f)"


def _pinned_stages(origin):
    """roughcut -> matchcut where the editor pinned c001/c002 before the matchcut pass."""
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    pin = Decision("editor", "pinned", ["c001", "c002"], f"{origin} by the editor: out 2.00s, in 3.00s", None,
                   {"out_shot": 0, "in_shot": 1, "out_time": 2.0, "in_time": 3.0, "origin": origin, "note": "", "out": 2.0, "in_": 3.0})
    passed = tl("matchcut", [clip("c001", 0, 0.0, 2.0, locked_out=True), clip("c002", 1, 3.0, 5.0, locked_in=True)], history=[pin])
    return [("roughcut", rough), ("matchcut", passed)]


def test_a_reverted_pin_reads_reverted_in_the_history_and_the_origin():
    j = build_junctions(_pinned_stages("reverted"))[0]
    assert j.pin_origin == "reverted"
    assert j.history()[-1] == "editor (before matchcut): reverted"


def test_a_locked_pin_still_reads_locked():
    j = build_junctions(_pinned_stages("locked"))[0]
    assert j.pin_origin == "locked"
    assert j.history()[-1] == "editor (before matchcut): locked"


def test_a_junction_without_a_pin_has_no_origin():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    assert build_junctions([("roughcut", rough)])[0].pin_origin is None


def test_a_pin_decision_taken_after_a_move_belongs_to_the_junction_the_move_made():
    # The pin was applied by moving c003 next to c001 before matchcut ran: the editor's decisions name rough-cut ids,
    # and the two shots were not adjacent in the rough cut, so only the clips' lineage can attribute them.
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0), clip("c003", 2, 6.0, 8.0)])
    move = Decision("editor", "pin_moved", ["c003", "c001"], "moved shot 2 next to shot 0 to hold the editor's pin", None, {"out_shot": 0, "in_shot": 2})
    pinned = Decision("editor", "pinned", ["c001", "c003"], "locked by the editor: out 1.80s, in 6.20s", None,
                      {"out_shot": 0, "in_shot": 2, "out_time": 1.8, "in_time": 6.2, "origin": "locked", "note": "", "out": 1.8, "in_": 6.2})
    passed = tl("matchcut", [clip("c001", 0, 0.0, 1.8, locked_out=True), clip("c003", 2, 6.2, 8.0, locked_in=True), clip("c002", 1, 3.0, 5.0)], [move, pinned])
    junctions = build_junctions([("roughcut", rough), ("matchcut", passed)])
    j = junctions[0]
    assert (j.out_shot, j.in_shot) == (0, 2)
    assert j.pinned and j.pin_origin == "locked"
    assert j.why().startswith("editor:")
    assert not junctions[1].pinned  # the junction the moved clip left behind is not the pin's


def test_a_trim_in_the_stage_that_dropped_the_clip_between_does_not_replace_the_adjacency_story():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0), clip("c003", 2, 6.0, 8.0)])
    drop = Decision("pacing", "drop", ["c002"], "total: over target, dropped lowest-value clip")
    trim = Decision("pacing", "trim", ["c001"], "total: fitted to target, -0.50s")
    paced = tl("pacing", [clip("c001", 0, 0.0, 1.5), clip("c002", 2, 6.0, 8.0)], [drop, trim])
    j = build_junctions([("roughcut", rough), ("pacing", paced)])[0]
    assert j.why() == "made adjacent by pacing: total: over target, dropped lowest-value clip"
    assert not j.decisions  # neither the drop nor the trim concerns the junction when the clips were apart before


# -- junctions a pass created are attributed by the lineage of their clips (intended behaviour) -----
def test_a_continuity_insert_junction_reads_as_the_insert_that_made_it():
    # c003 was moved between c001 and c002: the insert names all three clips, so it is the junction's own decision
    # (not just "made adjacent by continuity"), for both junctions the insert created.
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 0, 2.2, 4.0), clip("c003", 2, 6.0, 8.0)])
    insert = Decision("continuity", "insert", ["c001", "c003", "c002"], "moved c003 between c001 and c002 to break a jump cut")
    cont = tl("continuity", [clip("c001", 0, 0.0, 2.0), clip("c003", 2, 6.0, 8.0), clip("c002", 0, 2.2, 4.0)], [insert])
    first, second = build_junctions([("roughcut", rough), ("continuity", cont)])
    for j in (first, second):
        assert j.baseline.stage == "continuity"
        assert j.decisions == [insert]
        assert j.why() == "continuity: moved c003 between c001 and c002 to break a jump cut"


def test_a_reorder_made_junction_gets_the_match_cut_and_rejected_verdicts_about_it():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0), clip("c003", 2, 6.0, 8.0)])
    match = Decision("matchcut", "match_cut", ["c001", "c003"], "graphic match cut: accepted on feature score alone", 0.74, {"match_type": "graphic"})
    rejected = Decision("matchcut", "rejected", ["c003", "c002"], "no match: scores below the threshold", 0.31)
    reordered = tl("matchcut", [clip("c001", 0, 0.0, 2.0), clip("c003", 2, 6.0, 8.0), clip("c002", 1, 3.0, 5.0)], [match, rejected])
    first, second = build_junctions([("roughcut", rough), ("matchcut", reordered)])
    assert first.decisions == [match]
    assert first.why() == "matchcut: graphic match cut: accepted on feature score alone"
    assert second.decisions == [rejected]
    assert second.why() == "matchcut: no match: scores below the threshold"
