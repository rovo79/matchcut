"""The common time model: rates, frames, rationals, snapping, ids, and the exact dissolve conversion."""
from __future__ import annotations

import json
from fractions import Fraction

import pytest

from matchcut.interchange import InterchangeError, common
from matchcut.interchange.common import (clip_key, exact_rate, export_timeline, junction_id, junction_ids, layout, record_frames, rt, snap_rate,
                                         source_info, to_frames)
from matchcut.pipeline.demo import make_demo_footage
from matchcut.pipeline.timeline import Clip, Timeline, Transition, clip_origin

from .conftest import make_cover_art_mp3, record_tolerance_frames, exported

NTSC = Fraction(30000, 1001)
FILM = Fraction(24000, 1001)


def synthetic() -> Timeline:
    """A (1-3 s) --dissolve 0.5 s--> B (5-7 s) --cut--> C (10-12 s) at 24 fps; record duration 5.5 s."""
    tl = Timeline(source="/x/media.mp4", fps=24.0, stage="final")
    tl.clips = [Clip("c001", 0, 1.0, 3.0), Clip("c002", 1, 5.0, 7.0, transition_in=Transition("dissolve", 0.5)), Clip("c003", 2, 10.0, 12.0)]
    return tl


# -- rates, frames, rationals --------------------------------------------------------------------
def test_source_info_of_the_demo_footage_is_exact(world):
    src = source_info(world.video)
    assert src.rate == Fraction(25, 1) and isinstance(src.rate, Fraction)
    assert src.path == str(world.src.path) and src.path.startswith("/") and src.url == "file://" + src.path
    assert (src.width, src.height, src.has_audio) == (320, 180, True)
    assert abs(src.duration_frames - 18.5 * 25) <= 1
    assert src.size > 0 and src.mtime_ns > 0
    assert source_info(world.video) == src  # frozen and comparable


def test_source_info_without_audio_and_of_an_audio_only_file(silent_footage, music_file):
    assert source_info(str(silent_footage)).has_audio is False
    m = source_info(str(music_file))
    assert (m.width, m.height, m.has_audio) == (0, 0, True) and m.rate == Fraction(8000) and abs(m.seconds - 30.0) < 0.01


def test_source_info_missing_file_is_a_one_line_error(tmp_path):
    with pytest.raises(InterchangeError, match=r"^source footage not found: .*nothere\.mp4$"):
        source_info(str(tmp_path / "nothere.mp4"))


def test_source_info_ignores_cover_art_and_supports_an_audio_only_mode(tmp_path):
    mp3 = make_cover_art_mp3(tmp_path / "cover.mp3", 10.0)
    m = source_info(str(mp3))  # the attached picture is a video stream to ffprobe (90000 fps, 0.04 s); it is not footage
    assert (m.width, m.height, m.has_audio) == (0, 0, True) and m.rate == Fraction(22050) and abs(m.seconds - 10.0) < 0.2
    assert source_info(str(mp3), audio_only=True) == m
    with pytest.raises(InterchangeError, match="no audio stream"):
        source_info(str(make_demo_footage(tmp_path / "v.mp4", shots=[("color=c=0x204080:s={w}x{h}:r={fps}", 1.0)], audio=False)), audio_only=True)


def test_audio_only_mode_ignores_the_video_of_a_file_that_has_audio(footage):
    a = source_info(str(footage), audio_only=True)
    assert (a.width, a.height, a.has_audio) == (0, 0, True) and a.rate != Fraction(25) and abs(a.seconds - 18.5) < 0.2


def test_probed_rationals_a_hair_off_a_common_rate_are_snapped(world, monkeypatch):
    for text, expected in (("2997/100", NTSC), ("23976/1000", FILM), ("24/1", Fraction(24)), ("5994/100", Fraction(60000, 1001)),
                           ("30/1", Fraction(30)), ("12/1", Fraction(12))):
        class Proc:
            stdout = json.dumps({"streams": [{"codec_type": "video", "r_frame_rate": text, "width": 10, "height": 10, "duration": "2.0"}],
                                 "format": {"duration": "2.0"}}).encode()

        monkeypatch.setattr(common, "run", lambda cmd, Proc=Proc: Proc)
        assert source_info(world.video).rate == expected, text
    assert exact_rate(Fraction(30000, 1001)) == NTSC and exact_rate(Fraction(2997, 100)) == NTSC


def test_source_info_falls_back_to_the_nearest_common_rate_for_a_float_only_probe(world, monkeypatch):
    class Proc:
        stdout = json.dumps({"streams": [{"codec_type": "video", "r_frame_rate": "0/0", "avg_frame_rate": "23.976", "width": 10, "height": 10,
                                          "duration": "2.0"}], "format": {"duration": "2.0"}}).encode()

    monkeypatch.setattr(common, "run", lambda cmd: Proc)
    src = source_info(world.video)
    assert src.rate == FILM and src.duration_frames == 48


@pytest.mark.parametrize("value,expected", [
    (23.976, FILM), (23.976023976, FILM), (24.0, Fraction(24)), (25.0, Fraction(25)), (29.97, NTSC), (30.0, Fraction(30)),
    (50.0, Fraction(50)), (59.94, Fraction(60000, 1001)), (60.0, Fraction(60)), (12.0, Fraction(12)), (47.952, Fraction(48000, 1001)),
    (48.0, Fraction(48)), (119.88, Fraction(120000, 1001)), (120.0, Fraction(120)),
])
def test_snap_rate_table(value, expected):
    assert snap_rate(value) == expected


@pytest.mark.parametrize("t,rate,frames", [
    (0.0, Fraction(25), 0), (0.04, Fraction(25), 1), (1.0, Fraction(25), 25), (0.02, Fraction(25), 1),  # 0.5 frame rounds away from zero
    (0.06, Fraction(25), 2), (0.019, Fraction(25), 0), (-0.02, Fraction(25), -1), (-0.01, Fraction(25), 0),
    (1 / 48, Fraction(24), 1), (3 / 48, Fraction(24), 2), (-1 / 48, Fraction(24), -1), (0.5 * 1001 / 30000, NTSC, 1), (1.5 * 1001 / 30000, NTSC, 2),
    (2.5 * 1001 / 24000, FILM, 3), (-0.5 * 1001 / 30000, NTSC, -1), (1.02, Fraction(25), 26),
    (1.001, FILM, 24), (2.002, FILM, 48), (10.01, NTSC, 300), (0.0417, FILM, 1), (1.0 / 24 * 1.5, Fraction(24), 2),
])
def test_to_frames_rounds_half_away_from_zero(t, rate, frames):
    assert to_frames(t, rate) == frames


@pytest.mark.parametrize("frames,rate,text", [
    (0, Fraction(25), "0s"), (0, FILM, "0s"), (1, Fraction(25), "1/25s"), (25, Fraction(25), "1s"), (50, Fraction(25), "2s"),
    (1, FILM, "1001/24000s"), (24, FILM, "1001/1000s"), (1, NTSC, "1001/30000s"), (30000, NTSC, "1001s"), (42, Fraction(24), "7/4s"),
    (-1, Fraction(25), "-1/25s"),
])
def test_rt_is_a_reduced_fcpxml_rational(frames, rate, text):
    assert rt(frames, rate) == text


# -- snapping for export -------------------------------------------------------------------------
def test_export_timeline_reports_the_clips_the_snap_moved_for_a_rough_cut_and_none_for_final(world):
    rough, notes = exported(world, "roughcut")
    assert notes, "the rough cut is not on the frame grid"
    assert all(n.split(":")[0] in {c.id for c in world.by_stage["roughcut"].clips} and " moved " in n and "frame" in n for n in notes)
    for c in rough.clips:  # snapped copy: on the grid, identity kept
        assert abs(c.source_in * 25 - round(c.source_in * 25)) < 0.01 and abs(c.source_out * 25 - round(c.source_out * 25)) < 0.01
    assert [clip_origin(c) for c in rough.clips] == [clip_origin(c) for c in world.by_stage["roughcut"].clips]
    final, final_notes = exported(world, "final")
    assert final_notes == []
    assert [(c.source_in, c.source_out) for c in final.clips] == [(c.source_in, c.source_out) for c in world.by_stage["final"].clips]


def test_export_timeline_note_text_and_untouched_canonical():
    tl = Timeline(source="/x/m.mp4", fps=25.0, stage="roughcut")
    tl.clips = [Clip("c001", 0, 1.0, 3.0), Clip("c003", 1, 5.0, 6.0), Clip("c004", 2, 8.0, 9.52)]
    before = tl.to_dict()
    # shot 1 starts one frame after the clip's in-point: the snap pulls the in-point back inside the shot
    snapped, notes = export_timeline(tl, {0: (0.0, 5.0), 1: (5.04, 8.0), 2: (7.0, 12.0)}, Fraction(25))
    assert tl.to_dict() == before  # the canonical timeline is never touched
    assert notes == ["c003: in moved +1 frame"]
    assert snapped.clips[1].source_in == pytest.approx(5.04)
    assert snapped.clips[1].origin == "1@5.000-6.000"  # identity of the unsnapped clip
    # off-grid times report the fraction of a frame
    tl.clips[0].source_in = 1.01
    assert export_timeline(tl, None, Fraction(25))[1] == ["c001: in moved -0.25 frame"]


# -- ids -----------------------------------------------------------------------------------------
def test_junction_and_clip_ids_are_stable_and_repeats_are_unique():
    tl = Timeline(source="/x/m.mp4", fps=25.0)
    tl.clips = [Clip("c001", 3, 0, 1), Clip("c002", 4, 2, 3), Clip("c003", 3, 4, 5), Clip("c004", 4, 6, 7), Clip("c005", 3, 8, 9), Clip("c006", 4, 10, 11)]
    assert junction_id(tl.clips[0], tl.clips[1]) == "s3-s4"
    assert junction_ids(tl) == ["s3-s4", "s4-s3", "s3-s4#2", "s4-s3#2", "s3-s4#3"]
    assert len(set(junction_ids(tl))) == 5
    assert clip_key(tl.clips[0]) == clip_origin(tl.clips[0]) == "3@0.000-1.000"
    tl.clips[0].origin = "0@0.000-9.000"
    assert clip_key(tl.clips[0]) == "0@0.000-9.000"


# -- the dissolve conversion ---------------------------------------------------------------------
def test_layout_converts_overlap_to_a_centred_transition_exactly():
    tl = synthetic()
    rows = layout(tl, Fraction(24))
    a, b, c = rows
    assert b.transition == ("dissolve", 6, 6) and a.transition == ("cut", 0, 0)
    assert (a.vis_in, a.dur, a.tail) == (24, 42, 6)  # visible range ends at source_out - d/2 (3 s - 6 f)
    assert (b.vis_in, b.dur, b.head) == (126, 42, 6)  # starts at source_in + d/2 (5 s + 6 f)
    assert (a.rec, b.rec, c.rec) == (0, 36, 84)  # = Timeline.record_start in frames: the dissolve starts 12 f before A's end
    assert abs(tl.record_start(1) * 24 - b.rec) < 1e-9
    assert (a.cut, b.cut, c.cut) == (0, 42, 84) and (b.rec_in, b.rec_out) == (42, 84)
    assert a.dur + b.dur + c.dur == record_frames(rows) == round(tl.duration * 24) == 132
    assert [(r.in_f, r.out_f) for r in rows] == [(24, 72), (120, 168), (240, 288)]  # matchcut's own ranges are untouched


def test_layout_odd_dissolve_splits_frames_and_diffuses_rounding_error():
    tl = Timeline(source="/x/m.mp4", fps=25.0)
    tl.clips = [Clip("c001", 0, 0.0, 4.0)] + [Clip(f"c{i + 2:03d}", i + 1, 5.0 * (i + 1), 5.0 * (i + 1) + 4.0, transition_in=Transition("dissolve", 0.5)) for i in range(3)]
    rows = layout(tl, Fraction(25))
    assert [r.tr for r in rows] == [0, 13, 12, 13]  # 12.5 f each: the running total is rounded, not each transition
    assert rows[1].transition == ("dissolve", 6, 7) and rows[2].transition == ("dissolve", 6, 6)
    assert abs(record_frames(rows) - tl.duration * 25) <= record_tolerance_frames(Fraction(25))


def test_layout_rejects_a_clip_swallowed_by_its_transitions():
    tl = Timeline(source="/x/m.mp4", fps=25.0)
    tl.clips = [Clip("c001", 0, 0.0, 2.0), Clip("c002", 1, 3.0, 3.08, transition_in=Transition("dissolve", 0.1)), Clip("c003", 2, 5.0, 7.0, transition_in=Transition("dissolve", 0.1))]
    with pytest.raises(InterchangeError, match="shorter than its transitions"):
        layout(tl, Fraction(25))


def test_ntsc_rates_use_exact_rationals_in_the_layout():
    fps = float(NTSC)
    tl = Timeline(source="/x/m.mp4", fps=fps, stage="final")
    tl.clips = [Clip("c001", 0, 1.0, 3.0), Clip("c002", 1, 5.0, 7.0, transition_in=Transition("dissolve", 14 / fps)), Clip("c003", 2, 10.0, 12.0)]
    rows = layout(tl, NTSC)
    assert rows[1].tr == 14 and rows[1].transition == ("dissolve", 7, 7)
    assert rows[1].rec == to_frames(3.0, NTSC) - to_frames(1.0, NTSC) - 14  # A is 2 s long
    assert abs(record_frames(rows) - tl.duration * fps) <= record_tolerance_frames(NTSC)


def test_layout_of_real_stages_keeps_the_record_length_within_half_a_frame(world):
    for stage in ("roughcut", "final"):
        tl, _ = exported(world, stage)
        rows = layout(tl, world.src.rate)
        assert abs(record_frames(rows) - tl.duration * 25) <= record_tolerance_frames(world.src.rate), stage
        for i, r in enumerate(rows):
            assert abs(r.rec - tl.record_start(i) * 25) <= record_tolerance_frames(world.src.rate), (stage, i)
