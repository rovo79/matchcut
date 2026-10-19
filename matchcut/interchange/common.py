"""The common time model of every interchange format.

* Exact frame rates (``Fraction``), never rounded floats: ``source_info`` reads ffprobe's ``r_frame_rate``.
* Integer frame ranges: ``to_frames`` (half away from zero) and ``rt`` (FCPXML rational seconds).
* ``export_timeline``: the frame-snapped copy that is exported, with a note for every clip the snap moved.
* Stable ids: ``junction_id`` / ``junction_ids`` (``s<out>-s<in>``) and ``clip_key`` (the M1 lineage key).
* ``layout``: matchcut's dissolve OVERLAPS the last d of the outgoing clip with the first d of the incoming one
  (record duration = sum - d); OTIO, FCPXML and EDL centre a transition on the cut point.  Converted exactly with
  d in whole frames (error diffusion keeps the total within half a frame of ``Timeline.duration``):
      in_off = d_f // 2, out_off = d_f - in_off
      the outgoing clip's visible range ends ``out_off`` frames early, the incoming clip's starts ``in_off`` late,
      the transition starts ``in_off`` before the cut and ends ``out_off`` after it, cut = record start + in_off.
  Everything a transition shows lies inside both clips' own [source_in, source_out], so no media outside matchcut's
  ranges is needed.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from matchcut.pipeline.junctions import Junction
from matchcut.pipeline.media import MediaError, _require, run
from matchcut.pipeline.timeline import Clip, Timeline, Transition, clip_origin

from . import InterchangeError

# the rates a probe that only reports a float (or an approximate rational) is snapped to
COMMON_RATES: Tuple[Fraction, ...] = (
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30), Fraction(48000, 1001), Fraction(48),
    Fraction(50), Fraction(60000, 1001), Fraction(60), Fraction(120000, 1001), Fraction(120),
)
_SNAP_TOLERANCE = 0.002  # relative, for a float rate: 23.976 and 29.97 are 0.0001 off their rationals
_EXACT_TOLERANCE = 0.0005  # relative, for a probed rational such as 2997/100: closer than this to a common rate is that rate


def _nearest_common(rate: float) -> Tuple[Fraction, float]:
    best = min(COMMON_RATES, key=lambda r: abs(float(r) - rate))
    return best, abs(float(best) - rate) / rate


def snap_rate(rate: float) -> Fraction:
    """The common rate nearest to a float rate (24000/1001 for 23.976); an unusual rate becomes a small rational."""
    if rate <= 0:
        raise InterchangeError(f"invalid frame rate {rate}")
    best, err = _nearest_common(rate)
    return best if err <= _SNAP_TOLERANCE else Fraction(rate).limit_denominator(10000)


def exact_rate(rate: Fraction) -> Fraction:
    """A probed rational, with one a hair off a common rate (2997/100, 23976/1000) replaced by that rate."""
    if rate in COMMON_RATES:
        return rate
    best, err = _nearest_common(float(rate))
    return best if err <= _EXACT_TOLERANCE else rate


def to_frames(t: float, rate: Fraction) -> int:
    """Seconds -> whole frames at ``rate``, ties rounded away from zero.

    A millionth of a frame of slack makes exact halves robust against the binary float ``t`` (1/48 s at 24 fps is 1 frame).
    """
    x = Fraction(float(t)) * Fraction(rate)
    n = math.floor(abs(x) + Fraction(1, 2) + Fraction(1, 1000000))
    return -n if x < 0 else n


def rt(frames: int, rate: Fraction) -> str:
    """Frames -> FCPXML rational seconds on the frame grid, reduced: ``"1001/24000s"``, ``"2s"``, ``"0s"``."""
    sec = Fraction(frames) / Fraction(rate)
    if sec == 0:
        return "0s"
    return f"{sec.numerator}s" if sec.denominator == 1 else f"{sec.numerator}/{sec.denominator}s"


# -- the source ----------------------------------------------------------------------------------
@dataclass(frozen=True)
class SourceInfo:
    """A media file as the interchange files describe it.  ``rate`` is the exact frame rate of the video stream
    (the sample rate for an audio-only file such as a music track, where ``duration_frames`` counts samples)."""

    path: str  # absolute
    url: str
    rate: Fraction
    duration_frames: int
    width: int
    height: int
    has_audio: bool
    size: int
    mtime_ns: int

    @property
    def seconds(self) -> float:
        return float(Fraction(self.duration_frames) / self.rate)


def _fraction(text: Any) -> Optional[Fraction]:
    try:
        f = Fraction(str(text))
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def source_info(media_path: str, audio_only: bool = False) -> SourceInfo:
    """Probe ``media_path``: absolute path and URL, exact rate, duration in frames, size, audio, content key.

    Cover art (a video stream with ``disposition.attached_pic``) is not footage and is ignored.  ``audio_only`` reads
    the audio stream even when the file also has real video (the music path); the rate is then the sample rate.
    """
    p = Path(media_path)
    if not p.is_file():
        raise InterchangeError(f"source footage not found: {media_path}")
    p = p.resolve()
    try:
        proc = run([_require("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)])
        data = json.loads(proc.stdout.decode("utf-8"))
    except MediaError as exc:
        raise InterchangeError(f"cannot probe {media_path}: {str(exc).splitlines()[0]}") from exc
    streams = data.get("streams", [])
    video = None if audio_only else next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        raise InterchangeError(f"no {'audio' if audio_only else 'video or audio'} stream in {media_path}")
    st = p.stat()
    fmt_seconds = float(data.get("format", {}).get("duration") or 0.0)
    seconds = float((video or audio).get("duration") or fmt_seconds or 0.0)
    if video is not None:
        rate = _fraction(video.get("r_frame_rate"))
        if rate is None:  # a float only (or 0/0): the nearest common rate
            fps = _fraction(video.get("avg_frame_rate"))
            rate = snap_rate(float(fps)) if fps is not None else Fraction(25)
        rate = exact_rate(rate)
        nb = str(video.get("nb_frames") or "")
        frames = int(nb) if nb.isdigit() and int(nb) > 0 else to_frames(seconds, rate)
        width, height = int(video.get("width", 0)), int(video.get("height", 0))
    else:
        rate = Fraction(int(audio.get("sample_rate") or 44100))
        frames, width, height = to_frames(seconds, rate), 0, 0
    return SourceInfo(path=str(p), url=p.as_uri(), rate=rate, duration_frames=frames, width=width, height=height,
                      has_audio=audio is not None, size=st.st_size, mtime_ns=st.st_mtime_ns)


# -- what is exported ----------------------------------------------------------------------------
def clip_key(c: Clip) -> str:
    """The clip-instance key carried by every interchange file: the M1 lineage key."""
    return clip_origin(c)


def _plural(n: float) -> str:
    return "s" if abs(n) > 1 + 1e-9 else ""


def _moved(v: float) -> str:
    return f"{round(v):+d}" if abs(v - round(v)) < 0.005 else f"{v:+.2f}"


def export_timeline(tl: Timeline, bounds: Optional[Dict[int, Sequence[float]]], rate: Union[Fraction, SourceInfo]) -> Tuple[Timeline, List[str]]:
    """A frame-snapped copy of ``tl`` (``Timeline.snap_to_frames(bounds)``) and a note per clip the snap moved.

    Snapping is on the source's exact ``rate`` (a ``Fraction`` or the ``SourceInfo``), never on the float ``tl.fps``.
    The copy keeps each clip's identity: ``origin`` is stamped with the unsnapped clip's key.
    """
    base = tl.copy()
    base.fps = float(rate.rate if isinstance(rate, SourceInfo) else rate)
    fps = base.fps
    for c in base.clips:
        c.origin = clip_origin(c)
    snapped = base.snap_to_frames(bounds)
    notes: List[str] = []
    for a, b in zip(base.clips, snapped.clips):
        parts = []
        for label, x, y in (("in", a.source_in, b.source_in), ("out", a.source_out, b.source_out)):
            d = (y - x) * fps
            if abs(d) > 0.01:
                parts.append(f"{label} moved {_moved(d)} frame{_plural(d)}")
        if parts:
            notes.append(f"{a.id}: " + ", ".join(parts))
    return snapped, notes


def junction_id(a: Clip, b: Clip) -> str:
    """The id of the cut between clips ``a`` and ``b``: ``s<out shot>-s<in shot>`` (unique ids: ``junction_ids``)."""
    return f"s{a.shot_index}-s{b.shot_index}"


def junction_ids(tl: Timeline) -> List[str]:
    """One id per cut of ``tl`` (index i = between clip i and i+1); a repeated shot pair gets ``#2``, ``#3``."""
    seen: Dict[str, int] = {}
    out: List[str] = []
    for a, b in zip(tl.clips, tl.clips[1:]):
        base = junction_id(a, b)
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return out


def plain(x: Any) -> Any:
    """JSON-safe copy (numpy scalars and other odd values flattened)."""
    return json.loads(json.dumps(x, default=lambda o: o.item() if hasattr(o, "item") else str(o)))


def truncate(s: str, n: int = 240) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def junction_meta(j: Junction, jid: str, scores: bool = False) -> Dict[str, Any]:
    """The decision record of one junction as interchange metadata."""
    cur = j.current
    meta: Dict[str, Any] = {
        "junction_id": jid, "why": j.why(), "history": list(j.history()), "pinned": j.pinned, "pin_origin": j.pin_origin,
        "transition": {"kind": cur.transition, "duration": round(cur.transition_seconds, 4)},
    }
    if scores:
        meta["scores"] = plain(cur.score.to_dict()) if cur.score is not None else None
    return meta


def music_window(music: Any, music_src: Optional["SourceInfo"], rate: Fraction, total: int) -> Tuple[int, int, int]:
    """(start, duration, available) in frames at ``rate`` of the music clip under a cut of ``total`` frames.

    The renderer plays ``min(total, music_duration - start)`` of the track from ``music.start``; every exporter follows
    the same rule, and a start at or past the end of the track is an error.  ``available`` is floored, so the clip never
    runs past the real end of the track.
    """
    if music_src is None:
        raise InterchangeError(f"music file not probed: {music.path}")
    if max(0.0, music.start) >= music_src.seconds:
        raise InterchangeError(f"music.start ({music.start:.1f}s) is past the end of the track ({music_src.seconds:.1f}s)")
    avail = math.floor(Fraction(music_src.duration_frames) / music_src.rate * Fraction(rate))
    start = max(to_frames(music.start, rate), 0)
    dur = min(total, avail - start)
    if dur < 1:
        raise InterchangeError(f"music.start ({music.start:.2f}s) leaves less than one frame of music before the end of the track "
                               f"({music_src.seconds:.2f}s)")
    return start, dur, avail


# -- the dissolve conversion ---------------------------------------------------------------------
@dataclass(frozen=True)
class LaidClip:
    """One clip on the frame grid.  Frames are integers at the exported rate."""

    index: int
    in_f: int  # matchcut source range
    out_f: int
    rec: int  # matchcut record start (Timeline.record_start)
    tr: int  # incoming transition, frames (0 = cut)
    kind: str  # incoming transition kind ("cut" when tr == 0)
    head: int  # in_off of the incoming transition: frames before the cut, trimmed from this clip's visible start
    tail: int  # out_off of the NEXT clip's transition: trimmed from this clip's visible end
    vis_in: int  # what an editor shows: source range after the trims
    vis_out: int
    cut: int  # record frame of the cut (the editor's clip start) = rec + head

    @property
    def dur(self) -> int:
        return self.vis_out - self.vis_in

    @property
    def rec_in(self) -> int:
        return self.cut

    @property
    def rec_out(self) -> int:
        return self.cut + self.dur

    @property
    def transition(self) -> Tuple[str, int, int]:
        """(kind, in_off, out_off) of the incoming transition, centred on ``cut``; ("cut", 0, 0) without one."""
        return (self.kind, self.head, self.tr - self.head) if self.tr else ("cut", 0, 0)


def layout(tl: Timeline, rate: Fraction) -> List[LaidClip]:
    """The frame-grid geometry shared by every exporter and by the validation."""
    base: List[Dict[str, Any]] = []
    cum, done, rec = 0.0, 0, 0
    for i, c in enumerate(tl.clips):
        in_f = to_frames(c.source_in, rate)
        out_f = max(to_frames(c.source_out, rate), in_f + 1)
        tr = 0
        if i and not c.transition_in.is_cut:
            cum += c.transition_in.duration
            tr = max(to_frames(cum, rate) - done, 0)  # error diffusion keeps the total within half a frame
            done += tr
        if i:
            rec = base[-1]["rec"] + (base[-1]["out_f"] - base[-1]["in_f"]) - tr
        base.append({"in_f": in_f, "out_f": out_f, "rec": rec, "tr": tr, "head": tr // 2,
                     "kind": c.transition_in.kind if tr else "cut"})
    rows: List[LaidClip] = []
    for i, r in enumerate(base):
        nxt = base[i + 1] if i + 1 < len(base) else None
        tail = nxt["tr"] - nxt["head"] if nxt else 0
        vis_in, vis_out = r["in_f"] + r["head"], r["out_f"] - tail
        if vis_out - vis_in < 1:
            raise InterchangeError(f"clip {tl.clips[i].id} is shorter than its transitions")
        rows.append(LaidClip(i, r["in_f"], r["out_f"], r["rec"], r["tr"], r["kind"], r["head"], tail, vis_in, vis_out, r["rec"] + r["head"]))
    return rows


def record_frames(rows: Sequence[LaidClip]) -> int:
    """Length of the exported cut in frames."""
    return rows[-1].rec_out if rows else 0


def assemble(items: List[Tuple[str, Dict[str, Any]]], rate: Fraction, meta: Dict[str, Any], name: str, source: str,
             bounds: Optional[Dict[int, Sequence[float]]]) -> Timeline:
    """Back end of the importers: [("clip", {vis_in, dur, name, meta}), ("tr", {in, out, kind})] -> Timeline.

    Converts centred transitions back to matchcut's overlap semantics (source_in = vis_in - in_off,
    source_out = vis_end + out_off).  ``meta`` of a clip may hold clip_id, clip_key, shot_index, beat_id.
    """
    fps = float(rate)
    clips: List[Clip] = []
    seen: set = set()
    pend: Optional[Dict[str, Any]] = None
    fresh = 0
    for k, (kind, d) in enumerate(items):
        if kind == "tr":
            pend = d
            continue
        nxt = items[k + 1][1] if k + 1 < len(items) and items[k + 1][0] == "tr" else None
        tin = pend if (pend and clips) else None
        pend = None
        in_f = d["vis_in"] - (tin["in"] if tin else 0)
        out_f = d["vis_in"] + d["dur"] + (nxt["out"] if nxt else 0)
        cm = d["meta"]
        cid = str(cm.get("clip_id") or "")
        while not cid or cid in seen:
            fresh += 1
            cid = f"h{fresh:03d}"
        seen.add(cid)
        shot = cm.get("shot_index")
        if shot is None:
            m = re.search(r"shot(\d+)", d["name"])
            shot = int(m.group(1)) if m else None
        if shot is None:  # last resort: the shot whose bounds overlap the source range most
            t0, t1 = in_f / fps, out_f / fps
            best = max(((max(0.0, min(b[1], t1) - max(b[0], t0)), s) for s, b in (bounds or {}).items()), default=(0.0, -1))
            shot = best[1] if best[0] > 0 else -1
        clips.append(Clip(cid, int(shot), round(in_f / fps, 4), round(out_f / fps, 4), str(cm.get("beat_id", "")),
                          transition_in=Transition(tin["kind"], round((tin["in"] + tin["out"]) / fps, 4)) if tin else Transition(),
                          origin=str(cm.get("clip_key") or "")))
    return Timeline(source=source, fps=fps, clips=clips, fade_in=float(meta.get("fade_in", 0.0)), fade_out=float(meta.get("fade_out", 0.0)),
                    name=name, stage=str(meta.get("stage", "")))
