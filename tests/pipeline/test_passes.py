from pathlib import Path

import pytest

from matchcut.pipeline import schemas
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.llm import MockBackend
from matchcut.pipeline.passes import ContinuityPass, MatchCutPass, PacingPass, PolishPass, TransitionsPass, run_passes
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Timeline, Transition


@pytest.fixture(scope="module")
def ctx(matchcut_video, tmp_path_factory):
    cfg = PipelineConfig(source=str(matchcut_video), workspace=str(tmp_path_factory.mktemp("ws")), target_duration=6.0)
    cfg.llm.backend = "mock"
    pipe = Pipeline(cfg)
    ctx = pipe.narrative()
    ctx.narrative = {
        "beats": [
            {"id": "b1", "title": "A", "pace": "medium", "target_duration_s": 3.0, "shot_ids": [0, 2], "transition_in": "cut"},
            {"id": "b2", "title": "B", "pace": "fast", "target_duration_s": 3.0, "shot_ids": [3, 4, 1], "transition_in": "dissolve"},
        ]
    }
    return ctx


def base_timeline(ctx) -> Timeline:
    tl = Timeline(source=ctx.media.path, fps=ctx.media.fps)
    tl.clips = [
        Clip("c001", 0, 0.5, 2.5, "b1"),  # ends where shot 2 picks the motion up
        Clip("c002", 2, 4.5, 6.0, "b1"),
        Clip("c003", 3, 6.0, 8.0, "b2"),
        Clip("c004", 4, 8.0, 10.0, "b2"),
        Clip("c005", 1, 2.5, 4.5, "b2"),
    ]
    return tl


def test_matchcut_pass_finds_and_locks_the_designed_match(ctx):
    result = MatchCutPass().apply(base_timeline(ctx), ctx)
    matches = [d for d in result.decisions if d.action == "match_cut"]
    assert any(d.clip_ids == ["c001", "c002"] for d in matches)
    a, b = result.timeline.clips[0], result.timeline.clips[1]
    assert a.locked_out and b.locked_in
    assert any(t.startswith("match_out:") for t in a.tags)
    assert 0.5 + 0.75 <= a.source_out <= 2.5  # trimmed within slack, above min clip
    assert result.timeline.validate(ctx.shot_bounds) == []


def test_matchcut_pass_can_reorder_a_beat_to_create_a_match(ctx):
    tl = Timeline(source=ctx.media.path, fps=ctx.media.fps)
    tl.clips = [  # shot 0 then filler then shot 2: swapping c002/c003 puts the match together
        Clip("c001", 0, 0.5, 2.5, "b1"),
        Clip("c002", 1, 2.5, 4.5, "b1"),
        Clip("c003", 2, 4.5, 6.0, "b1"),
    ]
    result = MatchCutPass().apply(tl, ctx)
    assert any(d.action == "reorder" for d in result.decisions)
    assert [c.shot_index for c in result.timeline.clips] == [0, 2, 1]


def test_continuity_merges_and_breaks_jump_cuts(ctx):
    tl = Timeline(source=ctx.media.path, fps=ctx.media.fps)
    tl.clips = [
        Clip("c001", 3, 6.0, 6.8, "b2"),
        Clip("c002", 3, 6.8, 7.4, "b2"),  # contiguous: merge
        Clip("c003", 3, 7.6, 8.0, "b2"),  # same shot, gap: jump cut -> flash frame dropped first
        Clip("c004", 4, 8.0, 9.0, "b2"),
        Clip("c005", 1, 2.5, 3.5, "b2"),
    ]
    result = ContinuityPass().apply(tl, ctx)
    actions = [d.action for d in result.decisions]
    assert "merge" in actions
    assert "drop" in actions  # the 0.4 s piece
    assert result.timeline.clips[0].source_out == 7.4
    assert len(result.timeline.clips) == 3


def test_continuity_inserts_a_separator_between_same_shot_clips(ctx):
    tl = Timeline(source=ctx.media.path, fps=ctx.media.fps)
    tl.clips = [
        Clip("c001", 3, 6.0, 6.9, "b2"),
        Clip("c002", 3, 7.1, 8.0, "b2"),
        Clip("c003", 4, 8.0, 9.0, "b2"),
    ]
    result = ContinuityPass().apply(tl, ctx)
    assert [c.shot_index for c in result.timeline.clips] == [3, 4, 3]
    assert any(d.action == "insert" for d in result.decisions)


def test_pacing_hits_the_target_and_respects_locks(ctx):
    tl = base_timeline(ctx)
    tl.clips[0].locked_out = True
    tl.clips[1].locked_in = True
    before_out, before_in = tl.clips[0].source_out, tl.clips[1].source_in
    result = PacingPass().apply(tl, ctx)
    final = result.timeline
    assert abs(final.duration - 6.0) <= 6.0 * ctx.cfg.pacing.duration_tolerance + 0.3
    a = next(c for c in final.clips if c.shot_index == 0)
    b = next(c for c in final.clips if c.shot_index == 2)
    assert a.source_out == before_out and b.source_in == before_in
    assert all(c.duration >= ctx.cfg.pacing.min_clip_seconds - 1e-6 for c in final.clips)


def test_transitions_follow_beats_but_keep_match_cuts_hard(ctx):
    tl = base_timeline(ctx)
    result = TransitionsPass().apply(tl, ctx)
    c3 = result.timeline.clips[2]
    assert c3.transition_in.kind == "dissolve" and c3.transition_in.duration > 0
    assert result.timeline.fade_in > 0 and result.timeline.fade_out > 0
    tl2 = base_timeline(ctx)
    tl2.clips[1].locked_out = True
    tl2.clips[2].locked_in = True
    tl2.clips[1].tags.append("match_out:motion")  # the match-cut pass tags both ends; a bare lock (an editor's pin) keeps the beat transition
    tl2.clips[2].tags.append("match_in:motion")
    result2 = TransitionsPass().apply(tl2, ctx)
    assert result2.timeline.clips[2].transition_in.is_cut


class ScriptedPolish(MockBackend):
    def _polish(self, payload):
        return {
            "assessment": "tighten the opening, lose the filler",
            "edits": [
                {"op": "trim_head", "clip_id": "c001", "seconds": 0.5, "target_clip_id": "", "transition": "cut", "reason": "late start"},
                {"op": "drop", "clip_id": "c005", "seconds": 0, "target_clip_id": "", "transition": "cut", "reason": "filler"},
                {"op": "trim_tail", "clip_id": "c001", "seconds": 0.3, "target_clip_id": "", "transition": "cut", "reason": "locked, must be rejected"},
                {"op": "set_transition", "clip_id": "c003", "seconds": 0, "target_clip_id": "", "transition": "dip_to_black", "reason": "beat change"},
                {"op": "move_before", "clip_id": "c004", "seconds": 0, "target_clip_id": "c003", "transition": "cut", "reason": "swap"},
            ],
        }


def test_polish_applies_valid_edits_and_rejects_locked_ones(ctx):
    tl = base_timeline(ctx)
    tl.clips[0].locked_out = True
    tl.clips[1].locked_in = True
    original_llm = ctx.llm
    ctx.llm = ScriptedPolish(ctx.cfg.llm)
    try:
        result = PolishPass().apply(tl, ctx)
    finally:
        ctx.llm = original_llm
    actions = {(d.action, tuple(d.clip_ids)) for d in result.decisions}
    assert ("trim_head", ("c001",)) in actions
    assert ("drop", ("c005",)) in actions
    assert ("rejected", ("c001",)) in actions
    assert ("set_transition", ("c003",)) in actions
    assert ("move_before", ("c004",)) in actions
    final = result.timeline
    assert len(final.clips) == 4
    assert [c.shot_index for c in final.clips] == [0, 2, 4, 3]
    assert final.clips[0].source_in == 1.0 and final.clips[0].source_out == 2.5
    assert final.validate(ctx.shot_bounds) == []


def test_run_passes_end_to_end_saves_each_stage(ctx, tmp_path):
    final = run_passes(base_timeline(ctx), ctx, ["matchcut", "continuity", "pacing", "transitions", "polish"], save_dir=tmp_path)
    assert sorted(p.name for p in tmp_path.glob("*.json")) == [
        "01_matchcut.json",
        "02_continuity.json",
        "03_pacing.json",
        "04_transitions.json",
        "05_polish.json",
    ]
    assert final.validate(ctx.shot_bounds) == []
    assert any(d.action == "match_cut" for d in final.history)
