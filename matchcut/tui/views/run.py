"""Run view: stage ladder, live log, checkpoint panel."""
from __future__ import annotations

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


DETERMINISTIC_HINT = "this pass is deterministic: a redo only changes the result after a config change (5 Config)"
SUMMARY_MAX_LINES = 8  # title + body + hint
SUMMARY_LINE_WIDTH = 100


def _short(value: object, limit: int = SUMMARY_LINE_WIDTH) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _summary_line(key: str, value: object) -> str:
    if isinstance(value, (list, tuple)):
        first = value[0] if value else None
        label = ""
        if isinstance(first, dict):
            label = str(first.get("title") or first.get("id") or "")
        elif first is not None:
            label = str(first)
        return _short(f"{key}: {len(value)} items" + (f" (first: {label})" if label else ""))
    if isinstance(value, dict):
        return _short(f"{key}: " + ", ".join(f"{k}={v}" for k, v in value.items()))
    return _short(f"{key}: {value}")


def checkpoint_text(stage: str, summary: dict) -> Text:
    """Compact checkpoint panel text: one line per summary key, at most SUMMARY_MAX_LINES lines in all."""
    hint = stage.startswith("pass:")
    room = SUMMARY_MAX_LINES - 1 - (1 if hint else 0)
    lines = [_summary_line(k, v) for k, v in summary.items()]
    if len(lines) > room:
        lines = lines[: room - 1] + [f"... {len(lines) - room + 1} more"]
    text = Text.assemble((f"Checkpoint after {stage}", "bold magenta"))
    for line in lines:
        text.append("\n" + line)
    if hint:
        text.append("\n" + DETERMINISTIC_HINT, style="italic yellow")
    return text


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
        self._serial = 0
        self._filter = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="run-top"):
            yield StageLadder(id="ladder")
            yield RichLog(id="log", highlight=False, markup=False, wrap=True)
        with Vertical(id="checkpoint"):
            yield Static("", id="checkpoint-summary")
            with Vertical(id="checkpoint-foot"):  # pinned to the panel's bottom: always visible
                yield Static("[c] continue   [r] redo with note (Tab or click to the field, type, Enter or Esc to leave it, then r)   [a] abort", id="checkpoint-hint", markup=False)
                yield NoteInput(placeholder="director's note for a redo (optional)", id="note")

    def on_mount(self) -> None:
        self.query_one("#checkpoint").display = False

    # -- rendering -----------------------------------------------------------
    def pending_text(self) -> str:
        return str(self.query_one("#checkpoint-summary", Static).content)

    def refresh_from_state(self, state: RunState) -> None:
        self.query_one("#ladder", StageLadder).update_stages(state.stages)
        log = self.query_one("#log", RichLog)
        events_list = list(state.events)
        if state.run_serial != self._serial:  # a new run started: its log starts empty
            log.clear()
            self._seen = 0
            self._serial = state.run_serial
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
                checkpoint_text(cp.stage, cp.summary)
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
