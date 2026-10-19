# M2 Temporal Editorial Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an editor watch a junction: render a few seconds either side of the selected cut through the existing ffmpeg render graph (real transitions, timing and audio), compare the first cut with the refined cut as an A/B clip, and play both in an external player from the Edit workbench without blocking the UI.

**Architecture:** A new pipeline module `matchcut/pipeline/preview.py` computes the preview window on a stage timeline, builds a sub-timeline of the clips that window touches, renders it with the unchanged `render.render()`, trims and labels it with a second ffmpeg call, caches the result under `workspace/previews/` by a content key, and composes A/B files. `matchcut/pipeline/player.py` chooses and launches an external player. A `preview` CLI command and two Edit-view keys (`p`, `a`) drive it; the Edit view runs generation in a Textual thread worker and shows one status line.

**Tech Stack:** Python 3.9+ syntax (runs on 3.13), ffmpeg 6 (filters used: trim, atrim, drawtext, tpad, apad, concat, xfade, acrossfade), Textual 8.2.8, pytest, pytest-textual-snapshot; synthetic fixture footage from `matchcut.pipeline.demo.make_demo_footage`.

**Spec:** `docs/superpowers/specs/2026-10-18-m2-temporal-review.md` (binding).

## Global Constraints

- Pipeline code lives in `matchcut/pipeline/` and must never import `textual`, `textual_image` or anything under `matchcut/tui/`. Both suites must pass after every task: `python3 -m pytest tests/pipeline -q` and `python3 -m pytest tests/tui -q` (counts at the start of this plan are in the controller's dispatch).
- Python 3.9 compatible syntax: modules start with `from __future__ import annotations`; `Optional`, `List`, `Dict`, `Tuple`, `Sequence` from `typing`; no `X | None` at runtime.
- No new dependencies (no new pip packages). ffmpeg/ffprobe are already required.
- `render.render()` and `render.build_filter_graph()` are reused unchanged; previews never change how `final.mp4` is rendered.
- Textual 8 has no `Static.renderable`; read a Static's text with `str(widget.content)`.
- A preview failure never crashes the app or the CLI: it is a `preview.PreviewError` with one of the spec's one-line messages.
- Partial files are never reused: write under `workspace/previews/.tmp/`, then `os.replace` into place.
- Never put a model identifier in code, docs or commit titles. Commit with `git -c user.name="Claude" -c user.email="noreply@anthropic.com" commit -F <msgfile>`, the message file in `/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/`; the message ends with a `Co-Authored-By:` line carrying your own environment's attribution, then `Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ`. Push with `git push -u origin claude/ai-video-editing-pipeline-bkqqay` after each task. Never stage anything under `.superpowers/`. Never rewrite history.
- M1's human acceptance gate stays open; nothing in this plan may claim real-backend or terminal-graphics acceptance.

---

## File structure

| File | Responsibility |
|---|---|
| `matchcut/pipeline/config.py` | new `PreviewConfig` section (`preview`) |
| `matchcut/pipeline/preview.py` (new) | window, sub-timeline, cache key, render + trim + label, A/B, `build_preview`, `PreviewError` |
| `matchcut/pipeline/junctions.py` | public `locate_junction` wrapper around `_locate` |
| `matchcut/pipeline/player.py` (new) | `player_command`, `play` |
| `matchcut/pipeline/cli.py` | `preview` command |
| `matchcut/tui/views/edit.py` | `p` / `a` keys, worker, `#preview-status` |
| `matchcut/tui/styles.tcss` | height of `#preview-status` (one line) |
| `tools/demo_preview.py` (new) | renders demonstration A/B previews and contact sheets |
| `tests/pipeline/test_preview.py` (new) | Tasks 1-2 |
| `tests/pipeline/test_player.py` (new) | Task 3 |
| `tests/tui/test_preview_ui.py` (new) | Task 4 |
| `docs/M2-ACCEPTANCE.md` (new), `docs/TUI.md`, `docs/PIPELINE.md`, `README.md` | Task 5 |

---

### Task 1: Preview configuration, window and sub-timeline

**Files:**
- Modify: `matchcut/pipeline/config.py`
- Create: `matchcut/pipeline/preview.py` (pure part only in this task)
- Test: `tests/pipeline/test_preview.py`
- Snapshot: `tests/tui/__snapshots__/test_snapshots/test_view_snapshots[5-config].svg` (the Config view shows the new section)

**Interfaces:**
- Produces: `PreviewConfig`; `PipelineConfig.preview`; `preview.PREVIEW_VERSION: int = 1`; `preview.PreviewError(RuntimeError)`; `preview.PreviewWindow` (frozen dataclass: `junction, first, last, start, cut, end, offset`, property `duration`); `preview.preview_window(tl, junction, pre, post) -> PreviewWindow`; `preview.sub_timeline(tl, w) -> Timeline`; `preview.music_start(tl, w, music_start: float) -> float`; `preview.preview_key(sub, w, source: str, cfg: PipelineConfig, label: str) -> str`.

- [ ] **Step 1: Write the failing tests** — `tests/pipeline/test_preview.py`

```python
"""Junction previews: the window, the sub-timeline and the cache key (pure), then rendering (ffmpeg)."""
from __future__ import annotations

import pytest

from matchcut.pipeline.config import PipelineConfig, PreviewConfig
from matchcut.pipeline.preview import PreviewError, PreviewWindow, music_start, preview_key, preview_window, sub_timeline
from matchcut.pipeline.timeline import Clip, Timeline, Transition


def tl(*specs, fade_in=0.0, fade_out=0.0, stage="final"):
    """specs: (shot, in, out, transition_kind, transition_seconds); ids c001.. in order."""
    clips = [Clip(f"c{k + 1:03d}", s, a, b, "b1", transition_in=Transition(kind, d)) for k, (s, a, b, kind, d) in enumerate(specs)]
    return Timeline(source="s.mp4", fps=25.0, clips=clips, fade_in=fade_in, fade_out=fade_out, stage=stage)


CUTS = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "cut", 0), (2, 20.0, 23.0, "cut", 0), (3, 30.0, 33.0, "cut", 0))


def test_config_has_a_preview_section_with_defaults():
    cfg = PipelineConfig()
    assert cfg.preview == PreviewConfig(pre_seconds=2.0, post_seconds=2.0, width=640, crf=23, labels=True, player="")
    again = PipelineConfig.from_dict(cfg.to_dict())
    assert again.preview == cfg.preview


def test_a_hard_cut_window_spans_pre_and_post_around_the_cut():
    w = preview_window(CUTS, 1, 2.0, 2.0)  # junction between clip 1 (ends at 6.0) and clip 2
    assert (w.junction, w.first, w.last) == (1, 1, 2)
    assert (w.start, w.cut, w.end) == (4.0, 6.0, 8.0)
    assert w.offset == 1.0 and w.duration == 4.0


def test_a_long_pre_reaches_back_into_earlier_clips_and_clamps_at_zero():
    w = preview_window(CUTS, 1, 10.0, 0.5)
    assert (w.first, w.last) == (0, 2)
    assert (w.start, w.end, w.offset) == (0.0, 6.5, 0.0)


def test_post_reaches_forward_and_clamps_at_the_end():
    w = preview_window(CUTS, 2, 1.0, 10.0)
    assert (w.first, w.last, w.start, w.cut, w.end) == (2, 3, 8.0, 9.0, 12.0)


def test_a_dissolve_window_includes_the_transition_after_the_cut():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 0.5), (2, 20.0, 23.0, "cut", 0))
    w = preview_window(t, 0, 1.0, 1.0)  # clip 1 starts fading in at 2.5
    assert (w.start, w.cut, w.end) == (1.5, 2.5, 4.0)
    assert (w.first, w.last) == (0, 1)


def test_a_window_starting_inside_an_incoming_dissolve_includes_the_clip_before():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 1.0), (2, 20.0, 23.0, "cut", 0))
    # clip 1 starts at 2.0 and is a blend with clip 0 until 3.0; junction 1 cuts at 2.0 + 3.0 = 5.0
    w = preview_window(t, 1, 2.5, 1.0)  # start = 2.5, inside clip 1's dissolve
    assert w.start == 2.5 and w.first == 0 and w.offset == 2.5


def test_the_next_clips_incoming_dissolve_pulls_it_into_the_window():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "cut", 0), (2, 20.0, 23.0, "dissolve", 1.0))
    w = preview_window(t, 0, 1.0, 2.6)  # end = 3.0 + 2.6 = 5.6; clip 2 starts fading in at 5.0
    assert w.last == 2


def test_an_out_of_range_junction_is_an_error():
    with pytest.raises(PreviewError, match=r"junction 4 is out of range 1\.\.3"):
        preview_window(CUTS, 3, 1.0, 1.0)


def test_the_sub_timeline_cuts_into_its_first_clip_and_keeps_only_its_own_end_fades():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 0.5), (2, 20.0, 23.0, "dip_to_black", 0.4), fade_in=0.5, fade_out=0.7)
    t.history = []  # (any history is dropped anyway)
    sub = sub_timeline(t, preview_window(t, 1, 1.0, 1.0))
    assert [c.id for c in sub.clips] == ["c002", "c003"]
    assert sub.clips[0].transition_in.is_cut and sub.clips[1].transition_in.kind == "dip_to_black"
    assert sub.fade_in == 0.0 and sub.fade_out == 0.7 and sub.history == []
    assert t.clips[1].transition_in.kind == "dissolve"  # the input is untouched
    whole = sub_timeline(t, preview_window(t, 0, 9.0, 9.0))
    assert whole.fade_in == 0.5 and whole.fade_out == 0.7


def test_music_starts_where_the_first_rendered_clip_starts_in_the_full_cut():
    w = preview_window(CUTS, 2, 1.0, 1.0)  # first == 2, which starts at 6.0
    assert music_start(CUTS, w, 4.0) == 10.0


def test_the_cache_key_is_stable_and_changes_with_what_is_rendered(tmp_path):
    src = tmp_path / "s.mp4"
    src.write_bytes(b"x" * 10)
    cfg = PipelineConfig()
    w = preview_window(CUTS, 1, 2.0, 2.0)
    sub = sub_timeline(CUTS, w)
    k = preview_key(sub, w, str(src), cfg, "final  shot 1 -> shot 2")
    assert len(k) == 16 and k == preview_key(sub, w, str(src), cfg, "final  shot 1 -> shot 2")
    assert k != preview_key(sub, w, str(src), cfg, "B  final  shot 1 -> shot 2")
    cfg2 = PipelineConfig()
    cfg2.preview.width = 320
    assert k != preview_key(sub, w, str(src), cfg2, "final  shot 1 -> shot 2")
    moved = sub.copy()
    moved.clips[0].source_out -= 0.04
    assert k != preview_key(moved, w, str(src), cfg, "final  shot 1 -> shot 2")
    src.write_bytes(b"y" * 11)  # the footage changed
    assert k != preview_key(sub, w, str(src), cfg, "final  shot 1 -> shot 2")
```

- [ ] **Step 2: Run** `python3 -m pytest tests/pipeline/test_preview.py -q` — expected: ImportError / failures.

- [ ] **Step 3: Implement**

In `matchcut/pipeline/config.py`, add before `PipelineConfig` and register the field after `render`:

```python
@dataclass
class PreviewConfig:
    """Junction previews rendered for the editor (``matchcut.pipeline.preview``)."""

    pre_seconds: float = 2.0  # shown before the cut
    post_seconds: float = 2.0  # shown after the transition
    width: int = 640
    crf: int = 23
    labels: bool = True  # burn in the stage/shot label and a CUT tag
    player: str = ""  # external player command; empty = mpv, ffplay, then the system opener
```

```python
    render: RenderConfig = field(default_factory=RenderConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
```

Create `matchcut/pipeline/preview.py`:

```python
"""Junction previews: a few seconds either side of one cut, rendered through the real render graph.

A preview is cut from a stage timeline: the clips the window touches are copied into a
sub-timeline (its first clip cut in, its own fades kept only at the ends of the whole cut),
rendered with ``render.render`` (so transitions, audio and music behave as in the final
render), then trimmed to the window and labelled. Files are cached in ``workspace/previews``
under a content key, written to a temporary name first so a failed render is never reused.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .config import PipelineConfig
from .timeline import Timeline, Transition

PREVIEW_VERSION = 1
_EPS = 1e-6


class PreviewError(RuntimeError):
    """A preview could not be made; the message is one line, ready to show the editor."""


@dataclass(frozen=True)
class PreviewWindow:
    junction: int  # the cut between clip ``junction`` and clip ``junction + 1`` of the stage timeline
    first: int  # first clip rendered
    last: int  # last clip rendered
    start: float  # record time (stage timeline seconds) where the preview starts
    cut: float  # record time where the incoming clip starts (its transition, if any, starts here)
    end: float  # record time where the preview ends
    offset: float  # start - record_start(first): where the preview starts inside the rendered clips

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 4)


def _incoming(tl: Timeline, k: int) -> float:
    t = tl.clips[k].transition_in
    return 0.0 if k == 0 or t.is_cut else t.duration


def preview_window(tl: Timeline, junction: int, pre: float, post: float) -> PreviewWindow:
    n = len(tl.clips) - 1
    if not 0 <= junction < n:
        raise PreviewError(f"junction {junction + 1} is out of range 1..{n}")
    cut = tl.record_start(junction + 1)
    start = max(0.0, cut - max(0.0, pre))
    end = min(tl.duration, cut + _incoming(tl, junction + 1) + max(0.0, post))
    first = junction
    while first > 0 and (tl.record_start(first) > start + _EPS or start < tl.record_start(first) + _incoming(tl, first) - _EPS):
        first -= 1
    last = junction + 1
    while last < n and tl.record_start(last + 1) < end - _EPS:
        last += 1
    return PreviewWindow(junction, first, last, round(start, 4), round(cut, 4), round(end, 4), round(start - tl.record_start(first), 4))


def sub_timeline(tl: Timeline, w: PreviewWindow) -> Timeline:
    sub = tl.copy()
    sub.clips = sub.clips[w.first : w.last + 1]
    sub.clips[0].transition_in = Transition()
    sub.fade_in = tl.fade_in if w.first == 0 else 0.0
    sub.fade_out = tl.fade_out if w.last == len(tl.clips) - 1 else 0.0
    sub.history = []
    sub.name = f"preview {tl.stage} junction {w.junction + 1}"
    return sub


def music_start(tl: Timeline, w: PreviewWindow, music_start_seconds: float) -> float:
    """Where the music must start for the excerpt so it lines up with the full render."""
    return round(music_start_seconds + tl.record_start(w.first), 4)


def _stat(path: str) -> list:
    try:
        st = os.stat(path)
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return [None, None]


def preview_key(sub: Timeline, w: PreviewWindow, source: str, cfg: PipelineConfig, label: str) -> str:
    music = asdict(cfg.music)
    if cfg.music.path:
        music["file"] = _stat(str(cfg.music.path))
    doc = {
        "version": PREVIEW_VERSION,
        "clips": [c.to_dict() for c in sub.clips],
        "fades": [sub.fade_in, sub.fade_out],
        "fps": sub.fps,
        "window": asdict(w),
        "source": [str(Path(source).resolve()), *_stat(source)],
        "audio_bitrate": cfg.render.audio_bitrate,
        "preview": asdict(cfg.preview),
        "music": music,
        "label": label,
    }
    text = json.dumps(doc, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
```

Note: `Clip.to_dict()` includes `notes`, `tags` and `purpose`; that is fine (a tag change re-renders, which is harmless).

Regenerate the Config snapshot: `python3 -m pytest tests/tui/test_snapshots.py --snapshot-update`, then open the diff (`git diff --stat tests/tui/__snapshots__`) — only `[5-config]` may change. Any other snapshot change is a bug.

- [ ] **Step 4: Run** `python3 -m pytest tests/pipeline/test_preview.py -q`, then both full suites. Expected: all pass.

- [ ] **Step 5: Commit and push** (title: "Add the preview config, window and sub-timeline for junction previews")

---

### Task 2: Render, cache and compose previews

**Files:**
- Modify: `matchcut/pipeline/preview.py`
- Modify: `matchcut/pipeline/junctions.py` (add `locate_junction`)
- Test: `tests/pipeline/test_preview.py` (append)

**Interfaces:**
- Consumes: Task 1; `render.render(tl, media, cfg: RenderConfig, output, overwrite=True, music=None, music_duration=None)`; `render.snap_to_frames(tl, bounds)`; `media.MediaInfo`, `media.probe(path)`, `media.MediaError`, `media._require`; `shots.load_shots`; `junctions.load_stage_timelines`.
- Produces:
  - `junctions.locate_junction(timeline, a_ref: Clip, b_ref: Clip) -> Optional[int]` (public alias of `_locate`).
  - `preview.Preview` dataclass: `path: Path, window: PreviewWindow, stage: str, label: str, cached: bool`.
  - `preview.render_preview(tl, junction, media, cfg, out_dir, label="", bounds=None) -> Preview`.
  - `preview.render_ab(a: Preview, b: Preview, out_dir: Path, has_audio: bool, cfg: PipelineConfig) -> Tuple[Path, bool]` (path, cached).
  - `preview.PreviewRequest(stage: str, index: int, ab: bool = False)` (index 0-based).
  - `preview.PreviewResult`: `path: Path` (what to play), `b: Preview`, `a: Optional[Preview]`, `note: str = ""`, `cached: bool = False` (true when no ffmpeg ran for any part).
  - `preview.build_preview(ws: Path, cfg: PipelineConfig, request: PreviewRequest) -> PreviewResult`.

- [ ] **Step 1: Write the failing tests** (append to `tests/pipeline/test_preview.py`). Fixtures: `demo_video` (session, `tests/pipeline/conftest.py`): 320x180 at 25 fps with a 440 Hz tone; shots in source seconds: 0 `color 0x204080` [0, 2], 1 `testsrc2` [2, 5], 2 `color 0xc04020` [5, 6.5], 3 `smptebars` [6.5, 9], 4 `testsrc` [9, 12], 5 `color 0x208040` [12, 14]. Add a session fixture for a silent copy: `make_demo_footage(footage_dir / "silent.mp4", audio=False)`.

Helper code to put at the top of the ffmpeg section:

```python
import json
import shutil
import subprocess

import numpy as np

from matchcut.pipeline import preview as pv
from matchcut.pipeline.demo import make_demo_footage
from matchcut.pipeline.media import extract_frame, probe

BLUE, RED = 0, 2  # shot indices of the two solid-colour shots


def two_shots(kind="cut", seconds=0.0, stage="final", out=2.0, inn=5.0):
    """blue shot 0 [0.5, out] then red shot 2 [inn, 6.5], one beat each."""
    return Timeline(source="demo.mp4", fps=25.0, stage=stage, clips=[
        Clip("c001", BLUE, 0.5, out, "b1"),
        Clip("c002", RED, inn, 6.5, "b2", transition_in=Transition(kind, seconds)),
    ])


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


def has_audio_stream(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(out.stdout.strip())


def rgb(path, t):
    img = np.asarray(extract_frame(str(path), t, 64)).astype(float)
    return img[..., 0].mean(), img[..., 2].mean()  # (red, blue)


def small_cfg(pre=1.0, post=1.0):
    cfg = PipelineConfig()
    cfg.preview.pre_seconds, cfg.preview.post_seconds, cfg.preview.width = pre, post, 160
    return cfg
```

Tests (write each fully; the expected values are binding):

1. `test_a_hard_cut_preview_has_the_window_length_and_cuts_from_blue_to_red(demo_video, tmp_path)`: `p = pv.render_preview(two_shots(), 0, probe(str(demo_video)), small_cfg(), tmp_path / "previews", label="final  shot 0 -> shot 2")`; window cut at 1.5, start 0.5, end 2.5 → `abs(duration(p.path) - 2.0) < 0.1`; `red, blue = rgb(p.path, 0.8)` has `blue > red + 40`; at `1.2` `red > blue + 40`; `p.cached is False`; `p.path.parent == tmp_path / "previews"`; `p.path.name.startswith("final_s0-s2_")`; `has_audio_stream(p.path)`.
2. `test_a_dissolve_preview_blends_the_two_shots_mid_transition(demo_video, tmp_path)`: `two_shots("dissolve", 0.6)`: clip 1 lasts 1.5 s, so the incoming clip starts fading in at `cut = 1.5 - 0.6 = 0.9`; the whole cut lasts `1.5 + 1.5 - 0.6 = 2.4` s, so the window is start 0.0, cut 0.9, end 2.4 → duration ≈ 2.4 (±0.1). Mid-transition (`0.9 + 0.3 = 1.2` s into the preview) the red and blue means both lie strictly between their values on the pure frames at 0.3 s (blue shot: red≈32, blue≈128) and 2.2 s (red shot: red≈192, blue≈32), with a 15-unit margin on each side.
3. `test_a_silent_source_renders_a_silent_preview(silent_video, tmp_path)`: no audio stream in the result; duration still ≈ 2.0.
4. `test_a_second_request_reuses_the_cached_file(demo_video, tmp_path, monkeypatch)`: first render; record `mtime_ns`; second call returns `cached is True`, same path, same `mtime_ns`, and `render.render` is not called (monkeypatch `pv.render` with a function that fails the test if called). Then `small_cfg(pre=0.5)` gives a different path.
5. `test_labels_are_optional_and_a_missing_drawtext_is_not_an_error(demo_video, tmp_path, monkeypatch)`: `monkeypatch.setattr(pv, "_has_drawtext", lambda ffmpeg: False)` → render succeeds; with `cfg.preview.labels = False` and drawtext available → render succeeds; the two files differ in name (labels are part of `cfg.preview`, so the key differs).
6. `test_a_labelled_render_that_fails_is_retried_without_labels(demo_video, tmp_path, monkeypatch)`: wrap `pv._trim_and_label` (see implementation) so the first call with `labels=True` raises `MediaError("command failed (1): ffmpeg\nNo such filter: 'drawtext'")`; assert the preview is still produced and the wrapper saw a second call with `labels=False`.
7. `test_build_preview_ab_plays_the_first_cut_then_the_refined_one(demo_video, tmp_path)`: workspace `ws` with `probe.json` (`json.dumps(probe(str(demo_video)).to_dict())`), `roughcut.json` = `two_shots(stage="roughcut")` and `final.json` = `two_shots("dissolve", 0.6, stage="final", out=1.8)`, `cfg = small_cfg()`. `r = pv.build_preview(ws, cfg, pv.PreviewRequest("final", 0, ab=True))`: `r.a.stage == "roughcut"`, `r.b.stage == "final"`, `r.note == ""`, `r.path.name.startswith("ab_")`, `abs(duration(r.path) - (duration(r.a.path) + 0.5 + duration(r.b.path))) < 0.2`, `has_audio_stream(r.path)`; the frame at `duration(r.a.path) + 0.25` is black (both channel means < 20).
8. `test_ab_of_a_junction_that_first_appears_at_the_inspected_stage_plays_it_alone(demo_video, tmp_path)`: same ws but `roughcut.json` has shot 0 then shot 3 (`Clip("c002", 3, 6.6, 8.0, "b2")`); `r.a is None`, `r.note == "nothing earlier to compare: this junction first appears at final"`, `r.path == r.b.path`.
9. `test_preview_errors_are_one_line_and_leave_nothing_behind(demo_video, tmp_path, monkeypatch)`:
   - empty ws → `PreviewError` matching `no timelines in .*: run the pipeline first`;
   - unknown stage `"pacing"` → `no stage pacing in`;
   - index 5 → `junction 6 is out of range 1..1`;
   - `probe.json` pointing at a missing file → `source footage not found: `;
   - `monkeypatch.setattr(pv, "render", raising MediaError("command failed (1): ffmpeg -i x\nsomething\nInvalid argument\n"))` → `PreviewError("ffmpeg failed: Invalid argument")`;
   - after all of these, `list((ws / "previews").rglob("*.mp4")) == []` (no file and no temp file left).
10. `test_music_is_offset_to_the_first_rendered_clip(tmp_path, monkeypatch)`: monkeypatch `pv.render` to record its `music` argument and write an empty file at the output path, and `pv._trim_and_label` to copy the input to the output; with `cfg.music.path = str(some_file)` and `cfg.music.start = 4.0` on `CUTS` junction 2 → recorded `music.start == 10.0` and `cfg.music.start` is still `4.0` afterwards (the config is not mutated). (Use a `MediaInfo(path=..., duration=40, width=320, height=180, fps=25.0, has_audio=False)` built by hand; no ffmpeg runs.)

- [ ] **Step 2: Run** the tests — expected failures (functions missing).

- [ ] **Step 3: Implement** (append to `matchcut/pipeline/preview.py`; keep Task 1's code). Reference implementation — follow it, adjust only if a test proves a detail wrong, and say so in the report:

```python
import dataclasses
import functools
import json
import subprocess
import tempfile
from typing import Dict, List, Sequence, Tuple

from .config import RenderConfig
from .junctions import load_stage_timelines, locate_junction
from .media import MediaError, MediaInfo, _require, probe, run
from .render import render, snap_to_frames
from .shots import load_shots

CUT_TAG_SECONDS = 0.3
AB_GAP_SECONDS = 0.5
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ._->")


@dataclass
class Preview:
    path: Path
    window: PreviewWindow
    stage: str
    label: str
    cached: bool


@dataclass(frozen=True)
class PreviewRequest:
    stage: str
    index: int  # 0-based junction index in that stage
    ab: bool = False


@dataclass
class PreviewResult:
    path: Path
    b: Preview
    a: Optional[Preview] = None
    note: str = ""
    cached: bool = False


@functools.lru_cache(maxsize=None)
def _has_drawtext(ffmpeg: str) -> bool:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, check=False).stdout
    except OSError:
        return False
    return any(line.split()[1:2] == ["drawtext"] for line in out.splitlines() if line.strip())


def _ffmpeg_error(e: Exception) -> PreviewError:
    text = str(e)
    if "not found on PATH" in text:
        return PreviewError("ffmpeg not found on PATH")
    lines = [ln.strip() for ln in text.splitlines()[1:] if ln.strip()]
    return PreviewError(f"ffmpeg failed: {lines[-1] if lines else text.splitlines()[0]}")


def _label_filter(label: str, cut_at: float) -> str:
    text = "".join(ch if ch in _SAFE else " " for ch in label)
    box = "fontcolor=white:fontsize=h/16:box=1:boxcolor=black@0.55:boxborderw=6"
    return (
        f"drawtext=text='{text}':x=12:y=12:{box},"
        f"drawtext=text='CUT':x=w-tw-12:y=12:{box}:enable='between(t,{cut_at:.3f},{cut_at + CUT_TAG_SECONDS:.3f})'"
    )


def _trim_and_label(src: Path, dst: Path, w: PreviewWindow, has_audio: bool, label: str, labels: bool, cfg: PipelineConfig) -> None:
    ffmpeg = _require("ffmpeg")
    v = f"[0:v]trim=start={w.offset:.4f}:duration={w.duration:.4f},setpts=PTS-STARTPTS"
    if labels and label:
        v += "," + _label_filter(label, w.cut - w.start)
    graph = [v + "[v]"]
    if has_audio:
        graph.append(f"[0:a]atrim=start={w.offset:.4f}:duration={w.duration:.4f},asetpts=PTS-STARTPTS[a]")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(src), "-filter_complex", ";".join(graph), "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", cfg.render.audio_bitrate]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cfg.preview.crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]
    run(cmd, capture=False)


def _tmp(out_dir: Path, suffix: str) -> Path:
    tmp_dir = out_dir / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=suffix, dir=tmp_dir)
    os.close(fd)
    return Path(name)


def _cached(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def render_preview(tl: Timeline, junction: int, media: MediaInfo, cfg: PipelineConfig, out_dir: Path,
                   label: str = "", bounds: Optional[Dict[int, Sequence[float]]] = None) -> Preview:
    w = preview_window(tl, junction, cfg.preview.pre_seconds, cfg.preview.post_seconds)
    sub = snap_to_frames(sub_timeline(tl, w), bounds)
    key = preview_key(sub, w, media.path, cfg, label)
    a, b = tl.clips[junction], tl.clips[junction + 1]
    path = Path(out_dir) / f"{tl.stage or 'cut'}_s{a.shot_index}-s{b.shot_index}_{key}.mp4"
    if _cached(path):
        return Preview(path, w, tl.stage, label, True)
    music = None
    if cfg.music.path:
        music = dataclasses.replace(cfg.music, start=music_start(tl, w, cfg.music.start))
    has_audio = media.has_audio or music is not None
    rcfg = RenderConfig(enabled=True, width=cfg.preview.width, crf=cfg.preview.crf, preset="veryfast", audio_bitrate=cfg.render.audio_bitrate)
    full, trimmed = _tmp(Path(out_dir), ".full.mp4"), _tmp(Path(out_dir), ".mp4")
    try:
        render(sub, media, rcfg, full, music=music)
        labels = cfg.preview.labels and bool(label) and _has_drawtext(_require("ffmpeg"))
        try:
            _trim_and_label(full, trimmed, w, has_audio, label, labels, cfg)
        except MediaError:
            if not labels:
                raise
            _trim_and_label(full, trimmed, w, has_audio, label, False, cfg)
        os.replace(trimmed, path)
    except MediaError as e:
        raise _ffmpeg_error(e) from e
    finally:
        for f in (full, trimmed):
            try:
                f.unlink()
            except OSError:
                pass
    return Preview(path, w, tl.stage, label, False)


def render_ab(a: Preview, b: Preview, out_dir: Path, has_audio: bool, cfg: PipelineConfig) -> Tuple[Path, bool]:
    path = Path(out_dir) / f"ab_{a.path.stem.rsplit('_', 1)[-1]}_{b.path.stem.rsplit('_', 1)[-1]}.mp4"
    if _cached(path):
        return path, True
    ffmpeg = _require("ffmpeg")
    gap = f"{AB_GAP_SECONDS:.3f}"
    if has_audio:
        graph = (f"[0:v]tpad=stop_mode=add:stop_duration={gap}:color=black[va];[0:a]apad=pad_dur={gap}[aa];"
                 "[va][aa][1:v][1:a]concat=n=2:v=1:a=1[v][a]")
    else:
        graph = f"[0:v]tpad=stop_mode=add:stop_duration={gap}:color=black[va];[va][1:v]concat=n=2:v=1:a=0[v]"
    tmp = _tmp(Path(out_dir), ".mp4")
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-y", "-i", str(a.path), "-i", str(b.path), "-filter_complex", graph, "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", cfg.render.audio_bitrate]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cfg.preview.crf), "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(tmp)]
    try:
        run(cmd, capture=False)
        os.replace(tmp, path)
    except MediaError as e:
        raise _ffmpeg_error(e) from e
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return path, False


def _media(ws: Path, cfg: PipelineConfig) -> MediaInfo:
    probe_path = ws / "probe.json"
    if probe_path.exists():
        try:
            media = MediaInfo.from_dict(json.loads(probe_path.read_text()))
        except (OSError, ValueError, TypeError) as e:
            raise PreviewError(f"cannot read {probe_path}: {e}") from e
    elif cfg.source and Path(cfg.source).exists():
        try:
            media = probe(cfg.source)
        except MediaError as e:
            raise _ffmpeg_error(e) from e
    else:
        raise PreviewError(f"source footage not found: {cfg.source or '(no source configured)'}")
    if not Path(media.path).exists():
        raise PreviewError(f"source footage not found: {media.path}")
    return media


def _bounds(ws: Path) -> Optional[Dict[int, Sequence[float]]]:
    try:
        return {s.index: (s.start, s.end) for s in load_shots(ws / "shots.json")}
    except Exception:
        return None


def build_preview(ws: Path, cfg: PipelineConfig, request: PreviewRequest) -> PreviewResult:
    ws = Path(ws)
    stages = load_stage_timelines(ws)
    if not stages:
        raise PreviewError(f"no timelines in {ws}: run the pipeline first")
    names = [s for s, _ in stages]
    if request.stage not in names:
        raise PreviewError(f"no stage {request.stage} in {ws}")
    k = names.index(request.stage)
    tl = stages[k][1]
    n = len(tl.clips) - 1
    if not 0 <= request.index < n:
        raise PreviewError(f"junction {request.index + 1} is out of range 1..{max(n, 0)}")
    media = _media(ws, cfg)
    bounds = _bounds(ws)
    out_dir = ws / "previews"
    a_ref, b_ref = tl.clips[request.index], tl.clips[request.index + 1]
    shots = f"shot {a_ref.shot_index} -> shot {b_ref.shot_index}"
    if not request.ab:
        p = render_preview(tl, request.index, media, cfg, out_dir, f"{request.stage}  {shots}", bounds)
        return PreviewResult(p.path, p, None, "", p.cached)
    base: Optional[Tuple[str, Timeline, int]] = None
    for stage, timeline in stages[:k]:
        idx = locate_junction(timeline, a_ref, b_ref)
        if idx is not None:
            base = (stage, timeline, idx)
            break
    if base is None:
        p = render_preview(tl, request.index, media, cfg, out_dir, f"{request.stage}  {shots}", bounds)
        return PreviewResult(p.path, p, None, f"nothing earlier to compare: this junction first appears at {request.stage}", p.cached)
    b = render_preview(tl, request.index, media, cfg, out_dir, f"B  {request.stage}  {shots}", bounds)
    a = render_preview(base[1], base[2], media, cfg, out_dir, f"A  {base[0]}  {shots}", bounds)
    path, cached = render_ab(a, b, out_dir, media.has_audio or bool(cfg.music.path), cfg)
    return PreviewResult(path, b, a, "", cached and a.cached and b.cached)
```

Put all imports at the top of the module (merge with Task 1's). In `matchcut/pipeline/junctions.py` add:

```python
def locate_junction(timeline: Timeline, a_ref: Clip, b_ref: Clip) -> Optional[int]:
    """Index i where clip i descends from ``a_ref`` and clip i+1 from ``b_ref`` (same shot, overlapping source), or None."""
    return _locate(timeline, a_ref, b_ref)
```

Check for an import cycle (`preview` imports `junctions`, which must not import `preview`).

- [ ] **Step 4: Run** `python3 -m pytest tests/pipeline/test_preview.py -q` (expect ~1 minute: real ffmpeg renders at 160 px), then both full suites.

- [ ] **Step 5: Commit and push** (title: "Render, cache and compose junction previews through the render graph")

---

### Task 3: External player and the `preview` CLI command

**Files:**
- Create: `matchcut/pipeline/player.py`
- Modify: `matchcut/pipeline/cli.py`
- Test: `tests/pipeline/test_player.py` (new), `tests/pipeline/test_preview.py` (CLI test appended)

**Interfaces:**
- Produces: `player.player_command(path: Path, configured: str = "", which=shutil.which, platform: str = sys.platform) -> Optional[List[str]]`; `player.play(path: Path, configured: str = "", popen=subprocess.Popen, which=shutil.which, platform: str = sys.platform) -> Tuple[bool, str]`.
- Messages (binding): launched → `playing <file name>`; nothing to launch → `preview saved: <path> (no video player found: set preview.player)`; `OSError` from Popen → `preview saved: <path> (could not start <argv0>: <error>)`.

- [ ] **Step 1: Tests** — `tests/pipeline/test_player.py`:

```python
from pathlib import Path

from matchcut.pipeline import player

P = Path("/tmp/x/final_s1-s2_abc.mp4")


def only(*names):
    return lambda name: f"/usr/bin/{name}" if name in names else None


def test_a_configured_player_wins_and_gets_the_path_appended():
    assert player.player_command(P, "vlc --play-and-exit", which=only("mpv")) == ["vlc", "--play-and-exit", str(P)]


def test_mpv_then_ffplay_then_the_system_opener():
    assert player.player_command(P, which=only("mpv", "ffplay")) == ["mpv", "--force-window=yes", "--really-quiet", str(P)]
    assert player.player_command(P, which=only("ffplay", "xdg-open")) == ["ffplay", "-autoexit", "-loglevel", "error", "-window_title", P.name, str(P)]
    assert player.player_command(P, which=only("xdg-open"), platform="linux") == ["xdg-open", str(P)]
    assert player.player_command(P, which=only("open"), platform="darwin") == ["open", str(P)]
    assert player.player_command(P, which=only()) is None


def test_play_launches_detached_and_reports():
    calls = []

    def popen(cmd, **kw):
        calls.append((cmd, kw))
    assert player.play(P, which=only("ffplay"), popen=popen) == (True, f"playing {P.name}")
    cmd, kw = calls[0]
    assert cmd[0] == "ffplay" and kw.get("start_new_session") is True
    assert all(kw.get(k) is not None for k in ("stdin", "stdout", "stderr"))


def test_play_without_a_player_says_where_the_file_is():
    assert player.play(P, which=only()) == (False, f"preview saved: {P} (no video player found: set preview.player)")


def test_a_configured_player_that_cannot_start_is_reported_without_fallback():
    def popen(cmd, **kw):
        raise FileNotFoundError(2, "No such file or directory")
    ok, message = player.play(P, "nonexistent-player", which=only("ffplay"), popen=popen)
    assert ok is False and message.startswith(f"preview saved: {P} (could not start nonexistent-player:")
```

CLI test (append to `tests/pipeline/test_preview.py`; reuse the Task 2 workspace helper): `cli.main(["preview", str(ws), "--index", "1"])` returns 0 and prints `preview: <path>`; a second identical call prints `preview: <path> (cached)`; `--ab` on the workspace of Task 2 test 8 prints the note line; `--index 9` returns 1 and prints `junction 9 is out of range 1..1` to stderr; `--play` with `monkeypatch.setattr(player, "play", lambda path, configured="": (True, "playing x"))` prints `playing x`. A workspace without `config.json` uses `PipelineConfig()` defaults (the source is read from `probe.json`).

- [ ] **Step 2: Run** — failures expected.

- [ ] **Step 3: Implement** `matchcut/pipeline/player.py`:

```python
"""Launch an external video player for a preview file (no terminal video playback)."""
from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple


def player_command(path: Path, configured: str = "", which: Callable[[str], Optional[str]] = shutil.which, platform: str = sys.platform) -> Optional[List[str]]:
    if configured.strip():
        return shlex.split(configured) + [str(path)]
    if which("mpv"):
        return ["mpv", "--force-window=yes", "--really-quiet", str(path)]
    if which("ffplay"):
        return ["ffplay", "-autoexit", "-loglevel", "error", "-window_title", Path(path).name, str(path)]
    opener = "open" if platform == "darwin" else "xdg-open"
    if which(opener):
        return [opener, str(path)]
    return None


def play(path: Path, configured: str = "", popen=subprocess.Popen, which: Callable[[str], Optional[str]] = shutil.which, platform: str = sys.platform) -> Tuple[bool, str]:
    cmd = player_command(path, configured, which, platform)
    if cmd is None:
        return False, f"preview saved: {path} (no video player found: set preview.player)"
    try:
        popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        return False, f"preview saved: {path} (could not start {cmd[0]}: {e})"
    return True, f"playing {Path(path).name}"
```

In `matchcut/pipeline/cli.py`: add the subparser next to `junctions`:

```python
    sp = sub.add_parser("preview", help="render (or reuse) a junction preview and print its path")
    sp.add_argument("workspace")
    sp.add_argument("--index", type=int, required=True, help="1-based junction number")
    sp.add_argument("--stage", default=None, help="stage to preview (default: the last stage)")
    sp.add_argument("--ab", action="store_true", help="first cut, then this stage, back to back")
    sp.add_argument("--play", action="store_true", help="open the preview in a video player")
```

and dispatch `if args.command == "preview": return _preview(Path(args.workspace), args.index, args.stage, args.ab, args.play)` before logging is configured (like `junctions`). `_preview` loads `config.json` when present (same error handling as `_junctions`), else `PipelineConfig()`; resolves the default stage as the last of `load_stage_timelines(ws)` (none → print `no timelines in <ws>: run the pipeline first` to stderr, return 1); calls `preview.build_preview(ws, cfg, PreviewRequest(stage, index - 1, ab))`; on `PreviewError` prints the message to stderr and returns 1; otherwise prints `preview: <path>` plus ` (cached)` when `result.cached`, then the note on its own line when present, then with `--play` the message of `player.play(result.path, cfg.preview.player)` (call it as `player.play(...)` through the module so tests can monkeypatch it).

- [ ] **Step 4: Run** `python3 -m pytest tests/pipeline/test_player.py tests/pipeline/test_preview.py -q`, then both full suites.

- [ ] **Step 5: Commit and push** (title: "Add the external player launcher and the preview CLI command")

---

### Task 4: Play and A/B in the Edit workbench

**Files:**
- Modify: `matchcut/tui/views/edit.py`
- Modify: `matchcut/tui/styles.tcss`
- Create: `tests/tui/test_preview_ui.py`
- Snapshot: `tests/tui/__snapshots__/test_snapshots/test_view_snapshots[4-edit].svg`

**Interfaces:**
- Consumes: `preview.build_preview`, `preview.PreviewRequest`, `preview.PreviewResult`, `preview.PreviewError`, `player.play` — always called through the module objects (`preview.build_preview(...)`, `player.play(...)`) so tests can monkeypatch `matchcut.tui.views.edit.preview.build_preview` and `matchcut.tui.views.edit.player.play`.

- [ ] **Step 1: Tests** — `tests/tui/test_preview_ui.py`. Reuse `app_for`, `own_copy`, `wait_for`, `run_async` patterns from `tests/tui/test_edit.py` (import them from there or copy the two small helpers). Each test monkeypatches the builder and/or the player:

```python
def fake_result(tmp_path, name="final_s0-s1_k.mp4", note="", cached=False):
    path = tmp_path / name
    path.write_bytes(b"\0")
    p = Preview(path, PreviewWindow(0, 0, 1, 0.0, 1.0, 2.0, 0.0), "final", "label", cached)
    return PreviewResult(path, p, None, note, cached)
```

1. `test_p_renders_in_the_background_then_plays`: builder records its `request` and returns `fake_result`; player records the path and returns `(True, "playing final_s0-s1_k.mp4")`. Press `4`, wait for junctions, press `p`; `wait_for` the status (`str(app.query_one("#preview-status").content)`) to contain `playing final_s0-s1_k.mp4`; assert the recorded request is `PreviewRequest(view.current_stage(), view.current_junction().index, False)` and the player got the file path.
2. `test_a_requests_an_ab_and_shows_the_note`: builder returns `fake_result(note="nothing earlier to compare: this junction first appears at final")`; press `a`; the request has `ab=True`; the status contains the note.
3. `test_a_cached_preview_says_cached`: `cached=True` → status contains `(cached)`.
4. `test_a_failure_is_shown_and_notified_not_raised`: builder raises `PreviewError("source footage not found: /x.mp4")`; monkeypatch `app.notify` to record `(message, severity)`; status contains `preview failed: source footage not found: /x.mp4`; a notification with severity `error` carries the same text; the app is still running (press `right` afterwards works).
5. `test_without_a_player_the_status_says_where_the_file_is`: player returns `(False, "preview saved: /p.mp4 (no video player found: set preview.player)")` → status shows it and a `warning` notification carries it.
6. `test_rendering_does_not_block_and_a_duplicate_request_is_ignored`: builder blocks on a `threading.Event` (wait up to 30 s) and counts calls. Press `p`; `wait_for` status `rendering`; press `right` → `view.selected` changed and the status now shows the idle text of the newly selected junction (starts with `p play this cut`); press `left`, press `p` again → a notification `already rendering this preview` and the builder count stays 1; set the event; `wait_for` the player to be called exactly once.
7. `test_the_idle_status_offers_a_comparison_only_when_there_is_an_earlier_stage`: on the mock workspace, for a junction whose `baseline.stage != view.current_stage()` the idle status is `p play this cut · a compare with <baseline stage>`; step to the first stage with `[` until `current_stage()` is the first stage → the idle status is `p play this cut`.
8. `test_a_real_preview_renders_into_the_workspace` (uses the real builder, only the player is faked): on `own_copy(mock_workspace, tmp_path)` with `app.state.config.preview.width = 160` set before pressing `p` (or write it into the copied `config.json`), press `p`, `wait_for(..., timeout=120)` the player call; the path exists under `ws / "previews"` and its name starts with `final_`.

- [ ] **Step 2: Run** — failures expected.

- [ ] **Step 3: Implement** in `matchcut/tui/views/edit.py`:

- imports: `import threading` is not needed; add `from typing import Dict, Set` to the existing typing import; `from matchcut.pipeline import player, preview`.
- `BINDINGS`: add `Binding("p", "play_preview", "Play")` and `Binding("a", "ab_preview", "A/B")`.
- `__init__`: `self._previews_running: Set[tuple] = set()` and `self._preview_status: Dict[tuple, Text] = {}`.
- `compose`: directly after the `with Horizontal(id="junction-frames")` block, `yield Static("", id="preview-status")`.
- `_show_junction`: at its end (both branches), call `self._show_preview_status()`.
- new methods:

```python
    # -- previews -------------------------------------------------------------------
    def _status_key(self) -> Optional[tuple]:
        j, stage = self.current_junction(), self.current_stage()
        return (stage, j.out_shot, j.in_shot) if j is not None and stage is not None else None

    def _show_preview_status(self) -> None:
        status = self.query_one("#preview-status", Static)
        key = self._status_key()
        if key is None:
            status.update("")
            return
        text = self._preview_status.get(key)
        if text is None:
            j = self.current_junction()
            idle = "p play this cut" + (f" · a compare with {j.baseline.stage}" if j.baseline.stage != key[0] else "")
            text = Text(idle, style="dim")
        status.update(text)

    def _set_preview_status(self, key: tuple, text: Text) -> None:
        self._preview_status[key] = text
        self._show_preview_status()  # shows it only when that junction is the one selected

    def _request_preview(self, ab: bool) -> None:
        j, stage = self.current_junction(), self.current_stage()
        if j is None or stage is None or self._state is None:
            self.app.notify("no junction selected", severity="warning")
            return
        key = (stage, j.out_shot, j.in_shot, ab)
        if key in self._previews_running:
            self.app.notify("already rendering this preview")
            return
        self._previews_running.add(key)
        label = f"{'A/B of ' if ab else ''}{stage} shot {j.out_shot} -> shot {j.in_shot}"
        self._set_preview_status(key[:3], Text(f"rendering {label}…"))
        ws, cfg, request = self._state.workspace, self._state.config, preview.PreviewRequest(stage, j.index, ab)

        def work() -> None:
            try:
                result = preview.build_preview(ws, cfg, request)
            except Exception as e:  # PreviewError, or anything unexpected: report it, never crash the app
                self._from_thread(self._preview_failed, key, e)
                return
            self._from_thread(self._preview_ready, key, result)

        self.run_worker(work, thread=True, group="preview", exit_on_error=False)

    def _from_thread(self, fn, *args) -> None:
        try:
            self.app.call_from_thread(fn, *args)
        except RuntimeError:  # the app closed while ffmpeg was running
            pass

    def _preview_ready(self, key: tuple, result: "preview.PreviewResult") -> None:
        self._previews_running.discard(key)
        cfg = self._state.config if self._state is not None else None
        launched, message = player.play(result.path, cfg.preview.player if cfg is not None else "")
        text = message + (" (cached)" if launched and result.cached else "")
        if result.note:
            text += f" · {result.note}"
            self.app.notify(result.note)
        if not launched:
            self.app.notify(message, severity="warning")
        self._set_preview_status(key[:3], Text(text))

    def _preview_failed(self, key: tuple, error: Exception) -> None:
        self._previews_running.discard(key)
        message = f"preview failed: {error}"
        self._set_preview_status(key[:3], Text(message, style="red"))
        self.app.notify(message, severity="error")

    def action_play_preview(self) -> None:
        self._request_preview(False)

    def action_ab_preview(self) -> None:
        self._request_preview(True)
```

Status entries are kept per (stage, shot pair): a new run that changes the cut leaves an old "playing …" line, which is still true of that file. Clear `self._preview_status` in `refresh_from_state` when the workspace data fingerprint changes (`fingerprint != self._fingerprint`, before rebuilding) so a status never describes a cut that no longer exists — except entries whose key is in `self._previews_running`.

In `matchcut/tui/styles.tcss` add `#preview-status { height: 1; }` (match the existing selector style in the file).

Regenerate the Edit snapshot (`python3 -m pytest tests/tui/test_snapshots.py --snapshot-update`); only `[4-edit]` may change (footer `p Play`, `a A/B`, the idle status line). Check that the Edit layout at 120x40 still shows the whole inspector panel's first lines (the status line takes one row from the scroll area).

- [ ] **Step 4: Run** `python3 -m pytest tests/tui/test_preview_ui.py tests/tui/test_edit.py -q`, then both full suites.

- [ ] **Step 5: Commit and push** (title: "Play and A/B junction previews from the Edit workbench without blocking")

---

### Task 5: Demonstration artifact, acceptance criteria and docs

**Files:**
- Create: `tools/demo_preview.py`
- Create: `docs/M2-ACCEPTANCE.md`
- Modify: `docs/TUI.md`, `docs/PIPELINE.md`, `README.md`, `docs/screenshots/README.md`
- Create (generated): `docs/screenshots/m2-ab-junction-1.jpg`, `docs/screenshots/m2-ab-synthetic.jpg`, `docs/demo/m2-ab-junction-1.mp4` (only if ≤ 2 MB)

**Interfaces:**
- Consumes: `preview.build_preview`, `PreviewRequest`.

- [ ] **Step 1: `tools/demo_preview.py`** (run as `python tools/demo_preview.py WORKSPACE OUTDIR [--index N ...] [--stage S] [--pre P --post Q]`): for each 1-based index (default: every junction of the stage), build the A/B preview (`ab=True`), copy the played file to `OUTDIR/ab-junction-<N>.mp4` and write a contact sheet `OUTDIR/ab-junction-<N>.jpg` (ffmpeg `fps=4,scale=320:-1,tile=<cols>x<rows>` over the A/B file, cols = 8, rows = ceil(frames / 8), a single image), printing each path and the result's note. Same `sys.path` bootstrap as `tools/capture_edit.py`. Exit 1 with the message on `PreviewError`.

- [ ] **Step 2: Generate the artifacts.**
  - Synthetic: `python -m matchcut.pipeline.cli run <tmp>/demo.mp4 ...` is not needed — build a workspace with the mock backend in a temporary directory: `python -c` or a small script calling `make_demo_footage` and `Pipeline(cfg).run()` (`cfg.llm.backend = "mock"`, `cfg.render.enabled = False`, `target_duration = 8.0`), then `tools/demo_preview.py` on it for the first junction whose A/B differs (a junction with a dissolve in the final cut and a cut in the rough cut). Commit its contact sheet as `docs/screenshots/m2-ab-synthetic.jpg`.
  - Real footage: the controller's dispatch names a Big Buck Bunny workspace (CC BY 3.0, already credited in `docs/screenshots/README.md`) made with the mock backend. Run `tools/demo_preview.py` on junction 1 of it; commit the contact sheet as `docs/screenshots/m2-ab-junction-1.jpg`, and the A/B mp4 as `docs/demo/m2-ab-junction-1.mp4` only if it is ≤ 2 MB. Add both to the credits in `docs/screenshots/README.md` (say they come from a mock-backend run).
  - Look at both contact sheets (open the JPGs) and confirm: labels visible, A then black then B, the B side shows the transition the final cut has.

- [ ] **Step 3: `docs/M2-ACCEPTANCE.md`** with these sections:
  - **What M2 adds** (three sentences).
  - **Automated evidence**: the test files and what each proves (window and sub-timeline maths; hard cut colour switch at the cut; dissolve blend mid-transition; audio present/absent; cache reuse without ffmpeg; label fallback; A/B layout with the black gap; error messages leave no files; player selection; CLI; non-blocking UI and duplicate suppression; a real render from the Edit view).
  - **Demonstration**: the two contact sheets and the mp4 (if committed), with the commands that regenerate them.
  - **Human checklist** (needs a desktop with a video player; the real backend is optional for M2 but makes item 3 meaningful): (1) `p` on three junctions including a dissolve and a junction a pass moved; first render time and cached replay time noted; (2) the same record time in `final.mp4` looks and sounds the same as the preview (picture, transition, audio crossfade; music loudness may differ slightly — the spec's documented approximation); (3) `a` on a moved junction shows the change; (4) `←`/`→` stay responsive while a preview renders; (5) `preview.player` set to a nonexistent command gives the `could not start` message and the path; (6) renaming the source footage gives `preview failed: source footage not found: …`.
  - **Verdict** table: `Motion and timing judgeable from previews (yes/no, notes)`, `Preview matches final render (yes/no)`, `First render latency (s)`, `Decision: continue in Textual with external playback / build a separate review surface`.
  - **Status**: automated checks pass; the human checklist is **pending**; M1's own gate is still open and independent.
  - **Known limitations**: excerpt-level loudness normalisation and ducking; previews are not deleted automatically; one player window per request; the first render of a junction takes a few seconds (two ffmpeg passes); no in-terminal playback.

- [ ] **Step 4: Docs.** `docs/TUI.md` (Edit section): keys `p`/`a`, the status line wording, where files go, `preview.player`. `docs/PIPELINE.md`: the `preview` CLI command with a sample, the `preview` config section table, the cache and its key. `README.md`: one line under the TUI/Edit feature list pointing to M2.

- [ ] **Step 5: Run both full suites**, then commit (the tool, docs, generated images and the mp4 if ≤ 2 MB) and push (title: "Add the M2 demonstration previews, acceptance criteria and docs").
