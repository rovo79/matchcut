"""UI-side model of a run and of the workspace it reads from."""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from matchcut.pipeline import events
from matchcut.pipeline.audio import MusicAnalysis
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.media import MediaInfo
from matchcut.pipeline.shots import Shot, load_shots
from matchcut.pipeline.timeline import Decision, Timeline

STAGE_STATES = ("pending", "cached", "running", "done", "waiting", "failed", "stopping")

# USD per million tokens (input, output); an estimate shown as "~$" in the status bar
PRICES: Dict[str, Tuple[float, float]] = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-1": (10.0, 50.0),
}


def file_fingerprint(path: Path) -> Optional[Tuple[int, int]]:
    """(mtime_ns, size) of a file, None when it is missing: a cheap identity of what a view last rendered."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


@dataclass
class StageStatus:
    name: str
    state: str = "pending"
    seconds: float = 0.0
    path: str = ""
    error: str = ""


@dataclass
class PendingCheckpoint:
    stage: str
    summary: dict


class RunState:
    def __init__(self, cfg: PipelineConfig):
        self.config = cfg
        self.workspace = Path(cfg.workspace)
        self.stages: Dict[str, StageStatus] = {n: StageStatus(n) for n in self.stage_names()}
        self.events: Deque[events.Event] = deque(maxlen=5000)
        self.event_count = 0  # events applied in this run; keeps counting once the deque is full
        self.run_serial = 0  # incremented on every RunStarted: views clear per-run state when it changes
        self.checkpoint: Optional[PendingCheckpoint] = None
        self.run_status = "idle"
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self._cost = 0.0  # accumulated per call, at the price of the model that served it
        self.model = cfg.llm.model
        self.last_error = ""
        self.image_mode = ""
        self._cache: Dict[str, Tuple[float, Any]] = {}  # path -> (mtime, parsed)

    # -- stages ----------------------------------------------------------
    def stage_names(self) -> List[str]:
        return ["ingest", "describe", "narrative", "roughcut"] + [f"pass:{p}" for p in self.config.passes] + ["render", "report"]

    def _stage(self, name: str) -> StageStatus:
        if name not in self.stages:
            self.stages[name] = StageStatus(name)
        return self.stages[name]

    # -- events ----------------------------------------------------------
    def apply(self, event: events.Event) -> None:
        if isinstance(event, events.RunStarted):
            self.event_count = 0
            self.run_serial += 1
        self.events.append(event)
        self.event_count += 1
        if isinstance(event, events.RunStarted):
            self.stages = {n: StageStatus(n) for n in self.stage_names()}
            self.checkpoint = None
            self.run_status = "running"
            self.started_at = time.time()
            self.finished_at = None
            self.calls = self.input_tokens = self.output_tokens = 0
            self._cost = 0.0
            self.last_error = ""
        elif isinstance(event, events.StageStarted):
            self._stage(event.stage).state = "running"
            if self.run_status != "stopping":
                self.run_status = "running"
        elif isinstance(event, events.StageCached):
            s = self._stage(event.stage)
            s.state, s.path = "cached", event.path
        elif isinstance(event, events.StageFinished):
            s = self._stage(event.stage)
            s.state, s.seconds, s.path = "done", event.seconds, event.path
            if event.path:
                self.invalidate(event.path)
        elif isinstance(event, events.Checkpoint):
            self._stage(event.stage).state = "waiting"
            self.checkpoint = PendingCheckpoint(event.stage, dict(event.summary))
            self.run_status = "waiting"
        elif isinstance(event, events.ModelCall):
            self.calls += 1
            self.input_tokens += event.input_tokens
            self.output_tokens += event.output_tokens
            self.model = event.model or self.model
            inp, out = PRICES.get(self.model, (0.0, 0.0))
            self._cost += event.input_tokens / 1e6 * inp + event.output_tokens / 1e6 * out
        elif isinstance(event, events.RunFinished):
            self.run_status = "finished"
            self.finished_at = time.time()
            self.checkpoint = None
        elif isinstance(event, events.RunFailed):
            self.run_status = "failed"
            self.finished_at = time.time()
            self.last_error = event.error
            self.checkpoint = None
            if event.stage:
                self._stage(event.stage).state = "failed"
                self._stage(event.stage).error = event.error

    def resolve_checkpoint(self) -> None:
        """The UI answered the pending checkpoint."""
        if self.checkpoint is not None:
            s = self._stage(self.checkpoint.stage)
            if s.state == "waiting":
                s.state = "done"
        self.checkpoint = None
        if self.run_status == "waiting":
            self.run_status = "running"

    def mark_stopping(self) -> None:
        self.run_status = "stopping"
        for s in self.stages.values():
            if s.state in ("running", "waiting"):
                s.state = "stopping"

    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def cost_estimate(self) -> float:
        return self._cost

    # -- workspace readers (cached by mtime) --------------------------------
    def invalidate(self, path: str) -> None:
        self._cache.pop(str(Path(path)), None)

    def _load(self, path: Path, loader):
        key = str(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:  # missing, or unlinked by a redo on the pipeline thread between check and use
            self._cache.pop(key, None)
            return None
        cached = self._cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            value = loader(path)
        except Exception:
            return None
        self._cache[key] = (mtime, value)
        return value

    def media(self) -> Optional[MediaInfo]:
        return self._load(self.workspace / "probe.json", lambda p: MediaInfo.from_dict(json.loads(p.read_text())))

    def shots(self) -> List[Shot]:
        return self._load(self.workspace / "shots.json", load_shots) or []

    def descriptions(self) -> Dict[int, dict]:
        data = self._load(self.workspace / "descriptions.json", lambda p: json.loads(p.read_text()))
        return {int(k): v for k, v in data.items()} if data else {}

    def narrative(self) -> dict:
        return self._load(self.workspace / "narrative.json", lambda p: json.loads(p.read_text())) or {}

    def music(self) -> Optional[MusicAnalysis]:
        if not self.config.music.path:
            return None
        return self._load(self.workspace / "music.json", MusicAnalysis.load)

    def _timeline_entries(self) -> List[Tuple[str, Path]]:
        """(stage, path) for every place a timeline can live, in stage order: the one copy of the layout."""
        ws = self.workspace
        entries = [("roughcut", ws / "roughcut.json")]
        entries += [(p.stem.split("_", 1)[-1], p) for p in sorted((ws / "passes").glob("*.json"))]
        entries.append(("final", ws / "final.json"))
        return entries

    def timeline_files(self) -> List[Path]:
        """Every file a timeline stage can come from (present or not), for change fingerprints."""
        return [path for _, path in self._timeline_entries()]

    def _timeline_path(self, stage: str) -> Optional[Path]:
        for name, path in self._timeline_entries():
            if name == stage:
                return path
        return None

    def timeline_stages(self) -> List[str]:
        return [name for name, path in self._timeline_entries() if path.exists()]

    def timeline(self, stage: str) -> Optional[Timeline]:
        path = self._timeline_path(stage)
        return self._load(path, Timeline.load) if path is not None else None

    def decisions_for(self, clip_id: str, stage: str) -> List[Decision]:
        tl = self.timeline(stage)
        if tl is None:
            return []
        return [d for d in tl.history if clip_id in d.clip_ids]
