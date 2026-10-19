"""The fidelity check and report."""
from __future__ import annotations

import copy
import json
from fractions import Fraction

import pytest

from matchcut.interchange import InterchangeError
from matchcut.interchange.common import export_timeline
from matchcut.interchange.edl import export_edl, import_edl
from matchcut.interchange.fcpxml import export_fcpxml, import_fcpxml
from matchcut.interchange.fidelity import FORMATS, KNOWN, FidelityResult, check, differences, write_report
from matchcut.interchange.otio import export_otio, import_otio
from matchcut.pipeline.timeline import Clip, Timeline, Transition

from .conftest import exported
from .test_common import synthetic
from .test_otio import fake_src

RATE24 = Fraction(24)


def roundtrip(world, stage: str, fmt: str):
    """(canonical, exported, reimported) of one workspace stage through one format."""
    canonical = world.by_stage[stage]
    tl, _ = exported(world, stage)
    if fmt == "otio":
        back, _ = import_otio(export_otio(tl, stage, world.src, world.jb[stage]), world.bounds)
    elif fmt == "fcpxml":
        back, _ = import_fcpxml(export_fcpxml([(stage, tl, world.jb[stage])], world.src), stage, world.bounds)
    else:
        back, _ = import_edl(export_edl(tl, stage, world.src, f"demo - {stage}"), world.src.rate, world.src.path, world.bounds)
    return canonical, tl, back


def unsnapped() -> Timeline:
    """Fractional times at 25 fps and two dissolves of 0.5 s (12.5 frames): the snap and the dissolve rounding both show."""
    tl = Timeline(source="/x/media.mp4", fps=25.0, stage="roughcut")
    tl.clips = [Clip("c001", 0, 1.013, 3.0), Clip("c002", 1, 5.0, 7.02, transition_in=Transition("dissolve", 0.5)),
                Clip("c003", 2, 10.0, 12.0, transition_in=Transition("dissolve", 0.5))]
    return tl


# -- check -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("stage", ["roughcut", "final"])
def test_every_format_of_the_workspace_stages_passes(world, stage, fmt):
    canonical, tl, back = roundtrip(world, stage, fmt)
    r = check(canonical, back, fmt, world.src.rate, tl)
    assert isinstance(r, FidelityResult) and r.errors == [] and r.ok and (r.fmt, r.stage) == (fmt, stage)
    assert any("source in/out frames" in p and "exact" in p for p in r.preserved)
    assert any("clip count and order" in p for p in r.preserved) and any("transition" in p for p in r.preserved)
    assert any("record duration" in p for p in r.preserved) and any("clip keys" in p for p in r.preserved)


def test_a_clip_in_point_one_frame_off_fails_with_a_specific_error():
    tl = synthetic()
    for fmt, back in (("otio", import_otio(export_otio(tl, "final", fake_src(), []))[0]),
                      ("edl", import_edl(export_edl(tl, "final", fake_src(), "t"), RATE24, "/x/media.mp4")[0])):
        assert check(tl, back, fmt, RATE24).errors == []
        bad = copy.deepcopy(back)
        bad.clips[1].source_in = round(bad.clips[1].source_in + 1 / 24, 4)
        r = check(tl, bad, fmt, RATE24)
        assert not r.ok
        assert r.errors[0] == "clip 2 (c002, shot 1): source in 121 != 120 frames"
        assert not any("source in/out" in p for p in r.preserved) and any("clip count" in p for p in r.preserved)


def test_other_differences_are_named():
    tl = synthetic()
    back, _ = import_otio(export_otio(tl, "final", fake_src(), []))

    def errs(mutate):
        bad = copy.deepcopy(back)
        mutate(bad)
        return check(tl, bad, "otio", RATE24).errors

    assert errs(lambda b: b.clips.pop()) == ["clip count 2 != 3 (reimported vs exported)"]
    assert errs(lambda b: setattr(b.clips[2], "shot_index", 9))[0] == "clip 3 (c003, shot 2): shot 9 != 2"
    assert any("source out" in e for e in errs(lambda b: setattr(b.clips[0], "source_out", round(b.clips[0].source_out - 1 / 24, 4))))
    assert any("transition length" in e for e in errs(lambda b: setattr(b.clips[1], "transition_in", Transition("dissolve", 0.25))))
    assert any("transition cut != dissolve" in e for e in errs(lambda b: setattr(b.clips[1], "transition_in", Transition())))
    assert any("clip key" in e for e in errs(lambda b: setattr(b.clips[0], "origin", "")))
    assert any("fade metadata" in e for e in errs(lambda b: setattr(b, "fade_in", 0.7)))
    # a moved clip shifts the record time of everything after it as well
    assert any("record" in e for e in errs(lambda b: setattr(b.clips[0], "source_out", round(b.clips[0].source_out + 1 / 24, 4))))


def test_differences_helper_has_a_tolerance():
    a = synthetic()
    b = copy.deepcopy(a)
    b.clips[0].source_in += 1 / 24
    assert differences(a, b, RATE24) and differences(a, b, RATE24, tol=1) == []
    assert differences(a, a, RATE24) == []


def test_unknown_format_is_an_error():
    with pytest.raises(InterchangeError, match=r"^unknown interchange format 'aaf'$"):
        check(synthetic(), synthetic(), "aaf", RATE24)


# -- known and measured items ------------------------------------------------------------------------
def test_known_table_lists_the_losses_of_every_format():
    assert set(KNOWN) == {"otio", "fcpxml", "edl"}
    for fmt in KNOWN:
        text = " ".join(KNOWN[fmt]["approximated"] + KNOWN[fmt]["lost"])
        assert "ducking" in text and "loudness" in text and "fade in/out" in text and "dip to black" in text, fmt
        assert "lossless" not in text.lower()
    assert "md keys" in " ".join(KNOWN["fcpxml"]["lost"]) and "UNVERIFIED" in " ".join(KNOWN["fcpxml"]["lost"])
    edl = " ".join(KNOWN["edl"]["lost"] + KNOWN["edl"]["approximated"])
    assert "music" in edl and "metadata other than comments" in edl and "NON-DROP" in edl and "one video track" in edl


def test_result_always_includes_the_known_items():
    tl = synthetic()
    for fmt in FORMATS:
        r = check(tl, import_otio(export_otio(tl, "final", fake_src(), []))[0], fmt, RATE24)
        assert all(k in r.lost for k in KNOWN[fmt]["lost"]) and all(k in r.approximated for k in KNOWN[fmt]["approximated"])


def test_snapping_and_dissolve_rounding_are_measured_for_a_rough_cut():
    canonical = unsnapped()
    snapped, notes = export_timeline(canonical, None, Fraction(25))
    assert notes  # c001 in moved, c002 out moved
    back, _ = import_edl(export_edl(snapped, "roughcut", fake_src(Fraction(25)), "t"), Fraction(25), "/x/media.mp4")
    r = check(canonical, back, "edl", Fraction(25), snapped)
    assert r.errors == []
    snap = next(a for a in r.approximated if a.startswith("snapped to the frame grid"))
    assert "2 of 3 clips moved" in snap and "largest move 0.50 frame" in snap
    diss = next(a for a in r.approximated if a.startswith("dissolve lengths rounded"))
    assert "2 transitions" in diss and "largest error" in diss
    # an already-snapped, whole-frame cut has neither
    clean = check(snapped, back, "edl", Fraction(25))
    assert not any(a.startswith("snapped") for a in clean.approximated)


# -- the report ------------------------------------------------------------------------------------
def test_report_files_list_every_format_per_stage(tmp_path):
    tl = synthetic()
    canonical = unsnapped()
    snapped, _ = export_timeline(canonical, None, Fraction(25))
    back_rc, _ = import_edl(export_edl(snapped, "roughcut", fake_src(Fraction(25)), "t"), Fraction(25), "/x/media.mp4")
    results = [check(tl, import_otio(export_otio(tl, "final", fake_src(), []))[0], fmt, RATE24) for fmt in FORMATS]
    results += [check(canonical, back_rc, "edl", Fraction(25), snapped)]
    jp, mp = write_report(results, tmp_path / "out")
    assert (jp.name, mp.name) == ("fidelity.json", "fidelity.md") and jp.parent == mp.parent == tmp_path / "out"
    data = json.loads(jp.read_text())
    assert set(data["stages"]) == {"final", "roughcut"} and set(data["stages"]["final"]) == {"otio", "fcpxml", "edl"} and data["ok"] is True
    assert data["stages"]["final"]["edl"]["lost"] == KNOWN["edl"]["lost"] and data["stages"]["final"]["otio"]["errors"] == []
    md = mp.read_text()
    assert "## Stage: final" in md and "## Stage: roughcut" in md
    assert md.count("| format | preserved | approximated | lost | errors |") == 2
    for fmt_name in ("OpenTimelineIO", "FCPXML 1.10", "CMX3600 EDL"):
        assert f"| {fmt_name} |" in md
    for fmt in KNOWN:
        for item in KNOWN[fmt]["lost"]:
            assert item.replace("|", "\\|") in md
    assert "snapped to the frame grid: 2 of 3 clips moved" in md and "dissolve lengths rounded" in md
    assert "lossless" not in md.lower() and "lossless" not in jp.read_text().lower()


def test_report_records_errors_and_marks_a_failing_run(tmp_path):
    tl = synthetic()
    back, _ = import_otio(export_otio(tl, "final", fake_src(), []))
    back.clips[1].source_in += 1 / 24
    r = check(tl, back, "otio", RATE24)
    jp, mp = write_report([r], tmp_path)
    assert json.loads(jp.read_text())["ok"] is False and "clip 2 (c002, shot 1): source in 121 != 120 frames" in mp.read_text()
    assert FidelityResult("otio", "final").to_dict()["ok"] is True


def test_a_stale_entry_round_trips_is_marked_and_does_not_decide_ok(tmp_path):
    old = FidelityResult("otio", "roughcut", errors=["record duration 99 != 100 frames"], canonical_sha256="abc", stale=True)
    again = FidelityResult.from_dict(old.to_dict())
    assert again.stale is True and old.to_dict()["stale"] is True and "stale" not in FidelityResult("otio", "final").to_dict()
    jp, mp = write_report([old, FidelityResult("edl", "final")], tmp_path)
    assert json.loads(jp.read_text())["ok"] is True  # a stale entry describes an earlier export
    md = mp.read_text()
    assert "OpenTimelineIO **STALE**" in md and "CMX3600 EDL |" in md and "Rows marked **STALE**" in md
    assert "STALE" not in write_report([FidelityResult("edl", "final")], tmp_path)[1].read_text()


def test_report_on_the_workspace_has_the_exact_snapping_measurement(world, tmp_path):
    """A rough cut over the workspace's real shots, entered 0.25 frame late and left 0.4 frame early: the snap moves each edge back."""
    rate = world.src.rate
    fps = float(rate)
    clips = []
    for k, (shot, (lo, hi)) in enumerate(sorted(world.bounds.items())[:3]):
        f_in = int(round(lo * fps)) + 10
        f_out = min(f_in + 30, int(hi * fps) - 5)  # well inside the shot
        assert f_out - f_in >= 10 and (f_out + 1) / fps < hi
        clips.append(Clip(f"c{k + 1:03d}", shot, f_in / fps + 0.25 / fps, f_out / fps - 0.4 / fps))
    canonical = Timeline(source=world.video, fps=fps, clips=clips, stage="roughcut")
    snapped, notes = export_timeline(canonical, world.bounds, rate)
    assert len(notes) == 3
    back, _ = import_edl(export_edl(snapped, "roughcut", world.src, "t"), rate, world.src.path, world.bounds, world.src)
    r = check(canonical, back, "edl", rate, snapped)
    assert r.errors == []
    assert "snapped to the frame grid: 3 of 3 clips moved, largest move 0.40 frame (c001)" in " ".join(r.approximated)
    md = write_report([r], tmp_path)[1].read_text()
    assert "3 of 3 clips moved, largest move 0.40 frame" in md


# -- the exported copy must be the stage (review: check never compared the two) ----------------------------
def test_an_export_that_dropped_a_clip_or_moved_one_far_is_an_error_not_an_approximation():
    canonical = unsnapped()
    snapped, _ = export_timeline(canonical, None, Fraction(25))

    def run(bad: Timeline):
        back, _ = import_edl(export_edl(bad, "roughcut", fake_src(Fraction(25)), "t"), Fraction(25), "/x/media.mp4")
        return check(canonical, back, "edl", Fraction(25), bad)

    dropped = copy.deepcopy(snapped)
    dropped.clips.pop()
    r = run(dropped)
    assert not r.ok and r.errors == ["export drops or adds clips: 2 written for 3 in the stage"]
    assert not any("clip count" in p or "source in/out" in p for p in r.preserved) and not any(a.startswith("snapped") for a in r.approximated)

    moved = copy.deepcopy(snapped)
    moved.clips[0].source_in = round(moved.clips[0].source_in + 0.8, 4)  # 20 frames
    r = run(moved)
    assert not r.ok and r.errors[0] == "clip 1 (c001, shot 0): source in moved +19.68 frames by the snap; snapping moves an edge by at most 1 frame"
    assert not any(a.startswith("snapped") and "19" in a for a in r.approximated)

    both = copy.deepcopy(moved)
    both.clips.pop()
    assert len(run(both).errors) == 1  # the count is reported once; the per-clip moves are not compared against a missing clip

    shot = copy.deepcopy(snapped)
    shot.clips[1].shot_index = 7
    assert run(shot).errors[0] == "clip 2 (c002, shot 1): exported as shot 7"
    kind = copy.deepcopy(snapped)
    kind.clips[2].transition_in = Transition("dip_to_black", 0.5)
    assert "exported transition dip_to_black != dissolve" in run(kind).errors[0]
    key = copy.deepcopy(snapped)
    key.clips[0].origin = "elsewhere"
    assert "exported clip key elsewhere" in run(key).errors[0]
    # the legitimate snap (at most one frame per edge) and a clip under two frames long pass
    assert run(snapped).errors == []
    tiny = Timeline(source="/x/media.mp4", fps=25.0, stage="roughcut", clips=[Clip("c001", 0, 1.0, 1.02), Clip("c002", 1, 5.0, 7.0)])
    tsnap, _ = export_timeline(tiny, None, Fraction(25))
    back, _ = import_edl(export_edl(tsnap, "roughcut", fake_src(Fraction(25)), "t"), Fraction(25), "/x/media.mp4")
    assert check(tiny, back, "edl", Fraction(25), tsnap).errors == []


def test_dips_to_black_are_counted_and_the_record_messages_print_the_field_that_differs():
    tl = synthetic()
    tl.clips[1].transition_in = Transition("dip_to_black", 0.5)
    back, _ = import_otio(export_otio(tl, "final", fake_src(), []))
    r = check(tl, back, "otio", RATE24)
    assert "dip to black: 1 of 1 transition in this cut (approximated as described above)" in r.approximated
    assert "dip to black: 0 of 1 transition in this cut" in check(synthetic(), import_otio(export_otio(synthetic(), "final", fake_src(), []))[0], "otio", RATE24).approximated
    assert "dissolve lengths rounded" not in " ".join(r.approximated)  # 12 frames exactly: nothing to report
    # a transition one frame longer moves the record start and the cut; each message prints its own field
    a = synthetic()
    b = copy.deepcopy(a)
    b.clips[1].transition_in = Transition("dissolve", 13 / 24)
    out = differences(a, b, RATE24, rec_tol=0)
    assert "clip 2 (c002, shot 1): transition length 13 != 12 frames" in out
    assert "clip 2 (c002, shot 1): record start 35 != 36 frames" in out
    assert "clip 2 (c002, shot 1): cut at record frame 41 != 42" in out


def test_the_timecode_conformance_caveat_is_in_every_format():
    for fmt in KNOWN:
        assert any("embedded start timecode" in a and "UNVERIFIED" in a for a in KNOWN[fmt]["approximated"]), fmt


def test_a_stage_that_passes_validate_does_not_fail_the_snap_gate():
    """59.94 fps, an out point 1 ms past its shot (what Timeline.validate allows, stored to 3 decimals): the snap pulls it back 1.02 frames."""
    rate = Fraction(60000, 1001)
    fps = float(rate)
    hi = next((k + 0.995) / fps for k in range(200, 400) if (round((k + 0.995) / fps, 3) - (k + 0.995) / fps) * fps > 0.02)
    tl = Timeline(source="/x/m.mp4", fps=fps, stage="roughcut", clips=[Clip("c001", 0, 1.0, round(hi, 3)), Clip("c002", 1, 6.0, 8.0)])
    bounds = {0: (0.0, hi), 1: (5.5, 9.0)}
    assert tl.validate(bounds) == []
    snapped, notes = export_timeline(tl, bounds, rate)
    assert "out moved -1.02 frames" in notes[0]
    back, _ = import_edl(export_edl(snapped, "roughcut", fake_src(rate), "t"), rate, "/x/media.mp4")
    assert check(tl, back, "edl", rate, snapped).errors == []
    # a real fault is still caught
    bad = copy.deepcopy(snapped)
    bad.clips[0].source_out = round(bad.clips[0].source_out - 3 / fps, 4)
    assert check(tl, import_edl(export_edl(bad, "roughcut", fake_src(rate), "t"), rate, "/x/media.mp4")[0], "edl", rate, bad).errors


def sweep_stage(rng, rate: Fraction):
    """A valid random stage (2-40 clips, dissolves and dips) and its shot bounds."""
    fps = float(rate)
    clips, bounds, t = [], {}, 0.0
    for i in range(rng.randint(2, 40)):
        start = t + rng.uniform(0.0, 0.3)
        end = start + rng.uniform(0.6, 4.0)
        bounds[i] = (round(start, 3), round(end, 3))
        a = round(rng.uniform(bounds[i][0], bounds[i][0] + 0.2), 4)
        b = round(rng.uniform(bounds[i][1] - 0.2, bounds[i][1]), 4)
        tin = Transition()
        if i and rng.random() < 0.6:
            d = round(rng.uniform(0.04, min(0.5 * (b - a), 0.5 * (clips[-1].source_out - clips[-1].source_in), 1.2)), 3)
            tin = Transition(rng.choice(["dissolve", "dissolve", "dip_to_black"]), d)
        clips.append(Clip(f"c{i + 1:03d}", i, a, b, transition_in=tin))
        t = end
    return Timeline(source="/m/x.mov", fps=fps, clips=clips, fade_in=0.5, fade_out=0.5, stage="s"), bounds, int(t * fps) + 50


def test_the_placement_check_passes_every_legitimate_stage_at_every_rate():
    """Final review M2: the sweep behind the new gate (random stages x six rates x media timecodes x three formats) has no false positive."""
    import random

    from matchcut.interchange import edl as edl_mod, fcpxml as fcpxml_mod, otio as otio_mod
    from matchcut.interchange.common import SourceInfo, layout
    from matchcut.interchange.workspace import _PLACEMENT_NOTE

    rng = random.Random(11)
    done = 0
    for trial in range(60):
        rate = rng.choice([Fraction(24000, 1001), Fraction(25), Fraction(30000, 1001), Fraction(24), Fraction(60000, 1001), Fraction(50)])
        tc0 = rng.choice([0, 90000, 107892])
        drop = tc0 == 107892 and rate in (Fraction(30000, 1001), Fraction(60000, 1001))
        tl, bounds, frames = sweep_stage(rng, rate)
        if tl.validate(bounds):
            continue
        src = SourceInfo("/m/x.mov", "file:///m/x.mov", rate, frames, 1920, 1080, rng.random() < 0.8, 1, 1, tc0, drop)
        snapped, _ = export_timeline(tl, bounds, src)
        rows = layout(snapped, rate)
        doc = export_otio(snapped, "s", src, [])
        xml = export_fcpxml([("s", snapped, [])], src)
        text = export_edl(snapped, "s", src, "t")
        assert otio_mod.placement_errors(doc, rows, rate) == [] and fcpxml_mod.placement_errors(xml, "s", rows, rate) == []
        assert edl_mod.placement_errors(text, rows, rate) == []
        notes = import_otio(doc, bounds, src)[1] + import_fcpxml(xml, "s", bounds, src)[1] + import_edl(text, rate, src.path, bounds, src)[1]
        assert not [n for n in notes if _PLACEMENT_NOTE.match(n)], (trial, rate, notes)
        done += 1
    assert done >= 40


# -- the music start on the video frame grid --------------------------------------------------------------------------
def test_a_music_start_moved_to_the_frame_grid_is_an_approximation_with_the_size_of_the_move():
    """The tester's D4: 2.0 s at 23.976 fps is written as 2.002 s (frame 48) and the report said nothing."""
    from types import SimpleNamespace

    from matchcut.interchange.fidelity import music_start_note

    note = music_start_note(SimpleNamespace(start=2.0), Fraction(24000, 1001))
    assert note == "music start moved to the video frame grid: 2 s written as 2.002 s (frame 48 at 23.976 fps), +2.0 ms (0.05 frame)"
    assert "\n" not in note
    assert music_start_note(SimpleNamespace(start=2.01), Fraction(25)) == \
        "music start moved to the video frame grid: 2.01 s written as 2 s (frame 50 at 25 fps), -10.0 ms (0.25 frame)"
    assert music_start_note(SimpleNamespace(start=2.0), Fraction(25)) is None  # on the grid: nothing moved
    assert music_start_note(SimpleNamespace(start=-3.0), Fraction(25)) is None  # a negative start plays from 0, as the exports write it
    assert music_start_note(SimpleNamespace(start=0.0), Fraction(24000, 1001)) is None


def test_check_lists_extra_approximations_after_the_known_ones():
    tl = synthetic()
    snapped, _ = export_timeline(tl, None, fake_src())
    back, _ = import_otio(export_otio(snapped, "final", fake_src(), []))
    res = check(tl, back, "otio", RATE24, snapped, extra_approximated=["music start moved to the video frame grid: x"])
    assert res.approximated[-1] == "music start moved to the video frame grid: x"
    assert set(KNOWN["otio"]["approximated"]) <= set(res.approximated)
