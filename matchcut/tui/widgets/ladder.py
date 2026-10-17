"""The stage ladder: one row per pipeline stage with its state and timing."""
from __future__ import annotations

from typing import Dict

from rich.text import Text
from textual.widgets import DataTable

from ..state import StageStatus

STATE_STYLES = {
    "pending": "dim",
    "cached": "cyan",
    "running": "bold yellow",
    "done": "green",
    "waiting": "bold magenta",
    "failed": "bold red",
    "stopping": "red",
    "skipped": "dim italic",
}


class StageLadder(DataTable):
    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = True
        self.add_columns("Stage", "State", "Time")

    def update_stages(self, stages: Dict[str, StageStatus]) -> None:
        self.clear()
        for name, s in stages.items():
            seconds = f"{s.seconds:.1f}s" if s.state in ("done",) else ""
            label = f"skipped: {s.note}" if s.state == "skipped" and s.note else (s.state if not s.error else f"{s.state}: {s.error[:40]}")
            self.add_row(name, Text(label, style=STATE_STYLES.get(s.state, "")), seconds, key=name)
