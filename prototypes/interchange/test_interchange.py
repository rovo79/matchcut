"""Tests for the interchange feasibility proof. Run: cd /home/user/matchcut && python3 -m pytest prototypes/interchange -q

External tools are optional and their tests skip when missing: the OTIO venv (OTIO_VENV_PYTHON), xmllint + the FCPXML 1.10 DTD
(FCPXML_DTD), mpv. The Resolve test runs against a FAKE of the documented scripting API, never a live Resolve.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import runpy
import shutil
import socket
import subprocess
import sys
import time
import types
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from typing import List

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import interchange as I  # noqa: E402

from matchcut.pipeline.config import PipelineConfig  # noqa: E402
from matchcut.pipeline.demo import make_demo_footage  # noqa: E402
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines, scorer_for_workspace  # noqa: E402
from matchcut.pipeline.pipeline import Pipeline  # noqa: E402
from matchcut.pipeline.shots import load_shots  # noqa: E402
from matchcut.pipeline.timeline import Clip, Timeline, Transition  # noqa: E402

RESEARCH = Path(os.environ.get("INTEGRATION_RESEARCH", "/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/integration"))
VENV_PY = Path(os.environ.get("OTIO_VENV_PYTHON", str(RESEARCH / "venv/bin/python")))
DTD = Path(os.environ.get("FCPXML_DTD", str(RESEARCH / "dtd/FCPXMLv1_10.dtd")))

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


def check_equivalent(a: Timeline, b: Timeline, tol: float = 1.0) -> None:
    """Same clips, transitions and record times, to ``tol`` frames."""
    f = a.fps
    assert [c.shot_index for c in a.clips] == [c.shot_index for c in b.clips]
    assert abs(a.duration - b.duration) * f <= tol + 1e-6
    for i, (x, y) in enumerate(zip(a.clips, b.clips)):
        assert abs(x.source_in - y.source_in) * f <= tol + 1e-6, i
        assert abs(x.source_out - y.source_out) * f <= tol + 1e-6, i
        assert x.transition_in.is_cut == y.transition_in.is_cut, i
        if not x.transition_in.is_cut:
            assert x.transition_in.kind == y.transition_in.kind
            assert abs(x.transition_in.duration - y.transition_in.duration) * f <= tol + 1e-6, i
        assert abs(a.record_start(i) - b.record_start(i)) * f <= tol + 1e-6, i


def synthetic() -> Timeline:
    """A (1-3 s) --dissolve 0.5 s--> B (5-7 s) --cut--> C (10-12 s) at 24 fps; record duration 5.5 s."""
    tl = Timeline(source="/x/media.mp4", fps=24.0, stage="final")
    tl.clips = [Clip("c001", 0, 1.0, 3.0), Clip("c002", 1, 5.0, 7.0, transition_in=Transition("dissolve", 0.5)), Clip("c003", 2, 10.0, 12.0)]
    return tl


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    base = tmp_path_factory.mktemp("proto")
    video = make_demo_footage(base / "demo.mp4")
    ws = base / "ws"
    cfg = PipelineConfig(source=str(video), workspace=str(ws), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()
    stages = load_stage_timelines(ws)
    scorer = scorer_for_workspace(ws, cfg)
    jb = {n: build_junctions(stages, scorer, upto=n) for n, _ in stages}
    sel = [(n, t) for n, t in stages if n in ("roughcut", "final")]
    fr = I.rational_fps(sel[-1][1].fps)
    return SimpleNamespace(ws=ws, video=str(video), stages=stages, jb=jb, sel=sel, fr=fr, final=dict(stages)["final"], scorer=scorer,
                           bounds={s.index: (s.start, s.end) for s in load_shots(ws / "shots.json")})


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def xmllint_ok(xml_path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["xmllint", "--noout", "--dtdvalid", str(DTD), str(xml_path)], capture_output=True, text=True)


# -- dissolve semantics (pure) -------------------------------------------------------------------
def test_dissolve_overlap_to_centred_conversion_is_exact():
    tl = synthetic()
    doc = I.export_otio([("final", tl)], "/x/media.mp4", (24, 1), {}, media_duration=60)["final"]
    kids = doc["tracks"]["children"][0]["children"]
    assert [k["OTIO_SCHEMA"].split(".")[0] for k in kids] == ["Clip", "Transition", "Clip", "Clip"]
    a, tr, b, c = kids
    assert (tr["in_offset"]["value"], tr["out_offset"]["value"]) == (6, 6) and tr["transition_type"] == "SMPTE_Dissolve"
    val = lambda k, f: k["source_range"][f]["value"]  # noqa: E731
    assert (val(a, "start_time"), val(a, "duration")) == (24, 42)  # visible range ends at source_out - d/2 (3 s - 6 f)
    assert (val(b, "start_time"), val(b, "duration")) == (126, 42)  # starts at source_in + d/2 (5 s + 6 f)
    assert val(a, "duration") + val(b, "duration") + val(c, "duration") == round(tl.duration * 24) == 132
    back = I.import_otio(doc)
    assert [(c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration) for c in back.clips] == \
        [(c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration) for c in tl.clips]
    # FCPXML: transition centred on the cut, clip B offset = end of A, record times as matchcut's (transition starts at record_start)
    root = ET.fromstring(I.export_fcpxml([("final", tl)], "/x/media.mp4", (24, 1), {}))
    spine = root.find(".//spine")
    assert [e.tag for e in spine] == ["asset-clip", "transition", "asset-clip", "asset-clip"]
    assert abs(tl.record_start(1) * 24 - 36) < 1e-9  # matchcut: the dissolve starts 12 f before A's end (48 f - 12 f)
    assert spine[1].get("offset") == "36/24s" and spine[1].get("duration") == "12/24s"  # = record_start(1), 6 f before the cut
    assert spine[2].get("offset") == "42/24s" and spine[3].get("offset") == "84/24s"  # the cut (36 + 6 f); C follows B's visible range
    assert (spine[0].get("start"), spine[0].get("duration")) == ("1s", "42/24s") and spine[2].get("start") == "126/24s"
    check_equivalent(tl, I.import_fcpxml(I.export_fcpxml([("final", tl)], "/x/media.mp4", (24, 1), {}), "final"), tol=0)


def test_dip_to_black_is_exported_but_labelled_unverified(tmp_path):
    tl = synthetic()
    tl.clips[1].transition_in = Transition("dip_to_black", 0.5)
    doc = I.export_otio([("final", tl)], "/x/m.mp4", (24, 1), {})["final"]
    tr = doc["tracks"]["children"][0]["children"][1]
    assert tr["transition_type"] == "Custom_Transition" and "UNVERIFIED" in tr["metadata"]["matchcut"]["unverified"]
    xml = I.export_fcpxml([("final", tl)], "/x/m.mp4", (24, 1), {})
    assert "Fade To Color" in xml and "UNVERIFIED" in xml
    assert I.import_otio(doc).clips[1].transition_in.kind == "dip_to_black"
    assert I.import_fcpxml(xml).clips[1].transition_in.kind == "dip_to_black"
    if shutil.which("xmllint") and DTD.exists():
        p = tmp_path / "dip.fcpxml"
        p.write_text(xml)
        assert xmllint_ok(p).returncode == 0


def test_ntsc_rates_use_exact_rationals(tmp_path):
    assert I.rational_fps(23.976) == (24000, 1001) and I.rational_fps(29.97) == (30000, 1001) and I.rational_fps(25.0) == (25, 1)
    fps = 30000 / 1001
    tl = Timeline(source="/x/m.mp4", fps=fps, stage="final")
    tl.clips = [Clip("c001", 0, 1.0, 3.0), Clip("c002", 1, 5.0, 7.0, transition_in=Transition("dissolve", 14 / fps)), Clip("c003", 2, 10.0, 12.0)]
    xml = I.export_fcpxml([("final", tl)], "/x/m.mp4", I.rational_fps(fps), {})
    assert 'frameDuration="1001/30000s"' in xml
    check_equivalent(tl, I.import_fcpxml(xml, "final"))
    doc = I.export_otio([("final", tl)], "/x/m.mp4", I.rational_fps(fps), {})["final"]
    assert doc["metadata"]["matchcut"]["fps"] == [30000, 1001]  # exact rate lives in metadata; RationalTime.rate is only a float
    check_equivalent(tl, I.import_otio(doc))
    if shutil.which("xmllint") and DTD.exists():
        p = tmp_path / "ntsc.fcpxml"
        p.write_text(xml)
        assert xmllint_ok(p).returncode == 0


def test_import_fcpxml_parses_arbitrary_rationals():
    xml = """<?xml version="1.0"?><!DOCTYPE fcpxml><fcpxml version="1.10"><resources>
      <format id="r1" frameDuration="1001/30000s"/><asset id="r2" name="m"><media-rep src="file:///x/m.mp4"/></asset>
      <effect id="r3" name="Cross Dissolve" uid="FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265"/></resources>
      <library><event name="e"><project name="p"><sequence format="r1" duration="1s"><spine>
        <asset-clip ref="r2" name="shot4 a" offset="0s" start="12s" duration="1001/300s"/>
        <transition offset="2002/3000s" duration="1001/3000s"><filter-video ref="r3" name="Cross Dissolve"/></transition>
        <asset-clip ref="r2" name="shot7 b" offset="3003/3000s" start="20020/3000s" duration="6006/6000s"/>
      </spine></sequence></project></event></library></fcpxml>"""
    tl = I.import_fcpxml(xml)
    f = 30000 / 1001
    assert abs(tl.fps - f) < 1e-9 and [c.shot_index for c in tl.clips] == [4, 7]
    assert tl.source == "/x/m.mp4"
    # a: 12 s -> frame 360. Transition 20..30 f ends exactly at the cut (b.offset = 30 f), so it is all "before the cut":
    # in_off = 10, out_off = 0; b.start = 20020/3000 s = frame 200, hence b.source_in = frame 190.
    a, b = tl.clips
    assert round(a.source_in * f) == 360
    assert b.transition_in.kind == "dissolve" and round(b.transition_in.duration * f) == 10
    assert round(b.source_in * f) == 190 and round(a.source_out * f) == 460


# -- OTIO, validated by the real opentimelineio in a throwaway venv ----------------------------
def test_otio_documents_load_in_real_opentimelineio(world, tmp_path):
    if not VENV_PY.exists():
        pytest.skip("OTIO venv missing")
    docs = I.export_otio(world.sel, world.video, world.fr, world.jb, media_duration=15.0)
    assert set(docs) == {"roughcut", "final"}
    paths = []
    for stage, doc in docs.items():
        p = tmp_path / f"{stage}.otio"
        p.write_text(json.dumps(doc, indent=1))
        paths.append(str(p))
    res = subprocess.run([str(VENV_PY), "-c", OTIO_INSPECT] + paths, capture_output=True, text=True)
    assert res.returncode == 0, res.stderr
    out = json.loads(res.stdout)
    for (stage, tl), path in zip(world.sel, paths):
        o, doc = out[path], docs[stage]
        fps = world.fr[0] / world.fr[1]
        assert abs(o["frames"] - tl.duration * fps) <= 1.0, (stage, o["frames"], tl.duration * fps)
        trs = [i for i in o["items"] if i["type"] == "Transition"]
        want = [c.transition_in.duration for c in tl.clips[1:] if not c.transition_in.is_cut]
        assert len(trs) == len(want) and len(trs) > 0 or stage == "roughcut"
        for t, d in zip(trs, want):
            assert abs((t["in_off"] + t["out_off"]) - d * fps) <= 1.0 and t["ttype"] == "SMPTE_Dissolve"
        assert o["markers"] == len(world.jb[stage]) == len(tl.clips) - 1 and o["audio_tracks"] == 1
        # record times: a clip with an incoming transition starts where the transition starts (= matchcut record_start)
        starts = []
        items = o["items"]
        for k, it in enumerate(items):
            if it["type"] == "Clip" and not (k and items[k - 1]["type"] == "Transition"):
                starts.append(it["start"])
            elif it["type"] == "Transition":
                starts.append(it["start"])
        for i, s in enumerate(starts):
            assert abs(s / fps - tl.record_start(i)) * fps <= 1.0 + 1e-6, (stage, i)
        # metadata survives the real library's load + re-serialise exactly
        again = o["again"]
        assert again["metadata"] == doc["metadata"]
        v_doc, v_again = doc["tracks"]["children"][0], again["tracks"]["children"][0]
        assert [m["metadata"] for m in v_again["markers"]] == [m["metadata"] for m in v_doc["markers"]]
        assert [m["name"] for m in v_again["markers"]] == [m["name"] for m in v_doc["markers"]]
        assert [k["metadata"] for k in v_again["children"]] == [k["metadata"] for k in v_doc["children"]]
        clips = [k for k in v_again["children"] if k["OTIO_SCHEMA"].startswith("Clip")]
        assert [k["metadata"]["matchcut"] for k in clips] == [{"clip_id": c.id, "shot_index": c.shot_index, "beat_id": c.beat_id, "stage": stage} for c in tl.clips]
        for m, j in zip(v_again["markers"], world.jb[stage]):
            md = m["metadata"]["matchcut"]
            assert set(md) == {"junction_id", "why", "history", "pinned", "pin_origin", "scores", "transition"}
            assert md["junction_id"] == f"s{j.out_shot}-s{j.in_shot}" and md["why"] == j.why() and md["history"] == j.history()
        assert again["metadata"]["matchcut"]["fps"] == list(world.fr)
        assert set(again["metadata"]["matchcut"]) >= {"stage", "source", "fps", "fade_in", "fade_out", "provenance_file"}


def test_otio_roundtrips_through_import_without_a_library(world):
    docs = I.export_otio(world.sel, world.video, world.fr, world.jb)
    for stage, tl in world.sel:
        check_equivalent(tl, I.import_otio(json.loads(json.dumps(docs[stage]))))


# -- FCPXML 1.10 ---------------------------------------------------------------------------------
def test_fcpxml_is_dtd_valid_and_reimports(world, tmp_path):
    xml = I.export_fcpxml(world.sel, world.video, world.fr, world.jb, media_duration=15.0)
    assert '<fcpxml version="1.10">' in xml and xml.count("<project ") == 2
    assert xml.count("<marker ") == sum(len(world.jb[s]) for s, _ in world.sel)
    p = tmp_path / "matchcut.fcpxml"
    p.write_text(xml)
    if shutil.which("xmllint") and DTD.exists():
        r = xmllint_ok(p)
        assert r.returncode == 0, r.stderr
    else:
        pytest.skip("xmllint or the 1.10 DTD missing (re-import check follows in test_fcpxml_reimport_without_xmllint)")
    for stage, tl in world.sel:
        check_equivalent(tl, I.import_fcpxml(xml, stage))
        assert I.fcpxml_junction_ids(xml, stage) == [f"s{j.out_shot}-s{j.in_shot}" for j in world.jb[stage]]


def test_fcpxml_reimport_without_xmllint(world):
    xml = I.export_fcpxml(world.sel, world.video, world.fr, world.jb)
    for stage, tl in world.sel:
        check_equivalent(tl, I.import_fcpxml(xml, stage))


def test_fcpxml_import_survives_stripped_metadata(world):
    """If an editor drops our md keys and renames clips, shots come from the source ranges against shot bounds."""
    xml = I.export_fcpxml(world.sel, world.video, world.fr, world.jb)
    root = ET.fromstring(xml)
    for el in root.iter():
        for md in el.findall("metadata"):
            el.remove(md)
        if el.tag == "asset-clip":
            el.set("name", "edited name")
    stripped = ET.tostring(root, encoding="unicode")
    tl = I.import_fcpxml(stripped, shot_bounds=world.bounds)  # first project = roughcut
    assert [c.shot_index for c in tl.clips] == [c.shot_index for c in dict(world.sel)["roughcut"].clips]
    assert [c.id for c in tl.clips] == [f"h{i:03d}" for i in range(1, len(tl.clips) + 1)]  # ids are lost: they come back as new clips


# -- human edits back ----------------------------------------------------------------------------
def test_human_edit_roundtrip_keeps_canonical_and_provenance(world, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    docs = I.export_otio(world.sel, world.video, world.fr, world.jb)
    for stage, doc in docs.items():
        (out / f"{stage}.otio").write_text(json.dumps(doc, indent=1))
    (out / "matchcut.fcpxml").write_text(I.export_fcpxml(world.sel, world.video, world.fr, world.jb))
    side = I.write_provenance(out / "provenance.json", world.stages, world.jb, world.video, world.fr)
    before = {p.name: sha(p) for p in out.iterdir()}
    final = world.final
    snapshot = copy.deepcopy(final.to_dict())

    # an editor's changes, applied to the exported JSON
    doc = copy.deepcopy(docs["final"])
    vt = doc["tracks"]["children"][0]
    kids = vt["children"]
    pos = [k for k, c in enumerate(kids) if c["OTIO_SCHEMA"].startswith("Clip")]
    cid = lambda k: kids[k]["metadata"]["matchcut"]["clip_id"]  # noqa: E731
    trim_k = next(k for k in pos if k > 0 and kids[k - 1]["OTIO_SCHEMA"].startswith("Clip") and kids[k]["source_range"]["duration"]["value"] >= 12)
    dis_k = max(k for k, c in enumerate(kids) if c["OTIO_SCHEMA"].startswith("Transition"))
    prev_id, next_id = cid(dis_k - 1), cid(dis_k + 1)
    trim_id = cid(trim_k)
    kids[trim_k]["source_range"]["start_time"]["value"] += 5
    kids[trim_k]["source_range"]["duration"]["value"] -= 5
    del kids[dis_k]  # dissolve -> cut at the same cut point
    gone = vt["markers"].pop()["metadata"]["matchcut"]["junction_id"]
    kids[pos[-1] if pos[-1] < dis_k else pos[-1] - 1]["name"] = "renamed by the editor"

    edited = I.import_otio(doc)
    decisions = I.diff_human_edits(final, edited)
    got = [(d.pass_name, d.action, d.clip_ids, d.data) for d in decisions]
    trim = ("human", "trim", [trim_id], {"in_frames": 5, "out_frames": 0})
    trans = [g for g in got if g[1] == "transition"]
    assert len(got) == 2 and trim in got and len(trans) == 1
    assert trans[0][2] == [prev_id, next_id] and trans[0][3]["after"]["frames"] == 0 and trans[0][3]["before"]["kind"] == "dissolve"
    assert got.index(trim) < got.index(trans[0]) if trim_k < dis_k else got.index(trim) > got.index(trans[0])

    human = I.human_stage(final, edited)
    assert human.stage == "human" and [c.id for c in human.clips] == [c.id for c in final.clips]
    assert [d.to_dict() for d in human.history[: len(final.history)]] == [d.to_dict() for d in final.history]
    assert [d.action for d in human.history[len(final.history):]] == [d[1] for d in got]
    # build_junctions over [... final, human] shows the human change in history and decisions
    stages2 = world.stages + [("human", human)]
    js = build_junctions(stages2, world.scorer)
    jt = next(j for j in js if j.in_clip == trim_id)
    assert any(h.startswith("human:") and "in +0.20s (+5f)" in h for h in jt.history()), jt.history()
    assert any(d.pass_name == "human" and d.action == "trim" for d in jt.decisions)
    jd = next(j for j in js if (j.out_clip, j.in_clip) == (prev_id, next_id))
    assert any(h.startswith("human:") and h.endswith("cut") for h in jd.history()), jd.history()
    assert any(d.pass_name == "human" and d.action == "transition" for d in jd.decisions)

    # nothing canonical changed: object, files, sidecar
    assert final.to_dict() == snapshot
    assert before == {p.name: sha(p) for p in out.iterdir()}
    # sidecar ids still resolve every surviving junction (and the deleted marker's junction too)
    fin_j = side["stages"]["final"]["junctions"]
    surviving = I.otio_junction_ids(doc)
    assert surviving and all(s in fin_j for s in surviving) and gone in fin_j and gone not in surviving
    jb2 = {n: build_junctions(stages2, world.scorer, upto=n) for n, _ in stages2}
    side2 = I.write_provenance(tmp_path / "provenance_after.json", stages2, jb2, world.video, world.fr)
    ids_human = {f"s{j.out_shot}-s{j.in_shot}" for j in js}
    assert ids_human <= set(side2["stages"]["human"]["junctions"]) and set(fin_j) <= set(side2["stages"]["final"]["junctions"])
    assert any(d["pass_name"] == "human" for jr in side2["stages"]["human"]["junctions"].values() for d in jr["decisions"])


def test_human_edit_removed_added_reordered_and_fcpxml_trim(world):
    final = world.final
    ed = final.copy()
    ed.clips = [ed.clips[1], ed.clips[0]] + ed.clips[3:]  # swapped first two, dropped clip 3
    ed.clips.append(Clip("zz", ed.clips[0].shot_index + 100, 1.0, 2.0))  # a clip from a shot that is not in the cut
    acts = [(d.action, d.clip_ids) for d in I.diff_human_edits(final, ed)]
    assert ("removed", [final.clips[2].id]) in acts and any(a == "added" for a, _ in acts)
    assert ("reorder", [final.clips[1].id, final.clips[0].id] + [c.id for c in final.clips[3:]]) in acts
    # FCPXML path: trim an incoming clip's start by 5 frames
    xml = I.export_fcpxml(world.sel, world.video, world.fr, world.jb)
    root = ET.fromstring(xml)
    proj = [p for p in root.iter("project") if p.get("name").endswith("final")][0]
    ac = [e for e in proj.iter("asset-clip")][1]
    f = lambda s: round(I.parse_time(s) * world.fr[0] / world.fr[1])  # noqa: E731
    ac.set("start", f"{(f(ac.get('start')) + 5)}/{world.fr[0]}s")
    ac.set("duration", f"{(f(ac.get('duration')) - 5)}/{world.fr[0]}s")
    d = I.diff_human_edits(final, I.import_fcpxml(ET.tostring(root, encoding="unicode"), "final"))
    assert [(x.action, x.data) for x in d] == [("trim", {"in_frames": 5, "out_frames": 0})]


# -- provenance sidecar --------------------------------------------------------------------------
def test_provenance_sidecar_is_keyed_by_stable_ids(world, tmp_path):
    side = I.write_provenance(tmp_path / "p.json", world.stages, world.jb, world.video, world.fr)
    loaded = json.loads((tmp_path / "p.json").read_text())
    assert loaded["stage_order"] == [s for s, _ in world.stages] and loaded["fps"] == list(world.fr)
    fin = loaded["stages"]["final"]
    assert list(fin["junctions"]) == I.junction_ids(world.jb["final"])
    assert all(k.startswith("shot") and "@" in k for k in fin["clips"])
    for jid, j in zip(fin["junctions"], world.jb["final"]):
        e = fin["junctions"][jid]
        assert e["history"] == j.history() and e["why"] == j.why() and len(e["decisions"]) == len(j.decisions)
        assert [s["stage"] for s in e["states"]] == [s.stage for s in j.states]
    assert fin["history"] == [d.to_dict() for d in world.final.history] and side["schema"] == "matchcut.provenance/1"


# -- mpv: EDL + JSON IPC -------------------------------------------------------------------------
class MpvIPC:
    def __init__(self, path: str):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(10)
        end = time.time() + 10
        while True:
            try:
                self.sock.connect(path)
                break
            except OSError:
                if time.time() > end:
                    raise
                time.sleep(0.05)
        self.buf, self.events, self.rid = b"", [], 0

    def _line(self) -> dict:
        while b"\n" not in self.buf:
            self.buf += self.sock.recv(65536)
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line)

    def cmd(self, command: List) -> dict:
        self.rid += 1
        self.sock.sendall(json.dumps({"command": command, "request_id": self.rid}).encode() + b"\n")
        while True:
            m = self._line()
            if m.get("request_id") == self.rid:
                assert m["error"] == "success", m
                return m
            if "event" in m:
                self.events.append(m["event"])

    def wait_event(self, name: str) -> None:
        end = time.time() + 15
        while name not in self.events:
            assert time.time() < end, f"no {name} event"
            m = self._line()
            if "event" in m:
                self.events.append(m["event"])
        self.events.remove(name)

    def get(self, prop: str):
        return self.cmd(["get_property", prop]).get("data")


@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")
def test_mpv_edl_duration_and_junction_loop(world, tmp_path):
    tl = world.final
    edl = tmp_path / "final.edl"
    edl.write_text(I.mpv_edl(tl, world.video))
    text = edl.read_text()
    assert text.startswith("# mpv EDL v0\n") and text.count("title=") == len(tl.clips) and "J1 s" in text
    sock = str(tmp_path / "mpv.sock")
    proc = subprocess.Popen(["mpv", "--no-config", "--idle=yes", "--pause", "--vo=null", "--ao=null", "--no-terminal", f"--input-ipc-server={sock}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ipc = MpvIPC(sock)
        ipc.cmd(["loadfile", str(edl), "replace"])
        ipc.wait_event("file-loaded")
        dur = ipc.get("duration")
        summed = sum(c.duration for c in tl.clips)
        assert abs(dur - summed) <= 2 / tl.fps, (dur, summed)
        # documented limitation: the EDL has no transitions, so it is longer than the dissolving timeline by the overlap
        overlap = sum(c.transition_in.duration for c in tl.clips[1:] if not c.transition_in.is_cut)
        assert overlap > 0 and abs((dur - tl.duration) - overlap) <= 2 / tl.fps
        index = next(i for i, c in enumerate(tl.clips[1:]) if not c.transition_in.is_cut)  # a dissolve junction
        cmds = I.mpv_ipc_commands(tl, index, pre=0.4, post=0.4, why="proof")
        assert [c["command"][0] for c in cmds] == ["set_property", "set_property", "seek", "show-text"] and "proof" in cmds[3]["command"][1]
        a, b = cmds[0]["command"][2], cmds[1]["command"][2]
        t_edl = I.junction_time(tl, index, edl=True)[0]
        assert abs((a + b) / 2 - t_edl) < 1e-6 and abs(t_edl - sum(c.duration for c in tl.clips[: index + 1])) <= 1 / tl.fps
        for c in cmds:
            ipc.cmd(c["command"])
        ipc.wait_event("playback-restart")
        assert abs(ipc.get("ab-loop-a") - a) < 1e-3 and abs(ipc.get("ab-loop-b") - b) < 1e-3
        ipc.cmd(["set_property", "pause", False])
        seen: List[float] = []
        start = time.time()
        while time.time() - start < 2.0 * (b - a) + 0.6:  # long enough to pass B at least once, so the loop must have wrapped
            seen.append(ipc.get("time-pos"))
            time.sleep(0.1)
        assert min(seen) >= a - 0.15 and max(seen) <= b + 0.3, (a, b, seen)
        assert max(seen) - min(seen) < (b - a) + 0.45
    finally:
        proc.kill()
        proc.wait(timeout=10)


# -- Resolve: a FAKE of the documented scripting API ---------------------------------------------
MARKER_COLORS = {"Blue", "Cyan", "Green", "Yellow", "Red", "Pink", "Purple", "Fuchsia", "Rose", "Lavender", "Sky", "Mint", "Lemon", "Sand", "Cocoa", "Cream"}


class FakeTimeline:
    def __init__(self, log: list, name: str):
        self.log, self.name = log, name

    def GetName(self):
        self.log.append(("Timeline.GetName",))
        return self.name

    def AddMarker(self, frameId, color, name, note, duration, customData):
        assert color in MARKER_COLORS and isinstance(frameId, int) and isinstance(customData, str)
        self.log.append(("AddMarker", frameId, color, name, note, duration, customData))
        return True

    def SetCurrentTimecode(self, timecode):
        self.log.append(("SetCurrentTimecode", timecode))
        return True


class FakeMediaPool:
    def __init__(self, log: list, project):
        self.log, self.project = log, project

    def ImportTimelineFromFile(self, filePath, importOptions=None):
        assert set(importOptions or {}) <= {"timelineName", "importSourceClips", "sourceClipsPath", "sourceClipsFolders"}
        self.log.append(("ImportTimelineFromFile", filePath, dict(importOptions)))
        tl = FakeTimeline(self.log, importOptions["timelineName"])
        self.project.timelines.append(tl)
        return tl


class FakeProject:
    def __init__(self, log: list):
        self.log, self.timelines, self.current = log, [], None

    def GetMediaPool(self):
        self.log.append(("GetMediaPool",))
        return FakeMediaPool(self.log, self)

    def SetCurrentTimeline(self, timeline):
        self.log.append(("SetCurrentTimeline", timeline.name))
        return True

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, idx):
        return self.timelines[idx - 1]


class FakeProjectManager:
    def __init__(self, log: list):
        self.log = log

    def GetCurrentProject(self):
        self.log.append(("GetCurrentProject",))
        return FakeProject(self.log)


class FakeResolve:
    def __init__(self, log: list):
        self.log = log

    def GetProjectManager(self):
        self.log.append(("GetProjectManager",))
        return FakeProjectManager(self.log)

    def OpenPage(self, pageName):
        assert pageName in {"media", "photo", "cut", "edit", "fusion", "color", "fairlight", "deliver"}
        self.log.append(("OpenPage", pageName))
        return True


def test_resolve_script_against_fake_documented_api(world, tmp_path, monkeypatch):
    tl = world.final
    js = world.jb["final"]
    jids = I.junction_ids(js)
    frames = {jid: I.junction_time(tl, j.index)[1] for j, jid in zip(js, jids)}
    nav = jids[-1]
    notes = {jid: (f"J{j.index + 1} s{j.out_shot}->s{j.in_shot}", j.why()) for j, jid in zip(js, jids)}
    text = I.resolve_script("/tmp/final.otio", "matchcut final", frames, nav, fps=tl.fps, notes=notes)
    forbidden = ("AddTransition", "SetFades", "SetClipColor", "SetMetadata", "Export(")
    assert not any(f in text for f in forbidden)
    script = tmp_path / "resolve_open_junction.py"
    script.write_text(text)
    compile(text, str(script), "exec")
    log: list = []
    fake = types.ModuleType("DaVinciResolveScript")
    fake.scriptapp = lambda name: (log.append(("scriptapp", name)) or FakeResolve(log))
    monkeypatch.setitem(sys.modules, "DaVinciResolveScript", fake)
    runpy.run_path(str(script), run_name="__main__")
    calls = [c for c in log if c[0] != "Timeline.GetName"]
    head = [c[0] for c in calls[:7]]
    assert head == ["scriptapp", "GetProjectManager", "GetCurrentProject", "GetMediaPool", "ImportTimelineFromFile", "SetCurrentTimeline", "OpenPage"]
    assert calls[0] == ("scriptapp", "Resolve") and calls[4][1:] == ("/tmp/final.otio", {"timelineName": "matchcut final", "importSourceClips": True})
    assert calls[5] == ("SetCurrentTimeline", "matchcut final") and calls[6] == ("OpenPage", "edit")
    markers = [c for c in calls if c[0] == "AddMarker"]
    assert [m[6] for m in markers] == jids and [m[1] for m in markers] == [frames[j] for j in jids]
    assert [m[3] for m in markers] == [notes[j][0] for j in jids]
    assert calls[-1] == ("SetCurrentTimecode", I.junction_time(tl, js[-1].index)[2])
    assert [c[0] for c in calls[7:]] == ["AddMarker"] * len(jids) + ["SetCurrentTimecode"]  # playhead is set last, after the markers


def test_fake_resolve_rejects_undocumented_calls():
    log: list = []
    r = FakeResolve(log)
    with pytest.raises(AttributeError):
        r.GetProjectManager().GetCurrentProject().GetMediaPool().AddTransition  # noqa: B018
