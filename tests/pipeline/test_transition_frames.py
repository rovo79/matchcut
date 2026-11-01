"""Transition lengths in whole frames: the canonical final cut stores them, the renderer renders them, and both agree with the
interchange exporters' layout (the frames an editor sees).  Saved workspaces are never rewritten."""
from __future__ import annotations

import hashlib
import json
import random
from fractions import Fraction
from pathlib import Path

import pytest

from matchcut.interchange.common import export_timeline, layout, record_frames
from matchcut.pipeline.config import PipelineConfig, RenderConfig
from matchcut.pipeline.media import probe
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.render import render
from matchcut.pipeline.timeline import Clip, Timeline, Transition

from . import signals as sig

RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30), Fraction(50), Fraction(60000, 1001)]


def _random_timeline(rnd: random.Random, rate: Fraction) -> Timeline:
    fps = float(rate)
    tl = Timeline(source="x.mp4", fps=fps)
    t = 0.0
    for i in range(rnd.randint(2, 12)):
        n = rnd.randint(20, 90)
        start = round(round(t * fps) / fps, 4)
        kind = rnd.choice(["cut", "dissolve", "dip_to_black"]) if i else "cut"
        prev = tl.clips[-1].duration if tl.clips else 1.0
        tr = Transition(kind, round(rnd.uniform(0.05, 0.35 * min(prev, n / fps)), 3)) if kind != "cut" else Transition()  # fits both clips
        tl.clips.append(Clip(f"c{i + 1:03d}", i, start, round((round(t * fps) + n) / fps, 4), transition_in=tr))
        t += n / fps + 1.0
    return tl


@pytest.mark.parametrize("rate", RATES, ids=str)
def test_whole_frame_transitions_match_the_exporters_layout(rate):
    """The same frames as M2a's layout (error diffusion included), for random cuts at common rates; source points untouched."""
    rnd = random.Random(int(rate * 1000))
    for _ in range(200):
        tl = _random_timeline(rnd, rate)
        snapped = tl.snap_transitions()
        before, after = layout(tl, rate), layout(snapped, rate)
        assert [(r.tr, r.kind if r.tr else "cut") for r in before] == [(r.tr, r.kind if r.tr else "cut") for r in after]
        assert [(r.rec, r.cut) for r in before] == [(r.rec, r.cut) for r in after]  # junction positions unchanged
        for c, r in zip(snapped.clips, after):
            assert abs(c.transition_in.duration * float(rate) - r.tr) < 0.01  # whole frames, stored to 4 decimals (0.0012 frame at 23.976)
        assert [(c.source_in, c.source_out) for c in snapped.clips] == [(c.source_in, c.source_out) for c in tl.clips]
        assert tl.to_dict() == Timeline.from_dict(tl.to_dict()).to_dict()  # the original was not touched


def test_a_transition_that_rounds_to_no_frame_becomes_a_cut():
    tl = Timeline(source="x.mp4", fps=25.0)
    tl.clips = [Clip("c001", 0, 0, 2, transition_in=Transition("dissolve", 0.4)), Clip("c002", 1, 3, 5, transition_in=Transition("dissolve", 0.01))]
    out = tl.snap_transitions()
    assert out.clips[0].transition_in.is_cut  # the first clip has no incoming transition
    assert out.clips[1].transition_in == Transition()


def test_the_pipeline_stores_whole_frame_transitions_and_m2a_needs_no_rounding_note(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    pipe = Pipeline(cfg)
    final = pipe.refine(pipe.rough_cut())
    saved = Timeline.load(tmp_path / "ws" / "final.json")
    transitions = [c.transition_in for c in saved.clips[1:] if not c.transition_in.is_cut]
    assert transitions, "the demo cut should have at least one transition"
    fps = probe(str(demo_video)).fps
    assert all(abs(t.duration * fps - round(t.duration * fps)) < 0.01 for t in transitions), [t.duration for t in transitions]
    assert final.to_dict() == saved.to_dict()
    from matchcut.interchange.workspace import export_workspace

    result = export_workspace(tmp_path / "ws", cfg, stages=("final",), formats=("otio",))
    assert result.results and all(r.ok for r in result.results)
    approximated = [a for r in result.results for a in r.approximated]
    assert not any("dissolve lengths rounded" in a for a in approximated), approximated  # nothing left for the exporter to round


@pytest.fixture(scope="module")
def coded(tmp_path_factory) -> Path:
    return sig.make_coded_footage(tmp_path_factory.mktemp("coded_tf") / "coded.mp4", seconds=12)


def test_a_saved_stage_with_fractional_transitions_renders_its_exported_frames_and_is_not_rewritten(coded, tmp_path):
    """A final.json saved before this change (0.336 s = 8.4 frames): rendered on the frames its OTIO export shows; the file is unchanged."""
    media = probe(str(coded))
    tl = Timeline(source=str(coded), fps=media.fps)
    tl.clips = [Clip("c001", 0, 6.0, 8.0), Clip("c002", 1, 1.0, 3.0, transition_in=Transition("dissolve", 0.336)),
                Clip("c003", 2, 9.0, 11.0, transition_in=Transition("dip_to_black", 0.75))]
    path = tmp_path / "final.json"
    tl.save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    rate = Fraction(25)
    rows = layout(tl, rate)
    out = render(Timeline.load(path), media, RenderConfig(crf=18, preset="veryfast"), tmp_path / "old.mp4")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest  # never rewritten
    got = sig.frame_numbers(out)
    assert len(got) == record_frames(rows)  # the exported cut's length, in frames
    # the frames each clip shows alone are the exported layout's: from the end of the incoming transition to the start of the next
    for i, r in enumerate(rows):
        lo = r.rec + r.tr
        hi = rows[i + 1].rec if i + 1 < len(rows) else record_frames(rows)
        assert all(got[k] == r.in_f + (k - r.rec) for k in range(lo, hi)), (i, [(k, got[k], r.in_f + (k - r.rec)) for k in range(lo, hi) if got[k] != r.in_f + (k - r.rec)][:3])
    snapped, _ = export_timeline(tl, None, rate)
    assert snapped.clips[1].transition_in.duration == 0.336  # M2a's export copy keeps the stage's own transition (its fidelity rule)


def test_m2a_export_of_an_existing_workspace_is_unchanged(tmp_path):
    """A workspace saved before this change exports exactly as before: export_timeline does not round transitions."""
    tl = Timeline(source="x.mp4", fps=25.0)
    tl.clips = [Clip("c001", 0, 0.0, 2.0), Clip("c002", 1, 4.0, 6.0, transition_in=Transition("dissolve", 0.336))]
    snapped, _ = export_timeline(tl, None, Fraction(25))
    assert [c.transition_in for c in snapped.clips] == [c.transition_in for c in tl.clips]


def _edl_events(text: str):
    """(kind, transition frames, [source in, source out, record in, record out]) per event line of an EDL."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 8 and parts[0].isdigit() and parts[1] == "AX":
            kind = parts[3]
            out.append((kind, int(parts[4]) if kind == "D" else 0, parts[-4:]))
    return out


def _source(rate: Fraction):
    from matchcut.interchange.common import SourceInfo

    return SourceInfo(path="/x.mp4", url="file:///x.mp4", rate=rate, duration_frames=10**6, width=320, height=180, has_audio=True, size=1, mtime_ns=1)


@pytest.mark.parametrize("rate", RATES, ids=str)
def test_the_pipeline_edl_is_the_m2a_edl_line_for_line(rate):
    """final.edl (Timeline.to_edl) has every event of M2a's EDL with the same four timecodes and dissolve length: half-frame ties and long
    runs of fractional dissolves included.  (It used round() per dissolve, ties to even, and ran the outgoing event through the dissolve.)"""
    from matchcut.interchange.edl import export_edl

    rnd = random.Random(int(rate * 7))
    for _ in range(60):
        tl = _random_timeline(rnd, rate)
        if rnd.random() < 0.5:  # exact ties: transitions of an odd number of half frames
            for c in tl.clips[1:]:
                if not c.transition_in.is_cut:
                    c.transition_in = Transition(c.transition_in.kind, round((2 * rnd.randint(3, 8) + 1) / (2 * float(rate)), 6))
        ours = _edl_events(tl.to_edl("t"))
        theirs = _edl_events(export_edl(tl, "final", _source(rate), "t"))
        assert ours == theirs, (str(rate), ours[:4], theirs[:4])


@pytest.mark.parametrize("rate", [Fraction(25), Fraction(30000, 1001)], ids=str)
def test_the_pipeline_edl_reads_back_as_the_same_cut(rate):
    """M2a's EDL reader reads final.edl back with the same clips, source frames and transitions, and no overlap notes."""
    from matchcut.interchange.edl import import_edl

    rnd = random.Random(5)
    for _ in range(30):
        tl = _random_timeline(rnd, rate).snap_transitions()
        back, notes = import_edl(tl.to_edl("t"), rate, "/x.mp4", None, _source(rate))
        assert not any("overlap" in n or "starts" in n for n in notes), notes
        want = [(r.in_f, r.out_f, r.tr) for r in layout(tl, rate)]
        got = [(r.in_f, r.out_f, r.tr) for r in layout(back, rate)]
        assert got == want


@pytest.mark.parametrize("rate", RATES, ids=str)
def test_whole_frame_transitions_are_idempotent_and_survive_storage_over_long_runs(rate):
    """Stored to the microsecond, a thousand short dissolves re-laid out from the stored values give the same frames, and snapping twice
    changes nothing (to 4 decimals, the stored values drifted from the layout after a few hundred dissolves at 59.94 fps)."""
    fps = float(rate)
    for tr_frames in (1, 2, 3, 6):
        tl = Timeline(source="x.mp4", fps=fps)
        for i in range(1000):
            tl.clips.append(Clip(f"c{i + 1:04d}", i, round(i * 40 / fps, 6), round((i * 40 + 30) / fps, 6),
                                 transition_in=Transition("dissolve", tr_frames / fps) if i else Transition()))
        once = tl.snap_transitions()
        stored = Timeline.from_dict(json.loads(json.dumps(once.to_dict())))
        assert stored.snap_transitions().to_dict() == stored.to_dict()
        assert [r.tr for r in layout(stored, rate)] == [0] + [tr_frames] * 999


def test_the_shared_rounding_is_the_exporters_rounding():
    """The pipeline and the interchange exporters use one implementation of seconds -> frames and of the rate snap."""
    from matchcut.interchange import common
    from matchcut.pipeline import frames

    assert common.to_frames is frames.to_frames and common.COMMON_RATES is frames.COMMON_RATES
    assert all(common.snap_rate(float(r)) == frames.snap_rate(float(r)) for r in frames.COMMON_RATES)


def test_rounding_that_changes_the_cut_is_reported_not_silent():
    """A dissolve that rounds to no frame becomes a cut, and one that rounds to a whole clip cannot be laid out: both are recorded."""
    tl = Timeline(source="x.mp4", fps=25.0)
    tl.clips = [Clip("c001", 0, 0, 2), Clip("c002", 1, 3, 5, transition_in=Transition("dissolve", 0.015)),
                Clip("c003", 2, 6, 6.08), Clip("c004", 3, 7, 9, transition_in=Transition("dip_to_black", 0.07))]
    rounded = tl.snap_transitions()
    found = {(d.action, tuple(d.clip_ids)) for d in tl.transition_rounding_problems(rounded)}
    assert ("transition_dropped", ("c001", "c002")) in found
    assert ("transition_too_long", ("c003", "c004")) in found  # 2 frames (0.07 s) against a 2-frame clip
    ordinary = Timeline(source="x.mp4", fps=25.0)
    ordinary.clips = [Clip("c001", 0, 0, 2), Clip("c002", 1, 3, 5, transition_in=Transition("dissolve", 0.336))]
    assert ordinary.transition_rounding_problems(ordinary.snap_transitions()) == []  # 8.4 -> 8 frames: ordinary rounding, not reported


def test_pins_and_match_cuts_are_untouched_by_whole_frame_transitions():
    """Rounding transitions changes only transition lengths: cut points, locks, tags (pins, match cuts) and order stay as they were."""
    tl = Timeline(source="x.mp4", fps=30000 / 1001)
    tl.clips = [Clip("c001", 0, 0.0, 2.0), Clip("c002", 1, 3.0, 5.0, transition_in=Transition("dissolve", 0.336), locked_in=True, tags=["pinned"]),
                Clip("c003", 2, 6.0, 8.0, locked_out=True, tags=["match_out:3"]), Clip("c004", 3, 8.5, 10.0, locked_in=True, tags=["match_in:2"])]
    tl.clips[0].locked_out = True
    tl.clips[0].tags = ["pinned"]
    out = tl.snap_transitions()
    for a, b in zip(tl.clips, out.clips):
        assert (a.id, a.shot_index, a.source_in, a.source_out, a.locked_in, a.locked_out, a.tags) == (b.id, b.shot_index, b.source_in, b.source_out, b.locked_in, b.locked_out, b.tags)
    assert out.clips[3].transition_in.is_cut  # the match cut stays a hard cut


@pytest.mark.parametrize("rate", [Fraction(30000, 1001), Fraction(25)], ids=str)
def test_a_long_sequence_of_fractional_transitions_stays_on_the_layout(rate):
    """200 clips, every junction a fractional dissolve: the stored frames equal the layout's, and the cut stays within half a frame."""
    fps = float(rate)
    tl = Timeline(source="x.mp4", fps=fps)
    for i in range(200):
        tl.clips.append(Clip(f"c{i + 1:03d}", i, round(i * 3 * 60 / fps, 4), round((i * 3 * 60 + 60) / fps, 4),
                             transition_in=Transition("dissolve", 0.337) if i else Transition()))
    out = tl.snap_transitions()
    assert [r.tr for r in layout(tl, rate)] == [round(c.transition_in.duration * fps) if not c.transition_in.is_cut else 0 for c in out.clips]
    assert abs(record_frames(layout(out, rate)) - tl.duration * fps) <= 0.5 + 1e-6


@pytest.mark.parametrize("with_music", [False, True], ids=["no music", "music"])
def test_a_dissolve_that_rounds_to_no_frame_is_recorded_with_or_without_music(tmp_path, with_music, caplog):
    """At 12 fps a 0.06 s dissolve is under a frame: the final cut turns it into a cut and says so, whether the final stage rounds it or (with
    music) the beat-sync pass, which measures beats on whole-frame positions, rounds it first."""
    from matchcut.pipeline.demo import make_click_track, make_demo_footage

    src = make_demo_footage(tmp_path / "d12.mp4", fps=12)
    cfg = PipelineConfig(source=str(src), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    cfg.transitions.dissolve_seconds = 0.06
    if with_music:
        cfg.music.path = str(make_click_track(tmp_path / "click.wav"))
    pipe = Pipeline(cfg)
    with caplog.at_level("WARNING", logger="matchcut.pipeline"):
        final = pipe.refine(pipe.rough_cut())
    assert any("rounds to no frame" in r.getMessage() for r in caplog.records)  # logged, as well as recorded
    dropped = [d for d in final.history if d.action == "transition_dropped"]
    assert dropped, [(d.pass_name, d.action) for d in final.history[-12:]]
    assert all(c.transition_in.is_cut or c.transition_in.duration * 12 >= 1 - 1e-4 for c in final.clips)  # stored to the microsecond


def test_an_edl_event_with_no_frames_of_its_own_says_so():
    tl = Timeline(source="x.mp4", fps=25.0)
    tl.clips = [Clip("c001", 0, 0, 2), Clip("c002", 1, 3, 3.2, transition_in=Transition("dissolve", 0.2)), Clip("c003", 2, 6, 8, transition_in=Transition("dissolve", 0.2))]
    assert "has no frames of its own" in tl.to_edl("t")
