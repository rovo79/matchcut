"""Workspace export and import: the files, the fidelity reports, the human stage, stage detection and the staleness warning."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import pytest

from matchcut.interchange import InterchangeError, workspace
from matchcut.interchange.common import export_timeline, layout
from matchcut.interchange.edl import export_edl
from matchcut.interchange.fcpxml import export_fcpxml, fcpxml_stages
from matchcut.interchange.otio import export_otio, import_otio
from matchcut.interchange.workspace import ExportResult, ImportResult, diff_human_edits, export_workspace, human_stage, import_timeline
from matchcut.pipeline.config import MusicConfig
from matchcut.pipeline.editorial import Pin, PinStore, pin_outcome
from matchcut.pipeline.junctions import build_junctions, load_stage_timelines, scorer_for_workspace, stale_human, stage_timeline_paths
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Decision, Timeline, Transition
from matchcut.tui.widgets.inspector import render_inspector

from .conftest import exported
from .test_otio import fake_src


# -- fixtures and helpers -------------------------------------------------------------------------
def canonical_bytes(ws: Path) -> dict:
    files = [ws / "roughcut.json", ws / "final.json", ws / "probe.json", ws / "shots.json", ws / "config.json"] + sorted((ws / "passes").glob("*.json"))
    return {str(p.relative_to(ws)): p.read_bytes() for p in files}


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def final_of(wsx):
    snapped, _ = exported(wsx.world, "final")
    return wsx.world.by_stage["final"], snapped


def editor_timeline(snapped: Timeline, rate) -> Timeline:
    """The snapped final cut after an editor's changes: the second clip trimmed 5 frames at its start and the dissolve into the third
    clip deleted (the visible cut stays where it was).  Matchcut's overlap semantics, so any exporter can write it."""
    rows = layout(snapped, rate)
    fps = float(rate)
    ed = snapped.copy()
    for k in range(1, len(ed.clips)):  # an editor's dissolves are whole frames; matchcut's (0.75 s = 18.75 frames) are not
        ed.clips[k].transition_in = Transition(rows[k].kind, round(rows[k].tr / fps, 4)) if rows[k].tr else Transition()
    ed.clips[1].source_in = round(ed.clips[1].source_in + 5 / fps, 4)
    ed.clips[1].source_out = round(rows[1].vis_out / fps, 4)
    ed.clips[2].source_in = round(rows[2].vis_in / fps, 4)
    ed.clips[2].transition_in = Transition()
    return ed


def summary(decisions):
    return [(d.action, d.clip_ids, d.data) for d in decisions]


def expected_edit(snapped, rate):
    rows = layout(snapped, rate)
    assert rows[2].tr > 0 and rows[1].tr == 0
    return [("trim", ["c002"], {"in_frames": 5, "out_frames": 0}),
            ("transition", ["c002", "c003"], {"before": {"kind": "dissolve", "frames": rows[2].tr}, "after": {"kind": "cut", "frames": 0}})]


def write_edited_files(wsx, ed: Timeline, tmp: Path, rename: bool = True) -> dict:
    """The edited cut as an editor's re-export would be: one file per format, clip ids renamed."""
    w = wsx.world
    if rename:
        ed = ed.copy()
        for k, c in enumerate(ed.clips):
            c.id = f"x{k + 1:03d}"
    jb = w.jb["final"]
    otio = tmp / "edited.otio"
    fp = sha(wsx.ws / "final.json")  # like every export of matchcut: the file says which version of the stage it was made from
    otio.write_text(json.dumps(export_otio(ed, "final", w.src, jb, stage_sha256=fp)))
    fcp = tmp / "edited.fcpxml"
    fcp.write_text(export_fcpxml([("final", ed, jb)], w.src, stage_sha256={"final": fp}))
    edl = tmp / "edited.edl"
    edl.write_text(export_edl(ed, "final", w.src, "demo - final", stage_sha256=fp))
    return {"otio": otio, "fcpxml": fcp, "edl": edl}


# -- export ----------------------------------------------------------------------------------------
def test_export_of_the_mock_workspace_writes_every_file_and_report(wsx):
    res = export_workspace(wsx.ws, wsx.cfg)
    out = wsx.ws / "exports"
    names = sorted(p.name for p in out.iterdir())
    assert names == [".stages", "fidelity.json", "fidelity.md", "final.edl", "final.otio", "matchcut.fcpxml", "provenance.json", "roughcut.edl", "roughcut.otio"]
    assert isinstance(res, ExportResult) and sorted(p.name for p in res.files) == [n for n in names if n != ".stages"] and all(p.parent == out for p in res.files)
    assert [(r.stage, r.fmt) for r in res.results] == [(s, f) for s in ("roughcut", "final") for f in ("otio", "fcpxml", "edl")]
    assert all(r.ok and r.preserved and r.approximated and r.lost for r in res.results)
    report = json.loads((out / "fidelity.json").read_text())
    assert report["ok"] is True and set(report["stages"]) == {"roughcut", "final"}
    assert all(d["errors"] == [] for by in report["stages"].values() for d in by.values()) and "Stage: final" in (out / "fidelity.md").read_text()
    prov = json.loads((out / "provenance.json").read_text())
    assert prov["stage_order"] == ["roughcut", "final"] and set(prov["stages"]) == {"roughcut", "final"}
    doc = json.loads((out / "final.otio").read_text())
    assert doc["metadata"]["matchcut"]["stage"] == "final"
    assert sorted(fcpxml_stages((out / "matchcut.fcpxml").read_text())) == ["final", "roughcut"]
    assert (out / "final.edl").read_text().startswith("TITLE: demo - final")


def test_export_subsets_and_a_different_out_dir(wsx):
    out = wsx.tmp / "elsewhere"
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl", "otio"), out_dir=out)
    assert sorted(p.name for p in out.iterdir()) == [".stages", "fidelity.json", "fidelity.md", "final.edl", "final.otio", "provenance.json"]
    assert [(r.stage, r.fmt) for r in res.results] == [("final", "otio"), ("final", "edl")]
    assert not (wsx.ws / "exports").exists()
    # a pass stage and the frame snap: the snap is reported, not hidden
    export_workspace(wsx.ws, wsx.cfg, stages=("polish",), formats=("otio",), out_dir=out)
    assert (out / "polish.otio").exists()


def test_export_errors_are_one_line(wsx):
    for kwargs, match in (({"stages": ("nope",)}, f"^no stage nope in {re.escape(str(wsx.ws))}$"), ({"formats": ("aaf",)}, "^unknown format aaf: use otio, fcpxml or edl$"),
                          ({"stages": ()}, "no stage"), ({"formats": ()}, "no format")):
        with pytest.raises(InterchangeError, match=match) as exc:
            export_workspace(wsx.ws, wsx.cfg, **kwargs)
        assert "\n" not in str(exc.value)
    cfg = copy.deepcopy(wsx.cfg)
    cfg.source = str(wsx.tmp / "gone.mp4")
    (wsx.ws / "probe.json").unlink()
    with pytest.raises(InterchangeError, match="source footage not found"):
        export_workspace(wsx.ws, cfg)


def test_the_human_stage_is_not_exportable(wsx):
    import_timeline(wsx.ws, wsx.cfg, write_edited_files(wsx, final_of(wsx)[1], wsx.tmp)["otio"])
    with pytest.raises(InterchangeError, match="human stage is an import result"):
        export_workspace(wsx.ws, wsx.cfg, stages=("human",))


def test_a_failing_validation_writes_the_reports_then_raises(wsx, monkeypatch):
    real = workspace.fidelity.check

    def broken(canonical, back, fmt, rate, exported=None, **kw):
        r = real(canonical, back, fmt, rate, exported, **kw)
        if fmt == "edl" and r.stage == "final":
            r.errors.append("clip 2 (c002, shot 1): source in 1 != 2 frames")
        return r

    monkeypatch.setattr(workspace.fidelity, "check", broken)
    with pytest.raises(InterchangeError, match=r"^export of edl/final does not match the canonical timeline: clip 2 \(c002, shot 1\): source in 1 != 2 frames$"):
        export_workspace(wsx.ws, wsx.cfg)
    report = json.loads((wsx.ws / "exports" / "fidelity.json").read_text())
    assert report["ok"] is False and report["stages"]["final"]["edl"]["errors"]
    assert (wsx.ws / "exports" / "final.edl").exists() and (wsx.ws / "exports" / "provenance.json").exists()


def test_music_is_probed_exported_and_music_only_is_noted(wsx, music_file):
    cfg = copy.deepcopy(wsx.cfg)
    cfg.music = MusicConfig(path=str(music_file), start=1.0, gain_db=-6.0)
    res = export_workspace(wsx.ws, cfg, stages=("final",))
    tracks = json.loads((wsx.ws / "exports" / "final.otio").read_text())["tracks"]["children"]
    assert [t["name"] for t in tracks] == ["V1", "A1", "A2"] and tracks[2]["children"][0]["metadata"]["matchcut"]["gain_db"] == -6.0
    assert 'lane="-1"' in (wsx.ws / "exports" / "matchcut.fcpxml").read_text() and not any("music.only" in n for n in res.notes)
    cfg.music.only = True
    res = export_workspace(wsx.ws, cfg, stages=("final",))
    assert any(n.startswith("music.only:") and "source audio" in n and "muted" in n for n in res.notes), res.notes
    assert tracks and json.loads((wsx.ws / "exports" / "final.otio").read_text())["tracks"]["children"][1]["enabled"] is False
    cfg.music.path = str(wsx.tmp / "missing.wav")
    with pytest.raises(InterchangeError, match="^music file not found: "):
        export_workspace(wsx.ws, cfg, stages=("final",))


def test_export_notes_the_frame_snap_per_stage(wsx):
    res = export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("otio",))
    snapped, snap_notes = exported(wsx.world, "roughcut")
    if snap_notes:
        assert any(n.startswith("roughcut: ") and "frame snap" in n for n in res.notes)
    else:
        assert not any("frame snap" in n for n in res.notes)


# -- import: the human stage ---------------------------------------------------------------------------
def patched_otio(wsx) -> Path:
    """final.otio as an editor leaves it: the 2nd clip trimmed 5 frames at its start, the dissolve into the 3rd deleted, a marker deleted,
    a clip renamed, every clip id renumbered."""
    export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("otio",))
    doc = json.loads((wsx.ws / "exports" / "final.otio").read_text())
    vt = doc["tracks"]["children"][0]
    kids = vt["children"]
    clips = [k for k in kids if k["OTIO_SCHEMA"].startswith("Clip")]
    clips[1]["source_range"]["start_time"]["value"] += 5
    clips[1]["source_range"]["duration"]["value"] -= 5
    first_tr = next(k for k, c in enumerate(kids) if c["OTIO_SCHEMA"].startswith("Transition"))
    assert kids[first_tr + 1] is clips[2]
    del kids[first_tr]
    assert len(vt["markers"]) == 4
    vt["markers"].pop(1)
    clips[3]["name"] = "renamed by the editor"
    for t in doc["tracks"]["children"]:
        for n, k in enumerate(c for c in t["children"] if c["OTIO_SCHEMA"].startswith("Clip")):
            k["metadata"]["matchcut"]["clip_id"] = f"x{n + 1:03d}"
    path = wsx.tmp / "edited_by_hand.otio"
    path.write_text(json.dumps(doc))
    return path


def test_otio_edited_like_an_editor_gives_exactly_the_expected_human_decisions(wsx):
    final, snapped = final_of(wsx)
    edited = patched_otio(wsx)
    before = canonical_bytes(wsx.ws)
    prov_before = (wsx.ws / "exports" / "provenance.json").read_bytes()
    res = import_timeline(wsx.ws, wsx.cfg, edited)
    assert isinstance(res, ImportResult) and res.stage_path == wsx.ws / "human.json" and res.stage_path.exists()
    assert summary(res.decisions) == expected_edit(snapped, wsx.world.src.rate)
    assert [d.pass_name for d in res.decisions] == ["human", "human"]
    assert res.decisions[0].detail == "human trimmed c002: in +5f" and res.decisions[1].detail.startswith("human changed transition dissolve ")
    assert res.decisions[1].detail.endswith("f -> cut")
    # nothing canonical changed, nor the exported provenance
    assert canonical_bytes(wsx.ws) == before and (wsx.ws / "exports" / "provenance.json").read_bytes() == prov_before
    # the human stage: canonical ids back (the editor renumbered them), lineage keys, canonical history first
    human = Timeline.load(res.stage_path)
    assert human.stage == "human" and [c.id for c in human.clips] == [c.id for c in final.clips]
    assert [c.origin for c in human.clips] == [c.origin or f"{c.shot_index}@{c.source_in:.3f}-{c.source_out:.3f}" for c in final.clips]
    assert [d.to_dict() for d in human.history[: len(final.history)]] == [d.to_dict() for d in final.history]
    assert [d.to_dict() for d in human.history[len(final.history):]] == [d.to_dict() for d in res.decisions]
    # what the editor did not touch keeps matchcut's own values (no snapping noise in the junction history)
    for k in (0, 4):
        assert (human.clips[k].source_in, human.clips[k].source_out) == (final.clips[k].source_in, final.clips[k].source_out)
    assert human.clips[2].transition_in.is_cut and final.clips[3].transition_in.duration == 0.75
    # matchcut's 0.75 s (18.75 frames) went out as the 18 frames error diffusion gave it behind the first dissolve; the editor did not touch it,
    # so the human stage keeps matchcut's own value and the junction shows no human line
    assert human.clips[3].transition_in == final.clips[3].transition_in
    assert human.clips[1].source_in == pytest.approx(final.clips[1].source_in + 0.2, abs=1e-3)
    assert human.fade_in == final.fade_in and human.fade_out == final.fade_out


def test_junction_ids_of_an_edited_file_still_resolve_in_the_provenance_record(wsx):
    """Ported from the feasibility proof: the ids the editor's file still carries, and the id of the marker it deleted, are all in provenance.json."""
    full = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("otio",))
    original = json.loads((wsx.ws / "exports" / "final.otio").read_text())["tracks"]["children"][0]["markers"]
    prov_before = (wsx.ws / "exports" / "provenance.json").read_bytes()
    edited = patched_otio(wsx)
    left = json.loads(edited.read_text())["tracks"]["children"][0]["markers"]
    surviving = [m["metadata"]["matchcut"]["junction_id"] for m in left]
    gone = [m["metadata"]["matchcut"]["junction_id"] for m in original if m["metadata"]["matchcut"]["junction_id"] not in surviving]
    import_timeline(wsx.ws, wsx.cfg, edited)
    record = json.loads((wsx.ws / "exports" / "provenance.json").read_text())["stages"]["final"]["junctions"]
    assert full.files and surviving and len(gone) == 1
    assert all(j in record for j in surviving) and gone[0] in record and (wsx.ws / "exports" / "provenance.json").read_bytes() == prov_before


def test_the_human_stage_is_listed_last_and_shows_in_the_junction_history(wsx):
    assert stage_timeline_paths(wsx.ws)[-1] == ("human", wsx.ws / "human.json") and stage_timeline_paths(wsx.ws)[-2][0] == "final"
    assert [s for s, _ in load_stage_timelines(wsx.ws)][-1] == "final"  # absent until an import
    import_timeline(wsx.ws, wsx.cfg, patched_otio(wsx))
    stages = load_stage_timelines(wsx.ws)
    assert [s for s, _ in stages][-2:] == ["final", "human"]
    js = build_junctions(stages, scorer_for_workspace(wsx.ws, wsx.cfg))
    j_trim = next(j for j in js if j.in_clip == "c002")
    assert any(h.startswith("human:") and "in +0.20s (+5f)" in h for h in j_trim.history()), j_trim.history()
    assert any(d.pass_name == "human" and d.action == "trim" for d in j_trim.decisions)
    j_tr = next(j for j in js if (j.out_clip, j.in_clip) == ("c002", "c003"))
    assert any(h.startswith("human:") and h.endswith("cut") for h in j_tr.history()), j_tr.history()
    assert any(d.pass_name == "human" and d.action == "transition" for d in j_tr.decisions)
    untouched = next(j for j in js if (j.out_clip, j.in_clip) == ("c004", "c005"))
    assert not any(h.startswith("human:") for h in untouched.history()) and not any(d.pass_name == "human" for d in untouched.decisions)


def test_fcpxml_and_edl_edits_give_the_same_decisions_as_otio(wsx):
    final, snapped = final_of(wsx)
    rate = wsx.world.src.rate
    files = write_edited_files(wsx, editor_timeline(snapped, rate), wsx.tmp)
    got = {}
    humans = {}
    for fmt, path in files.items():
        res = import_timeline(wsx.ws, wsx.cfg, path)
        got[fmt] = summary(res.decisions)
        humans[fmt] = [(c.id, c.shot_index, c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration) for c in Timeline.load(res.stage_path).clips]
    assert got["otio"] == expected_edit(snapped, rate) and got["fcpxml"] == got["otio"] and got["edl"] == got["otio"]
    assert humans["fcpxml"] == humans["otio"] == humans["edl"]
    # the by-hand patched OTIO says the same
    assert summary(import_timeline(wsx.ws, wsx.cfg, patched_otio(wsx)).decisions) == got["otio"]


def test_removed_added_and_reordered_clips(wsx):
    final, snapped = final_of(wsx)
    rate = wsx.world.src.rate
    ed = snapped.copy()
    ed.clips = [ed.clips[1], ed.clips[0]] + ed.clips[3:]  # swapped the first two, dropped the third
    ed.clips.append(Clip("zz", ed.clips[0].shot_index, 0.0, 1.0))  # a new piece of footage from a shot that is in the cut, no lineage
    ed.clips[-1].origin = "mystery"
    files = write_edited_files(wsx, ed, wsx.tmp, rename=False)
    res = import_timeline(wsx.ws, wsx.cfg, files["otio"])
    acts = [(d.action, d.clip_ids) for d in res.decisions]
    assert ("removed", ["c003"]) in acts
    assert any(a == "added" and ids[0].startswith("h") for a, ids in acts)
    assert ("reorder", ["c002", "c001", "c004", "c005"]) in acts
    human = Timeline.load(res.stage_path)
    assert [c.id for c in human.clips][:4] == ["c002", "c001", "c004", "c005"] and human.clips[-1].id == "h001" and human.clips[-1].origin == "mystery"
    # same through an EDL
    assert [(d.action, d.clip_ids) for d in import_timeline(wsx.ws, wsx.cfg, files["edl"]).decisions] == acts


def test_clips_without_a_clip_key_are_matched_by_source_range(wsx):
    final, snapped = final_of(wsx)
    doc = json.loads(json.dumps(export_otio(editor_timeline(snapped, wsx.world.src.rate), "final", wsx.world.src, wsx.world.jb["final"])))
    for t in doc["tracks"]["children"]:
        for k in t["children"]:
            if k["OTIO_SCHEMA"].startswith("Clip"):
                k["metadata"] = {}
                k["name"] = "edited"
    doc["metadata"] = {}
    path = wsx.tmp / "stripped.otio"
    path.write_text(json.dumps(doc))
    res = import_timeline(wsx.ws, wsx.cfg, path)
    assert summary(res.decisions) == expected_edit(snapped, wsx.world.src.rate)
    assert any("matched by source range" in n for n in res.notes), res.notes
    assert [c.id for c in Timeline.load(res.stage_path).clips] == [c.id for c in final.clips]


def test_unchanged_file_gives_no_decisions_and_an_identical_stage(wsx):
    final, snapped = final_of(wsx)
    files = write_edited_files(wsx, snapped, wsx.tmp, rename=False)
    for fmt, path in files.items():
        res = import_timeline(wsx.ws, wsx.cfg, path)
        assert res.decisions == [], (fmt, summary(res.decisions))
        human = Timeline.load(res.stage_path)
        assert [c.to_dict() for c in human.clips] == [c.to_dict() for c in final.clips], fmt
        assert [d.to_dict() for d in human.history] == [d.to_dict() for d in final.history]


def test_an_item_of_other_media_is_listed_in_the_notes_and_ignored(wsx):
    final, snapped = final_of(wsx)
    files = write_edited_files(wsx, snapped, wsx.tmp, rename=False)
    # OTIO: a clip that references another file
    doc = json.loads(files["otio"].read_text())
    kids = doc["tracks"]["children"][0]["children"]
    other = copy.deepcopy(kids[0])
    other["name"] = "broll"
    other["metadata"] = {}
    other["media_references"]["DEFAULT_MEDIA"]["target_url"] = "file:///elsewhere/broll.mov"
    kids.insert(1, other)  # between two clips joined by a cut
    files["otio"].write_text(json.dumps(doc))
    # FCPXML: a clip of another asset
    root = ET.fromstring(files["fcpxml"].read_text())
    asset = ET.SubElement(root.find("resources"), "asset", id="r9", name="broll", start="0s", duration="10s", hasVideo="1", format="r1")
    ET.SubElement(asset, "media-rep", kind="original-media", src="file:///elsewhere/broll.mov")
    next(root.iter("spine")).append(ET.Element("asset-clip", name="broll", ref="r9", offset="0s", start="0s", duration="1s"))
    files["fcpxml"].write_text(ET.tostring(root, encoding="unicode"))
    # EDL: an event of another file
    text = files["edl"].read_text()
    files["edl"].write_text(text.replace("\n* MATCHCUT STAGE", "\n006  AX       AA/V  C        00:00:01:00 00:00:02:00 00:00:09:00 00:00:10:00\n* FROM CLIP NAME: broll.mov\n\n* MATCHCUT STAGE", 1))
    for fmt, path in files.items():
        res = import_timeline(wsx.ws, wsx.cfg, path)
        assert any("other media" in n and "broll" in n and "ignored" in n for n in res.notes), (fmt, res.notes)
        assert res.decisions == [], (fmt, summary(res.decisions))
        assert len(Timeline.load(res.stage_path).clips) == len(final.clips)


# -- stage detection ----------------------------------------------------------------------------------
def test_the_stage_comes_from_the_file(wsx):
    w = wsx.world
    stages = {}
    for stage in ("roughcut", "final"):
        tl, _ = exported(w, stage)
        stages[stage] = tl
    rough_otio = wsx.tmp / "rough.otio"
    rough_otio.write_text(json.dumps(export_otio(stages["roughcut"], "roughcut", w.src, w.jb["roughcut"])))
    res = import_timeline(wsx.ws, wsx.cfg, rough_otio)
    record = json.loads((wsx.ws / "human_import.json").read_text())
    assert record["stage"] == "roughcut" and res.stage == "roughcut" and res.decisions == []
    rough_edl = wsx.tmp / "rough.edl"
    rough_edl.write_text(export_edl(stages["roughcut"], "roughcut", w.src, "demo - roughcut"))
    assert import_timeline(wsx.ws, wsx.cfg, rough_edl).stage == "roughcut"
    # FCPXML with both projects: final unless told otherwise; a stage can be named
    fcp = wsx.tmp / "both.fcpxml"
    fcp.write_text(export_fcpxml([(s, stages[s], w.jb[s]) for s in ("roughcut", "final")], w.src))
    res = import_timeline(wsx.ws, wsx.cfg, fcp)
    assert res.stage == "final" and any("roughcut" in n and "final" in n for n in res.notes), res.notes
    assert import_timeline(wsx.ws, wsx.cfg, fcp, stage="roughcut").stage == "roughcut"
    only = wsx.tmp / "only_rough.fcpxml"
    only.write_text(export_fcpxml([("roughcut", stages["roughcut"], w.jb["roughcut"])], w.src))
    assert import_timeline(wsx.ws, wsx.cfg, only).stage == "roughcut"
    # no metadata at all: the project/timeline name carries the stage; failing that, final with a note
    doc = json.loads(rough_otio.read_text())
    doc["metadata"] = {}
    (wsx.tmp / "named.otio").write_text(json.dumps(doc))
    assert import_timeline(wsx.ws, wsx.cfg, wsx.tmp / "named.otio").stage == "roughcut"
    doc["name"] = "my timeline"
    (wsx.tmp / "anon.otio").write_text(json.dumps(doc))
    res = import_timeline(wsx.ws, wsx.cfg, wsx.tmp / "anon.otio")
    assert res.stage == "final" and any("does not say" in n and "final" in n for n in res.notes), res.notes
    # a stage that is not in the workspace
    doc["metadata"] = {"matchcut": {"stage": "nonesuch"}}
    (wsx.tmp / "alien.otio").write_text(json.dumps(doc))
    res = import_timeline(wsx.ws, wsx.cfg, wsx.tmp / "alien.otio")
    assert res.stage == "final" and any("nonesuch" in n for n in res.notes)
    with pytest.raises(InterchangeError, match="no stage nonesuch in "):
        import_timeline(wsx.ws, wsx.cfg, wsx.tmp / "alien.otio", stage="nonesuch")
    with pytest.raises(InterchangeError, match="human stage"):
        import_timeline(wsx.ws, wsx.cfg, wsx.tmp / "alien.otio", stage="human")


def test_import_errors_are_one_line(wsx):
    final, snapped = final_of(wsx)
    bad = wsx.tmp / "bad.otio"
    bad.write_text("{not json")
    notfile = wsx.tmp / "x.txt"
    notfile.write_text("hello")
    cases = [(wsx.tmp / "missing.otio", None, "cannot read "), (bad, None, "invalid OTIO"), (notfile, None, "cannot tell the format"),
             (notfile, "aaf", "^unknown format aaf: use otio, fcpxml or edl$"), (notfile, "edl", "invalid EDL"), (notfile, "fcpxml", "invalid FCPXML")]
    for path, fmt, match in cases:
        with pytest.raises(InterchangeError, match=match) as exc:
            import_timeline(wsx.ws, wsx.cfg, path, fmt)
        assert "\n" not in str(exc.value), str(exc.value)
    assert not (wsx.ws / "human.json").exists() and not (wsx.ws / "human_import.json").exists()
    empty = wsx.tmp / "empty"
    empty.mkdir()
    with pytest.raises(InterchangeError, match="no timelines in "):
        import_timeline(empty, wsx.cfg, patched_otio(wsx))


def test_format_comes_from_the_extension_or_the_argument(wsx):
    final, snapped = final_of(wsx)
    files = write_edited_files(wsx, snapped, wsx.tmp, rename=False)
    odd = wsx.tmp / "export.txt"
    odd.write_text(files["edl"].read_text())
    assert import_timeline(wsx.ws, wsx.cfg, odd, "edl").fmt == "edl"
    xml = wsx.tmp / "export.xml"
    xml.write_text(files["fcpxml"].read_text())
    assert import_timeline(wsx.ws, wsx.cfg, xml).fmt == "fcpxml"
    assert import_timeline(wsx.ws, wsx.cfg, files["otio"]).fmt == "otio"


# -- the import record and the staleness warning ------------------------------------------------------------
def test_human_import_json_records_the_import(wsx):
    final, snapped = final_of(wsx)
    edited = patched_otio(wsx)
    res = import_timeline(wsx.ws, wsx.cfg, edited)
    rec = json.loads((wsx.ws / "human_import.json").read_text())
    assert rec["source_file"] == str(edited.resolve()) and rec["format"] == "otio" and rec["stage"] == "final"
    assert rec["stage_file"] == "final.json" and rec["stage_sha256"] == sha(wsx.ws / "final.json")
    assert rec["notes"] == res.notes and rec["decisions"] == 2 and rec["imported_at"].endswith("+00:00") and rec["schema"] == "matchcut.human_import/1"
    assert not any("stale" in n for n in res.notes)


def test_stale_human_stage_is_warned_by_export_and_import(wsx):
    edited = patched_otio(wsx)
    assert not any("human.json is stale" in n for n in export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",)).notes)  # no human stage yet
    import_timeline(wsx.ws, wsx.cfg, edited)
    assert not any("human.json is stale" in n for n in export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",)).notes)  # fresh
    final = json.loads((wsx.ws / "final.json").read_text())
    final["clips"][0]["source_out"] = round(final["clips"][0]["source_out"] - 0.04, 4)
    (wsx.ws / "final.json").write_text(json.dumps(final, indent=2))
    msg = "human.json is stale: final.json changed since the import (re-export, or re-import the editor's file: matchcut diffs it against the stage it was exported from)"
    assert msg in export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",)).notes
    keep = (wsx.ws / "human.json").read_bytes(), (wsx.ws / "human_import.json").read_bytes()
    assert msg in export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("otio",)).notes
    assert ((wsx.ws / "human.json").read_bytes(), (wsx.ws / "human_import.json").read_bytes()) == keep  # export never touches them
    again = import_timeline(wsx.ws, wsx.cfg, edited)
    assert any(n.startswith(msg) and n.endswith("this import replaces it") for n in again.notes), again.notes  # the replaced stage was stale
    assert not any("human.json is stale" in n for n in export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",)).notes)  # the new import is fresh
    # a missing stage file is stale too
    (wsx.ws / "final.json").unlink()
    assert any("human.json is stale" in n for n in export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("edl",)).notes)


# -- pins and the TUI with human as the last stage ----------------------------------------------------------
def pinned_workspace(wsx):
    """The workspace copy re-run with a pin on its first beat-boundary junction (the editor's decision is in every later stage's history)."""
    stages = load_stage_timelines(wsx.ws)
    j0 = next(j for j in build_junctions(stages) if j.baseline.stage == "roughcut" and j.beat_boundary)
    pin = Pin(j0.out_shot, j0.in_shot, round(j0.current.out_time, 3), round(j0.current.in_time, 3))
    PinStore(wsx.ws).put(pin)
    Pipeline(copy.deepcopy(wsx.cfg)).refine()
    return pin, j0


def test_a_pinned_junction_keeps_its_badge_and_outcome_after_an_import_that_does_not_touch_it(wsx):
    pin, j0 = pinned_workspace(wsx)
    stages = load_stage_timelines(wsx.ws)
    final = dict(stages)["final"]
    assert pin_outcome(final, pin) is not None
    js_before = build_junctions(stages)
    jf = next(j for j in js_before if (j.out_shot, j.in_shot) == (pin.out_shot, pin.in_shot))
    badge_before = render_inspector(jf, "final", pin, pin_decision=pin_outcome(final, pin)).plain
    assert "pinned (locked)" in badge_before and "pending" not in badge_before
    # an edit that trims a clip away from the pinned junction
    w = wsx.world
    snapped, _ = export_timeline(final, w.bounds, w.src)
    keep = {jf.out_clip, jf.in_clip}
    k = next(i for i, c in enumerate(snapped.clips) if i >= 1 and c.id not in keep and c.duration > 0.5)
    ed = snapped.copy()
    ed.clips[k].source_in = round(ed.clips[k].source_in + 3 / float(w.src.rate), 4)
    path = wsx.tmp / "pin_edit.otio"
    path.write_text(json.dumps(export_otio(ed, "final", w.src, build_junctions(stages))))
    res = import_timeline(wsx.ws, wsx.cfg, path)
    assert [d.action for d in res.decisions] == ["trim"] and res.decisions[0].clip_ids == [snapped.clips[k].id]
    after = load_stage_timelines(wsx.ws)
    assert after[-1][0] == "human"
    human = after[-1][1]
    assert pin_outcome(human, pin) is not None and pin_outcome(human, pin).to_dict() == pin_outcome(final, pin).to_dict()
    jh = next(j for j in build_junctions(after) if (j.out_shot, j.in_shot) == (pin.out_shot, pin.in_shot))
    assert jh.pinned and jh.pin_origin == jf.pin_origin
    badge_after = render_inspector(jh, "human", pin, pin_decision=pin_outcome(human, pin)).plain
    assert "pinned (locked)" in badge_after and "pending" not in badge_after
    assert [m for m in badge_after.splitlines()[1].split(" · ") if m.startswith(("pin", "locked", "MATCH"))] == \
        [m for m in badge_before.splitlines()[1].split(" · ") if m.startswith(("pin", "locked", "MATCH"))]
    assert not any(h.startswith("human:") for h in jh.history())  # nothing human happened at this junction
    assert [d.to_dict() for d in jh.decisions] == [d.to_dict() for d in jf.decisions]


def test_human_history_continues_the_latest_canonical_stage_even_when_a_roughcut_was_edited(wsx):
    """The junction code reads a stage's new decisions as ``history[len(previous history):]``: a human stage built on the rough cut still
    continues the final stage's history, so its decisions are not sliced away."""
    final, _ = final_of(wsx)
    rough = wsx.world.by_stage["roughcut"]
    assert len(rough.history) < len(final.history)
    snapped, _ = exported(wsx.world, "roughcut")
    ed = snapped.copy()
    ed.clips[0].source_in = round(ed.clips[0].source_in + 4 / float(wsx.world.src.rate), 4)
    path = wsx.tmp / "rough_edit.otio"
    path.write_text(json.dumps(export_otio(ed, "roughcut", wsx.world.src, wsx.world.jb["roughcut"])))
    res = import_timeline(wsx.ws, wsx.cfg, path)
    assert res.stage == "roughcut" and [d.action for d in res.decisions] == ["trim"]
    human = Timeline.load(res.stage_path)
    assert [d.to_dict() for d in human.history[: len(final.history)]] == [d.to_dict() for d in final.history]
    assert [d.to_dict() for d in human.history[: len(rough.history)]] == [d.to_dict() for d in rough.history]
    j = next(j for j in build_junctions(load_stage_timelines(wsx.ws)) if j.in_clip == snapped.clips[1].id or j.out_clip == snapped.clips[0].id)
    assert any(d.pass_name == "human" for d in j.decisions)


# -- the ported diff on plain timelines ------------------------------------------------------------------------
def test_diff_and_stage_functions_leave_their_inputs_alone():
    rate = Fraction(25)
    tl = Timeline(source="/x/m.mp4", fps=25.0, stage="final", history=[Decision("x", "notes", [], "d")])
    tl.clips = [Clip("c001", 0, 0.0, 2.0, origin="0@0.000-2.000"), Clip("c002", 1, 4.0, 6.0, origin="1@4.000-6.000", transition_in=Transition("dissolve", 0.4))]
    ed = tl.copy()
    ed.clips[0].source_out = 1.8
    snap, before = copy.deepcopy(tl.to_dict()), copy.deepcopy(ed.to_dict())
    decisions = diff_human_edits(tl, ed, rate)
    assert summary(decisions) == [("trim", ["c001"], {"in_frames": 0, "out_frames": -5})]
    stage = human_stage(tl, ed, rate)
    assert tl.to_dict() == snap and ed.to_dict() == before
    assert stage.stage == "human" and stage.history[0].action == "notes" and stage.history[-1].action == "trim"


# -- fix round 1 ---------------------------------------------------------------------------------------------
def test_an_fcpxml_of_another_film_is_refused_like_the_other_formats(wsx):
    final, snapped = final_of(wsx)
    root = ET.fromstring(export_fcpxml([("final", snapped, wsx.world.jb["final"])], wsx.world.src))
    for el in list(root.iter()):  # an editor's file: no matchcut metadata, markers or keywords, and a different movie
        for m in list(el):
            if m.tag in ("metadata", "marker", "keyword"):
                el.remove(m)
    for a in root.iter("asset"):
        a.set("uid", "ABCDEF")
        a.set("name", "otherfilm")
        for r in a.iter("media-rep"):
            r.set("src", "file:///Volumes/other/otherfilm.mov")
    for ac in root.iter("asset-clip"):
        ac.set("name", "otherfilm")
    for pr in root.iter("project"):
        pr.set("name", "Other film")
    other = wsx.tmp / "otherfilm.fcpxml"
    other.write_text(ET.tostring(root, encoding="unicode"))
    with pytest.raises(InterchangeError, match=r"^no clip of the FCPXML spine references the source footage ") as exc:
        import_timeline(wsx.ws, wsx.cfg, other)
    assert "\n" not in str(exc.value) and str(wsx.world.src.path) in str(exc.value)
    assert not (wsx.ws / "human.json").exists()
    # the reader itself, with the workspace's footage given
    with pytest.raises(InterchangeError, match="references the source footage"):
        workspace.import_fcpxml(other.read_text(), None, None, wsx.world.src)


def rough_edit_file(wsx, untouched: bool = False) -> Path:
    w = wsx.world
    snapped, _ = exported(w, "roughcut")
    ed = snapped.copy()
    if not untouched:
        ed.clips[0].source_in = round(ed.clips[0].source_in + 4 / float(w.src.rate), 4)
    path = wsx.tmp / "rough_edit.otio"
    path.write_text(json.dumps(export_otio(ed, "roughcut", w.src, w.jb["roughcut"])))
    return path


def test_the_human_stage_is_stale_when_the_latest_stage_whose_history_it_copies_changed(wsx):
    res = import_timeline(wsx.ws, wsx.cfg, rough_edit_file(wsx))
    rec = json.loads((wsx.ws / "human_import.json").read_text())
    assert (rec["stage"], rec["latest_stage"]) == ("roughcut", "final")
    assert rec["stage_sha256"] == sha(wsx.ws / "roughcut.json") and rec["latest_stage_sha256"] == sha(wsx.ws / "final.json")
    assert stale_human(wsx.ws) is None and [s for s, _ in load_stage_timelines(wsx.ws)][-1] == "human"
    human_bytes = (wsx.ws / "human.json").read_bytes()
    final = json.loads((wsx.ws / "final.json").read_text())  # only the LATEST stage changes (a re-run that left the rough cut alone)
    final["history"].append({"pass_name": "x", "action": "notes", "clip_ids": [], "detail": "a later run", "score": None, "data": {}})
    (wsx.ws / "final.json").write_text(json.dumps(final, indent=2))
    msg = stale_human(wsx.ws)
    assert msg == "human.json is stale: final.json changed since the import (re-export, or re-import the editor's file: matchcut diffs it against the stage it was exported from)"
    assert [s for s, _ in stage_timeline_paths(wsx.ws)][-1] == "final" and [s for s, _ in load_stage_timelines(wsx.ws)][-1] == "final"
    assert (wsx.ws / "human.json").read_bytes() == human_bytes  # never deleted nor rewritten
    assert msg in export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("edl",)).notes
    # a re-import makes it current again
    import_timeline(wsx.ws, wsx.cfg, rough_edit_file(wsx))
    assert stale_human(wsx.ws) is None and [s for s, _ in load_stage_timelines(wsx.ws)][-1] == "human"


def test_a_pin_is_judged_by_the_canonical_stage_once_the_human_stage_is_stale(wsx):
    import_timeline(wsx.ws, wsx.cfg, rough_edit_file(wsx))
    stages = load_stage_timelines(wsx.ws)
    j = build_junctions(stages)[1]
    pin = Pin(j.out_shot, j.in_shot, round(j.current.out_time, 3), round(j.current.in_time, 3))
    PinStore(wsx.ws).put(pin)
    Pipeline(copy.deepcopy(wsx.cfg)).refine()  # rewrites the rough cut (pins are applied to it) and every later stage
    assert stale_human(wsx.ws) is not None
    after = load_stage_timelines(wsx.ws)
    assert [s for s, _ in after][-1] == "final"
    assert pin_outcome(after[-1][1], pin) is not None  # the new pin reads as applied against the latest stage, not "pending"
    assert not any(d.pass_name == "human" for jj in build_junctions(after) for d in jj.decisions)
    assert (wsx.ws / "human.json").exists()


def test_the_stale_message_survives_a_missing_or_unreadable_import_record(wsx):
    import_timeline(wsx.ws, wsx.cfg, rough_edit_file(wsx))
    (wsx.ws / "human_import.json").write_text("{broken")
    assert "human_import.json is missing or unreadable" in stale_human(wsx.ws) and [s for s, _ in load_stage_timelines(wsx.ws)][-1] == "final"
    (wsx.ws / "human_import.json").unlink()
    assert "human_import.json is missing or unreadable" in stale_human(wsx.ws)


def cut_with_fractional_dissolves() -> Timeline:
    """Seven clips on the 25 fps grid; dissolves of 0.3, 0.75, 0.45 and 0.75 s (7.5, 18.75, 11.25, 18.75 frames: error diffusion matters)."""
    tl = Timeline(source="/x/media.mp4", fps=25.0, stage="final")
    ds = [0.0, 0.0, 0.3, 0.75, 0.45, 0.75, 0.0]
    tl.clips = [Clip(f"c{k + 1:03d}", k, 4.0 * k, 4.0 * k + 2.0, transition_in=Transition("dissolve", d) if d else Transition()) for k, d in enumerate(ds)]
    return tl


def deleted_early(tl: Timeline) -> Timeline:
    """What an editor's re-export reads as when the second clip is deleted from matchcut's OTIO by hand."""
    doc = export_otio(tl, "final", fake_src(Fraction(25)), [])
    kids = doc["tracks"]["children"][0]["children"]
    del kids[next(k for k, c in enumerate(kids) if c["OTIO_SCHEMA"].startswith("Clip") and c["metadata"]["matchcut"]["clip_id"] == "c002")]
    return import_otio(doc)[0]


def test_untouched_dissolves_show_no_human_line_when_a_clip_is_deleted_early():
    rate = Fraction(25)
    tl = cut_with_fractional_dissolves()
    human = human_stage(tl, deleted_early(tl), rate)
    assert [d.action for d in human.history[len(tl.history):]] == ["removed"]
    assert [c.transition_in for c in human.clips[1:]] == [c.transition_in for c in tl.clips[2:]]  # 0.3, 0.75, 0.45, 0.75 exactly as matchcut had them
    js = build_junctions([("final", tl), ("human", human)])
    untouched = [j for j in js if j.baseline.stage == "final"]
    assert [(j.out_clip, j.in_clip) for j in untouched] == [("c003", "c004"), ("c004", "c005"), ("c005", "c006"), ("c006", "c007")]
    for j in untouched:
        assert len(j.history()) == 1 and not any(d.pass_name == "human" for d in j.decisions), (j.in_clip, j.history())
    # a dissolve the editor did change takes the editor's frame count
    doc = export_otio(tl, "final", fake_src(rate), [])
    trs = [k for k in doc["tracks"]["children"][0]["children"] if k["OTIO_SCHEMA"].startswith("Transition")]
    trs[1]["in_offset"]["value"], trs[1]["out_offset"]["value"] = 4, 4  # the 0.75 s dissolve into c004 becomes 8 frames
    edited = import_otio(doc)[0]
    decisions = diff_human_edits(tl, edited, rate)
    assert [(d.action, d.clip_ids, d.data["after"]) for d in decisions] == [("transition", ["c003", "c004"], {"kind": "dissolve", "frames": 8})]
    assert human_stage(tl, edited, rate).clips[3].transition_in.duration == 0.32


def test_a_junction_made_adjacent_by_a_human_deletion_says_what_was_removed():
    rate = Fraction(25)
    tl = cut_with_fractional_dissolves()
    human = human_stage(tl, deleted_early(tl), rate)
    j = next(j for j in build_junctions([("final", tl), ("human", human)]) if (j.out_clip, j.in_clip) == ("c001", "c003"))
    assert j.why().startswith("made adjacent by human: removed c002 (shot 1")
    assert j.adjacent_detail.startswith("removed c002")


def test_importing_against_an_earlier_stage_warns_in_one_line(wsx):
    warning = "human stage is based on roughcut, not final: junction histories compare it with final, so changes the later stages made show as differences"
    res = import_timeline(wsx.ws, wsx.cfg, rough_edit_file(wsx, untouched=True))
    assert res.stage == "roughcut" and warning in res.notes
    assert any(n.startswith("the human history continues the final stage's history") for n in res.notes)
    assert json.loads((wsx.ws / "human_import.json").read_text())["notes"] == res.notes
    final_import = import_timeline(wsx.ws, wsx.cfg, patched_otio(wsx))
    assert not any("human stage is based on" in n for n in final_import.notes) and not any("continues the" in n for n in final_import.notes)


def test_a_narrower_export_keeps_the_records_of_the_earlier_ones_while_their_stages_hold(wsx):
    export_workspace(wsx.ws, wsx.cfg)
    out = wsx.ws / "exports"
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",))
    prov = json.loads((out / "provenance.json").read_text())
    assert list(prov["stages"]) == ["roughcut", "final"] and prov["stage_order"] == ["roughcut", "final"]
    assert all(prov["stages"][s]["canonical_sha256"] == sha(wsx.ws / f"{s}.json") for s in prov["stages"])
    fid = json.loads((out / "fidelity.json").read_text())
    assert {s: sorted(by) for s, by in fid["stages"].items()} == {"roughcut": ["edl", "fcpxml", "otio"], "final": ["edl", "fcpxml", "otio"]}
    assert all(d["canonical_sha256"] == sha(wsx.ws / f"{s}.json") for s, by in fid["stages"].items() for d in by.values()) and fid["ok"] is True
    assert "Stage: roughcut" in (out / "fidelity.md").read_text() and any("earlier records" in n for n in res.notes)
    assert [(r.stage, r.fmt) for r in res.results] == [("final", "edl")]
    # matchcut.fcpxml is rewritten with the one project, so the roughcut project's record goes; the other roughcut files are still there
    export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("fcpxml",))
    fid = json.loads((out / "fidelity.json").read_text())
    assert sorted(fid["stages"]["roughcut"]) == ["edl", "otio"] and sorted(fid["stages"]["final"]) == ["edl", "fcpxml", "otio"]
    # a stage file that changed since keeps its record, flagged stale (the exported files are still in the directory)
    rough = json.loads((wsx.ws / "roughcut.json").read_text())
    rough["name"] = "re-run"
    (wsx.ws / "roughcut.json").write_text(json.dumps(rough))
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",))
    prov = json.loads((out / "provenance.json").read_text())
    assert list(prov["stages"]) == ["roughcut", "final"] and prov["stages"]["roughcut"]["stale"] is True and "stale" not in prov["stages"]["final"]
    fid = json.loads((out / "fidelity.json").read_text())
    assert sorted(fid["stages"]["roughcut"]) == ["edl", "otio"] and all(d["stale"] is True for d in fid["stages"]["roughcut"].values())
    assert not any("stale" in d for d in fid["stages"]["final"].values()) and fid["ok"] is True
    assert [(r.stage, r.fmt) for r in res.results] == [("final", "edl")]


def test_a_changed_stage_file_keeps_its_old_record_flagged_stale_and_says_which_files(wsx):
    out = wsx.ws / "exports"
    export_workspace(wsx.ws, wsx.cfg)
    original = (wsx.ws / "roughcut.json").read_text()
    rough = json.loads(original)
    rough["name"] = "re-run"
    (wsx.ws / "roughcut.json").write_text(json.dumps(rough))
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",))
    note = next(n for n in res.notes if "stale" in n)
    assert "\n" not in note and note.startswith("the stage file changed since these were exported")
    assert "roughcut.otio, matchcut.fcpxml (project roughcut), roughcut.edl" in note and "final" not in note
    prov = json.loads((out / "provenance.json").read_text())
    assert prov["stage_order"] == ["roughcut", "final"] and prov["stages"]["roughcut"]["canonical_sha256"] != sha(wsx.ws / "roughcut.json")
    assert prov["stages"]["roughcut"]["junctions"] and prov["stages"]["roughcut"]["clips"]  # the old record itself, not a stub
    fid = json.loads((out / "fidelity.json").read_text())
    assert sorted(fid["stages"]["roughcut"]) == ["edl", "fcpxml", "otio"] and fid["ok"] is True
    md = (out / "fidelity.md").read_text()
    assert md.count("STALE") >= 3 and "Stage: roughcut" in md
    # a stale record is not carried as if it held: a second narrow export still says so
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("otio",))
    assert any("roughcut.otio, matchcut.fcpxml (project roughcut), roughcut.edl" in n for n in res.notes)
    # a stage file that goes back to what was exported makes its record current again
    (wsx.ws / "roughcut.json").write_text(original)
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",))
    assert not any("stale" in n for n in res.notes)
    assert "stale" not in json.loads((out / "provenance.json").read_text())["stages"]["roughcut"]
    assert not any("stale" in d for by in json.loads((out / "fidelity.json").read_text())["stages"].values() for d in by.values())
    # exporting the changed stage again replaces the stale record
    (wsx.ws / "roughcut.json").write_text(json.dumps(rough))
    assert any("stale" in n for n in export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",)).notes)
    res = export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("otio", "edl", "fcpxml"))
    prov = json.loads((out / "provenance.json").read_text())
    fid = json.loads((out / "fidelity.json").read_text())
    assert "stale" not in prov["stages"]["roughcut"] and not any("stale" in d for by in fid["stages"].values() for d in by.values())
    assert not any("stale" in n for n in res.notes)


def test_a_stage_exported_again_in_one_format_leaves_only_the_other_formats_stale(wsx):
    out = wsx.ws / "exports"
    export_workspace(wsx.ws, wsx.cfg)
    rough = json.loads((wsx.ws / "roughcut.json").read_text())
    rough["name"] = "re-run"
    (wsx.ws / "roughcut.json").write_text(json.dumps(rough))
    res = export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("otio",))
    note = next(n for n in res.notes if "stale" in n)
    assert "stale in fidelity.json: matchcut.fcpxml (project roughcut), roughcut.edl;" in note and "roughcut.otio" not in note  # the otio file is current
    assert note.endswith("provenance.json now describes the new export of roughcut") and "stale in provenance.json" not in note
    fid = json.loads((out / "fidelity.json").read_text())["stages"]["roughcut"]
    assert "stale" not in fid["otio"] and fid["edl"]["stale"] is True and fid["fcpxml"]["stale"] is True
    assert "stale" not in json.loads((out / "provenance.json").read_text())["stages"]["roughcut"]


def test_a_fully_stale_stage_names_each_json_file_and_the_whole_stage_in_provenance(wsx):
    export_workspace(wsx.ws, wsx.cfg)
    rough = json.loads((wsx.ws / "roughcut.json").read_text())
    rough["name"] = "re-run"
    (wsx.ws / "roughcut.json").write_text(json.dumps(rough))
    note = next(n for n in export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",)).notes if "stale" in n)
    assert ("stale in fidelity.json: roughcut.otio, matchcut.fcpxml (project roughcut), roughcut.edl; "
            "stale in provenance.json: roughcut (roughcut.otio, matchcut.fcpxml (project roughcut), roughcut.edl)") in note
    assert "now describes the new export" not in note


def read_target(path: Path) -> str:
    return re.search(r"^TIMELINE_FILE = '.*/(\w+)\.otio'$", path.read_text(), re.M).group(1)


def test_an_existing_resolve_script_is_regenerated_when_its_otio_is_exported_again_flag_or_not(wsx):
    out = wsx.ws / "exports"
    script = out / "open_in_resolve.py"
    export_workspace(wsx.ws, wsx.cfg, resolve_script=True)
    assert read_target(script) == "final" and "regenerated" not in " ".join(n for n in export_workspace(wsx.ws, wsx.cfg, resolve_script=True).notes)
    final = json.loads((wsx.ws / "final.json").read_text())
    final["clips"][0]["source_out"] = round(final["clips"][0]["source_out"] - 0.2, 4)  # moves every later cut
    (wsx.ws / "final.json").write_text(json.dumps(final))
    old = script.read_text()
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("otio", "edl"))  # no flag
    assert script.read_text() != old and script in res.files
    assert "open_in_resolve.py regenerated for the new final.otio" in res.notes
    assert not any("open_in_resolve.py was made" in n for n in res.notes)
    # the regenerated script is what the flag would have written
    fresh = script.read_text()
    export_workspace(wsx.ws, wsx.cfg, resolve_script=True)
    assert script.read_text() == fresh
    # a script for another stage than final follows its own stage
    export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("otio",), resolve_script=True)
    assert read_target(script) == "roughcut"
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("otio",))
    assert read_target(script) == "roughcut" and "regenerated" not in " ".join(res.notes)
    res = export_workspace(wsx.ws, wsx.cfg, stages=("roughcut", "final"), formats=("edl",))
    assert "regenerated" not in " ".join(res.notes)  # the OTIO was not exported again
    res = export_workspace(wsx.ws, wsx.cfg, stages=("roughcut", "final"), formats=("otio",))
    assert "open_in_resolve.py regenerated for the new roughcut.otio" in res.notes


def test_no_script_is_written_without_the_flag_or_an_earlier_script(wsx):
    export_workspace(wsx.ws, wsx.cfg)
    assert not (wsx.ws / "exports" / "open_in_resolve.py").exists()
    (wsx.ws / "exports" / "open_in_resolve.py").write_text("print('mine')\n")  # a file of the user's that is not ours: left alone
    export_workspace(wsx.ws, wsx.cfg)
    assert (wsx.ws / "exports" / "open_in_resolve.py").read_text() == "print('mine')\n"


def test_a_script_for_an_otio_that_was_not_exported_again_is_named_in_the_stale_note(wsx):
    out = wsx.ws / "exports"
    export_workspace(wsx.ws, wsx.cfg, resolve_script=True)
    final = json.loads((wsx.ws / "final.json").read_text())
    final["name"] = "re-run"
    (wsx.ws / "final.json").write_text(json.dumps(final))
    old = (out / "open_in_resolve.py").read_text()
    res = export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("otio",))
    note = next(n for n in res.notes if "stale" in n)
    assert note.endswith("open_in_resolve.py was made for the old final.otio") and "final.otio" in note
    assert (out / "open_in_resolve.py").read_text() == old
    export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("otio",))  # regenerates it
    res = export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("otio",))
    assert not any("open_in_resolve.py was made" in n for n in res.notes)


def test_a_stale_record_whose_files_are_gone_is_dropped(wsx):
    out = wsx.ws / "exports"
    export_workspace(wsx.ws, wsx.cfg)
    rough = json.loads((wsx.ws / "roughcut.json").read_text())
    rough["name"] = "re-run"
    (wsx.ws / "roughcut.json").write_text(json.dumps(rough))
    (out / "roughcut.otio").unlink()
    (out / "roughcut.edl").unlink()
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",))
    fid = json.loads((out / "fidelity.json").read_text())
    assert list(fid["stages"]["roughcut"]) == ["fcpxml"] and fid["stages"]["roughcut"]["fcpxml"]["stale"] is True
    assert any("matchcut.fcpxml (project roughcut)" in n and "roughcut.otio" not in n for n in res.notes)
    (out / "matchcut.fcpxml").write_text(export_fcpxml([("final", *exported(wsx.world, "final")[:1], wsx.world.jb["final"])], wsx.world.src))
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",))
    assert list(json.loads((out / "fidelity.json").read_text())["stages"]) == ["final"]
    assert list(json.loads((out / "provenance.json").read_text())["stages"]) == ["final"] and not any("stale" in n for n in res.notes)


def test_a_stale_record_of_a_stage_that_no_longer_exists_keeps_its_place(wsx):
    out = wsx.ws / "exports"
    export_workspace(wsx.ws, wsx.cfg)
    (wsx.ws / "roughcut.json").unlink()
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",))
    prov = json.loads((out / "provenance.json").read_text())
    assert prov["stage_order"] == ["final", "roughcut"] and prov["stages"]["roughcut"]["stale"] is True
    assert any("roughcut.otio" in n for n in res.notes)


def tc_shift(tc: str, frames: int, fps: int = 25) -> str:
    h, m, sec, f = (int(x) for x in tc.split(":"))
    n = ((h * 60 + m) * 60 + sec) * fps + f + frames
    return f"{n // (fps * 3600):02d}:{n // (fps * 60) % 60:02d}:{n // fps % 60:02d}:{n % fps:02d}"


def test_an_edl_edited_by_hand_in_its_text(wsx):
    final, snapped = final_of(wsx)
    assert wsx.world.src.start_tc_frames == 0 and wsx.world.src.rate == 25
    text = export_edl(snapped, "final", wsx.world.src, "demo - final")
    lines = text.splitlines()
    k = next(i for i, l in enumerate(lines) if l.startswith("005 "))
    tcs = lines[k].split()[-4:]  # source in, source out, record in, record out
    assert len(tcs) == 4
    si, so, ri, ro = tcs
    lines[k] = lines[k].replace(" ".join(tcs), " ".join([tc_shift(si, 5), so, ri, tc_shift(ro, -5)]))  # the editor trimmed 5 frames off the head
    assert lines[k] != text.splitlines()[k]
    path = wsx.tmp / "by_hand.edl"
    path.write_text("\n".join(lines) + "\n")
    res = import_timeline(wsx.ws, wsx.cfg, path)
    assert summary(res.decisions) == [("trim", ["c005"], {"in_frames": 5, "out_frames": 0})], (res.decisions, res.notes)


def test_an_fcpxml_edited_by_hand_in_its_text(wsx):
    final, snapped = final_of(wsx)
    xml = export_fcpxml([("final", snapped, wsx.world.jb["final"])], wsx.world.src)
    root = ET.fromstring(xml)
    last = [e for e in root.iter("asset-clip") if "shot7" in e.get("name", "")][0]

    def frames(t):
        num, _, den = t.rstrip("s").partition("/")
        return round(Fraction(int(num), int(den or 1)) * 25)

    def secs(n):
        f = Fraction(n, 25)
        return f"{f.numerator}/{f.denominator}s"

    last.set("start", secs(frames(last.get("start")) + 5))  # the editor slipped the head by 5 frames ...
    last.set("duration", secs(frames(last.get("duration")) - 5))  # ... and shortened the clip
    path = wsx.tmp / "by_hand.fcpxml"
    path.write_text(ET.tostring(root, encoding="unicode"))
    res = import_timeline(wsx.ws, wsx.cfg, path)
    assert summary(res.decisions) == [("trim", ["c005"], {"in_frames": 5, "out_frames": 0})], (res.decisions, res.notes)


# -- the documented example script ------------------------------------------------------------------------
def load_simulator():
    import importlib.util
    path = Path(__file__).resolve().parents[2] / "docs" / "examples" / "m2a" / "simulate_editor.py"
    spec = importlib.util.spec_from_file_location("simulate_editor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("strip", [False, True])
def test_simulate_editor_edits_only_the_mirrored_tracks_of_a_workspace_with_music(wsx, music_file, capsys, strip):
    """The music track A2 has one clip: the example script leaves it alone, and its edits still import as the three decisions."""
    cfg = copy.deepcopy(wsx.cfg)
    cfg.music = MusicConfig(path=str(music_file), start=1.0, gain_db=-6.0)
    export_workspace(wsx.ws, cfg, stages=("final",), formats=("otio",))
    src = wsx.ws / "exports" / "final.otio"
    before = json.loads(src.read_text())
    assert [t["name"] for t in before["tracks"]["children"]] == ["V1", "A1", "A2"]
    edited = wsx.tmp / "edited.otio"
    code = load_simulator().main([str(src), str(edited)] + (["--strip-metadata"] if strip else []))
    out = capsys.readouterr().out
    assert code == 0 and "A2: left alone" in out and "A1: trimmed the head" in out
    after = json.loads(edited.read_text())
    assert after["tracks"]["children"][2]["children"] == before["tracks"]["children"][2]["children"]
    res = import_timeline(wsx.ws, cfg, edited)
    assert [d.action for d in res.decisions] == ["removed", "trim", "transition"]


def test_simulate_editor_copes_with_a_cut_without_dissolves(wsx, capsys):
    export_workspace(wsx.ws, wsx.cfg, stages=("roughcut",), formats=("otio",))
    edited = wsx.tmp / "edited.otio"
    assert load_simulator().main([str(wsx.ws / "exports" / "roughcut.otio"), str(edited)]) == 0
    assert "no transition to turn into a cut" in capsys.readouterr().out


# -- the stage fingerprint and its snapshot: re-importing after the pipeline ran again --------------------------------
def rerun_with_another_target(wsx, seconds: float = 12.0) -> None:
    """The pipeline run again with another target length: the passes (and so final.json) change, the rough cut stays."""
    before = sha(wsx.ws / "final.json")
    cfg = copy.deepcopy(wsx.cfg)
    cfg.target_duration = seconds
    Pipeline(cfg).refine()
    assert sha(wsx.ws / "final.json") != before


def test_every_export_records_the_stage_fingerprint_and_a_snapshot_of_the_stage_file(wsx):
    export_workspace(wsx.ws, wsx.cfg)
    out = wsx.ws / "exports"
    for stage in ("roughcut", "final"):
        digest = sha(wsx.ws / f"{stage}.json")
        snap = out / ".stages" / f"{stage}-{digest[:12]}.json"
        assert snap.read_bytes() == (wsx.ws / f"{stage}.json").read_bytes()
        assert json.loads((out / f"{stage}.otio").read_text())["metadata"]["matchcut"]["stage_sha256"] == digest
        assert f"* MATCHCUT STAGE_SHA256 {digest}" in (out / f"{stage}.edl").read_text().splitlines()
        seq = next(p for p in ET.fromstring((out / "matchcut.fcpxml").read_text()).iter("sequence") if f"{stage}" in {m.get("value") for m in p.iter("md")})
        assert {m.get("key"): m.get("value") for m in seq.iter("md")}["com.matchcut.stage_sha256"] == digest
    assert not list((out / ".stages").glob("*.tmp")) and len(list((out / ".stages").iterdir())) == 2
    # a second export of the same stage files writes nothing new; a pipeline re-run adds a snapshot and never removes the old one
    first = {p.name: p.stat().st_mtime_ns for p in (out / ".stages").iterdir()}
    export_workspace(wsx.ws, wsx.cfg)
    assert {p.name: p.stat().st_mtime_ns for p in (out / ".stages").iterdir()} == first
    rerun_with_another_target(wsx)
    export_workspace(wsx.ws, wsx.cfg)
    assert set(first) < {p.name for p in (out / ".stages").iterdir()} and len(list((out / ".stages").iterdir())) == 3
    # the snapshot is a stage file: it loads
    assert Timeline.load(out / ".stages" / sorted(first)[0]).clips


@pytest.mark.parametrize("fmt", ["otio", "fcpxml", "edl"])
def test_a_reimport_after_a_pipeline_rerun_gives_exactly_the_editors_decisions(wsx, fmt):
    final, snapped = final_of(wsx)
    rate = wsx.world.src.rate
    export_workspace(wsx.ws, wsx.cfg)
    old_hash = sha(wsx.ws / "final.json")
    files = write_edited_files(wsx, editor_timeline(snapped, rate), wsx.tmp)  # fingerprinted with the final.json that was exported
    expected = expected_edit(snapped, rate)
    assert summary(import_timeline(wsx.ws, wsx.cfg, files[fmt]).decisions) == expected  # before the re-run
    rerun_with_another_target(wsx)
    assert stale_human(wsx.ws) is not None  # the earlier import is stale now
    res = import_timeline(wsx.ws, wsx.cfg, files[fmt])
    assert summary(res.decisions) == expected, [d.detail for d in res.decisions]  # not one pipeline change among them
    note = (f"{files[fmt].name} was exported from an earlier final: the editor's changes were diffed against that version; "
            "junction histories compare the human stage with the current final")
    assert note in res.notes and not any("fingerprint" in n for n in res.notes)
    rec = json.loads((wsx.ws / "human_import.json").read_text())
    assert rec["base_snapshot"] == str(wsx.ws / "exports" / ".stages" / f"final-{old_hash[:12]}.json") and rec["base_snapshot_sha256"] == old_hash
    assert rec["stage_sha256"] == sha(wsx.ws / "final.json") and old_hash != rec["stage_sha256"] and note in rec["notes"]
    # the human stage is not stale at once, is listed after the CURRENT final, and its history continues the current final's
    assert stale_human(wsx.ws) is None and [s for s, _ in load_stage_timelines(wsx.ws)][-2:] == ["final", "human"]
    now = Timeline.load(wsx.ws / "final.json")
    human = Timeline.load(wsx.ws / "human.json")
    assert [d.to_dict() for d in human.history[: len(now.history)]] == [d.to_dict() for d in now.history]
    assert [d.action for d in human.history[len(now.history):]] == ["trim", "transition"]
    # and the editor's file imported without the re-run being involved gives the same human decisions
    assert [c.id for c in human.clips] == [c.id for c in final.clips]


def test_the_reviewers_case_three_editor_changes_then_a_rerun_with_a_longer_target(wsx):
    """The editor removed a clip, trimmed another and turned a dissolve into a cut; the pipeline then ran with another target."""
    final, snapped = final_of(wsx)
    rate = wsx.world.src.rate
    export_workspace(wsx.ws, wsx.cfg)
    ed = editor_timeline(snapped, rate)
    ed.clips = ed.clips[:-1]  # the last clip removed
    files = write_edited_files(wsx, ed, wsx.tmp, rename=False)
    first = import_timeline(wsx.ws, wsx.cfg, files["otio"])
    assert sorted(d.action for d in first.decisions) == ["removed", "transition", "trim"]
    rerun_with_another_target(wsx)
    again = import_timeline(wsx.ws, wsx.cfg, files["otio"])
    assert summary(again.decisions) == summary(first.decisions)
    assert not any(d.action == "trim" and d.clip_ids != ["c002"] for d in again.decisions)


def test_a_fingerprint_without_its_snapshot_is_a_one_line_error_naming_the_fix(wsx):
    final, snapped = final_of(wsx)
    export_workspace(wsx.ws, wsx.cfg)
    old_hash = sha(wsx.ws / "final.json")
    files = write_edited_files(wsx, editor_timeline(snapped, wsx.world.src.rate), wsx.tmp)
    rerun_with_another_target(wsx)
    snap = wsx.ws / "exports" / ".stages" / f"final-{old_hash[:12]}.json"
    snap.unlink()
    before = {p.name: p.read_bytes() for p in wsx.ws.glob("human*.json")}
    with pytest.raises(InterchangeError) as exc:
        import_timeline(wsx.ws, wsx.cfg, files["otio"])
    assert str(exc.value) == (f"edited.otio was exported from an earlier final (sha {old_hash[:12]}) and its snapshot "
                              f"exports/.stages/final-{old_hash[:12]}.json is missing: re-export and redo the edit, or restore the snapshot")
    assert {p.name: p.read_bytes() for p in wsx.ws.glob("human*.json")} == before  # nothing was written
    # restored: it imports
    snap.parent.mkdir(exist_ok=True)
    snap.write_bytes(json.dumps({"x": 1}).encode())  # a copy that is not the exported stage is not used
    with pytest.raises(InterchangeError, match="snapshot exports/.stages/final-"):
        import_timeline(wsx.ws, wsx.cfg, files["otio"])


def test_the_snapshot_is_found_next_to_the_imported_file_for_an_export_with_out(wsx):
    final, snapped = final_of(wsx)
    out = wsx.tmp / "elsewhere"
    export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("otio",), out_dir=out)
    doc = json.loads((out / "final.otio").read_text())
    edited = out / "edited.otio"
    doc["tracks"]["children"][0]["children"][0]["source_range"]["start_time"]["value"] += 0  # untouched
    edited.write_text(json.dumps(doc))
    rerun_with_another_target(wsx)
    res = import_timeline(wsx.ws, wsx.cfg, edited)
    assert res.decisions == [] and any("exported from an earlier final" in n for n in res.notes)
    assert json.loads((wsx.ws / "human_import.json").read_text())["base_snapshot"].startswith(str(out / ".stages"))


@pytest.mark.parametrize("fmt", ["otio", "fcpxml", "edl"])
def test_a_file_without_a_fingerprint_is_read_against_the_current_stage_and_says_so(wsx, fmt):
    final, snapped = final_of(wsx)
    files = write_edited_files(wsx, snapped, wsx.tmp, rename=False)
    text = files[fmt].read_text()
    if fmt == "otio":
        doc = json.loads(text)
        del doc["metadata"]["matchcut"]["stage_sha256"]
        text = json.dumps(doc)
    else:
        text = "".join(l for l in text.splitlines(keepends=True) if "stage_sha256" not in l and "STAGE_SHA256" not in l)
    assert "stage_sha256" not in text.lower()
    files[fmt].write_text(text)
    res = import_timeline(wsx.ws, wsx.cfg, files[fmt])
    assert (f"{files[fmt].name} carries no stage fingerprint: if the pipeline ran after it was exported, the pipeline's changes show as human "
            "decisions") in res.notes
    assert res.decisions == [] and "base_snapshot" not in json.loads((wsx.ws / "human_import.json").read_text())
    # after a re-run the pipeline's changes do show as decisions, which is what the note says
    rerun_with_another_target(wsx)
    assert import_timeline(wsx.ws, wsx.cfg, files[fmt]).decisions


def test_a_file_exported_from_the_current_stage_has_no_fingerprint_note(wsx):
    final, snapped = final_of(wsx)
    files = write_edited_files(wsx, snapped, wsx.tmp, rename=False)
    for fmt in files:
        res = import_timeline(wsx.ws, wsx.cfg, files[fmt])
        assert not any("fingerprint" in n or "earlier" in n for n in res.notes), res.notes
        assert "base_snapshot" not in json.loads((wsx.ws / "human_import.json").read_text())


def foreign_otio(wsx, extra_front=(), extra_back=()) -> Path:
    """The exported final.otio as Resolve would write it: no matchcut metadata, the picture track only, other clips before and after."""
    export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("otio",))
    doc = json.loads((wsx.ws / "exports" / "final.otio").read_text())
    doc["metadata"] = {"Resolve_OTIO": {}}
    vt = doc["tracks"]["children"][0]
    vt["markers"] = []
    for k in vt["children"]:
        k["metadata"] = {}
    vt["children"] = list(extra_front) + vt["children"] + list(extra_back)
    doc["tracks"]["children"] = [vt]
    path = wsx.tmp / "foreign.otio"
    path.write_text(json.dumps(doc))
    return path


def test_a_broll_first_or_generator_clips_in_an_otio_without_matchcut_metadata_add_no_decisions(wsx):
    from .test_otio import BROLL, GENERATOR, MISSING, external, media_clip

    plain = import_timeline(wsx.ws, wsx.cfg, foreign_otio(wsx))
    assert plain.decisions == []
    res = import_timeline(wsx.ws, wsx.cfg, foreign_otio(wsx, [media_clip("broll.mov", external(BROLL))]))
    assert res.decisions == [] and any("other media /Volumes/Media/broll.mov ignored" in n for n in res.notes)  # foreign/broll_first.otio
    res = import_timeline(wsx.ws, wsx.cfg, foreign_otio(wsx, [media_clip("Solid Color", dict(GENERATOR))], [media_clip("offline.mov", dict(MISSING))]))
    assert res.decisions == [], [d.detail for d in res.decisions]  # foreign/resolve_edited.otio's generator and offline clips
    assert sum("generator/missing media ignored" in n for n in res.notes) == 2


def dip_workspace(wsx) -> Timeline:
    """The workspace copy whose final cut has a dip to black where it had a dissolve (the final review's "x" variant)."""
    path = wsx.ws / "final.json"
    doc = json.loads(path.read_text())
    k = next(i for i, c in enumerate(doc["clips"]) if c["transition_in"]["duration"] > 0)
    doc["clips"][k]["transition_in"]["kind"] = "dip_to_black"
    path.write_text(json.dumps(doc, indent=2))
    return Timeline.load(path)


def stripped_files(wsx) -> dict:
    """The exports of the final stage with every matchcut mark removed, as an application that keeps none would save them."""
    out = wsx.ws / "exports"
    export_workspace(wsx.ws, wsx.cfg, stages=("final",))
    doc = json.loads((out / "final.otio").read_text())

    def strip(x):
        if isinstance(x, dict):
            x.pop("metadata", None)
            for v in x.values():
                strip(v)
        elif isinstance(x, list):
            for v in x:
                strip(v)

    strip(doc)
    doc["tracks"]["children"] = doc["tracks"]["children"][:1]
    (wsx.tmp / "s.otio").write_text(json.dumps(doc))
    root = ET.fromstring((out / "matchcut.fcpxml").read_text())
    for el in root.iter():
        for md in el.findall("metadata"):
            el.remove(md)
    (wsx.tmp / "s.fcpxml").write_text(ET.tostring(root, encoding="unicode"))
    (wsx.tmp / "s.edl").write_text("".join(l for l in (out / "final.edl").read_text().splitlines(keepends=True) if "MATCHCUT" not in l))
    return {"otio": wsx.tmp / "s.otio", "fcpxml": wsx.tmp / "s.fcpxml", "edl": wsx.tmp / "s.edl"}


def test_an_untouched_dip_to_black_reads_back_the_same_without_matchcut_metadata_except_in_an_edl(wsx):
    """Final review M1: OTIO recognises a Custom_Transition called a dip like FCPXML does; an EDL cannot say it (documented)."""
    tl = dip_workspace(wsx)
    assert any(c.transition_in.kind == "dip_to_black" for c in tl.clips)
    files = stripped_files(wsx)
    assert "matchcut" not in files["otio"].read_text() and "Dip To Black" in files["otio"].read_text()
    got = {fmt: import_timeline(wsx.ws, wsx.cfg, path, stage="final") for fmt, path in files.items()}
    assert got["otio"].decisions == [] and got["fcpxml"].decisions == []
    # an EDL has no way to say "dip": a dissolve, so the transition reads as changed (the documented exception)
    (edl_decision,) = got["edl"].decisions
    assert edl_decision.action == "transition" and re.fullmatch(r"human changed transition dip_to_black (\d+)f -> dissolve \1f", edl_decision.detail), edl_decision.detail
    # with its MATCHCUT TRANSITION comment the same EDL says it
    export_workspace(wsx.ws, wsx.cfg, stages=("final",))
    assert import_timeline(wsx.ws, wsx.cfg, wsx.ws / "exports" / "final.edl").decisions == []


def test_the_edl_report_says_drop_frame_for_drop_frame_media_and_non_drop_otherwise(wsx, monkeypatch):
    """Final review M3: the static NON-DROP entry was reported for the drop-frame media whose EDL says FCM: DROP FRAME."""
    from dataclasses import replace

    from matchcut.interchange.fidelity import EDL_DROP_FRAME, EDL_NON_DROP, KNOWN

    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",))
    assert EDL_NON_DROP in res.results[0].approximated and EDL_DROP_FRAME not in res.results[0].approximated
    src = wsx.world.src
    df = replace(src, rate=Fraction(30000, 1001), duration_frames=int(src.seconds * 29.97), start_tc_frames=107892, start_tc_drop=True)
    monkeypatch.setattr(workspace, "_source", lambda ws, cfg: df)
    res = export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("otio", "edl"), out_dir=wsx.tmp / "df")
    by_fmt = {r.fmt: r for r in res.results}
    assert "FCM: DROP FRAME" in (wsx.tmp / "df" / "final.edl").read_text()
    assert EDL_DROP_FRAME in by_fmt["edl"].approximated and EDL_NON_DROP not in by_fmt["edl"].approximated
    assert [e for e in by_fmt["edl"].approximated if "NON-DROP" in e] == []
    assert not any("DROP FRAME" in e for e in by_fmt["otio"].approximated)
    assert EDL_NON_DROP in KNOWN["edl"]["approximated"]  # the table keeps the default entry
    assert EDL_DROP_FRAME in json.loads((wsx.tmp / "df" / "fidelity.json").read_text())["stages"]["final"]["edl"]["approximated"]


def test_an_fcpxmld_bundle_is_read_through_its_info_fcpxml(wsx):
    """Final Cut Pro's default export since 10.6 is a bundle (a directory ending in .fcpxmld) with Info.fcpxml inside (final review M4)."""
    final, snapped = final_of(wsx)
    files = write_edited_files(wsx, editor_timeline(snapped, wsx.world.src.rate), wsx.tmp)
    bundle = wsx.tmp / "edited.fcpxmld"
    bundle.mkdir()
    (bundle / "Info.fcpxml").write_text(files["fcpxml"].read_text())
    (bundle / "Info.plist").write_text("<plist/>")
    expected = summary(import_timeline(wsx.ws, wsx.cfg, files["fcpxml"]).decisions)
    for kwargs in ({}, {"fmt": "fcpxml"}):
        res = import_timeline(wsx.ws, wsx.cfg, bundle, **kwargs)
        assert res.fmt == "fcpxml" and summary(res.decisions) == expected
    assert json.loads((wsx.ws / "human_import.json").read_text())["source_file"] == str(bundle.resolve())
    # one-line errors: no Info.fcpxml inside, and a directory that is not an FCPXML bundle for another format
    (bundle / "Info.fcpxml").unlink()
    with pytest.raises(InterchangeError, match=r"^cannot read .*edited\.fcpxmld: no Info\.fcpxml inside$"):
        import_timeline(wsx.ws, wsx.cfg, bundle)
    with pytest.raises(InterchangeError, match=r"^cannot read .*edited\.fcpxmld: ") as exc:
        import_timeline(wsx.ws, wsx.cfg, bundle, fmt="otio")
    assert "\n" not in str(exc.value)


# -- the gate checks where the clips sit on the record side (final review M2) ---------------------------------------------
def shift_edl_event(text: str, number: str, frames: int, rate: Fraction) -> str:
    from matchcut.interchange.edl import _frames, _tc

    lines = text.splitlines()
    for k, ln in enumerate(lines):
        if ln.startswith(f"{number} "):
            tcs = ln.split()[-4:]
            ri, ro = (_frames(x, rate, False, 0) for x in tcs[2:])
            lines[k] = ln.replace(tcs[2] + " " + tcs[3], _tc(ri + frames, rate) + " " + _tc(ro + frames, rate))
    return "\n".join(lines) + "\n"


def tamper_fcpxml(xml: str, how: str) -> str:
    root = ET.fromstring(xml)
    if how == "offset":
        clip = [k for k in root.iter("asset-clip") if k.get("lane") is None][2]
        clip.set("offset", "9999/25s")
    elif how == "duration":
        next(root.iter("sequence")).set("duration", "1/25s")
    elif how == "transition":
        next(root.iter("transition")).set("offset", "0s")
    return ET.tostring(root, encoding="unicode")


def tamper_otio(doc: dict, how: str) -> dict:
    a1 = next(t for t in doc["tracks"]["children"] if t["name"] == "A1")
    if how == "truncate":
        a1["children"] = a1["children"][:-1]
    else:  # a clip of the audio mirror one frame short
        next(k for k in a1["children"] if k["OTIO_SCHEMA"].startswith("Clip"))["source_range"]["duration"]["value"] -= 1
    return doc


@pytest.mark.parametrize("fmt, tamper, match", [
    ("edl", lambda t, rate: shift_edl_event(t, "003", 10, rate), r"final: .*gap of 10 frames before event 003|event 003: record frames"),
    ("fcpxml", lambda t, rate: tamper_fcpxml(t, "offset"), r"clip 3 offset"),
    ("fcpxml", lambda t, rate: tamper_fcpxml(t, "duration"), r"sequence duration"),
    ("fcpxml", lambda t, rate: tamper_fcpxml(t, "transition"), r"transition into clip \d+ offset"),
    ("otio", lambda t, rate: json.dumps(tamper_otio(json.loads(t), "truncate")), r"track A1 has \d+ clips for \d+"),
    ("otio", lambda t, rate: json.dumps(tamper_otio(json.loads(t), "short")), r"track A1: clip 1 lasts"),
])
def test_a_misplaced_export_fails_the_gate_that_used_to_pass_it(wsx, monkeypatch, fmt, tamper, match):
    """A shifted EDL record timecode, a moved spine offset, a 1-frame sequence duration, a truncated A1: all were written and passed."""
    rate = wsx.world.src.rate
    name = {"otio": "export_otio", "fcpxml": "export_fcpxml", "edl": "export_edl"}[fmt]
    real = getattr(workspace, name)
    monkeypatch.setattr(workspace, name, lambda *a, **kw: tamper(real(*a, **kw), rate) if fmt != "otio" else tamper_otio_doc(real(*a, **kw), tamper))
    with pytest.raises(InterchangeError) as exc:
        export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=(fmt,))
    assert re.search(rf"^export of {fmt}/final does not match the canonical timeline: ", str(exc.value)) and "\n" not in str(exc.value)
    report = json.loads((wsx.ws / "exports" / "fidelity.json").read_text())
    errors = report["stages"]["final"][fmt]["errors"]
    assert report["ok"] is False and any(re.search(match, e) for e in errors), errors


def tamper_otio_doc(doc: dict, tamper) -> dict:
    return json.loads(tamper(json.dumps(doc), None))


def test_a_legitimate_export_records_that_its_placement_was_checked(wsx):
    res = export_workspace(wsx.ws, wsx.cfg)
    assert all(r.ok for r in res.results)
    for r in res.results:
        assert any(p.startswith("record placement of every clip and transition (") for p in r.preserved), (r.stage, r.fmt, r.preserved)


def test_export_notes_a_frame_rate_the_pipeline_did_not_use(wsx, monkeypatch):
    """Final review M6: the engine's avg_frame_rate and the exports' r_frame_rate differ for some streams (a raw H.264 stream: 25 and 50)."""
    from dataclasses import replace

    assert not any("frame rate" in n for n in export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",)).notes)
    wide = replace(wsx.world.src, rate=Fraction(50), duration_frames=int(wsx.world.src.seconds * 50))
    monkeypatch.setattr(workspace, "_source", lambda ws, cfg: wide)
    res = export_workspace(wsx.ws, wsx.cfg, formats=("edl",), out_dir=wsx.tmp / "wide")
    (note,) = [n for n in res.notes if "frame rate" in n]  # one line for both stages
    assert note.startswith("frame rate: ffprobe reports 50 fps (r_frame_rate) but the pipeline cut at 25 fps (avg_frame_rate): the exports are written at 50 fps")
    assert "\n" not in note
    # 23.976 against 24000/1001 is the same rate
    doc = json.loads((wsx.ws / "final.json").read_text())
    doc["fps"] = 23.976
    (wsx.ws / "final.json").write_text(json.dumps(doc))
    monkeypatch.setattr(workspace, "_source", lambda ws, cfg: replace(wsx.world.src, rate=Fraction(24000, 1001)))
    assert not any("frame rate" in n for n in export_workspace(wsx.ws, wsx.cfg, stages=("final",), formats=("edl",), out_dir=wsx.tmp / "ntsc").notes)
