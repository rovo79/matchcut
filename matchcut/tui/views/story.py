"""Story view: logline, theme, beats as cards, music energy curve."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..state import RunState
from ..widgets.sparkline import EnergyCurve


def file_fingerprint(paths: Iterable[Path]) -> Tuple[Optional[Tuple[int, int]], ...]:
    """(mtime_ns, size) per path, None for a missing file: cheap identity of what a view last rendered."""
    out: List[Optional[Tuple[int, int]]] = []
    for path in paths:
        try:
            st = path.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def timeline_files(workspace: Path) -> List[Path]:
    """Every file a timeline stage can come from, in stage order."""
    passes = sorted((workspace / "passes").glob("*.json"))
    return [workspace / "roughcut.json"] + passes + [workspace / "final.json"]


class StoryView(VerticalScroll):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._fingerprint: Optional[tuple] = None  # identity of the data last rendered

    def compose(self) -> ComposeResult:
        yield Static("", id="story-head")
        yield EnergyCurve(id="story-energy")

    @staticmethod
    def _data_fingerprint(state: RunState) -> tuple:
        ws = state.workspace
        files = [ws / "narrative.json", ws / "music.json"] + timeline_files(ws)
        return (file_fingerprint(files), state.config.music.path, state.config.music.start)

    def refresh_from_state(self, state: RunState) -> None:
        fingerprint = self._data_fingerprint(state)
        if fingerprint == self._fingerprint:
            return  # nothing changed on disk: keep the cards and the curve as they are
        self._fingerprint = fingerprint
        n = state.narrative()
        head = self.query_one("#story-head", Static)
        for card in list(self.query(".beat-card")):
            card.remove()
        if not n:
            head.update("(no narrative yet: run the pipeline)")
            self.query_one("#story-energy", EnergyCurve).update_music(state.music())
            return
        head.update(
            Text.assemble(
                ("Logline  ", "bold"), n.get("logline", ""), "\n",
                ("Theme    ", "bold"), n.get("theme", ""), "\n",
                ("Notes    ", "bold"), n.get("structure_notes", ""), "\n",
                ("Unused   ", "bold"), ", ".join(map(str, n.get("unused_shot_ids", []))) or "-",
            )
        )
        for b in n.get("beats", []):
            text = Text.assemble(
                (f"{b.get('id', '')}  {b.get('title', '')}", "bold cyan"),
                f"   {b.get('pace', '')}   {float(b.get('target_duration_s', 0)):.1f}s   {b.get('emotional_tone', '')}   in: {b.get('transition_in', 'cut')}\n",
                b.get("intent", ""), "\n",
                ("shots: ", "dim"), ", ".join(map(str, b.get("shot_ids", []))),
            )
            self.mount(Static(text, classes="beat-card"))
        self.query_one("#story-energy", EnergyCurve).update_music(state.music(), self._beat_change_times(state))

    @staticmethod
    def _beat_change_times(state: RunState) -> List[float]:
        stages = state.timeline_stages()
        tl = state.timeline("final") or (state.timeline(stages[-1]) if stages else None)
        if tl is None:
            return []
        times = []
        for i in range(1, len(tl.clips)):
            if tl.clips[i].beat_id != tl.clips[i - 1].beat_id:
                times.append(round(tl.record_start(i) + state.config.music.start, 2))
        return times
