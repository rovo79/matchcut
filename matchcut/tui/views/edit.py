"""Edit workbench: pick a junction on the track and see why the cut is there."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from matchcut.pipeline.editorial import Pin, PinStore, pin_outcome
from matchcut.pipeline.junctions import Junction, build_junctions, scorer_for_workspace
from matchcut.pipeline.sampling import FrameCache
from matchcut.pipeline.timeline import clip_origin
from matchcut.review import controller, items
from .. import images
from ..state import RunState, file_fingerprint
from ..widgets.frame import Frame
from ..widgets.inspector import render_inspector
from ..widgets.timeline import TimelineTrack

FRAME_IDS = ("out-frame", "in-frame", "blend-frame")


class _Busy(Exception):
    """A request for a junction that is already being prepared."""


class EditView(Vertical):
    can_focus = True  # junction, stage and pin keys only apply while focus sits here
    BINDINGS = [
        Binding("left", "prev_junction", "Previous junction", show=False),
        Binding("right", "next_junction", "Next junction", show=False),
        Binding("left_square_bracket", "prev_stage", "Earlier stage", show=False),
        Binding("right_square_bracket", "next_stage", "Later stage", show=False),
        Binding("l", "lock", "Lock"),
        Binding("v", "revert", "Revert"),
        Binding("x", "clear_pin", "Clear pin"),
        Binding("enter", "toggle_history", "History"),
        Binding("p", "play", "Play"),
        Binding("a", "ab", "A/B"),
        Binding("s", "stop_play", "Stop"),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._state: Optional[RunState] = None
        self.stages: List[str] = []
        self.stage_index = -1  # -1 follows the latest stage
        self.junctions: List[Junction] = []
        self.selected = 0
        self.expanded = False
        self._fingerprint: Optional[tuple] = None  # identity of the workspace data last rendered
        self._shown: Optional[tuple] = None  # (fingerprint, stage, junction, expanded) the panel text was rendered for
        self._frames_key: Optional[tuple] = None  # (stage, clips, cut points, media) the three frames were rendered for
        self._blend_image = None
        self._keep_pair: Optional[Tuple[int, int]] = None  # the shot pair to stay on when the junctions are rebuilt for another stage
        self._review_fp: Optional[tuple] = None  # the workspace data the review statuses belong to
        self._review_status: Dict[tuple, Tuple[str, str]] = {}  # (stage, out_shot, in_shot) -> (text, style)
        self._preparing: set = set()  # keys being prepared; touched by the UI thread and the review workers
        self._preparing_lock = threading.Lock()
        self._ab_key: Optional[tuple] = None  # the junction an A/B pair was last handed to the controller for
        self._playing_key: Optional[tuple] = None  # the junction the controller last played (what `s` pauses)

    def compose(self) -> ComposeResult:
        yield Static("", id="edit-header")
        yield TimelineTrack(id="edit-track")
        yield Static("", id="beat-strip")
        with Horizontal(id="junction-frames"):
            yield Frame(id="out-frame", caption="outgoing", classes="junction-frame")
            yield Frame(id="in-frame", caption="incoming", classes="junction-frame")
            yield Frame(id="blend-frame", caption="overlay", classes="junction-frame")
        yield Static("", id="review-status")
        with VerticalScroll(id="junction-scroll"):
            yield Static("", id="junction-panel")

    # -- what is shown ----------------------------------------------------------
    def current_stage(self) -> Optional[str]:
        if not self.stages:
            return None
        idx = self.stage_index if self.stage_index >= 0 else len(self.stages) - 1
        return self.stages[min(idx, len(self.stages) - 1)]

    def current_junction(self) -> Optional[Junction]:
        return self.junctions[self.selected] if self.junctions else None

    @staticmethod
    def _data_fingerprint(state: RunState) -> tuple:
        ws = state.workspace
        files = [ws / "probe.json", ws / "analysis.npz", ws / "narrative.json", ws / "editorial.json"] + state.timeline_files()
        return tuple(file_fingerprint(f) for f in files)

    def refresh_from_state(self, state: RunState) -> None:
        self._state = state
        fingerprint = self._data_fingerprint(state)
        if fingerprint != self._review_fp:
            self._review_fp = fingerprint  # the cut changed under the old results: only the requests in flight stay
            with self._preparing_lock:
                busy = set(self._preparing)
            self._review_status = {k: v for k, v in self._review_status.items() if k in busy}
            self._ab_key = None  # a re-run or a pin may have changed the pair: prepare again before toggling
        if fingerprint == self._fingerprint:
            return
        self._fingerprint = fingerprint
        self.stages = state.timeline_stages()
        if self.stage_index >= len(self.stages):
            self.stage_index = -1
        stage = self.current_stage()
        track = self.query_one("#edit-track", TimelineTrack)
        header = self.query_one("#edit-header", Static)
        strip = self.query_one("#beat-strip", Static)
        if stage is None:
            self.junctions = []
            header.update("(no cut yet: run the pipeline, then inspect its junctions here)")
            track.set_timeline(None)
            strip.update("")
            self._show_junction()
            return
        upto = self.stages[: self.stages.index(stage) + 1]
        pairs = [(s, state.timeline(s)) for s in upto]
        pairs = [(s, t) for s, t in pairs if t is not None]
        self.junctions = build_junctions(pairs, scorer_for_workspace(state.workspace, state.config)) if pairs else []
        pair, self._keep_pair = self._keep_pair, None
        kept = next((i for i, j in enumerate(self.junctions) if (j.out_shot, j.in_shot) == pair), None) if pair is not None else None
        self.selected = kept if kept is not None else min(self.selected, max(0, len(self.junctions) - 1))
        tl = state.timeline(stage)
        track.set_timeline(tl)
        track.set_junction(self.selected if self.junctions else None)
        pins = PinStore(state.workspace).load()
        unplaced = [d for d in (tl.history if tl else []) if d.pass_name == "editor" and d.action == "pin_unplaced"]
        summary = f" ({self.stages.index(stage) + 1}/{len(self.stages)}) · {len(tl.clips) if tl else 0} clips · {tl.duration if tl else 0:.1f}s · {len(self.junctions)} junctions · {len(pins)} pinned"
        if unplaced:
            summary += f" · {len(unplaced)} pin(s) not placed"
        header.update(Text.assemble(("stage ", "dim"), (stage, "bold"), summary, ("   [ ] stage   ←/→ junction", "dim")))
        strip.update(self._beat_strip(state, tl))
        self._show_junction()

    @staticmethod
    def _beat_strip(state: RunState, tl) -> Text:
        beats = (state.narrative() or {}).get("beats") or []
        if not beats or tl is None:
            return Text("")
        t = Text("beats  ", style="dim")
        for b in beats:
            seconds = sum(c.duration for c in tl.clips if c.beat_id == b.get("id"))
            t.append(f"{b.get('id', '?')} {b.get('title', '')} {seconds:.1f}s   ")
        return t

    def _frames(self) -> List[Frame]:
        return [self.query_one(f"#{fid}", Frame) for fid in FRAME_IDS]

    def _show_junction(self, force: bool = False) -> None:
        if self._state is None:
            return
        panel = self.query_one("#junction-panel", Static)
        j, stage = self.current_junction(), self.current_stage()
        self._show_review_status()
        if j is None or stage is None:
            panel.update("(no junctions: the cut needs at least two clips)" if stage else "")
            for f in self._frames():
                f.show(None)
            self._shown, self._frames_key, self._blend_image = None, None, None
            return
        key = (self._fingerprint, stage, self.selected, self.expanded)
        if self._shown != key or force:
            self._shown = key
            pin = PinStore(self._state.workspace).get(j.out_shot, j.in_shot)
            panel.update(render_inspector(j, stage, pin, self.expanded, self._pin_decision(pin)))
        # The frames depend only on what is cut where, not on every workspace write (a pin changes editorial.json).
        shown = j.state_at(stage) or j.current
        media = self._state.media()
        frames_key = (stage, j.out_clip, j.in_clip, shown.out_time, shown.in_time, media.path if media is not None else None)
        if self._frames_key != frames_key or force:
            self._frames_key = frames_key
            self._show_frames(j, stage)

    def _pin_decision(self, pin: Optional[Pin]):
        """What the latest stage's history says happened to ``pin``, whichever stage is being inspected."""
        if pin is None or self._state is None or not self.stages:
            return None
        latest = self._state.timeline(self.stages[-1])
        return pin_outcome(latest, pin) if latest is not None else None

    def _show_frames(self, j: Junction, stage: str) -> None:
        frames = self._frames()
        media = self._state.media()
        shown = j.state_at(stage) or j.current
        if media is None:
            for f in frames:
                f.show(None)
            self._blend_image = None
            return
        cache = FrameCache(media.path, self._state.workspace / "frames", self._state.config.sampling)
        fps = media.fps or j.fps or 25.0
        grid = lambda t: round(t * fps) / fps  # rough-cut points are off the frame grid; ask for the frame that is actually shown
        out_t = max(0.0, grid(shown.out_time - 1.0 / fps))  # the last frame shown before the cut
        in_t = grid(shown.in_time)
        try:
            out_img, in_img = cache.frame(out_t), cache.frame(in_t)
        except Exception:
            out_img = in_img = None
        self._blend_image = images.blend(out_img, in_img) if out_img is not None and in_img is not None else None
        frames[0].show(out_img, caption=f"outgoing · {j.out_clip} @ {out_t:.2f}s")
        frames[1].show(in_img, caption=f"incoming · {j.in_clip} @ {in_t:.2f}s")
        frames[2].show(self._blend_image, caption="overlay · 50% blend")
        mode = next((f.image_mode for f in frames if f.image_mode), "")
        if mode:
            self._state.image_mode = mode

    def reshow(self) -> None:
        """Re-mount the frames once the tab is visible: images mounted while it was hidden get no height."""
        self._show_junction(force=True)

    def open_target(self) -> Optional[Path]:
        j = self.current_junction()
        if j is None or self._blend_image is None or self._state is None:
            return None
        path = self._state.workspace / "frames" / f"blend_{j.out_clip}_{j.in_clip}_{self.current_stage()}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._blend_image.save(path, quality=90)
        return path

    # -- actions --------------------------------------------------------------------
    def _select(self, i: int) -> None:
        if not self.junctions:
            return
        self.selected = max(0, min(i, len(self.junctions) - 1))
        self.query_one("#edit-track", TimelineTrack).set_junction(self.selected)
        self._show_junction()

    def on_timeline_track_junction_clicked(self, message: TimelineTrack.JunctionClicked) -> None:
        self._select(message.index)

    def action_prev_junction(self) -> None:
        self._select(self.selected - 1)

    def action_next_junction(self) -> None:
        self._select(self.selected + 1)

    def _step_stage(self, delta: int) -> None:
        if not self.stages or self._state is None:
            return
        current = self.stages.index(self.current_stage())
        j = self.current_junction()
        self._keep_pair = (j.out_shot, j.in_shot) if j is not None else None
        target = max(0, min(len(self.stages) - 1, current + delta))
        self.stage_index = -1 if target == len(self.stages) - 1 else target
        self._fingerprint = None  # rebuild the junctions for the newly inspected stage
        self.refresh_from_state(self._state)

    def action_prev_stage(self) -> None:
        self._step_stage(-1)

    def action_next_stage(self) -> None:
        self._step_stage(1)

    def action_toggle_history(self) -> None:
        if self.current_junction() is None:
            return
        self.expanded = not self.expanded
        self._show_junction()

    def _apply_hint(self, message: str) -> str:
        bridge = getattr(self.app, "bridge", None)
        if bridge is not None and bridge.is_running():
            pending = getattr(bridge, "pending", None)
            stage = str(pending[0]) if pending else ""
            if stage in ("describe", "narrative", "roughcut"):
                return message + ": applies in this run when you continue (the passes have not run yet)"
            return message + ": applies on the next run (R); this run's passes have already started"
        return message + ": takes effect on the next run (R); cached stages are reused and only the passes re-run"

    def _pin(self, origin: str) -> None:
        j, stage = self.current_junction(), self.current_stage()
        if j is None or stage is None or self._state is None:
            self.app.notify("no junction selected", severity="warning")
            return
        source = j.baseline if origin == "reverted" else (j.state_at(stage) or j.current)
        # The pin remembers the two clip instances (not just their points) so a regenerated cut finds the same pieces. A clip
        # names its instance only through a real ``origin`` (or in the rough cut, where its range IS the instance key); from
        # a stage file that predates origins nothing is written rather than a key computed from a modified range.
        stage_tl = self._state.timeline(stage)
        by_id = {c.id: c for c in stage_tl.clips} if stage_tl is not None else {}

        def instance(clip_id: str, shot: int) -> str:
            c = by_id.get(clip_id)
            if c is None or c.shot_index != shot:
                return ""
            return c.origin or (clip_origin(c) if stage == "roughcut" else "")

        pin = Pin(j.out_shot, j.in_shot, round(source.out_time, 3), round(source.in_time, 3), origin,
                  out_origin=instance(j.out_clip, j.out_shot), in_origin=instance(j.in_clip, j.in_shot))
        PinStore(self._state.workspace).put(pin)
        note = self._apply_hint(f"pinned shot {j.out_shot} → shot {j.in_shot} ({origin}) at out {pin.out_time:.2f}s / in {pin.in_time:.2f}s")
        if j.baseline.stage != j.first_stage:
            note += (". these shots are not adjacent in the rough cut: the next run moves one of these shots next to the other, "
                     "unless that would split or reorder the beats or another pin holds them")
        self.app.notify(note)
        self._fingerprint = None
        self.refresh_from_state(self._state)

    def action_lock(self) -> None:
        self._pin("locked")

    def action_revert(self) -> None:
        self._pin("reverted")

    def action_clear_pin(self) -> None:
        j = self.current_junction()
        if j is None or self._state is None:
            self.app.notify("no junction selected", severity="warning")
            return
        if PinStore(self._state.workspace).remove(j.out_shot, j.in_shot):
            self.app.notify("pin cleared: the passes decide this junction again on the next run")
        else:
            self.app.notify("this junction has no pin", severity="warning")
        self._fingerprint = None
        self.refresh_from_state(self._state)

    # -- review in mpv -----------------------------------------------------------------
    def _review_key(self) -> Optional[tuple]:
        j, stage = self.current_junction(), self.current_stage()
        return (stage, j.out_shot, j.in_shot) if j is not None and stage is not None else None

    def _show_review_status(self) -> None:
        j, stage = self.current_junction(), self.current_stage()
        line = self.query_one("#review-status", Static)
        if j is None or stage is None:
            line.update("")
            return
        text, style = self._review_status.get((stage, j.out_shot, j.in_shot)) or (None, "")
        if text is None:
            base = j.baseline.stage
            text, style = ("p play" if base == stage else f"p play · a compare with {base}"), "dim"
        line.update(Text(text, style=style))

    def _set_review_status(self, key: tuple, text: str, style: str = "") -> None:
        if text:
            self._review_status[key] = (text, style)
        else:
            self._review_status.pop(key, None)  # no status: the idle line shows
        if key == self._review_key():
            self._show_review_status()

    def _claim(self, key: tuple) -> bool:
        with self._preparing_lock:
            if key in self._preparing:
                return False
            self._preparing.add(key)
            return True

    def _release(self, key: tuple) -> None:
        with self._preparing_lock:
            self._preparing.discard(key)

    def _review_request(self, ab: bool) -> None:
        key, j, stage = self._review_key(), self.current_junction(), self.current_stage()
        if key is None or j is None or stage is None or self._state is None:
            self.app.notify("no junction selected", severity="warning")
            return
        if not hasattr(self.app, "review_controller"):
            return
        ctl = self.app.review_controller()
        pair = getattr(ctl, "pair", None)
        # A/B toggles only a pair prepared for this very junction; anything else is prepared first.
        toggle = ab and self._ab_key == key and pair is not None and pair.a is not None and pair.b is not None
        if not toggle and not self._claim(key):
            self.app.notify("already preparing this junction", severity="warning")
            return
        if not toggle:
            self._set_review_status(key, "preparing the junction for review...", "dim")
        ws, cfg, index = self._state.workspace, self._state.config, j.index
        self.run_worker(
            lambda: self._review_job(ctl, key, ws, cfg, stage, index, ab, toggle),
            thread=True, group="review", exit_on_error=False,
        )

    def _review_job(self, ctl, key: tuple, ws: Path, cfg, stage: str, index: int, ab: bool, toggle: bool) -> None:
        """Runs in a worker thread: nothing here touches a widget; the outcome goes back through ``_review_done``."""
        text, style, note, ab_pair, played = "", "", "", False, False
        claimed = not toggle
        try:
            if getattr(self.app, "review_closing", False):
                return
            if toggle and ctl.toggle():
                text, played = ctl.status, True
            else:
                if toggle:  # nothing to toggle any more (the mpv window was closed): prepare again
                    if not self._claim(key):
                        raise _Busy()
                    claimed = True
                    self._ui(self._set_review_status, key, "preparing the junction for review...", "dim")
                mpv = ctl.has_mpv()
                pair = items.prepare_review(ws, cfg, stage=stage, index=index, ab=ab, force_render=not mpv)
                if getattr(self.app, "review_closing", False):
                    return
                if ab and not mpv and pair.a is not None:
                    ctl.open_file(items.ab_file(pair, ws, cfg))
                else:
                    ctl.play_pair(pair)
                    ab_pair = ab and pair.a is not None
                played = True
                text, note = ctl.status, pair.note
                if note:
                    text += " · " + note
        except _Busy:
            text, style = "already preparing this junction", "busy"
        except Exception as exc:
            text, style = f"review failed: {exc}", "red"
        finally:
            if claimed:
                self._release(key)
            if getattr(self.app, "review_closing", False):
                try:
                    ctl.close()  # the app closed while this job was in flight: it must not leave an mpv behind
                except Exception:
                    pass
        if text:
            self._ui(self._review_done, key, text, style, note, ab_pair, played)

    def _ui(self, fn, *args) -> None:
        try:
            self.app.call_from_thread(fn, *args)
        except Exception:
            pass  # the app has closed

    def _review_done(self, key: tuple, text: str, style: str, note: str, ab_pair: bool, played: bool = False) -> None:
        if style == "busy":
            self.app.notify(text, severity="warning")
            return
        if ab_pair:
            self._ab_key = key
        if played:
            self._playing_key = key
        self._set_review_status(key, text, style)
        if style == "red":
            self.app.notify(text, severity="error")
        elif note:
            self.app.notify(note)

    def action_play(self) -> None:
        self._review_request(False)

    def action_ab(self) -> None:
        self._review_request(True)

    def action_stop_play(self) -> None:
        if self._state is None or not hasattr(self.app, "review_controller"):
            return
        key = self._playing_key  # pause what was played, whichever junction is selected now
        if key is None:
            self.app.notify("nothing is playing")
            return
        ctl = self.app.review_controller()

        def job() -> None:
            text, style = "", ""
            try:
                ctl.stop()
                text = ctl.status
            except Exception as exc:
                text, style = f"review failed: {exc}", "red"
            if text:
                self._ui(self._review_done, key, text, style, "", False, False)

        self.run_worker(job, thread=True, group="review", exit_on_error=False)
