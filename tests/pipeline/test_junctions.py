"""Junctions followed across stages, rescored with the match-cut scorer, and the CLI listing."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from matchcut.pipeline import cli
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.junctions import (
    _locate,
    build_junctions,
    frames_delta,
    load_stage_timelines,
    scorer_for_workspace,
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
    assert j.why() == "no pass changed this junction"


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
    assert [s for s, _ in stage_timeline_paths(tmp_path)] == ["roughcut", "matchcut", "continuity", "final"]
    assert [s for s, _ in load_stage_timelines(tmp_path)] == ["roughcut", "matchcut"]
    assert scorer_for_workspace(tmp_path, PipelineConfig()) is None


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
    assert j.why() == "no pass changed this junction"


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
