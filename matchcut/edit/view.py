"""Looking at a session's cut: ``inspect``, the frames at a junction (``frames``), renders of a revision or of an accepted proposal before it
is applied (``render``, ``preview``), and the transition protection of an editor pin (``pin_transition``).

Everything here reads the session; only ``pin_transition`` changes the workspace (``editorial.json``), and the renders write files under
``WS/edits/renders/``:

- a whole cut, through the pipeline's renderer (``pipeline.render.render``): ``cut-<key>.mp4``;
- one junction's review window, through M2b's ``review.render.render_window``: ``window-<key>.mp4``;
- a junction's outgoing and incoming frames, as JPEG files for vision models: ``frame-<key>.jpg``.

``<key>`` is the first 16 hex characters of the sha256 of everything the file depends on (what the timeline renders, the window, the
source file and its frame rate, the render and review settings, the quality, the music track and its settings): an existing file with the
same name is the same render and is reused, and a preview of a proposal is the very file the render of its revision would be.  Every file
is written to a temporary file first and moved into place with ``os.replace``, so a failed render leaves nothing behind.

Frames are counted at the source's exact rate (``EditContext.rate``, e.g. 30000/1001), never at a timeline's rounded ``fps`` (29.97): the
renderer is handed an exact-rate copy of the timeline and of the source's ``MediaInfo``, and a junction's review window is laid out in
whole frames (``review_window_frames``), so a window starts and ends on exactly the frames ``model.frame_rows`` says.
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import io
import json
import math
import os
import re
import time
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from matchcut.interchange.common import junction_ids
from matchcut.pipeline import render as pipeline_render
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore, pin_outcome
from matchcut.pipeline.frames import to_frames
from matchcut.pipeline.media import MediaError, MediaInfo, _require, probe, run
from matchcut.pipeline.timeline import HUMAN_STAGE, TRANSITION_KINDS, Timeline
from matchcut.review.render import held

from . import rules
from . import session as S
from .model import EditContext, Refused, Row, frame_rows, running_frames
from .ops import MAX_FRAMES, beat_frame, find_junction, held_pins, pin_label
from .ops import held_transition as held_of
from .session import SessionError

RENDER_VERSION = 2  # part of every render key: a change to how a file is made changes its name
QUALITIES = ("proxy", "full")
MAX_FRAME_WIDTH = 1280  # a junction frame wider than this is scaled down (a vision model gains nothing from a 4K still)
JPEG_QUALITY = 92
TMP_MAX_AGE_SECONDS = 3600.0  # a temporary render file older than this was left by a killed render
TMP_QUIET_SECONDS = 60.0  # ... and one whose process is gone, once it has not been written for this long


# ----------------------------------------------------------------------------------------------- reading the session
@contextlib.contextmanager
def _reading(ws: Path, rev: Optional[str]) -> Iterator[Tuple[S.Session, str, Timeline]]:
    """(the session, the revision id, its timeline) under the shared session lock."""
    rev = None if rev is None else S.check_revision_id(rev)
    with S._locked(Path(ws), shared=True):
        s = S.load(ws)
        rid = rev or s.head
        yield s, rid, s.timeline(rid)


def _junction(tl: Timeline, ref: Any) -> int:
    try:
        return find_junction(tl, ref)
    except Refused as exc:
        raise SessionError(str(exc)) from None


def _config(ws: Path) -> PipelineConfig:
    try:
        return PipelineConfig.load(Path(ws) / "config.json")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SessionError(f"cannot read {Path(ws) / 'config.json'} ({exc})") from None


def _media(ws: Path, cfg: PipelineConfig, rate: Fraction) -> MediaInfo:
    """The source footage the session's frames count in (M2a's choice of file, as ``session.workspace_context``), as the renderer reads
    it, with its frame rate the exact ``rate`` (the renderer counts its frames at ``media.fps``)."""
    from matchcut.interchange import InterchangeError
    from matchcut.interchange.workspace import _source

    try:
        src, _notes = _source(Path(ws), cfg)
    except InterchangeError as exc:
        raise SessionError(f"cannot read the source footage: {exc}") from None
    media: Optional[MediaInfo] = None
    try:
        probed = MediaInfo.from_dict(json.loads((Path(ws) / "probe.json").read_text()))
        if Path(probed.path).is_file() and Path(probed.path).resolve() == Path(src.path).resolve():
            media = probed
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if media is None:
        try:
            media = probe(src.path)
        except (MediaError, OSError, ValueError, KeyError) as exc:
            raise SessionError(f"cannot probe the source footage {src.path}: {str(exc).splitlines()[0] if str(exc) else exc!r}") from None
    return dataclasses.replace(media, fps=float(rate))


def _renders_dir(s: S.Session) -> Path:
    d = s.dir / "renders"
    d.mkdir(parents=True, exist_ok=True)
    _sweep_tmp(d / ".tmp")
    return d


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OverflowError, ValueError):
        return True
    return True


def _sweep_tmp(d: Path, max_age: float = TMP_MAX_AGE_SECONDS, quiet: float = TMP_QUIET_SECONDS) -> None:
    """Remove what a killed render left in ``renders/.tmp`` (best effort): a file no writer holds locked (``held``; every writer of a
    temporary file here keeps an exclusive ``flock`` on it until it is moved into place or removed) that is older than ``max_age`` seconds,
    or that is named for a process that is gone (``<pid>-...``, as ``_tmp`` names them) and not written for ``quiet`` seconds.  A file
    being written is locked, so it is never swept, whatever its age and whichever pid namespace its writer runs in."""
    try:
        entries = list(d.iterdir())
    except OSError:
        return
    now = time.time()
    for f in entries:
        try:
            if f.is_symlink() or not f.is_file():
                continue
            age = now - f.stat().st_mtime
            m = re.match(r"([0-9]{1,9})-", f.name)
            gone = m is not None and int(m.group(1)) != os.getpid() and not _pid_alive(int(m.group(1)))
            if (age > max_age or (gone and age > quiet)) and not held(f):
                f.unlink()
        except OSError:
            pass


def _stat(path: Optional[str]) -> List[Any]:
    """What tells a file's content changed without reading it: size, mtime, inode and ctime (a file replaced by one of the same size and
    mtime still has another inode or ctime)."""
    try:
        st = os.stat(str(path))
        return [st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns]
    except (OSError, TypeError, ValueError):
        return [None, None, None, None]


def _key(doc: Dict[str, Any]) -> str:
    text = json.dumps(dict(doc, version=RENDER_VERSION), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _cached(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _tmp(out_dir: Path, suffix: str) -> Path:
    d = out_dir / ".tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{os.getpid()}-{uuid.uuid4().hex[:12]}{suffix}"


@contextlib.contextmanager
def _writing(out_dir: Path, suffix: str) -> Iterator[Path]:
    """A new temporary file in ``<out_dir>/.tmp``, created here and held under an exclusive ``flock`` for the whole block (whoever writes
    it: this process, or an ffmpeg child that opens the same path), so ``_sweep_tmp`` never removes it.  Removed at the end unless the
    block moved it into place; the lock is released after that."""
    path = _tmp(out_dir, suffix)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield path
    finally:
        try:
            path.unlink(missing_ok=True)
        finally:
            os.close(fd)


def _one_line(exc: BaseException) -> str:
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    return lines[-1] if lines else type(exc).__name__


def _exact(tl: Timeline, rate: Fraction) -> Timeline:
    """A copy of ``tl`` whose ``fps`` is the exact rate, so everything that snaps at ``tl.fps`` (M2b's review windows) snaps on the same
    frames ``frame_rows`` counts."""
    out = tl.copy()
    out.fps = float(rate)
    return out


def _content(tl: Timeline) -> Dict[str, Any]:
    """What of a timeline a render shows: each clip's source range and incoming transition, and the fades (ids, tags and history are not
    rendered, so a proposal and the revision it becomes render to the same file)."""
    return {"clips": [[c.source_in, c.source_out, c.transition_in.kind, c.transition_in.duration] for c in tl.clips],
            "fades": [tl.fade_in, tl.fade_out]}


def _music(cfg: PipelineConfig, ctx: EditContext):
    """(the music settings the renderer mixes, the track's length) or (None, None) without music."""
    if not cfg.music.path:
        return None, None
    path = str(cfg.music.path)
    if not Path(path).is_file():
        raise SessionError(f"the music track {path} is missing")
    if ctx.music is not None:
        duration: Optional[float] = float(ctx.music.duration)
    else:
        from matchcut.review.render import _audio_duration
        duration = _audio_duration(path)
    if duration is not None and cfg.music.start >= duration:
        raise SessionError(f"music.start ({cfg.music.start:.1f}s) is past the end of the track ({duration:.1f}s)")
    return cfg.music, duration


def _music_key(music, duration: Optional[float]) -> Any:
    if music is None:
        return None
    return dict(dataclasses.asdict(music), file=_stat(music.path), duration=duration)


def _seconds(frames: int, rate: Fraction) -> float:
    return round(float(Fraction(frames) / rate), 6)


def _rel(ws: Path, path: Path) -> str:
    try:
        return path.relative_to(ws).as_posix()
    except ValueError:
        return str(path)


# ----------------------------------------------------------------------------------------------- inspect
def _label(kind: str, frames: int) -> Dict[str, Any]:
    return {"kind": kind if frames else "cut", "frames": int(frames)}


def _same_transition(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> bool:
    if a is None or b is None:
        return False
    return _label(str(a.get("kind")), int(a.get("frames") or 0)) == _label(str(b.get("kind")), int(b.get("frames") or 0))


def _pins(tl: Timeline, rows: List[Row], ctx: EditContext, ids: List[str]) -> List[Dict[str, Any]]:
    """Every stored pin and where it stands in ``tl``: held at a junction (with the transition it protects, and whether that differs from
    the junction's transition now), or not held (released by the cut, never placed, or not applied yet) with the reason."""
    held = {id(p): site for p, site in held_pins(tl, ctx)}
    out: List[Dict[str, Any]] = []
    for pin in ctx.pins:
        entry: Dict[str, Any] = {"pin": pin_label(pin), "out_shot": pin.out_shot, "in_shot": pin.in_shot, "out_time": pin.out_time,
                                 "in_time": pin.in_time, "origin": pin.origin, "note": pin.note, "hold_transition": pin.hold_transition,
                                 "held_transition": pin.held_transition}
        site = held.get(id(pin))
        if site is not None:
            now = _label(rows[site + 1].kind, rows[site + 1].tr)
            mismatch = bool(pin.hold_transition and pin.held_transition is not None and not _same_transition(pin.held_transition, now))
            entry.update(held=True, state="held", reason="", junction=ids[site], n=site + 1,
                         clips=[tl.clips[site].id, tl.clips[site + 1].id], transition=now, transition_mismatch=mismatch)
            if mismatch:
                cur = (now["kind"], now["frames"])
                entry["reason"] = rules.mismatch_repair(pin, site + 1, cur, held_of(pin, cur))
        else:
            d = pin_outcome(tl, pin, HUMAN_STAGE)
            if d is None:  # (a pipeline run would apply it, and make this session and a published edit stage stale)
                state, reason = "pending", ("not held by this cut: the pin was stored after the pipeline's last run, so the base stage "
                                            "does not hold it either")
            elif d.action == "pin_unplaced":
                state, reason = "not_placed", d.detail or "the pipeline could not place it"
            else:
                state, reason = "released", d.detail or "this cut no longer holds it"
            entry.update(held=False, state=state, reason=reason, junction=None, n=None, clips=None, transition=None,
                         transition_mismatch=False)
        out.append(entry)
    return out


def _text(t: Optional[Dict[str, Any]]) -> str:
    if not t:
        return "nothing"
    t = _label(str(t.get("kind")), int(t.get("frames") or 0))
    return "cut" if not t["frames"] else f"{t['kind'].replace('_', ' ')} of {t['frames']} frames"


def _story_beats(ws: Path, tl: Timeline, rows: List[Row]) -> List[Dict[str, Any]]:
    """The cut's story beats in record order (a beat that recurs after a move is listed again), with their titles from ``narrative.json``
    when it is readable."""
    titles: Dict[str, Dict[str, Any]] = {}
    try:
        doc = json.loads((Path(ws) / "narrative.json").read_text())
        for b in doc.get("beats", []) if isinstance(doc, dict) else []:
            if isinstance(b, dict) and "id" in b:
                titles[str(b["id"])] = {"title": b.get("title"), "intent": b.get("intent")}
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    runs: List[Dict[str, Any]] = []
    for c, r in zip(tl.clips, rows):
        if runs and runs[-1]["beat"] == c.beat_id:
            runs[-1]["clips"].append(c.id)
            runs[-1]["record_frames"][1] = r.rec_out - r.tail
            continue
        runs.append({"beat": c.beat_id, "clips": [c.id], "record_frames": [r.rec + r.head, r.rec_out - r.tail],
                     **titles.get(c.beat_id, {"title": None, "intent": None})})
    for k, run_ in enumerate(runs):
        run_["frames"] = run_["record_frames"][1] - run_["record_frames"][0]
        run_["n"] = k + 1
    return runs


def _music_beats(tl: Timeline, rows: List[Row], ctx: EditContext, ids: List[str]) -> Optional[Dict[str, Any]]:
    music = ctx.music
    if music is None:
        return None
    total = running_frames(rows)
    beats = [float(b) for b in music.beats]
    downs = {round(float(b), 6) for b in music.downbeats}
    in_cut = [(b, beat_frame(b, ctx)) for b in beats if 0 <= beat_frame(b, ctx) <= total]
    junctions = []
    for i, jid in enumerate(ids):
        rec = rows[i + 1].rec
        beat = music.nearest_beat(float(Fraction(rec) / ctx.rate) + ctx.music_start)
        junctions.append({"n": i + 1, "id": jid, "record_frame": rec,
                          "nearest_beat_frame": None if beat is None else beat_frame(beat, ctx),
                          "offset_frames": rules.beat_offset(tl, rows, i, ctx),
                          "downbeat": beat is not None and round(beat, 6) in downs})
    return {"track": str(music.path), "tempo_bpm": round(float(music.tempo_bpm), 3), "beats_per_bar": int(music.beats_per_bar),
            "start": ctx.music_start, "beats_in_cut": len(in_cut), "beat_frames": [f for _, f in in_cut],
            "downbeat_frames": [f for b, f in in_cut if round(b, 6) in downs], "junctions": junctions}


def inspect(ws: Path, rev: Optional[str] = None) -> Dict[str, Any]:
    """The cut of revision ``rev`` (default: the head), in frames at the source's exact rate: the session (head, staleness), its clips,
    junctions (with their protections), shots and their use, story beats, music beats (beat offsets per junction, with music), every
    editor pin (held or not, the transition it protects and whether the junction's transition still matches it) and the
    ``transition_mismatches`` (``rules.transition_mismatches``, each with how to resolve it)."""
    ws = Path(ws)
    with _reading(ws, rev) as (s, rid, tl):
        why = s.stale()
        ctx = s.context()
        n_revs = len(s.revision_ids())
    d = rules.describe(tl, ctx)
    rows = frame_rows(tl, ctx.rate)
    ids = [j["id"] for j in d["junctions"]]
    pins = _pins(tl, rows, ctx, ids)
    by_site = {p["n"] - 1: p for p in pins if p["held"]}
    for c, entry in zip(tl.clips, d["clips"]):
        entry["origin"] = c.origin or None
    protections: List[Dict[str, Any]] = []
    for i, j in enumerate(d["junctions"]):
        prot: List[str] = []
        p = by_site.get(i)
        j["held_transition"] = None
        j["transition_mismatch"] = False
        if p is not None:
            prot.append(f"editor pin {p['pin']}: its clips stay adjacent with both cut points")
            if p["hold_transition"] and p["transition_mismatch"]:
                prot.append(f"editor pin {p['pin']}: its transition stays as it is ({_text(j['transition'])}) or becomes the "
                            f"{_text(p['held_transition'])} the pin holds")
            elif p["hold_transition"]:
                prot.append(f"editor pin {p['pin']}: its transition stays as it is ({_text(j['transition'])})")
            if p["hold_transition"]:
                j["held_transition"] = p["held_transition"]
                j["transition_mismatch"] = p["transition_mismatch"]
            protections.append({"n": i + 1, "junction": j["id"], "kind": "pin", "pin": p["pin"], "transition": bool(p["hold_transition"]),
                                "held_transition": p["held_transition"], "transition_mismatch": p["transition_mismatch"]})
        if j["match_cut"]:
            prot.append("match cut: its clips stay adjacent with both cut points and a hard cut")
            protections.append({"n": i + 1, "junction": j["id"], "kind": "match_cut", "pin": None, "transition": True,
                                "held_transition": None, "transition_mismatch": False})
        j["protection"] = prot
    used: Dict[int, List[str]] = {}
    for c in tl.clips:
        used.setdefault(c.shot_index, []).append(c.id)
    shots = []
    for k in sorted(ctx.bounds):
        lo, hi = ctx.shot_frames(k)
        shots.append({"shot": k, "frames": [lo, hi], "length": hi - lo, "used_by": used.get(k, [])})
    unknown = sorted(k for k in used if not ctx.has_shot(k))
    total = d["duration_frames"]
    return {
        "session": {"session": s.session_id, "base_stage": s.base_stage, "head": s.head, "revision": rid, "is_head": rid == s.head,
                    "revisions": n_revs, "stale": why is not None, "stale_reason": why,
                    "can_publish": S.publish_blocker(ws, s.base_stage) is None, "publish_blocker": S.publish_blocker(ws, s.base_stage)},
        "rate": d["rate"], "fps": d["fps"], "duration_frames": total, "duration_seconds": _seconds(total, ctx.rate),
        "clips": d["clips"],
        "junctions": d["junctions"],
        "shots": shots,
        "unused_shots": [x["shot"] for x in shots if not x["used_by"]],
        "unknown_shots": unknown,
        "story_beats": _story_beats(ws, tl, rows),
        "music": _music_beats(tl, rows, ctx, ids),
        "pins": pins,
        "protections": protections,
        "transition_mismatches": rules.transition_mismatches(tl, ctx),
    }


# ----------------------------------------------------------------------------------------------- frames
def _grab(media: MediaInfo, n: int, rate: Fraction) -> bytes:
    """Source frame ``n`` as PNG bytes, read exactly as the renderer reads a clip that starts there (``pipeline.render.clip_inputs``: the
    same seek and the same trim, ``TRIM_EPSILON_SECONDS`` before the frame's time at the exact rate), so it is the frame a render shows."""
    fps = float(rate)
    t = float(Fraction(n) / rate)
    margin = pipeline_render.seek_margin(media.path)
    if margin is None:
        seek, start = 0.0, round(t, 6)
    else:
        seek = round(max(0.0, math.floor((t - margin) * fps + 1e-6) / fps), 6)
        start = round(t - seek, 6)
    trim = max(0.0, start - pipeline_render.TRIM_EPSILON_SECONDS)
    # the source's audio is read too, as the renderer reads it: ffmpeg times a seeked MPEG-TS/PS input whose audio is left unread
    # differently (its video trims land on other frames: 1.5 s early on a TS remux), and reading it costs little elsewhere
    graph = f"[0:v]trim=start={trim:.6f},setpts=PTS-STARTPTS[v]" + (";[0:a]anullsink" if media.has_audio else "")
    cmd = [_require("ffmpeg"), "-v", "error", "-nostdin", "-ss", f"{seek:.6f}", "-t", f"{start + 0.5 + 1 / fps:.6f}", "-i", media.path,
           "-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"]
    try:
        out = run(cmd).stdout
    except MediaError as exc:
        raise SessionError(f"cannot read frame {n} of {Path(media.path).name}: {_one_line(exc)}") from None
    if not out:
        raise SessionError(f"{Path(media.path).name} has no frame {n}")
    return out


def _frame_file(ws: Path, media: MediaInfo, n: int, rate: Fraction, out_dir: Path) -> Tuple[Path, bool]:
    from PIL import Image

    key = _key({"kind": "frame", "source": [str(Path(media.path).resolve()), *_stat(media.path)], "rate": str(rate), "frame": n,
                "max_width": MAX_FRAME_WIDTH, "quality": JPEG_QUALITY})
    path = out_dir / f"frame-{key}.jpg"
    if _cached(path):
        return path, True
    img = Image.open(io.BytesIO(_grab(media, n, rate))).convert("RGB")
    if img.width > MAX_FRAME_WIDTH:
        img = img.resize((MAX_FRAME_WIDTH, max(1, round(img.height * MAX_FRAME_WIDTH / img.width))), Image.LANCZOS)
    with _writing(out_dir, ".jpg") as tmp:
        img.save(tmp, "JPEG", quality=JPEG_QUALITY)
        os.replace(tmp, path)
    return path, False


def frames(ws: Path, junction: Any, rev: Optional[str] = None) -> Dict[str, Any]:
    """The outgoing clip's last frame and the incoming clip's first frame at ``junction`` (number or id) of revision ``rev`` (default: the
    head), as JPEG files under ``WS/edits/renders/``, with their source frame numbers (``frame_rows``: the outgoing clip's out-point - 1,
    the incoming clip's in-point) and the record frames where they show.  With a transition, both frames are inside it in a render."""
    ws = Path(ws)
    with _reading(ws, rev) as (s, rid, tl):
        why = s.stale()
        ctx = s.context()
    j = _junction(tl, junction)
    rows = frame_rows(tl, ctx.rate)
    ids = junction_ids(tl)
    media = _media(ws, _config(ws), ctx.rate)
    out_dir = _renders_dir(s)
    a, b = tl.clips[j], tl.clips[j + 1]
    sides = {}
    for side, c, r, n, rec in (("outgoing", a, rows[j], rows[j].out_f - 1, rows[j].rec_out - 1), ("incoming", b, rows[j + 1], rows[j + 1].in_f, rows[j + 1].rec)):
        path, cached = _frame_file(ws, media, n, ctx.rate, out_dir)
        sides[side] = {"clip": c.id, "shot": c.shot_index, "source_frame": n, "record_frame": rec, "path": str(path), "file": _rel(ws, path),
                       "cached": cached}
    return {"revision": rid, "junction": {"n": j + 1, "id": ids[j]}, "transition": _label(rows[j + 1].kind, rows[j + 1].tr),
            "rate": str(ctx.rate), **sides, "stale": why is not None, "stale_reason": why}


# ----------------------------------------------------------------------------------------------- renders
def review_window_frames(tl: Timeline, junction: int, rate: Fraction, pre_seconds: float, post_seconds: float):
    """M2b's review window around ``junction`` (0-based), laid out in whole frames at the exact ``rate``: from ``pre_seconds`` before the
    cut (rounded to frames) to ``post_seconds`` after the incoming transition, its first clip never starting inside a transition (as
    ``review.window.review_window``).  Returns (the ``Window`` to hand to ``review.render.render_window``, its frames).

    The window's offset and length are a whole number of frames, each placed ``TRIM_EPSILON_SECONDS`` early, so the window's 4-decimal
    trims select exactly the frames [start, end) of the cut at a rate such as 30000/1001 (``review_window`` rounds seconds, which can start
    a window a frame late there).  ``tl`` must be the exact-rate copy (``_exact``)."""
    from matchcut.review.window import Window

    rows = frame_rows(tl, rate)
    n = len(rows) - 1
    total = running_frames(rows)
    cut, tr = rows[junction + 1].rec, rows[junction + 1].tr
    s_f = max(0, cut - to_frames(max(0.0, float(pre_seconds)), rate))
    e_f = min(total, cut + tr + to_frames(max(0.0, float(post_seconds)), rate))
    first = junction
    while first > 0 and (rows[first].rec > s_f or s_f < rows[first].rec + rows[first].tr):
        first -= 1
    last = junction + 1
    while last < n and rows[last + 1].rec < e_f:
        last += 1
    eps = pipeline_render.TRIM_EPSILON_SECONDS
    base = float(Fraction(rows[first].rec) / rate)
    off = s_f - rows[first].rec
    offset = 0.0 if off == 0 else float(Fraction(off) / rate) - eps
    length = float(Fraction(e_f - s_f) / rate) - eps
    w = Window(junction, first, last, base + offset, float(Fraction(cut) / rate), base + offset + length, offset)
    return w, {"record_frames": [s_f, e_f], "frames": e_f - s_f, "cut_frame": cut, "cut_offset_frames": cut - s_f,
               "clips": [c.id for c in tl.clips[first:last + 1]]}


def _render_cut(ws: Path, s: S.Session, tl: Timeline, ctx: EditContext, quality: str) -> Dict[str, Any]:
    cfg = _config(ws)
    media = _media(ws, cfg, ctx.rate)
    tl = _exact(tl, ctx.rate)
    rcfg = dataclasses.replace(cfg.render, enabled=True, proxy=quality == "proxy")
    music, music_duration = _music(cfg, ctx)
    key = _key({"kind": "cut", "timeline": _content(tl), "rate": str(ctx.rate), "media": media.to_dict(),
                "source": [str(Path(media.path).resolve()), *_stat(media.path)], "render": dataclasses.asdict(rcfg),
                "music": _music_key(music, music_duration)})
    out_dir = _renders_dir(s)
    out = out_dir / f"cut-{key}.mp4"
    cached = _cached(out)
    if not cached:
        try:
            with _writing(out_dir, ".mp4") as tmp:  # locked from before ffmpeg starts until it is moved into place
                pipeline_render.render(tl, media, rcfg, tmp, music=music, music_duration=music_duration)
                os.replace(tmp, out)
        except (MediaError, ValueError) as exc:
            raise SessionError(f"the render failed: {_one_line(exc)}") from None
    total = running_frames(frame_rows(tl, ctx.rate))
    return {"kind": "cut", "path": str(out), "file": _rel(ws, out), "cached": cached, "quality": quality, "frames": total,
            "seconds": _seconds(total, ctx.rate), "rate": str(ctx.rate), "music": bool(music)}


def _render_window(ws: Path, s: S.Session, tl: Timeline, ctx: EditContext, junction: Any, quality: str) -> Dict[str, Any]:
    from matchcut.review.render import render_window
    from matchcut.review.window import ReviewError, music_start, sub_timeline

    cfg = _config(ws)
    media = _media(ws, cfg, ctx.rate)
    j = _junction(tl, junction)
    ids = junction_ids(tl)
    tl = _exact(tl, ctx.rate)
    w, info = review_window_frames(tl, j, ctx.rate, cfg.review.pre_seconds, cfg.review.post_seconds)
    if info["frames"] <= 0:
        raise SessionError("the review window is empty: raise review.pre_seconds or review.post_seconds")
    if quality == "proxy":
        width = cfg.render.proxy_width if not cfg.review.width else min(cfg.review.width, cfg.render.proxy_width)
        cfg = dataclasses.replace(cfg, review=dataclasses.replace(cfg.review, width=width))
    music, music_duration = _music(cfg, ctx)
    sub = sub_timeline(tl, w)  # what render_window renders: the window's clips (the first cut in), with the fades the window reaches
    music_doc = _music_key(music, music_duration)
    if music_doc is not None:  # where the window's music starts in the track, and whether it fades (only at the end of the cut)
        music_doc.update(start_in_track=music_start(tl, w, music.start), ends_cut=w.last == len(tl.clips) - 1)
        if not music_doc["ends_cut"]:
            music_doc.pop("fade_out")  # render_window fades the music only at the end of the cut
    key = _key({"kind": "window", "timeline": _content(sub), "offset": round(w.offset, 6), "duration": w.duration,
                "frames": info["frames"], "rate": str(ctx.rate), "media": media.to_dict(),
                "source": [str(Path(media.path).resolve()), *_stat(media.path)],
                "review": {"width": cfg.review.width, "crf": cfg.review.crf}, "audio_bitrate": cfg.render.audio_bitrate,
                "music": music_doc})
    out_dir = _renders_dir(s)
    out = out_dir / f"window-{key}.mp4"
    cached = _cached(out)
    if not cached:
        try:
            render_window(tl, w, media, cfg, out, label="", bounds=None)  # through <dir>/.tmp, then os.replace; nothing left on failure
        except ReviewError as exc:
            raise SessionError(f"the render failed: {exc}") from None
    return {"kind": "window", "path": str(out), "file": _rel(ws, out), "cached": cached, "quality": quality,
            "junction": {"n": j + 1, "id": ids[j]}, "record_frames": info["record_frames"], "frames": info["frames"],
            "cut_offset_frames": info["cut_offset_frames"], "clips": info["clips"], "seconds": _seconds(info["frames"], ctx.rate),
            "rate": str(ctx.rate), "music": bool(music)}


def _quality(quality: Any) -> str:
    if quality not in QUALITIES:
        raise SessionError(f"quality must be one of {', '.join(QUALITIES)}, not {quality!r}")
    return quality


def render(ws: Path, rev: Optional[str] = None, junction: Any = None, quality: str = "proxy") -> Dict[str, Any]:
    """Render revision ``rev`` (default: the head): the whole cut through the pipeline's renderer, or, with ``junction``, that junction's
    M2b review window.  ``quality``: ``proxy`` (the render config's proxy width and quality; the default) or ``full``.  The file is
    ``WS/edits/renders/cut-<key>.mp4`` or ``window-<key>.mp4``, reused when it exists (see the module's docstring).  A stale session still
    renders (nothing is changed) and says so."""
    ws = Path(ws)
    quality = _quality(quality)
    with _reading(ws, rev) as (s, rid, tl):
        why = s.stale()
        ctx = s.context()
        sha = s.revision(rid)["timeline_sha256"]
    out = _render_window(ws, s, tl, ctx, junction, quality) if junction is not None else _render_cut(ws, s, tl, ctx, quality)
    return dict(out, revision=rid, timeline_sha256=sha, stale=why is not None, stale_reason=why)


def preview(ws: Path, proposal: str, junction: Any = None, quality: str = "proxy") -> Dict[str, Any]:
    """Render the accepted proposal ``proposal`` before it is applied (the whole cut, or with ``junction`` its review window, as
    ``render``).  The proposal is re-validated exactly as ``apply`` re-validates it (``session.check_proposal``): a stale session, a moved
    head, a changed context or a proposal file that no longer follows from its operations is refused.  No revision is written."""
    ws = Path(ws)
    quality = _quality(quality)
    pid = S.check_proposal_id(proposal)
    with S._locked(ws, shared=True) as d:  # as session.check_proposal, keeping the session and the context it checked against
        s = S._fresh(ws, None)
        doc = S._revalidated(s, d, pid)
        ctx = s.context()
    tl = Timeline.from_dict(doc["timeline"])
    out = _render_window(ws, s, tl, ctx, junction, quality) if junction is not None else _render_cut(ws, s, tl, ctx, quality)
    return dict(out, proposal=doc["id"], head=doc["head"], timeline_sha256=doc["timeline_sha256"], changes=doc["changes"],
                timing=doc["timing"])


# ----------------------------------------------------------------------------------------------- pin_transition
_PIN = re.compile(r"s([0-9]{1,7})->s([0-9]{1,7})")


def _frames_arg(frames: Any) -> int:
    if isinstance(frames, bool) or not isinstance(frames, (int, float)) or (isinstance(frames, float) and not (math.isfinite(frames) and frames.is_integer())):
        raise SessionError(f"frames must be a whole number of frames, not {str(frames)[:24]!s}")
    if not 0 <= frames <= MAX_FRAMES:
        shown = str(frames) if abs(frames) < 10 ** 15 else "out of range"
        raise SessionError(f"frames must be from 0 to {MAX_FRAMES}, not {shown}")
    return int(frames)


def pin_transition(ws: Path, pin: Optional[str] = None, junction: Any = None, hold: bool = True, kind: Optional[str] = None,
                   frames: Optional[int] = None) -> Dict[str, Any]:
    """Protect (``hold``) or release the transition at an editor pin's junction: sets or clears the pin's ``hold_transition`` and
    ``held_transition`` ({"kind", "frames"}) in ``editorial.json``.  The pins are read through ``PinStore``; the file is written
    surgically (``_write_pin``), so nothing else in it changes: no other field of the pin, no other pin (a malformed one included) and no
    other top-level key.

    The pin is named by its label (``pin``: ``s1->s3``) or by the junction of the head revision it holds (``junction``).  Without ``kind``
    and ``frames``, the transition held is the one at the pin's junction in the head revision (the pin must be held there).  ``kind`` and
    ``frames`` (``kind`` cut, dissolve or dip_to_black; ``frames`` whole, 0 only for a cut) are for a pin the head does not hold (e.g. one
    the pipeline has not applied yet); at a pin the head holds they must be the head's transition, since the rules keep a protected
    transition as it is and another one could never be reached there.

    The pins are part of the edit context, so a change makes every proposal made before it stale: ``apply`` and ``preview`` refuse them
    until they are proposed again (the result lists them)."""
    ws = Path(ws)
    if not ws.is_dir():
        raise SessionError(f"no workspace {ws}")
    if (pin is None) == (junction is None):
        raise SessionError("name the pin by pin (its label, e.g. s1->s3) or by junction (of the head revision), one of them")
    if not isinstance(hold, bool):
        raise SessionError(f"hold must be true or false, not {hold!r}")
    if not hold and (kind is not None or frames is not None):
        raise SessionError("releasing a transition takes no kind or frames")
    if kind is not None and kind not in TRANSITION_KINDS:
        raise SessionError(f"unknown transition {kind!r} (one of: {', '.join(TRANSITION_KINDS)})")
    n_frames = None if frames is None else _frames_arg(frames)
    if n_frames is not None and kind is None:
        raise SessionError("frames needs kind (cut, dissolve or dip_to_black)")
    if kind is not None:
        if kind == "cut":
            n_frames = n_frames or 0
            if n_frames:
                raise SessionError(f"a cut has no frames (frames {n_frames})")
        elif n_frames is None or n_frames < 1:
            raise SessionError(f"a {kind} needs at least 1 frame" + ("" if n_frames is None else f", not {n_frames}"))
    has_session = (S.edits_dir(ws) / "session.json").exists()
    lock = S._locked(ws) if has_session else contextlib.nullcontext()
    with lock:
        store = PinStore(ws)
        pins = store.load()
        head: Optional[Timeline] = None
        ctx: Optional[EditContext] = None
        s: Optional[S.Session] = None
        if has_session:
            s = S.load(ws)
            head, ctx = s.timeline(), s.context()
        target: Optional[Pin] = None
        if junction is not None:
            if head is None or ctx is None:
                raise SessionError(f"no edit session in {ws}: name the pin by its label (pin), or run start first")
            j = _junction(head, junction)
            target = next((p for p, site in held_pins(head, ctx) if site == j), None)
            if target is None:
                raise SessionError(f"no editor pin holds junction {junction_ids(head)[j]} in the head revision {s.head}"
                                   + (f" (pins: {', '.join(pin_label(p) for p in pins)})" if pins else " (there are no pins)"))
        else:
            m = _PIN.fullmatch(pin) if isinstance(pin, str) else None
            if m is None:
                raise SessionError(f"a pin is named by its label (s<out shot>->s<in shot>, e.g. s1->s3), not {str(pin)[:40]!r}")
            key = (int(m.group(1)), int(m.group(2)))
            target = next((p for p in pins if p.key == key), None)
            if target is None:
                raise SessionError(f"no pin {pin} in editorial.json" + (f" (pins: {', '.join(pin_label(p) for p in pins)})" if pins else
                                                                       " (there are no pins)"))
        site: Optional[int] = None
        now: Optional[Dict[str, Any]] = None
        if head is not None and ctx is not None:
            site = next((st for p, st in held_pins(head, ctx) if p.key == target.key), None)
            if site is not None:
                rows = frame_rows(head, ctx.rate)
                now = _label(rows[site + 1].kind, rows[site + 1].tr)
        if hold:
            if kind is None:
                if now is None:
                    where = "there is no edit session" if head is None else f"it is not held in the head revision {s.head}"
                    raise SessionError(f"pin {pin_label(target)}: {where}, so there is no transition to take: give kind and frames")
                held = dict(now)
            else:
                held = _label(kind, int(n_frames or 0))
                if now is not None and held != now:
                    raise SessionError(
                        f"pin {pin_label(target)} is held at junction {junction_ids(head)[site]} of the head revision {s.head}, where the "
                        f"transition is a {_text(now)}: kind and frames are for a pin the head does not hold.  Omit kind and frames to "
                        f"protect the {_text(now)}, or change the transition first (propose and apply set_transition), then protect it")
            new = dataclasses.replace(target, hold_transition=True, held_transition=held)
        else:
            new = dataclasses.replace(target, hold_transition=False, held_transition=None)
        changed = new.to_dict() != target.to_dict()
        live: List[str] = _live_proposals(s, ctx) if changed and s is not None and ctx is not None else []
        if changed:
            _write_pin(ws, target, new)
        stale_proposals = live
    mismatch = bool(new.hold_transition and now is not None and not _same_transition(new.held_transition, now))
    if not changed:
        note = "nothing changed: editorial.json was not written"
    else:
        note = ("editorial.json changed: the pins are part of the edit context, so every proposal made before this must be proposed again "
                "(apply and preview refuse them)")
    if mismatch:  # (a release keeps no transition; a protection set here is the head's: this is only for safety)
        note += (f"; the head revision has a {_text(now)} at this junction, not the {_text(new.held_transition)} held")
    ids = junction_ids(head) if head is not None else []
    return {"pin": pin_label(new), "hold_transition": new.hold_transition, "held_transition": new.held_transition,
            "previous": {"hold_transition": target.hold_transition, "held_transition": target.held_transition}, "changed": changed,
            "junction": None if site is None else {"n": site + 1, "id": ids[site]}, "transition": now, "transition_mismatch": mismatch,
            "context_changed": changed, "proposals_to_propose_again": stale_proposals, "note": note}


def _live_proposals(s: S.Session, ctx: EditContext) -> List[str]:
    """The stored proposals ``apply`` would still take before the pins change (made at the head, with the session's reserved clip ids and
    the current context): the ones the change makes stale.  Proposals that were stale already are left out."""
    out = []
    d = s.dir / "proposals"
    sha, reserved = S.context_sha256(ctx), s.reserved_ids()
    for p in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(doc, dict) and S._PROPOSAL_ID.fullmatch(p.stem) and doc.get("head") == s.head and doc.get("context_sha256") == sha
                and doc.get("reserved_ids") == reserved):
            out.append(p.stem)
    return out


_PIN_ERRORS = (KeyError, ValueError, TypeError, AttributeError, OverflowError)


def _write_pin(ws: Path, target: Pin, new: Pin) -> None:
    """Write ``new``'s protection into ``editorial.json`` surgically: only the ``hold_transition`` and ``held_transition`` of the entry
    ``PinStore`` reads as ``target`` (the first well-formed entry with its two shots) change.  Every other entry (a malformed one
    included), every other field of that entry and every other top-level key are kept as they are; ``PinStore.put`` would drop what it
    does not understand."""
    path = Path(ws) / "editorial.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SessionError(f"cannot read {path.name} ({exc})") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("pins"), list):
        raise SessionError(f"{path.name} holds no list of pins: nothing was written")
    for i, entry in enumerate(raw["pins"]):
        try:
            found = isinstance(entry, dict) and Pin.from_dict(entry).key == target.key
        except _PIN_ERRORS:
            continue
        if found:
            break
    else:
        raise SessionError(f"pin {pin_label(target)} is no longer in {path.name}: nothing was written")
    entry = dict(raw["pins"][i])
    if new.hold_transition:
        entry["hold_transition"] = True
        entry["held_transition"] = dict(new.held_transition or {})
    else:
        entry.pop("hold_transition", None)
        entry.pop("held_transition", None)
    raw["pins"][i] = entry
    S._atomic_write(path, json.dumps(raw, indent=2))
