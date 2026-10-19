"""Music energy curve with tempo caption and beat-change markers."""
from __future__ import annotations

from typing import List, Optional

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Sparkline, Static

from matchcut.pipeline.audio import MusicAnalysis


class EnergyCurve(Vertical):
    DEFAULT_CSS = "EnergyCurve { height: auto; } EnergyCurve > Sparkline { height: 3; }"

    def compose(self) -> ComposeResult:
        yield Sparkline([0.0, 0.0], id="energy")
        yield Static("no music", id="energy-caption")

    def caption_text(self) -> str:
        return str(self.query_one("#energy-caption", Static).content)

    def update_music(self, music: Optional[MusicAnalysis], beat_times: Optional[List[float]] = None) -> None:
        spark = self.query_one("#energy", Sparkline)
        cap = self.query_one("#energy-caption", Static)
        if music is None:
            spark.data = [0.0, 0.0]
            spark.display = False
            cap.update("no music track")
            return
        spark.display = True
        spark.data = [float(x) for x in music.energy] or [0.0, 0.0]
        marks = ", ".join(f"{t:.1f}s" for t in (beat_times or []))
        cap.update(f"{music.tempo_bpm:.0f} BPM, {music.duration:.1f}s, {len(music.beats)} beats" + (f"; beat changes at {marks}" if marks else ""))
