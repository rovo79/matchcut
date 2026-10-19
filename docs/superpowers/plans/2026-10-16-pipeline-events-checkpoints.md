# Pipeline Events, Checkpoints and Director's Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the pipeline an event bus, blocking checkpoints with redo-with-a-note, director's notes in the prompts, and a cancel flag, so a UI can drive and watch a run while the CLI behaves exactly as before.

**Architecture:** A new `events.py` defines typed event dataclasses, the `CheckpointAnswer` variants and `RunAborted`. `Pipeline(cfg, on_event=None, on_checkpoint=None, cancel=None)` emits events from its existing stage boundaries, converts the `matchcut.pipeline` log records to `Log` events during `run()`, and wraps the checkpointed stages (describe, narrative, roughcut, each pass) in a loop that re-runs a stage when the handler answers `Redo(note)`, after invalidating that stage's and all downstream cached outputs. Notes live on `Context.notes` and are rendered into the four prompts that have one. `LLMBackend` gets an `on_call` callback for `ModelCall` events. `run_passes` gets `on_event`, `after_pass` and `cancel` keyword arguments.

**Tech Stack:** Python 3.9+, dataclasses, `threading.Event`, the existing mock backend and synthetic footage for tests. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-16-tui-design.md` (sections "Pipeline additions", "Threading model" as far as it constrains the pipeline, "Testing: Pipeline side", and "Implementation split" plan 1).

## Global Constraints

- All pipeline code lives in `matchcut/pipeline/`; tests in `tests/pipeline/`. Run tests with `python3 -m pytest tests/pipeline -q` (47 pass today; the legacy `tests/test_*.py` need sklearn/torch and are not part of this work).
- Python 3.9 compatible syntax: every module starts with `from __future__ import annotations`; use `Optional[...]`, `List[...]`, `Dict[...]`, `Callable[...]` from `typing`, never `X | None` at runtime.
- No new runtime dependencies.
- The CLI (`python -m matchcut.pipeline ...`) must behave exactly as before: no handler means every checkpoint answers `Continue()`, no cancel flag, no event callback. All 47 existing tests keep passing after every task.
- Callbacks are invoked on the pipeline's own thread; the pipeline never starts threads itself.
- Stage names used in events, checkpoints and notes are exactly: `ingest`, `describe`, `narrative`, `roughcut`, `pass:<name>` (e.g. `pass:matchcut`), `render`, `report`.
- Commit messages end with these two trailer lines, verbatim:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ
  ```
  committed with `git -c user.name="Claude" -c user.email="noreply@anthropic.com" commit -F <msgfile>` (write the message to a file outside the repo; a nested heredoc is not needed). Never put a model identifier in code or docs.
- Branch: `claude/ai-video-editing-pipeline-bkqqay`. Push with `git push -u origin claude/ai-video-editing-pipeline-bkqqay` after each task's commit. Never stage anything under `.superpowers/`.

---

## File structure

| File | Responsibility |
|---|---|
| `matchcut/pipeline/events.py` (create) | Event dataclasses, `CheckpointAnswer` variants (`Continue`, `Redo`, `Abort`), `RunAborted`, handler type aliases |
| `matchcut/pipeline/context.py` (modify) | `Context.notes`, `Context.notes_text(stage)` |
| `matchcut/pipeline/prompts.py` (modify) | `{notes}` placeholder in `DESCRIBE`, `NARRATIVE`, `ROUGH_CUT`, `POLISH` |
| `matchcut/pipeline/stages.py` (modify) | pass `notes=` into the three stage prompts |
| `matchcut/pipeline/llm.py` (modify) | `LLMBackend.on_call`, shared `_record_call`, `ModelCall` events from both backends |
| `matchcut/pipeline/passes.py` (modify) | `notes=` into `POLISH`; `run_passes(..., on_event, after_pass, cancel)` with per-pass events, redo loop, note decisions |
| `matchcut/pipeline/pipeline.py` (modify) | constructor arguments, `_emit`, log-to-event handler, stage events, checkpoint loop with invalidation, cancel checks, run lifecycle events |
| `docs/PIPELINE.md` (modify) | "Events and checkpoints" section |
| `tests/__init__.py` (create) | makes `tests` a package so the conftest helper has one import path |
| `tests/pipeline/conftest.py` (modify) | shared `RecordingMock` helper class |
| `tests/pipeline/test_events.py` (create) | event dataclasses, backend `ModelCall`, run event order, cached stages |
| `tests/pipeline/test_checkpoints.py` (create) | redo with note, abort, cancel, pass redo |
| `tests/pipeline/test_pipeline.py` (modify) | import `RecordingMock` from conftest instead of defining it |

---

### Task 1: Event types, director's notes on the context, notes in the prompts

**Files:**
- Create: `matchcut/pipeline/events.py`
- Modify: `matchcut/pipeline/context.py` (field after `music`, method after `music_text`)
- Modify: `matchcut/pipeline/prompts.py` (`DESCRIBE`, `NARRATIVE`, `ROUGH_CUT`, `POLISH`)
- Modify: `matchcut/pipeline/stages.py:44`, `:92`, `:138` (the three `.format(` calls)
- Modify: `matchcut/pipeline/passes.py:535` (`prompts.POLISH.format(`)
- Create: `tests/__init__.py` (empty; makes `tests` a package so `from tests.pipeline.conftest import ...` resolves the same module pytest loads)
- Modify: `tests/pipeline/conftest.py` (add `RecordingMock`), `tests/pipeline/test_pipeline.py:66-79` (remove the local `RecordingMock`, import it)
- Test: `tests/pipeline/test_events.py`

**Interfaces:**
- Consumes: `Context` (fields `descriptions`, `narrative`, `music`), `prompts`, `MockBackend`.
- Produces:
  - `events.Event` base dataclass with `time: float` and `to_dict() -> dict` (includes `"type": <class name>`); subclasses `RunStarted(source: str, workspace: str, config: dict)`, `StageStarted(stage: str)`, `StageCached(stage: str, path: str)`, `StageFinished(stage: str, seconds: float, path: str)`, `Decision(pass_name: str, action: str, clip_ids: List[str], detail: str, score: Optional[float], data: dict)`, `ModelCall(name: str, images: int, seconds: float, input_tokens: int, output_tokens: int, model: str)`, `Log(level: str, message: str)`, `Checkpoint(stage: str, summary: dict)`, `RunFinished(final_path: str, render_path: str, report_path: str, seconds: float)`, `RunFailed(stage: str, error: str, traceback: str)`.
  - `events.Continue()`, `events.Redo(note: str = "")`, `events.Abort()`; `events.CheckpointAnswer = Union[Continue, Redo, Abort]`; `events.EventHandler = Callable[[Event], None]`; `events.CheckpointHandler = Callable[[str, dict], CheckpointAnswer]`; `events.RunAborted(Exception)` with attribute `stage`.
  - `Context.notes: Dict[str, List[str]]` (default empty) and `Context.notes_text(stage: str) -> str` returning exactly `"Director's notes: (none)"` when there are none, else `"Director's notes:\n- <note>\n- <note>"`.
  - Prompts `DESCRIBE`, `NARRATIVE`, `ROUGH_CUT`, `POLISH` take a `notes=` format argument; stage keys are `"describe"`, `"narrative"`, `"roughcut"`, `"pass:polish"`.
  - `tests/pipeline/conftest.py` exports class `RecordingMock(MockBackend)` with `self.texts: Dict[str, List[str]]` keyed by request name.

- [ ] **Step 1: Write the failing tests**

Create `tests/pipeline/test_events.py`:

```python
import time

from matchcut.pipeline import events
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.pipeline import Pipeline
from tests.pipeline.conftest import RecordingMock


def test_event_dataclasses_serialise_with_type_and_time():
    before = time.time()
    e = events.StageFinished(stage="describe", seconds=1.25, path="/ws/descriptions.json")
    d = e.to_dict()
    assert d["type"] == "StageFinished"
    assert d["stage"] == "describe" and d["seconds"] == 1.25 and d["path"] == "/ws/descriptions.json"
    assert before <= d["time"] <= time.time()
    assert events.Decision(pass_name="matchcut", action="snap", clip_ids=["c001"], detail="x", score=0.5, data={}).to_dict()["type"] == "Decision"


def test_checkpoint_answers():
    assert events.Redo().note == ""
    assert events.Redo("tighter").note == "tighter"
    assert isinstance(events.Continue(), events.Continue) and isinstance(events.Abort(), events.Abort)
    err = events.RunAborted("roughcut")
    assert err.stage == "roughcut" and "roughcut" in str(err)


def test_notes_text_and_prompt_rendering(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    pipe = Pipeline(cfg)
    pipe.llm = RecordingMock(cfg.llm)
    ctx = pipe.ingest()
    assert ctx.notes == {}
    assert ctx.notes_text("narrative") == "Director's notes: (none)"
    ctx.notes["narrative"] = ["end on the bird", "slower opening"]
    assert ctx.notes_text("narrative") == "Director's notes:\n- end on the bird\n- slower opening"
    ctx.notes["describe"] = ["call the rabbit by name"]
    ctx.notes["roughcut"] = ["start on the sky"]
    pipe.rough_cut()
    assert "call the rabbit by name" in pipe.llm.texts["describe"][0]
    assert "- end on the bird" in pipe.llm.texts["narrative"][0]
    assert "start on the sky" in pipe.llm.texts["rough_cut"][0]
    assert "Director's notes: (none)" not in pipe.llm.texts["narrative"][0]
```

Add to `tests/pipeline/conftest.py` (after the imports; note the `MockBackend` import):

```python
from matchcut.pipeline.llm import MockBackend


class RecordingMock(MockBackend):
    """Mock backend that keeps the prompt text of every call, keyed by request name."""

    def __init__(self, cfg, log=None):
        super().__init__(cfg, log)
        self.texts = {}

    def structured(self, request):
        self.texts.setdefault(request.name, []).append(request.text)
        return super().structured(request)
```

Create an empty `tests/__init__.py` (`tests/pipeline/__init__.py` already exists). With both in place pytest imports the conftest as `tests.pipeline.conftest`, the same module the tests import, so the class is one object and fixtures are registered once.

In `tests/pipeline/test_pipeline.py`, delete the local `RecordingMock` class and its `from matchcut.pipeline.llm import MockBackend` line (lines 66-79) and add `from tests.pipeline.conftest import RecordingMock` next to the other top-of-file imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/pipeline/test_events.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'matchcut.pipeline.events'`.

- [ ] **Step 3: Create `events.py`**

```python
"""Typed events a pipeline run emits, and the answers a checkpoint handler returns.

Callbacks run on the pipeline's own thread. A UI that wants them on its loop
queues them; the pipeline never starts threads.
"""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Union


@dataclass
class Event:
    time: float = field(default_factory=time.time, init=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["type"] = type(self).__name__
        return d


@dataclass
class RunStarted(Event):
    source: str = ""
    workspace: str = ""
    config: dict = field(default_factory=dict)


@dataclass
class StageStarted(Event):
    stage: str = ""


@dataclass
class StageCached(Event):
    stage: str = ""
    path: str = ""


@dataclass
class StageFinished(Event):
    stage: str = ""
    seconds: float = 0.0
    path: str = ""


@dataclass
class Decision(Event):
    pass_name: str = ""
    action: str = ""
    clip_ids: List[str] = field(default_factory=list)
    detail: str = ""
    score: Optional[float] = None
    data: dict = field(default_factory=dict)


@dataclass
class ModelCall(Event):
    name: str = ""
    images: int = 0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


@dataclass
class Log(Event):
    level: str = "info"
    message: str = ""


@dataclass
class Checkpoint(Event):
    stage: str = ""
    summary: dict = field(default_factory=dict)


@dataclass
class RunFinished(Event):
    final_path: str = ""
    render_path: str = ""
    report_path: str = ""
    seconds: float = 0.0


@dataclass
class RunFailed(Event):
    stage: str = ""
    error: str = ""
    traceback: str = ""


# -- checkpoint answers -----------------------------------------------------
@dataclass
class Continue:
    pass


@dataclass
class Redo:
    note: str = ""


@dataclass
class Abort:
    pass


CheckpointAnswer = Union[Continue, Redo, Abort]
EventHandler = Callable[[Event], None]
CheckpointHandler = Callable[[str, dict], CheckpointAnswer]


class RunAborted(Exception):
    """Raised inside a run when a checkpoint answers Abort or the cancel flag is set."""

    def __init__(self, stage: str):
        super().__init__(f"run aborted at {stage}")
        self.stage = stage
```

- [ ] **Step 4: Add notes to the context**

In `matchcut/pipeline/context.py`, after `music: Optional[MusicAnalysis] = None` add:

```python
    notes: Dict[str, List[str]] = field(default_factory=dict)  # director's notes per stage, from checkpoints
```

and after the `music_text` method add:

```python
    def notes_text(self, stage: str) -> str:
        """Prompt paragraph with the director's notes recorded for ``stage``."""
        notes = self.notes.get(stage, [])
        if not notes:
            return "Director's notes: (none)"
        return "Director's notes:\n" + "\n".join(f"- {n}" for n in notes)
```

- [ ] **Step 5: Add the placeholder to the four prompts**

In `matchcut/pipeline/prompts.py`:

`DESCRIBE`: change the final lines
```
dominant motion direction from optical analysis) are:
{machine_notes}"""
```
to
```
dominant motion direction from optical analysis) are:
{machine_notes}

{notes}"""
```

`NARRATIVE`: change
```
Target running time: {target}
{music}
Footage summary:
```
to
```
Target running time: {target}
{music}
{notes}
Footage summary:
```

`ROUGH_CUT`: change
```
Target running time: {target}
{music}
Narrative:
```
to
```
Target running time: {target}
{music}
{notes}
Narrative:
```

`POLISH`: change
```
Target running time: {target}
Narrative:
```
to
```
Target running time: {target}
{notes}
Narrative:
```
(`POLISH` is the only prompt with "Target running time" directly followed by "Narrative:" now that `ROUGH_CUT` has `{music}` between them; anchor on that.)

- [ ] **Step 6: Pass the notes from the call sites**

`matchcut/pipeline/stages.py`: in `describe_shots` add `notes=ctx.notes_text("describe"),` to `prompts.DESCRIBE.format(...)`; in `find_narrative` add `notes=ctx.notes_text("narrative"),` to `prompts.NARRATIVE.format(...)`; in `rough_cut` add `notes=ctx.notes_text("roughcut"),` to `prompts.ROUGH_CUT.format(...)`.

`matchcut/pipeline/passes.py`, `PolishPass.apply`: add `notes=ctx.notes_text("pass:polish"),` to `prompts.POLISH.format(...)`.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m pytest tests/pipeline/test_events.py -q` then `python3 -m pytest tests/pipeline -q`
Expected: 3 passed, then 50 passed overall (47 + 3), including the two existing music-prompt tests that now import `RecordingMock` from conftest.

- [ ] **Step 8: Commit**

Message:
```
Add pipeline event types and director's notes in the prompts

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ
```
Stage: `matchcut/pipeline/events.py matchcut/pipeline/context.py matchcut/pipeline/prompts.py matchcut/pipeline/stages.py matchcut/pipeline/passes.py tests/__init__.py tests/pipeline/conftest.py tests/pipeline/test_events.py tests/pipeline/test_pipeline.py`. Push.

---

### Task 2: ModelCall events from the backends

**Files:**
- Modify: `matchcut/pipeline/llm.py:58-70` (`LLMBackend`), `:98-135` (`AnthropicBackend.structured`), `:177-185` (`MockBackend.structured`)
- Test: `tests/pipeline/test_events.py`

**Interfaces:**
- Consumes: `events.ModelCall` (Task 1).
- Produces: `LLMBackend.on_call: Optional[Callable[[ModelCall], None]]` (attribute, default None) and `LLMBackend._record_call(name: str, images: int, seconds: float, input_tokens: int, output_tokens: int) -> None`, which increments `self.calls`, adds to `self.usage`, and fires `on_call` with a `ModelCall(model=self.cfg.model)`. Both backends call it exactly once per successful `structured` call.

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_events.py`:

```python
def test_backends_report_model_calls(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    pipe = Pipeline(cfg)
    calls = []
    pipe.llm.on_call = calls.append
    pipe.narrative()
    assert len(calls) == pipe.llm.calls == 2  # describe + narrative with 8 shots on one sheet
    assert [c.name for c in calls] == ["describe", "narrative"]
    assert calls[0].images == 1 and calls[1].images == 0
    assert all(isinstance(c, events.ModelCall) and c.model == cfg.llm.model and c.seconds >= 0 for c in calls)
    assert calls[0].input_tokens == 0 and calls[0].output_tokens == 0  # the mock reports no tokens
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/pipeline/test_events.py::test_backends_report_model_calls -q`
Expected: FAIL (`calls` stays empty: `assert 0 == 2`).

- [ ] **Step 3: Add `on_call` and `_record_call` to the base class**

In `matchcut/pipeline/llm.py` add the import `from .events import ModelCall` after `from .config import LLMConfig`, and change `LLMBackend` to:

```python
class LLMBackend:
    def __init__(self, cfg: LLMConfig, log: Optional[CallLog] = None):
        self.cfg = cfg
        self.log = log or CallLog(None)
        self.calls = 0
        self.usage: Dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.on_call: Optional[Callable[[ModelCall], None]] = None  # set by the pipeline to emit events

    def structured(self, request: LLMRequest) -> BaseModel:
        raise NotImplementedError

    def _record_call(self, name: str, images: int, seconds: float, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.usage["input_tokens"] += int(input_tokens)
        self.usage["output_tokens"] += int(output_tokens)
        if self.on_call is not None:
            self.on_call(
                ModelCall(
                    name=name,
                    images=images,
                    seconds=round(seconds, 3),
                    input_tokens=int(input_tokens),
                    output_tokens=int(output_tokens),
                    model=self.cfg.model,
                )
            )
```

Add `Callable` to the `typing` import line.

- [ ] **Step 4: Use it in both backends**

In `AnthropicBackend.structured`, replace the three lines
```python
            usage = getattr(response, "usage", None)
            if usage is not None:
                self.usage["input_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
                self.usage["output_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
            self.calls += 1
```
with
```python
            usage = getattr(response, "usage", None)
            in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage is not None else 0
            out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage is not None else 0
```
and, inside the `try:` right after `parsed = request.schema.model_validate_json(text)` succeeds (before `self.log.write(`), add:
```python
                self._record_call(request.name, len(request.images), time.time() - started, in_tok, out_tok)
```
(so a call whose output fails validation and is retried is counted once, on the attempt that parsed).

In `MockBackend.structured`, replace `self.calls += 1` with nothing at the top, and after `result = handler(request.payload)` add:
```python
        self._record_call(request.name, len(request.images), 0.0, 0, 0)
```
keeping the existing `self.log.write(...)` and `return request.schema.model_validate(result)`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/pipeline -q`
Expected: 51 passed. (`test_full_run_with_mock_backend` asserts `pipe2.llm.calls == 1`, which still holds.)

- [ ] **Step 6: Commit**

Message:
```
Report model calls through an on_call callback on the backends

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ
```
Stage `matchcut/pipeline/llm.py tests/pipeline/test_events.py`. Push.

---

### Task 3: `run_passes` with events, per-pass checkpoints and cancel

**Files:**
- Modify: `matchcut/pipeline/passes.py` (`run_passes`, the last function in the file)
- Test: `tests/pipeline/test_checkpoints.py`

**Interfaces:**
- Consumes: `events.StageStarted/StageFinished/Decision`, `events.Continue/Redo/Abort`, `events.RunAborted`, `timeline.Decision` (existing), `Context.notes`.
- Produces: `run_passes(timeline, ctx, names, save_dir=None, on_event=None, after_pass=None, cancel=None) -> Timeline` where `on_event: Optional[Callable[[Event], None]]`, `after_pass: Optional[Callable[[str, PassResult], CheckpointAnswer]]` is called after each pass with the pass name and its result, and `cancel: Optional[threading.Event]` is checked before each pass. A `Redo(note)` re-runs the same pass on the same input timeline; a non-empty note is appended to `ctx.notes["pass:<name>"]`, recorded on the input timeline's history as `timeline.Decision(pass_name=name, action="note", clip_ids=[], detail=note)` and emitted as an `events.Decision`. The saved file for a redone pass is overwritten.

- [ ] **Step 1: Write the failing tests**

Create `tests/pipeline/test_checkpoints.py`:

```python
import threading

import pytest

from matchcut.pipeline import events
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.passes import run_passes
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Timeline
from tests.pipeline.conftest import RecordingMock


@pytest.fixture(scope="module")
def ctx(matchcut_video, tmp_path_factory):
    cfg = PipelineConfig(source=str(matchcut_video), workspace=str(tmp_path_factory.mktemp("ws")), target_duration=6.0)
    cfg.llm.backend = "mock"
    pipe = Pipeline(cfg)
    pipe.llm = RecordingMock(cfg.llm)
    ctx = pipe.narrative()
    ctx.narrative = {
        "beats": [
            {"id": "b1", "title": "A", "pace": "medium", "target_duration_s": 3.0, "shot_ids": [0, 2], "transition_in": "cut"},
            {"id": "b2", "title": "B", "pace": "fast", "target_duration_s": 3.0, "shot_ids": [3, 4, 1], "transition_in": "dissolve"},
        ]
    }
    return ctx


def base_timeline(ctx) -> Timeline:
    tl = Timeline(source=ctx.media.path, fps=ctx.media.fps)
    tl.clips = [
        Clip("c001", 0, 0.5, 2.5, "b1"),
        Clip("c002", 2, 4.5, 6.0, "b1"),
        Clip("c003", 3, 6.0, 8.0, "b2"),
        Clip("c004", 4, 8.0, 10.0, "b2"),
        Clip("c005", 1, 2.5, 4.5, "b2"),
    ]
    return tl


def test_run_passes_emits_stage_and_decision_events(ctx, tmp_path):
    seen = []
    run_passes(base_timeline(ctx), ctx, ["matchcut", "pacing"], save_dir=tmp_path, on_event=seen.append)
    kinds = [type(e).__name__ for e in seen]
    assert kinds[0] == "StageStarted" and seen[0].stage == "pass:matchcut"
    finished = [e for e in seen if isinstance(e, events.StageFinished)]
    assert [e.stage for e in finished] == ["pass:matchcut", "pass:pacing"]
    assert finished[0].path.endswith("01_matchcut.json") and finished[0].seconds >= 0
    decisions = [e for e in seen if isinstance(e, events.Decision)]
    assert decisions and all(d.pass_name in ("matchcut", "pacing") for d in decisions)
    assert any(d.action == "match_cut" for d in decisions)
    # events are in order: every Decision of a pass sits between its StageStarted and StageFinished
    idx = {id(e): i for i, e in enumerate(seen)}
    start = next(i for i, e in enumerate(seen) if isinstance(e, events.StageStarted) and e.stage == "pass:pacing")
    end = next(i for i, e in enumerate(seen) if isinstance(e, events.StageFinished) and e.stage == "pass:pacing")
    assert all(start < idx[id(d)] < end for d in decisions if d.pass_name == "pacing")


def test_after_pass_redo_reruns_the_pass_with_a_note(ctx, tmp_path):
    answers = iter([events.Redo("let the opening breathe"), events.Continue(), events.Continue()])
    calls = []

    def after_pass(name, result):
        calls.append(name)
        return next(answers)

    seen = []
    final = run_passes(base_timeline(ctx), ctx, ["pacing", "transitions"], save_dir=tmp_path, on_event=seen.append, after_pass=after_pass)
    assert calls == ["pacing", "pacing", "transitions"]
    assert [e.stage for e in seen if isinstance(e, events.StageStarted)] == ["pass:pacing", "pass:pacing", "pass:transitions"]
    assert ctx.notes["pass:pacing"] == ["let the opening breathe"]
    notes = [d for d in final.history if d.pass_name == "pacing" and d.action == "note"]
    assert len(notes) == 1 and notes[0].detail == "let the opening breathe"
    assert any(isinstance(e, events.Decision) and e.action == "note" and e.detail == "let the opening breathe" for e in seen)
    assert (tmp_path / "01_pacing.json").exists() and (tmp_path / "02_transitions.json").exists()
    ctx.notes.clear()


def test_after_pass_abort_raises_run_aborted(ctx, tmp_path):
    with pytest.raises(events.RunAborted) as info:
        run_passes(base_timeline(ctx), ctx, ["pacing", "transitions"], save_dir=tmp_path, after_pass=lambda name, result: events.Abort())
    assert info.value.stage == "pass:pacing"
    assert not (tmp_path / "02_transitions.json").exists()


def test_cancel_flag_stops_before_the_next_pass(ctx, tmp_path):
    cancel = threading.Event()

    def on_event(e):
        if isinstance(e, events.StageFinished) and e.stage == "pass:pacing":
            cancel.set()

    with pytest.raises(events.RunAborted) as info:
        run_passes(base_timeline(ctx), ctx, ["pacing", "transitions", "polish"], save_dir=tmp_path, on_event=on_event, cancel=cancel)
    assert info.value.stage == "pass:transitions"
    assert (tmp_path / "01_pacing.json").exists() and not (tmp_path / "02_transitions.json").exists()


def test_run_passes_without_callbacks_is_unchanged(ctx, tmp_path):
    final = run_passes(base_timeline(ctx), ctx, ["matchcut", "continuity"], save_dir=tmp_path)
    assert final.validate(ctx.shot_bounds) == []
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["01_matchcut.json", "02_continuity.json"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/pipeline/test_checkpoints.py -q`
Expected: 4 FAIL with `TypeError: run_passes() got an unexpected keyword argument 'on_event'` (or `after_pass`/`cancel`); `test_run_passes_without_callbacks_is_unchanged` passes.

- [ ] **Step 3: Rewrite `run_passes`**

In `matchcut/pipeline/passes.py` add the imports (keep the existing ones):

```python
import threading
import time

from . import events
```

and replace `run_passes` with:

```python
def run_passes(
    timeline: Timeline,
    ctx: Context,
    names: Sequence[str],
    save_dir: Optional[Path] = None,
    on_event: Optional[events.EventHandler] = None,
    after_pass: Optional[Callable[[str, PassResult], events.CheckpointAnswer]] = None,
    cancel: Optional[threading.Event] = None,
) -> Timeline:
    """Run the named passes in order.

    ``on_event`` receives StageStarted/StageFinished/Decision events; ``after_pass``
    is asked after every pass whether to Continue, Redo (re-run the same pass on the
    same input, with an optional director's note) or Abort; ``cancel`` is checked
    before each pass. Without callbacks the behaviour is unchanged.
    """

    def emit(e: events.Event) -> None:
        if on_event is not None:
            on_event(e)

    tl = timeline
    for n, name in enumerate(names, start=1):
        if name not in PASSES:
            raise ValueError(f"unknown pass {name!r}; available: {', '.join(PASSES)}")
        stage = f"pass:{name}"
        if cancel is not None and cancel.is_set():
            raise events.RunAborted(stage)
        while True:
            log.info("pass %d/%d: %s", n, len(names), name)
            emit(events.StageStarted(stage=stage))
            started = time.time()
            result = PASSES[name]().apply(tl, ctx)
            candidate = result.timeline
            problems = candidate.validate(ctx.shot_bounds, min_clip=0.1)
            if problems:
                raise RuntimeError(f"pass {name} produced an invalid timeline: {problems[:5]}")
            for d in result.decisions:
                emit(events.Decision(pass_name=d.pass_name, action=d.action, clip_ids=list(d.clip_ids), detail=d.detail, score=d.score, data=dict(d.data)))
            path = ""
            if save_dir is not None:
                candidate.name = f"after {name}"
                out = save_dir / f"{n:02d}_{name}.json"
                candidate.save(out)
                path = str(out)
            log.info("  %d decisions, %d clips, %.1fs", len(result.decisions), len(candidate.clips), candidate.duration)
            emit(events.StageFinished(stage=stage, seconds=round(time.time() - started, 3), path=path))
            answer = after_pass(name, result) if after_pass is not None else events.Continue()
            if isinstance(answer, events.Abort):
                raise events.RunAborted(stage)
            if isinstance(answer, events.Redo):
                if answer.note:
                    ctx.notes.setdefault(stage, []).append(answer.note)
                    note = Decision(pass_name=name, action="note", clip_ids=[], detail=answer.note)
                    tl.history.append(note)
                    emit(events.Decision(pass_name=name, action="note", clip_ids=[], detail=answer.note, score=None, data={}))
                continue
            tl = candidate
            break
    return tl
```

Add `Callable` and `Path` to the imports at the top of `passes.py` if missing (`from pathlib import Path`; `Callable` in the `typing` import). `Decision` here is the existing `timeline.Decision` already imported in `passes.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/pipeline/test_checkpoints.py -q` then `python3 -m pytest tests/pipeline -q`
Expected: 5 passed, then 56 passed overall.

- [ ] **Step 5: Commit**

Message:
```
Emit events from run_passes and support per-pass redo, abort and cancel

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ
```
Stage `matchcut/pipeline/passes.py tests/pipeline/test_checkpoints.py`. Push.

---

### Task 4: Pipeline lifecycle events, stage checkpoints with invalidation, cancel, docs

**Files:**
- Modify: `matchcut/pipeline/pipeline.py` (whole class)
- Modify: `docs/PIPELINE.md` (new section before "## Working iteratively")
- Test: `tests/pipeline/test_events.py`, `tests/pipeline/test_checkpoints.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: `Pipeline(cfg, on_event: Optional[EventHandler] = None, on_checkpoint: Optional[CheckpointHandler] = None, cancel: Optional[threading.Event] = None)`. `run()` emits `RunStarted` first and `RunFinished` last, or `RunFailed` and re-raises. Stage methods emit `StageCached` or `StageStarted`/`StageFinished` for `ingest`, `describe`, `narrative`, `roughcut`, `render`, `report`. Checkpoints after `describe`, `narrative`, `roughcut` (in `run()`) and after each pass (via `run_passes(after_pass=...)`), each preceded by a `Checkpoint(stage, summary)` event. `Redo(note)` on a stage calls `Pipeline.invalidate_from(stage)` (public: deletes that stage's and downstream outputs and clears the matching context state) and re-runs it. The `matchcut.pipeline` logger's records become `Log` events while `run()` is active. `Pipeline.invalidate_from(stage: str) -> None` and `Pipeline.summary_for(stage: str, result) -> dict` are public for the UI.

- [ ] **Step 1: Write the failing tests**

Append to `tests/pipeline/test_events.py`:

```python
def test_run_emits_lifecycle_and_stage_events_in_order(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.proxy = True
    seen = []
    pipe = Pipeline(cfg, on_event=seen.append)
    final = pipe.run()
    kinds = [type(e).__name__ for e in seen]
    assert kinds[0] == "RunStarted" and kinds[-1] == "RunFinished"
    assert seen[0].source == str(demo_video) and seen[0].config["target_duration"] == 8.0
    started = [e.stage for e in seen if isinstance(e, events.StageStarted)]
    assert started[:4] == ["ingest", "describe", "narrative", "roughcut"]
    assert started[4:10] == [f"pass:{p}" for p in cfg.passes]
    assert started[10:] == ["render", "report"]
    finished = {e.stage: e for e in seen if isinstance(e, events.StageFinished)}
    assert finished["describe"].path.endswith("descriptions.json") and finished["roughcut"].path.endswith("roughcut.json")
    assert finished["render"].path.endswith("final_proxy.mp4") and finished["report"].path.endswith("report.md")
    checkpoints = [e.stage for e in seen if isinstance(e, events.Checkpoint)]
    assert checkpoints == ["describe", "narrative", "roughcut"] + [f"pass:{p}" for p in cfg.passes]
    describe_summary = next(e.summary for e in seen if isinstance(e, events.Checkpoint) and e.stage == "describe")
    assert describe_summary["shots"] == 8 and "unusable" in describe_summary
    pass_summary = next(e.summary for e in seen if isinstance(e, events.Checkpoint) and e.stage == "pass:transitions")
    assert pass_summary["pass"] == "transitions" and "decisions" in pass_summary and pass_summary["clips"] == len(final.clips)
    assert any(isinstance(e, events.Log) and "shots detected" in e.message for e in seen)
    assert sum(isinstance(e, events.ModelCall) for e in seen) == pipe.llm.calls
    done = seen[-1]
    assert done.final_path.endswith("final.json") and done.render_path.endswith("final_proxy.mp4") and done.seconds > 0
    assert not any(isinstance(e, events.StageCached) for e in seen)


def test_second_run_reports_cached_stages(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()
    seen = []
    Pipeline(PipelineConfig.load(tmp_path / "ws" / "config.json"), on_event=seen.append).run()
    cached = [e.stage for e in seen if isinstance(e, events.StageCached)]
    assert cached == ["ingest", "describe", "narrative", "roughcut"]
    assert all(e.path for e in seen if isinstance(e, events.StageCached))
    assert [e.stage for e in seen if isinstance(e, events.StageStarted)][:1] == ["pass:matchcut"]
    assert type(seen[-1]).__name__ == "RunFinished"


def test_failure_emits_run_failed_and_reraises(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.passes = ("matchcut", "nosuchpass")
    seen = []
    with pytest.raises(ValueError):
        Pipeline(cfg, on_event=seen.append).run()
    failed = seen[-1]
    assert isinstance(failed, events.RunFailed)
    assert failed.stage == "refine" and "nosuchpass" in failed.error and "Traceback" in failed.traceback
```

Add `import pytest` to the top of `tests/pipeline/test_events.py`.

Append to `tests/pipeline/test_checkpoints.py`:

```python
def test_redo_at_narrative_reruns_with_the_note(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    answers = {"narrative": iter([events.Redo("end on the bird"), events.Continue()])}

    def handler(stage, summary):
        return next(answers.get(stage, iter([])), events.Continue())

    seen = []
    pipe = Pipeline(cfg, on_event=seen.append, on_checkpoint=handler)
    pipe.llm = RecordingMock(cfg.llm)
    pipe.run()
    assert [e.stage for e in seen if isinstance(e, events.StageStarted) and e.stage == "narrative"] == ["narrative", "narrative"]
    assert len(pipe.llm.texts["narrative"]) == 2
    assert "end on the bird" not in pipe.llm.texts["narrative"][0]
    assert "- end on the bird" in pipe.llm.texts["narrative"][1]
    assert "- end on the bird" not in pipe.llm.texts["rough_cut"][0]  # notes are per stage
    assert len(pipe.llm.texts["describe"]) == 1  # upstream stage not re-run
    assert (tmp_path / "ws" / "final.json").exists()
    # the stage was re-run on a cleared cache: narrative.json exists and the roughcut after it was computed once
    assert len(pipe.llm.texts["rough_cut"]) == 1


def test_redo_at_describe_invalidates_downstream(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    Pipeline(cfg).run()  # populate every cache
    assert (tmp_path / "ws" / "narrative.json").exists() and (tmp_path / "ws" / "passes" / "01_matchcut.json").exists()
    pipe = Pipeline(PipelineConfig.load(tmp_path / "ws" / "config.json"))
    pipe.llm = RecordingMock(cfg.llm)
    pipe.invalidate_from("describe")
    assert not (tmp_path / "ws" / "descriptions.json").exists()
    assert not (tmp_path / "ws" / "narrative.json").exists()
    assert not (tmp_path / "ws" / "roughcut.json").exists()
    assert not list((tmp_path / "ws" / "passes").glob("*.json"))
    assert not (tmp_path / "ws" / "final.json").exists() and not (tmp_path / "ws" / "report.md").exists()
    assert (tmp_path / "ws" / "shots.json").exists()  # ingest is never invalidated by a checkpoint
    pipe.run()
    assert set(pipe.llm.texts) >= {"describe", "narrative", "rough_cut"}


def test_abort_at_roughcut_emits_run_failed_and_raises(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    seen = []

    def handler(stage, summary):
        return events.Abort() if stage == "roughcut" else events.Continue()

    with pytest.raises(events.RunAborted):
        Pipeline(cfg, on_event=seen.append, on_checkpoint=handler).run()
    failed = seen[-1]
    assert isinstance(failed, events.RunFailed) and failed.stage == "roughcut" and failed.error == "aborted"
    assert not (tmp_path / "ws" / "final.json").exists()
    assert (tmp_path / "ws" / "roughcut.json").exists()  # the stage ran; the run stopped after it


def test_cancel_during_run_stops_at_the_next_boundary(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cancel = threading.Event()
    seen = []

    def on_event(e):
        seen.append(e)
        if isinstance(e, events.StageFinished) and e.stage == "narrative":
            cancel.set()

    with pytest.raises(events.RunAborted):
        Pipeline(cfg, on_event=on_event, cancel=cancel).run()
    assert isinstance(seen[-1], events.RunFailed) and seen[-1].error == "aborted" and seen[-1].stage == "roughcut"
    assert not (tmp_path / "ws" / "roughcut.json").exists()


def test_cli_style_run_without_handlers_is_unchanged(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    final = Pipeline(cfg).run()
    assert final.clips and (tmp_path / "ws" / "report.md").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/pipeline/test_events.py tests/pipeline/test_checkpoints.py -q`
Expected: the new tests FAIL with `TypeError: __init__() got an unexpected keyword argument 'on_event'`; the earlier ones pass.

- [ ] **Step 3: Rewrite the `Pipeline` class**

Replace everything in `matchcut/pipeline/pipeline.py` from `log = logging.getLogger("matchcut.pipeline")` to the end of the file with the code below (the module docstring and existing imports stay; add the new imports shown):

```python
import threading
import time
import traceback as _traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import events
from .events import Abort, CheckpointAnswer, CheckpointHandler, Continue, EventHandler, Redo, RunAborted
from .passes import PassResult, run_passes

log = logging.getLogger("matchcut.pipeline")

# order matters: invalidating a stage also invalidates everything after it
CHECKPOINT_STAGES: Tuple[str, ...] = ("describe", "narrative", "roughcut", "passes")


class _EventLogHandler(logging.Handler):
    """Turns records of the pipeline logger into Log events while a run is active."""

    def __init__(self, emit: Callable[[events.Event], None]):
        super().__init__(level=logging.INFO)
        self._emit = emit

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401 (logging API name)
        try:
            self._emit(events.Log(level=record.levelname.lower(), message=record.getMessage()))
        except Exception:  # never let a UI callback break logging
            pass


class Pipeline:
    def __init__(
        self,
        cfg: PipelineConfig,
        on_event: Optional[EventHandler] = None,
        on_checkpoint: Optional[CheckpointHandler] = None,
        cancel: Optional[threading.Event] = None,
    ):
        if not cfg.source:
            raise ValueError("config.source is required")
        self.cfg = cfg
        self.ws = cfg.workspace_path
        self.ws.mkdir(parents=True, exist_ok=True)
        cfg.save(self.ws / "config.json")
        self.on_event = on_event
        self.on_checkpoint = on_checkpoint
        self.cancel = cancel
        self.llm = make_backend(cfg.llm, self.ws / "llm_calls.jsonl")
        self.llm.on_call = self._emit
        self._ctx: Optional[Context] = None
        self._current_stage = ""

    # ------------------------------------------------------------ events
    def _emit(self, event: events.Event) -> None:
        if self.on_event is not None:
            self.on_event(event)

    def _check_cancel(self, stage: str) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise RunAborted(stage)

    def _stage(self, stage: str, cache_paths: List[Path], fn: Callable[[], Any], result_path: Optional[Path] = None) -> Any:
        """Run ``fn`` as a named stage, emitting StageCached or StageStarted/StageFinished."""
        self._check_cancel(stage)
        self._current_stage = stage
        if cache_paths and self._fresh(*cache_paths):
            result = fn()
            self._emit(events.StageCached(stage=stage, path=str(cache_paths[0])))
            return result
        self._emit(events.StageStarted(stage=stage))
        started = time.time()
        result = fn()
        path = result_path or (cache_paths[0] if cache_paths else None)
        self._emit(events.StageFinished(stage=stage, seconds=round(time.time() - started, 3), path=str(path) if path else ""))
        return result

    def _ask(self, stage: str, summary: dict) -> CheckpointAnswer:
        self._emit(events.Checkpoint(stage=stage, summary=summary))
        if self.on_checkpoint is None:
            return Continue()
        return self.on_checkpoint(stage, summary)

    # ------------------------------------------------------------ helpers
    def _fresh(self, *paths: Path) -> bool:
        return not self.cfg.force and all(p.exists() for p in paths)

    def _json(self, path: Path, data) -> None:
        path.write_text(json.dumps(data, indent=2))

    # ------------------------------------------------------------ summaries / invalidation
    def summary_for(self, stage: str, result: Any) -> dict:
        """Small dict describing a stage result, for checkpoint handlers and UIs."""
        if stage == "describe":
            descs = result.descriptions
            return {"shots": len(descs), "unusable": sum(1 for d in descs.values() if not d.get("usable", True))}
        if stage == "narrative":
            n = result.narrative
            return {
                "logline": n.get("logline", ""),
                "beats": [{"id": b["id"], "title": b.get("title", ""), "pace": b.get("pace", ""), "target_duration_s": b.get("target_duration_s", 0)} for b in n.get("beats", [])],
            }
        if stage == "roughcut":
            tl = result
            per_beat: Dict[str, float] = {}
            for c in tl.clips:
                per_beat[c.beat_id] = round(per_beat.get(c.beat_id, 0.0) + c.duration, 2)
            return {"clips": len(tl.clips), "duration": round(tl.duration, 2), "beats": per_beat}
        if stage.startswith("pass:"):
            pr: PassResult = result
            counts: Dict[str, int] = {}
            for d in pr.decisions:
                counts[d.action] = counts.get(d.action, 0) + 1
            return {"pass": stage.split(":", 1)[1], "decisions": counts, "clips": len(pr.timeline.clips), "duration": round(pr.timeline.duration, 2)}
        return {}

    def invalidate_from(self, stage: str) -> None:
        """Delete ``stage``'s cached output and everything downstream, and clear matching context state."""
        key = "passes" if stage.startswith("pass:") else stage
        if key not in CHECKPOINT_STAGES:
            raise ValueError(f"cannot invalidate stage {stage!r}")
        start = CHECKPOINT_STAGES.index(key)
        for s in CHECKPOINT_STAGES[start:]:
            if s == "describe":
                (self.ws / "descriptions.json").unlink(missing_ok=True)
                if self._ctx is not None:
                    self._ctx.descriptions = {}
            elif s == "narrative":
                (self.ws / "narrative.json").unlink(missing_ok=True)
                if self._ctx is not None:
                    self._ctx.narrative = {}
            elif s == "roughcut":
                (self.ws / "roughcut.json").unlink(missing_ok=True)
            elif s == "passes":
                for p in (self.ws / "passes").glob("*.json") if (self.ws / "passes").exists() else []:
                    p.unlink()
                for name in ("final.json", "final.edl", "final.csv", "final.mp4", "final_proxy.mp4", "report.md"):
                    (self.ws / name).unlink(missing_ok=True)

    def _checkpointed(self, stage: str, produce: Callable[[], Any]) -> Any:
        """Run a stage, ask the checkpoint handler, and redo on request."""
        while True:
            result = produce()
            answer = self._ask(stage, self.summary_for(stage, result))
            if isinstance(answer, Abort):
                raise RunAborted(stage)
            if isinstance(answer, Redo):
                if answer.note:
                    ctx = self.ingest()
                    ctx.notes.setdefault(stage, []).append(answer.note)
                self.invalidate_from(stage)
                continue
            return result

    # ------------------------------------------------------------ music
    def music(self) -> Optional[MusicAnalysis]:
        """Analyse the music track once (cached in music.json). None when no track is configured."""
        mcfg = self.cfg.music
        if not mcfg.path:
            return None
        path = self.ws / "music.json"
        if self._fresh(path):
            cached = MusicAnalysis.load(path)
            if cached.path == str(mcfg.path):
                log.info("music: cached (%.0f bpm)", cached.tempo_bpm)
                return cached
        log.info("music: analysing %s", mcfg.path)
        analysis = analyse_music(str(mcfg.path), mcfg)
        analysis.save(path)
        log.info("music: %.0f bpm, %d beats, %.1fs", analysis.tempo_bpm, len(analysis.beats), analysis.duration)
        return analysis

    # ------------------------------------------------------------ stage 1
    def ingest(self) -> Context:
        """Probe, decode, detect shots, compute features (cached)."""
        if self._ctx is not None:
            return self._ctx
        probe_path, analysis_path, shots_path = self.ws / "probe.json", self.ws / "analysis.npz", self.ws / "shots.json"

        def _do() -> Context:
            if self._fresh(probe_path, analysis_path, shots_path):
                media = MediaInfo.from_dict(json.loads(probe_path.read_text()))
                analysis = Analysis.load(analysis_path)
                shots = load_shots(shots_path)
                log.info("ingest: cached (%d shots)", len(shots))
            else:
                media = probe(self.cfg.source)
                self._json(probe_path, media.to_dict())
                log.info("ingest: analysing %s (%.1fs)", media.path, media.duration)
                series, cells = analyse_footage(media.path, self.cfg.analysis)
                energy = (
                    audio_energy(media.path, self.cfg.analysis.audio_sample_rate, self.cfg.analysis.audio_hop_seconds)
                    if media.has_audio
                    else np.zeros(0, dtype=np.float32)
                )
                analysis = Analysis(series=series, cells=cells, audio_energy=energy, audio_hop_seconds=self.cfg.analysis.audio_hop_seconds)
                analysis.save(analysis_path)
                shots = detect_shots(series, self.cfg.shots, duration=media.duration)
                save_shots(shots, shots_path)
                log.info("ingest: %d shots detected", len(shots))
            frames = FrameCache(media.path, self.ws / "frames", self.cfg.sampling)
            ctx = Context(cfg=self.cfg, workspace=self.ws, media=media, analysis=analysis, shots=shots, frames=frames, llm=self.llm)
            ctx.music = self.music()
            return ctx

        self._ctx = self._stage("ingest", [probe_path, analysis_path, shots_path], _do)
        return self._ctx

    # ------------------------------------------------------------ stage 2
    def describe(self) -> Context:
        ctx = self.ingest()
        if ctx.descriptions:
            return ctx
        path = self.ws / "descriptions.json"

        def _do() -> Context:
            if self._fresh(path):
                data = json.loads(path.read_text())
                ctx.descriptions = {int(k): v for k, v in data.items()}
                log.info("describe: cached")
            else:
                log.info("describe: %d shots", len(ctx.shots))
                ctx.descriptions = stages.describe_shots(ctx)
                self._json(path, {str(k): v for k, v in ctx.descriptions.items()})
            return ctx

        return self._stage("describe", [path], _do)

    # ------------------------------------------------------------ stage 3
    def narrative(self) -> Context:
        ctx = self.describe()
        if ctx.narrative:
            return ctx
        path = self.ws / "narrative.json"

        def _do() -> Context:
            if self._fresh(path):
                ctx.narrative = json.loads(path.read_text())
                log.info("narrative: cached")
            else:
                log.info("narrative: finding the story")
                ctx.narrative = stages.find_narrative(ctx)
                self._json(path, ctx.narrative)
            return ctx

        return self._stage("narrative", [path], _do)

    # ------------------------------------------------------------ stage 4
    def rough_cut(self) -> Timeline:
        ctx = self.narrative()
        path = self.ws / "roughcut.json"

        def _do() -> Timeline:
            if self._fresh(path):
                log.info("rough cut: cached")
                return Timeline.load(path)
            log.info("rough cut: assembling")
            tl = stages.rough_cut(ctx)
            tl.save(path)
            return tl

        return self._stage("roughcut", [path], _do)

    # ------------------------------------------------------------ stage 5
    def refine(self, timeline: Optional[Timeline] = None, passes: Optional[List[str]] = None) -> Timeline:
        ctx = self.narrative()
        tl = timeline or self.rough_cut()
        names = list(passes or self.cfg.passes)
        passes_dir = self.ws / "passes"
        passes_dir.mkdir(exist_ok=True)
        self._current_stage = "refine"

        def after_pass(name: str, result: PassResult) -> CheckpointAnswer:
            return self._ask(f"pass:{name}", self.summary_for(f"pass:{name}", result))

        final = run_passes(tl, ctx, names, save_dir=passes_dir, on_event=self._emit, after_pass=after_pass, cancel=self.cancel)
        final = snap_to_frames(final, ctx.shot_bounds)
        final.name = "final cut"
        final.stage = "final"
        final.save(self.ws / "final.json")
        (self.ws / "final.edl").write_text(final.to_edl(title=Path(ctx.media.path).stem))
        (self.ws / "final.csv").write_text(final.to_csv())
        return final

    # ------------------------------------------------------------ stage 6
    def render(self, timeline: Optional[Timeline] = None, output: Optional[Path] = None) -> Path:
        ctx = self.ingest()
        tl = timeline or Timeline.load(self.ws / "final.json")
        out = output or self.ws / ("final_proxy.mp4" if self.cfg.render.proxy else "final.mp4")

        def _do() -> Path:
            music = self.cfg.music if self.cfg.music.path else None
            music_duration = None
            if music is not None and ctx.music is not None:
                music_duration = ctx.music.duration
                if music.start >= music_duration:
                    raise ValueError(f"music.start ({music.start:.1f}s) is past the end of the track ({music_duration:.1f}s)")
            return render(tl, ctx.media, self.cfg.render, out, music=music, music_duration=music_duration)

        return self._stage("render", [], _do, result_path=out)

    # ------------------------------------------------------------ report
    def report(self, final: Optional[Timeline] = None) -> str:
        ctx = self.narrative()
        final = final or Timeline.load(self.ws / "final.json")
        out = self.ws / "report.md"

        def _do() -> str:
            rough = Timeline.load(self.ws / "roughcut.json") if (self.ws / "roughcut.json").exists() else None
            passes: Dict[str, Timeline] = {}
            for p in sorted((self.ws / "passes").glob("*.json")) if (self.ws / "passes").exists() else []:
                passes[p.stem.split("_", 1)[1]] = Timeline.load(p)
            text = build_report(ctx, final, rough, passes)
            out.write_text(text)
            return text

        return self._stage("report", [], _do, result_path=out)

    # ------------------------------------------------------------ all
    def run(self) -> Timeline:
        """Run every stage with checkpoints after describe, narrative, roughcut and each pass."""
        handler = _EventLogHandler(self._emit)
        log.addHandler(handler)
        previous_level = log.level
        if log.getEffectiveLevel() > logging.INFO:
            log.setLevel(logging.INFO)  # without basicConfig the effective level is WARNING and no Log events would fire
        started = time.time()
        self._emit(events.RunStarted(source=self.cfg.source, workspace=str(self.ws), config=self.cfg.to_dict()))
        try:
            self._checkpointed("describe", self.describe)
            self._checkpointed("narrative", self.narrative)
            rough = self._checkpointed("roughcut", self.rough_cut)
            final = self.refine(rough)
            render_path = ""
            if self.cfg.render.enabled:
                render_path = str(self.render(final))
            self.report(final)
            self._emit(
                events.RunFinished(
                    final_path=str(self.ws / "final.json"),
                    render_path=render_path,
                    report_path=str(self.ws / "report.md"),
                    seconds=round(time.time() - started, 3),
                )
            )
            return final
        except RunAborted as e:
            self._emit(events.RunFailed(stage=e.stage, error="aborted", traceback=""))
            raise
        except Exception as e:
            self._emit(events.RunFailed(stage=self._current_stage, error=str(e), traceback=_traceback.format_exc()))
            raise
        finally:
            log.removeHandler(handler)
            log.setLevel(previous_level)
```

Notes for the implementer:
- `RunAborted` raised by `run_passes` carries the pass stage; `_check_cancel` in `_stage` raises with the stage being entered (so a cancel set after `narrative` surfaces as `roughcut`, which `test_cancel_during_run_stops_at_the_next_boundary` expects).
- `_stage` emits `StageCached` *after* `fn()` so the context is populated either way; the cached check mirrors each stage's own `_fresh` test on the same paths.
- `refine` sets `_current_stage = "refine"` so an unknown pass name (a `ValueError` from `run_passes` before any pass starts) reports `stage == "refine"`, as `test_failure_emits_run_failed_and_reraises` expects.
- `Path.unlink(missing_ok=True)` needs Python 3.8+, which the project already requires.
- The logger's effective level is WARNING when nothing configured logging (pytest, a library caller). `run()` lifts the `matchcut.pipeline` logger to INFO while its handler is attached and restores the previous level afterwards, so `Log` events fire without changing what the CLI prints (the CLI already configures INFO).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/pipeline -q`
Expected: 64 passed (56 + 3 in `test_events.py` + 5 in `test_checkpoints.py`). If `test_run_emits_lifecycle_and_stage_events_in_order` fails on the `StageStarted` order, print `started` first: `ingest` must come from `_stage("ingest", ...)` inside `describe()`'s call to `ingest()`, so it is emitted before `describe`.

- [ ] **Step 5: Document the API**

In `docs/PIPELINE.md`, add before `## Working iteratively`:

```markdown
## Events and checkpoints

`Pipeline(cfg, on_event=None, on_checkpoint=None, cancel=None)` lets a UI
drive a run. Every callback runs on the pipeline's thread.

* **Events** (`matchcut.pipeline.events`): `RunStarted`, `StageStarted`,
  `StageCached`, `StageFinished`, `Decision` (one per pass decision),
  `ModelCall` (one per Claude call, with tokens), `Log` (the pipeline's log
  lines), `Checkpoint`, `RunFinished`, `RunFailed`. Each has `to_dict()`.
  Stage names: `ingest`, `describe`, `narrative`, `roughcut`, `pass:<name>`,
  `render`, `report`.
* **Checkpoints**: after `describe`, `narrative`, `roughcut` and every pass,
  `on_checkpoint(stage, summary)` is called and the run waits for its answer:
  `Continue()`, `Redo(note)` or `Abort()`. `Redo` deletes that stage's cached
  output and everything downstream (`Pipeline.invalidate_from(stage)`),
  appends the note to the stage's director's notes and re-runs it. Notes are
  rendered into the describe, narrative, rough-cut and polish prompts; the
  deterministic passes record the note in the timeline history and simply
  re-run. `Abort` raises `RunAborted` after emitting `RunFailed(error="aborted")`.
* **Cancel**: a `threading.Event`; the run stops at the next stage or pass
  boundary with `RunAborted`.

Without handlers (the CLI) every checkpoint continues and nothing changes.
```

- [ ] **Step 6: Run the full suite once more and the CLI by hand**

Run: `python3 -m pytest tests/pipeline -q` (64 passed) and
```bash
S=/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad
python3 -m matchcut.pipeline run $S/demo.mp4 -w $S/ws_events --backend mock -t 10 --no-render --force | tail -2
```
Expected: the CLI run completes exactly as before (`final cut: ... clips`), with no new output.

- [ ] **Step 7: Commit**

Message:
```
Add run lifecycle events, stage checkpoints with redo, and a cancel flag

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ
```
Stage `matchcut/pipeline/pipeline.py docs/PIPELINE.md tests/pipeline/test_events.py tests/pipeline/test_checkpoints.py`. Push.

---

## Self-review

**Spec coverage.** Event types and fields: Task 1 (`events.py`) matches the spec's table, with `Decision` from `run_passes` (Task 3) and `ModelCall` from the backends (Task 2); `StageCached` for cached stages, `Log` via the logging handler, `RunStarted/RunFinished/RunFailed` in `run()` (Task 4). Checkpoints after describe, narrative, roughcut and each pass with the three answers, invalidation of downstream caches, notes on `Context.notes` and in the four prompts, note decisions for deterministic passes: Tasks 1, 3, 4. Cancel flag checked between stages and passes: Tasks 3 and 4. CLI unchanged: default handlers in Task 4, verified by `test_cli_style_run_without_handlers_is_unchanged` and the manual run. Summaries per stage: `summary_for` in Task 4. The spec's `Pipeline.summary_for` and `invalidate_from` are public for the TUI plan.

**Type consistency.** `events.Decision` field names match `timeline.Decision` (`pass_name, action, clip_ids, detail, score, data`). `run_passes` keyword names (`on_event`, `after_pass`, `cancel`) are identical in Task 3's definition, its tests, and Task 4's call. `CheckpointAnswer` variants are used with `isinstance` everywhere. Stage keys for notes (`"describe"`, `"narrative"`, `"roughcut"`, `"pass:polish"`, `"pass:<name>"`) match between `notes_text` call sites (Task 1), `run_passes` (Task 3) and `_checkpointed` (Task 4).

**Known judgement calls.** `StageCached` is emitted after the stage function runs (so the context is populated in both paths); the cost is nil since cached stages only read files. `invalidate_from("pass:x")` clears all pass outputs rather than just pass x's, because later passes depend on earlier ones; a redo of pass x re-runs x only (in `run_passes`) and the later passes run fresh afterwards anyway. Test counts: 47 today, +3 (T1), +1 (T2), +5 (T3), +8 (T4) = 64.
