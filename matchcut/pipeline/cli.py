"""Command-line entry point: ``python -m matchcut.pipeline``."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .config import DEFAULT_PASSES, PipelineConfig
from .junctions import build_junctions, junction_detail, junction_line, load_stage_timelines, scorer_for_workspace
from .pipeline import Pipeline
from .timeline import Timeline


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("source", help="path to the footage (any format ffmpeg reads)")
    p.add_argument("-w", "--workspace", default=None, help="working directory (default: <source>_edit/)")
    p.add_argument("-b", "--brief", default="", help="creative direction for the model")
    p.add_argument("-t", "--target-duration", type=float, default=None, help="target running time in seconds")
    p.add_argument("--backend", choices=["anthropic", "mock"], default=None, help="model backend (default: anthropic)")
    p.add_argument("--model", default=None, help="Claude model id (default: claude-opus-5-5)")
    p.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--passes", default=None, help=f"comma-separated pass list (default: {','.join(DEFAULT_PASSES)})")
    p.add_argument("--force", action="store_true", help="ignore cached stage outputs")
    p.add_argument("--no-render", action="store_true", help="skip the ffmpeg render")
    p.add_argument("--proxy", action="store_true", help="render a quick low-res preview")
    p.add_argument("--music", default=None, help="music track to play under the cut (mp3/wav/m4a...)")
    p.add_argument("--music-start", type=float, default=None, help="seconds into the track where the cut begins")
    p.add_argument("--music-gain", type=float, default=None, help="music level in dB relative to the source audio (default -8)")
    p.add_argument("--config", default=None, help="JSON config file to start from")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override any config value, e.g. matchcut.candidate_threshold=0.6")
    p.add_argument("-v", "--verbose", action="store_true")


def build_config(args: argparse.Namespace) -> PipelineConfig:
    cfg = PipelineConfig.load(Path(args.config)) if args.config else PipelineConfig()
    cfg.source = args.source
    cfg.workspace = args.workspace or f"{Path(args.source).with_suffix('')}_edit"
    if args.brief:
        cfg.brief = args.brief
    if args.target_duration is not None:
        cfg.target_duration = args.target_duration
    if args.backend:
        cfg.llm.backend = args.backend
    if args.model:
        cfg.llm.model = args.model
    if args.effort:
        cfg.llm.effort = args.effort
    if args.passes:
        cfg.passes = tuple(p.strip() for p in args.passes.split(",") if p.strip())
    cfg.force = args.force
    cfg.render.enabled = not args.no_render
    cfg.render.proxy = args.proxy
    if args.music:
        cfg.music.path = args.music
    if args.music_start is not None:
        cfg.music.start = args.music_start
    if args.music_gain is not None:
        cfg.music.gain_db = args.music_gain
    cfg.apply_overrides(args.set)
    return cfg


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="matchcut.pipeline", description="AI editing pipeline: sample -> narrative -> rough cut -> match-cut passes -> final cut")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [
        ("run", "run every stage end to end"),
        ("analyze", "probe, detect shots, compute features and describe shots"),
        ("narrative", "find the visual narrative (beats)"),
        ("roughcut", "assemble the rough cut"),
        ("refine", "run the refinement passes on the rough cut"),
        ("render", "render final.json with ffmpeg"),
        ("report", "write report.md from the workspace"),
    ]:
        sp = sub.add_parser(name, help=help_text)
        _common(sp)
        if name == "refine":
            sp.add_argument("--from", dest="from_timeline", default=None, help="timeline JSON to refine instead of roughcut.json")
        if name == "render":
            sp.add_argument("--timeline", default=None, help="timeline JSON to render (default: final.json)")
            sp.add_argument("-o", "--output", default=None)
    sp = sub.add_parser("junctions", help="list a workspace's junctions, or explain one with --index")
    sp.add_argument("workspace")
    sp.add_argument("--index", type=int, default=None, help="1-based junction number to explain")
    return parser.parse_args(argv)


def _junctions(ws: Path, index: Optional[int]) -> int:
    cfg_path = ws / "config.json"
    if not cfg_path.exists():
        print(f"no workspace at {ws} (config.json missing)", file=sys.stderr)
        return 1
    try:
        cfg = PipelineConfig.load(cfg_path)
    except Exception as exc:  # corrupt or hand-edited JSON
        print(f"cannot read {cfg_path}: {exc}", file=sys.stderr)
        return 1
    stages = load_stage_timelines(ws)
    if not stages:
        print(f"no timelines in {ws}: run the pipeline first", file=sys.stderr)
        return 1
    junctions = build_junctions(stages, scorer_for_workspace(ws, cfg))
    if index is None:
        print(f"{len(junctions)} junctions in {stages[-1][0]} ({len(stages)} stages)")
        for j in junctions:
            print(junction_line(j))
        return 0
    if not 1 <= index <= len(junctions):
        print(f"junction {index} is out of range 1..{len(junctions)}", file=sys.stderr)
        return 1
    print(junction_detail(junctions[index - 1]))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "junctions":
        return _junctions(Path(args.workspace), args.index)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("PIL").setLevel(logging.WARNING)
    cfg = build_config(args)
    pipe = Pipeline(cfg)

    if args.command == "run":
        final = pipe.run()
        print(f"final cut: {len(final.clips)} clips, {final.duration:.1f}s -> {pipe.ws}")
    elif args.command == "analyze":
        ctx = pipe.describe()
        print(f"{len(ctx.shots)} shots described -> {pipe.ws / 'descriptions.json'}")
    elif args.command == "narrative":
        ctx = pipe.narrative()
        print(f"{len(ctx.narrative['beats'])} beats -> {pipe.ws / 'narrative.json'}")
    elif args.command == "roughcut":
        tl = pipe.rough_cut()
        print(f"rough cut: {len(tl.clips)} clips, {tl.duration:.1f}s -> {pipe.ws / 'roughcut.json'}")
    elif args.command == "refine":
        start = Timeline.load(Path(args.from_timeline)) if args.from_timeline else None
        final = pipe.refine(start)
        pipe.report(final)
        print(f"final cut: {len(final.clips)} clips, {final.duration:.1f}s -> {pipe.ws / 'final.json'}")
    elif args.command == "render":
        tl = Timeline.load(Path(args.timeline)) if args.timeline else None
        out = pipe.render(tl, Path(args.output) if args.output else None)
        print(f"rendered -> {out}")
    elif args.command == "report":
        print(pipe.report())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
