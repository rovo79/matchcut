import numpy as np
import pytest

from matchcut.pipeline.audio import analyse_music
from matchcut.pipeline.config import MusicConfig, PipelineConfig
from matchcut.pipeline.passes import BeatSyncPass
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Timeline


@pytest.fixture(scope="module")
def ctx(matchcut_video, click_track, tmp_path_factory):
    cfg = PipelineConfig(source=str(matchcut_video), workspace=str(tmp_path_factory.mktemp("ws")))
    cfg.llm.backend = "mock"
    cfg.music.path = str(click_track)
    pipe = Pipeline(cfg)
    ctx = pipe.narrative()
    ctx.narrative = {"beats": [{"id": "b1", "pace": "medium", "target_duration_s": 4, "shot_ids": [0, 1, 3], "transition_in": "cut"}, {"id": "b2", "pace": "medium", "target_duration_s": 2, "shot_ids": [4], "transition_in": "cut"}]}
    return ctx


def off_grid(t: float, grid: float = 0.5) -> float:
    return abs(t - round(t / grid) * grid)


def make_tl(ctx) -> Timeline:
    tl = Timeline(source=ctx.media.path, fps=ctx.media.fps)
    tl.clips = [
        Clip("c001", 0, 0.5, 1.8, "b1"),  # junction 1 at 1.3 -> beat 1.5 (+0.2): c001 out -> 2.0
        Clip("c002", 1, 2.6, 3.7, "b1"),  # junction 2 at 1.5+1.1=2.6 -> beat 2.5 (-0.1): c002 out -> 3.6
        Clip("c003", 3, 6.2, 7.4, "b1"),  # junction 3 at 2.5+1.2=3.7, beat change -> downbeat 4.0 (+0.3): c003 out -> 7.7
        Clip("c004", 4, 8.3, 9.3, "b2"),
    ]
    return tl


def test_cuts_land_on_beats(ctx):
    tl = make_tl(ctx)
    result = BeatSyncPass().apply(tl, ctx)
    out = result.timeline
    for i in range(1, len(out.clips)):
        assert off_grid(out.record_start(i)) < 0.07, (i, out.record_start(i))
    snaps = [d for d in result.decisions if d.action == "snap"]
    assert len(snaps) >= 2
    assert all(abs(d.data["delta"]) <= ctx.cfg.music.snap_window + 1e-6 for d in snaps)
    assert out.validate(ctx.shot_bounds) == []
    # input untouched
    assert tl.clips[0].source_out == 1.8


def test_beatsync_output_is_frame_aligned(ctx):
    out = BeatSyncPass().apply(make_tl(ctx), ctx).timeline
    again = out.snap_to_frames(ctx.shot_bounds)
    assert [(c.source_in, c.source_out) for c in again.clips] == [(c.source_in, c.source_out) for c in out.clips]
    fps = out.fps
    for d in out.history:
        if d.pass_name == "beatsync" and d.action == "snap":
            assert abs(d.data["delta"] * fps - round(d.data["delta"] * fps)) < 1e-6  # whole frames


def test_beat_change_prefers_downbeats(ctx):
    tl = make_tl(ctx)
    result = BeatSyncPass().apply(tl, ctx)
    out = result.timeline
    j = out.record_start(3) + ctx.cfg.music.start  # junction into beat b2, in track time
    assert ctx.music.nearest_beat(j, downbeats=True) is not None
    assert abs(ctx.music.nearest_beat(j, downbeats=True) - j) < 0.07
    d = next(d for d in result.decisions if d.clip_ids == ["c003", "c004"])
    assert d.action in ("snap", "on_beat")
    if d.action == "snap":
        assert d.data["downbeat"] is True


def test_locked_out_point_moves_the_in_point_instead(ctx):
    tl = make_tl(ctx)
    tl.clips[0].locked_out = True
    before_out = tl.clips[0].source_out
    result = BeatSyncPass().apply(tl, ctx)
    c = result.timeline.clips[0]
    assert c.source_out == before_out
    assert c.source_in != 0.5  # head trimmed/extended to move the junction
    assert off_grid(result.timeline.record_start(1)) < 0.07


def test_fully_locked_clip_is_left_alone(ctx):
    tl = make_tl(ctx)
    tl.clips[0].locked_out = True
    tl.clips[0].locked_in = True
    result = BeatSyncPass().apply(tl, ctx)
    c = result.timeline.clips[0]
    expected = make_tl(ctx).snap_to_frames(ctx.shot_bounds).clips[0]
    assert (c.source_in, c.source_out) == (expected.source_in, expected.source_out)
    assert any(d.action == "locked" and d.clip_ids == ["c001", "c002"] for d in result.decisions)


def test_no_music_is_a_noop(ctx):
    music, ctx.music = ctx.music, None
    try:
        tl = make_tl(ctx)
        result = BeatSyncPass().apply(tl, ctx)
        assert result.decisions == []
        assert result.timeline.to_dict()["clips"] == tl.to_dict()["clips"]
    finally:
        ctx.music = music


def test_snap_disabled_is_a_noop(ctx):
    ctx.cfg.music.snap = False
    try:
        result = BeatSyncPass().apply(make_tl(ctx), ctx)
        assert result.decisions == []
    finally:
        ctx.cfg.music.snap = True


def test_music_offset_shifts_the_grid(ctx):
    ctx.cfg.music.start = 0.25  # beats now fall at 0.25, 0.75, ... on the timeline
    try:
        out = BeatSyncPass().apply(make_tl(ctx), ctx).timeline
        for i in range(1, len(out.clips)):
            assert off_grid(out.record_start(i) + 0.25) < 0.07
    finally:
        ctx.cfg.music.start = 0.0
