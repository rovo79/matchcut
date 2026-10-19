"""The review window around one junction: its clips, sub-timeline, render rule and mpv EDL.

A window is cut from a stage timeline: the clips it touches are copied into a sub-timeline
(its first clip cut in, its own fades kept only at the ends of the whole cut). Hard-cut
windows play exactly from an mpv EDL; windows with transitions, fades or music are rendered.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..pipeline.config import PipelineConfig
from ..pipeline.timeline import Timeline, Transition

_EPS = 1e-6
_NO_MUSIC = PipelineConfig()


class ReviewError(RuntimeError):
    """A window could not be made or played; the message is one line, ready to show the editor."""


@dataclass(frozen=True)
class Window:
    junction: int  # the cut between clip ``junction`` and clip ``junction + 1`` of the stage timeline
    first: int  # first clip rendered
    last: int  # last clip rendered
    start: float  # record time (stage timeline seconds) where the window starts
    cut: float  # record time where the incoming clip starts (its transition, if any, starts here)
    end: float  # record time where the window ends
    offset: float  # start - record_start(first): where the window starts inside the rendered clips

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 4)


def _incoming(tl: Timeline, k: int) -> float:
    t = tl.clips[k].transition_in
    return 0.0 if k == 0 or t.is_cut else t.duration


def review_window(tl: Timeline, junction: int, pre: float, post: float) -> Window:
    n = len(tl.clips) - 1
    if not 0 <= junction < n:
        raise ReviewError(f"junction {junction + 1} is out of range 1..{n}")
    cut = tl.record_start(junction + 1)
    start = max(0.0, cut - max(0.0, pre))
    end = min(tl.duration, cut + _incoming(tl, junction + 1) + max(0.0, post))
    first = junction
    while first > 0 and (tl.record_start(first) > start + _EPS or start < tl.record_start(first) + _incoming(tl, first) - _EPS):
        first -= 1
    last = junction + 1
    while last < n and tl.record_start(last + 1) < end - _EPS:
        last += 1
    return Window(junction, first, last, round(start, 4), round(cut, 4), round(end, 4), round(start - tl.record_start(first), 4))


def sub_timeline(tl: Timeline, w: Window) -> Timeline:
    sub = tl.copy()
    sub.clips = sub.clips[w.first : w.last + 1]
    sub.clips[0].transition_in = Transition()
    sub.fade_in = tl.fade_in if w.first == 0 else 0.0
    sub.fade_out = tl.fade_out if w.last == len(tl.clips) - 1 else 0.0
    sub.history = []
    sub.name = f"review {tl.stage} junction {w.junction + 1}"
    return sub


def music_start(tl: Timeline, w: Window, music_start_seconds: float) -> float:
    """Where the music must start for the excerpt so it lines up with the full render."""
    return round(music_start_seconds + tl.record_start(w.first), 4)


def _fade_overlap(tl: Timeline, w: Window) -> str:
    if w.first == 0 and tl.fade_in > 0 and w.offset < tl.fade_in - _EPS:
        return "fade-in"
    if w.last == len(tl.clips) - 1 and tl.fade_out > 0 and w.end > tl.duration - tl.fade_out + _EPS:
        return "fade-out"
    return ""


def _render_reason(tl: Timeline, w: Window, cfg: PipelineConfig) -> str:
    for c in tl.clips[w.first + 1 : w.last + 1]:
        if not c.transition_in.is_cut:
            return c.transition_in.kind.replace("_", " ")
    fade = _fade_overlap(tl, w)
    if fade:
        return fade
    if cfg.music.path:
        return "music"
    return ""


def needs_render(tl: Timeline, w: Window, cfg: PipelineConfig) -> bool:
    return bool(_render_reason(tl, w, cfg)) or cfg.review.always_render


def _edl_path(path: str) -> str:
    return f"%{len(path.encode('utf-8'))}%{path}" if any(ch in path for ch in ",;\n!") else path


def window_edl(tl: Timeline, w: Window, source_path: str) -> str:
    """mpv EDL of a hard-cut window: one segment per clip, trimmed to the window (record time == window time)."""
    reason = _render_reason(tl, w, _NO_MUSIC)  # music and always_render never block an EDL; needs_render decides
    if reason:
        raise ReviewError(f"window needs a render: {reason}")
    path = _edl_path(source_path)  # callers pass an absolute path
    lines = ["# mpv EDL v0"]
    t = w.start
    for k in range(w.first, w.last + 1):
        c = tl.clips[k]
        clip_start = tl.record_start(k)
        a = c.source_in + max(0.0, t - clip_start)
        length = min(c.source_out, c.source_in + (w.end - clip_start)) - a
        if length <= _EPS:
            continue
        lines.append(f"{path},{a:.4f},{length:.4f},title={tl.stage or 'cut'} s{c.shot_index} {c.id}")
        t = clip_start + (a - c.source_in) + length
    return "\n".join(lines) + "\n"
