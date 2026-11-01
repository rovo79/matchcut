"""Renders, previews and frames of ``matchcut.edit.view``, checked with the renderer suite's identifiable media (``tests.pipeline.signals``):
every rendered frame names its source frame and the source audio names its second, so a render is checked frame by frame against
``model.frame_rows``, at 25 and 29.97 fps, for the whole cut and for a junction's review window at the exact rate."""
from __future__ import annotations

import json
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Callable, Dict

import numpy as np
import pytest
from PIL import Image

from matchcut.edit import session as S
from matchcut.edit import view as V
from matchcut.edit.model import frame_rows
from matchcut.pipeline.media import MediaError
from matchcut.pipeline.timeline import Timeline
from matchcut.review.window import review_window

from tests.pipeline import signals as sig

from .conftest import write_workspace

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

RATES = ["25", "30000/1001"]
EDIT = [{"op": "set_transition", "junction": 3, "kind": "dissolve", "frames": 6}, {"op": "trim", "clip": "c002", "edge": "out", "frames": -3},
        {"op": "slip", "clip": "c004", "frames": 7}]


@pytest.fixture(scope="session")
def coded(tmp_path_factory) -> Callable[[str], Path]:
    """Frame-numbered footage with a pitch-stepped tone, per rate (34 s: the workspaces' 8 shots of 100 frames)."""
    root = tmp_path_factory.mktemp("coded")
    made: Dict[str, Path] = {}

    def get(rate: str) -> Path:
        if rate not in made:
            made[rate] = sig.make_coded_footage(root / f"coded-{rate.replace('/', '_')}.mp4", seconds=34, rate=rate)
        return made[rate]
    return get


@pytest.fixture(scope="session")
def ticks(tmp_path_factory) -> Path:
    return sig.make_tick_music(tmp_path_factory.mktemp("ticks") / "ticks.wav")


def coded_ws(coded, tmp_path: Path, rate: str, name: str = "ws") -> Path:
    w = write_workspace(tmp_path / name, coded(rate), Fraction(rate))
    cfg = json.loads((w / "config.json").read_text())
    cfg["render"]["proxy_width"] = 320  # the footage's own width: the stripes are read at 320x180
    (w / "config.json").write_text(json.dumps(cfg))
    S.start(w)
    return w


def edited(w: Path) -> Dict:
    p = S.propose(w, EDIT)
    return p


def exact(tl: Timeline, rate: str) -> Timeline:
    return V._exact(tl, Fraction(rate))


def assert_cut_frames(path: Path, tl: Timeline, rate: str, total: int) -> None:
    got = sig.frame_numbers(path)
    assert len(got) == total
    want = sig.expected_frames(tl, float(Fraction(rate)))
    assert len(want) >= 0.8 * total
    wrong = [(k, got[k], v) for k, v in want.items() if got[k] != v]
    assert not wrong, wrong[:5]


def assert_audio(path: Path, tl: Timeline, record_from: float = 0.0, record_to: float = 1e9) -> int:
    """The source tone of every clip, where the file shows it (record seconds ``record_from``..``record_to`` of ``tl`` at the file's start):
    present, and the right second's pitch; the audio lasts as long as the video.  Returns how many clips were checked."""
    st = sig.streams(path)
    assert "audio" in st, "no audio stream"
    assert abs(float(st["audio"]["duration"]) - float(st["video"]["duration"])) <= 0.03
    y = sig.audio(path)
    checked = 0
    for i in range(len(tl.clips)):
        found = sig.tone_window(tl, i)
        if found is None:
            continue
        t, second = found
        if t < record_from or t + 0.2 > record_to:
            continue
        at = t - record_from
        want = sig.tone_hz(second)
        assert sig.dominant_tone(y, at) == pytest.approx(want, abs=10), (i, at, sig.dominant_tone(y, at), want)
        assert sig.band_rms(y, at, 0.2, want - 15, want + 15) > 0.02
        checked += 1
    return checked


# ----------------------------------------------------------------------------------------------- whole-cut renders and previews
@pytest.mark.parametrize("rate", RATES)
def test_preview_then_render_of_the_applied_revision(coded, tmp_path, rate):
    w = coded_ws(coded, tmp_path, rate)
    p = edited(w)
    pv = V.preview(w, p["proposal"])
    assert pv["kind"] == "cut" and not pv["cached"] and pv["proposal"] == p["proposal"] and pv["changes"] == p["changes"]
    path = Path(pv["path"])
    assert path.parent == w / "edits" / "renders" and path.name.startswith("cut-") and pv["file"] == f"edits/renders/{path.name}"
    tl = exact(Timeline.from_dict(json.loads((w / "edits" / "proposals" / f"{p['proposal']}.json").read_text())["timeline"]), rate)
    total = frame_rows(tl, Fraction(rate))[-1].rec_out
    assert pv["frames"] == total == 262 - 6 - 3
    assert_cut_frames(path, tl, rate, total)
    assert assert_audio(path, tl) == 5
    assert S.load(w).revision_ids() == ["r000"]  # a preview writes no revision
    S.apply(w, p["proposal"])
    r = V.render(w)
    assert r["revision"] == "r001" and r["path"] == pv["path"] and r["cached"]  # the very file the preview made
    r0 = V.render(w, "r000")
    assert r0["path"] != r["path"] and not r0["cached"]
    t0 = exact(S.load(w).timeline("r000"), rate)
    assert_cut_frames(Path(r0["path"]), t0, rate, 262)


def test_render_names_are_deterministic_and_reused(coded, tmp_path):
    a, b = coded_ws(coded, tmp_path, "25", "a"), coded_ws(coded, tmp_path, "25", "b")
    ra = V.render(a)
    rb = V.render(b)
    assert Path(ra["path"]).name == Path(rb["path"]).name  # the same cut, source and settings: the same name in either workspace
    mtime = Path(ra["path"]).stat().st_mtime_ns
    again = V.render(a)
    assert again["cached"] and again["path"] == ra["path"] and Path(ra["path"]).stat().st_mtime_ns == mtime
    full = V.render(a, quality="full")
    assert full["path"] != ra["path"] and full["quality"] == "full"
    cfg = json.loads((a / "config.json").read_text())
    cfg["render"]["crf"] = 30
    (a / "config.json").write_text(json.dumps(cfg))
    assert V.render(a, quality="full")["path"] != full["path"]  # a render setting is part of the name
    names = sorted(p.name for p in (a / "edits" / "renders").iterdir() if p.is_file())
    assert all(n.startswith("cut-") and n.endswith(".mp4") and len(n) == len("cut-") + 16 + 4 for n in names)
    assert not list((a / "edits" / "renders" / ".tmp").iterdir())


def test_a_failed_render_leaves_no_file(coded, tmp_path, monkeypatch):
    import matchcut.pipeline.render as pr
    w = coded_ws(coded, tmp_path, "25")

    real = pr.run

    def broken(cmd, capture=True):
        if "-filter_complex" not in cmd:  # a probe: as it is
            return real(cmd, capture)
        Path(cmd[-1]).write_bytes(b"\x00" * 1000)  # ffmpeg wrote half a file, then failed
        raise MediaError("ffmpeg failed\nboom: the disk is full")
    monkeypatch.setattr(pr, "run", broken)
    with pytest.raises(S.SessionError, match="the render failed: boom: the disk is full"):
        V.render(w)
    with pytest.raises(S.SessionError, match="the render failed"):
        V.render(w, junction=2)
    d = w / "edits" / "renders"
    assert [p for p in d.rglob("*") if p.is_file()] == []
    monkeypatch.undo()
    r = V.render(w)
    assert not r["cached"] and Path(r["path"]).stat().st_size > 1000


# ----------------------------------------------------------------------------------------------- junction windows
@pytest.mark.parametrize("rate", RATES)
def test_junction_windows_at_the_exact_rate(coded, tmp_path, rate):
    w = coded_ws(coded, tmp_path, rate)
    p = edited(w)
    S.apply(w, p["proposal"])
    tl = exact(S.load(w).timeline(), rate)
    want = sig.expected_frames(tl, float(Fraction(rate)))
    fr = Fraction(rate)
    for junction in (1, "s1-s3", 3, 4):  # a cut from the first clip, a dissolve, a new dissolve, a cut inside the cut
        r = V.render(w, junction=junction)
        assert r["kind"] == "window" and Path(r["path"]).name.startswith("window-")
        s_f, e_f = r["record_frames"]
        rows = frame_rows(tl, fr)
        j = r["junction"]["n"] - 1
        assert r["frames"] == e_f - s_f and r["cut_offset_frames"] == rows[j + 1].rec - s_f
        assert s_f == max(0, rows[j + 1].rec - round(2.0 * float(fr)))
        got = sig.frame_numbers(Path(r["path"]))
        assert len(got) == e_f - s_f, (junction, len(got), e_f - s_f)
        wrong = [(k, got[k], want[s_f + k]) for k in range(len(got)) if (s_f + k) in want and got[k] != want[s_f + k]]
        assert not wrong, (junction, wrong[:5])
        assert assert_audio(Path(r["path"]), tl, s_f / float(fr), e_f / float(fr)) >= 1
        assert V.render(w, junction=junction)["cached"]


def test_the_window_differs_from_m2bs_rounded_rate_window_at_29_97(coded, tmp_path):
    """Why the window is laid out in frames: M2b's window, from a timeline whose fps is rounded to 29.97 and whose seconds are rounded to
    4 decimals, starts on another frame for a junction late in the cut; this one starts exactly where frame_rows says."""
    w = coded_ws(coded, tmp_path, "30000/1001")
    tl = S.load(w).timeline()
    assert tl.fps == 29.97
    rate = Fraction(30000, 1001)
    m2b = review_window(tl, 3, 2.0, 2.0)
    ours, info = V.review_window_frames(V._exact(tl, rate), 3, rate, 2.0, 2.0)
    assert info["record_frames"][0] == frame_rows(tl, rate)[4].rec - 60
    assert m2b.start * float(rate) != pytest.approx(info["record_frames"][0], abs=1e-3)  # 2.0 s is 59.94 frames: off the frame grid


def test_preview_of_a_junction_window(coded, tmp_path):
    w = coded_ws(coded, tmp_path, "30000/1001")
    p = edited(w)
    pv = V.preview(w, p["proposal"], junction=3)
    assert pv["kind"] == "window" and pv["junction"] == {"n": 3, "id": "s3-s5"}
    S.apply(w, p["proposal"])
    assert V.render(w, junction=3)["path"] == pv["path"]


def test_a_window_with_music(coded, ticks, tmp_path):
    w = coded_ws(coded, tmp_path, "25")
    cfg = json.loads((w / "config.json").read_text())
    cfg["music"].update(path=str(ticks), gain_db=-6.0)
    (w / "config.json").write_text(json.dumps(cfg))
    r = V.render(w, junction=2)
    assert r["music"]
    tl = exact(S.load(w).timeline(), "25")
    s_f, e_f = r["record_frames"]
    y = sig.audio(Path(r["path"]))
    assert max(sig.band_rms(y, t, 0.5, sig.TICK_HZ - 100, sig.TICK_HZ + 100) for t in (0.2, 1.0, 2.0)) > 0.0002
    assert assert_audio(Path(r["path"]), tl, s_f / 25, e_f / 25) >= 1
    got = sig.frame_numbers(Path(r["path"]))
    want = sig.expected_frames(tl, 25.0)
    assert len(got) == e_f - s_f and all(got[k] == want[s_f + k] for k in range(len(got)) if (s_f + k) in want)


# ----------------------------------------------------------------------------------------------- frames
def stripes(path: Path) -> int:
    """The frame number a JPEG of the coded footage shows (its stripes, read with room for compression)."""
    img = np.asarray(Image.open(path).convert("L"), dtype=np.float32)
    assert img.shape == (180, 320)
    stripe = 320 // sig.BITS
    return sum(1 << i for i in range(sig.BITS) if img[:, i * stripe + 3:(i + 1) * stripe - 3].mean() > 128)


@pytest.mark.parametrize("rate", RATES)
def test_frames_are_the_junctions_outgoing_and_incoming_frames(coded, tmp_path, rate):
    w = coded_ws(coded, tmp_path, rate)
    p = edited(w)
    S.apply(w, p["proposal"])
    tl = S.load(w).timeline()
    rows = frame_rows(tl, Fraction(rate))
    for n in (1, 2, 3, 4):
        f = V.frames(w, n)
        out, inc = f["outgoing"], f["incoming"]
        assert out["source_frame"] == rows[n - 1].out_f - 1 and inc["source_frame"] == rows[n].in_f
        assert out["record_frame"] == rows[n - 1].rec_out - 1 and inc["record_frame"] == rows[n].rec
        assert (out["clip"], inc["clip"]) == (tl.clips[n - 1].id, tl.clips[n].id)
        assert f["transition"] == {"kind": rows[n].kind, "frames": rows[n].tr} and f["rate"] == str(Fraction(rate))
        for side in (out, inc):
            path = Path(side["path"])
            assert path.parent == w / "edits" / "renders" and path.suffix == ".jpg" and side["file"] == f"edits/renders/{path.name}"
            with Image.open(path) as im:
                assert im.format == "JPEG"
            assert stripes(path) == side["source_frame"], (n, side)
    again = V.frames(w, 2)
    assert again["outgoing"]["cached"] and again["incoming"]["cached"]
    old = V.frames(w, 3, "r000")
    assert old["revision"] == "r000" and old["transition"] == {"kind": "cut", "frames": 0}
    assert stripes(Path(old["incoming"]["path"])) == frame_rows(S.load(w).timeline("r000"), Fraction(rate))[3].in_f
    with pytest.raises(S.SessionError, match="no junction 9"):
        V.frames(w, 9)
    assert not list((w / "edits" / "renders" / ".tmp").iterdir())


def test_a_stale_session_still_renders_and_says_so(coded, tmp_path):
    w = coded_ws(coded, tmp_path, "25")
    tl = Timeline.load(w / "final.json")
    tl.clips[0].source_out = round(tl.clips[0].source_out - 0.2, 6)
    tl.save(w / "final.json")
    f = V.frames(w, 1)
    assert f["stale"] and "changed since" in f["stale_reason"]
    assert stripes(Path(f["outgoing"]["path"])) == 59
