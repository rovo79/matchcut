"""``python -m matchcut.review WORKSPACE --index N [--stage S] [--ab] [--play] [--render]``"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Optional, Sequence, TextIO

from ..pipeline.config import PipelineConfig
from .controller import ReviewController, open_with_system
from .items import ReviewItem, ReviewPair, ab_file, prepare_review
from .mpv import MpvError
from .window import ReviewError


def _line(tag: str, item: ReviewItem) -> str:
    text = f"{tag} {item.kind} {item.path} cut={item.cut:.2f} duration={item.duration:.2f} {item.transition}"
    return text + (" cached" if item.cached else "")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m matchcut.review", description="Prepare (and play) the review window of one junction.")
    p.add_argument("workspace", help="a pipeline workspace")
    p.add_argument("--index", type=int, required=True, help="junction number, 1-based")
    p.add_argument("--stage", default=None, help="stage to inspect (default: the last one)")
    p.add_argument("--ab", action="store_true", help="also prepare the same junction at the stage before (A)")
    p.add_argument("--play", action="store_true", help="play it in mpv; Enter toggles A/B, s pauses, q quits")
    p.add_argument("--render", action="store_true", help="render the window instead of playing hard cuts from an EDL")
    return p


def _play(ws: Path, cfg: PipelineConfig, args: argparse.Namespace, pair: ReviewPair, controller: ReviewController, stdin: TextIO) -> None:
    if not controller.has_mpv():
        if pair.a is not None:
            ok, message = open_with_system(ab_file(pair, ws, cfg))
            print(f"mpv not found: {message}" + (" (A, a short black gap, then B)" if ok else ""))
            return
        item = pair.b
        if item.kind == "edl":
            item = prepare_review(ws, cfg, args.stage, args.index - 1, force_render=True).b
        controller.play(item)
        print(controller.status)
        return
    controller.play_pair(pair)
    print(controller.status)
    if pair.a is not None:
        print("Enter: switch A/B at the same offset from the cut   s: pause   q: quit")
    else:
        print("s: pause   q: quit")
    for raw in iter(stdin.readline, ""):
        key = raw.strip().lower()
        if key == "q":
            break
        if key == "s":
            controller.stop()
        elif key == "":
            if not controller.toggle():
                print(pair.note or "nothing to toggle")
                continue
        else:
            continue
        print(controller.status)


def main(
    argv: Optional[Sequence[str]] = None,
    stdin: Optional[TextIO] = None,
    controller_factory: Callable[[PipelineConfig], ReviewController] = ReviewController,
) -> int:
    args = _parser().parse_args(argv)
    ws = Path(args.workspace)
    controller: Optional[ReviewController] = None
    try:
        cfg_path = ws / "config.json"
        cfg = PipelineConfig.load(cfg_path) if cfg_path.exists() else PipelineConfig()
        pair = prepare_review(ws, cfg, args.stage, args.index - 1, ab=args.ab, force_render=args.render)
        if pair.a is not None:
            print(_line("A", pair.a))
        print(_line("B", pair.b))
        if pair.note:
            print(pair.note)
        if args.play:
            controller = controller_factory(cfg)
            _play(ws, cfg, args, pair, controller, stdin if stdin is not None else sys.stdin)
        return 0
    except (ReviewError, MpvError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if controller is not None:
            controller.close()


if __name__ == "__main__":
    raise SystemExit(main())
