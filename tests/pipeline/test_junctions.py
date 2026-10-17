"""Junctions followed across stages, rescored with the match-cut scorer, and the CLI listing."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from matchcut.pipeline import cli
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.junctions import (
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
