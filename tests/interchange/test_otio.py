"""OTIO export/import and the provenance sidecar.  The real OpenTimelineIO library is only used by one optional test."""
from __future__ import annotations

import copy
import json
import subprocess
from fractions import Fraction

import pytest

from matchcut.interchange import InterchangeError
from matchcut.interchange.common import SourceInfo, clip_key, junction_ids, layout, record_frames, source_info, to_frames
from matchcut.interchange.otio import export_otio, import_otio
from matchcut.interchange.provenance import write_provenance
from matchcut.pipeline.config import MusicConfig
from matchcut.pipeline.timeline import Clip, Timeline, Transition, clip_origin

from .conftest import OTIO_VENV_PYTHON, check_equivalent, exported, make_cover_art_mp3, record_tolerance_frames, write_wav
from .test_common import FILM, NTSC, synthetic

OTIO_INSPECT = r"""
import json, sys, opentimelineio as otio
out = {}
for path in sys.argv[1:]:
    tl = otio.adapters.read_from_file(path)
    v = tl.video_tracks()[0]
    items = []
    for c in v:
        r = v.range_of_child(c)
        t = isinstance(c, otio.schema.Transition)
        items.append({"type": type(c).__name__, "start": r.start_time.value, "dur": r.duration.value,
                      "in_off": c.in_offset.value if t else None, "out_off": c.out_offset.value if t else None,
                      "ttype": c.transition_type if t else None})
    audio = []
    for a in tl.audio_tracks():
        audio.append({"name": a.name, "enabled": a.enabled, "items": [{"start": a.range_of_child(c).start_time.value, "dur": a.range_of_child(c).duration.value,
                                                                         "src_start": c.source_range.start_time.value if hasattr(c, "source_range") and c.source_range else None}
                                                                        for c in a if not isinstance(c, otio.schema.Transition)]})
    out[path] = {"frames": v.duration().value, "items": items, "markers": len(v.markers), "audio_tracks": len(tl.audio_tracks()), "audio": audio,
                 "marker_starts": [m.marked_range.start_time.value for m in v.markers], "marker_durs": [m.marked_range.duration.value for m in v.markers],
                 "again": json.loads(otio.adapters.write_to_string(tl, "otio_json"))}
print(json.dumps(out))
"""


def fake_src(rate: Fraction = Fraction(24), audio: bool = True) -> SourceInfo:
    return SourceInfo(path="/x/media.mp4", url="file:///x/media.mp4", rate=rate, duration_frames=int(60 * rate), width=1920, height=1080,
                      has_audio=audio, size=123, mtime_ns=456)


def tracks(doc):
    return doc["tracks"]["children"]


def val(k, f):
    return k["source_range"][f]["value"]


def doc_of(world, stage, music=None, music_src=None):
    tl, _ = exported(world, stage)
    return tl, export_otio(tl, stage, world.src, world.jb[stage], music, music_src)


# -- structure -----------------------------------------------------------------------------------
def test_dissolve_overlap_to_centred_conversion_is_exact():
    tl = synthetic()
    doc = export_otio(tl, "final", fake_src(), [])
    kids = tracks(doc)[0]["children"]
    assert [k["OTIO_SCHEMA"].split(".")[0] for k in kids] == ["Clip", "Transition", "Clip", "Clip"]
    a, tr, b, c = kids
    assert (tr["in_offset"]["value"], tr["out_offset"]["value"]) == (6, 6) and tr["transition_type"] == "SMPTE_Dissolve"
    assert (val(a, "start_time"), val(a, "duration")) == (24, 42)  # visible range ends at source_out - d/2 (3 s - 6 f)
    assert (val(b, "start_time"), val(b, "duration")) == (126, 42)  # starts at source_in + d/2 (5 s + 6 f)
    assert val(a, "duration") + val(b, "duration") + val(c, "duration") == round(tl.duration * 24) == 132
    back, notes = import_otio(doc)
    assert notes == []
    assert [(c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration) for c in back.clips] == \
        [(c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration) for c in tl.clips]


def test_document_metadata_and_clip_metadata_follow_the_spec():
    tl = synthetic()
    doc = export_otio(tl, "final", fake_src(), [])
    assert doc["OTIO_SCHEMA"] == "Timeline.1" and doc["tracks"]["OTIO_SCHEMA"] == "Stack.1"
    meta = doc["metadata"]["matchcut"]
    assert set(meta) == {"stage", "source", "rate", "fade_in", "fade_out", "provenance_file", "matchcut_version"}
    assert (meta["stage"], meta["source"], meta["rate"], meta["provenance_file"]) == ("final", "/x/media.mp4", [24, 1], "provenance.json")
    v, a1 = tracks(doc)
    assert [t["name"] for t in tracks(doc)] == ["V1", "A1"] and (v["kind"], a1["kind"]) == ("Video", "Audio")
    clips = [k for k in v["children"] if k["OTIO_SCHEMA"].startswith("Clip")]
    assert clips[0]["metadata"]["matchcut"] == {"clip_id": "c001", "clip_key": "0@1.000-3.000", "shot_index": 0, "beat_id": "", "stage": "final"}
    ref = clips[0]["media_references"]["DEFAULT_MEDIA"]
    assert ref["OTIO_SCHEMA"] == "ExternalReference.1" and ref["target_url"] == "file:///x/media.mp4"
    assert ref["available_range"]["duration"]["value"] == 60 * 24 and ref["metadata"]["matchcut"] == {"size": 123, "mtime_ns": 456}
    # the audio track mirrors the picture clip for clip, transition for transition
    assert [(k["OTIO_SCHEMA"], k.get("source_range")) for k in a1["children"]] == [(k["OTIO_SCHEMA"], k.get("source_range")) for k in v["children"]]


def test_no_audio_track_when_the_source_has_no_audio():
    doc = export_otio(synthetic(), "final", fake_src(audio=False), [])
    assert [t["name"] for t in tracks(doc)] == ["V1"]


def test_dip_to_black_is_exported_as_a_labelled_custom_transition():
    tl = synthetic()
    tl.clips[1].transition_in = Transition("dip_to_black", 0.5)
    doc = export_otio(tl, "final", fake_src(), [])
    tr = tracks(doc)[0]["children"][1]
    assert tr["transition_type"] == "Custom_Transition" and tr["metadata"]["matchcut"]["kind"] == "dip_to_black"
    assert "UNVERIFIED" in tr["metadata"]["matchcut"]["unverified"]
    back, _ = import_otio(doc)
    assert back.clips[1].transition_in.kind == "dip_to_black"


def test_ntsc_rates_use_exact_rationals_in_otio():
    fps = float(NTSC)
    tl = Timeline(source="/x/m.mp4", fps=fps, stage="final")
    tl.clips = [Clip("c001", 0, 1.0, 3.0), Clip("c002", 1, 5.0, 7.0, transition_in=Transition("dissolve", 14 / fps)), Clip("c003", 2, 10.0, 12.0)]
    doc = export_otio(tl, "final", fake_src(NTSC), [])
    assert doc["metadata"]["matchcut"]["rate"] == [30000, 1001]  # the exact rate lives in metadata; RationalTime.rate is only a float
    assert tracks(doc)[0]["children"][0]["source_range"]["start_time"]["rate"] == pytest.approx(fps)
    back, _ = import_otio(copy.deepcopy(doc))
    check_equivalent(tl, back, NTSC)
    assert back.fps == pytest.approx(fps)


def test_markers_sit_on_the_cut_of_each_junction_with_the_decision_metadata(world):
    tl, doc = doc_of(world, "final")
    rows = layout(tl, world.src.rate)
    markers = tracks(doc)[0]["markers"]
    js = world.jb["final"]
    assert len(markers) == len(js) == len(tl.clips) - 1
    for m, j in zip(markers, js):
        md = m["metadata"]["matchcut"]
        assert set(md) == {"junction_id", "why", "history", "pinned", "pin_origin", "transition"}
        assert md["junction_id"] == junction_ids(tl)[j.index] == f"s{j.out_shot}-s{j.in_shot}"
        assert md["why"] == j.why() and md["history"] == j.history() and md["pinned"] == j.pinned
        assert m["marked_range"]["start_time"]["value"] == rows[j.index + 1].cut and m["marked_range"]["duration"]["value"] == 1
        assert md["transition"]["kind"] == j.current.transition


def test_clips_carry_stage_ids_and_keys(world):
    tl, doc = doc_of(world, "final")
    clips = [k for k in tracks(doc)[0]["children"] if k["OTIO_SCHEMA"].startswith("Clip")]
    assert [k["metadata"]["matchcut"] for k in clips] == [
        {"clip_id": c.id, "clip_key": clip_key(c), "shot_index": c.shot_index, "beat_id": c.beat_id, "stage": "final"} for c in tl.clips]


# -- music ---------------------------------------------------------------------------------------
def test_music_track_a2_has_the_cut_duration_gain_and_start(world, music_file):
    music = MusicConfig(path=str(music_file), start=2.0, gain_db=-6.0, duck=False)
    msrc = source_info(str(music_file))
    tl, doc = doc_of(world, "final", music, msrc)
    assert [t["name"] for t in tracks(doc)] == ["V1", "A1", "A2"]
    a2 = tracks(doc)[2]
    assert a2["kind"] == "Audio" and len(a2["children"]) == 1
    clip = a2["children"][0]
    total = record_frames(layout(tl, world.src.rate))
    assert (val(clip, "start_time"), val(clip, "duration")) == (50, total)  # music.start = 2.0 s at 25 fps; the cut's record duration
    assert clip["media_references"]["DEFAULT_MEDIA"]["target_url"] == msrc.url
    assert clip["media_references"]["DEFAULT_MEDIA"]["available_range"]["duration"]["value"] == 750  # 30 s at the timeline's rate
    assert clip["metadata"]["matchcut"] == {"kind": "music", "gain_db": -6.0, "duck": False, "start": 2.0, "fade_out": 2.0, "only": False}
    assert tracks(doc)[1]["enabled"] is True
    assert import_otio(json.loads(json.dumps(doc)))[1] == []  # our own music track is not an "ignored" item


def test_music_clip_is_clamped_to_the_track_like_the_renderer(world, tmp_path):
    short = write_wav(tmp_path / "short.wav", 5.0)  # 125 frames at 25 fps; the cut is longer
    tl, doc = doc_of(world, "final", MusicConfig(path=str(short), start=3.0), source_info(str(short)))
    assert record_frames(layout(tl, world.src.rate)) > 125
    clip = tracks(doc)[2]["children"][0]
    assert (val(clip, "start_time"), val(clip, "duration")) == (75, 50)  # min(total, music_duration - start) = 2 s
    assert val(clip, "start_time") + val(clip, "duration") <= clip["media_references"]["DEFAULT_MEDIA"]["available_range"]["duration"]["value"]


def test_music_start_past_the_end_of_the_track_is_an_error(world, tmp_path):
    short = write_wav(tmp_path / "short.wav", 5.0)
    for start in (5.0, 7.5):
        with pytest.raises(InterchangeError, match=r"^music\.start \(%.1fs\) is past the end of the track \(5\.0s\)$" % start):
            doc_of(world, "final", MusicConfig(path=str(short), start=start), source_info(str(short)))


def test_music_from_a_file_with_cover_art_uses_the_audio_length(world, tmp_path):
    mp3 = make_cover_art_mp3(tmp_path / "cover.mp3", 10.0)
    tl, doc = doc_of(world, "final", MusicConfig(path=str(mp3), start=0.0), source_info(str(mp3), audio_only=True))
    avail = tracks(doc)[2]["children"][0]["media_references"]["DEFAULT_MEDIA"]["available_range"]["duration"]["value"]
    assert abs(avail - 250) <= 5  # 10 s at 25 fps, not the cover picture's single frame


def test_music_only_disables_the_source_audio_track(world, music_file):
    msrc = source_info(str(music_file))
    _, doc = doc_of(world, "final", MusicConfig(path=str(music_file), only=True), msrc)
    a1, a2 = tracks(doc)[1], tracks(doc)[2]
    assert a1["name"] == "A1" and a1["enabled"] is False and a1["metadata"]["matchcut"]["muted"] is True
    assert "music.only" in a1["metadata"]["matchcut"]["note"] and a2["enabled"] is True
    assert a2["children"][0]["metadata"]["matchcut"]["only"] is True
    back, notes = import_otio(json.loads(json.dumps(doc)))
    assert notes == [] and len(back.clips) == len(world.by_stage["final"].clips)


def test_no_music_track_without_a_configured_file(world, music_file):
    tl, doc = doc_of(world, "final", MusicConfig(path=None), None)
    assert [t["name"] for t in tracks(doc)] == ["V1", "A1"]
    _, doc2 = doc_of(world, "final", None, source_info(str(music_file)))
    assert [t["name"] for t in tracks(doc2)] == ["V1", "A1"]


def test_music_configured_but_not_probed_is_an_error(world):
    with pytest.raises(InterchangeError, match="music file not probed"):
        doc_of(world, "final", MusicConfig(path="/x/score.wav"), None)


# -- round trips and import ----------------------------------------------------------------------
def test_otio_roundtrips_through_import_without_a_library(world):
    for stage in ("roughcut", "final"):
        tl, doc = doc_of(world, stage)
        back, notes = import_otio(json.loads(json.dumps(doc)), world.bounds)
        assert notes == []
        check_equivalent(tl, back, world.src.rate)
        assert back.stage == stage and back.fade_in == tl.fade_in and back.fade_out == tl.fade_out
        assert [c.id for c in back.clips] == [c.id for c in tl.clips]


def test_clip_keys_roundtrip_into_clip_origin(world):
    for stage in ("roughcut", "final"):
        canonical = world.by_stage[stage]
        tl, doc = doc_of(world, stage)
        back, _ = import_otio(json.loads(json.dumps(doc)))
        # the key is the canonical (unsnapped) clip's identity, even when the snap moved the clip's range
        assert [clip_origin(c) for c in back.clips] == [clip_origin(c) for c in canonical.clips]
        assert [c.origin for c in back.clips] == [clip_origin(c) for c in canonical.clips]


def test_import_snaps_float_rates_and_survives_stripped_metadata():
    doc = export_otio(synthetic(), "final", fake_src(FILM), [])
    doc["metadata"] = {}  # an editor that dropped our metadata
    for t in tracks(doc):
        for k in t["children"]:
            k["metadata"] = {}
    for t in tracks(doc):  # a float-only 23.976 time base
        for k in t["children"]:
            for rt_ in [k.get("source_range", {}).get(f) for f in ("start_time", "duration")] + [k.get("in_offset"), k.get("out_offset")]:
                if rt_:
                    rt_["rate"] = 23.976
    back, notes = import_otio(doc)
    assert back.fps == pytest.approx(float(FILM)) and [c.shot_index for c in back.clips] == [0, 1, 2]  # shot from the clip name "… shot<N>"
    assert [c.transition_in.kind for c in back.clips] == ["cut", "dissolve", "cut"]
    assert all(c.origin == "" for c in back.clips)
    assert back.clips[1].transition_in.duration == pytest.approx(12 / float(FILM), abs=1e-3)


def test_import_maps_shots_from_bounds_when_names_and_metadata_are_gone():
    tl = synthetic()
    doc = export_otio(tl, "final", fake_src(), [])
    for k in tracks(doc)[0]["children"]:
        k["name"] = "x"
        k["metadata"] = {}
    back, _ = import_otio(doc, {0: (0.0, 4.0), 1: (4.5, 8.0), 2: (9.0, 13.0)})
    assert [c.shot_index for c in back.clips] == [0, 1, 2]


def test_import_lists_what_it_ignores_and_rejects_foreign_documents():
    tl = synthetic()
    doc = export_otio(tl, "final", fake_src(), [])
    v = tracks(doc)[0]
    other = copy.deepcopy(v["children"][3])
    other["media_references"]["DEFAULT_MEDIA"]["target_url"] = "file:///elsewhere/other.mp4"
    v["children"].append(other)
    v["children"].append({"OTIO_SCHEMA": "Gap.1", "name": "gap", "source_range": {"start_time": {"value": 0, "rate": 24}, "duration": {"value": 10, "rate": 24}}})
    v["children"][0]["effects"] = [{"OTIO_SCHEMA": "LinearTimeWarp.1", "name": "speed", "time_scalar": 2.0}]
    v2 = copy.deepcopy(v)
    v2["name"] = "V2"
    tracks(doc).insert(1, v2)
    back, notes = import_otio(doc)
    assert len(back.clips) == 3
    text = "\n".join(notes)
    assert "other media" in text and "gap" in text and "effect speed" in text and "video track V2" in text
    # a document whose video clips are all other media is rejected
    for k in v["children"]:
        if k["OTIO_SCHEMA"].startswith("Clip"):
            k["media_references"]["DEFAULT_MEDIA"]["target_url"] = "file:///elsewhere/other.mp4"
    foreign = {"OTIO_SCHEMA": "Timeline.1", "metadata": {"matchcut": {"source": "/x/media.mp4", "rate": [24, 1]}}, "name": "n",
               "tracks": {"OTIO_SCHEMA": "Stack.1", "children": [v]}}
    with pytest.raises(InterchangeError, match="no clip of the OTIO video track references the source footage"):
        import_otio(foreign)
    with pytest.raises(InterchangeError, match="no video track"):
        import_otio({"OTIO_SCHEMA": "Timeline.1", "tracks": {"children": []}})


def test_import_accepts_a_clip_relinked_to_the_same_file_name():
    doc = export_otio(synthetic(), "final", fake_src(), [])
    for k in tracks(doc)[0]["children"]:
        if k["OTIO_SCHEMA"].startswith("Clip"):
            k["media_references"]["DEFAULT_MEDIA"]["target_url"] = "file:///moved/media.mp4"
    assert len(import_otio(doc)[0].clips) == 3


def test_import_reads_the_rate_from_the_first_time_value_that_has_one():
    # a clip with "source_range": null falls back to its media's available range
    doc = export_otio(synthetic(), "final", fake_src(), [])
    doc["metadata"] = {}
    kids = tracks(doc)[0]["children"]
    kids[0]["source_range"] = None
    back, notes = import_otio(doc)
    assert back.fps == 24.0 and len(back.clips) == 3
    assert back.clips[0].source_in == 0.0 and back.clips[0].source_out > 59.9  # the whole 60 s of the media
    assert any("whole available range" in n for n in notes)
    # no range anywhere and no transition: no time base at all
    doc2 = export_otio(synthetic(), "final", fake_src(), [])
    doc2["metadata"] = {}
    for k in tracks(doc2)[0]["children"]:
        if k["OTIO_SCHEMA"].startswith("Clip"):
            k["source_range"] = None
            k["media_references"]["DEFAULT_MEDIA"]["available_range"] = None
    # a transition supplies the rate, but there is no clip left to map
    with pytest.raises(InterchangeError, match="no usable clips"):
        import_otio(doc2)
    tracks(doc2)[0]["children"].pop(1)
    with pytest.raises(InterchangeError, match="no usable time base"):
        import_otio(doc2)


@pytest.mark.parametrize("value,ok", [([24, 1], True), ([24.0, 1.0], True), (24.0, True), (24, True), ([30000, 1001], True), ("24", False),
                                      ([24], False), ([0, 1], False), ([24, 0], False), (None, False), ([True, 1], False), ({"a": 1}, False), (-24.0, False)])
def test_import_validates_the_rate_metadata(value, ok):
    doc = export_otio(synthetic(), "final", fake_src(), [])
    doc["metadata"]["matchcut"]["rate"] = value
    back, notes = import_otio(doc)
    assert back.fps == (pytest.approx(float(Fraction(30000, 1001))) if value == [30000, 1001] else 24.0) or not ok
    assert [c.shot_index for c in back.clips] == [0, 1, 2]
    assert any("not a valid rate" in n for n in notes) is (not ok)


def test_import_reports_a_transition_without_offsets_and_a_clip_without_a_range():
    doc = export_otio(synthetic(), "final", fake_src(), [])
    kids = tracks(doc)[0]["children"]
    del kids[1]["in_offset"]
    back, notes = import_otio(doc)
    assert [c.transition_in.kind for c in back.clips] == ["cut", "cut", "cut"] and any("in_offset or out_offset" in n for n in notes)
    doc = export_otio(synthetic(), "final", fake_src(), [])
    kids = tracks(doc)[0]["children"]
    kids[3]["source_range"] = None
    kids[3]["media_references"]["DEFAULT_MEDIA"]["available_range"] = None
    back, notes = import_otio(doc)
    assert len(back.clips) == 2 and any("no usable source range" in n for n in notes)


@pytest.mark.parametrize("duration", [0, -20])
@pytest.mark.parametrize("position", [0, 2, 3])
def test_a_clip_with_zero_or_negative_duration_is_ignored_with_a_note(duration, position):
    """Like the FCPXML reader (``clip ...: no usable start, duration or offset, ignored``): never a clip whose source out is not after its in."""
    doc = export_otio(synthetic(), "final", fake_src(), [])
    kids = tracks(doc)[0]["children"]
    name = kids[position]["name"]
    kids[position]["source_range"]["duration"]["value"] = duration
    back, notes = import_otio(doc)
    assert all(c.source_out > c.source_in for c in back.clips)
    assert len(back.clips) == 2 and any(n.startswith(f"clip {name}: ") and "ignored" in n and "duration" in n for n in notes), notes
    assert all("\n" not in n for n in notes)


def test_every_clip_with_no_duration_is_a_one_line_error():
    doc = export_otio(synthetic(), "final", fake_src(), [])
    for k in tracks(doc)[0]["children"]:
        if k["OTIO_SCHEMA"].startswith("Clip"):
            k["source_range"]["duration"]["value"] = 0
    with pytest.raises(InterchangeError, match="no usable clips"):
        import_otio(doc)


@pytest.mark.parametrize("damage", [
    lambda d: d.update(tracks=None), lambda d: d.update(tracks={"children": [None, 5]}), lambda d: d["tracks"]["children"][0]["children"].__setitem__(0, 7),
    lambda d: d["tracks"]["children"][0]["children"][0]["source_range"]["start_time"].pop("rate"),
    lambda d: d["tracks"]["children"][0]["children"][0]["source_range"]["start_time"].pop("value"),
    lambda d: d["tracks"]["children"][0]["children"][0]["source_range"].update(duration="x"),
    lambda d: d["tracks"]["children"][0]["children"][0].update(media_references=7),
    lambda d: d.update(metadata=7), lambda d: d["metadata"].update(matchcut=[1]),
    lambda d: d["tracks"]["children"][0]["children"][0].update(metadata=5),
])
def test_import_never_leaks_a_bare_exception_on_a_malformed_document(damage):
    doc = export_otio(synthetic(), "final", fake_src(), [])
    try:
        damage(doc)
    except (KeyError, TypeError, IndexError):
        pytest.skip("damage does not apply")
    try:
        import_otio(doc)
    except InterchangeError as exc:
        assert "\n" not in str(exc) and str(exc)
    for bad in (None, [], "x", 5):
        with pytest.raises(InterchangeError):
            import_otio(bad)


def test_import_subtracts_the_media_start_timecode():
    tl = synthetic()
    doc = export_otio(tl, "final", fake_src(), [])
    start = 24 * 3600  # an editor re-export: the media starts at 01:00:00:00 and source ranges are in that time
    for t in tracks(doc):
        for k in t["children"]:
            if k["OTIO_SCHEMA"].startswith("Clip"):
                k["source_range"]["start_time"]["value"] += start
                k["media_references"]["DEFAULT_MEDIA"]["available_range"]["start_time"]["value"] = float(start)
    back, notes = import_otio(doc)
    assert [(c.source_in, c.source_out) for c in back.clips] == [(c.source_in, c.source_out) for c in tl.clips]
    assert any(f"source start timecode of {start} frames subtracted" in n and "3 clips" in n for n in notes)


# -- provenance ----------------------------------------------------------------------------------
def test_provenance_sidecar_is_keyed_by_stable_ids(world, tmp_path):
    side = write_provenance(tmp_path / "p.json", world.stages, world.jb, world.src)
    loaded = json.loads((tmp_path / "p.json").read_text())
    assert loaded["stage_order"] == [s for s, _ in world.stages] and loaded["rate"] == [25, 1] and loaded["source"] == world.src.path
    fin = loaded["stages"]["final"]
    final = world.by_stage["final"]
    assert list(fin["junctions"]) == junction_ids(final)
    assert list(fin["clips"]) == [clip_key(c) for c in final.clips]
    for jid, j in zip(fin["junctions"], world.jb["final"]):
        e = fin["junctions"][jid]
        assert e["history"] == j.history() and e["why"] == j.why() and len(e["decisions"]) == len(j.decisions)
        assert [s["stage"] for s in e["states"]] == [s.stage for s in j.states]
        assert set(e) >= {"junction_id", "pinned", "pin_origin", "scores", "transition", "index", "out_clip", "in_clip"}
    c0 = final.clips[0]
    assert fin["clips"][clip_key(c0)]["id"] == c0.id and fin["clips"][clip_key(c0)]["source_frames"] == [to_frames(c0.source_in, Fraction(25)), to_frames(c0.source_out, Fraction(25))]
    assert fin["history"] == [d.to_dict() for d in final.history] and side["schema"] == "matchcut.provenance/1"


def test_provenance_keys_repeated_clip_keys_and_junctions_uniquely(tmp_path):
    tl = Timeline(source="/x/m.mp4", fps=24.0, stage="final")
    tl.clips = [Clip("c001", 3, 0.0, 1.0), Clip("c002", 4, 2.0, 3.0), Clip("c003", 3, 0.0, 1.0)]
    doc = write_provenance(tmp_path / "p.json", [("final", tl)], {}, fake_src())
    assert list(doc["stages"]["final"]["clips"]) == ["3@0.000-1.000", "4@2.000-3.000", "3@0.000-1.000#2"]
    assert doc["stages"]["final"]["clips"]["3@0.000-1.000#2"]["key"] == "3@0.000-1.000"


# -- optional: the real library -------------------------------------------------------------------
@pytest.mark.skipif(not OTIO_VENV_PYTHON.exists(), reason="OpenTimelineIO venv missing (OTIO_VENV_PYTHON)")
def test_otio_documents_load_in_real_opentimelineio(world, music_file, tmp_path):
    music = MusicConfig(path=str(music_file), start=1.0, gain_db=-7.0, duck=True)
    msrc = source_info(str(music_file))
    docs, paths = {}, []
    for stage in ("roughcut", "final"):
        _, docs[stage] = doc_of(world, stage, music if stage == "final" else None, msrc if stage == "final" else None)
    dip_tl = synthetic()
    dip_tl.clips[1].transition_in = Transition("dip_to_black", 0.5)
    docs["dip"] = export_otio(dip_tl, "final", fake_src(), [])
    for stage, doc in docs.items():
        p = tmp_path / f"{stage}.otio"
        p.write_text(json.dumps(doc, indent=1))
        paths.append(str(p))
    res = subprocess.run([str(OTIO_VENV_PYTHON), "-c", OTIO_INSPECT] + paths, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    rate = 25
    # a dip to black loads as a Custom_Transition centred like a dissolve, with its matchcut metadata intact
    dip = out[paths[2]]
    assert [i["type"] for i in dip["items"]] == ["Clip", "Transition", "Clip", "Clip"]
    assert (dip["items"][1]["ttype"], dip["items"][1]["in_off"], dip["items"][1]["out_off"]) == ("Custom_Transition", 6, 6)
    assert dip["again"]["tracks"]["children"][0]["children"][1]["metadata"]["matchcut"]["kind"] == "dip_to_black"
    for stage, path in zip(("roughcut", "final"), paths):
        tl, _ = exported(world, stage)
        rows = layout(tl, world.src.rate)
        o, doc = out[path], docs[stage]
        assert abs(o["frames"] - tl.duration * rate) <= record_tolerance_frames(Fraction(rate)), (stage, o["frames"], tl.duration * rate)
        assert o["frames"] == record_frames(rows)
        trs = [i for i in o["items"] if i["type"] == "Transition"]
        want = [r for r in rows if r.tr]
        assert len(trs) == len(want)
        for t, r in zip(trs, want):
            assert (t["in_off"], t["out_off"]) == r.transition[1:] and t["ttype"] == "SMPTE_Dissolve"
        assert o["markers"] == len(world.jb[stage]) == len(tl.clips) - 1
        assert o["marker_starts"] == [rows[j.index + 1].cut for j in world.jb[stage]] and set(o["marker_durs"]) <= {1.0}
        assert o["audio_tracks"] == (2 if stage == "final" else 1)
        if stage == "final":  # A2: the music clip's range survives the real library
            a2 = o["audio"][1]
            assert a2["name"] == "A2" and a2["items"][0]["src_start"] == 25.0 and a2["items"][0]["start"] == 0
            assert a2["items"][0]["dur"] == min(record_frames(rows), 750 - 25)
        # record times: a clip with an incoming transition starts where the transition starts (= matchcut record_start)
        starts, items = [], o["items"]
        for k, it in enumerate(items):
            if it["type"] == "Clip" and not (k and items[k - 1]["type"] == "Transition"):
                starts.append(it["start"])
            elif it["type"] == "Transition":
                starts.append(it["start"])
        for i, s in enumerate(starts):
            assert abs(s - tl.record_start(i) * rate) <= record_tolerance_frames(Fraction(rate)), (stage, i)
        # metadata survives the real library's load + re-serialise exactly
        again = o["again"]
        assert again["metadata"] == doc["metadata"]
        v_doc, v_again = tracks(doc)[0], tracks(again)[0]
        assert [m["metadata"] for m in v_again["markers"]] == [m["metadata"] for m in v_doc["markers"]]
        assert [m["name"] for m in v_again["markers"]] == [m["name"] for m in v_doc["markers"]]
        assert [k["metadata"] for k in v_again["children"]] == [k["metadata"] for k in v_doc["children"]]
        if stage == "final":
            a2 = tracks(again)[2]["children"][0]
            assert a2["metadata"]["matchcut"] == {"kind": "music", "gain_db": -7.0, "duck": True, "start": 1.0, "fade_out": 2.0, "only": False}
            assert a2["source_range"]["start_time"]["value"] == 25.0
        assert again["metadata"]["matchcut"]["rate"] == [25, 1]
        back, _ = import_otio(again, world.bounds)  # what the library re-serialised still imports to the same cut
        check_equivalent(tl, back, world.src.rate)


# -- the source footage: its exact path, never a generator or an offline clip (final review I2, T2-a) -----------------
BROLL = "file:///Volumes/Media/broll.mov"


def foreign_doc(src=None):
    """matchcut's synthetic cut as a Resolve-style OTIO without any matchcut metadata."""
    doc = export_otio(synthetic(), "final", src or fake_src(), [])
    doc["metadata"] = {"Resolve_OTIO": {"Resolve OTIO Meta Version": "1.0"}}
    doc["tracks"]["children"] = tracks(doc)[:1]  # the picture track only
    for t in tracks(doc):
        for k in t["children"]:
            k["metadata"] = {"Resolve_OTIO": {}}
    return doc


def media_clip(name: str, ref: dict, start: int = 10, dur: int = 30) -> dict:
    rt_ = lambda v: {"OTIO_SCHEMA": "RationalTime.1", "rate": 24.0, "value": float(v)}  # noqa: E731
    return {"OTIO_SCHEMA": "Clip.2", "metadata": {}, "name": name, "effects": [], "markers": [], "enabled": True, "color": None,
            "source_range": {"OTIO_SCHEMA": "TimeRange.1", "start_time": rt_(start), "duration": rt_(dur)},
            "media_references": {"DEFAULT_MEDIA": ref}, "active_media_reference_key": "DEFAULT_MEDIA"}


def external(url: str) -> dict:
    return {"OTIO_SCHEMA": "ExternalReference.1", "metadata": {}, "name": "", "available_range": None, "available_image_bounds": None, "target_url": url}


GENERATOR = {"OTIO_SCHEMA": "GeneratorReference.1", "metadata": {}, "name": "", "available_range": None, "available_image_bounds": None,
             "generator_kind": "SolidColor", "parameters": {}}
MISSING = {"OTIO_SCHEMA": "MissingReference.1", "metadata": {}, "name": "", "available_range": None, "available_image_bounds": None}


def test_a_broll_first_in_a_foreign_file_is_not_the_source_when_the_workspace_has_one():
    """Resolve-style OTIO, no matchcut metadata, first V1 clip is other footage: the workspace's footage decides."""
    doc = foreign_doc()
    tracks(doc)[0]["children"].insert(0, media_clip("broll.mov", external(BROLL)))
    back, notes = import_otio(doc, None, fake_src())
    assert [c.shot_index for c in back.clips] == [0, 1, 2] and back.source == "/x/media.mp4"
    check_equivalent(synthetic(), back, Fraction(24))
    assert notes == ["clip broll.mov: other media /Volumes/Media/broll.mov ignored"] and all(c.shot_index >= 0 for c in back.clips)
    # no clip of it is the source: a one-line error naming the footage, however many other clips there are
    for k in tracks(doc)[0]["children"]:
        if k["OTIO_SCHEMA"].startswith("Clip"):
            k["media_references"]["DEFAULT_MEDIA"]["target_url"] = BROLL
    with pytest.raises(InterchangeError, match="^no clip of the OTIO video track references the source footage /x/media.mp4$"):
        import_otio(doc, None, fake_src())


def test_generator_missing_and_unreferenced_clips_are_noted_and_skipped_never_read_as_footage():
    doc = foreign_doc()
    kids = tracks(doc)[0]["children"]
    kids.insert(0, media_clip("Solid Color", dict(GENERATOR)))
    kids.append(media_clip("offline.mov", dict(MISSING)))
    bare = media_clip("no media", external("x"))
    bare["media_references"], bare["active_media_reference_key"] = {}, ""
    kids.append(bare)
    for src in (fake_src(), None):
        back, notes = import_otio(copy.deepcopy(doc), None, src)
        assert [c.shot_index for c in back.clips] == [0, 1, 2]  # not one invented clip (the old code added each as "human added h00n, shot 0")
        assert sorted(notes) == sorted(f"clip {n}: generator/missing media ignored" for n in ("Solid Color", "no media", "offline.mov"))
        check_equivalent(synthetic(), back, Fraction(24))
    # a video track with nothing but generators has no footage in it
    only = foreign_doc()
    tracks(only)[0]["children"] = [media_clip("Solid Color", dict(GENERATOR)), media_clip("offline.mov", dict(MISSING))]
    with pytest.raises(InterchangeError, match="^no clip of the OTIO video track references the source footage /x/media.mp4$"):
        import_otio(only, None, fake_src())
    with pytest.raises(InterchangeError, match="^OTIO video track has no clip with a media file$"):
        import_otio(only)


def test_the_exact_path_beats_the_same_file_name_in_another_folder():
    """Camera names repeat across cards: with the source's exact path in the file, a clip of the same name elsewhere is other media."""
    doc = foreign_doc()
    kids = tracks(doc)[0]["children"]
    clips = [k for k in kids if k["OTIO_SCHEMA"].startswith("Clip")]
    other = copy.deepcopy(clips[0])
    other["media_references"]["DEFAULT_MEDIA"]["target_url"] = "file:///card2/media.mp4"
    other["source_range"]["start_time"]["value"] = 500
    kids.insert(0, other)
    back, notes = import_otio(doc, None, fake_src())
    assert [c.shot_index for c in back.clips] == [0, 1, 2]
    assert notes == ["clip c001 shot0: other media /card2/media.mp4 ignored"] and not any("file name" in n for n in notes)
    # without the file's own source declared the first clip's path is not trusted over the workspace's, same result
    # nothing has the exact path: the same file name is taken for the source, and the note says so
    doc = foreign_doc()
    for k in tracks(doc)[0]["children"]:
        if k["OTIO_SCHEMA"].startswith("Clip"):
            k["media_references"]["DEFAULT_MEDIA"]["target_url"] = "file:///moved/media.mp4"
    back, notes = import_otio(doc, None, fake_src())
    assert len(back.clips) == 3 and notes == ["no clip references /x/media.mp4: the clips whose file name is the same were taken for the source footage"]


def on_base(doc: dict, base: float) -> dict:
    """Every RationalTime of ``doc`` re-expressed on another time base (the same seconds)."""
    def walk(x):
        if isinstance(x, dict):
            if x.get("OTIO_SCHEMA", "").startswith("RationalTime"):
                x["value"], x["rate"] = x["value"] * base / x["rate"], base
            else:
                for v in x.values():
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    doc = copy.deepcopy(doc)
    walk(doc)
    return doc


def test_an_otio_on_a_48000_time_base_reads_at_the_media_rate_without_false_outside_the_media_notes():
    """Resolve writes some timelines on a 48000 base (final review M5): with the media known, frames are the media's."""
    src = fake_src()
    doc = foreign_doc(src)
    tl = synthetic()
    wide = on_base(doc, 48000.0)
    back, notes = import_otio(wide, None, src)
    assert notes == [] and not any("outside the media" in n for n in notes)
    check_equivalent(tl, back, Fraction(24))
    assert [(c.source_in, c.source_out) for c in back.clips] == [(c.source_in, c.source_out) for c in tl.clips]


def test_a_timeline_rate_that_differs_from_the_media_rate_is_noted_and_converted():
    src25 = fake_src(Fraction(25))
    doc = export_otio(synthetic(), "final", src25, [])
    base, base_notes = import_otio(copy.deepcopy(doc), None, src25)
    assert base_notes == []
    doc["metadata"]["matchcut"]["rate"] = [24, 1]  # a timeline of 24 fps holding 25 fps media; the times are still seconds on the media's base
    back, notes = import_otio(doc, None, src25)
    assert notes == ["the timeline runs at 24 fps and the media at 25 fps: times were converted at the media's rate"]
    assert [(c.source_in, c.source_out, c.transition_in.duration) for c in back.clips] == [(c.source_in, c.source_out, c.transition_in.duration) for c in base.clips]
