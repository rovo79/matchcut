"""Run view: stage ladder, live log, checkpoint panel."""
from __future__ import annotations

import json
from typing import Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, RichLog, Static

from matchcut.pipeline import events
from ..state import RunState
from ..widgets.ladder import StageLadder

FILTERS = ("all", "decisions", "model", "log")


class NoteInput(Input):
    """The director's note field; Escape leaves it without submitting."""

    BINDINGS = [Binding("escape", "leave", "Leave field", show=False)]

    def action_leave(self) -> None:
        self.screen.query_one("#ladder").focus()


class RunView(Vertical):
    BINDINGS = [
        Binding("c", "cp_continue", "Continue", show=False),
        Binding("r", "cp_redo", "Redo with note", show=False),
        Binding("a", "cp_abort", "Abort", show=False),
        Binding("f", "cycle_filter", "Filter log", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._seen = 0
        self._filter = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="run-top"):
            yield StageLadder(id="ladder")
            yield RichLog(id="log", highlight=False, markup=False, wrap=True)
        with Vertical(id="checkpoint"):
            yield Static("", id="checkpoint-summary")
            yield NoteInput(placeholder="director's note for a redo (optional)", id="note")
            yield Static("[c] continue   [r] redo with note (type note, Enter or Esc to leave the field, then r)   [a] abort", id="checkpoint-hint")

    def on_mount(self) -> None:
        self.query_one("#checkpoint").display = False

    # -- rendering -----------------------------------------------------------
    def pending_text(self) -> str:
        return str(self.query_one("#checkpoint-summary", Static).content)

    def refresh_from_state(self, state: RunState) -> None:
        self.query_one("#ladder", StageLadder).update_stages(state.stages)
        log = self.query_one("#log", RichLog)
        events_list = list(state.events)
        if state.event_count < self._seen:  # a new run restarted the count
            log.clear()
            self._seen = 0
        fresh = min(state.event_count - self._seen, len(events_list))
        if fresh > 0:
            for e in events_list[-fresh:]:
                line = self._format(e)
                if line is not None:
                    log.write(line)
        self._seen = state.event_count
        panel = self.query_one("#checkpoint")
        if state.checkpoint is not None:
            cp = state.checkpoint
            panel.display = True
            self.query_one("#checkpoint-summary", Static).update(
                Text.assemble((f"Checkpoint after {cp.stage}", "bold magenta"), "\n", json.dumps(cp.summary, indent=1)[:1200])
            )
        else:
            panel.display = False
            note = self.query_one("#note", Input)
            if note.has_focus:  # the field is hidden with the panel; keep the run keys alive
                self.query_one("#ladder").focus()

    def _format(self, e: events.Event) -> Optional[Text]:
        f = FILTERS[self._filter]
        if isinstance(e, events.Decision):
            if f not in ("all", "decisions"):
                return None
            score = f" [{e.score:.2f}]" if e.score is not None else ""
            return Text.assemble((f"{e.pass_name}:{e.action}", "cyan"), f" {', '.join(e.clip_ids[:4])}{score} {e.detail}")
        if isinstance(e, events.ModelCall):
            if f not in ("all", "model"):
                return None
            return Text.assemble(("model ", "yellow"), f"{e.name} images={e.images} in={e.input_tokens} out={e.output_tokens} {e.seconds:.1f}s")
        if isinstance(e, events.Log):
            if f not in ("all", "log"):
                return None
            return Text(e.message, style="dim" if e.level == "info" else "red")
        if isinstance(e, (events.StageStarted, events.StageFinished, events.StageCached)):
            if f != "all":
                return None
            return Text(f"{type(e).__name__.replace('Stage', '').lower()} {e.stage}", style="green")
        if isinstance(e, events.RunFailed):
            return Text.assemble(("FAILED ", "bold red"), f"{e.stage}: {e.error}\n{e.traceback[-800:]}")
        if isinstance(e, events.RunFinished):
            return Text(f"run finished in {e.seconds:.1f}s -> {e.final_path}", style="bold green")
        if isinstance(e, events.Checkpoint):
            return Text(f"checkpoint {e.stage}", style="magenta")
        return None

    # -- actions ------------------------------------------------------------------
    def _answer(self, answer: events.CheckpointAnswer) -> None:
        app = self.app
        if getattr(app, "bridge", None) is None or app.state.checkpoint is None:
            return
        if app.bridge.answer(answer):
            app.state.resolve_checkpoint()
            self.query_one("#note", Input).value = ""
            app.refresh_views()

    def action_cp_continue(self) -> None:
        self._answer(events.Continue())

    def action_cp_redo(self) -> None:
        self._answer(events.Redo(self.query_one("#note", Input).value.strip()))

    def action_cp_abort(self) -> None:
        self._answer(events.Abort())

    def action_cycle_filter(self) -> None:
        self._filter = (self._filter + 1) % len(FILTERS)
        self.query_one("#log", RichLog).clear()
        self._seen = 0
        self.refresh_from_state(self.app.state)
        self.app.notify(f"log filter: {FILTERS[self._filter]}")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "note":
            self.query_one("#ladder").focus()
