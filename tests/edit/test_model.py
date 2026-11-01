"""EditContext: frame arithmetic at exact rates, building it from the config; the Pin's optional transition protection."""
from __future__ import annotations

import json
from fractions import Fraction

import pytest

from matchcut.edit.model import EditContext, Refused, context_from, frame_rows, running_frames
from matchcut.interchange.common import layout, record_frames
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore
from matchcut.pipeline.frames import to_frames

from .util import R23976, R25, R2997, R5994, click, ctx_for, cut


@pytest.mark.parametrize("rate", [R25, R2997, R23976, R5994, Fraction(24), Fraction(50)])
def test_seconds_and_frames_round_trip_at_exact_rates(rate):
    ctx = ctx_for(rate)
    for n in list(range(0, 400)) + [10 ** 5, 10 ** 6 + 7, 3 * 10 ** 6 + 1]:  # up to ~28 hours at 29.97
        assert ctx.f(ctx.s(n)) == n
        assert ctx.f(ctx.s(n)) == to_frames(ctx.s(n), rate)


def test_min_frames_and_shot_frames():
    assert ctx_for(R25).min_frames == 19  # 0.75 s at 25 fps = 18.75 frames, rounded up
    assert ctx_for(R25, min_clip=0.8).min_frames == 20  # exactly 20 frames: not 21
    assert ctx_for(R2997).min_frames == 23  # 0.75 * 29.97 = 22.48
    assert ctx_for(R23976).min_frames == 18  # 17.98
    assert ctx_for(R5994).min_frames == 45  # 44.96
    ctx = ctx_for(R2997, frames_per_shot=100)
    assert ctx.shot_frames(0) == (0, 100) and ctx.shot_frames(3) == (300, 400)
    ctx = EditContext(R25, {0: (0.01, 2.0), 1: (2.0, 4.06)})
    # a shot's start and end round like any clip point (as M2a's layout rounds them): a point inside the shot is inside it in frames
    assert ctx.shot_frames(0) == (0, 50) and ctx.shot_frames(1) == (50, 102)
    with pytest.raises(KeyError):
        ctx.shot_frames(7)


def test_max_fraction_is_exact():
    assert ctx_for(frac=0.4).max_fraction * 45 == 18
    assert ctx_for(frac=0.29).max_fraction * 100 == 29  # 0.29 * 100 in floats is 28.999999999999996


def test_a_float_rate_is_snapped_to_the_exact_rate():
    assert EditContext(29.97).rate == R2997
    assert EditContext(23.976).rate == R23976
    with pytest.raises(ValueError):
        EditContext(Fraction(0))


def test_context_from_the_config():
    cfg = PipelineConfig()
    cfg.pacing.min_clip_seconds = 0.5
    cfg.transitions.max_fraction_of_clip = 0.3
    cfg.music.start = 1.25
    cfg.music.snap_window = 0.2
    cfg.music.prefer_downbeats_at_beat_changes = False
    pin = Pin(0, 1, 1.0, 2.0)
    m = click()
    ctx = context_from(cfg, R2997, {0: [0, 2], 1: (2, 4)}, [pin], m)
    assert (ctx.rate, ctx.min_clip_seconds, ctx.max_fraction_of_clip, ctx.music_start, ctx.snap_window, ctx.prefer_downbeats) == (
        R2997, 0.5, 0.3, 1.25, 0.2, False)
    assert ctx.bounds == {0: (0.0, 2.0), 1: (2.0, 4.0)} and ctx.pins == (pin,) and ctx.music is m
    assert context_from(cfg, 29.97, {}).rate == R2997
    with pytest.raises(Exception):
        ctx.rate = R25  # frozen


def test_refused_is_one_line_with_an_optional_index():
    r = Refused("no clip c999", 2)
    assert str(r) == "no clip c999" and r.index == 2 and Refused("x").index is None


@pytest.mark.parametrize("rate", [R25, R2997, R23976, R5994])
def test_frame_rows_agree_with_layout(rate):
    ctx = ctx_for(rate)
    tl = cut(ctx, [("c001", 0, 10, 60), ("c002", 1, 110, 170, "b1", ("dissolve", 9)), ("c003", 2, 205, 250, "b2", ("dip_to_black", 6)),
                   ("c004", 3, 300, 340), ("c005", 4, 420, 480, "b2", ("dissolve", 7))])
    rows, laid = frame_rows(tl, rate), layout(tl, rate)
    for r, l in zip(rows, laid):
        assert (r.in_f, r.out_f, r.tr, r.kind, r.rec, r.head, r.tail) == (l.in_f, l.out_f, l.tr, l.kind, l.rec, l.head, l.tail)
        assert r.visible == l.dur
    assert running_frames(rows) == record_frames(laid) == 50 + 60 + 45 + 40 + 60 - 9 - 6 - 7


def test_frame_rows_report_a_clip_shorter_than_its_transitions_instead_of_refusing():
    ctx = ctx_for()
    tl = cut(ctx, [("c001", 0, 0, 40), ("c002", 1, 100, 104, "b1", ("dissolve", 6)), ("c003", 2, 200, 240, "b1", ("dissolve", 6))])
    rows = frame_rows(tl, ctx.rate)
    assert rows[1].visible == 4 - 3 - 3
    with pytest.raises(Exception):
        layout(tl, ctx.rate)


# -- the Pin's transition protection (decision 2) --------------------------------------------------------------------------------
def test_an_unprotected_pin_is_written_as_before():
    p = Pin(1, 3, 2.0, 4.5, "locked", "", "1@0.000-2.000", "3@4.500-6.000")
    assert "hold_transition" not in p.to_dict() and "held_transition" not in p.to_dict()
    assert Pin.from_dict(p.to_dict()) == p


def test_a_protected_pin_round_trips():
    p = Pin(1, 3, 2.0, 4.5, hold_transition=True, held_transition={"kind": "dissolve", "frames": 8})
    d = p.to_dict()
    assert d["hold_transition"] is True and d["held_transition"] == {"kind": "dissolve", "frames": 8}
    assert Pin.from_dict(json.loads(json.dumps(d))) == p


def test_old_pins_read_as_unprotected_and_a_malformed_protection_keeps_the_pin(tmp_path):
    old = {"out_shot": 1, "in_shot": 3, "out_time": 2.0, "in_time": 4.5, "origin": "locked", "note": ""}
    p = Pin.from_dict(old)
    assert p.hold_transition is False and p.held_transition is None
    (tmp_path / "editorial.json").write_text(json.dumps({"pins": [
        old, dict(old, out_shot=5, hold_transition=True, held_transition={"kind": "dissolve", "frames": 2.5}),
        dict(old, out_shot=6, hold_transition="yes")]}))
    pins = PinStore(tmp_path).load()
    assert [(q.out_shot, q.hold_transition, q.held_transition) for q in pins] == [(1, False, None), (5, True, None), (6, False, None)]


def test_the_pin_store_keeps_the_protection(tmp_path):
    store = PinStore(tmp_path)
    store.put(Pin(1, 3, 2.0, 4.5, hold_transition=True, held_transition={"kind": "cut", "frames": 0}))
    store.put(Pin(4, 5, 7.0, 9.5))
    a, b = PinStore(tmp_path).load()
    assert a.hold_transition and a.held_transition == {"kind": "cut", "frames": 0} and not b.hold_transition
    raw = json.loads((tmp_path / "editorial.json").read_text())["pins"]
    assert "hold_transition" in raw[0] and "hold_transition" not in raw[1]
