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
