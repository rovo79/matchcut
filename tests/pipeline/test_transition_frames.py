"""Transition lengths in whole frames: the canonical final cut stores them, the renderer renders them, and both agree with the
interchange exporters' layout (the frames an editor sees).  Saved workspaces are never rewritten."""
from __future__ import annotations

import hashlib
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
    """(transition frames, source in, source out, record in, record out) per event line, as frame numbers, from a pipeline EDL."""
    def frames(tc: str, base: int) -> int:
        h, m, sec, f = (int(x) for x in tc.split(":"))
        return ((h * 60 + m) * 60 + sec) * base + f
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 8 and parts[1] == "AX" and parts[0].isdigit():
            tr = int(parts[4]) if parts[3] == "D" else 0
            tcs = parts[-4:]
            out.append((parts[3], tr, tcs))
    return out


@pytest.mark.parametrize("rate", RATES, ids=str)
def test_the_pipeline_edl_agrees_with_the_exporters_layout(rate):
    """final.edl (Timeline.to_edl) gives every dissolve the frames the M2a layout gives it, at the same record frame: half-frame ties and
    long runs of fractional dissolves included (it used Python's round(), ties to even, one dissolve at a time)."""
    rnd = random.Random(int(rate * 7))
    base = int(round(float(rate)))
    for _ in range(100):
        tl = _random_timeline(rnd, rate)
        if rnd.random() < 0.5:  # exact ties: transitions of an odd number of half frames
            for c in tl.clips[1:]:
                if not c.transition_in.is_cut:
                    c.transition_in = Transition(c.transition_in.kind, round((2 * rnd.randint(3, 8) + 1) / (2 * float(rate)), 6))
        rows = layout(tl, rate)
        dissolves = [e for e in _edl_events(tl.to_edl("t")) if e[0] == "D"]
        want = [(r.tr, r.rec) for r in rows if r.tr]
        got = []
        for _, tr, tcs in dissolves:
            h, m, sec, f = (int(x) for x in tcs[2].split(":"))
            got.append((tr, ((h * 60 + m) * 60 + sec) * base + f))
        assert got == want, (str(rate), got, want)


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
