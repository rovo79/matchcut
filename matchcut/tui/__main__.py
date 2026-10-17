"""Launcher: parse arguments, build the config, guard the optional dependencies, run the app."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from matchcut.pipeline.config import PipelineConfig

INSTALL_HINT = "The TUI needs textual and textual-image: pip install -r requirements-tui.txt"


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="matchcut.tui", description="Control room for the editing pipeline")
    p.add_argument("source", nargs="?", default=None, help="footage to edit (omit to open an existing --workspace read-only)")
    p.add_argument("-w", "--workspace", default=None, help="working directory (default: <source>_edit/)")
    p.add_argument("-b", "--brief", default="", help="creative direction for the model")
    p.add_argument("-t", "--target-duration", type=float, default=None)
    p.add_argument("--backend", choices=["anthropic", "mock"], default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--music", default=None)
    p.add_argument("--music-start", type=float, default=None)
    p.add_argument("--config", default=None, help="JSON config file to start from")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    return p.parse_args(argv)


def build_config(args: argparse.Namespace) -> PipelineConfig:
    if args.config:
        cfg = PipelineConfig.load(Path(args.config))
    elif args.workspace and (Path(args.workspace) / "config.json").exists():
        cfg = PipelineConfig.load(Path(args.workspace) / "config.json")
    else:
        cfg = PipelineConfig()
    if args.source:
        cfg.source = args.source
    if not cfg.source:
        raise SystemExit("give a footage file, or a --workspace that already holds a config.json")
    cfg.workspace = args.workspace or cfg.workspace or f"{Path(cfg.source).with_suffix('')}_edit"
    if args.brief:
        cfg.brief = args.brief
    if args.target_duration is not None:
        cfg.target_duration = args.target_duration
    if args.backend:
        cfg.llm.backend = args.backend
    if args.model:
        cfg.llm.model = args.model
    if args.music:
        cfg.music.path = args.music
    if args.music_start is not None:
        cfg.music.start = args.music_start
    cfg.apply_overrides(args.set)
    return cfg


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    cfg = build_config(args)
    # A terminal that never answers the probe would make textual-image log a traceback to stderr.
    logging.getLogger("textual_image").setLevel(logging.ERROR)
    try:
        import textual_image.widget  # noqa: F401  probes the terminal; must run before App.run()
        from .app import MatchcutApp
    except ModuleNotFoundError as e:  # textual / textual-image missing
        missing = e.name or ""
        if not any(missing == pkg or missing.startswith(pkg + ".") for pkg in ("textual", "textual_image")):
            raise  # a real bug elsewhere: show the traceback
        print(f"{INSTALL_HINT}\n({e})", file=sys.stderr)
        return 1
    app = MatchcutApp(cfg)
    app.run()
    return app.return_code or 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
