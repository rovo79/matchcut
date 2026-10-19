"""A one-line timeline track: clips as coloured spans, transitions, locks, beat grid."""
from __future__ import annotations

from typing import Dict, List, Optional

from rich.text import Text
from textual.events import Click
from textual.message import Message
from textual.widgets import Static

from matchcut.pipeline.timeline import Timeline

PALETTE = ["on dark_blue", "on dark_green", "on dark_magenta", "on dark_cyan", "on dark_orange3", "on grey37"]
GLYPHS = {"cut": "|", "dissolve": "~", "dip_to_black": "#"}


def _layout(tl: Timeline, width: int, center: int):
    """The clips shown and their widths. Windowed around ``center`` when even one cell per clip plus glyphs would not fit."""
    n = len(tl.clips)
    visible = min(n, max(1, (width + 1) // 2))
    lo = min(max(0, center - visible // 2), n - visible)
    window = range(lo, lo + visible)
    clip_total = max(sum(tl.clips[i].duration for i in window), 1e-6)
    usable = max(width - (visible - 1), visible)
    spans = [max(1, int(round(tl.clips[i].duration / clip_total * usable))) for i in window]
    while sum(spans) > usable and max(spans) > 1:  # rounding and the 1-cell minimum can overshoot
        spans[spans.index(max(spans))] -= 1
    return window, spans


def junction_columns(tl: Timeline, width: int, center: int) -> Dict[int, int]:
    """Column of each visible junction glyph; junction j sits between clip j and clip j+1."""
    if len(tl.clips) < 2:
        return {}
    window, spans = _layout(tl, max(width, 1), center)
    cols: Dict[int, int] = {}
    x = 0
    for k, i in enumerate(window):
        if k > 0:
            cols[i - 1] = x
            x += 1
        x += spans[k]
    return cols


def render_track(
    tl: Timeline,
    width: int,
    selected: int,
    beats: Optional[List[float]] = None,
    music_start: float = 0.0,
    selected_junction: Optional[int] = None,
) -> Text:
    width = max(width, 1)
    text = Text(no_wrap=True, overflow="crop")
    if not tl.clips:
        text.append("(empty timeline)", style="dim")
        return text
    total = max(tl.duration, 1e-6)
    beat_ids = []
    for c in tl.clips:
        if c.beat_id not in beat_ids:
            beat_ids.append(c.beat_id)
    window, spans = _layout(tl, width, selected_junction if selected_junction is not None else selected)
    for k, i in enumerate(window):
        c = tl.clips[i]
        if k > 0:
            prev = tl.clips[i - 1]
            locked = prev.locked_out and c.locked_in
            glyph = "L" if locked else GLYPHS.get(c.transition_in.kind, "|")
            if i - 1 == selected_junction:
                glyph_style = "bold black on yellow"
            else:
                glyph_style = "bold white" if locked else "white"
            text.append(glyph, style=glyph_style)
        span = spans[k]
        label = f"{c.id[-3:].lstrip('0') or '0'}:s{c.shot_index}"[:span].ljust(span)
        style = PALETTE[beat_ids.index(c.beat_id) % len(PALETTE)]
        if selected_junction is None and i == selected:
            style += " reverse bold"
        text.append(label, style=style)
    if beats is not None:
        text.append("\n")
        grid = [" "] * width
        for b in beats:
            t = b - music_start
            if 0 <= t <= total:
                col = min(width - 1, int(t / total * width))
                grid[col] = "."
        text.append("".join(grid), style="dim")
    return text


class TimelineTrack(Static):
    DEFAULT_CSS = "TimelineTrack { height: 2; }"

    class JunctionClicked(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.timeline: Optional[Timeline] = None
        self.beats: Optional[List[float]] = None
        self.music_start = 0.0
        self.selected = 0
        self.selected_junction: Optional[int] = None

    def set_timeline(self, tl: Optional[Timeline], beats: Optional[List[float]] = None, music_start: float = 0.0) -> None:
        self.timeline, self.beats, self.music_start = tl, beats, music_start
        self.selected = min(self.selected, max(0, (len(tl.clips) - 1) if tl else 0))
        if tl is None:
            self.selected = 0
        self._redraw()

    def select_next(self) -> None:
        if self.timeline and self.selected < len(self.timeline.clips) - 1:
            self.selected += 1
            self._redraw()

    def select_prev(self) -> None:
        if self.selected > 0:
            self.selected -= 1
            self._redraw()

    def set_junction(self, j: Optional[int]) -> None:
        if j is None or self.timeline is None or len(self.timeline.clips) < 2:
            self.selected_junction = None
        else:
            self.selected_junction = max(0, min(j, len(self.timeline.clips) - 2))
        self._redraw()

    def _width(self) -> int:
        return max(20, self.size.width or 80)

    def on_click(self, event: Click) -> None:
        if self.selected_junction is None or self.timeline is None:
            return
        cols = junction_columns(self.timeline, self._width(), self.selected_junction)
        if not cols:
            return
        j, col = min(cols.items(), key=lambda kv: abs(kv[1] - event.x))
        if abs(col - event.x) <= 1:
            self.post_message(self.JunctionClicked(j))

    def _redraw(self) -> None:
        width = self._width()
        if self.timeline is None:
            self.update(Text("(no timeline yet)", style="dim"))
            return
        self.update(render_track(self.timeline, width, self.selected, self.beats, self.music_start, selected_junction=self.selected_junction))

    def on_resize(self) -> None:
        self._redraw()
