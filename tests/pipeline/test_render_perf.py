"""A recorded performance regression case, run only on request: ``MATCHCUT_PERF=1 pytest tests/pipeline/test_render_perf.py``.

With one input per clip, a cut of many very short clips decodes each clip's lead-in from its keyframe, so it renders slower than the old
shared input did (independent review: a 1080p cut of 100 x 0.4 s clips took 28.6 s, against 9.0 s on the shared input, with less memory).
The budget below keeps that from getting worse unnoticed; a faster design must still pass test_render_order.py's frame and audio checks.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from matchcut.pipeline.media import probe
from matchcut.pipeline.timeline import Clip, Timeline

pytestmark = pytest.mark.skipif(os.environ.get("MATCHCUT_PERF") != "1", reason="performance case: set MATCHCUT_PERF=1 to run")

BUDGET_SECONDS_PER_CLIP = 0.6  # reviewer's machine: 0.29 s per clip; a generous ceiling against a slower CI machine


def test_many_very_short_clips_at_1080p(tmp_path):
    src = tmp_path / "src1080.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=25:d=60", "-f", "lavfi", "-i", "sine=f=440:r=48000:d=60",
                    "-c:v", "libx264", "-preset", "veryfast", "-g", "50", "-pix_fmt", "yuv420p", "-c:a", "aac", str(src)], check=True)
    media = probe(str(src))
    tl = Timeline(source=str(src), fps=media.fps)
    tl.clips = [Clip(f"c{i + 1:03d}", i, round(i * 0.56, 4), round(i * 0.56 + 0.4, 4)) for i in range(100)]
    # rendered in a child process, so its peak memory is the render's alone (not the encode that made the source above)
    code = ("import json,sys,time,resource; from matchcut.pipeline.config import RenderConfig; from matchcut.pipeline.media import probe; "
            "from matchcut.pipeline.render import render; from matchcut.pipeline.timeline import Timeline; "
            "tl=Timeline.from_dict(json.loads(sys.argv[1])); t=time.perf_counter(); "
            "render(tl, probe(tl.source), RenderConfig(preset='veryfast'), __import__('pathlib').Path(sys.argv[2])); "
            "print(time.perf_counter()-t, resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss//1024)")
    proc = subprocess.run(["python", "-c", code, __import__("json").dumps(tl.to_dict()), str(tmp_path / "out.mp4")], capture_output=True, text=True, check=True,
                          cwd=Path(__file__).resolve().parents[2])
    seconds, peak_mb = float(proc.stdout.split()[0]), int(proc.stdout.split()[1])
    print(f"\n100 x 0.4 s clips at 1080p: {seconds:.1f} s, peak {peak_mb} MB")
    assert seconds <= BUDGET_SECONDS_PER_CLIP * len(tl.clips), seconds
