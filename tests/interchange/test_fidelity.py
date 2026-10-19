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


def test_report_on_the_workspace_has_the_rough_cut_snapping_note_when_the_snap_moved_clips(world, tmp_path):
    results, moved = [], 0
    for stage in ("roughcut", "final"):
        canonical, tl, back = roundtrip(world, stage, "edl")
        moved += len([1 for a, b in zip(canonical.clips, tl.clips) if abs(a.source_in - b.source_in) * 25 > 0.01 or abs(a.source_out - b.source_out) * 25 > 0.01])
        results.append(check(canonical, back, "edl", world.src.rate, tl))
    md = write_report(results, tmp_path)[1].read_text()
    assert ("snapped to the frame grid" in md) == bool(moved)
