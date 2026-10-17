"""Shots view: the catalog, three keyframes and the model's description."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static

from matchcut.pipeline.sampling import FrameCache, shot_sample_times
from .. import images
from ..state import RunState
from ..widgets.frame import Frame


class ShotsView(Horizontal):
    BINDINGS = [Binding("u", "toggle_usable", "Toggle usable", show=False)]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._state: Optional[RunState] = None
        self._loaded_for: Optional[int] = None

    def compose(self) -> ComposeResult:
        yield DataTable(id="shots-table")
        with Vertical(id="shot-detail"):
            with Horizontal(id="shot-frames"):
                yield Frame(id="shot-head", caption="head")
                yield Frame(id="shot-mid", caption="middle")
                yield Frame(id="shot-tail", caption="tail")
            yield Static("", id="shot-desc")

    def on_mount(self) -> None:
        table = self.query_one("#shots-table", DataTable)
        table.cursor_type = "row"
        table.zebra_stripes = True
        table.add_columns("#", "Start", "End", "Dur", "Subject", "Size", "Move", "Q", "Use")

    # -- data --------------------------------------------------------------
    def refresh_from_state(self, state: RunState) -> None:
        self._state = state
        table = self.query_one("#shots-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        descs = state.descriptions()
        for shot in state.shots():
            d = descs.get(shot.index, {})
            table.add_row(
                str(shot.index),
                f"{shot.start:.2f}",
                f"{shot.end:.2f}",
                f"{shot.duration:.1f}",
                str(d.get("subject", ""))[:28],
                str(d.get("shot_size", "")),
                str(d.get("camera_movement", "")),
                f"{float(d.get('quality', 0.0)):.2f}" if d else "",
                Text("yes" if d.get("usable", True) else "no", style="green" if d.get("usable", True) else "red"),
                key=str(shot.index),
            )
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))
            self._show(self.current_shot_index())
        else:
            self.query_one("#shot-desc", Static).update("(no shots yet: run the pipeline)")

    def current_shot_index(self) -> Optional[int]:
        table = self.query_one("#shots-table", DataTable)
        if not table.row_count:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            return int(str(key.value))
        except Exception:
            return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show(int(str(event.row_key.value)) if event.row_key is not None else None)

    def _show(self, index: Optional[int]) -> None:
        if self._state is None or index is None:
            return
        shots = {s.index: s for s in self._state.shots()}
        shot = shots.get(index)
        media = self._state.media()
        if shot is None or media is None:
            return
        self._loaded_for = index
        cache = FrameCache(media.path, self._state.workspace / "frames", self._state.config.sampling)
        times = shot_sample_times(shot, 3)
        for fid, t in zip(("#shot-head", "#shot-mid", "#shot-tail"), times):
            frame = self.query_one(fid, Frame)
            try:
                img = cache.frame(t)
            except Exception:
                img = None
            frame.show(img, caption=f"{t:.2f}s")
            if frame.image_mode:
                self._state.image_mode = frame.image_mode
        d = self._state.descriptions().get(index, {})
        lines = [f"Shot {index}  {shot.start:.2f}-{shot.end:.2f}s ({shot.duration:.1f}s)"]
        if d:
            lines.append(d.get("description", ""))
            lines.append(f"subject: {d.get('subject', '')}   setting: {d.get('setting', '')}   mood: {d.get('mood', '')}")
            lines.append(f"size: {d.get('shot_size', '')}   camera: {d.get('camera_movement', '')}   motion: {d.get('subject_motion', '')}   shapes: {d.get('dominant_shapes', '')}")
            lines.append(f"key action: {d.get('key_action', '')}   quality: {d.get('quality', '')}   usable: {d.get('usable', True)}   tags: {', '.join(d.get('tags', []))}")
        else:
            lines.append("(no description yet)")
        self.query_one("#shot-desc", Static).update("\n".join(lines))

    # -- actions ---------------------------------------------------------------
    def action_toggle_usable(self) -> None:
        index = self.current_shot_index()
        if self._state is None or index is None:
            return
        path = self._state.workspace / "descriptions.json"
        if not path.exists():
            self.app.notify("no descriptions yet", severity="warning")
            return
        data: Dict[str, dict] = json.loads(path.read_text())
        entry = data.get(str(index))
        if entry is None:
            return
        entry["usable"] = not entry.get("usable", True)
        path.write_text(json.dumps(data, indent=2))
        self._state.invalidate(str(path))
        bridge = getattr(self.app, "bridge", None)
        if bridge is not None:
            bridge.set_descriptions({int(k): v for k, v in data.items()})
        self.refresh_from_state(self._state)
        self.app.notify(f"shot {index} usable: {entry['usable']}")
