"""Orchestrator: runs the stages and passes, caching results in the workspace.

    workspace/
      config.json          the configuration used
      probe.json           ffprobe summary
      analysis.npz         per-frame series + per-cell features
      shots.json           detected shots
      frames/              cached keyframes
      sheets/              contact sheets sent to the model
      descriptions.json    model's per-shot descriptions
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
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from . import stages
from .config import PipelineConfig
from .context import Context
from .features import Analysis, analyse_footage
from .llm import make_backend
from .media import MediaInfo, audio_energy, probe
from .passes import run_passes
from .render import render, snap_to_frames
from .report import build_report
from .sampling import FrameCache
from .shots import detect_shots, load_shots, save_shots
from .timeline import Timeline

log = logging.getLogger("matchcut.pipeline")


class Pipeline:
    def __init__(self, cfg: PipelineConfig):
        if not cfg.source:
            raise ValueError("config.source is required")
        self.cfg = cfg
        self.ws = cfg.workspace_path
        self.ws.mkdir(parents=True, exist_ok=True)
        cfg.save(self.ws / "config.json")
        self.llm = make_backend(cfg.llm, self.ws / "llm_calls.jsonl")
        self._ctx: Optional[Context] = None

    # ------------------------------------------------------------ helpers
    def _fresh(self, *paths: Path) -> bool:
        return not self.cfg.force and all(p.exists() for p in paths)

    def _json(self, path: Path, data) -> None:
        path.write_text(json.dumps(data, indent=2))

    # ------------------------------------------------------------ stage 1
    def ingest(self) -> Context:
        """Probe, decode, detect shots, compute features (cached)."""
        if self._ctx is not None:
            return self._ctx
        probe_path, analysis_path, shots_path = self.ws / "probe.json", self.ws / "analysis.npz", self.ws / "shots.json"
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
            analysis = Analysis(
                series=series,
                cells=cells,
                audio_energy=energy,
                audio_hop_seconds=self.cfg.analysis.audio_hop_seconds,
            )
            analysis.save(analysis_path)
            shots = detect_shots(series, self.cfg.shots, duration=media.duration)
            save_shots(shots, shots_path)
            log.info("ingest: %d shots detected", len(shots))
        frames = FrameCache(media.path, self.ws / "frames", self.cfg.sampling)
        self._ctx = Context(cfg=self.cfg, workspace=self.ws, media=media, analysis=analysis, shots=shots, frames=frames, llm=self.llm)
        return self._ctx

    # ------------------------------------------------------------ stage 2
    def describe(self) -> Context:
        ctx = self.ingest()
        if ctx.descriptions:
            return ctx
        path = self.ws / "descriptions.json"
        if self._fresh(path):
            data = json.loads(path.read_text())
            ctx.descriptions = {int(k): v for k, v in data.items()}
            log.info("describe: cached")
        else:
            log.info("describe: %d shots", len(ctx.shots))
            ctx.descriptions = stages.describe_shots(ctx)
            self._json(path, {str(k): v for k, v in ctx.descriptions.items()})
        return ctx

    # ------------------------------------------------------------ stage 3
    def narrative(self) -> Context:
        ctx = self.describe()
        if ctx.narrative:
            return ctx
        path = self.ws / "narrative.json"
        if self._fresh(path):
            ctx.narrative = json.loads(path.read_text())
            log.info("narrative: cached")
        else:
            log.info("narrative: finding the story")
            ctx.narrative = stages.find_narrative(ctx)
            self._json(path, ctx.narrative)
        return ctx

    # ------------------------------------------------------------ stage 4
    def rough_cut(self) -> Timeline:
        ctx = self.narrative()
        path = self.ws / "roughcut.json"
        if self._fresh(path):
            log.info("rough cut: cached")
            return Timeline.load(path)
        log.info("rough cut: assembling")
        tl = stages.rough_cut(ctx)
        tl.save(path)
        return tl

    # ------------------------------------------------------------ stage 5
    def refine(self, timeline: Optional[Timeline] = None, passes: Optional[List[str]] = None) -> Timeline:
        ctx = self.narrative()
        tl = timeline or self.rough_cut()
        names = list(passes or self.cfg.passes)
        passes_dir = self.ws / "passes"
        passes_dir.mkdir(exist_ok=True)
        final = run_passes(tl, ctx, names, save_dir=passes_dir)
        final = snap_to_frames(final)
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
        return render(tl, ctx.media, self.cfg.render, out)

    # ------------------------------------------------------------ report
    def report(self, final: Optional[Timeline] = None) -> str:
        ctx = self.narrative()
        final = final or Timeline.load(self.ws / "final.json")
        rough = Timeline.load(self.ws / "roughcut.json") if (self.ws / "roughcut.json").exists() else None
        passes: Dict[str, Timeline] = {}
        for p in sorted((self.ws / "passes").glob("*.json")) if (self.ws / "passes").exists() else []:
            passes[p.stem.split("_", 1)[1]] = Timeline.load(p)
        text = build_report(ctx, final, rough, passes)
        (self.ws / "report.md").write_text(text)
        return text

    # ------------------------------------------------------------ all
    def run(self) -> Timeline:
        rough = self.rough_cut()
        final = self.refine(rough)
        if self.cfg.render.enabled:
            self.render(final)
        self.report(final)
        return final
