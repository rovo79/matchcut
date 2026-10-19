"""Capture the Edit tab of a workspace as SVG screenshots, one per junction (headless)."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matchcut.pipeline.config import PipelineConfig  # noqa: E402
from matchcut.tui.app import MatchcutApp  # noqa: E402
from matchcut.tui.views.edit import FRAME_IDS, EditView  # noqa: E402
from matchcut.tui.widgets.frame import Frame  # noqa: E402


async def capture(ws: Path, out: Path, count: int, size: Tuple[int, int] = (140, 46)) -> int:
    config_path = ws / "config.json"
    cfg = PipelineConfig.load(config_path) if config_path.exists() else PipelineConfig()
    cfg.workspace = str(ws)
    app = MatchcutApp(cfg)
    async with app.run_test(size=size) as pilot:
        await pilot.press("4")
        view = app.query_one(EditView)
        for _ in range(400):
            if view.junctions and all(app.query_one(f"#{i}", Frame).image_mode for i in FRAME_IDS):
                break
            await pilot.pause(0.05)
        if not view.junctions:
            print(f"no junctions in {ws}", file=sys.stderr)
            return 1
        out.mkdir(parents=True, exist_ok=True)
        for k in range(min(count, len(view.junctions))):
            await pilot.pause(0.2)
            name = f"edit-junction-{k + 1}.svg"
            app.save_screenshot(filename=name, path=str(out))
            print(out / name)
            await pilot.press("right")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("workspace")
    p.add_argument("outdir")
    p.add_argument("--junctions", type=int, default=3)
    a = p.parse_args(argv)
    return asyncio.run(capture(Path(a.workspace), Path(a.outdir), a.junctions))


if __name__ == "__main__":
    sys.exit(main())
