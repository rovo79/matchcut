"""``python -m matchcut.resolve probe WS [--stage final] [--out DIR] [--run]`` and ``python -m matchcut.resolve report WS JSON``.

Both print what they did and exit 1 with a one-line message on stderr for any failure.  ``probe`` never talks to Resolve itself unless
``--run`` is given and Resolve's scripting module can be imported here; ``report`` is offline.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from . import ProbeError

NOT_LIVE = ("status: run once against a live Resolve (21.0.2.4 free, macOS, from its console); Studio, other versions and camera footage "
            "are not verified, so every run you send back counts")


def _args(argv: Optional[List[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m matchcut.resolve", description="DaVinci Resolve Studio capability probe (M2c-0)")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe", help="write the probe bundle: the stage's exports, expected.json and resolve_probe.py")
    p.add_argument("workspace")
    p.add_argument("--stage", default="final", help="the canonical stage to probe with (default: final)")
    p.add_argument("--out", default=None, help="the bundle directory (default: WORKSPACE/resolve_probe)")
    p.add_argument("--run", action="store_true", help="also run the probe here, when Resolve's scripting module can be imported")
    r = sub.add_parser("report", help="read resolve_probe.json (offline) and write probe_report.md next to it")
    r.add_argument("workspace")
    r.add_argument("json", help="a run's resolve_probe.json, a run directory, or the bundle directory (its latest run)")
    return parser.parse_args(argv)


def _config(ws: Path):
    from matchcut.pipeline.config import PipelineConfig

    path = ws / "config.json"
    if not path.exists():
        raise ProbeError(f"no workspace at {ws} (config.json missing)")
    try:
        cfg = PipelineConfig.load(path)
    except Exception as exc:  # corrupt or hand-edited JSON
        raise ProbeError(f"cannot read {path}: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}") from exc
    return cfg


def main(argv: Optional[List[str]] = None) -> int:
    args = _args(argv)
    ws = Path(args.workspace)
    try:
        cfg = _config(ws)
        if args.command == "probe":
            from .probe import RUNS, build_bundle, command, console_lines, run_script

            b = build_bundle(ws, cfg, args.stage, Path(args.out) if args.out else None)
            print(f"probe bundle for stage {args.stage}: {b.dir}")
            for f in b.files:
                print(f"  {f}")
            for n in b.notes:
                print(f"note: {n}")
            print(NOT_LIVE)
            print("run it with DaVinci Resolve Studio open (Preferences > System > General > External scripting using = Local) and an empty")
            print("project open (no timeline, no clip); try --dry-run first. It asks before it switches projects (--yes skips the question):")
            print(f"  {command(b.script)} --dry-run")
            print(f"  {command(b.script)}")
            dry, real = console_lines(b.script)
            print("or, for the free edition (or without external scripting), from Resolve's own console (Workspace > Console, Py3):")
            print("(paste each line whole, as printed: no leading spaces)")
            print("# dry run (creates nothing):")
            print(dry)
            print("# real run (this switches to a new probe project):")
            print(real)
            print(f"each run writes {b.dir / RUNS}/<UTC time>/ (resolve_probe.json and exports/): send back that folder, or read it here:")
            print(f'  python -m matchcut.resolve report "{ws}" "{b.dir}"     # the newest run that measured something; or give a run\'s resolve_probe.json')
            if args.run:
                return run_script(b.script)
            return 0
        from .report import build_report

        rep = build_report(ws, cfg, Path(args.json))
        sys.stdout.write(rep.text)
        print(f"wrote {rep.path}")
        return 0
    except ProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
