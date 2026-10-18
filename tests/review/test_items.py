"""Review items: exact EDL windows, rendered transitions, the cache, A/B and errors (real ffmpeg at 160 px)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.demo import make_click_track
from matchcut.pipeline.media import MediaError, MediaInfo, extract_frame, probe
from matchcut.pipeline.timeline import Clip, Timeline, Transition
from matchcut.review import items as it
from matchcut.review import render as rv
from matchcut.review.items import ReviewItem, ReviewPair, ab_file, prepare_review, review_key
from matchcut.review.window import ReviewError, review_window, sub_timeline, window_edl

BLUE, RED = 0, 2  # shot indices of the two solid-colour shots


def two_shots(kind="cut", seconds=0.0, stage="final", out=2.0, inn=5.0):
    """blue shot 0 [0.5, out] then red shot 2 [inn, 6.5], one beat each."""
    return Timeline(source="demo.mp4", fps=25.0, stage=stage, clips=[
        Clip("c001", BLUE, 0.5, out, "b1"),
        Clip("c002", RED, inn, 6.5, "b2", transition_in=Transition(kind, seconds)),
    ])


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


def has_audio_stream(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(out.stdout.strip())


def rgb(path, t):
    img = np.asarray(extract_frame(str(path), t, 64)).astype(float)
    return img[..., 0].mean(), img[..., 2].mean()  # (red, blue)


def small_cfg(pre=1.0, post=1.0):
    cfg = PipelineConfig()
    cfg.review.pre_seconds, cfg.review.post_seconds, cfg.review.width = pre, post, 160
    return cfg


def workspace(tmp_path, video, **timelines):
    """A workspace with probe.json and the named stage timelines (roughcut=..., final=...)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "probe.json").write_text(json.dumps(probe(str(video)).to_dict()))
    for stage, tl in timelines.items():
        tl.save(ws / f"{stage}.json")
    return ws


def no_ffmpeg(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("ffmpeg must not run")

    monkeypatch.setattr(rv, "render", fail)
    monkeypatch.setattr(rv, "run", fail)


def test_a_hard_cut_junction_prepares_an_exact_edl_without_running_ffmpeg(demo_video, tmp_path, monkeypatch):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    no_ffmpeg(monkeypatch)
    cfg = small_cfg()
    pair = prepare_review(ws, cfg, "final", 0)
    b = pair.b
    assert isinstance(pair, ReviewPair) and pair.a is None and pair.note == ""
    assert isinstance(b, ReviewItem)
    assert (b.kind, b.stage, b.index, b.cached, b.transition) == ("edl", "final", 0, False, "cut")
    assert b.cut == 1.0 and b.duration == 2.0
    assert b.label == "final  shot 0 -> shot 2  cut"
    assert b.path.parent == ws / "review" and b.path.suffix == ".edl" and b.path.name.startswith("final_s0-s2_")
    tl = two_shots()
    w = review_window(tl, 0, 1.0, 1.0)
    assert b.path.read_text() == window_edl(tl, w, str(Path(demo_video).resolve()))


def test_a_forced_render_of_a_hard_cut_cuts_from_blue_to_red(demo_video, tmp_path):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    b = prepare_review(ws, small_cfg(), "final", 0, force_render=True).b
    assert (b.kind, b.cached) == ("render", False)
    assert b.path.suffix == ".mp4" and b.path.name.startswith("final_s0-s2_")
    assert abs(duration(b.path) - 2.0) < 0.1 and abs(b.duration - 2.0) < 1e-6 and b.cut == 1.0
    red, blue = rgb(b.path, 0.8)
    assert blue > red + 40
    red, blue = rgb(b.path, 1.2)
    assert red > blue + 40
    assert has_audio_stream(b.path)


def test_a_dissolve_junction_renders_and_blends_mid_transition(demo_video, tmp_path):
    # clip 1 lasts 1.5 s, so the incoming clip fades in from 1.5 - 0.6 = 0.9; the cut lasts 2.4 s
    ws = workspace(tmp_path, demo_video, final=two_shots("dissolve", 0.6))
    b = prepare_review(ws, small_cfg(), "final", 0).b
    assert b.kind == "render" and b.transition == "dissolve 0.60s"
    assert abs(b.cut - 0.9) < 1e-6 and abs(duration(b.path) - 2.4) < 0.1
    blue_red, blue_blue = rgb(b.path, 0.3)  # pure blue shot
    red_red, red_blue = rgb(b.path, 2.2)  # pure red shot
    mid_red, mid_blue = rgb(b.path, 1.2)
    assert blue_red + 15 < mid_red < red_red - 15
    assert red_blue + 15 < mid_blue < blue_blue - 15


def test_a_silent_source_renders_without_audio(silent_video, tmp_path):
    ws = workspace(tmp_path, silent_video, final=two_shots())
    b = prepare_review(ws, small_cfg(), "final", 0, force_render=True).b
    assert not has_audio_stream(b.path)
    assert abs(duration(b.path) - 2.0) < 0.1


def test_a_second_identical_call_is_cached_and_a_new_window_changes_the_path(demo_video, tmp_path, monkeypatch):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    cfg = small_cfg()
    first = prepare_review(ws, cfg, "final", 0, force_render=True).b
    mtime = first.path.stat().st_mtime_ns
    no_ffmpeg(monkeypatch)
    again = prepare_review(ws, cfg, "final", 0, force_render=True).b
    assert again.cached is True and again.path == first.path and again.path.stat().st_mtime_ns == mtime
    assert prepare_review(ws, cfg, "final", 0, force_render=True).b.cached is True
    other = prepare_review(ws, small_cfg(pre=0.5), "final", 0).b
    assert other.path != first.path
    edl = prepare_review(ws, cfg, "final", 0).b
    assert edl.kind == "edl" and edl.path != first.path
    assert prepare_review(ws, cfg, "final", 0).b.cached is True  # the EDL is reused too


def test_ab_compares_the_roughcut_hard_cut_with_the_refined_dissolve(demo_video, tmp_path):
    rough = two_shots(stage="roughcut")
    final = two_shots("dissolve", 0.6, stage="final", out=1.8)
    ws = workspace(tmp_path, demo_video, roughcut=rough, final=final)
    pair = prepare_review(ws, small_cfg(), "final", 0, ab=True)
    assert pair.note == ""
    assert pair.a.kind == "edl" and pair.b.kind == "render"
    assert pair.a.stage == "roughcut" and pair.b.stage == "final"
    assert pair.a.label.startswith("A  roughcut") and pair.b.label.startswith("B  final")

    # a junction that first appears at final has nothing earlier to compare
    other = Timeline(source="demo.mp4", fps=25.0, stage="roughcut", clips=[
        Clip("c001", BLUE, 0.5, 2.0, "b1"), Clip("c002", 3, 6.6, 8.0, "b2")])
    other.save(ws / "roughcut.json")
    alone = prepare_review(ws, small_cfg(), "final", 0, ab=True)
    assert alone.a is None and alone.b.stage == "final"
    assert alone.note == "nothing earlier to compare: this junction first appears at final"


def test_ab_file_joins_both_renders_with_a_black_gap(demo_video, tmp_path):
    rough = two_shots(stage="roughcut")
    final = two_shots("dissolve", 0.6, stage="final", out=1.8)
    ws = workspace(tmp_path, demo_video, roughcut=rough, final=final)
    cfg = small_cfg()
    pair = prepare_review(ws, cfg, "final", 0, ab=True)
    path = ab_file(pair, ws, cfg)
    assert path.parent == ws / "review" and path.name.startswith("ab_") and path.suffix == ".mp4"
    assert abs(duration(path) - (pair.a.duration + 0.5 + pair.b.duration)) < 0.2
    assert has_audio_stream(path)
    red, blue = rgb(path, pair.a.duration + 0.25)
    assert red < 20 and blue < 20
    mtime = path.stat().st_mtime_ns
    assert ab_file(pair, ws, cfg) == path and path.stat().st_mtime_ns == mtime
    assert pair.a.kind == "edl"  # the pair itself is untouched; ab_file re-prepared renders


def test_errors_are_one_line_and_leave_no_media_behind(demo_video, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ReviewError, match=r"no timelines in .*: run the pipeline first"):
        prepare_review(empty, small_cfg())
    ws = workspace(tmp_path, demo_video, final=two_shots())
    cfg = small_cfg()
    with pytest.raises(ReviewError, match="no stage pacing in"):
        prepare_review(ws, cfg, "pacing", 0)
    with pytest.raises(ReviewError, match="junction 6 is out of range 1..1"):
        prepare_review(ws, cfg, "final", 5)
    with pytest.raises(ReviewError, match="the review window is empty"):
        prepare_review(ws, small_cfg(pre=0.0, post=0.0), "final", 0)

    def boom(*a, **k):
        raise MediaError("command failed (1): ffmpeg -i x\nsomething\nInvalid argument\n")

    monkeypatch.setattr(rv, "render", boom)
    with pytest.raises(ReviewError) as e:
        prepare_review(ws, cfg, "final", 0, force_render=True)
    assert str(e.value) == "ffmpeg failed: Invalid argument"

    def missing(*a, **k):
        raise MediaError("ffmpeg not found on PATH; install ffmpeg to use the pipeline")

    monkeypatch.setattr(rv, "render", missing)
    with pytest.raises(ReviewError) as e:
        prepare_review(ws, cfg, "final", 0, force_render=True)
    assert str(e.value) == "ffmpeg not found on PATH"

    gone = MediaInfo(path=str(tmp_path / "gone.mp4"), duration=10, width=320, height=180, fps=25.0, has_audio=True)
    (ws / "probe.json").write_text(json.dumps(gone.to_dict()))
    with pytest.raises(ReviewError, match="source footage not found: "):
        prepare_review(ws, cfg, "final", 0)
    assert [p for p in (ws / "review").rglob("*") if p.suffix in (".mp4", ".edl")] == []


def test_the_source_comes_from_the_config_when_there_is_no_probe_json(demo_video, tmp_path, monkeypatch):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    (ws / "probe.json").unlink()
    cfg = small_cfg()
    with pytest.raises(ReviewError, match="source footage not found: "):
        prepare_review(ws, cfg, "final", 0)
    cfg.source = str(demo_video)
    assert prepare_review(ws, cfg, "final", 0).b.kind == "edl"


def test_music_is_offset_to_the_first_rendered_clip_without_touching_the_config(demo_video, tmp_path, monkeypatch):
    click = make_click_track(tmp_path / "click.wav", seconds=20.0)
    cuts = Timeline(source="demo.mp4", fps=25.0, stage="final", clips=[
        Clip("c001", 0, 0.5, 2.0, "b1"), Clip("c002", 1, 2.5, 4.0, "b2"), Clip("c003", 2, 5.0, 6.5, "b3")])
    ws = workspace(tmp_path, demo_video, final=cuts)
    cfg = small_cfg()
    cfg.music.path, cfg.music.start = str(click), 4.0
    seen = []
    real = rv.render

    def record(tl, media, rcfg, output, **kw):
        seen.append(kw.get("music"))
        return real(tl, media, rcfg, output, **kw)

    monkeypatch.setattr(rv, "render", record)
    b = prepare_review(ws, cfg, "final", 1).b  # junction between clip 2 and clip 3: window starts inside clip 2
    assert b.kind == "render"
    w = review_window(cuts, 1, 1.0, 1.0)
    assert len(seen) == 1 and seen[0].start == 4.0 + cuts.record_start(w.first) and seen[0].start > 4.0
    assert cfg.music.start == 4.0
    assert has_audio_stream(b.path)


def test_the_key_depends_on_the_review_config_the_source_and_the_kind(tmp_path):
    src = tmp_path / "s.mp4"
    src.write_bytes(b"x" * 10)
    tl = two_shots()
    cfg = PipelineConfig()
    w = review_window(tl, 0, 2.0, 2.0)
    sub = sub_timeline(tl, w)
    k = review_key(sub, w, str(src), cfg, "edl")
    assert len(k) == 16 and k == review_key(sub, w, str(src), cfg, "edl")
    assert k != review_key(sub, w, str(src), cfg, "render")
    cfg2 = PipelineConfig()
    cfg2.review.width = 320
    assert k != review_key(sub, w, str(src), cfg2, "edl")
    moved = sub.copy()
    moved.clips[0].source_out -= 0.04
    assert k != review_key(moved, w, str(src), cfg, "edl")
    src.write_bytes(b"y" * 11)  # the footage changed
    assert k != review_key(sub, w, str(src), cfg, "edl")


def test_a_sub_frame_edit_gives_a_new_edl_not_a_stale_cached_one(demo_video, tmp_path):
    ws = workspace(tmp_path, demo_video, final=two_shots())
    cfg = small_cfg()
    first = prepare_review(ws, cfg, "final", 0).b
    two_shots(inn=5.01).save(ws / "final.json")  # 0.01 s at 25 fps: below the frame grid
    second = prepare_review(ws, cfg, "final", 0).b
    assert second.kind == "edl" and second.cached is False and second.path != first.path
    assert ",5.0100," in second.path.read_text() and ",5.0100," not in first.path.read_text()


def test_the_item_stage_is_the_loaded_stage_name_even_if_the_timeline_has_none(demo_video, tmp_path):
    ws = workspace(tmp_path, demo_video, final=two_shots(stage=""))
    cfg = small_cfg()
    b = prepare_review(ws, cfg, "final", 0).b
    assert b.stage == "final" and b.path.name.startswith("final_s0-s2_")
    assert prepare_review(ws, cfg, b.stage, b.index, force_render=True).b.kind == "render"
