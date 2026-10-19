"""FCPXML 1.10 export and import.  The 1.10 DTD (``FCPXML_DTD``) with ``xmllint`` is used where present and skipped otherwise."""
from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from fractions import Fraction
from typing import Optional

import pytest

from matchcut.interchange import InterchangeError
from matchcut.interchange.common import SourceInfo, junction_ids, layout, record_frames, rt, source_info
from matchcut.interchange.fcpxml import CROSS_DISSOLVE_UID, FADE_TO_COLOR_UID, export_fcpxml, fcpxml_junction_ids, import_fcpxml
from matchcut.interchange.otio import export_otio
from matchcut.pipeline.config import MusicConfig
from matchcut.pipeline.timeline import Clip, Timeline, Transition

from .conftest import FCPXML_DTD, check_equivalent, exported, make_cover_art_mp3, write_wav
from .test_common import NTSC, synthetic
from .test_otio import fake_src

HAVE_DTD = shutil.which("xmllint") is not None and FCPXML_DTD.exists()
RATE24 = Fraction(24)


def fx(tl: Timeline, stage: str = "final", junctions=(), src: Optional[SourceInfo] = None, music=None, music_src=None, name: str = "media") -> str:
    return export_fcpxml([(stage, tl, list(junctions))], src or fake_src(), music, music_src, name)


def dtd_valid(xml: str, tmp_path, label: str = "x") -> None:
    """Assert DTD validity when xmllint and the DTD are there; a no-op otherwise (``test_fcpxml_is_dtd_valid`` skips visibly)."""
    if not HAVE_DTD:
        return
    p = tmp_path / f"{label}.fcpxml"
    p.write_text(xml)
    r = subprocess.run(["xmllint", "--noout", "--dtdvalid", str(FCPXML_DTD), str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:2000]


def spine_of(xml: str, n: int = 0) -> ET.Element:
    return ET.fromstring(xml).findall(".//project")[n].find("sequence/spine")


def world_doc(world, stage, music=None, music_src=None):
    tl, _ = exported(world, stage)
    return tl, export_fcpxml([(stage, tl, world.jb[stage])], world.src, music, music_src, "demo")


# -- structure -----------------------------------------------------------------------------------
def test_dissolve_overlap_to_centred_conversion_is_exact():
    tl = synthetic()
    xml = fx(tl)
    spine = spine_of(xml)
    assert [e.tag for e in spine] == ["asset-clip", "transition", "asset-clip", "asset-clip"]
    assert abs(tl.record_start(1) * 24 - 36) < 1e-9  # matchcut: the dissolve starts 12 f before A's end (48 f - 12 f)
    assert spine[1].get("offset") == "3/2s" and spine[1].get("duration") == "1/2s"  # 36/24 s = record_start(1), 6 f before the cut
    assert spine[2].get("offset") == "7/4s" and spine[3].get("offset") == "7/2s"  # the cut (42 f); C follows B's visible range (84 f)
    assert (spine[0].get("start"), spine[0].get("duration")) == ("1s", "7/4s") and spine[2].get("start") == "21/4s"
    back, notes = import_fcpxml(xml, "final")
    assert notes == []
    assert [(c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration) for c in back.clips] == \
        [(c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration) for c in tl.clips]
    check_equivalent(tl, back, RATE24)


def test_document_structure_resources_and_metadata():
    src = fake_src()
    root = ET.fromstring(fx(synthetic(), name="demo"))
    assert root.tag == "fcpxml" and root.get("version") == "1.10"
    fmt = root.find("resources/format")
    assert (fmt.get("frameDuration"), fmt.get("width"), fmt.get("height")) == ("1/24s", "1920", "1080")
    asset = root.find("resources/asset")
    assert asset.get("hasAudio") == "1" and asset.get("hasVideo") == "1" and asset.get("format") == fmt.get("id")
    assert asset.get("duration") == "60s" and asset.get("start") == "0s" and asset.find("media-rep").get("src") == src.url
    assert asset.get("uid").startswith("com.matchcut.source.")  # a stable, content-keyed uid
    other = SourceInfo(**{**src.__dict__, "size": src.size + 1})
    assert ET.fromstring(fx(synthetic(), src=other)).find("resources/asset").get("uid") != asset.get("uid")
    assert ET.fromstring(fx(synthetic(), src=src)).find("resources/asset").get("uid") == asset.get("uid")
    eff = root.find("resources/effect")
    assert (eff.get("name"), eff.get("uid")) == ("Cross Dissolve", "FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265") and CROSS_DISSOLVE_UID == eff.get("uid")
    assert root.find("library/event").get("name") == "demo"
    proj = root.find("library/event/project")
    assert proj.get("name") == "demo - final"
    seq = proj.find("sequence")
    assert (seq.get("format"), seq.get("duration"), seq.get("tcStart")) == (fmt.get("id"), "11/2s", "0s")  # 132 frames
    smd = {m.get("key"): m.get("value") for m in seq.findall("metadata/md")}
    assert smd["com.matchcut.stage"] == "final" and smd["com.matchcut.provenance_file"] == "provenance.json" and "com.matchcut.fade_in" in smd
    clips = spine_of(ET.tostring(root, encoding="unicode")).findall("asset-clip")
    md = {m.get("key"): m.get("value") for m in clips[0].findall("metadata/md")}
    assert md == {"com.matchcut.clip_key": "0@1.000-3.000", "com.matchcut.clip_id": "c001", "com.matchcut.shot_index": "0",
                  "com.matchcut.beat_id": "", "com.matchcut.stage": "final"}
    assert all(m.get("type") == "string" for m in clips[0].findall("metadata/md"))
    assert clips[0].get("name") == "c001 shot0" and clips[0].get("ref") == asset.get("id")


def test_no_audio_source_is_declared_without_audio():
    asset = ET.fromstring(fx(synthetic(), src=fake_src(audio=False))).find("resources/asset")
    assert asset.get("hasAudio") == "0"


def test_dip_to_black_is_exported_as_a_labelled_fade_to_color(tmp_path):
    tl = synthetic()
    tl.clips[1].transition_in = Transition("dip_to_black", 0.5)
    xml = fx(tl)
    root = ET.fromstring(xml)
    effects = {e.get("name"): e.get("uid") for e in root.findall("resources/effect")}
    assert effects == {"Cross Dissolve": CROSS_DISSOLVE_UID, "Fade To Color": FADE_TO_COLOR_UID}
    assert FADE_TO_COLOR_UID == "FxPlug:F779C565-486D-4633-8035-0374B4DB8F5C"
    tr = root.find(".//spine/transition")
    assert tr.get("name") == "Dip To Black" and tr.find("filter-video/param").get("value") == "0 0 0 1"
    assert any(m.get("key") == "com.matchcut.unverified" and "UNVERIFIED" in m.get("value") for m in tr.findall("metadata/md"))
    assert import_fcpxml(xml)[0].clips[1].transition_in.kind == "dip_to_black"
    assert "Fade To Color" not in fx(synthetic())  # only declared when a dip is used
    dtd_valid(xml, tmp_path, "dip")


def test_ntsc_rates_use_exact_rationals(tmp_path):
    fps = float(NTSC)
    tl = Timeline(source="/x/m.mp4", fps=fps, stage="final")
    tl.clips = [Clip("c001", 0, 1.0, 3.0), Clip("c002", 1, 5.0, 7.0, transition_in=Transition("dissolve", 14 / fps)), Clip("c003", 2, 10.0, 12.0)]
    xml = fx(tl, src=fake_src(NTSC))
    assert 'frameDuration="1001/30000s"' in xml
    back, notes = import_fcpxml(xml, "final")
    assert notes == [] and back.fps == pytest.approx(fps)
    check_equivalent(tl, back, NTSC)
    dtd_valid(xml, tmp_path, "ntsc")


def test_junctions_have_a_marker_a_keyword_and_a_truncated_note_on_the_incoming_clip(world):
    tl, xml = world_doc(world, "final")
    rows = layout(tl, world.src.rate)
    ids = junction_ids(tl)
    clips = spine_of(xml).findall("asset-clip")
    js = world.jb["final"]
    assert len(js) == len(tl.clips) - 1 and clips[0].find("marker") is None and clips[0].find("keyword") is None
    for j in js:
        c, row = clips[j.index + 1], rows[j.index + 1]
        m, k = c.find("marker"), c.find("keyword")
        assert m.get("value") == f"{ids[j.index]} (s{j.out_shot} -> s{j.in_shot})" and m.get("note") == j.why()[:240] and len(m.get("note")) <= 240
        assert (m.get("start"), m.get("duration")) == (rt(row.vis_in, world.src.rate), rt(1, world.src.rate))
        assert k.get("value") == f"mc:{ids[j.index]}" and k.get("start") == m.get("start") and k.get("duration") == rt(row.dur, world.src.rate)
    assert fcpxml_junction_ids(xml, "final") == ids == [f"s{j.out_shot}-s{j.in_shot}" for j in js]
    with pytest.raises(InterchangeError, match="no project for stage 'nope'"):
        fcpxml_junction_ids(xml, "nope")
    with pytest.raises(InterchangeError, match="invalid FCPXML"):
        fcpxml_junction_ids("not xml")


def test_a_long_why_line_is_cut_to_240_characters_and_control_characters_are_dropped():
    class J:  # the little a Junction offers an exporter
        index, out_shot, in_shot = 0, 0, 1

        def why(self):
            return "x" * 400 + "\x00\x0b"

    xml = fx(synthetic(), junctions=[J()])
    note = ET.fromstring(xml).find(".//marker").get("note")
    assert len(note) == 240 and note.endswith("...")
    assert "\x00" not in xml and "\x0b" not in xml


# -- DTD and round trips -------------------------------------------------------------------------
def test_fcpxml_is_dtd_valid(world, tmp_path):
    if not HAVE_DTD:
        pytest.skip("xmllint or the FCPXML 1.10 DTD (FCPXML_DTD) missing")
    stages = [(n, exported(world, n)[0], world.jb[n]) for n in ("roughcut", "final")]
    dtd_valid(export_fcpxml(stages, world.src, None, None, "demo"), tmp_path, "plain")
    msrc = source_info(str(write_wav(tmp_path / "score.wav", 30.0)), audio_only=True)
    dtd_valid(export_fcpxml(stages, world.src, MusicConfig(path="x", start=2.0, gain_db=-6.5), msrc, "demo"), tmp_path, "music")
    dtd_valid(export_fcpxml(stages, world.src, MusicConfig(path="x", only=True), msrc, "demo"), tmp_path, "music_only")


def test_fcpxml_is_dtd_valid_and_reimports(world, tmp_path):
    stages = [(n, exported(world, n)[0], world.jb[n]) for n in ("roughcut", "final")]
    xml = export_fcpxml(stages, world.src, None, None, "demo")
    assert '<fcpxml version="1.10">' in xml and xml.count("<project ") == 2
    assert xml.count("<marker ") == sum(len(world.jb[s]) for s, _, _ in stages)
    dtd_valid(xml, tmp_path, "world")
    for stage, tl, js in stages:
        back, notes = import_fcpxml(xml, stage)
        assert notes == []
        check_equivalent(tl, back, world.src.rate)
        assert [c.origin for c in back.clips] == [c.origin for c in tl.clips]
        assert fcpxml_junction_ids(xml, stage) == [f"s{j.out_shot}-s{j.in_shot}" for j in js]


def test_fcpxml_reimport_without_xmllint(world):
    for stage in ("roughcut", "final"):
        tl, xml = world_doc(world, stage)
        back, notes = import_fcpxml(xml, stage, world.bounds)
        assert notes == []
        check_equivalent(tl, back, world.src.rate)
        assert [c.id for c in back.clips] == [c.id for c in tl.clips]
        assert [c.beat_id for c in back.clips] == [c.beat_id for c in tl.clips]
        assert [c.origin for c in back.clips] == [c.origin for c in tl.clips] and all(c.origin for c in back.clips)
        assert back.stage == stage and back.source == world.src.path


def test_fcpxml_import_survives_stripped_metadata(world):
    """If an editor drops our md keys and renames clips, shots come from the source ranges against shot bounds."""
    tl, xml = world_doc(world, "roughcut")
    root = ET.fromstring(xml)
    for el in root.iter():
        for md in el.findall("metadata"):
            el.remove(md)
        if el.tag == "asset-clip":
            el.set("name", "edited name")
    back, notes = import_fcpxml(ET.tostring(root, encoding="unicode"), bounds=world.bounds)  # the first project is read
    assert [c.shot_index for c in back.clips] == [c.shot_index for c in tl.clips]
    assert [c.id for c in back.clips] == [f"h{i:03d}" for i in range(1, len(tl.clips) + 1)]  # ids are lost: they come back as new clips
    assert back.source == world.src.path  # from the asset's media-rep
    check_equivalent(tl, back, world.src.rate)


def test_import_fcpxml_parses_arbitrary_rationals():
    xml = """<?xml version="1.0"?><!DOCTYPE fcpxml><fcpxml version="1.10"><resources>
      <format id="r1" frameDuration="1001/30000s"/><asset id="r2" name="m"><media-rep src="file:///x/m.mp4"/></asset>
      <effect id="r3" name="Cross Dissolve" uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/></resources>
      <library><event name="e"><project name="p"><sequence format="r1" duration="1s"><spine>
        <asset-clip ref="r2" name="shot4 a" offset="0s" start="12s" duration="1001/300s"/>
        <transition offset="2002/3000s" duration="1001/3000s"><filter-video ref="r3" name="Cross Dissolve"/></transition>
        <asset-clip ref="r2" name="shot7 b" offset="3003/3000s" start="20020/3000s" duration="6006/6000s"/>
      </spine></sequence></project></event></library></fcpxml>"""
    tl, notes = import_fcpxml(xml)
    f = 30000 / 1001
    assert abs(tl.fps - f) < 1e-9 and [c.shot_index for c in tl.clips] == [4, 7]
    assert tl.source == "/x/m.mp4" and notes == []
    # a: 12 s -> frame 360. The transition (20..30 f) ends exactly at the cut (b.offset = 30 f), so it is all "before the cut":
    # in_off = 10, out_off = 0; b.start = 20020/3000 s = frame 200, hence b.source_in = frame 190.
    a, b = tl.clips
    assert round(a.source_in * f) == 360
    assert b.transition_in.kind == "dissolve" and round(b.transition_in.duration * f) == 10
    assert round(b.source_in * f) == 190 and round(a.source_out * f) == 460


def fcp_style(xml: str, base: int = 3000, drop_md: bool = False) -> str:
    """Rewrite a file the way an editor's re-export looks: every time on a common ``/base`` grid, renumbered resource ids."""
    root = ET.fromstring(xml)
    ids = {}
    for el in root.iter():
        for key in ("offset", "start", "duration", "frameDuration", "tcStart", "audioStart", "audioDuration"):
            v = el.get(key)
            if v and v.endswith("s"):
                x = Fraction(v[:-1]) * base
                assert x.denominator == 1, (key, v)
                el.set(key, f"{int(x)}/{base}s")
        if el.get("id"):
            ids[el.get("id")] = "r" + str(int(el.get("id")[1:]) * 10 + 7)
    for el in root.iter():
        for key in ("id", "ref", "format"):
            if el.get(key) in ids:
                el.set(key, ids[el.get(key)])
        if drop_md:
            for md in el.findall("metadata"):
                el.remove(md)
    return ET.tostring(root, encoding="unicode")


def test_a_file_with_rewritten_ids_and_time_base_reimports_to_the_same_clips(world):
    for stage in ("roughcut", "final"):
        tl, xml = world_doc(world, stage)
        styled = fcp_style(xml)
        assert 'frameDuration="120/3000s"' in styled and 'id="r17"' in styled and "r1\"" not in styled
        back, notes = import_fcpxml(styled, stage)
        assert notes == []
        check_equivalent(tl, back, world.src.rate)
        assert [c.origin for c in back.clips] == [c.origin for c in tl.clips]
        # and when the metadata is gone as well, the clips are still the same shots at the same frames
        bare, _ = import_fcpxml(fcp_style(xml, drop_md=True), bounds=world.bounds)
        check_equivalent(tl, bare, world.src.rate)


def test_a_dissolve_survives_a_rewritten_time_base():
    tl = synthetic()
    back, notes = import_fcpxml(fcp_style(fx(tl), 6000), "final")
    assert notes == []
    check_equivalent(tl, back, RATE24)


def test_two_stages_produce_two_projects(world, tmp_path):
    stages = [(n, exported(world, n)[0], world.jb[n]) for n in ("roughcut", "final")]
    xml = export_fcpxml(stages, world.src, None, None, "demo")
    root = ET.fromstring(xml)
    assert [p.get("name") for p in root.findall("library/event/project")] == ["demo - roughcut", "demo - final"]
    assert len(root.findall("library/event")) == 1 and len(root.findall("resources/asset")) == 1 and len(root.findall("resources/format")) == 1
    assert len(spine_of(xml, 0).findall("asset-clip")) == len(stages[0][1].clips) and len(spine_of(xml, 1).findall("asset-clip")) == len(stages[1][1].clips)
    # the first project is read by default, a stage by name, an unknown stage is an error
    assert import_fcpxml(xml)[0].stage == "roughcut" and import_fcpxml(xml, "final")[0].stage == "final"
    with pytest.raises(InterchangeError, match=r"no project for stage 'draft' \(the file has: roughcut, final\)"):
        import_fcpxml(xml, "draft")
    # the stage is also found by the project name when the metadata is gone
    root2 = ET.fromstring(xml)
    for el in root2.iter():
        for md in el.findall("metadata"):
            el.remove(md)
    assert len(import_fcpxml(ET.tostring(root2, encoding="unicode"), "final", world.bounds)[0].clips) == len(stages[1][1].clips)


# -- music ---------------------------------------------------------------------------------------
def test_music_lane_has_the_cut_duration_gain_and_start(world, music_file, tmp_path):
    music = MusicConfig(path=str(music_file), start=2.0, gain_db=-6.0, duck=False)
    msrc = source_info(str(music_file), audio_only=True)
    tl, xml = world_doc(world, "final", music, msrc)
    rows = layout(tl, world.src.rate)
    root = ET.fromstring(xml)
    spine = spine_of(xml)
    assert all(e.get("lane") is None for e in spine)
    first = spine.findall("asset-clip")[0]
    conn = first.findall("asset-clip")
    assert len(conn) == 1 and conn[0].get("lane") == "-1" and len(root.findall(".//asset-clip[@lane]")) == 1
    total = record_frames(rows)
    # connected to the first clip: its offset is in that clip's own time, where the cut starts at the clip's start
    assert conn[0].get("offset") == first.get("start") == rt(rows[0].vis_in, world.src.rate)
    assert conn[0].get("start") == "2s" and conn[0].get("duration") == rt(total, world.src.rate)  # music.start = 2.0 s; the cut's duration
    assert conn[0].find("adjust-volume").get("amount") == "-6dB"
    asset = next(a for a in root.findall("resources/asset") if a.get("id") == conn[0].get("ref"))
    assert (asset.get("hasAudio"), asset.get("hasVideo"), asset.get("duration"), asset.get("start")) == ("1", "0", "30s", "0s")
    assert asset.find("media-rep").get("src") == msrc.url and asset.get("format") is None
    md = {m.get("key"): m.get("value") for m in conn[0].findall("metadata/md")}
    assert md == {"com.matchcut.kind": "music", "com.matchcut.gain_db": "-6.0", "com.matchcut.duck": "False", "com.matchcut.start": "2.0",
                  "com.matchcut.fade_out": "2.0", "com.matchcut.only": "False"}
    assert all(e.get("srcEnable") is None for e in spine.findall("asset-clip"))  # the source audio plays
    # our own music lane is not an ignored item
    back, notes = import_fcpxml(xml, "final")
    assert notes == [] and len(back.clips) == len(tl.clips)
    check_equivalent(tl, back, world.src.rate)
    dtd_valid(xml, tmp_path, "music")


def test_music_gain_is_written_as_a_plain_decibel_amount(world, music_file):
    msrc = source_info(str(music_file), audio_only=True)
    for gain, text in ((-8.0, "-8dB"), (-7.5, "-7.5dB"), (0.0, "0dB"), (3.25, "3.25dB"), (-0.00001, "0dB")):
        _, xml = world_doc(world, "final", MusicConfig(path=str(music_file), gain_db=gain), msrc)
        assert ET.fromstring(xml).find(".//asset-clip[@lane='-1']/adjust-volume").get("amount") == text


def test_music_clip_is_clamped_to_the_track_like_the_renderer(world, tmp_path):
    short = write_wav(tmp_path / "short.wav", 5.0)  # 125 frames at 25 fps; the cut is longer
    tl, xml = world_doc(world, "final", MusicConfig(path=str(short), start=3.0), source_info(str(short), audio_only=True))
    assert record_frames(layout(tl, world.src.rate)) > 125
    conn = ET.fromstring(xml).find(".//asset-clip[@lane='-1']")
    assert (conn.get("start"), conn.get("duration")) == ("3s", "2s")  # min(total, music_duration - start) = 2 s
    dtd_valid(xml, tmp_path, "clamp")


def test_music_start_past_the_end_of_the_track_is_an_error(world, tmp_path):
    short = write_wav(tmp_path / "short.wav", 5.0)
    for start in (5.0, 7.5):
        with pytest.raises(InterchangeError, match=r"^music\.start \(%.1fs\) is past the end of the track \(5\.0s\)$" % start):
            world_doc(world, "final", MusicConfig(path=str(short), start=start), source_info(str(short), audio_only=True))


def test_music_from_a_file_with_cover_art_uses_the_audio_length(world, tmp_path):
    mp3 = make_cover_art_mp3(tmp_path / "cover.mp3", 10.0)
    _, xml = world_doc(world, "final", MusicConfig(path=str(mp3), start=0.0), source_info(str(mp3), audio_only=True))
    asset = ET.fromstring(xml).findall("resources/asset")[1]
    assert abs(float(Fraction(asset.get("duration")[:-1])) - 10.0) < 0.2  # 10 s, not the cover picture's single frame


def test_music_only_disables_the_source_audio_on_the_spine(world, music_file, tmp_path):
    msrc = source_info(str(music_file), audio_only=True)
    tl, xml = world_doc(world, "final", MusicConfig(path=str(music_file), only=True), msrc)
    clips = spine_of(xml).findall("asset-clip")
    assert len(clips) == len(tl.clips) and all(c.get("srcEnable") == "video" for c in clips)  # picture only: the music lane carries the sound
    conn = clips[0].find("asset-clip")
    assert conn.get("srcEnable") is None and conn.get("enabled") is None
    md = {m.get("key"): m.get("value") for m in ET.fromstring(xml).findall(".//sequence/metadata/md")}
    assert "music.only" in md["com.matchcut.source_audio"]
    assert {m.get("key"): m.get("value") for m in conn.findall("metadata/md")}["com.matchcut.only"] == "True"
    back, notes = import_fcpxml(xml, "final")
    assert notes == [] and len(back.clips) == len(tl.clips)
    dtd_valid(xml, tmp_path, "only")
    # a source without audio has nothing to disable
    silent = SourceInfo(**{**world.src.__dict__, "has_audio": False})
    xml3 = export_fcpxml([("final", tl, [])], silent, MusicConfig(path=str(music_file), only=True), msrc, "demo")
    assert all(c.get("srcEnable") is None for c in spine_of(xml3).findall("asset-clip"))


def test_no_music_lane_without_a_configured_file(world, music_file):
    _, xml = world_doc(world, "final", MusicConfig(path=None), None)
    assert ET.fromstring(xml).find(".//asset-clip[@lane]") is None and len(ET.fromstring(xml).findall("resources/asset")) == 1
    _, xml2 = world_doc(world, "final", None, source_info(str(music_file), audio_only=True))
    assert ET.fromstring(xml2).find(".//asset-clip[@lane]") is None


def test_music_configured_but_not_probed_is_an_error(world):
    with pytest.raises(InterchangeError, match="music file not probed"):
        world_doc(world, "final", MusicConfig(path="/x/score.wav"), None)


# -- import: what is ignored, what is rejected ---------------------------------------------------
def test_import_lists_what_it_ignores():
    xml = fx(synthetic())
    root = ET.fromstring(xml)
    res = root.find("resources")
    ET.SubElement(res, "asset", id="r50", name="other", hasVideo="1").append(ET.Element("media-rep", src="file:///elsewhere/other.mp4"))
    spine = root.find(".//spine")
    first = spine.findall("asset-clip")[0]
    ET.SubElement(first, "title", lane="1", offset="1s", duration="1s", name="Lower third", ref="r99")
    ET.SubElement(first, "adjust-transform", position="1 1")
    ET.SubElement(first, "timeMap")
    gap = ET.Element("gap", name="Gap", duration="2s", offset="0s")
    spine.insert(1, gap)
    other = ET.SubElement(spine, "asset-clip", ref="r50", name="b-roll", offset="5s", start="0s", duration="1s")
    ET.SubElement(spine, "asset-clip", ref="r99", name="missing", offset="6s", start="0s", duration="1s")
    ET.SubElement(spine, "title", name="Title", offset="7s", duration="1s", ref="r99")
    back, notes = import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    assert len(back.clips) == 3 and other is not None
    text = "\n".join(notes)
    for needle in ("lane 1 title Lower third", "adjust-transform", "timeMap", "gap Gap", "b-roll: other media", "missing: unknown asset r99", "title Title"):
        assert needle in text, (needle, notes)
    assert all("\n" not in n for n in notes)
    assert [c.shot_index for c in back.clips] == [0, 1, 2]


def test_a_transition_next_to_an_ignored_clip_is_dropped_with_a_note():
    root = ET.fromstring(fx(synthetic()))
    spine = root.find(".//spine")
    spine.findall("asset-clip")[1].set("ref", "r99")  # clip B, which the dissolve leads into, is unknown
    back, notes = import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    assert len(back.clips) == 2 and all(c.transition_in.is_cut for c in back.clips)
    assert any("transition next to an ignored item dropped" in n for n in notes) and any("unknown asset" in n for n in notes)


def test_import_subtracts_the_asset_start_timecode():
    tl = synthetic()
    root = ET.fromstring(fx(tl))
    shift = 3600
    root.find("resources/asset").set("start", f"{shift}s")  # an editor re-export: the media starts at 01:00:00:00
    for c in root.iter("asset-clip"):
        c.set("start", rt(round((Fraction(c.get("start")[:-1]) + shift) * 24), RATE24))
    back, notes = import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    check_equivalent(tl, back, RATE24)
    assert any(n == f"source start timecode of {shift * 24} frames subtracted from the source times of 3 clips" for n in notes)


def test_a_disabled_clip_is_reported_and_read():
    root = ET.fromstring(fx(synthetic()))
    root.findall(".//spine/asset-clip")[1].set("enabled", "0")
    back, notes = import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    assert len(back.clips) == 3 and any("disabled" in n for n in notes)


def test_a_clip_relinked_to_the_same_file_name_is_accepted():
    root = ET.fromstring(fx(synthetic()))
    root.find("resources/asset/media-rep").set("src", "file:///moved/media.mp4")
    back, _ = import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    assert len(back.clips) == 3 and back.source == "/moved/media.mp4"


def test_import_rejects_foreign_documents():
    good = fx(synthetic())
    for bad, msg in (("not xml at all", "invalid FCPXML"), ("<other/>", "not an FCPXML document"), ("<fcpxml/>", "no project"),
                     ("<fcpxml><library><event><project><sequence><spine/></sequence></project></event></library></fcpxml>", "no usable frame duration"),
                     (None, "invalid FCPXML"), (b"\xff\xfe", "invalid FCPXML")):
        with pytest.raises(InterchangeError, match=msg) as e:
            import_fcpxml(bad)
        assert "\n" not in str(e.value)
    root = ET.fromstring(good)
    for c in root.iter("asset-clip"):
        c.set("ref", "r99")
    with pytest.raises(InterchangeError, match="no clip of the FCPXML spine references the source footage|no usable clips"):
        import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    root = ET.fromstring(good)
    root.find(".//spine").clear()
    with pytest.raises(InterchangeError, match="no clips"):
        import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    assert len(import_fcpxml(good.encode("utf-8"))[0].clips) == 3  # bytes are accepted


@pytest.mark.parametrize("damage", [
    lambda r: r.find("resources/format").set("frameDuration", "0s"),
    lambda r: r.find("resources/format").set("frameDuration", "x/y"),
    lambda r: r.find("resources/format").attrib.pop("frameDuration"),
    lambda r: r.find("resources").remove(r.find("resources/format")),
    lambda r: r.find(".//spine/asset-clip").set("duration", "garbage"),
    lambda r: r.find(".//spine/asset-clip").set("start", "1/0s"),
    lambda r: r.find(".//spine/asset-clip").set("offset", "??"),
    lambda r: r.find(".//spine/asset-clip").attrib.pop("ref"),
    lambda r: r.find(".//spine/transition").set("duration", "nonsense"),
    lambda r: r.find(".//spine/transition").set("offset", "zz"),
    lambda r: r.find(".//spine/transition").attrib.pop("duration"),
    lambda r: r.find(".//spine/asset-clip/metadata/md").set("value", "é"),
    lambda r: r.find(".//sequence/metadata/md[@key='com.matchcut.fade_in']").set("value", "abc"),
    lambda r: r.find(".//spine/asset-clip/metadata/md[@key='com.matchcut.shot_index']").set("value", "abc"),
    lambda r: r.find("resources/asset").remove(r.find("resources/asset/media-rep")),
])
def test_import_never_leaks_a_bare_exception_on_a_damaged_file(damage):
    root = ET.fromstring(fx(synthetic()))
    try:
        damage(root)
    except (KeyError, TypeError, AttributeError, ValueError):
        pytest.skip("damage does not apply")
    try:
        import_fcpxml(ET.tostring(root, encoding="unicode"))
    except InterchangeError as exc:
        assert "\n" not in str(exc) and str(exc)


def test_a_transition_without_an_offset_is_read_as_centred_on_the_cut():
    root = ET.fromstring(fx(synthetic()))
    del root.find(".//spine/transition").attrib["offset"]
    back, notes = import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    assert back.clips[1].transition_in.kind == "dissolve" and any("transition" in n and "offset" in n for n in notes)
    check_equivalent(synthetic(), back, RATE24)


# -- review round 1 ------------------------------------------------------------------------------
def with_broll_first(xml: str, n: int = 2) -> str:
    """``n`` clips of another file lead the spine."""
    root = ET.fromstring(xml)
    ET.SubElement(root.find("resources"), "asset", id="r50", name="other", hasVideo="1").append(ET.Element("media-rep", src="file:///elsewhere/other.mp4"))
    spine = root.find(".//spine")
    for i in range(n):
        spine.insert(0, ET.Element("asset-clip", ref="r50", name=f"b-roll {i}", offset="0s", start="0s", duration="1s"))
    return ET.tostring(root, encoding="unicode")


def strip_metadata(xml: str, keep_uid: bool = True) -> str:
    root = ET.fromstring(xml)
    for el in root.iter():
        for md in el.findall("metadata"):
            el.remove(md)
        if not keep_uid and el.tag == "asset":
            el.attrib.pop("uid", None)
    return ET.tostring(root, encoding="unicode")


def test_the_source_is_declared_in_the_sequence_metadata():
    src = fake_src()
    md = {m.get("key"): m.get("value") for m in ET.fromstring(fx(synthetic())).findall(".//sequence/metadata/md")}
    assert (md["com.matchcut.source"], md["com.matchcut.source_size"], md["com.matchcut.source_mtime_ns"]) == (src.path, str(src.size), str(src.mtime_ns))


def test_a_broll_inserted_first_does_not_become_the_source():
    tl = synthetic()
    back, notes = import_fcpxml(with_broll_first(fx(tl)), "final")  # the declared source wins
    assert [c.shot_index for c in back.clips] == [0, 1, 2] and back.source == "/x/media.mp4"
    check_equivalent(tl, back, RATE24)
    assert [n for n in notes if "other media" in n] == [f"clip b-roll {i}: other media /elsewhere/other.mp4 ignored" for i in (1, 0)]
    assert len(notes) == 2


def test_the_footage_is_found_without_the_declaration():
    tl = synthetic()
    # the uid our asset carries marks it even when five b-roll clips outnumber the three footage clips and all metadata is gone
    back, notes = import_fcpxml(strip_metadata(with_broll_first(fx(tl), 5)))
    assert [c.shot_index for c in back.clips] == [0, 1, 2] and sum("other media" in n for n in notes) == 5
    # without the uid as well: the clips' own metadata points at the footage
    root = ET.fromstring(with_broll_first(fx(tl), 5))
    seq = root.find(".//sequence")
    seq.remove(seq.find("metadata"))
    for a in root.iter("asset"):
        a.attrib.pop("uid", None)
    back, notes = import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    assert [c.shot_index for c in back.clips] == [0, 1, 2] and sum("other media" in n for n in notes) == 5
    # nothing at all: the most used asset
    back, notes = import_fcpxml(strip_metadata(with_broll_first(fx(tl), 2), keep_uid=False))
    assert [c.shot_index for c in back.clips] == [0, 1, 2] and sum("other media" in n for n in notes) == 2


def test_audio_only_spine_clips_split_edits_and_conform_rate_are_noted():
    tl = synthetic()
    root = ET.fromstring(fx(tl))
    spine = root.find(".//spine")
    clips = spine.findall("asset-clip")
    audio = ET.Element("asset-clip", ref=clips[0].get("ref"), name="sound", offset="0s", start="0s", duration="1s", srcEnable="audio")
    spine.insert(0, audio)
    clips[1].set("audioStart", "0s")
    clips[1].set("audioDuration", "1s")
    ET.SubElement(clips[2], "conform-rate", srcFrameRate="25")
    back, notes = import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    assert [c.shot_index for c in back.clips] == [0, 1, 2] and len(back.clips) == 3
    text = "\n".join(notes)
    assert "clip sound: srcEnable=audio (sound without picture) ignored" in text
    assert "audioStart/audioDuration (split edit) ignored" in text and "conform-rate ignored" in text


def test_a_transition_at_the_start_or_end_of_the_spine_says_so():
    root = ET.fromstring(fx(synthetic()))
    spine = root.find(".//spine")
    tr = spine.find("transition")
    lead = ET.Element("transition", offset="0s", duration="1/2s", name="Cross Dissolve")
    spine.insert(0, lead)
    spine.append(ET.Element("transition", offset="9s", duration="1/2s", name="Cross Dissolve"))
    back, notes = import_fcpxml(ET.tostring(root, encoding="unicode"), "final")
    assert tr is not None and len(back.clips) == 3 and back.clips[1].transition_in.kind == "dissolve"
    assert "transition at the start of the spine dropped" in notes and "transition at the end of the spine dropped" in notes


def test_junction_ids_follow_the_project_import_would_read(world):
    stages = [(n, exported(world, n)[0], world.jb[n]) for n in ("roughcut", "final")]
    xml = export_fcpxml(stages, world.src, None, None, "demo")
    assert fcpxml_junction_ids(xml) == fcpxml_junction_ids(xml, "roughcut")
    assert fcpxml_junction_ids(xml) == [f"s{j.out_shot}-s{j.in_shot}" for j in world.jb["roughcut"]]
    root = ET.fromstring(xml)
    for el in root.iter():
        for md in el.findall("metadata"):
            el.remove(md)
    assert fcpxml_junction_ids(ET.tostring(root, encoding="unicode"), "final") == [f"s{j.out_shot}-s{j.in_shot}" for j in world.jb["final"]]  # by project name


def test_music_clip_never_runs_past_the_real_end_of_the_track(world, tmp_path):
    track = write_wav(tmp_path / "frac.wav", 5.02)  # 125.5 frames at 25 fps: the clip may use 125, not 126
    msrc = source_info(str(track), audio_only=True)
    tl, xml = world_doc(world, "final", MusicConfig(path=str(track), start=3.0), msrc)
    conn = ET.fromstring(xml).find(".//asset-clip[@lane='-1']")
    assert (conn.get("start"), conn.get("duration")) == ("3s", "2s")
    asset = ET.fromstring(xml).findall("resources/asset")[1]
    assert asset.get("duration") == "5s"
    _, doc = tl, export_otio(tl, "final", world.src, [], MusicConfig(path=str(track), start=3.0), msrc)
    clip = doc["tracks"]["children"][2]["children"][0]
    assert clip["media_references"]["DEFAULT_MEDIA"]["available_range"]["duration"]["value"] == 125
    assert clip["source_range"]["start_time"]["value"] + clip["source_range"]["duration"]["value"] <= 125


def test_a_window_under_one_frame_has_its_own_message(world, tmp_path):
    track = write_wav(tmp_path / "five.wav", 5.0)
    msrc = source_info(str(track), audio_only=True)
    music = MusicConfig(path=str(track), start=4.99)
    msg = r"^music\.start \(4\.99s\) leaves less than one frame of music before the end of the track \(5\.00s\)$"
    with pytest.raises(InterchangeError, match=msg):
        world_doc(world, "final", music, msrc)
    tl, _ = exported(world, "final")
    with pytest.raises(InterchangeError, match=msg):
        export_otio(tl, "final", world.src, [], music, msrc)


def test_a_stage_without_clips_cannot_be_exported():
    empty = Timeline(source="/x/media.mp4", fps=24.0, stage="final")
    with pytest.raises(InterchangeError, match="^stage final has no clips to export$"):
        fx(empty)
    with pytest.raises(InterchangeError, match="^stage draft has no clips to export$"):
        export_fcpxml([("final", synthetic(), []), ("draft", empty, [])], fake_src(), None, None, "x")
    with pytest.raises(InterchangeError, match="^stage final has no clips to export$"):
        export_otio(empty, "final", fake_src(), [])
