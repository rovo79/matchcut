"""Source start timecode: probed, written by all three exporters in the media's own time, subtracted by the importers."""
from __future__ import annotations

import json
from dataclasses import replace
from fractions import Fraction

import pytest

from matchcut.interchange.common import SourceInfo, is_drop_rate, layout, parse_timecode, source_info
from matchcut.interchange.edl import _tc, export_edl, import_edl
from matchcut.interchange.fcpxml import export_fcpxml, import_fcpxml
from matchcut.interchange.otio import export_otio, import_otio
from matchcut.pipeline.timeline import Clip, Timeline, Transition

from .conftest import check_equivalent
from .test_common import NTSC
from .test_otio import fake_src

R25 = Fraction(25)


def cut(rate: Fraction) -> Timeline:
    """Three clips inside a 3 s file, the second entered by a dissolve of 5 frames, on the frame grid of ``rate``."""
    fps = float(rate)
    f = lambda n: round(n / fps, 4)  # noqa: E731
    tl = Timeline(source="/x/m.mp4", fps=fps, stage="final")
    tl.clips = [Clip("c001", 0, f(5), f(25)), Clip("c002", 1, f(30), f(50), transition_in=Transition("dissolve", 5 / fps)), Clip("c003", 2, f(55), f(70))]
    return tl


# -- probing -----------------------------------------------------------------------------------------
def test_source_info_reads_the_start_timecode(tc_footage, df_footage, footage, music_file):
    tc = source_info(str(tc_footage))
    assert (tc.rate, tc.start_tc_frames, tc.start_tc_drop) == (R25, 90000, False)
    df = source_info(str(df_footage))
    assert (df.rate, df.start_tc_frames, df.start_tc_drop) == (NTSC, 107892, True)  # 01:00:00;00 is frame 107892, not 108000
    assert source_info(str(footage)).start_tc_frames == 0
    assert source_info(str(tc_footage), audio_only=True).start_tc_frames == 0  # the music path reads sample time


def test_parse_timecode():
    assert parse_timecode("01:00:00:00", R25) == (90000, False)
    assert parse_timecode("00:00:01:12", Fraction(24)) == (36, False)
    assert parse_timecode("00:01:00;02", NTSC) == (1800, True)
    assert parse_timecode("00:01:00;00", NTSC) is None  # ;00 and ;01 do not exist at that minute
    assert parse_timecode("00:10:00;00", NTSC) == (17982, True)
    assert parse_timecode("00:01:00;02", R25) == (1502, False)  # a ';' at a non-drop rate is read as non-drop
    for bad in ("", None, "1:2", "00:00:00:99", "ab:cd:ef:gh"):
        assert parse_timecode(bad, R25) is None


def test_drop_frame_timecode_text_round_trips():
    for frames in list(range(0, 5000)) + [17981, 17982, 17983, 107892, 107893, 1000000]:
        assert parse_timecode(_tc(frames, NTSC, True), NTSC) == (frames, True), frames
        assert parse_timecode(_tc(frames, NTSC, False), NTSC) == (frames, False)
    assert _tc(1800, NTSC, True) == "00:01:00;02" and _tc(107892, NTSC, True) == "01:00:00;00"
    assert is_drop_rate(NTSC) and not is_drop_rate(R25)


# -- all three formats write the offset and read it back exactly ----------------------------------
def test_all_formats_write_the_media_timecode_and_round_trip_on_a_timecoded_file(tc_footage):
    src = source_info(str(tc_footage))
    tl = cut(R25)
    # OTIO: the media reference starts at the timecode, clip ranges are in it
    doc = export_otio(tl, "final", src, [])
    first = doc["tracks"]["children"][0]["children"][0]
    ref = first["media_references"]["DEFAULT_MEDIA"]["available_range"]
    assert (ref["start_time"]["value"], ref["duration"]["value"]) == (90000, src.duration_frames)
    assert first["source_range"]["start_time"]["value"] == 90005
    back, notes = import_otio(json.loads(json.dumps(doc)), None, src)
    check_equivalent(tl, back, R25)
    assert any("source start timecode of 90000 frames subtracted" in n for n in notes) and not any("outside" in n for n in notes)
    # FCPXML: the asset starts at the timecode, clip start / marker / keyword are asset times
    xml = export_fcpxml([("final", tl, [])], src)
    assert 'start="3600s"' in xml and 'start="18001/5s"' in xml  # asset 90000/25 s; clip 90005/25 s
    back, notes = import_fcpxml(xml, "final", None, src)
    check_equivalent(tl, back, R25)
    assert any("source start timecode of 90000 frames subtracted" in n for n in notes) and not any("outside" in n for n in notes)
    # EDL: source timecode = start timecode + frame
    text = export_edl(tl, "final", src, "t")
    assert "001  AX       AA/V  C        01:00:00:05 01:00:00:20 00:00:00:00 00:00:00:15" in text
    assert "* MATCHCUT SOURCE_START_TC 01:00:00:00" in text
    back, notes = import_edl(text, R25, src.path, None, src)
    check_equivalent(tl, back, R25)
    assert not any("outside" in n for n in notes)


def test_fcpxml_markers_keywords_and_music_follow_the_asset_time(tc_footage, music_file):
    from matchcut.pipeline.config import MusicConfig
    src = source_info(str(tc_footage))
    tl = cut(R25)
    music = MusicConfig(path=str(music_file), gain_db=-6.0)
    xml = export_fcpxml([("final", tl, [])], src, music, source_info(str(music_file), audio_only=True))
    # the music is connected inside the first clip, at that clip's own (asset) time
    assert 'lane="-1" offset="18001/5s"' in xml


def test_drop_frame_media_gets_a_drop_frame_edl_that_round_trips(df_footage):
    src = source_info(str(df_footage))
    tl = cut(NTSC)
    text = export_edl(tl, "final", src, "t")
    assert text.splitlines()[1] == "FCM: DROP FRAME"
    assert "01:00:00;05 01:00:00;20 00:00:00;00 00:00:00;15" in text and "* MATCHCUT TIMECODE NON-DROP" not in text
    back, notes = import_edl(text, NTSC, src.path, None, src)
    check_equivalent(tl, back, NTSC)
    assert not notes
    assert layout(back, NTSC)[0].in_f == 5
    # FCPXML: drop-frame media is written as DF (sequence and clips); non-drop media stays NDF
    xml = export_fcpxml([("final", tl, [])], src)
    assert 'tcFormat="NDF"' not in xml and xml.count('tcFormat="DF"') == 1 + len(tl.clips)
    assert 'tcFormat="DF"' not in export_fcpxml([("final", tl, [])], replace(src, start_tc_drop=False))
    assert import_fcpxml(xml, "final", None, src)[0].clips[0].source_in == round(5 / float(NTSC), 4)


def test_a_file_without_timecode_writes_from_frame_zero(footage):
    src = source_info(str(footage))
    assert src.start_tc_frames == 0
    text = export_edl(cut(src.rate), "final", src, "t")
    assert "SOURCE_START_TC" not in text and "  C        00:00:00:" in text.split("\n\n")[1]
    doc = export_otio(cut(src.rate), "final", src, [])
    assert doc["tracks"]["children"][0]["children"][0]["media_references"]["DEFAULT_MEDIA"]["available_range"]["start_time"]["value"] == 0


# -- clips outside the media are reported, not silently given a shot -------------------------------------
def test_importers_report_a_clip_outside_the_media_length():
    src = replace(fake_src(Fraction(24)), duration_frames=100)
    tl = cut(Fraction(24))  # ends at 70 frames: fits
    for back, notes in (import_otio(export_otio(tl, "final", src, []), None, src),
                        import_fcpxml(export_fcpxml([("final", tl, [])], src), "final", None, src),
                        import_edl(export_edl(tl, "final", src, "t"), Fraction(24), src.path, None, src)):
        assert not any("outside the media" in n for n in notes)
    short = replace(src, duration_frames=60)
    for back, notes in (import_otio(export_otio(tl, "final", src, []), None, short),
                        import_fcpxml(export_fcpxml([("final", tl, [])], src), "final", None, short),
                        import_edl(export_edl(tl, "final", src, "t"), Fraction(24), src.path, None, short)):
        assert [n for n in notes if "outside the media (0-60 frames)" in n] == [n for n in notes if "clip c003" in n and "outside" in n] != []


def test_workspace_round_trip_with_a_start_timecode_and_junction_markers(world):
    """The mock workspace as if its footage carried 01:00:00:00: markers and keywords sit at their clips' asset time."""
    import xml.etree.ElementTree as ET
    from .conftest import exported
    src = replace(world.src, start_tc_frames=90000)
    tl, _ = exported(world, "final")
    xml = export_fcpxml([("final", tl, world.jb["final"])], src)
    root = ET.fromstring(xml)
    marked = [c for c in root.iter("asset-clip") if c.find("marker") is not None]
    assert marked and all(c.find("marker").get("start") == c.get("start") == c.find("keyword").get("start") for c in marked)
    back, notes = import_fcpxml(xml, "final", world.bounds, src)
    check_equivalent(tl, back, src.rate)
    assert not any("outside" in n or "lies in no shot" in n for n in notes)
    back, _ = import_otio(export_otio(tl, "final", src, world.jb["final"]), world.bounds, src)
    check_equivalent(tl, back, src.rate)
    back, _ = import_edl(export_edl(tl, "final", src, "t"), src.rate, src.path, world.bounds, src)
    check_equivalent(tl, back, src.rate)
