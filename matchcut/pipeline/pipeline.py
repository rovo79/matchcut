"""Orchestrator: runs the stages and passes, caching results in the workspace.

    workspace/
      config.json          the configuration used
      probe.json           ffprobe summary
      analysis.npz         per-frame series + per-cell features
      shots.json           detected shots
      frames/              cached keyframes
      sheets/              contact sheets sent to the model
      descriptions.json    model's per-shot descriptions
      music.json           music analysis (only when a track is given)
      narrative.json       beats / arc
      roughcut.json        first assembly
      passes/NN_name.json  timeline after each pass
      final.json/.edl/.csv the polished cut
      final.mp4            render (optional)
      report.md            human-readable summary
      llm_calls.jsonl      every model call
"""
from __future__ import annotations

import json
import logging
import threading
import time
import traceback as _traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from . import events, stages
from .audio import MusicAnalysis, analyse_music
from .config import PipelineConfig
from .context import Context
from .events import Abort, CheckpointAnswer, CheckpointHandler, Continue, EventHandler, Redo, RunAborted
from .features import Analysis, analyse_footage
from .llm import make_backend
from .media import MediaInfo, audio_energy, probe
from .passes import PASSES, PassResult, run_passes
from .render import render, snap_to_frames
from .report import build_report
from .sampling import FrameCache
from .shots import detect_shots, load_shots, save_shots
from .timeline import Timeline

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
        if isinstance(event, events.StageStarted):
            self._current_stage = event.stage
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
                    self._emit(events.Decision(pass_name=stage, action="note", clip_ids=[], detail=answer.note, score=None, data={}))
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

        def after_pass(name: str, result: PassResult) -> CheckpointAnswer:
            return self._ask(f"pass:{name}", self.summary_for(f"pass:{name}", result))

        unknown = [n for n in names if n not in PASSES]
        if unknown:
            self._current_stage = f"pass:{unknown[0]}"
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
        handler: Optional[_EventLogHandler] = None
        previous_level = log.level
        started = time.time()
        try:
            if self.on_event is not None:
                handler = _EventLogHandler(self._emit)
                log.addHandler(handler)
                if log.getEffectiveLevel() > logging.INFO:
                    log.setLevel(logging.INFO)  # without basicConfig the effective level is WARNING and no Log events would fire
            self._emit(events.RunStarted(source=self.cfg.source, workspace=str(self.ws), config=self.cfg.to_dict()))
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
            if handler is not None:
                log.removeHandler(handler)
            log.setLevel(previous_level)
