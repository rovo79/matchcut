# M1 Junction Inspector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Cut tab with an Edit workbench where an editor selects any junction of the cut, sees the outgoing frame, the incoming frame and their overlay, the match scores at the first and the inspected stage, the cut-point deltas, why the junction is the way it is and how each pass changed it, and can lock or revert it with a pin that survives regenerating the cut.

**Architecture:** Two new pipeline modules carry the data and know nothing about the UI. `junctions.py` follows each junction (a pair of clip ids) through every stage timeline and rescores it with the same `MatchScorer` the match-cut pass used, rebuilt from the workspace's cached `analysis.npz`. `editorial.py` stores pins in `workspace/editorial.json`, keyed by shot pair, applies them to the rough cut before the passes run (locked ends, which every pass already honours) and turns them into rough-cut prompt constraints. The TUI gets a junction mode on the timeline track, a pure text renderer for the inspector panel, an image blend helper, and `EditView`, which replaces `CutView`. A `junctions` CLI command prints the same data as text.

**Tech Stack:** Python 3.9+, numpy, Pillow, Textual 8.x, textual-image, pytest, pytest-textual-snapshot; the mock backend and the synthetic fixture footage for tests.

**Spec:** `docs/superpowers/specs/2026-10-17-m1-junction-inspector.md` (binding). The control-room design it builds on: `docs/superpowers/specs/2026-10-16-tui-design.md`.

## Global Constraints

- Pipeline code lives in `matchcut/pipeline/` and must never import `textual`, `textual_image` or anything under `matchcut/tui/`. TUI code lives in `matchcut/tui/`. Run `python3 -m pytest tests/pipeline -q` (68 pass before this plan) and `python3 -m pytest tests/tui -q` (81 pass before this plan); both must pass after every task.
- Python 3.9 compatible syntax: every new module starts with `from __future__ import annotations`; `Optional[...]`, `List[...]`, `Dict[...]`, `Tuple[...]`, `Sequence[...]` from `typing`, never `X | None` at runtime.
- Textual 8 has no `Static.renderable`; read a Static's text with `str(widget.content)`.
- Every TUI view memoises its inputs with `file_fingerprint` and returns early from `refresh_from_state` when nothing changed; `refresh_views()` runs every 50 ms. The app clears focus when switching to a non-Run tab and refocuses the tab's primary widget through `_focus_if_active(pane_id, selector)`.
- A pin is keyed by `(out_shot, in_shot)`, never by clip ids. Pins are applied only in `Pipeline.refine`, before the first pass.
- No view may crash on a missing or corrupt workspace file; it shows an empty state.
- Never put a model identifier in code, docs or commit titles. Commit messages end with these two trailer lines, verbatim:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ
  ```
  committed with `git -c user.name="Claude" -c user.email="noreply@anthropic.com" commit -F <msgfile>` (message file written outside the repo, in `/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/`). Push with `git push -u origin claude/ai-video-editing-pipeline-bkqqay` after each task. Never stage anything under `.superpowers/`.

---

## File structure

| File | Responsibility |
|---|---|
| `matchcut/pipeline/junctions.py` (create) | stage timeline layout, `JunctionState`, `Junction`, `build_junctions`, `scorer_for_workspace`, text formatting for the CLI |
| `matchcut/pipeline/editorial.py` (create) | `Pin`, `PinStore`, `apply_pins`, `pin_constraints` |
| `matchcut/pipeline/context.py` (modify) | `constraints` field; rough-cut notes text carries them |
| `matchcut/pipeline/pipeline.py` (modify) | constraints before the rough cut; pins applied in `refine` |
| `matchcut/pipeline/cli.py` (modify) | `junctions` command |
| `matchcut/tui/state.py` (modify) | layout delegated to `junctions.stage_timeline_paths`; `skipped` stages; `is_mock`; `finished_label` |
| `matchcut/tui/app.py` (modify) | `status_text`; Edit tab replaces Cut |
| `matchcut/tui/views/run.py`, `widgets/ladder.py` (modify) | `(mock)` model lines; skipped stage label |
| `matchcut/tui/images.py` (modify) | `blend` |
| `matchcut/tui/widgets/timeline.py` (modify) | junction selection, `junction_columns`, click hit-testing |
| `matchcut/tui/widgets/inspector.py` (create) | `badges`, `render_inspector` |
| `matchcut/tui/views/edit.py` (create), `views/cut.py` (delete) | the Edit workbench |
| `matchcut/tui/styles.tcss` (modify) | Edit layout |
| `tools/capture_edit.py` (create) | headless SVG capture of the Edit tab for any workspace |
| `docs/TUI.md`, `docs/PIPELINE.md`, `docs/M1-ACCEPTANCE.md` (create), `docs/screenshots/` (create), `README.md` | docs and acceptance |
| `tests/pipeline/test_junctions.py`, `tests/pipeline/test_editorial.py`, `tests/tui/test_edit.py` (create); `tests/tui/test_state.py`, `test_app.py`, `test_widgets.py`, `test_views.py`, `test_snapshots.py` (modify) | tests |

---

### Task 1: Junction data layer and the `junctions` CLI command

**Files:**
- Create: `matchcut/pipeline/junctions.py`, `tests/pipeline/test_junctions.py`
- Modify: `matchcut/pipeline/cli.py`, `matchcut/tui/state.py` (`_timeline_entries` delegates)

**Interfaces:**
- Consumes: `Timeline`, `Clip`, `Transition`, `Decision` (`matchcut/pipeline/timeline.py`; `Decision(pass_name, action, clip_ids, detail, score=None, data={})`), `MatchScorer(cells, cfg.matchcut).score_cut(out_time, in_time) -> MatchScore` (`matching.py`; `MatchScore` fields `structure, composition, luminance, color, frame, motion, combined, jump_cut_risk, motion_type`), `Analysis.load(path).cells` (`features.py`), `PipelineConfig.load(path)`, `run_passes(tl, ctx, names, save_dir=...)`.
- Produces (later tasks rely on these exact names):
  - `stage_timeline_paths(ws: Path) -> List[Tuple[str, Path]]`, `load_stage_timelines(ws: Path) -> List[Tuple[str, Timeline]]`, `scorer_for_workspace(ws: Path, cfg: PipelineConfig) -> Optional[MatchScorer]`, `frames_delta(seconds: float, fps: float) -> str`, `SCORE_ROWS: Tuple[str, ...]`.
  - `JunctionState(stage, out_time, in_time, transition="cut", transition_seconds=0.0, locked_out=False, locked_in=False, score=None)` with `.locked` and `.change_from(prev, fps) -> str`.
  - `Junction(index, out_clip, in_clip, out_shot, in_shot, out_beat, in_beat, fps, states=[], decisions=[])` with `.beat_boundary`, `.baseline`, `.current`, `.state_at(stage)`, `.latest(*actions)`, `.match_type`, `.pinned`, `.why() -> str`, `.history() -> List[str]`.
  - `build_junctions(stages: Sequence[Tuple[str, Timeline]], scorer: Optional[MatchScorer] = None, upto: Optional[str] = None) -> List[Junction]`.
  - `junction_line(j) -> str`, `junction_detail(j) -> str`.
  - CLI: `python -m matchcut.pipeline.cli junctions WORKSPACE [--index N]` returns 0 on success, 1 when the workspace or its timelines are missing or N is out of range.

- [ ] **Step 1: Write the failing tests**

`tests/pipeline/test_junctions.py`:
```python
"""Junctions followed across stages, rescored with the match-cut scorer, and the CLI listing."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from matchcut.pipeline import cli
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.junctions import (
    build_junctions,
    frames_delta,
    load_stage_timelines,
    scorer_for_workspace,
    stage_timeline_paths,
)
from matchcut.pipeline.passes import run_passes
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Decision, Timeline, Transition


def clip(cid, shot, a, b, beat="b1", **attrs):
    c = Clip(cid, shot, a, b, beat)
    for k, v in attrs.items():
        setattr(c, k, v)
    return c


def tl(stage, clips, history=()):
    t = Timeline(source="src.mp4", fps=25.0, clips=list(clips), stage=stage)
    t.history = list(history)
    return t


MATCH = Decision("matchcut", "match_cut", ["c001", "c002"], "graphic match cut (trimmed to best junction): accepted on feature score alone", 0.74, {"match_type": "graphic"})
DISSOLVE = Decision("transitions", "dissolve", ["c002", "c003"], "beat b1 -> b2: dissolve 0.30s")


def three_stages():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0), clip("c003", 2, 6.0, 8.0, "b2")])
    mc = tl("matchcut", [clip("c001", 0, 0.0, 1.88, locked_out=True), clip("c002", 1, 3.12, 5.0, locked_in=True), clip("c003", 2, 6.0, 8.0, "b2")], [MATCH])
    final = tl(
        "final",
        [clip("c001", 0, 0.0, 1.88, locked_out=True), clip("c002", 1, 3.12, 5.0, locked_in=True), clip("c003", 2, 6.0, 8.0, "b2", transition_in=Transition("dissolve", 0.3))],
        [MATCH, DISSOLVE],
    )
    return [("roughcut", rough), ("matchcut", mc), ("final", final)]


def test_a_junction_is_followed_through_every_stage():
    first, second = build_junctions(three_stages())
    assert (first.out_clip, first.in_clip, second.out_clip, second.in_clip) == ("c001", "c002", "c002", "c003")
    assert [s.stage for s in first.states] == ["roughcut", "matchcut", "final"]
    assert first.baseline.out_time == 2.0 and first.current.out_time == 1.88
    assert first.current.locked and not first.baseline.locked
    assert first.history() == ["roughcut: cut at out 2.00s / in 3.00s", "matchcut: out -0.12s (-3f), in +0.12s (+3f), locked"]
    assert first.match_type == "graphic"
    assert first.why().startswith("matchcut: graphic match cut")
    assert not first.beat_boundary and second.beat_boundary
    assert second.history()[-1] == "final: dissolve 0.30s"
    assert second.why() == "transitions: beat b1 -> b2: dissolve 0.30s"
    assert first.state_at("matchcut").out_time == 1.88 and first.state_at("nope") is None


def test_a_junction_made_by_a_reorder_starts_where_it_first_exists():
    rough = tl("roughcut", [clip("c001", 0, 0, 2), clip("c002", 1, 3, 5), clip("c003", 2, 6, 8)])
    final = tl("final", [clip("c001", 0, 0, 2), clip("c003", 2, 6, 8), clip("c002", 1, 3, 5)])
    j = build_junctions([("roughcut", rough), ("final", final)])[0]
    assert [s.stage for s in j.states] == ["final"]
    assert j.history() == ["final: cut at out 2.00s / in 6.00s"]
    assert j.why() == "no pass changed this junction"


def test_upto_builds_the_junctions_of_an_earlier_stage():
    js = build_junctions(three_stages(), upto="roughcut")
    assert [(j.out_clip, j.in_clip) for j in js] == [("c001", "c002"), ("c002", "c003")]
    assert all([s.stage for s in j.states] == ["roughcut"] for j in js)
    assert build_junctions([]) == []


def test_frames_delta_formats_seconds_and_frames():
    assert frames_delta(-0.12, 25.0) == "-0.12s (-3f)"
    assert frames_delta(0.04, 25.0) == "+0.04s (+1f)"
    assert frames_delta(0.5, 0.0) == "+0.50s (+0f)"


def test_stage_layout_and_loading_skip_missing_and_corrupt_files(tmp_path):
    (tmp_path / "passes").mkdir()
    tl("roughcut", [clip("c001", 0, 0, 2)]).save(tmp_path / "roughcut.json")
    tl("matchcut", [clip("c001", 0, 0, 2)]).save(tmp_path / "passes" / "01_matchcut.json")
    (tmp_path / "passes" / "02_continuity.json").write_text("{not json")
    assert [s for s, _ in stage_timeline_paths(tmp_path)] == ["roughcut", "matchcut", "continuity", "final"]
    assert [s for s, _ in load_stage_timelines(tmp_path)] == ["roughcut", "matchcut"]
    assert scorer_for_workspace(tmp_path, PipelineConfig()) is None


# -- real footage: the scores shown are the scores the pass compared ---------------------------
@pytest.fixture(scope="module")
def mc_ctx(matchcut_video, tmp_path_factory):
    cfg = PipelineConfig(source=str(matchcut_video), workspace=str(tmp_path_factory.mktemp("ws")), target_duration=6.0)
    cfg.llm.backend = "mock"
    ctx = Pipeline(cfg).narrative()
    ctx.narrative = {
        "beats": [
            {"id": "b1", "title": "A", "pace": "medium", "target_duration_s": 3.0, "shot_ids": [0, 2], "transition_in": "cut"},
            {"id": "b2", "title": "B", "pace": "fast", "target_duration_s": 3.0, "shot_ids": [3, 4, 1], "transition_in": "dissolve"},
        ]
    }
    return ctx


def designed_rough_cut(ctx) -> Timeline:
    """Same layout as tests/pipeline/test_passes.py: c001 -> c002 is the designed match cut."""
    t = Timeline(source=ctx.media.path, fps=ctx.media.fps, stage="roughcut")
    t.clips = [
        Clip("c001", 0, 0.5, 2.5, "b1"),
        Clip("c002", 2, 4.5, 6.0, "b1"),
        Clip("c003", 3, 6.0, 8.0, "b2"),
        Clip("c004", 4, 8.0, 10.0, "b2"),
        Clip("c005", 1, 2.5, 4.5, "b2"),
    ]
    return t


def test_rescored_junctions_equal_what_the_matchcut_pass_recorded(mc_ctx, tmp_path):
    rough = designed_rough_cut(mc_ctx)
    run_passes(rough, mc_ctx, ["matchcut"], save_dir=tmp_path)
    stages = [("roughcut", rough), ("matchcut", Timeline.load(tmp_path / "01_matchcut.json"))]
    scorer = scorer_for_workspace(Path(mc_ctx.cfg.workspace), mc_ctx.cfg)
    assert scorer is not None
    matched = [j for j in build_junctions(stages, scorer) if j.latest("match_cut") is not None]
    assert matched, "the fixture is designed to produce a match cut at c001 -> c002"
    for j in matched:
        recorded = j.latest("match_cut")
        assert j.current.score.combined == pytest.approx(recorded.score, abs=1e-3)
        assert j.baseline.score.combined == pytest.approx(recorded.data["baseline"], abs=1e-3)


def test_cli_lists_and_details_junctions(mc_ctx, tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "passes").mkdir(parents=True)
    shutil.copy(Path(mc_ctx.cfg.workspace) / "analysis.npz", ws / "analysis.npz")
    mc_ctx.cfg.save(ws / "config.json")
    rough = designed_rough_cut(mc_ctx)
    rough.save(ws / "roughcut.json")
    run_passes(rough, mc_ctx, ["matchcut"], save_dir=ws / "passes")
    assert cli.main(["junctions", str(ws)]) == 0
    listing = capsys.readouterr().out
    assert "4 junctions in matchcut (2 stages)" in listing and "match:" in listing
    assert cli.main(["junctions", str(ws), "--index", "1"]) == 0
    detail = capsys.readouterr().out
    assert "why: matchcut:" in detail and "combined" in detail and "history:" in detail
    assert cli.main(["junctions", str(ws), "--index", "9"]) == 1
    assert cli.main(["junctions", str(tmp_path / "nope")]) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/pipeline/test_junctions.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'matchcut.pipeline.junctions'`.

- [ ] **Step 3: Write `matchcut/pipeline/junctions.py`**

```python
"""Junctions: the cut between two consecutive clips, followed through every stage of a cut.

A junction is identified by its two clip ids. Passes keep clip ids, so the same
junction can be traced from the rough cut through each pass to the final cut.
Scores come from the same MatchScorer the match-cut pass uses, rebuilt from the
workspace's cached analysis, so the numbers shown are the numbers the pass compared.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .config import PipelineConfig
from .features import Analysis
from .matching import MatchScore, MatchScorer
from .timeline import Decision, Timeline

SCORE_ROWS: Tuple[str, ...] = ("structure", "composition", "luminance", "color", "frame", "motion", "combined")


def stage_timeline_paths(ws: Path) -> List[Tuple[str, Path]]:
    """(stage, path) for every place a stage timeline can live, in stage order, present or not."""
    entries = [("roughcut", ws / "roughcut.json")]
    entries += [(p.stem.split("_", 1)[-1], p) for p in sorted((ws / "passes").glob("*.json"))]
    entries.append(("final", ws / "final.json"))
    return entries


def load_stage_timelines(ws: Path) -> List[Tuple[str, Timeline]]:
    stages: List[Tuple[str, Timeline]] = []
    for stage, path in stage_timeline_paths(ws):
        if not path.exists():
            continue
        try:
            stages.append((stage, Timeline.load(path)))
        except Exception:  # a half-written or hand-edited file: leave that stage out
            continue
    return stages


def scorer_for_workspace(ws: Path, cfg: PipelineConfig) -> Optional[MatchScorer]:
    path = ws / "analysis.npz"
    if not path.exists():
        return None
    try:
        return MatchScorer(Analysis.load(path).cells, cfg.matchcut)
    except Exception:
        return None


def frames_delta(seconds: float, fps: float) -> str:
    frames = int(round(seconds * fps)) if fps > 0 else 0
    return f"{seconds:+.2f}s ({frames:+d}f)"


@dataclass
class JunctionState:
    stage: str
    out_time: float
    in_time: float
    transition: str = "cut"
    transition_seconds: float = 0.0
    locked_out: bool = False
    locked_in: bool = False
    score: Optional[MatchScore] = None

    @property
    def locked(self) -> bool:
        return self.locked_out and self.locked_in

    def change_from(self, prev: "JunctionState", fps: float) -> str:
        parts: List[str] = []
        if abs(self.out_time - prev.out_time) > 1e-4:
            parts.append(f"out {frames_delta(self.out_time - prev.out_time, fps)}")
        if abs(self.in_time - prev.in_time) > 1e-4:
            parts.append(f"in {frames_delta(self.in_time - prev.in_time, fps)}")
        if (self.transition, round(self.transition_seconds, 3)) != (prev.transition, round(prev.transition_seconds, 3)):
            parts.append(self.transition if self.transition == "cut" else f"{self.transition} {self.transition_seconds:.2f}s")
        if self.locked and not prev.locked:
            parts.append("locked")
        return ", ".join(parts)


@dataclass
class Junction:
    index: int  # junction j sits between clip j and clip j+1 of the stage it was built from
    out_clip: str
    in_clip: str
    out_shot: int
    in_shot: int
    out_beat: str
    in_beat: str
    fps: float
    states: List[JunctionState] = field(default_factory=list)  # stage order; only stages where the clips are adjacent
    decisions: List[Decision] = field(default_factory=list)  # every decision naming both clips

    @property
    def beat_boundary(self) -> bool:
        return self.out_beat != self.in_beat

    @property
    def baseline(self) -> JunctionState:
        return self.states[0]

    @property
    def current(self) -> JunctionState:
        return self.states[-1]

    def state_at(self, stage: str) -> Optional[JunctionState]:
        return next((s for s in self.states if s.stage == stage), None)

    def latest(self, *actions: str) -> Optional[Decision]:
        for d in reversed(self.decisions):
            if not actions or d.action in actions:
                return d
        return None

    @property
    def match_type(self) -> str:
        d = self.latest("match_cut")
        return str(d.data.get("match_type") or "") if d is not None else ""

    @property
    def pinned(self) -> bool:
        return any(d.pass_name == "editor" and d.action == "pinned" for d in self.decisions)

    def why(self) -> str:
        d = (
            next((x for x in reversed(self.decisions) if x.pass_name == "editor"), None)
            or self.latest("match_cut", "rejected", "jump_cut_risk")
            or self.latest()
        )
        return f"{d.pass_name}: {d.detail}" if d is not None else "no pass changed this junction"

    def history(self) -> List[str]:
        b = self.baseline
        lines = [f"{b.stage}: {b.transition} at out {b.out_time:.2f}s / in {b.in_time:.2f}s"]
        for prev, cur in zip(self.states, self.states[1:]):
            change = cur.change_from(prev, self.fps)
            if change:
                lines.append(f"{cur.stage}: {change}")
        return lines


def _state(stage: str, timeline: Timeline, i: int, scorer: Optional[MatchScorer]) -> JunctionState:
    a, b = timeline.clips[i], timeline.clips[i + 1]
    score: Optional[MatchScore] = None
    if scorer is not None:
        try:
            score = scorer.score_cut(a.source_out, b.source_in)
        except Exception:
            score = None
    return JunctionState(stage, a.source_out, b.source_in, b.transition_in.kind, b.transition_in.duration, a.locked_out, b.locked_in, score)


def build_junctions(stages: Sequence[Tuple[str, Timeline]], scorer: Optional[MatchScorer] = None, upto: Optional[str] = None) -> List[Junction]:
    """Junctions of stage ``upto`` (default: the last stage), each followed through the stages up to it."""
    if not stages:
        return []
    names = [s for s, _ in stages]
    end = names.index(upto) if upto in names else len(stages) - 1
    considered = list(stages[: end + 1])
    ref = considered[-1][1]
    out: List[Junction] = []
    for i in range(len(ref.clips) - 1):
        a, b = ref.clips[i], ref.clips[i + 1]
        j = Junction(i, a.id, b.id, a.shot_index, b.shot_index, a.beat_id, b.beat_id, ref.fps)
        for stage, timeline in considered:
            ids = [c.id for c in timeline.clips]
            if a.id in ids and b.id in ids and ids.index(b.id) == ids.index(a.id) + 1:
                j.states.append(_state(stage, timeline, ids.index(a.id), scorer))
        j.decisions = [d for d in ref.history if a.id in d.clip_ids and b.id in d.clip_ids]
        out.append(j)
    return out


def junction_line(j: Junction) -> str:
    cur = j.current
    score = f"{cur.score.combined:.2f}" if cur.score is not None else "  - "
    tags = ([f"match:{j.match_type}"] if j.match_type else []) + (["locked"] if cur.locked else []) + (["pinned"] if j.pinned else []) + (["beat"] if j.beat_boundary else [])
    return (
        f"{j.index + 1:>3}  {j.out_clip}->{j.in_clip}  s{j.out_shot}->s{j.in_shot}  "
        f"out {cur.out_time:7.2f}  in {cur.in_time:7.2f}  {cur.transition:<12} score {score}  {' '.join(tags)}"
    ).rstrip()


def junction_detail(j: Junction) -> str:
    lines = [junction_line(j), f"why: {j.why()}", "history:"] + [f"  {h}" for h in j.history()]
    b, c = j.baseline, j.current
    if b.score is not None and c.score is not None:
        lines.append(f"scores ({b.stage} -> {c.stage}):")
        lines += [f"  {name:<12} {getattr(b.score, name):.3f} -> {getattr(c.score, name):.3f}" for name in SCORE_ROWS]
    lines.append("decisions:")
    lines += [f"  {d.pass_name}:{d.action}  {d.detail}" for d in j.decisions] or ["  (none)"]
    return "\n".join(lines)
```

- [ ] **Step 4: Add the CLI command**

In `matchcut/pipeline/cli.py`:
- Import: `from .junctions import build_junctions, junction_detail, junction_line, load_stage_timelines, scorer_for_workspace` (and `Optional` from typing if not already imported; `PipelineConfig` is already imported for `build_config`, confirm).
- In `parse_args`, after the loop that adds the stage subcommands and before `return parser.parse_args(argv)`:
```python
    sp = sub.add_parser("junctions", help="list a workspace's junctions, or explain one with --index")
    sp.add_argument("workspace")
    sp.add_argument("--index", type=int, default=None, help="1-based junction number to explain")
```
- At the top of `main`, right after `args = parse_args(argv)` (before logging setup, because this command has no `-v`):
```python
    if args.command == "junctions":
        return _junctions(Path(args.workspace), args.index)
```
- Add the function above `main`:
```python
def _junctions(ws: Path, index: Optional[int]) -> int:
    cfg_path = ws / "config.json"
    if not cfg_path.exists():
        print(f"no workspace at {ws} (config.json missing)", file=sys.stderr)
        return 1
    cfg = PipelineConfig.load(cfg_path)
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
```

- [ ] **Step 5: Make the TUI use the one layout definition**

In `matchcut/tui/state.py`, import `from matchcut.pipeline.junctions import stage_timeline_paths` and replace the body of `RunState._timeline_entries` with:
```python
        return stage_timeline_paths(self.workspace)
```
(keep its docstring; `timeline_files`, `_timeline_path` and `timeline_stages` stay as they are).

- [ ] **Step 6: Run the tests**

Run: `python3 -m pytest tests/pipeline/test_junctions.py -q` (8 passed), then `python3 -m pytest tests/pipeline -q` (76 passed) and `python3 -m pytest tests/tui -q` (81 passed). Confirm the pipeline stays Textual-free: `python3 -c "import sys, matchcut.pipeline.cli, matchcut.pipeline.junctions; assert 'textual' not in sys.modules; print('ok')"`.

- [ ] **Step 7: Commit**

Message: `Follow every junction across the stages and rescore it; add the junctions command` plus the trailers. Stage `matchcut/pipeline/junctions.py matchcut/pipeline/cli.py matchcut/tui/state.py tests/pipeline/test_junctions.py`. Push.

---

### Task 2: Editorial pins that survive regeneration

**Files:**
- Create: `matchcut/pipeline/editorial.py`, `tests/pipeline/test_editorial.py`
- Modify: `matchcut/pipeline/context.py`, `matchcut/pipeline/pipeline.py`

**Interfaces:**
- Consumes: `Timeline.copy()`, `Clip` fields (`source_in`, `source_out`, `locked_in`, `locked_out`, `tags`, `shot_index`, `id`), `Decision`, `Context.notes_text(stage)`, `Context.shot_bounds -> Dict[int, (start, end)]`, `Pipeline.rough_cut`, `Pipeline.refine`, `Pipeline._emit`, `Pipeline.invalidate_from(stage)`, `events.Decision(pass_name, action, clip_ids, detail, score, data)`, `tests.pipeline.conftest.RecordingMock` (records prompt text per request name in `.texts`; the rough-cut request name is `rough_cut`).
- Produces:
  - `Pin(out_shot: int, in_shot: int, out_time: float, in_time: float, origin: str = "locked", note: str = "")` with `.key -> (out_shot, in_shot)`, `to_dict()`, `from_dict(d)`. `origin` is `"locked"` (keep the inspected points) or `"reverted"` (back to the junction's first-stage points).
  - `PinStore(ws: Path)` with `.path` (`ws / "editorial.json"`), `load() -> List[Pin]`, `get(out_shot, in_shot) -> Optional[Pin]`, `put(pin)` (replaces a pin with the same key in place, else appends), `remove(out_shot, in_shot) -> bool`.
  - `apply_pins(tl: Timeline, pins: Sequence[Pin], bounds: Dict[int, Tuple[float, float]], min_clip: float) -> Tuple[Timeline, List[Decision]]` (returns a copy; decisions use `pass_name="editor"`, actions `pinned` or `pin_unplaced`, and are appended to the copy's history).
  - `pin_constraints(pins) -> List[str]`.
  - `Context.constraints: List[str]` (rough-cut prompt only).

- [ ] **Step 1: Write the failing tests**

`tests/pipeline/test_editorial.py`:
```python
"""Editorial pins: storage, application before the passes, prompt constraints, regeneration."""
from __future__ import annotations

from pathlib import Path

from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import Pin, PinStore, apply_pins, pin_constraints
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.timeline import Clip, Timeline
from tests.pipeline.conftest import RecordingMock

BOUNDS = {0: (0.0, 2.5), 1: (2.5, 4.5), 2: (4.5, 6.0)}


def cut() -> Timeline:
    return Timeline(source="s.mp4", fps=25.0, clips=[Clip("c001", 0, 0.5, 2.0, "b1"), Clip("c002", 2, 4.6, 5.8, "b1"), Clip("c003", 1, 2.6, 4.0, "b2")])


def test_pin_store_round_trip_replace_and_remove(tmp_path):
    store = PinStore(tmp_path)
    assert store.load() == [] and store.get(0, 2) is None
    store.put(Pin(0, 2, 2.0, 4.6))
    store.put(Pin(3, 4, 7.5, 8.2))
    store.put(Pin(0, 2, 1.9, 4.7, origin="reverted"))
    pins = PinStore(tmp_path).load()
    assert [(p.out_shot, p.in_shot, p.out_time, p.origin) for p in pins] == [(0, 2, 1.9, "reverted"), (3, 4, 7.5, "locked")]
    assert store.get(3, 4).in_time == 8.2
    assert store.remove(0, 2) is True and store.remove(0, 2) is False
    assert [p.key for p in store.load()] == [(3, 4)]


def test_pin_store_tolerates_a_corrupt_file(tmp_path):
    (tmp_path / "editorial.json").write_text("{oops")
    assert PinStore(tmp_path).load() == []


def test_apply_pins_sets_locks_and_records_the_junction():
    source = cut()
    out, decisions = apply_pins(source, [Pin(0, 2, 2.2, 4.8)], BOUNDS, min_clip=0.75)
    a, b = out.clips[0], out.clips[1]
    assert (a.source_out, b.source_in) == (2.2, 4.8)
    assert a.locked_out and b.locked_in and "pinned" in a.tags and "pinned" in b.tags
    assert [(d.pass_name, d.action, d.clip_ids) for d in decisions] == [("editor", "pinned", ["c001", "c002"])]
    assert out.history[-1].action == "pinned"
    assert source.clips[0].source_out == 2.0 and not source.clips[0].locked_out  # the input is untouched


def test_apply_pins_clamps_to_the_shots_and_the_minimum_clip():
    out, _ = apply_pins(cut(), [Pin(0, 2, 3.0, 4.0)], BOUNDS, min_clip=0.75)
    assert (out.clips[0].source_out, out.clips[1].source_in) == (2.5, 4.5)
    out, _ = apply_pins(cut(), [Pin(0, 2, 0.9, 5.5)], BOUNDS, min_clip=0.75)
    assert (out.clips[0].source_out, out.clips[1].source_in) == (1.25, 5.05)


def test_a_pin_whose_shots_are_not_adjacent_is_reported_not_applied():
    out, decisions = apply_pins(cut(), [Pin(1, 0, 3.0, 1.0)], BOUNDS, min_clip=0.75)
    assert [d.action for d in decisions] == ["pin_unplaced"]
    assert "not adjacent" in decisions[0].detail
    assert [(c.source_in, c.source_out) for c in out.clips] == [(c.source_in, c.source_out) for c in cut().clips]


def test_pin_constraints_name_the_shot_pairs():
    assert pin_constraints([Pin(0, 2, 2.0, 4.6)]) == ["keep shot 0 immediately followed by shot 2 (an editor-locked junction)"]


def mock_cfg(video, ws: Path) -> PipelineConfig:
    cfg = PipelineConfig(source=str(video), workspace=str(ws), target_duration=6.0)
    cfg.llm.backend = "mock"
    cfg.render.enabled = False
    return cfg


def test_pins_reach_the_rough_cut_prompt_only(matchcut_video, tmp_path):
    cfg = mock_cfg(matchcut_video, tmp_path / "ws")
    pipe = Pipeline(cfg)
    PinStore(Path(cfg.workspace)).put(Pin(0, 2, 2.0, 4.6))
    mock = RecordingMock(cfg.llm)
    pipe.llm = mock
    pipe.rough_cut()
    assert "Editor constraints" in mock.texts["rough_cut"][-1]
    assert "keep shot 0 immediately followed by shot 2" in mock.texts["rough_cut"][-1]
    assert "Editor constraints" not in mock.texts["narrative"][-1]


def test_pins_survive_regenerating_the_rough_cut(matchcut_video, tmp_path):
    cfg = mock_cfg(matchcut_video, tmp_path / "ws")
    ws = Path(cfg.workspace)
    Pipeline(cfg).run()
    rough = Timeline.load(ws / "roughcut.json")
    room = 0.2 + cfg.pacing.min_clip_seconds
    pair = next(((a, b) for a, b in zip(rough.clips, rough.clips[1:]) if a.shot_index != b.shot_index and a.duration >= room and b.duration >= room), None)
    assert pair is not None, "the mock rough cut of the fixture should have a junction with room to move"
    a, b = pair
    pin = Pin(a.shot_index, b.shot_index, round(a.source_out - 0.2, 2), round(b.source_in + 0.2, 2))
    PinStore(ws).put(pin)
    Pipeline(cfg).invalidate_from("roughcut")  # regenerate: the rough cut and everything after it
    assert not (ws / "roughcut.json").exists()
    Pipeline(cfg).run()
    final = Timeline.load(ws / "final.json")
    pairs = [(x, y) for x, y in zip(final.clips, final.clips[1:]) if (x.shot_index, y.shot_index) == pin.key]
    assert len(pairs) == 1
    x, y = pairs[0]
    tolerance = 1.0 / final.fps + 1e-6
    assert x.locked_out and y.locked_in
    assert abs(x.source_out - pin.out_time) <= tolerance and abs(y.source_in - pin.in_time) <= tolerance
    assert any(d.pass_name == "editor" and d.action == "pinned" for d in final.history)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/pipeline/test_editorial.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'matchcut.pipeline.editorial'`.

- [ ] **Step 3: Write `matchcut/pipeline/editorial.py`**

```python
"""Editorial pins: junctions the editor has locked or reverted, kept across regenerations.

A pin names a junction by its two shots (clip ids change when a cut is
regenerated; shots do not) and stores the cut points to hold. Pins are applied
to the rough cut before any pass runs: both ends of the junction are locked,
and every pass already leaves locked ends alone.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .timeline import Decision, Timeline


@dataclass
class Pin:
    out_shot: int
    in_shot: int
    out_time: float
    in_time: float
    origin: str = "locked"  # "locked": keep the inspected points; "reverted": back to the first cut's points
    note: str = ""

    @property
    def key(self) -> Tuple[int, int]:
        return (self.out_shot, self.in_shot)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Pin":
        return cls(int(d["out_shot"]), int(d["in_shot"]), float(d["out_time"]), float(d["in_time"]), str(d.get("origin", "locked")), str(d.get("note", "")))


class PinStore:
    """``workspace/editorial.json``: {"pins": [...]}, written atomically."""

    def __init__(self, ws: Path):
        self.path = Path(ws) / "editorial.json"

    def load(self) -> List[Pin]:
        try:
            data = json.loads(self.path.read_text())
            return [Pin.from_dict(d) for d in data.get("pins", [])]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return []

    def _save(self, pins: Sequence[Pin]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"pins": [p.to_dict() for p in pins]}, indent=2))
        os.replace(tmp, self.path)

    def get(self, out_shot: int, in_shot: int) -> Optional[Pin]:
        return next((p for p in self.load() if p.key == (out_shot, in_shot)), None)

    def put(self, pin: Pin) -> None:
        pins = self.load()
        for i, p in enumerate(pins):
            if p.key == pin.key:
                pins[i] = pin
                break
        else:
            pins.append(pin)
        self._save(pins)

    def remove(self, out_shot: int, in_shot: int) -> bool:
        pins = self.load()
        kept = [p for p in pins if p.key != (out_shot, in_shot)]
        if len(kept) == len(pins):
            return False
        self._save(kept)
        return True


def _snap(t: float, fps: float) -> float:
    return round(round(t * fps) / fps, 3) if fps > 0 else round(t, 3)


def apply_pins(tl: Timeline, pins: Sequence[Pin], bounds: Dict[int, Tuple[float, float]], min_clip: float) -> Tuple[Timeline, List[Decision]]:
    """A copy of ``tl`` with every placeable pin applied and locked, plus the decisions taken."""
    out = tl.copy()
    decisions: List[Decision] = []
    for pin in pins:
        pair = next(((a, b) for a, b in zip(out.clips, out.clips[1:]) if (a.shot_index, b.shot_index) == pin.key), None)
        if pair is None:
            decisions.append(Decision("editor", "pin_unplaced", [], f"shot {pin.out_shot} -> shot {pin.in_shot} are not adjacent in this cut", None, pin.to_dict()))
            continue
        a, b = pair
        a_lo = a.source_in + min_clip
        a_hi = bounds.get(a.shot_index, (a.source_in, a.source_out))[1]
        b_lo = bounds.get(b.shot_index, (b.source_in, b.source_out))[0]
        b_hi = b.source_out - min_clip
        if a_lo > a_hi or b_lo > b_hi:
            decisions.append(Decision("editor", "pin_unplaced", [a.id, b.id], "not enough room in these clips for the pinned points", None, pin.to_dict()))
            continue
        out_t = min(max(_snap(pin.out_time, out.fps), a_lo), a_hi)
        in_t = min(max(_snap(pin.in_time, out.fps), b_lo), b_hi)
        a.source_out, b.source_in = round(out_t, 3), round(in_t, 3)
        a.locked_out = b.locked_in = True
        for c in (a, b):
            if "pinned" not in c.tags:
                c.tags.append("pinned")
        detail = f"{pin.origin} by the editor: out {a.source_out:.2f}s, in {b.source_in:.2f}s"
        decisions.append(Decision("editor", "pinned", [a.id, b.id], detail, None, dict(pin.to_dict(), out=a.source_out, in_=b.source_in)))
    out.history.extend(decisions)
    return out, decisions


def pin_constraints(pins: Sequence[Pin]) -> List[str]:
    return [f"keep shot {p.out_shot} immediately followed by shot {p.in_shot} (an editor-locked junction)" for p in pins]
```

- [ ] **Step 4: Carry constraints in the rough-cut prompt**

In `matchcut/pipeline/context.py`, add a field next to `notes` on the `Context` dataclass:
```python
    constraints: List[str] = field(default_factory=list)  # editor constraints for the rough cut (from pins)
```
and replace `notes_text` with:
```python
    def notes_text(self, stage: str) -> str:
        """Prompt paragraph with the director's notes recorded for ``stage`` (plus editor constraints for the rough cut)."""
        notes = self.notes.get(stage, [])
        text = "Director's notes: (none)" if not notes else "Director's notes:\n" + "\n".join(f"- {n}" for n in notes)
        if stage == "roughcut" and self.constraints:
            text += "\nEditor constraints (must hold):\n" + "\n".join(f"- {c}" for c in self.constraints)
        return text
```
(`List` must be imported from `typing` in that module; check.)

- [ ] **Step 5: Apply pins in the pipeline**

In `matchcut/pipeline/pipeline.py`, import `from . import editorial`. In `rough_cut`'s inner `_do`, immediately before `tl = stages.rough_cut(ctx)`:
```python
            ctx.constraints = editorial.pin_constraints(editorial.PinStore(self.ws).load())
```
In `refine`, immediately after `tl = timeline or self.rough_cut()`:
```python
        pins = editorial.PinStore(self.ws).load()
        if pins:
            tl, pin_decisions = editorial.apply_pins(tl, pins, ctx.shot_bounds, self.cfg.pacing.min_clip_seconds)
            for d in pin_decisions:
                log.info("editor: %s %s", d.action, d.detail)
                self._emit(events.Decision(pass_name=d.pass_name, action=d.action, clip_ids=list(d.clip_ids), detail=d.detail, score=d.score, data=dict(d.data)))
```
(`events` and `log` are already imported in that module; confirm.)

- [ ] **Step 6: Run the tests**

Run: `python3 -m pytest tests/pipeline/test_editorial.py -q` (9 passed), then `python3 -m pytest tests/pipeline -q` (85 passed) and `python3 -m pytest tests/tui -q` (81 passed).

- [ ] **Step 7: Commit**

Message: `Add editorial pins: lock or revert a junction and keep it when the cut is regenerated` plus the trailers. Stage `matchcut/pipeline/editorial.py matchcut/pipeline/context.py matchcut/pipeline/pipeline.py tests/pipeline/test_editorial.py`. Push.

---

### Task 3: Honest status — mock runs and skipped stages

**Files:**
- Modify: `matchcut/tui/state.py`, `matchcut/tui/app.py`, `matchcut/tui/views/run.py`, `matchcut/tui/widgets/ladder.py`
- Test: `tests/tui/test_state.py`, `tests/tui/test_app.py`

**Interfaces:**
- Consumes: `events.RunStarted`, `StageStarted`, `StageFinished`, `RunFinished(final_path, render_path, report_path, seconds)`, `ModelCall` (check the exact field names in `matchcut/pipeline/events.py` before writing the tests).
- Produces: `StageStatus.note: str = ""`; state value `"skipped"`; `RunState.render_path: str`; `RunState.is_mock -> bool`; `RunState.finished_label() -> str` (`"finished · rendered <file name>"` or `"finished · analysis only"`); module function `app.status_text(state: RunState) -> rich.text.Text` used by `_update_status`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/tui/test_state.py` (reuse that file's existing config helper if it has one; otherwise build `PipelineConfig(source="x.mp4", workspace=str(tmp_path))`):
```python
def finished_run(cfg, render_path=""):
    from matchcut.pipeline import events
    from matchcut.tui.state import RunState

    st = RunState(cfg)
    st.apply(events.RunStarted(source=cfg.source, workspace=cfg.workspace, config={}))
    for name in [n for n in st.stage_names() if n != "render"]:
        st.apply(events.StageStarted(stage=name))
        st.apply(events.StageFinished(stage=name, seconds=0.1, path=""))
    st.apply(events.RunFinished(final_path="final.json", render_path=render_path, report_path="report.md", seconds=1.0))
    return st


def test_a_finished_run_marks_unreached_stages_skipped(tmp_path):
    from matchcut.pipeline.config import PipelineConfig

    cfg = PipelineConfig(source="x.mp4", workspace=str(tmp_path))
    cfg.render.enabled = False
    st = finished_run(cfg)
    assert st.stages["render"].state == "skipped" and st.stages["render"].note == "render disabled"
    assert st.stages["report"].state == "done"
    assert st.finished_label() == "finished · analysis only"


def test_a_rendered_run_names_its_file(tmp_path):
    from matchcut.pipeline.config import PipelineConfig

    st = finished_run(PipelineConfig(source="x.mp4", workspace=str(tmp_path)), render_path=str(tmp_path / "final.mp4"))
    assert st.finished_label() == "finished · rendered final.mp4"


def test_is_mock_follows_the_backend(tmp_path):
    from matchcut.pipeline.config import PipelineConfig
    from matchcut.tui.state import RunState

    cfg = PipelineConfig(source="x.mp4", workspace=str(tmp_path))
    cfg.llm.backend = "mock"
    assert RunState(cfg).is_mock
    cfg.llm.backend = "anthropic"
    assert not RunState(cfg).is_mock
```
Append to `tests/tui/test_app.py`:
```python
def test_status_text_labels_mock_runs_and_prices_real_ones(tmp_path):
    from matchcut.pipeline.config import PipelineConfig
    from matchcut.tui.app import status_text
    from matchcut.tui.state import RunState

    cfg = PipelineConfig(source="/footage/demo.mp4", workspace=str(tmp_path))
    cfg.llm.backend = "mock"
    mock = status_text(RunState(cfg)).plain
    assert mock.startswith("MOCK") and "mock backend" in mock and "~$" not in mock and "tok" not in mock
    cfg.llm.backend = "anthropic"
    real = status_text(RunState(cfg)).plain
    assert "MOCK" not in real and "0/0 tok" in real and "~$0.000" in real and "demo.mp4" in real
```
Also search `tests/tui/test_app.py` for assertions on the status bar of a mock run that expect `tok` or `~$` (`grep -n '~\$\|tok' tests/tui/test_app.py`) and change them to expect `MOCK`; a finished mock run's status must contain `finished · analysis only`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/tui/test_state.py tests/tui/test_app.py -q -k "skipped or rendered or is_mock or status_text"`
Expected: failures (`AttributeError: ... 'note'`, `ImportError: cannot import name 'status_text'`).

- [ ] **Step 3: Implement**

`matchcut/tui/state.py`:
- `StageStatus` gains `note: str = ""` (last field).
- `RunState.__init__` gains `self.render_path = ""`.
- In `apply`, the `RunStarted` branch also sets `self.render_path = ""`; the `RunFinished` branch becomes:
```python
        elif isinstance(event, events.RunFinished):
            self.run_status = "finished"
            self.finished_at = time.time()
            self.checkpoint = None
            self.render_path = event.render_path or ""
            for s in self.stages.values():
                if s.state == "pending":
                    s.state = "skipped"
                    s.note = "render disabled" if s.name == "render" and not self.config.render.enabled else "not run"
```
- New members:
```python
    @property
    def is_mock(self) -> bool:
        return self.config.llm.backend == "mock"

    def finished_label(self) -> str:
        if self.render_path:
            return f"finished · rendered {Path(self.render_path).name}"
        return "finished · analysis only"
```

`matchcut/tui/widgets/ladder.py`: add `"skipped": "dim italic"` to `STATE_STYLES`, and build the label as `f"skipped: {s.note}" if s.state == "skipped" and s.note else (s.state if not s.error else f"{s.state}: {s.error[:40]}")`.

`matchcut/tui/app.py`: add `from rich.text import Text` and a module function, and make `_update_status` call it (`status.first(Static).update(status_text(self.state))`):
```python
def status_text(st: RunState) -> Text:
    source = st.config.source.rsplit("/", 1)[-1]
    running = st.checkpoint.stage if st.checkpoint else next((n for n, s in st.stages.items() if s.state in ("running", "stopping")), "-")
    status = st.finished_label() if st.run_status == "finished" else st.run_status
    usage = f"{st.calls} calls  mock backend" if st.is_mock else f"{st.calls} calls  {st.input_tokens}/{st.output_tokens} tok  ~${st.cost_estimate():.3f}"
    text = Text()
    if st.is_mock:
        text.append("MOCK", style="bold black on yellow")
        text.append("  ")
    text.append(f"{source}  |  {status}  |  stage {running}  |  {st.elapsed():6.1f}s  |  {usage}")
    if st.image_mode:
        text.append(f"  |  images: {st.image_mode}")
    return text
```

`matchcut/tui/views/run.py`: in `refresh_from_state`, before formatting events, set `self._mock = state.is_mock` (initialise `self._mock = False` in `__init__`); in `_format`'s `ModelCall` branch append `" (mock)"` to the line when `self._mock` is true.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/tui -q` (all pass; the count grows by 4). Snapshots: the status bar of every snapshot changes (it now starts with `MOCK`); regenerate with `python3 -m pytest tests/tui/test_snapshots.py --snapshot-update -q`, then run `python3 -m pytest tests/tui/test_snapshots.py -q` twice without the flag to confirm determinism.

- [ ] **Step 5: Commit**

Message: `Label mock runs and skipped stages honestly in the control room` plus the trailers. Stage the four source files, the two test files and `tests/tui/__snapshots__/`. Push.

---

### Task 4: Inspector building blocks — blend, junction track, inspector text

**Files:**
- Modify: `matchcut/tui/images.py`, `matchcut/tui/widgets/timeline.py`
- Create: `matchcut/tui/widgets/inspector.py`
- Test: `tests/tui/test_widgets.py`

**Interfaces:**
- Consumes: Task 1's `Junction`, `JunctionState`, `SCORE_ROWS`, `frames_delta`; Task 2's `Pin`; `MatchScore`; the existing `render_track` and `TimelineTrack`.
- Produces:
  - `images.blend(a: PIL.Image.Image, b: PIL.Image.Image, alpha: float = 0.5) -> PIL.Image.Image` (RGB, size of `a`).
  - `widgets.timeline.junction_columns(tl: Timeline, width: int, center: int) -> Dict[int, int]` (column of each visible junction glyph; junction `j` sits between clip `j` and `j+1`).
  - `render_track(..., selected_junction: Optional[int] = None)`: when given, the window centres on that junction, no clip is highlighted, and that junction's glyph is styled `bold black on yellow`.
  - `TimelineTrack.selected_junction: Optional[int]` (default None = clip mode, as today), `TimelineTrack.set_junction(j: Optional[int])` (clamped to `0..len(clips)-2`), message `TimelineTrack.JunctionClicked(index)` posted when a click lands within one column of a junction glyph while in junction mode.
  - `widgets.inspector.badges(j, shown, pin) -> List[str]` and `widgets.inspector.render_inspector(j: Junction, stage: str, pin: Optional[Pin], expanded: bool = False) -> rich.text.Text`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/tui/test_widgets.py`:
```python
from matchcut.pipeline.editorial import Pin
from matchcut.pipeline.junctions import Junction, JunctionState
from matchcut.pipeline.matching import MatchScore
from matchcut.pipeline.timeline import Decision
from matchcut.tui.widgets.inspector import render_inspector
from matchcut.tui.widgets.timeline import junction_columns


def test_blend_is_the_average_of_the_two_frames_at_the_first_size():
    a = PILImage.new("RGB", (40, 20), (200, 0, 0))
    b = PILImage.new("RGB", (80, 40), (0, 0, 200))
    out = images.blend(a, b)
    assert out.size == (40, 20) and out.mode == "RGB"
    assert out.getpixel((5, 5)) == (100, 0, 100)


def five_clips():
    tl = Timeline(source="s.mp4", fps=25.0)
    tl.clips = [Clip(f"c00{i}", i, 0.0, 1.0 + i * 0.1, "b1" if i < 3 else "b2") for i in range(1, 6)]
    return tl


def test_junction_columns_sit_between_the_spans():
    tl = five_clips()
    cols = junction_columns(tl, 60, center=0)
    assert sorted(cols) == [0, 1, 2, 3]
    plain = render_track(tl, 60, selected=-1, selected_junction=2).plain
    for j, col in cols.items():
        assert plain[col] in "|~#L"


def test_the_selected_junction_glyph_is_highlighted_and_no_clip_is():
    text = render_track(five_clips(), 60, selected=0, selected_junction=1)
    col = junction_columns(five_clips(), 60, center=1)[1]
    styles = [str(span.style) for span in text.spans if span.start <= col < span.end]
    assert any("yellow" in s for s in styles)
    assert not any("reverse" in str(span.style) for span in text.spans)


class TrackApp(App):
    def __init__(self):
        super().__init__()
        self.clicked = []

    def compose(self) -> ComposeResult:
        yield TimelineTrack(id="t")

    def on_timeline_track_junction_clicked(self, message):
        self.clicked.append(message.index)


def test_clicking_a_junction_glyph_posts_its_index():
    async def body():
        app = TrackApp()
        async with app.run_test(size=(60, 6)) as pilot:
            track = app.query_one("#t", TimelineTrack)
            track.set_timeline(five_clips())
            track.set_junction(0)
            await pilot.pause()
            col = junction_columns(five_clips(), track.size.width, center=0)[2]
            await pilot.click("#t", offset=(col, 0))
            await pilot.pause()
            assert app.clicked == [2]
            track.set_junction(99)
            assert track.selected_junction == 3

    run_async(body())


def inspected_junction() -> Junction:
    s0 = MatchScore(0.41, 0.38, 0.50, 0.60, 0.45, 0.40, 0.44, False, "static")
    s1 = MatchScore(0.63, 0.71, 0.55, 0.62, 0.66, 0.75, 0.69, False, "moving")
    j = Junction(1, "c002", "c003", 1, 3, "b1", "b2", 25.0)
    j.states = [
        JunctionState("roughcut", 5.0, 7.0, score=s0),
        JunctionState("matchcut", 4.88, 7.12, locked_out=True, locked_in=True, score=s1),
        JunctionState("final", 4.88, 7.12, "cut", 0.0, True, True, s1),
    ]
    j.decisions = [Decision("matchcut", "match_cut", ["c002", "c003"], "motion match cut (trimmed to best junction): accepted on feature score alone", 0.69, {"match_type": "motion"})]
    return j


def test_the_inspector_explains_the_junction():
    plain = render_inspector(inspected_junction(), "final", None).plain
    assert "Junction 2 · c002 → c003 · shot 1 → shot 3 · beat b1 → b2" in plain
    assert "MATCH CUT (motion)" in plain and "locked by matchcut" in plain and "beat boundary" in plain
    assert "combined" in plain and "0.44" in plain and "0.69" in plain and "+0.25" in plain
    assert "out 5.00s → 4.88s [-0.12s (-3f)]" in plain and "in 7.00s → 7.12s [+0.12s (+3f)]" in plain
    assert "why: matchcut: motion match cut" in plain
    assert "matchcut: out -0.12s (-3f), in +0.12s (+3f), locked" in plain


def test_the_inspector_flags_a_pending_pin_and_missing_scores():
    j = inspected_junction()
    plain = render_inspector(j, "final", Pin(1, 3, 4.70, 7.12)).plain
    assert "pinned (locked)" in plain and "pin pending" in plain
    for s in j.states:
        s.score = None
    assert "scores unavailable" in render_inspector(j, "final", None).plain


def test_the_expanded_inspector_lists_every_decision():
    plain = render_inspector(inspected_junction(), "final", None, expanded=True).plain
    assert "decisions:" in plain and "matchcut:match_cut" in plain
```
(`PILImage`, `images`, `Timeline`, `Clip`, `render_track`, `TimelineTrack`, `App`, `ComposeResult` and `run_async` are already imported at the top of `test_widgets.py`; add any that are missing.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/tui/test_widgets.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'matchcut.tui.widgets.inspector'`.

- [ ] **Step 3: Implement `images.blend`**

In `matchcut/tui/images.py` (PIL's `Image` is already imported there as `PILImage`; check the alias):
```python
def blend(a: PILImage.Image, b: PILImage.Image, alpha: float = 0.5) -> PILImage.Image:
    """The two frames mixed, at the first frame's size: the classic match-cut alignment check."""
    a = a.convert("RGB")
    b = b.convert("RGB").resize(a.size)
    return PILImage.blend(a, b, alpha)
```

- [ ] **Step 4: Junction mode on the track**

In `matchcut/tui/widgets/timeline.py`, factor the window and span computation out of `render_track` into a helper shared with hit-testing, add the junction parameter, and extend the widget:
```python
from typing import Dict, List, Optional

from textual.events import Click
from textual.message import Message


def _layout(tl: Timeline, width: int, center: int):
    n = len(tl.clips)
    visible = min(n, max(1, (width + 1) // 2))
    lo = min(max(0, center - visible // 2), n - visible)
    window = range(lo, lo + visible)
    clip_total = max(sum(tl.clips[i].duration for i in window), 1e-6)
    usable = max(width - (visible - 1), visible)
    spans = [max(1, int(round(tl.clips[i].duration / clip_total * usable))) for i in window]
    while sum(spans) > usable and max(spans) > 1:  # rounding and the 1-cell minimum can overshoot
        spans[spans.index(max(spans))] -= 1
    return window, spans


def junction_columns(tl: Timeline, width: int, center: int) -> Dict[int, int]:
    """Column of each visible junction glyph; junction j sits between clip j and clip j+1."""
    if len(tl.clips) < 2:
        return {}
    window, spans = _layout(tl, max(width, 1), center)
    cols: Dict[int, int] = {}
    x = 0
    for k, i in enumerate(window):
        if k > 0:
            cols[i - 1] = x
            x += 1
        x += spans[k]
    return cols
```
In `render_track`, add the keyword `selected_junction: Optional[int] = None`, replace the inline window/span code with `window, spans = _layout(tl, width, selected_junction if selected_junction is not None else selected)`, style the glyph of junction `i - 1` as `"bold black on yellow"` when `i - 1 == selected_junction`, and only add `" reverse bold"` to a clip when `selected_junction is None and i == selected`. Everything else in `render_track` stays as it is.

In `TimelineTrack`:
```python
    class JunctionClicked(Message):
        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    # in __init__: self.selected_junction: Optional[int] = None

    def set_junction(self, j: Optional[int]) -> None:
        if j is None or self.timeline is None or len(self.timeline.clips) < 2:
            self.selected_junction = None
        else:
            self.selected_junction = max(0, min(j, len(self.timeline.clips) - 2))
        self._redraw()

    def _width(self) -> int:
        return max(20, self.size.width or 80)

    def on_click(self, event: Click) -> None:
        if self.selected_junction is None or self.timeline is None:
            return
        cols = junction_columns(self.timeline, self._width(), self.selected_junction)
        if not cols:
            return
        j, col = min(cols.items(), key=lambda kv: abs(kv[1] - event.x))
        if abs(col - event.x) <= 1:
            self.post_message(self.JunctionClicked(j))
```
and make `_redraw` use `self._width()` and pass `selected_junction=self.selected_junction` to `render_track`.

- [ ] **Step 5: Write `matchcut/tui/widgets/inspector.py`**

```python
"""The junction inspector's text: badges, the score comparison, cut points, why and history."""
from __future__ import annotations

from typing import List, Optional

from rich.text import Text

from matchcut.pipeline.editorial import Pin
from matchcut.pipeline.junctions import SCORE_ROWS, Junction, JunctionState, frames_delta

KEYS = "l lock these points · v revert to the first cut · x clear pin · o open blend · Enter full history"


def badges(j: Junction, shown: JunctionState, pin: Optional[Pin]) -> List[str]:
    out: List[str] = []
    if j.match_type:
        out.append(f"MATCH CUT ({j.match_type})")
    if shown.locked:
        out.append("locked by the editor" if j.pinned else "locked by matchcut")
    if j.beat_boundary:
        out.append("beat boundary")
    if shown.score is not None and shown.score.jump_cut_risk:
        out.append("jump-cut risk")
    if pin is not None:
        out.append(f"pinned ({pin.origin})")
        tol = (1.0 / j.fps if j.fps > 0 else 0.04) + 1e-6
        if abs(shown.out_time - pin.out_time) > tol or abs(shown.in_time - pin.in_time) > tol or not shown.locked:
            out.append("pin pending: run again (R) to apply")
    return out


def render_inspector(j: Junction, stage: str, pin: Optional[Pin], expanded: bool = False) -> Text:
    shown = j.state_at(stage) or j.current
    base = j.baseline
    t = Text()
    t.append(f"Junction {j.index + 1} · {j.out_clip} → {j.in_clip} · shot {j.out_shot} → shot {j.in_shot} · beat {j.out_beat} → {j.in_beat}\n", style="bold")
    marks = badges(j, shown, pin)
    t.append((" · ".join(marks) if marks else "no match cut, lock or pin on this junction") + "\n", style="yellow" if marks else "dim")
    if base.score is None or shown.score is None:
        t.append("scores unavailable: the workspace has no analysis.npz\n", style="dim")
    else:
        t.append(f"{'':<13}{base.stage:>10}{shown.stage:>10}{'Δ':>8}\n", style="dim")
        for name in SCORE_ROWS:
            v0, v1 = getattr(base.score, name), getattr(shown.score, name)
            t.append(f"{name:<13}{v0:>10.2f}{v1:>10.2f}{v1 - v0:>+8.2f}\n", style="bold" if name == "combined" else "")
        t.append(f"motion type  {shown.score.motion_type}\n", style="dim")
    t.append(
        f"cut points   out {base.out_time:.2f}s → {shown.out_time:.2f}s [{frames_delta(shown.out_time - base.out_time, j.fps)}]"
        f"   in {base.in_time:.2f}s → {shown.in_time:.2f}s [{frames_delta(shown.in_time - base.in_time, j.fps)}]\n"
    )
    t.append("why: ", style="bold")
    t.append(j.why() + "\n")
    if expanded:
        t.append("history:\n", style="bold")
        for line in j.history():
            t.append(f"  {line}\n")
        t.append("decisions:\n", style="bold")
        for d in j.decisions:
            t.append(f"  {d.pass_name}:{d.action}  {d.detail}\n")
    else:
        t.append("history: ", style="bold")
        t.append(" · ".join(j.history()) + "\n")
    t.append(KEYS, style="dim")
    return t
```

- [ ] **Step 6: Run the tests**

Run: `python3 -m pytest tests/tui/test_widgets.py -q` (7 new pass, existing pass), then `python3 -m pytest tests/tui -q`. The clip-mode track is unchanged, so the snapshots must not change; if one does, find out why before regenerating.

- [ ] **Step 7: Commit**

Message: `Add the frame blend, junction selection on the track, and the junction inspector text` plus the trailers. Stage `matchcut/tui/images.py matchcut/tui/widgets/timeline.py matchcut/tui/widgets/inspector.py tests/tui/test_widgets.py`. Push.

---

### Task 5: The Edit workbench replaces the Cut tab

**Files:**
- Create: `matchcut/tui/views/edit.py`, `tests/tui/test_edit.py`
- Delete: `matchcut/tui/views/cut.py`
- Modify: `matchcut/tui/app.py`, `matchcut/tui/styles.tcss`, `tests/tui/test_views.py`, `tests/tui/test_app.py`, `tests/tui/test_snapshots.py`, `tests/tui/__snapshots__/`

**Interfaces:**
- Consumes: Tasks 1, 2 and 4 (`build_junctions`, `scorer_for_workspace`, `PinStore`, `Pin`, `render_inspector`, `TimelineTrack.set_junction`, `TimelineTrack.JunctionClicked`, `images.blend`); `RunState.timeline_stages()`, `timeline(stage)`, `timeline_files()`, `media()`, `narrative()`, `workspace`, `config`, `image_mode`; `FrameCache(source, root, cfg.sampling).frame(t)`; `Frame.show(image, caption)`.
- Produces: `views.edit.EditView(Vertical)` with `refresh_from_state(state)`, `reshow()`, `current_stage() -> Optional[str]`, `current_junction() -> Optional[Junction]`, `open_target() -> Optional[Path]`, attributes `junctions`, `selected`, `stages`, `stage_index`, `expanded`; child ids `#edit-header #edit-track #beat-strip #out-frame #in-frame #blend-frame #junction-panel`; bindings `left`/`right` junction, `left_square_bracket`/`right_square_bracket` stage, `l` lock, `v` revert, `x` clear pin, `enter` full history. The app's tab 4 is `tab-edit` labelled `4 Edit`.

- [ ] **Step 1: Write the failing tests**

`tests/tui/test_edit.py`:
```python
"""The Edit workbench: junction selection, evidence, pins, and the hidden-tab frame case."""
from __future__ import annotations

import json
import shutil

from matchcut.pipeline.config import PipelineConfig
from matchcut.tui import images
from matchcut.tui.app import MatchcutApp
from matchcut.tui.views.edit import EditView
from matchcut.tui.widgets.frame import Frame
from matchcut.tui.widgets.timeline import TimelineTrack, junction_columns
from tests.tui.conftest import run_async, wait_for

FRAMES = ("#out-frame", "#in-frame", "#blend-frame")


def app_for(ws):
    cfg = PipelineConfig.load(ws / "config.json")
    cfg.workspace = str(ws)
    return MatchcutApp(cfg)


def own_copy(mock_workspace, tmp_path):
    ws = tmp_path / "ws"
    shutil.copytree(mock_workspace, ws)
    return ws


def frames_ready(app):
    return all(app.query_one(f, Frame).image_mode for f in FRAMES)


def image_heights(app):
    return [app.query_one(f).query(".frame-image").first().region.height for f in FRAMES]


def test_a_junction_shows_three_frames_and_its_evidence(mock_workspace):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions) and frames_ready(app))
            assert app.focused is view
            assert min(image_heights(app)) >= 8
            j = view.current_junction()
            panel = str(app.query_one("#junction-panel").content)
            assert f"Junction 1 · {j.out_clip} → {j.in_clip}" in panel
            assert "combined" in panel and "cut points" in panel and "why:" in panel and "history:" in panel
            assert "junctions" in str(app.query_one("#edit-header").content)

    run_async(body())


def test_frames_render_when_the_data_arrived_while_the_tab_was_hidden(mock_workspace):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))  # loaded by refresh_views while tab 1 is shown
            await pilot.press("4")
            await wait_for(pilot, lambda: frames_ready(app) and min(image_heights(app)) >= 8)

    run_async(body())


def test_junctions_are_selected_by_key_and_by_click(mock_workspace):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: len(view.junctions) >= 3)
            await pilot.press("right")
            assert view.selected == 1 and "Junction 2" in str(app.query_one("#junction-panel").content)
            await pilot.press("left")
            assert view.selected == 0
            track = app.query_one("#edit-track", TimelineTrack)
            col = junction_columns(track.timeline, track.size.width, view.selected)[2]
            await pilot.click("#edit-track", offset=(col, 0))
            await wait_for(pilot, lambda: view.selected == 2)
            await pilot.press("enter")
            assert "decisions:" in str(app.query_one("#junction-panel").content)

    run_async(body())


def test_stepping_back_inspects_an_earlier_stage(mock_workspace):
    async def body():
        app = app_for(mock_workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            assert view.current_stage() == "final"
            await pilot.press("left_square_bracket")
            await wait_for(pilot, lambda: view.current_stage() != "final")
            assert view.current_stage() in str(app.query_one("#edit-header").content)

    run_async(body())


def test_lock_revert_and_clear_write_editorial_json(mock_workspace, tmp_path):
    ws = own_copy(mock_workspace, tmp_path)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions))
            j = view.current_junction()
            await pilot.press("l")
            pins = json.loads((ws / "editorial.json").read_text())["pins"]
            assert pins == [dict(out_shot=j.out_shot, in_shot=j.in_shot, out_time=round(j.current.out_time, 3), in_time=round(j.current.in_time, 3), origin="locked", note="")]
            await wait_for(pilot, lambda: "pinned (locked)" in str(app.query_one("#junction-panel").content))
            await pilot.press("v")
            pins = json.loads((ws / "editorial.json").read_text())["pins"]
            assert pins[0]["origin"] == "reverted" and pins[0]["out_time"] == round(j.baseline.out_time, 3)
            await pilot.press("x")
            assert json.loads((ws / "editorial.json").read_text())["pins"] == []

    run_async(body())


def test_refresh_is_idempotent_and_open_saves_the_blend(mock_workspace, tmp_path, monkeypatch):
    ws = own_copy(mock_workspace, tmp_path)
    opened = []
    monkeypatch.setattr(images, "open_externally", lambda p: opened.append(p) or True)

    async def body():
        app = app_for(ws)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            view = app.query_one(EditView)
            await wait_for(pilot, lambda: bool(view.junctions) and frames_ready(app))
            calls = []
            monkeypatch.setattr(Frame, "show", lambda self, *a, **k: calls.append(self.id))
            for _ in range(3):
                app.refresh_views()
                await pilot.pause()
            assert calls == []
            monkeypatch.undo()
            monkeypatch.setattr(images, "open_externally", lambda p: opened.append(p) or True)
            await pilot.press("o")
            await wait_for(pilot, lambda: bool(opened))
            assert opened[0].exists() and opened[0].suffix == ".jpg"

    run_async(body())


def test_an_empty_workspace_shows_how_to_start(tmp_path):
    async def body():
        cfg = PipelineConfig(source="none.mp4", workspace=str(tmp_path))
        app = MatchcutApp(cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("4")
            await pilot.pause()
            assert "no cut yet" in str(app.query_one("#edit-header").content)
            for key in ("left", "right", "left_square_bracket", "l", "v", "x", "enter"):
                await pilot.press(key)
            assert not (tmp_path / "editorial.json").exists()

    run_async(body())
```

In `tests/tui/test_views.py` and `tests/tui/test_app.py`, delete or rewrite every test that refers to `CutView`, `tab-cut`, `#cut-header`, `#track`, `#clip-frame`, `#clip-detail` or the Cut open target (`grep -n "CutView\|tab-cut\|cut-header\|#track\|clip-frame\|clip-detail" tests/tui/*.py`). The Edit tests above replace them; an open-target test in `test_app.py` that pressed `4` should now expect a `.jpg` blend path. In `tests/tui/test_snapshots.py` rename the Cut case to `edit` (key `4`) and delete the old Cut snapshot file from `tests/tui/__snapshots__/`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/tui/test_edit.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'matchcut.tui.views.edit'`.

- [ ] **Step 3: Write `matchcut/tui/views/edit.py`**

```python
"""Edit workbench: pick a junction on the track and see why the cut is there."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static

from matchcut.pipeline.editorial import Pin, PinStore
from matchcut.pipeline.junctions import Junction, build_junctions, scorer_for_workspace
from matchcut.pipeline.sampling import FrameCache
from .. import images
from ..state import RunState, file_fingerprint
from ..widgets.frame import Frame
from ..widgets.inspector import render_inspector
from ..widgets.timeline import TimelineTrack

FRAME_IDS = ("out-frame", "in-frame", "blend-frame")


class EditView(Vertical):
    can_focus = True  # junction, stage and pin keys only apply while focus sits here
    BINDINGS = [
        Binding("left", "prev_junction", "Previous junction", show=False),
        Binding("right", "next_junction", "Next junction", show=False),
        Binding("left_square_bracket", "prev_stage", "Earlier stage", show=False),
        Binding("right_square_bracket", "next_stage", "Later stage", show=False),
        Binding("l", "lock", "Lock"),
        Binding("v", "revert", "Revert"),
        Binding("x", "clear_pin", "Clear pin"),
        Binding("enter", "toggle_history", "History"),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._state: Optional[RunState] = None
        self.stages: List[str] = []
        self.stage_index = -1  # -1 follows the latest stage
        self.junctions: List[Junction] = []
        self.selected = 0
        self.expanded = False
        self._fingerprint: Optional[tuple] = None  # identity of the workspace data last rendered
        self._shown: Optional[tuple] = None  # (fingerprint, stage, junction, expanded) on screen
        self._blend_image = None

    def compose(self) -> ComposeResult:
        yield Static("", id="edit-header")
        yield TimelineTrack(id="edit-track")
        yield Static("", id="beat-strip")
        with Horizontal(id="junction-frames"):
            yield Frame(id="out-frame", caption="outgoing", classes="junction-frame")
            yield Frame(id="in-frame", caption="incoming", classes="junction-frame")
            yield Frame(id="blend-frame", caption="overlay", classes="junction-frame")
        with VerticalScroll(id="junction-scroll"):
            yield Static("", id="junction-panel")

    # -- what is shown ----------------------------------------------------------
    def current_stage(self) -> Optional[str]:
        if not self.stages:
            return None
        idx = self.stage_index if self.stage_index >= 0 else len(self.stages) - 1
        return self.stages[min(idx, len(self.stages) - 1)]

    def current_junction(self) -> Optional[Junction]:
        return self.junctions[self.selected] if self.junctions else None

    @staticmethod
    def _data_fingerprint(state: RunState) -> tuple:
        ws = state.workspace
        files = [ws / "probe.json", ws / "analysis.npz", ws / "narrative.json", ws / "editorial.json"] + state.timeline_files()
        return tuple(file_fingerprint(f) for f in files)

    def refresh_from_state(self, state: RunState) -> None:
        self._state = state
        fingerprint = self._data_fingerprint(state)
        if fingerprint == self._fingerprint:
            return
        self._fingerprint = fingerprint
        self.stages = state.timeline_stages()
        if self.stage_index >= len(self.stages):
            self.stage_index = -1
        stage = self.current_stage()
        track = self.query_one("#edit-track", TimelineTrack)
        header = self.query_one("#edit-header", Static)
        strip = self.query_one("#beat-strip", Static)
        if stage is None:
            self.junctions = []
            header.update("(no cut yet: run the pipeline, then inspect its junctions here)")
            track.set_timeline(None)
            strip.update("")
            self._show_junction()
            return
        upto = self.stages[: self.stages.index(stage) + 1]
        pairs = [(s, state.timeline(s)) for s in upto]
        pairs = [(s, t) for s, t in pairs if t is not None]
        self.junctions = build_junctions(pairs, scorer_for_workspace(state.workspace, state.config)) if pairs else []
        self.selected = min(self.selected, max(0, len(self.junctions) - 1))
        tl = state.timeline(stage)
        track.set_timeline(tl)
        track.set_junction(self.selected if self.junctions else None)
        pins = PinStore(state.workspace).load()
        unplaced = [d for d in (tl.history if tl else []) if d.pass_name == "editor" and d.action == "pin_unplaced"]
        summary = f" ({self.stages.index(stage) + 1}/{len(self.stages)}) · {len(tl.clips) if tl else 0} clips · {tl.duration if tl else 0:.1f}s · {len(self.junctions)} junctions · {len(pins)} pinned"
        if unplaced:
            summary += f" · {len(unplaced)} pin(s) not placed"
        header.update(Text.assemble(("stage ", "dim"), (stage, "bold"), summary, ("   [ ] stage   ←/→ junction", "dim")))
        strip.update(self._beat_strip(state, tl))
        self._show_junction()

    @staticmethod
    def _beat_strip(state: RunState, tl) -> Text:
        beats = (state.narrative() or {}).get("beats") or []
        if not beats or tl is None:
            return Text("")
        t = Text("beats  ", style="dim")
        for b in beats:
            seconds = sum(c.duration for c in tl.clips if c.beat_id == b.get("id"))
            t.append(f"{b.get('id', '?')} {b.get('title', '')} {seconds:.1f}s   ")
        return t

    def _frames(self) -> List[Frame]:
        return [self.query_one(f"#{fid}", Frame) for fid in FRAME_IDS]

    def _show_junction(self, force: bool = False) -> None:
        if self._state is None:
            return
        panel = self.query_one("#junction-panel", Static)
        j, stage = self.current_junction(), self.current_stage()
        if j is None or stage is None:
            panel.update("(no junctions: the cut needs at least two clips)" if stage else "")
            for f in self._frames():
                f.show(None)
            self._shown, self._blend_image = None, None
            return
        key = (self._fingerprint, stage, self.selected, self.expanded)
        if self._shown == key and not force:
            return
        need_frames = force or self._shown is None or self._shown[:3] != key[:3]
        self._shown = key
        panel.update(render_inspector(j, stage, PinStore(self._state.workspace).get(j.out_shot, j.in_shot), self.expanded))
        if need_frames:
            self._show_frames(j, stage)

    def _show_frames(self, j: Junction, stage: str) -> None:
        frames = self._frames()
        media = self._state.media()
        shown = j.state_at(stage) or j.current
        if media is None:
            for f in frames:
                f.show(None)
            self._blend_image = None
            return
        cache = FrameCache(media.path, self._state.workspace / "frames", self._state.config.sampling)
        fps = media.fps or j.fps or 25.0
        out_t = max(0.0, shown.out_time - 1.0 / fps)  # the last frame shown before the cut
        try:
            out_img, in_img = cache.frame(out_t), cache.frame(shown.in_time)
        except Exception:
            out_img = in_img = None
        self._blend_image = images.blend(out_img, in_img) if out_img is not None and in_img is not None else None
        frames[0].show(out_img, caption=f"outgoing · {j.out_clip} @ {out_t:.2f}s")
        frames[1].show(in_img, caption=f"incoming · {j.in_clip} @ {shown.in_time:.2f}s")
        frames[2].show(self._blend_image, caption="overlay · 50% blend")
        mode = next((f.image_mode for f in frames if f.image_mode), "")
        if mode:
            self._state.image_mode = mode

    def reshow(self) -> None:
        """Re-mount the frames once the tab is visible: images mounted while it was hidden get no height."""
        self._show_junction(force=True)

    def open_target(self) -> Optional[Path]:
        j = self.current_junction()
        if j is None or self._blend_image is None or self._state is None:
            return None
        path = self._state.workspace / "frames" / f"blend_{j.out_clip}_{j.in_clip}_{self.current_stage()}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        self._blend_image.save(path, quality=90)
        return path

    # -- actions --------------------------------------------------------------------
    def _select(self, i: int) -> None:
        if not self.junctions:
            return
        self.selected = max(0, min(i, len(self.junctions) - 1))
        self.query_one("#edit-track", TimelineTrack).set_junction(self.selected)
        self._show_junction()

    def on_timeline_track_junction_clicked(self, message: TimelineTrack.JunctionClicked) -> None:
        self._select(message.index)

    def action_prev_junction(self) -> None:
        self._select(self.selected - 1)

    def action_next_junction(self) -> None:
        self._select(self.selected + 1)

    def _step_stage(self, delta: int) -> None:
        if not self.stages or self._state is None:
            return
        current = self.stages.index(self.current_stage())
        target = max(0, min(len(self.stages) - 1, current + delta))
        self.stage_index = -1 if target == len(self.stages) - 1 else target
        self._fingerprint = None  # rebuild the junctions for the newly inspected stage
        self.refresh_from_state(self._state)

    def action_prev_stage(self) -> None:
        self._step_stage(-1)

    def action_next_stage(self) -> None:
        self._step_stage(1)

    def action_toggle_history(self) -> None:
        if self.current_junction() is None:
            return
        self.expanded = not self.expanded
        self._show_junction()

    def _apply_hint(self, message: str) -> str:
        bridge = getattr(self.app, "bridge", None)
        if bridge is not None and bridge.is_running():
            return message + ": takes effect when this run's passes run again (redo the rough cut) or on the next run"
        return message + ": takes effect on the next run (R); cached stages are reused and only the passes re-run"

    def _pin(self, origin: str) -> None:
        j, stage = self.current_junction(), self.current_stage()
        if j is None or stage is None or self._state is None:
            self.app.notify("no junction selected", severity="warning")
            return
        source = j.baseline if origin == "reverted" else (j.state_at(stage) or j.current)
        pin = Pin(j.out_shot, j.in_shot, round(source.out_time, 3), round(source.in_time, 3), origin)
        PinStore(self._state.workspace).put(pin)
        self.app.notify(self._apply_hint(f"pinned shot {j.out_shot} → shot {j.in_shot} ({origin}) at out {pin.out_time:.2f}s / in {pin.in_time:.2f}s"))
        self._fingerprint = None
        self.refresh_from_state(self._state)

    def action_lock(self) -> None:
        self._pin("locked")

    def action_revert(self) -> None:
        self._pin("reverted")

    def action_clear_pin(self) -> None:
        j = self.current_junction()
        if j is None or self._state is None:
            self.app.notify("no junction selected", severity="warning")
            return
        if PinStore(self._state.workspace).remove(j.out_shot, j.in_shot):
            self.app.notify("pin cleared: the passes decide this junction again on the next run")
        else:
            self.app.notify("this junction has no pin", severity="warning")
        self._fingerprint = None
        self.refresh_from_state(self._state)
```

- [ ] **Step 4: Wire it into the app and the stylesheet**

`matchcut/tui/app.py`:
- Replace `from .views.cut import CutView` with `from .views.edit import EditView`; delete `matchcut/tui/views/cut.py`.
- Binding `4`: `Binding("4", "show_tab('tab-edit')", "Edit")`.
- In `compose`: `with TabPane("4 Edit", id="tab-edit"): yield EditView(id="edit-view")` in place of the Cut pane.
- `refresh_views`: query `"RunView, ShotsView, StoryView, EditView, ConfigView"`.
- `on_tabbed_content_tab_activated`: replace the `tab-cut` branch with
```python
        elif event.pane.id == "tab-edit":
            self.call_after_refresh(self._focus_if_active, "tab-edit", "#edit-view")
            self.call_after_refresh(self.query_one(EditView).reshow)
```
- `current_open_target`: the branch for the Cut tab becomes `tab-edit` and returns `self.query_one(EditView).open_target()`.
- `HELP_TEXT`: tab names `Run / Shots / Story / Edit / Config`; replace the Cut line with `←/→ junction · [ ] stage · l lock · v revert · x clear pin · Enter history (Edit)`.

`matchcut/tui/styles.tcss`: delete the `#clip-row`, `#clip-frame`, `#clip-detail`, `#cut-header` rules and add
```css
#edit-header { height: 1; }
#beat-strip { height: 1; color: $text-muted; }
#junction-frames { height: 16; }
.junction-frame { width: 1fr; height: 100%; }
.junction-frame > .frame-image { width: 100%; height: 1fr; }
#junction-scroll { height: 1fr; }
```
If the image heights in `test_a_junction_shows_three_frames_and_its_evidence` stay below 8, adjust these rules (not the test) until each image fills most of its frame; textual-image's widgets size from their CSS `width`/`height`.

- [ ] **Step 5: Run the tests**

Run: `python3 -m pytest tests/tui/test_edit.py -q` (7 passed), then regenerate the snapshots (`python3 -m pytest tests/tui/test_snapshots.py --snapshot-update -q`), run `python3 -m pytest tests/tui/test_snapshots.py -q` twice without the flag, open the Edit snapshot SVG as text and confirm it contains half-block characters (`▀` or `▄`), then `python3 -m pytest tests/tui -q` and `python3 -m pytest tests/pipeline -q`.

- [ ] **Step 6: Commit**

Message: `Replace the Cut tab with the Edit workbench: junction frames, scores, history and pins` plus the trailers. Stage `matchcut/tui/views/edit.py`, the deletion of `matchcut/tui/views/cut.py`, `matchcut/tui/app.py matchcut/tui/styles.tcss tests/tui/ tests/tui/__snapshots__/`. Push.

---

### Task 6: Acceptance kit and docs

**Files:**
- Create: `tools/capture_edit.py`, `docs/M1-ACCEPTANCE.md`, `docs/screenshots/` (SVGs)
- Modify: `docs/TUI.md`, `docs/PIPELINE.md`, `README.md`

**Interfaces:**
- Consumes: everything above; `App.save_screenshot(filename=..., path=...)` (check the signature with `python3 -c "import inspect; from textual.app import App; print(inspect.signature(App.save_screenshot))"`).
- Produces: `python tools/capture_edit.py WORKSPACE OUTDIR [--junctions N]` writes `edit-junction-1.svg` … `edit-junction-N.svg` (default N=3) and returns 0, or 1 if the workspace has no timelines.

- [ ] **Step 1: Write `tools/capture_edit.py`**

```python
"""Capture the Edit tab of a workspace as SVG screenshots, one per junction (headless)."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from matchcut.pipeline.config import PipelineConfig  # noqa: E402
from matchcut.tui.app import MatchcutApp  # noqa: E402
from matchcut.tui.views.edit import EditView  # noqa: E402
from matchcut.tui.widgets.frame import Frame  # noqa: E402


async def capture(ws: Path, out: Path, count: int, size=(140, 46)) -> int:
    cfg = PipelineConfig.load(ws / "config.json")
    cfg.workspace = str(ws)
    app = MatchcutApp(cfg)
    async with app.run_test(size=size) as pilot:
        await pilot.press("4")
        view = app.query_one(EditView)
        for _ in range(400):
            if view.junctions and all(app.query_one(f"#{i}", Frame).image_mode for i in ("out-frame", "in-frame", "blend-frame")):
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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("workspace")
    p.add_argument("outdir")
    p.add_argument("--junctions", type=int, default=3)
    a = p.parse_args(argv)
    return asyncio.run(capture(Path(a.workspace), Path(a.outdir), a.junctions))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Produce real-footage screenshots**

Real footage, mock backend (the editorial evidence comes from the deterministic scorer, which is real; the descriptions and narrative are mock and the status bar says so):
```bash
SRC=$(ls /root/.claude/uploads/*/*big-buck-bunny-20s-720p.mp4 | head -1)
WS=/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/m1_bbb
python3 -m matchcut.pipeline.cli run "$SRC" -w "$WS" --backend mock -t 12 --set render.enabled=false
python3 -m matchcut.pipeline.cli junctions "$WS"
python3 tools/capture_edit.py "$WS" docs/screenshots --junctions 3
```
(Check the CLI flags with `python3 -m matchcut.pipeline.cli run -h`; use the equivalents if they differ.) If the upload is missing, use the synthetic match-cut footage instead: `python3 -c "from matchcut.pipeline.demo import make_demo_footage; from tests.pipeline.conftest import MC_SHOTS; make_demo_footage('$WS.mp4', shots=MC_SHOTS)"` and say so in the docs caption. Open each SVG as text and confirm it contains half-block characters. Add `docs/screenshots/README.md` with one line per SVG and, for Big Buck Bunny frames, the credit `Big Buck Bunny © Blender Foundation, CC BY 3.0 (peach.blender.org)`.

- [ ] **Step 3: Write `docs/M1-ACCEPTANCE.md`**

Content (adapt command flags to the real CLI):
```markdown
# M1 acceptance: can a cut be understood without logs?

M1 is accepted when an editor, on real footage with a real model backend in
their own terminal, can inspect cuts and say why matchcut made them without
reading a log file or a JSON document.

## Set up
- Footage: any real clip of 15–60 s with several shots (the default is the
  20 s Big Buck Bunny clip).
- A real backend: export `ANTHROPIC_API_KEY` (in a cloud environment, add it
  as an environment secret; never paste it into a chat).
- A terminal with an image protocol: Kitty, WezTerm, Ghostty or iTerm2
  (Sixel terminals also work). Plain terminals fall back to half-blocks; note
  which one the status bar reports.

## Run
    pip install -r requirements-pipeline.txt -r requirements-tui.txt
    python -m matchcut.tui footage.mp4 -w ws_m1 --backend anthropic
Press R, continue through the checkpoints, then press 4.

## Checklist
1. The status bar does **not** say MOCK, and shows tokens and a cost estimate.
2. The images line names a real protocol (kitty graphics, sixel) or half-block.
3. For three junctions, including at least one match cut and one beat
   boundary: from the frames, the overlay, the scores, the why line and the
   history alone, write one sentence on why the cut is there. Note any
   junction where you could not.
4. Lock one junction (l) and revert one (v). Press R. The header shows two
   pins; both junctions show `pinned`, no `pin pending`.
5. Regenerate the rough cut: run again, choose redo with a note at the rough
   cut checkpoint. After the passes, both pins still hold, or the header
   says which could not be placed and why.
6. Latency: moving between junctions with ←/→ feels immediate after the
   first visit (frames are cached). Note the first-visit delay.

## Verdict
| Question | Answer |
|---|---|
| Understood why, without logs, for N of 3 junctions | |
| Image fidelity good enough to judge shape and position | |
| Motion judgement possible without playback | |
| Decision | continue with Textual / keep Textual as control room and build a separate review surface |

If the decision is a separate surface, `python -m matchcut.pipeline.cli
junctions WORKSPACE --index N` already exposes every value the inspector
shows.
```

- [ ] **Step 4: Update the docs**

- `docs/TUI.md`: in the views table replace the Cut row with Edit (`4`: the cut as a track of junctions; select with ←/→ or a click; `[`/`]` inspect an earlier stage; outgoing, incoming and blended frames; scores at the first and inspected stage; cut-point deltas; why; history; `l`/`v`/`x` pins; `Enter` full history; `o` opens the blend). Add a short "Pins" paragraph (what lock and revert do, that pins are keyed by shots, take effect on the next run, and survive regenerating the rough cut) and a "Mock runs" sentence (the MOCK badge). Embed the three screenshots from `docs/screenshots/` with one-line captions.
- `docs/PIPELINE.md`: an "Editorial pins" section (`editorial.json` format, application before the passes, `editor:pinned` / `editor:pin_unplaced` decisions, rough-cut constraints) and the `junctions` command with an example of its output from the Step 2 workspace.
- `README.md`: one line under the TUI command: `python -m matchcut.pipeline.cli junctions WORKSPACE   # explain every cut as text`, and a pointer to `docs/M1-ACCEPTANCE.md`.

- [ ] **Step 5: Verify**

Run `python3 -m pytest tests/pipeline -q`, `python3 -m pytest tests/tui -q`, the Textual-free check `python3 -c "import sys, matchcut.pipeline.pipeline, matchcut.pipeline.cli, matchcut.pipeline.junctions, matchcut.pipeline.editorial; assert 'textual' not in sys.modules; print('ok')"`, and a pty smoke of the real app on the Step 2 workspace (spawn `python -m matchcut.tui -w "$WS"` under `pty.fork()`, send `4`, wait 3 s, send `q` twice, assert no `Traceback` in the captured output).

- [ ] **Step 6: Commit**

Message: `Add the M1 acceptance kit, Edit tab docs and real-footage screenshots` plus the trailers. Stage `tools/capture_edit.py docs/M1-ACCEPTANCE.md docs/screenshots/ docs/TUI.md docs/PIPELINE.md README.md`. Push.

---

## Self-review

**Spec coverage.** Edit workbench layout, selection by key and click, stage stepping (Tasks 4, 5). Evidence: three frames incl. blend (Tasks 4, 5), scores rebuilt from `analysis.npz` and proven equal to the pass's recorded scores (Task 1), cut-point deltas in seconds and frames, why, history, badges incl. pin pending (Tasks 1, 4). Junction identity by clip ids with reorder handling (Task 1). Pins keyed by shots, lock/revert/clear, applied before the passes, rough-cut constraints, unplaced reporting, survival across regeneration (Tasks 2, 5). Honest status: MOCK badge, mock usage text, `(mock)` model lines, skipped stages, finished label (Task 3). Text access: `junctions` command (Task 1). Hidden-tab frame bug found by the screenshot recapture: `reshow` on tab activation plus a test that loads data while the tab is hidden (Task 5). Acceptance procedure and decision gate (Task 6). Out-of-scope items are not touched.

**Type consistency.** `Junction`/`JunctionState` fields and methods are defined in Task 1 and used with the same names in Tasks 4 and 5. `Pin(out_shot, in_shot, out_time, in_time, origin, note)` and `PinStore.get/put/remove/load` are defined in Task 2 and used in Tasks 4 and 5; the JSON shape asserted in Task 5 matches `Pin.to_dict()`. `TimelineTrack.set_junction`, `selected_junction`, `JunctionClicked.index` and `junction_columns` are defined in Task 4 and used in Task 5. `RunState.is_mock`, `finished_label`, `render_path`, `StageStatus.note` and `app.status_text` are defined and tested in Task 3.

**Known judgement calls.** The blend image is the alignment evidence rather than an edge or structure overlay: it is what editors use to judge a match cut and needs no new analysis. Pins apply before the passes, not after, so the passes build around them (pacing and beat sync already respect locks). A pin made during a run applies when the passes run again; the notification says so instead of re-running passes mid-run. The automated part of M1 runs on the mock backend; the decision gate needs the human acceptance run in `docs/M1-ACCEPTANCE.md`.
