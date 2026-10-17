"""Cut view: the timeline track, clip detail, and a stepper through the passes."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from matchcut.pipeline.sampling import FrameCache
from .. import images
from ..state import RunState, file_fingerprint
from ..widgets.frame import Frame
from ..widgets.timeline import TimelineTrack


class CutView(Vertical):
    can_focus = True  # the clip and pass bindings only apply while focus sits here
    BINDINGS = [
        Binding("left", "prev_clip", "Previous clip", show=False),
        Binding("right", "next_clip", "Next clip", show=False),
        Binding("left_square_bracket", "prev_stage", "Earlier pass", show=False),
        Binding("right_square_bracket", "next_stage", "Later pass", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._state: Optional[RunState] = None
        self.stages: List[str] = []
        self.stage_index = -1  # -1 means "latest"
        self._fingerprint: Optional[tuple] = None  # identity of the data last rendered
        self._shown: Optional[tuple] = None  # (fingerprint, stage, clip) last shown in the detail row

    def compose(self) -> ComposeResult:
        yield Static("", id="cut-header")
        yield TimelineTrack(id="track")
        with Horizontal(id="clip-row"):
            yield Frame(id="clip-frame", caption="first frame")
            yield Static("", id="clip-detail")

    def current_stage(self) -> Optional[str]:
        if not self.stages:
            return None
        idx = self.stage_index if self.stage_index >= 0 else len(self.stages) - 1
        return self.stages[min(idx, len(self.stages) - 1)]

    @staticmethod
    def _data_fingerprint(state: RunState) -> tuple:
        ws = state.workspace
        files = [ws / "probe.json", ws / "music.json"] + state.timeline_files()
        return (tuple(file_fingerprint(f) for f in files), state.config.music.path, state.config.music.start)

    def refresh_from_state(self, state: RunState) -> None:
        self._state = state
        fingerprint = self._data_fingerprint(state)
        if fingerprint == self._fingerprint:
            return  # nothing changed on disk (a stage step resets this): keep the track and frame
        self._fingerprint = fingerprint
        self.stages = state.timeline_stages()
        if self.stage_index >= len(self.stages):
            self.stage_index = -1
        stage = self.current_stage()
        track = self.query_one("#track", TimelineTrack)
        header = self.query_one("#cut-header", Static)
        if stage is None:
            header.update("(no timeline yet: run the pipeline)")
            track.set_timeline(None)
            self.query_one("#clip-detail", Static).update("")
            self.query_one("#clip-frame", Frame).show(None)
            self._shown = None
            return
        tl = state.timeline(stage)
        music = state.music()
        beats = [float(b) for b in music.beats] if music is not None else None
        track.set_timeline(tl, beats, state.config.music.start)
        pos = self.stages.index(stage) + 1
        header.update(Text.assemble(("stage ", "dim"), (stage, "bold"), (f"  ({pos}/{len(self.stages)})  ", "dim"), f"{len(tl.clips) if tl else 0} clips, {tl.duration if tl else 0:.1f}s   [ ] step passes   ←/→ select clip"))
        self._show_clip()

    def _show_clip(self) -> None:
        if self._state is None:
            return
        track = self.query_one("#track", TimelineTrack)
        tl = track.timeline
        detail = self.query_one("#clip-detail", Static)
        frame = self.query_one("#clip-frame", Frame)
        if tl is None or not tl.clips:
            detail.update("")
            frame.show(None)
            self._shown = None
            return
        i = track.selected
        c = tl.clips[i]
        stage = self.current_stage() or "final"
        key = (self._fingerprint, stage, i)
        if self._shown == key:
            return  # this clip of this pass is already on screen: no second frame extraction
        self._shown = key
        decisions = self._state.decisions_for(c.id, stage)
        lines = [
            f"{c.id}  shot {c.shot_index}  beat {c.beat_id}",
            f"source {c.source_in:.2f}-{c.source_out:.2f}s  ({c.duration:.2f}s)  record in {tl.record_start(i):.2f}s",
            f"transition in: {c.transition_in.kind} {c.transition_in.duration:.2f}s   locked: {'in ' if c.locked_in else ''}{'out' if c.locked_out else ''}".rstrip(),
            f"tags: {', '.join(c.tags) or '-'}   purpose: {c.purpose or '-'}",
            "",
            f"decisions ({len(decisions)}):",
        ] + [f"  {d.pass_name}:{d.action}  {d.detail}" for d in decisions[-12:]]
        detail.update("\n".join(lines))
        media = self._state.media()
        if media is None:
            frame.show(None)
            return
        cache = FrameCache(media.path, self._state.workspace / "frames", self._state.config.sampling)
        try:
            img = cache.frame(c.source_in)
        except Exception:
            img = None
        frame.show(img, caption=f"{c.id} @ {c.source_in:.2f}s")
        if frame.image_mode:
            self._state.image_mode = frame.image_mode

    def open_target(self) -> Optional[Path]:
        """The frame file of the selected clip, as shown in the clip row."""
        track = self.query_one("#track", TimelineTrack)
        tl = track.timeline
        media = self._state.media() if self._state is not None else None
        if tl is None or not tl.clips or media is None:
            return None
        t = tl.clips[track.selected].source_in
        return images.cached_frame_path(media.path, self._state.workspace / "frames", self._state.config.sampling, t)

    # -- actions ------------------------------------------------------------------
    def action_prev_clip(self) -> None:
        self.query_one("#track", TimelineTrack).select_prev()
        self._show_clip()

    def action_next_clip(self) -> None:
        self.query_one("#track", TimelineTrack).select_next()
        self._show_clip()

    def action_prev_stage(self) -> None:
        if not self.stages or self._state is None:
            return
        current = self.stages.index(self.current_stage())
        self.stage_index = max(0, current - 1)
        self._fingerprint = None  # force the rebuild for the new pass
        self.refresh_from_state(self._state)

    def action_next_stage(self) -> None:
        if not self.stages or self._state is None:
            return
        current = self.stages.index(self.current_stage())
        self.stage_index = min(len(self.stages) - 1, current + 1)
        if self.stage_index == len(self.stages) - 1:
            self.stage_index = -1  # follow the latest again
        self._fingerprint = None  # force the rebuild for the new pass
        self.refresh_from_state(self._state)
