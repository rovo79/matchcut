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

from .conftest import OTIO_VENV_PYTHON, check_equivalent, exported
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
    out[path] = {"frames": v.duration().value, "items": items, "markers": len(v.markers), "audio_tracks": len(tl.audio_tracks()),
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
    assert clip["metadata"]["matchcut"]["gain_db"] == -6.0 and clip["metadata"]["matchcut"]["duck"] is False
    assert import_otio(json.loads(json.dumps(doc)))[1] == []  # our own music track is not an "ignored" item


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
        p = tmp_path / f"{stage}.otio"
        p.write_text(json.dumps(docs[stage], indent=1))
        paths.append(str(p))
    res = subprocess.run([str(OTIO_VENV_PYTHON), "-c", OTIO_INSPECT] + paths, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    rate = 25
    for stage, path in zip(("roughcut", "final"), paths):
        tl, _ = exported(world, stage)
        o, doc = out[path], docs[stage]
        assert abs(o["frames"] - tl.duration * rate) <= 1.0, (stage, o["frames"], tl.duration * rate)
        trs = [i for i in o["items"] if i["type"] == "Transition"]
        want = [c.transition_in.duration for c in tl.clips[1:] if not c.transition_in.is_cut]
        assert len(trs) == len(want)
        for t, d in zip(trs, want):
            assert abs((t["in_off"] + t["out_off"]) - d * rate) <= 1.0 and t["ttype"] == "SMPTE_Dissolve"
        assert o["markers"] == len(world.jb[stage]) == len(tl.clips) - 1
        assert o["audio_tracks"] == (2 if stage == "final" else 1)
        # record times: a clip with an incoming transition starts where the transition starts (= matchcut record_start)
        starts, items = [], o["items"]
        for k, it in enumerate(items):
            if it["type"] == "Clip" and not (k and items[k - 1]["type"] == "Transition"):
                starts.append(it["start"])
            elif it["type"] == "Transition":
                starts.append(it["start"])
        for i, s in enumerate(starts):
            assert abs(s / rate - tl.record_start(i)) * rate <= 1.0 + 1e-6, (stage, i)
        # metadata survives the real library's load + re-serialise exactly
        again = o["again"]
        assert again["metadata"] == doc["metadata"]
        v_doc, v_again = tracks(doc)[0], tracks(again)[0]
        assert [m["metadata"] for m in v_again["markers"]] == [m["metadata"] for m in v_doc["markers"]]
        assert [m["name"] for m in v_again["markers"]] == [m["name"] for m in v_doc["markers"]]
        assert [k["metadata"] for k in v_again["children"]] == [k["metadata"] for k in v_doc["children"]]
        if stage == "final":
            a2 = tracks(again)[2]["children"][0]
            assert a2["metadata"]["matchcut"] == {"kind": "music", "gain_db": -7.0, "duck": True, "start": 1.0}
            assert a2["source_range"]["start_time"]["value"] == 25.0
        assert again["metadata"]["matchcut"]["rate"] == [25, 1]
        back, _ = import_otio(again, world.bounds)  # what the library re-serialised still imports to the same cut
        check_equivalent(tl, back, world.src.rate)
