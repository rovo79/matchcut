"""Command-line entry point: ``python -m matchcut.pipeline``."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Optional

from .config import DEFAULT_PASSES, PipelineConfig
from .editorial import PinStore, pin_states
from .junctions import build_junctions, junction_detail, junction_line, load_stage_timelines, scorer_for_workspace, stale_edit, stale_human
from .pipeline import Pipeline
from .timeline import EDIT_STAGE, HUMAN_STAGE, Timeline


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
    sp = sub.add_parser("export", help="write stages as OpenTimelineIO, FCPXML and EDL files for an editor, with a fidelity report")
    sp.add_argument("workspace")
    sp.add_argument("--stage", action="append", default=None, dest="stages", metavar="S", help="stage to export, repeatable (default: roughcut and final; edit, an agent's published edit, when named)")
    sp.add_argument("--format", action="append", default=None, dest="formats", help="otio, fcpxml or edl, repeatable (default: all)")
    sp.add_argument("--out", default=None, help="output directory (default: WORKSPACE/exports)")
    sp.add_argument("--resolve-script", action="store_true", help="also write open_in_resolve.py, a script for DaVinci Resolve Studio that imports the final OTIO and marks the junctions (never run by matchcut)")
    sp = sub.add_parser("import", help="read an editor's re-export back as the workspace's human stage")
    sp.add_argument("workspace")
    sp.add_argument("file", help="the .otio, .fcpxml (or .fcpxmld bundle) or .edl file the editor saved")
    sp.add_argument("--format", default=None, help="otio, fcpxml or edl (default: from the extension)")
    sp.add_argument("--stage", default=None, help="the stage the file was exported from (default: what the file says, else final); edit for an agent's published edit")
    sp.add_argument("--base", default=None, metavar="SNAPSHOT", help="the exact version of --stage the file was exported from: a snapshot in "
                    "exports/.stages (path or name) or a stage file; needed when the workspace has an edit stage and the file carries no "
                    "fingerprint, or one that names another stage")
    return parser.parse_args(argv)


def _workspace_config(ws: Path) -> Optional[PipelineConfig]:
    """The workspace's config, or None after printing why there is none."""
    cfg_path = ws / "config.json"
    if not cfg_path.exists():
        print(f"no workspace at {ws} (config.json missing)", file=sys.stderr)
        return None
    try:
        return PipelineConfig.load(cfg_path)
    except Exception as exc:  # corrupt or hand-edited JSON
        print(f"cannot read {cfg_path}: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}", file=sys.stderr)
        return None


def _interchange(args: argparse.Namespace) -> int:
    """``export`` and ``import``: the interchange package is imported here, never at start-up."""
    from matchcut.interchange import InterchangeError, workspace

    ws = Path(args.workspace)
    cfg = _workspace_config(ws)
    if cfg is None:
        return 1
    try:
        if args.command == "export":
            res = workspace.export_workspace(ws, cfg, tuple(args.stages or workspace.DEFAULT_STAGES), tuple(args.formats or workspace.FORMATS),
                                             Path(args.out) if args.out else None, args.resolve_script)
            out_dir = res.files[0].parent
            print(f"exported {len(res.files)} files to {out_dir}")
            for f in res.files:
                print(f"  {f}")
            print(f"fidelity: {len(res.results)} checks, {sum(len(r.errors) for r in res.results)} errors ({out_dir / 'fidelity.md'})")
            for r in res.results:
                print(f"  {r.stage}  {r.fmt}  {'ok' if r.ok else 'FAILED'}: {len(r.preserved)} preserved, {len(r.approximated)} approximated, {len(r.lost)} lost")
            for n in res.notes:
                print(f"note: {n}")
        else:
            imp = workspace.import_timeline(ws, cfg, Path(args.file), args.format, args.stage, args.base)
            print(f"imported {Path(args.file).name} ({imp.fmt}) against {imp.stage}: {len(imp.decisions)} human decision"
                  f"{'s' if len(imp.decisions) != 1 else ''} -> {imp.stage_path}")
            for d in imp.decisions:
                print(f"  {d.action} {' '.join(d.clip_ids)}  {d.detail}")
            if not imp.decisions:
                print(f"  (no changes: the file matches the {imp.stage} stage)")
            if imp.notes:
                print(f"import report: {len(imp.notes)} note{'s' if len(imp.notes) != 1 else ''}")
                for n in imp.notes:
                    print(f"  - {n}")
    except InterchangeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


EDIT_NOT_HELD = ("not held by this edit (the pin was stored after the pipeline's last run; a run would apply it, and leave the edit "
                 "stage out as stale)")


def _pin_footer(ws: Path, stages) -> List[str]:
    """On the human or the edit stage, one line per stored pin: what that cut did with it (the engine's ``pin_states``, the answer the Edit
    view gives too).  The edit stage holds every pin its base held (a publish refuses otherwise): a pin it does not hold was stored since
    the pipeline's last run, and a run would make the edit stage stale, so it is not called pending."""
    if not stages or stages[-1][0] not in (HUMAN_STAGE, EDIT_STAGE):
        return []
    edit = stages[-1][0] == EDIT_STAGE
    lines = []
    for st in pin_states(stages[-1][1], PinStore(ws).load(), stages[-1][0]):
        what = {"applied": "held by this edit" if edit else "applied", "changed": f"changed by the editor: {st.reason}",
                "not_placed": f"not placed: {st.reason}", "pending": EDIT_NOT_HELD if edit else "pending (run again to apply)"}[st.state]
        lines.append(f"pin {st.label}: {what}")
    return lines


def _junctions(ws: Path, index: Optional[int]) -> int:
    cfg = _workspace_config(ws)
    if cfg is None:
        return 1
    stages = load_stage_timelines(ws)
    if not stages:
        print(f"no timelines in {ws}: run the pipeline first", file=sys.stderr)
        return 1
    junctions = build_junctions(stages, scorer_for_workspace(ws, cfg), pins=PinStore(ws).load())
    stale = "\n".join(f"note: {r}" for r in (stale_edit(ws), stale_human(ws)) if r)  # a stale edit.json / human.json is left out of the stages above; say so
    footer = _pin_footer(ws, stages)
    if index is None:
        print(f"{len(junctions)} junctions in {stages[-1][0]} ({len(stages)} stages)")
        for j in junctions:
            print(junction_line(j))
        if stale:
            print(stale)
        for line in footer:
            print(line)
        return 0
    if not 1 <= index <= len(junctions):
        print(f"junction {index} is out of range 1..{len(junctions)}", file=sys.stderr)
        return 1
    print(junction_detail(junctions[index - 1]))
    if stale:
        print(stale)
    for line in footer:
        print(line)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if args.command == "junctions":
        return _junctions(Path(args.workspace), args.index)
    if args.command in ("export", "import"):
        return _interchange(args)
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
