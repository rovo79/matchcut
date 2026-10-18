"""Junction review: the window, the sub-timeline, the render rule and the mpv EDL (all pure)."""
from __future__ import annotations

import pytest

from matchcut.pipeline.config import PipelineConfig, ReviewConfig
from matchcut.pipeline.timeline import Clip, Timeline, Transition
from matchcut.review.window import (
    ReviewError,
    Window,
    music_start,
    needs_render,
    review_window,
    sub_timeline,
    window_edl,
)


def tl(*specs, fade_in=0.0, fade_out=0.0, stage="final"):
    """specs: (shot, in, out, transition_kind, transition_seconds); ids c001.. in order."""
    clips = [Clip(f"c{k + 1:03d}", s, a, b, "b1", transition_in=Transition(kind, d)) for k, (s, a, b, kind, d) in enumerate(specs)]
    return Timeline(source="s.mp4", fps=25.0, clips=clips, fade_in=fade_in, fade_out=fade_out, stage=stage)


CUTS = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "cut", 0), (2, 20.0, 23.0, "cut", 0), (3, 30.0, 33.0, "cut", 0))


def test_config_has_a_review_section_with_defaults():
    cfg = PipelineConfig()
    assert cfg.review == ReviewConfig(pre_seconds=2.0, post_seconds=2.0, width=640, crf=23, mpv="mpv", always_render=False)
    again = PipelineConfig.from_dict(cfg.to_dict())
    assert again.review == cfg.review


def test_a_hard_cut_window_spans_pre_and_post_around_the_cut():
    w = review_window(CUTS, 1, 2.0, 2.0)  # junction between clip 1 (ends at 6.0) and clip 2
    assert isinstance(w, Window)
    assert (w.junction, w.first, w.last) == (1, 1, 2)
    assert (w.start, w.cut, w.end) == (4.0, 6.0, 8.0)
    assert w.offset == 1.0 and w.duration == 4.0


def test_a_long_pre_reaches_back_into_earlier_clips_and_clamps_at_zero():
    w = review_window(CUTS, 1, 10.0, 0.5)
    assert (w.first, w.last) == (0, 2)
    assert (w.start, w.end, w.offset) == (0.0, 6.5, 0.0)


def test_post_reaches_forward_and_clamps_at_the_end():
    w = review_window(CUTS, 2, 1.0, 10.0)
    assert (w.first, w.last, w.start, w.cut, w.end) == (2, 3, 8.0, 9.0, 12.0)


def test_a_dissolve_window_includes_the_transition_after_the_cut():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 0.5), (2, 20.0, 23.0, "cut", 0))
    w = review_window(t, 0, 1.0, 1.0)  # clip 1 starts fading in at 2.5
    assert (w.start, w.cut, w.end) == (1.5, 2.5, 4.0)
    assert (w.first, w.last) == (0, 1)


def test_a_window_starting_inside_an_incoming_dissolve_includes_the_clip_before():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 1.0), (2, 20.0, 23.0, "cut", 0))
    # clip 1 starts at 2.0 and is a blend with clip 0 until 3.0; junction 1 cuts at 2.0 + 3.0 = 5.0
    w = review_window(t, 1, 2.5, 1.0)  # start = 2.5, inside clip 1's dissolve
    assert w.start == 2.5 and w.first == 0 and w.offset == 2.5


def test_the_next_clips_incoming_dissolve_pulls_it_into_the_window():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "cut", 0), (2, 20.0, 23.0, "dissolve", 1.0))
    w = review_window(t, 0, 1.0, 2.6)  # end = 3.0 + 2.6 = 5.6; clip 2 starts fading in at 5.0
    assert w.last == 2


def test_an_out_of_range_junction_is_an_error():
    with pytest.raises(ReviewError, match=r"junction 4 is out of range 1\.\.3"):
        review_window(CUTS, 3, 1.0, 1.0)


def test_the_sub_timeline_cuts_into_its_first_clip_and_keeps_only_its_own_end_fades():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 0.5), (2, 20.0, 23.0, "dip_to_black", 0.4), fade_in=0.5, fade_out=0.7)
    t.history = []  # (any history is dropped anyway)
    sub = sub_timeline(t, review_window(t, 1, 1.0, 1.0))
    assert [c.id for c in sub.clips] == ["c002", "c003"]
    assert sub.clips[0].transition_in.is_cut and sub.clips[1].transition_in.kind == "dip_to_black"
    assert sub.fade_in == 0.0 and sub.fade_out == 0.7 and sub.history == []
    assert t.clips[1].transition_in.kind == "dissolve"  # the input is untouched
    whole = sub_timeline(t, review_window(t, 0, 9.0, 9.0))
    assert whole.fade_in == 0.5 and whole.fade_out == 0.7


def test_music_starts_where_the_first_rendered_clip_starts_in_the_full_cut():
    w = review_window(CUTS, 2, 1.0, 1.0)  # first == 2, which starts at 6.0
    assert music_start(CUTS, w, 4.0) == 10.0


def test_a_hard_cut_window_needs_no_render_and_becomes_an_edl():
    w = review_window(CUTS, 1, 2.0, 2.0)  # clips 1 (10-13) and 2 (20-23); start 4.0, cut 6.0, end 8.0, offset 1.0
    cfg = PipelineConfig()
    assert not needs_render(CUTS, w, cfg)
    assert window_edl(CUTS, w, "/media/src.mp4") == (
        "# mpv EDL v0\n"
        "/media/src.mp4,11.0000,2.0000,title=final s1 c002\n"
        "/media/src.mp4,20.0000,2.0000,title=final s2 c003\n"
    )


def test_a_window_with_a_dissolve_or_a_fade_or_music_needs_a_render():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 0.5), (2, 20.0, 23.0, "cut", 0))
    cfg = PipelineConfig()
    assert needs_render(t, review_window(t, 0, 1.0, 1.0), cfg)        # the dissolve is inside
    assert not needs_render(t, review_window(t, 1, 0.5, 0.5), cfg)    # junction 1 is a hard cut, the dissolve is outside the window
    faded = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "cut", 0), fade_in=1.0)
    assert needs_render(faded, review_window(faded, 0, 2.5, 0.5), cfg)      # starts at 0.5, inside the 1.0 s fade-in
    assert not needs_render(faded, review_window(faded, 0, 1.5, 0.5), cfg)  # starts at 1.5, after the fade-in
    with_music = PipelineConfig()
    with_music.music.path = "/m.wav"
    assert needs_render(CUTS, review_window(CUTS, 1, 1.0, 1.0), with_music)
    forced = PipelineConfig()
    forced.review.always_render = True
    assert needs_render(CUTS, review_window(CUTS, 1, 1.0, 1.0), forced)


def test_window_edl_refuses_a_window_that_needs_a_render():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 0.5))
    with pytest.raises(ReviewError, match="window needs a render: dissolve"):
        window_edl(t, review_window(t, 0, 1.0, 1.0), "/s.mp4")


def test_window_edl_escapes_a_path_with_special_characters():
    w = review_window(CUTS, 1, 1.0, 1.0)
    text = window_edl(CUTS, w, "/media/a,b;c.mp4")
    path = "/media/a,b;c.mp4"
    assert f"%{len(path.encode('utf-8'))}%{path},12.0000" in text
