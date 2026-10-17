"""A one-line timeline track: clips as coloured spans, transitions, locks, beat grid."""
from __future__ import annotations

from typing import List, Optional

from rich.text import Text
from textual.widgets import Static

from matchcut.pipeline.timeline import Timeline

PALETTE = ["on dark_blue", "on dark_green", "on dark_magenta", "on dark_cyan", "on dark_orange3", "on grey37"]
GLYPHS = {"cut": "|", "dissolve": "~", "dip_to_black": "#"}


def render_track(tl: Timeline, width: int, selected: int, beats: Optional[List[float]] = None, music_start: float = 0.0) -> Text:
    text = Text()
    if not tl.clips:
        text.append("(empty timeline)", style="dim")
        return text
    total = max(tl.duration, 1e-6)
    clip_total = max(sum(c.duration for c in tl.clips), 1e-6)  # tl.duration discounts transition overlaps
    beat_ids = []
    for c in tl.clips:
        if c.beat_id not in beat_ids:
            beat_ids.append(c.beat_id)
    usable = max(width - (len(tl.clips) - 1), len(tl.clips))
    spans = [max(1, int(round(c.duration / clip_total * usable))) for c in tl.clips]
    while sum(spans) > usable and max(spans) > 1:  # rounding and the 1-cell minimum can overshoot
        spans[spans.index(max(spans))] -= 1
    for i, c in enumerate(tl.clips):
        if i > 0:
            prev = tl.clips[i - 1]
            locked = prev.locked_out and c.locked_in
            glyph = "L" if locked else GLYPHS.get(c.transition_in.kind, "|")
            text.append(glyph, style="bold white" if locked else "white")
        span = spans[i]
        label = f"{c.id[-3:].lstrip('0') or '0'}:s{c.shot_index}"[:span].ljust(span)
        style = PALETTE[beat_ids.index(c.beat_id) % len(PALETTE)]
        if i == selected:
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

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self.timeline: Optional[Timeline] = None
        self.beats: Optional[List[float]] = None
        self.music_start = 0.0
        self.selected = 0

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

    def _redraw(self) -> None:
        width = max(20, self.size.width or 80)
        if self.timeline is None:
            self.update(Text("(no timeline yet)", style="dim"))
            return
        self.update(render_track(self.timeline, width, self.selected, self.beats, self.music_start))

    def on_resize(self) -> None:
        self._redraw()
