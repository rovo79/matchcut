"""Runs the pipeline on a worker thread and talks to it through a queue.

Framework-free: the TUI pumps ``drain()`` from its event loop and calls
``answer`` / ``stop`` from the UI thread. The pipeline thread only puts events
on the queue and blocks inside the checkpoint handler.

``interactive=False`` (the default) answers every checkpoint with Continue
immediately, so a run behaves like the CLI; the app passes ``interactive=True``
and answers from the Run view.
"""
from __future__ import annotations

import queue
import threading
from typing import Dict, List, Optional, Tuple

from matchcut.pipeline import events
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.llm import LLMBackend
from matchcut.pipeline.pipeline import Pipeline


class PipelineBridge:
    def __init__(self, cfg: PipelineConfig, backend: Optional[LLMBackend] = None, interactive: bool = False):
        self.cfg = cfg
        self.backend = backend
        self.interactive = interactive
        self.queue: "queue.Queue[events.Event]" = queue.Queue()
        self.cancel = threading.Event()
        self.pending: Optional[Tuple[str, dict]] = None
        self.pipeline: Optional[Pipeline] = None
        self._answer: Optional[events.CheckpointAnswer] = None
        self._answered = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self.is_running():
            raise RuntimeError("a run is already in progress")
        self.cancel.clear()
        self.pending = None
        self.pipeline = Pipeline(self.cfg, on_event=self.queue.put, on_checkpoint=self._checkpoint, cancel=self.cancel)
        if self.backend is not None:
            self.pipeline.llm = self.backend
            self.backend.on_call = self.pipeline._emit
        self._thread = threading.Thread(target=self._run, name="matchcut-pipeline", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self.pipeline is not None
        try:
            self.pipeline.run()
        except Exception:
            # RunFailed was already emitted by Pipeline.run(); the thread just ends
            pass
        finally:
            self.pending = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def drain(self) -> List[events.Event]:
        out: List[events.Event] = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except queue.Empty:
                return out

    # -- checkpoints (pipeline thread blocks here) ----------------------------
    def _checkpoint(self, stage: str, summary: dict) -> events.CheckpointAnswer:
        if not self.interactive:
            return events.Continue()
        self._answered.clear()
        self._answer = None
        self.pending = (stage, summary)
        if self.cancel.is_set():
            # stop() landed before pending was visible to it (a stage was still running): abort now
            # instead of blocking on a prompt nobody will answer. Checked after pending is set so
            # either this sees cancel or stop() sees pending.
            self.pending = None
            return events.Abort()
        self._answered.wait()
        self.pending = None
        return self._answer if self._answer is not None else events.Continue()

    def answer(self, answer: events.CheckpointAnswer) -> bool:
        """Answer the pending checkpoint from the UI thread. Returns False when none is pending."""
        if self.pending is None:
            return False
        if isinstance(answer, events.Redo):
            self.cfg.save(self.cfg.workspace_path / "config.json")  # config edits made while paused persist
        self._answer = answer
        self._answered.set()
        return True

    def stop(self) -> None:
        """Cancel at the next boundary; abort immediately if a checkpoint is pending."""
        self.cancel.set()
        if self.pending is not None:
            self.answer(events.Abort())

    # -- live context updates ------------------------------------------------
    def set_descriptions(self, descriptions: Dict[int, dict]) -> None:
        """Make edited shot descriptions visible to the stages that run after this point."""
        if self.pipeline is not None and self.pipeline._ctx is not None:
            self.pipeline._ctx.descriptions = dict(descriptions)
