"""Edit workbench: pick a junction on the track and see why the cut is there."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from matchcut.pipeline.editorial import Pin, PinStore, pin_outcome, pin_states
from matchcut.pipeline.junctions import Junction, build_junctions, scorer_for_workspace, stale_edit, stale_human
from matchcut.pipeline.sampling import FrameCache
from matchcut.pipeline.timeline import HUMAN_STAGE, clip_origin
from matchcut.review import controller, items
from .. import images
from ..state import RunState, file_fingerprint
from ..widgets.frame import Frame
from ..widgets.inspector import render_inspector
from ..widgets.timeline import TimelineTrack

FRAME_IDS = ("out-frame", "in-frame", "blend-frame")


class _Busy(Exception):
    """A request for a junction that is already being prepared."""


class _Identity(NamedTuple):
    """What identifies the selected junction across stages (``EditView._identity`` / ``_restore``)."""
    pair: Tuple[int, int]  # (out shot, in shot)
    occurrence: int  # how many junctions of the stage before it have this shot pair
    index: int
    origins: Optional[Tuple[str, str]]  # ``clip_origin`` of its two clips: the instances; None when the stage cannot be read
    origin_occurrence: int  # how many junctions of the stage before it have these two instances


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
        Binding("s", "stop_play", "Pause"),
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
        self._keep: Optional[_Identity] = None  # the junction to stay on when the junctions are rebuilt for another stage
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
        files = [ws / "probe.json", ws / "analysis.npz", ws / "narrative.json", ws / "editorial.json", ws / "human.json", ws / "human_import.json"] + state.timeline_files()
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
        self.junctions = build_junctions(pairs, scorer_for_workspace(state.workspace, state.config), pins=PinStore(state.workspace).load()) if pairs else []
        keep, self._keep = self._keep, None
        kept = self._restore(keep, state.timeline(stage)) if keep is not None else None
        self.selected = kept if kept is not None else min(self.selected, max(0, len(self.junctions) - 1))
        tl = state.timeline(stage)
        track.set_timeline(tl)
        track.set_junction(self.selected if self.junctions else None)
        pins = PinStore(state.workspace).load()
        unplaced = [d for d in (tl.history if tl else []) if d.pass_name == "editor" and d.action == "pin_unplaced"]
        summary = f" ({self.stages.index(stage) + 1}/{len(self.stages)}) · {len(tl.clips) if tl else 0} clips · {tl.duration if tl else 0:.1f}s · {len(self.junctions)} junctions · {len(pins)} pinned"
        if unplaced:
            summary += f" · {len(unplaced)} pin(s) not placed"
        for line in self._changed_pins(state, pins):
            summary += f" · {line}"
        # an agent's published edit or an imported editor cut that no longer fits is left out of the stages: say why
        stale = "\n".join(r for r in (stale_edit(state.workspace), stale_human(state.workspace)) if r)
        header.update(Text.assemble(("stage ", "dim"), (stage, "bold"), summary, ("   [ ] stage   ←/→ junction", "dim"), *([("\n" + stale, "yellow")] if stale else [])))
        strip.update(self._beat_strip(state, tl))
        self._show_junction()

    def _origins(self, tl, index: int) -> Optional[Tuple[str, str]]:
        """The clip instances (``clip_origin``) on either side of junction ``index`` of ``tl``."""
        if tl is None or index + 1 >= len(tl.clips):
            return None
        return (clip_origin(tl.clips[index]), clip_origin(tl.clips[index + 1]))

    def _identity(self, tl) -> Optional[_Identity]:
        """The selected junction as the other stages can find it again: its clip instances, its shot pair and which occurrence of each it is."""
        j = self.current_junction()
        if j is None:
            return None
        pair = (j.out_shot, j.in_shot)
        # (the stage file may have been rewritten since the junctions were built: its clips there are not this junction's)
        if tl is not None and j.index + 1 < len(tl.clips) and (tl.clips[j.index].id, tl.clips[j.index + 1].id) != (j.out_clip, j.in_clip):
            tl = None
        origins = self._origins(tl, j.index)
        before = self.junctions[: j.index]
        return _Identity(pair, sum((o.out_shot, o.in_shot) == pair for o in before), j.index, origins,
                         sum(self._origins(tl, o.index) == origins for o in before) if origins is not None else 0)

    def _restore(self, keep: _Identity, tl) -> Optional[int]:
        """The index in the rebuilt junctions (of stage timeline ``tl``) that is ``keep``, or None to keep the clamped index.
        In order: the junction between the same clip instances (the k-th when the cut repeats them, else the one nearest the old index); the
        same shot pair at the same occurrence; the same shot pair nearest the old index."""
        if keep.origins is not None:
            same = [j.index for j in self.junctions if self._origins(tl, j.index) == keep.origins]
            if same:
                return same[keep.origin_occurrence] if keep.origin_occurrence < len(same) else min(same, key=lambda i: abs(i - keep.index))
        same = [j.index for j in self.junctions if (j.out_shot, j.in_shot) == keep.pair]
        if not same:
            return None
        return same[keep.occurrence] if keep.occurrence < len(same) else min(same, key=lambda i: abs(i - keep.index))

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
            store = PinStore(self._state.workspace)
            # where the human stage says a pin changed, the badge is on the junction the pin now sits at (its pair, or its surviving clip), not by shots
            pin = (store.get(*j.pin_key) if j.pin_lost and j.pin_key else None) or store.get(j.out_shot, j.in_shot)
            panel.update(render_inspector(j, stage, pin, self.expanded, self._pin_decision(pin), self.stages[-1] if self.stages else ""))
        # The frames depend only on what is cut where, not on every workspace write (a pin changes editorial.json).
        shown = j.state_at(stage) or j.current
        media = self._state.media()
        frames_key = (stage, j.out_clip, j.in_clip, shown.out_time, shown.in_time, media.path if media is not None else None)
        if self._frames_key != frames_key or force:
            self._frames_key = frames_key
            self._show_frames(j, stage)

    def _changed_pins(self, state: RunState, pins: List[Pin]) -> List[str]:
        """One header phrase per pin the latest stage reports as changed by the editor, with its reason (only the human stage can: an imported
        editor cut that no longer holds the pin).  The same ``pin_states`` the ``junctions`` command prints."""
        if not pins or not self.stages or self.stages[-1] != HUMAN_STAGE:
            return []
        return [f"pin {st.label} changed by the editor: {st.reason}" for st in pin_states(state.timeline(HUMAN_STAGE), pins, HUMAN_STAGE) if st.state == "changed"]

    def _pin_decision(self, pin: Optional[Pin]):
        """What the latest stage's history says happened to ``pin``, whichever stage is being inspected."""
        if pin is None or self._state is None or not self.stages:
            return None
        latest = self._state.timeline(self.stages[-1])
        return pin_outcome(latest, pin, stage=self.stages[-1]) if latest is not None else None

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
        self._keep = self._identity(self._state.timeline(self.current_stage()))
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
        # the junction's index too: a stage can hold the same shot pair twice, and each is its own cut to review
        return (stage, j.index, j.out_shot, j.in_shot) if j is not None and stage is not None else None

    def _show_review_status(self) -> None:
        j, stage = self.current_junction(), self.current_stage()
        line = self.query_one("#review-status", Static)
        if j is None or stage is None:
            line.update("")
            return
        text, style = self._review_status.get(self._review_key()) or (None, "")
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
        if self._state is not None:
            # A cut rewritten outside the app (no run event behind it) is picked up before it is reviewed.  The selection follows its junction
            # (the same restore a stage switch uses), not its index: the clips may have moved.
            if self._data_fingerprint(self._state) != self._fingerprint:
                self._keep = self._identity(self._state.timeline(self.current_stage()))
            self.refresh_from_state(self._state)
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
        fp = self._data_fingerprint(self._state)  # the cut this request is about: a result for an older cut is not a pair to toggle
        self.run_worker(
            lambda: self._review_job(ctl, key, ws, cfg, stage, index, ab, toggle, fp),
            thread=True, group="review", exit_on_error=False,
        )

    def _review_job(self, ctl, key: tuple, ws: Path, cfg, stage: str, index: int, ab: bool, toggle: bool, fp: Optional[tuple] = None) -> None:
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
            self._ui(self._review_done, key, text, style, note, ab_pair, played, fp, ab)

    def _ui(self, fn, *args) -> None:
        try:
            self.app.call_from_thread(fn, *args)
        except Exception:
            pass  # the app has closed

    def _review_done(self, key: tuple, text: str, style: str, note: str, ab_pair: bool, played: bool = False,
                     fp: Optional[tuple] = None, ab: bool = False) -> None:
        if style == "busy":
            self.app.notify(text, severity="warning")
            return
        # The workspace data changed while this was prepared (a re-run, a pin): what was handed to the controller was cut from the old
        # timeline.  The editor asked for that playback, so it stays, but it is not the pair `a` toggles: the next `a` prepares afresh.
        stale = fp is not None and self._state is not None and self._data_fingerprint(self._state) != fp
        if stale and played and style != "red":
            text += " (the cut changed: press a again to compare the new cut)" if ab else " (the cut changed: press p again to play the new cut)"
        if ab_pair and not stale:
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
