"""CMX3600 EDL export and import."""
from __future__ import annotations

import re
from dataclasses import replace
from fractions import Fraction

import pytest

from matchcut.interchange import InterchangeError
from matchcut.interchange.common import clip_key, junction_ids, layout, record_frames
from matchcut.interchange.edl import export_edl, import_edl
from matchcut.pipeline.timeline import Clip, Timeline, Transition

from .conftest import check_equivalent, exported
from .test_common import FILM, NTSC, synthetic
from .test_otio import fake_src

RATE24 = Fraction(24)


def lines(text: str):
    return text.splitlines()


def events(text: str):
    """The event lines as token lists."""
    return [ln.split() for ln in lines(text) if re.match(r"^\d{3,}\s", ln)]


def edl_of(tl: Timeline, rate: Fraction = RATE24, audio: bool = True, stage: str = "final", title: str = "demo - final") -> str:
    return export_edl(tl, stage, fake_src(rate, audio), title)


# -- structure -----------------------------------------------------------------------------------
def test_synthetic_cut_with_a_dissolve_is_a_c_and_d_event_pair_on_the_frame_grid():
    text = edl_of(synthetic())
    assert lines(text)[:2] == ["TITLE: demo - final", "FCM: NON-DROP FRAME"]
    assert events(text) == [
        ["001", "AX", "AA/V", "C", "00:00:01:00", "00:00:02:12", "00:00:00:00", "00:00:01:12"],
        ["002", "AX", "AA/V", "C", "00:00:02:12", "00:00:02:12", "00:00:01:12", "00:00:01:12"],  # the outgoing side of the dissolve
        ["002", "AX", "AA/V", "D", "012", "00:00:05:00", "00:00:07:00", "00:00:01:12", "00:00:03:12"],
        ["003", "AX", "AA/V", "C", "00:00:10:00", "00:00:12:00", "00:00:03:12", "00:00:05:12"],
    ]
    # the last record frame is the cut's length: 5.5 s at 24 fps
    assert events(text)[-1][-1] == "00:00:05:12" and record_frames(layout(synthetic(), RATE24)) == 132


def test_video_only_when_the_source_has_no_audio():
    text = edl_of(synthetic(), audio=False)
    assert all(e[2] == "V" for e in events(text)) and len(events(text)) == 4


def test_standard_columns_are_aligned_like_other_cmx3600_writers():
    text = edl_of(synthetic())
    cut = next(ln for ln in lines(text) if ln.startswith("001"))
    dis = next(ln for ln in lines(text) if "D    012" in ln)
    assert cut.startswith("001  AX       AA/V  C        00:00:01:00 ")
    assert dis.startswith("002  AX       AA/V  D    012 00:00:05:00 ")


def test_comments_carry_the_file_name_clip_key_and_junction_ids():
    tl = synthetic()
    text = edl_of(tl)
    assert text.count("* FROM CLIP NAME: media.mp4") >= 3
    assert text.count("* MATCHCUT CLIP ") == 3
    for c in tl.clips:
        assert f"* MATCHCUT CLIP {clip_key(c)}" in text
    ids = junction_ids(tl)
    assert [ln for ln in lines(text) if "MATCHCUT JUNCTION" in ln] == [f"* MATCHCUT JUNCTION {j}" for j in ids]
    # each comment sits after the event of its incoming clip
    ls = lines(text)
    i = ls.index(f"* MATCHCUT JUNCTION {ids[0]}")
    assert any(ln.startswith("002") and "D" in ln.split() for ln in ls[:i]) and not any(ln.startswith("003") for ln in ls[:i])


def test_dip_to_black_is_a_dissolve_with_a_transition_comment():
    tl = synthetic()
    tl.clips[1].transition_in = Transition("dip_to_black", 0.5)
    text = edl_of(tl)
    assert "D    012" in text and "* MATCHCUT TRANSITION dip_to_black" in text
    assert text.count("MATCHCUT TRANSITION") == 1
    back, _ = import_edl(text, RATE24, "/x/media.mp4")
    assert back.clips[1].transition_in.kind == "dip_to_black" and back.clips[0].transition_in.is_cut


def test_document_level_comments_stage_source_rate_and_fades():
    tl = synthetic()
    tl.fade_in, tl.fade_out = 0.4, 0.8
    text = edl_of(tl, stage="roughcut")
    for want in ("* MATCHCUT STAGE roughcut", "* MATCHCUT SOURCE /x/media.mp4", "* MATCHCUT RATE 24/1", "* MATCHCUT FADE_IN 0.4", "* MATCHCUT FADE_OUT 0.8"):
        assert want in text
    back, notes = import_edl(text, RATE24, "/x/media.mp4")
    assert (back.stage, back.fade_in, back.fade_out, back.name) == ("roughcut", 0.4, 0.8, "demo - final")
    assert notes == []


# -- round trips ---------------------------------------------------------------------------------
def test_round_trip_of_the_synthetic_cut_is_frame_exact():
    tl = synthetic()
    back, notes = import_edl(edl_of(tl), RATE24, "/x/media.mp4")
    assert notes == []
    check_equivalent(tl, back, RATE24)
    assert [(c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration) for c in back.clips] == \
        [(c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration) for c in tl.clips]
    assert [c.origin for c in back.clips] == [clip_key(c) for c in tl.clips]
    assert [c.id for c in back.clips] == ["c001", "c002", "c003"] and [c.shot_index for c in back.clips] == [0, 1, 2]


@pytest.mark.parametrize("stage", ["roughcut", "final"])
def test_round_trip_of_the_workspace_stages(world, stage):
    tl, _ = exported(world, stage)
    back, notes = import_edl(export_edl(tl, stage, world.src, f"demo - {stage}"), world.src.rate, world.src.path, world.bounds)
    check_equivalent(tl, back, world.src.rate)
    assert not [n for n in notes if "ignored" in n]


@pytest.mark.parametrize("rate", [FILM, Fraction(25), NTSC])
def test_round_trip_at_other_rates(rate):
    fps = float(rate)
    tl = Timeline(source="/x/m.mp4", fps=fps, stage="final")
    tl.clips = [Clip("c001", 0, 1.0, 3.0), Clip("c002", 1, 5.0, 7.0, transition_in=Transition("dissolve", 14 / fps)),
                Clip("c003", 2, 10.0, 12.0, transition_in=Transition("dissolve", 9 / fps))]
    for c in tl.clips:  # on the frame grid of this rate
        c.source_in, c.source_out = round(round(c.source_in * fps) / fps, 4), round(round(c.source_out * fps) / fps, 4)
    back, _ = import_edl(edl_of(tl, rate), rate, "/x/media.mp4")
    check_equivalent(tl, back, rate)
    assert back.fps == pytest.approx(fps)


def test_timecodes_at_23_976_and_25_fps():
    def one(rate: Fraction, in_s: float, out_s: float) -> Timeline:
        fps = float(rate)
        tl = Timeline(source="/x/m.mp4", fps=fps, stage="final")
        tl.clips = [Clip("c001", 0, round(in_s, 6), round(out_s, 6))]
        return tl

    # 23.976: the timecode base is 24 frames per second, frames counted exactly
    f_in, f_out = 100, 2000  # 00:00:04:04 and 00:01:23:08
    tl = one(FILM, f_in / float(FILM), f_out / float(FILM))
    ev = events(edl_of(tl, FILM))[0]
    assert ev[4:] == ["00:00:04:04", "00:01:23:08", "00:00:00:00", "00:01:19:04"]
    back, _ = import_edl(edl_of(tl, FILM), FILM, "/x/media.mp4")
    assert layout(back, FILM)[0].in_f == f_in and layout(back, FILM)[0].out_f == f_out
    # 25: whole seconds
    ev = events(edl_of(one(Fraction(25), 4.0, 83.32), Fraction(25)))[0]
    assert ev[4:] == ["00:00:04:00", "00:01:23:08", "00:00:00:00", "00:01:19:08"]
    # 29.97: still the 30 base (non-drop); the note about wall-clock drift is in the file
    text = edl_of(one(NTSC, 1.0, 2.0), NTSC)
    assert "FCM: NON-DROP FRAME" in text and "* MATCHCUT TIMECODE NON-DROP at 30000/1001" in text
    assert "* MATCHCUT TIMECODE" not in edl_of(synthetic())


def test_export_errors_are_one_line():
    tl = Timeline(source="/x/m.mp4", fps=24.0, stage="final")
    with pytest.raises(InterchangeError, match=r"^stage final has no clips to export$"):
        edl_of(tl)
    tl.clips = [Clip("c001", 0, 0.0, 0.5), Clip("c002", 1, 5.0, 5.5, transition_in=Transition("dissolve", 0.5))]
    tl.clips[1].transition_in = Transition("dissolve", 0.5)
    tl.clips.append(Clip("c003", 2, 9.0, 9.5, transition_in=Transition("dissolve", 0.5)))
    with pytest.raises(InterchangeError) as exc:
        edl_of(tl)
    assert "\n" not in str(exc.value) and "shorter than its transitions" in str(exc.value)
    many = Timeline(source="/x/m.mp4", fps=24.0, stage="final")
    many.clips = [Clip(f"c{i:04d}", i, float(i), i + 0.5) for i in range(1000)]
    with pytest.raises(InterchangeError, match=r"^an EDL holds at most 999 events"):
        edl_of(many)


# -- import: files that did not come from matchcut -------------------------------------------------
FOREIGN = """TITLE: edited in an editor
FCM: NON-DROP FRAME

001  A001C002 V     C        00:00:01:00 00:00:03:00 01:00:00:00 01:00:02:00
* FROM CLIP NAME: media.mp4
002  A001C002 V     C        00:00:05:00 00:00:07:00 01:00:02:00 01:00:04:00
* FROM CLIP NAME: media.mp4
"""


def test_foreign_edl_without_matchcut_comments_maps_clips_by_source_range(world):
    shots = sorted(world.bounds.items())
    (s0, (a0, a1)), (s1, (b0, b1)) = shots[0], shots[1]
    f = lambda t: int(round(t * 25))  # noqa: E731

    def tc(n):
        return f"{n // 25 // 3600:02d}:{n // 25 // 60 % 60:02d}:{n // 25 % 60:02d}:{n % 25:02d}"

    text = (f"TITLE: x\nFCM: NON-DROP FRAME\n\n"
            f"001  AX       V     C        {tc(f(a0) + 5)} {tc(f(a0) + 30)} 01:00:00:00 01:00:01:00\n"
            f"002  AX       V     C        {tc(f(b0) + 5)} {tc(f(b0) + 30)} 01:00:01:00 01:00:02:00\n")
    back, notes = import_edl(text, Fraction(25), world.src.path, world.bounds)
    assert [c.shot_index for c in back.clips] == [s0, s1]
    assert [c.id for c in back.clips] == ["h001", "h002"]
    assert layout(back, Fraction(25))[0].in_f == f(a0) + 5 and all(c.origin == "" for c in back.clips)


def test_other_media_audio_events_black_gaps_wipes_and_speed_changes_are_listed():
    text = """TITLE: t
FCM: NON-DROP FRAME

001  AX       AA/V  C        00:00:01:00 00:00:03:00 00:00:00:00 00:00:02:00
* FROM CLIP NAME: media.mp4
002  BL       V     C        00:00:00:00 00:00:01:00 00:00:02:00 00:00:03:00
003  AX       AA    C        00:00:01:00 00:00:03:00 00:00:00:00 00:00:02:00
004  AX       V     C        00:00:00:00 00:00:01:00 00:00:03:00 00:00:04:00
* FROM CLIP NAME: broll.mov
005  AX       V     C        00:00:05:00 00:00:07:00 00:00:08:00 00:00:10:00
* FROM CLIP NAME: media.mp4
006  AX       V     C        00:00:09:00 00:00:09:00 00:00:10:00 00:00:10:00
006  AX       V     W001 012 00:00:09:00 00:00:11:00 00:00:10:00 00:00:12:00
* FROM CLIP NAME: media.mp4
007  AX       V     C        00:00:20:00 00:00:22:00 00:00:12:00 00:00:14:00
M2   AX       048.0                00:00:20:00
* FROM CLIP NAME: media.mp4
"""
    back, notes = import_edl(text, RATE24, "/x/media.mp4")
    assert [(c.source_in, c.source_out) for c in back.clips] == [(1.0, 3.0), (5.0, 7.0), (9.0, 11.0), (20.0, 22.0)]
    joined = "\n".join(notes)
    for want in ("event 002", "black", "event 003", "audio", "event 004", "broll.mov", "gap of 96 frames before event 005", "event 006", "W001",
                 "wipe", "event 007", "M2"):
        assert want in joined, (want, notes)
    assert len(notes) == 6 and all("\n" not in n for n in notes)


def test_dissolve_from_an_ignored_event_is_dropped_with_a_note():
    text = """TITLE: t
FCM: NON-DROP FRAME

001  AX       V     C        00:00:00:00 00:00:01:00 00:00:00:00 00:00:01:00
* FROM CLIP NAME: broll.mov
002  AX       V     C        00:00:05:00 00:00:05:00 00:00:01:00 00:00:01:00
002  AX       V     D    012 00:00:05:00 00:00:07:00 00:00:01:00 00:00:03:00
* FROM CLIP NAME: media.mp4
"""
    back, notes = import_edl(text, RATE24, "/x/media.mp4")
    assert len(back.clips) == 1 and back.clips[0].transition_in.is_cut and (back.clips[0].source_in, back.clips[0].source_out) == (5.0, 7.0)
    assert any("dissolve" in n and "dropped" in n for n in notes)


def test_drop_frame_timecode_is_converted_at_29_97():
    text = """TITLE: t
FCM: DROP FRAME

001  AX       V     C        00:01:00;02 00:01:10;02 00:00:00;00 00:00:10;00
* FROM CLIP NAME: media.mp4
"""
    back, notes = import_edl(text, NTSC, "/x/media.mp4")
    c = layout(back, NTSC)[0]
    assert (c.in_f, c.out_f) == (1800, 2100)  # minute 1 of drop-frame timecode starts at frame 1800 (frames ;00 and ;01 do not exist)
    back25, notes25 = import_edl(text, Fraction(25), "/x/media.mp4")
    assert any("drop-frame" in n for n in notes25)


@pytest.mark.parametrize("text, match", [
    ("", r"^invalid EDL: no events$"),
    ("TITLE: t\nFCM: NON-DROP FRAME\n", r"^invalid EDL: no events$"),
    ("TITLE: t\n001  AX       V     C        00:00:xx:00 00:00:03:00 00:00:00:00 00:00:02:00\n", r"^invalid EDL: line 2: bad timecode"),
    ("TITLE: t\n001  AX       V     C        00:00:01:30 00:00:03:00 00:00:00:00 00:00:02:00\n", r"^invalid EDL: line 2: frame 30 is out of range at 24 fps$"),
    ("TITLE: t\n001  AX       V     C        00:00:03:00 00:00:01:00 00:00:00:00 00:00:02:00\n", r"^invalid EDL: event 001: the source out is before its in$"),
])
def test_malformed_edl_is_a_one_line_error(text, match):
    with pytest.raises(InterchangeError, match=match) as exc:
        import_edl(text, RATE24, "/x/media.mp4")
    assert "\n" not in str(exc.value)


def test_no_event_for_the_source_footage_is_an_error():
    text = "TITLE: t\n001  AX       V     C        00:00:01:00 00:00:03:00 00:00:00:00 00:00:02:00\n* FROM CLIP NAME: other.mov\n"
    with pytest.raises(InterchangeError, match=r"^no event of the EDL references the source footage /x/media\.mp4$"):
        import_edl(text, RATE24, "/x/media.mp4")


def test_reel_names_and_clip_names_of_an_editor_are_accepted_when_the_name_matches_the_source():
    back, notes = import_edl(FOREIGN, RATE24, "/x/media.mp4")
    assert len(back.clips) == 2 and back.name == "edited in an editor" and notes == []
    assert [(c.source_in, c.source_out) for c in back.clips] == [(1.0, 3.0), (5.0, 7.0)]


def test_a_dissolve_with_an_editor_sized_handle_is_read_in_matchcut_overlap_terms():
    text = """TITLE: t
FCM: NON-DROP FRAME

001  AX       V     C        00:00:01:00 00:00:03:00 00:00:00:00 00:00:02:00
* FROM CLIP NAME: media.mp4
002  AX       V     C        00:00:03:00 00:00:03:00 00:00:02:00 00:00:02:00
002  AX       V     D    010 00:00:05:00 00:00:07:00 00:00:02:00 00:00:04:00
* FROM CLIP NAME: media.mp4
"""
    back, _ = import_edl(text, RATE24, "/x/media.mp4")
    a, b = back.clips
    assert (a.source_in, a.source_out) == (1.0, round(3.0 + 10 / 24, 4))  # the outgoing clip runs on through the dissolve
    assert (b.source_in, b.source_out) == (5.0, 7.0) and b.transition_in.kind == "dissolve" and b.transition_in.duration == round(10 / 24, 4)


# -- regression tests from the review ---------------------------------------------------------------
# an EDL as OpenTimelineIO's cmx_3600 writer turns matchcut's OTIO into: clip names "c001 shot0", reel "media", FROM CLIP urls
OTIO_STYLE = """TITLE: media - final

001  media    V     C        00:00:01:00 00:00:02:12 00:00:00:00 00:00:01:12
* FROM CLIP NAME:  c001 shot0
* FROM CLIP: file:///x/media.mp4
* OTIO TRUNCATED REEL NAME FROM: media.mp4
002  media    V     C        00:00:02:12 00:00:02:12 00:00:01:12 00:00:01:12
002  media    V     D 012    00:00:05:00 00:00:07:00 00:00:01:12 00:00:03:12
* FROM CLIP NAME:  c001 shot0
* FROM CLIP: file:///x/media.mp4
* OTIO TRUNCATED REEL NAME FROM: media.mp4
* TO CLIP NAME:  c002 shot1
* TO CLIP: file:///x/media.mp4
* OTIO TRUNCATED REEL NAME FROM: media.mp4
003  media    V     C        00:00:10:00 00:00:12:00 00:00:03:12 00:00:05:12
* FROM CLIP NAME:  c003 shot2
* FROM CLIP: file:///x/media.mp4
* OTIO TRUNCATED REEL NAME FROM: media.mp4
"""


def test_an_edl_written_from_matchcuts_otio_by_another_writer_is_read(tmp_path):
    back, notes = import_edl(OTIO_STYLE, RATE24, "/x/media.mp4")
    assert notes == []
    assert [(c.id, c.shot_index, c.source_in, c.source_out) for c in back.clips] == [
        ("c001", 0, 1.0, 3.0), ("c002", 1, 5.0, 7.0), ("c003", 2, 10.0, 12.0)]
    assert back.clips[1].transition_in.kind == "dissolve" and back.clips[1].transition_in.duration == 0.5
    check_equivalent(synthetic(), back, RATE24)


def test_media_is_other_only_on_positive_evidence_of_a_different_file():
    def one(extra: str, reel: str = "A001", name: str = "") -> str:
        names = f"* FROM CLIP NAME: {name}\n" if name else ""
        return (f"TITLE: t\n001  {reel:<8} V     C        00:00:01:00 00:00:03:00 00:00:00:00 00:00:02:00\n{names}{extra}\n"
                f"002  AX       V     C        00:00:05:00 00:00:07:00 00:00:02:00 00:00:04:00\n* FROM CLIP NAME: media.mp4\n")

    def n_clips(text: str) -> int:
        try:
            return len(import_edl(text, RATE24, "/x/media.mp4")[0].clips)
        except InterchangeError:
            return 0

    assert n_clips(one("")) == 2  # no evidence at all: accepted
    assert n_clips(one("* FROM CLIP: file:///x/media.mp4")) == 2 and n_clips(one("* SOURCE FILE: /elsewhere/media.mp4")) == 2  # same file name
    assert n_clips(one("* FROM CLIP: file:///x/other.mov")) == 1 and n_clips(one("* SOURCE FILE: /a/b/broll.mov")) == 1  # another file
    assert n_clips(one("", name="c010 shot4")) == 2  # matchcut's own name, whatever the reel
    assert n_clips(one("", reel="media")) == 2 and n_clips(one("", reel="med")) == 2  # a reel that is the start of the source's name
    assert n_clips(one("", name="broll.mov")) == 1 and n_clips(one("", name="media.mp4")) == 2
    # a path comment decides, even against matchcut-looking names
    assert n_clips(one("* FROM CLIP: file:///x/other.mov", name="c010 shot4")) == 1


def test_matchcut_source_comment_does_not_override_the_workspace_source():
    text = export_edl(synthetic(), "final", fake_src(), "t")  # names /x/media.mp4
    back, notes = import_edl(text, RATE24, "/x/media.mp4")
    assert back.source == "/x/media.mp4" and notes == []
    back, notes = import_edl(text, RATE24, "/moved/media.mp4")
    assert back.source == "/moved/media.mp4" and notes == ["the EDL names the source /x/media.mp4; the workspace's /moved/media.mp4 is used"]


SPLIT = """TITLE: x
FCM: NON-DROP FRAME

001  AX V     C        00:00:01:00 00:00:03:00 01:00:00:00 01:00:02:00
001  AX A     C        00:00:01:00 00:00:03:00 01:00:00:00 01:00:02:00
* FROM CLIP NAME: media.mp4
002  AX V     C        00:00:05:00 00:00:07:00 01:00:02:00 01:00:04:00
* FROM CLIP NAME: media.mp4
"""


def test_video_and_audio_lines_under_one_event_number_are_separate_events():
    back, notes = import_edl(SPLIT, RATE24, "/x/media.mp4")
    assert [(c.source_in, c.source_out) for c in back.clips] == [(1.0, 3.0), (5.0, 7.0)]
    assert notes == ["event 001: audio event (track A) ignored"]
    # the other order, and a dissolve on both tracks
    swapped = SPLIT.replace("001  AX V", "001  AX X").replace("001  AX A", "001  AX V").replace("001  AX X", "001  AX A")
    back, notes = import_edl(swapped, RATE24, "/x/media.mp4")
    assert len(back.clips) == 2 and notes == ["event 001: audio event (track A) ignored"]
    both = """TITLE: x
001  AX V     C        00:00:01:00 00:00:03:00 01:00:00:00 01:00:02:00
002  AX V     C        00:00:03:00 00:00:03:00 01:00:02:00 01:00:02:00
002  AX A     C        00:00:03:00 00:00:03:00 01:00:02:00 01:00:02:00
002  AX V     D    010 00:00:05:00 00:00:07:00 01:00:02:00 01:00:04:00
002  AX A     D    010 00:00:05:00 00:00:07:00 01:00:02:00 01:00:04:00
* FROM CLIP NAME: media.mp4
"""
    back, notes = import_edl(both, RATE24, "/x/media.mp4")
    assert len(back.clips) == 2 and back.clips[1].transition_in.kind == "dissolve" and back.clips[1].transition_in.duration == round(10 / 24, 4)
    assert len([n for n in notes if "audio event" in n]) == 1  # the A pair is one event


CAM = """TITLE: x
FCM: NON-DROP FRAME

001  A001     V     C        10:00:01:00 10:00:03:00 01:00:00:00 01:00:02:00
* FROM CLIP NAME: media.mp4
002  A001     V     C        10:00:05:00 10:00:07:00 01:00:02:00 01:00:04:00
* FROM CLIP NAME: media.mp4
"""


def test_source_timecode_of_a_camera_edl_is_conformed_to_the_media_start():
    src = replace(fake_src(RATE24), start_tc_frames=10 * 3600 * 24)
    back, notes = import_edl(CAM, RATE24, src.path, {0: (0, 3.5), 1: (4.0, 8.0)}, src)
    assert [(c.source_in, c.source_out, c.shot_index) for c in back.clips] == [(1.0, 3.0, 0), (5.0, 7.0, 1)] and notes == []
    # the same file against media without a timecode: the clips lie far outside it, and in no shot, and the notes say so
    plain = fake_src(RATE24)
    back, notes = import_edl(CAM, RATE24, plain.path, {0: (0, 3.5), 1: (4.0, 8.0)}, plain)
    assert [c.shot_index for c in back.clips] == [-1, -1]
    assert sum("outside the media (0-1440 frames)" in n and "start timecode" in n for n in notes) == 2
    assert sum("lies in no shot" in n for n in notes) == 2
    assert all("\n" not in n for n in notes)


def test_music_only_writes_video_events_and_says_so():
    from matchcut.pipeline.config import MusicConfig
    text = export_edl(synthetic(), "final", fake_src(), "t", MusicConfig(path="/x/score.wav", only=True))
    assert all(e[2] == "V" for e in events(text)) and "* MATCHCUT AUDIO none: music.only" in text
    text = export_edl(synthetic(), "final", fake_src(), "t", MusicConfig(path="/x/score.wav", only=False))
    assert all(e[2] == "AA/V" for e in events(text)) and "MATCHCUT AUDIO" not in text
    assert all(e[2] == "AA/V" for e in events(export_edl(synthetic(), "final", fake_src(), "t", MusicConfig())))


def test_invalid_drop_frame_numbers_and_overlapping_events():
    bad = "TITLE: t\nFCM: DROP FRAME\n001  AX       V     C        00:01:00;00 00:01:10;00 00:00:00;00 00:00:10;00\n"
    with pytest.raises(InterchangeError, match=r"^invalid EDL: line 3: 00:01:00;00 is not a valid drop-frame timecode$"):
        import_edl(bad, NTSC, "/x/media.mp4")
    ok = bad.replace("00:01:00;00", "00:01:00;02").replace("00:01:10;00", "00:01:10;02")
    assert len(import_edl(ok, NTSC, "/x/media.mp4")[0].clips) == 1
    assert len(import_edl(bad.replace("00:01:00;00", "00:10:00;00").replace("00:01:10;00", "00:10:10;00"), NTSC, "/x/media.mp4")[0].clips) == 1
    overlap = """TITLE: t
001  AX       V     C        00:00:01:00 00:00:03:00 00:00:00:00 00:00:02:00
002  AX       V     C        00:00:05:00 00:00:07:00 00:00:01:00 00:00:03:00
"""
    back, notes = import_edl(overlap, RATE24, "/x/media.mp4")
    assert len(back.clips) == 2 and notes == ["event 002 starts 24 frames before the previous video event ends (overlapping record times): read in file order"]


# -- review round 2 ---------------------------------------------------------------------------------
def _reel_edl(reel: str, name: str, second_name: str) -> str:
    return (f"TITLE: t\nFCM: NON-DROP FRAME\n\n"
            f"001  {reel:<8} V     C        00:00:01:00 00:00:03:00 00:00:00:00 00:00:02:00\n* FROM CLIP NAME: {name}\n"
            f"002  {reel:<8} V     C        00:00:05:00 00:00:07:00 00:00:02:00 00:00:04:00\n* FROM CLIP NAME: {second_name}\n")


def test_a_reel_names_the_roll_so_it_never_outvotes_a_file_name():
    cam = "/x/A001C003_230101.mov"
    # another clip on the same roll is another file, whatever the reel
    back, notes = import_edl(_reel_edl("A001", "A001C001_230101.mov", "A001C003_230101.mov"), RATE24, cam)
    assert len(back.clips) == 1 and back.clips[0].source_in == 5.0 and notes == ["event 001: other media A001C001_230101.mov ignored"]
    back, notes = import_edl(_reel_edl("media", "broll.mov", "media.mp4"), RATE24, "/x/media.mp4")
    assert len(back.clips) == 1 and notes == ["event 001: other media broll.mov ignored"]
    # the reel is the fallback only without a name, or with a name that is no file name
    assert len(import_edl(_reel_edl("A001", "Interview take 3", "Interview take 3"), RATE24, cam)[0].clips) == 2
    assert len(import_edl(_reel_edl("media", "", ""), RATE24, "/x/media.mp4")[0].clips) == 2
    with pytest.raises(InterchangeError, match="no event of the EDL references"):
        import_edl(_reel_edl("B001", "Interview take 3", "Interview take 3"), RATE24, cam)
    # OpenTimelineIO's cmx_3600 writer (reel "media", names "c001 shot0") is still the source
    assert len(import_edl(OTIO_STYLE.replace("* FROM CLIP: file:///x/media.mp4\n", "").replace("* TO CLIP: file:///x/media.mp4\n", ""),
                          RATE24, "/x/media.mp4")[0].clips) == 3


def test_a_dotted_number_in_a_clip_name_is_not_a_file_extension():
    cam = "/x/A001C003_230101.mov"
    # "Sc 12.3" and "Take v1.2" are names, not files: on the source's roll they are the source (the reel decides), never other media
    for name in ("Sc 12.3", "Take v1.2", "A001 take 2.10"):
        back, notes = import_edl(_reel_edl("A001", name, name), RATE24, cam)
        assert len(back.clips) == 2 and notes == [], name
    # a real extension that starts with a letter is still a file name: another file on the same roll
    with pytest.raises(InterchangeError, match="no event of the EDL references"):
        import_edl(_reel_edl("A001", "A001C001.mov", "A001C001.mov"), RATE24, cam)
    with pytest.raises(InterchangeError, match="no event of the EDL references"):
        import_edl(_reel_edl("B001", "Sc 12.3", "Sc 12.3"), RATE24, cam)  # a name that is no file name falls back to the reel, which is another roll


def test_a_comment_of_the_audio_line_does_not_overwrite_the_video_lines_name():
    text = """TITLE: x
FCM: NON-DROP FRAME

001  AX V     C        00:00:01:00 00:00:03:00 01:00:00:00 01:00:02:00
* FROM CLIP NAME: media.mp4
001  AX A     C        00:00:01:00 00:00:03:00 01:00:00:00 01:00:02:00
* FROM CLIP NAME: vo.wav
002  AX V     C        00:00:05:00 00:00:07:00 01:00:02:00 01:00:04:00
* FROM CLIP NAME: media.mp4
"""
    back, notes = import_edl(text, RATE24, "/x/media.mp4")
    assert [(c.source_in, c.source_out) for c in back.clips] == [(1.0, 3.0), (5.0, 7.0)]
    assert notes == ["event 001: audio event (track A) ignored"]


# -- the source footage: its exact path first, the same file name only when no event has it (final review T2-a) -------------
def test_the_exact_path_beats_an_event_of_the_same_file_name_in_another_folder():
    def edl(first: str, second: str) -> str:
        return ("TITLE: t\n"
                "001  AX       V     C        00:00:01:00 00:00:03:00 00:00:00:00 00:00:02:00\n" + first + "\n"
                "002  AX       V     C        00:00:05:00 00:00:07:00 00:00:02:00 00:00:04:00\n" + second + "\n")

    back, notes = import_edl(edl("* SOURCE FILE: /card2/media.mp4", "* SOURCE FILE: /x/media.mp4"), RATE24, "/x/media.mp4")
    assert [c.source_in for c in back.clips] == [5.0] and notes == ["event 001: other media /card2/media.mp4 ignored"]
    # no exact path in the file: the same name is the source, said once
    back, notes = import_edl(edl("* SOURCE FILE: /card2/media.mp4", "* FROM CLIP: file:///moved/media.mp4"), RATE24, "/x/media.mp4")
    assert [c.source_in for c in back.clips] == [1.0, 5.0]
    assert notes == ["no clip references /x/media.mp4: the clips whose file name is the same were taken for the source footage"]
    # an event with only a name is as before
    back, notes = import_edl(edl("* FROM CLIP NAME: media.mp4", "* FROM CLIP NAME: media.mp4"), RATE24, "/x/media.mp4")
    assert len(back.clips) == 2 and notes == []
