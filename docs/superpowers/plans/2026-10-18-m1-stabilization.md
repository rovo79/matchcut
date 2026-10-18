# M1 Stabilization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two wording defects the M1 rehearsal found, place pins on junctions that a pass created, let continuity soften a pinned jump cut, and add deterministic regression tests for moved cut points, reordered clips, preserved dissolves and persistent pins — without adding UI and without claiming the open human acceptance gate.

**Architecture:** All behaviour changes are in the pipeline package (`junctions.py`, `editorial.py`, `passes.py`), plus two wording changes in the TUI (`widgets/inspector.py`, `views/edit.py`). Tests use the existing synthetic fixture footage (`tests/pipeline/conftest.py`: `demo_video`, `matchcut_video`; `tests/tui/conftest.py`: `mock_workspace`) and the mock backend.

**Tech Stack:** Python 3.9+ syntax (runs on 3.13), numpy, Pillow, ffmpeg, Textual 8.2.8, pytest, pytest-textual-snapshot.

**Spec:** `docs/superpowers/specs/2026-10-18-m1-stabilization.md` (binding), amending `docs/superpowers/specs/2026-10-17-m1-junction-inspector.md`.

## Global Constraints

- Pipeline code lives in `matchcut/pipeline/` and must never import `textual`, `textual_image` or anything under `matchcut/tui/`. Before this plan `python3 -m pytest tests/pipeline -q` passes 111 tests and `python3 -m pytest tests/tui -q` passes 112; both suites must pass after every task (counts grow with new tests).
- Python 3.9 compatible syntax: modules start with `from __future__ import annotations`; `Optional`, `List`, `Dict`, `Tuple`, `Sequence` from `typing`; no `X | None` at runtime.
- Textual 8 has no `Static.renderable`; read a Static's text with `str(widget.content)`.
- A pin is keyed by `(out_shot, in_shot)`, never by clip ids, and is applied only in `Pipeline.refine`, before the first pass (through `editorial.apply_pins`).
- No new dependencies.
- Never put a model identifier in code, docs or commit titles. Commit with `git -c user.name="Claude" -c user.email="noreply@anthropic.com" commit -F <msgfile>`, the message file written in `/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/`. The message ends with a `Co-Authored-By:` line carrying your own environment's attribution, then `Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ`. Push with `git push -u origin claude/ai-video-editing-pipeline-bkqqay` after each task. Never stage anything under `.superpowers/`. Never rewrite history.
- Real-backend and terminal-graphics acceptance stays **pending**: no test, doc or commit may claim it as done.

---

## File structure

| File | Change |
|---|---|
| `matchcut/pipeline/junctions.py` | `signed()`, `Junction.pin_origin`, origin word in history, `pin_moved` counts as what made a junction adjacent |
| `matchcut/tui/widgets/inspector.py` | origin-aware editor badge; Δ column through `signed()` |
| `matchcut/pipeline/editorial.py` | `apply_pins` moves a clip to make the pinned shots adjacent |
| `matchcut/tui/views/edit.py` | new wording of the "not adjacent in the rough cut" notification |
| `matchcut/pipeline/passes.py` | `ContinuityPass` softens a pinned jump cut, never splits a pinned junction |
| `tests/pipeline/test_junctions.py`, `tests/tui/test_widgets.py` | Task 1 tests |
| `tests/pipeline/test_editorial.py`, `tests/tui/test_edit.py` | Task 2 tests (and the two existing "not adjacent" expectations updated) |
| `tests/pipeline/test_passes.py` | Task 3 tests |
| `tests/pipeline/test_regressions.py` (new) | Task 4 end-to-end scenarios |
| `docs/M1-ACCEPTANCE.md`, `docs/PIPELINE.md`, `docs/TUI.md` | Task 4 docs |

---

### Task 1: Reverted pins say "reverted", and no negative zero

**Files:**
- Modify: `matchcut/pipeline/junctions.py`
- Modify: `matchcut/tui/widgets/inspector.py`
- Test: `tests/pipeline/test_junctions.py`, `tests/tui/test_widgets.py`

**Interfaces:**
- Produces: `junctions.signed(value: float, places: int = 2) -> str`; `Junction.pin_origin -> Optional[str]`; `JunctionState.change_from(prev, fps, lock_word: str = "locked") -> str`.

- [ ] **Step 1: Write the failing tests** (append to `tests/pipeline/test_junctions.py`; it already defines `clip(cid, shot, a, b, beat="b1", **attrs)` and `tl(stage, clips, history=())` helpers — read them first and reuse them)

```python
from matchcut.pipeline.junctions import signed  # add to the existing import block instead if you prefer


def test_signed_never_prints_negative_zero():
    assert signed(-0.001) == "+0.00"
    assert signed(-0.004) == "+0.00"
    assert signed(0.0) == "+0.00"
    assert signed(-0.006) == "-0.01"
    assert signed(0.25) == "+0.25"
    assert signed(-1.5, 1) == "-1.5"


def test_frame_delta_of_a_tiny_negative_move_is_plus_zero():
    assert frame_delta(16.21, 16.2099, 25.0) == "+0.00s (+0f)"
    assert frames_delta(-0.0001, 25.0) == "+0.00s (+0f)"


def _pinned_stages(origin):
    """roughcut -> matchcut where the editor pinned c001/c002 before the matchcut pass."""
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    pin = Decision("editor", "pinned", ["c001", "c002"], f"{origin} by the editor: out 2.00s, in 3.00s", None,
                   {"out_shot": 0, "in_shot": 1, "out_time": 2.0, "in_time": 3.0, "origin": origin, "note": "", "out": 2.0, "in_": 3.0})
    passed = tl("matchcut", [clip("c001", 0, 0.0, 2.0, locked_out=True), clip("c002", 1, 3.0, 5.0, locked_in=True)], history=[pin])
    return [("roughcut", rough), ("matchcut", passed)]


def test_a_reverted_pin_reads_reverted_in_the_history_and_the_origin():
    j = build_junctions(_pinned_stages("reverted"))[0]
    assert j.pin_origin == "reverted"
    assert j.history()[-1] == "editor (before matchcut): reverted"


def test_a_locked_pin_still_reads_locked():
    j = build_junctions(_pinned_stages("locked"))[0]
    assert j.pin_origin == "locked"
    assert j.history()[-1] == "editor (before matchcut): locked"


def test_a_junction_without_a_pin_has_no_origin():
    rough = tl("roughcut", [clip("c001", 0, 0.0, 2.0), clip("c002", 1, 3.0, 5.0)])
    assert build_junctions([("roughcut", rough)])[0].pin_origin is None
```

Check `clip(...)` accepts `locked_out=`/`locked_in=` through `**attrs`; if its signature differs, adapt the calls, not the assertions. Add `frame_delta` / `frames_delta` / `Decision` to the imports if they are not already imported.

Append to `tests/tui/test_widgets.py` (it already has helpers such as `inspected_junction()`; read them and build the junctions below with `build_junctions` directly):

```python
from matchcut.pipeline.junctions import build_junctions
from matchcut.pipeline.timeline import Clip, Decision, Timeline


def _pinned_junction(origin):
    def t(stage, clips, history=()):
        return Timeline(source="s.mp4", fps=25.0, clips=clips, stage=stage, history=list(history))
    rough = t("roughcut", [Clip("c001", 0, 0.0, 2.0, "b1"), Clip("c002", 1, 3.0, 5.0, "b2")])
    d = Decision("editor", "pinned", ["c001", "c002"], f"{origin} by the editor: out 2.00s, in 3.00s", None,
                 {"out_shot": 0, "in_shot": 1, "out_time": 2.0, "in_time": 3.0, "origin": origin, "note": "", "out": 2.0, "in_": 3.0})
    final = t("final", [Clip("c001", 0, 0.0, 2.0, "b1", locked_out=True), Clip("c002", 1, 3.0, 5.0, "b2", locked_in=True)], [d])
    return build_junctions([("roughcut", rough), ("final", final)])[0], d


def test_a_reverted_pin_is_badged_reverted_by_the_editor():
    j, d = _pinned_junction("reverted")
    plain = render_inspector(j, "final", Pin(0, 1, 2.0, 3.0, "reverted"), pin_decision=d).plain
    assert "reverted by the editor" in plain and "locked by the editor" not in plain
    assert "pinned (reverted)" in plain


def test_a_locked_pin_is_badged_locked_by_the_editor():
    j, d = _pinned_junction("locked")
    plain = render_inspector(j, "final", Pin(0, 1, 2.0, 3.0, "locked"), pin_decision=d).plain
    assert "locked by the editor" in plain and "pinned (locked)" in plain


def test_the_inspector_never_prints_negative_zero():
    j, d = _pinned_junction("locked")
    j.states[-1].in_time = 2.99995  # a sub-frame rounding difference
    plain = render_inspector(j, "final", None).plain
    assert "-0.00" not in plain
```

(`Pin` is already imported in that test file; if not, import it from `matchcut.pipeline.editorial`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/pipeline/test_junctions.py tests/tui/test_widgets.py -q`
Expected: the new tests FAIL (`signed` not importable, `pin_origin` missing, history says `locked`, badge says `locked by the editor`).

- [ ] **Step 3: Implement**

In `matchcut/pipeline/junctions.py`, add after `SCORE_ROWS`:

```python
def signed(value: float, places: int = 2) -> str:
    """``value`` with an explicit sign, never ``-0.00``: a value that rounds to zero prints as ``+0.00``."""
    v = round(value, places)
    if v == 0:
        v = 0.0  # drops the sign of -0.0
    return f"{v:+.{places}f}"
```

Change `frames_delta` and `frame_delta` to format seconds with `signed(...)`:

```python
def frames_delta(seconds: float, fps: float) -> str:
    frames = int(round(seconds * fps)) if fps > 0 else 0
    return f"{signed(seconds)}s ({frames:+d}f)"


def frame_delta(t0: float, t1: float, fps: float) -> str:
    """The move from ``t0`` to ``t1`` in seconds and in whole frames on the frame grid (round(t1*fps) - round(t0*fps))."""
    frames = int(round(t1 * fps)) - int(round(t0 * fps)) if fps > 0 else 0
    return f"{signed(t1 - t0)}s ({frames:+d}f)"
```

Give `JunctionState.change_from` a `lock_word` parameter and use it where it appends `"locked"`:

```python
    def change_from(self, prev: "JunctionState", fps: float, lock_word: str = "locked") -> str:
        ...
        if self.locked and not prev.locked:
            parts.append(lock_word)
        return ", ".join(parts)
```

Add to `Junction` (next to `pinned`):

```python
    @property
    def pin_origin(self) -> Optional[str]:
        """How the editor pinned this junction ("locked" or "reverted"), from the latest pinned decision; None without one."""
        for d in reversed(self.decisions):
            if d.pass_name == "editor" and d.action == "pinned":
                origin = d.data.get("origin")
                return str(origin) if origin else "locked"
        return None
```

Make `_changes` pass the origin word for the stage a pin was applied before, and keep `history()` as is (it already prefixes `editor (before <stage>)`):

```python
    def _changes(self) -> List[Tuple[str, str]]:
        """(stage, change) for every stage after the baseline that moved something, in stage order."""
        out: List[Tuple[str, str]] = []
        origin = self.pin_origin or "locked"
        for prev, cur in zip(self.states, self.states[1:]):
            change = cur.change_from(prev, self.fps, lock_word=origin if self._pinned_in(cur.stage) else "locked")
            if change:
                out.append((cur.stage, change))
        return out
```

In `matchcut/tui/widgets/inspector.py`:
- import `signed` with the other junctions names;
- in `badges`, replace the lock badge line with

```python
    if shown.locked:
        if j.pinned:
            out.append(f"{j.pin_origin or 'locked'} by the editor")
        else:
            out.append("locked by matchcut")
```

- in `render_inspector`, the Δ cell: replace `f"{v1 - v0:>+8.2f}"` with `f"{signed(v1 - v0):>8}"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/pipeline/test_junctions.py tests/tui/test_widgets.py -q` — expected: all pass.
Then the full suites: `python3 -m pytest tests/pipeline -q` and `python3 -m pytest tests/tui -q` — expected: all pass. If the Edit snapshot (`tests/tui/__snapshots__/test_snapshots/test_view_snapshots[4-edit].svg`) fails only because a `-0.00` became `+0.00`, regenerate it with `python3 -m pytest tests/tui/test_snapshots.py --snapshot-update` and say so in the report; any other snapshot difference is a bug to investigate.

- [ ] **Step 5: Commit and push**

```bash
git add matchcut/pipeline/junctions.py matchcut/tui/widgets/inspector.py tests/pipeline/test_junctions.py tests/tui/test_widgets.py
# plus the snapshot file if it was regenerated
git -c user.name="Claude" -c user.email="noreply@anthropic.com" commit -F <msgfile>   # title: "Say reverted for reverted pins and never print -0.00"
git push -u origin claude/ai-video-editing-pipeline-bkqqay
```

---

### Task 2: Place a pin by moving a clip when its shots are not adjacent

**Files:**
- Modify: `matchcut/pipeline/editorial.py` (`apply_pins` and helpers)
- Modify: `matchcut/pipeline/junctions.py` (`_adjacency_detail` accepts `pin_moved`)
- Modify: `matchcut/tui/views/edit.py` (notification wording in `_pin`)
- Modify: `docs/PIPELINE.md`, `docs/TUI.md` (the sentences that say a pin on a pass-made junction is never placed)
- Test: `tests/pipeline/test_editorial.py`, `tests/tui/test_edit.py`

**Interfaces:**
- Consumes: `Pin`, `Decision`, `Timeline`, `Clip` as today.
- Produces: `apply_pins(tl, pins, bounds, min_clip) -> Tuple[Timeline, List[Decision]]` (same signature) that may emit `Decision("editor", "pin_moved", [moved_id, anchor_id], detail, None, pin.to_dict())` before the pin's `pinned` decision. Unplaced details are now exactly one of:
  - `shot N is not in this cut`
  - `shot N is held by another pin`
  - `moving shot N would split or reorder the beats`
  - `not enough room in these clips for the pinned points` (unchanged)

- [ ] **Step 1: Write the failing tests** (append to `tests/pipeline/test_editorial.py`; `cut()` there is `c001 shot0 b1 [0.5,2.0]`, `c002 shot2 b1 [4.6,5.8]`, `c003 shot1 b2 [2.6,4.0]`; `BOUNDS = {0: (0.0, 2.5), 1: (2.5, 4.5), 2: (4.5, 6.0)}`)

```python
def four() -> Timeline:
    """b1: shot0, shot2, shot3 | b2: shot1."""
    return Timeline(source="s.mp4", fps=25.0, clips=[
        Clip("c001", 0, 0.5, 2.0, "b1"), Clip("c002", 2, 4.6, 5.8, "b1"), Clip("c003", 3, 6.2, 7.6, "b1"), Clip("c004", 1, 2.6, 4.0, "b2"),
    ])


FOUR_BOUNDS = {0: (0.0, 2.5), 1: (2.5, 4.5), 2: (4.5, 6.0), 3: (6.0, 8.0)}


def test_a_pin_moves_the_incoming_clip_next_to_the_outgoing_one():
    out, decisions = apply_pins(four(), [Pin(0, 3, 2.0, 6.2)], FOUR_BOUNDS, min_clip=0.5)
    assert [c.shot_index for c in out.clips] == [0, 3, 2, 1]
    assert [d.action for d in decisions] == ["pin_moved", "pinned"]
    assert decisions[0].detail == "moved shot 3 next to shot 0 to hold the editor's pin"
    assert decisions[0].clip_ids == ["c003", "c001"]
    a, b = out.clips[0], out.clips[1]
    assert a.locked_out and b.locked_in and (a.source_out, b.source_in) == (2.0, 6.2)
    assert [c.beat_id for c in out.clips] == ["b1", "b1", "b1", "b2"]


def test_a_pin_moves_the_outgoing_clip_when_moving_the_incoming_one_would_split_a_beat():
    # pin shot2 -> shot1: moving shot1 (b2) after shot2 splits b1; moving shot2 (b1) to just before shot1 keeps the beats
    out, decisions = apply_pins(four(), [Pin(2, 1, 5.8, 2.6)], FOUR_BOUNDS, min_clip=0.5)
    assert [c.shot_index for c in out.clips] == [0, 3, 2, 1]
    assert [d.action for d in decisions] == ["pin_moved", "pinned"]
    assert decisions[0].detail == "moved shot 2 next to shot 1 to hold the editor's pin"


def test_a_pin_that_would_reorder_the_beats_is_not_placed():
    out, decisions = apply_pins(cut(), [Pin(1, 0, 3.0, 1.0)], BOUNDS, min_clip=0.75)
    assert [d.action for d in decisions] == ["pin_unplaced"]
    assert decisions[0].detail == "moving shot 0 would split or reorder the beats"
    assert [c.id for c in out.clips] == ["c001", "c002", "c003"]


def test_a_pin_on_a_missing_shot_is_not_placed():
    _, decisions = apply_pins(cut(), [Pin(0, 7, 2.0, 1.0)], BOUNDS, min_clip=0.75)
    assert [(d.action, d.detail) for d in decisions] == [("pin_unplaced", "shot 7 is not in this cut")]


def test_a_clip_held_by_an_earlier_pin_is_never_moved():
    # first pin holds shot0 -> shot2; the second wants shot3 right after shot0, which would need shot0's out-point
    out, decisions = apply_pins(four(), [Pin(0, 2, 2.0, 4.6), Pin(0, 3, 2.0, 6.2)], FOUR_BOUNDS, min_clip=0.5)
    assert [d.action for d in decisions] == ["pinned", "pin_unplaced"]
    assert decisions[1].detail == "shot 0 is held by another pin"
    assert [c.shot_index for c in out.clips] == [0, 2, 3, 1]
```

Replace the existing `test_a_pin_whose_shots_are_not_adjacent_is_reported_not_applied` body's detail check (`"not adjacent" in decisions[0].detail`) — it is superseded by `test_a_pin_that_would_reorder_the_beats_is_not_placed` above; delete the old test. In the same file, the test near line 251 that asserts `outcome.action == "pin_unplaced" and "not adjacent" in outcome.detail` must be read: if its scenario is now placed by a move, change it to assert the new outcome (`pinned`, preceded by `pin_moved`); if it is still unplaced, assert the new detail wording. Do not weaken it to "any outcome".

In `tests/tui/test_edit.py`, rewrite `test_a_pin_on_a_junction_a_later_pass_created_says_it_was_not_placed` as `test_a_pin_on_a_junction_a_later_pass_created_is_placed_by_a_move`: same setup (`refine_in(ws, target=2.0)`, pin `j0`), then in the app:
- the panel of `j0`'s shot pair contains `pinned (` and not `pin not placed` and not `pin pending` — **unless** the move is rejected for this data; in that case the test must instead assert the exact new unplaced detail. Determine which applies by running it; then make the test assert that one outcome exactly (and add a comment saying which and why);
- pressing `l` on a junction whose `baseline.stage != first_stage` notifies the new text: `these shots are not adjacent in the rough cut: the next run moves shot {in} next to shot {out}, unless that would split or reorder the beats or another pin holds them`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/pipeline/test_editorial.py tests/tui/test_edit.py -q`
Expected: the new tests FAIL (no `pin_moved`, old details).

- [ ] **Step 3: Implement** in `matchcut/pipeline/editorial.py`

Replace the pair lookup in `apply_pins` with a call to a new helper and keep the rest of the placement code as it is:

```python
def _beat_runs(clips: Sequence[Clip]) -> List[str]:
    runs: List[str] = []
    for c in clips:
        if not runs or runs[-1] != c.beat_id:
            runs.append(c.beat_id)
    return runs


def _make_adjacent(out: Timeline, pin: Pin) -> Tuple[Optional[Tuple[Clip, Clip]], Optional[Decision], str]:
    """The (outgoing, incoming) clips of ``pin``, moving one of them when they are not adjacent.

    Returns (pair, move decision or None, "") on success, or (None, None, reason) when the pin cannot be placed.
    """
    for a, b in zip(out.clips, out.clips[1:]):
        if (a.shot_index, b.shot_index) == pin.key:
            return (a, b), None, ""
    a = next((c for c in out.clips if c.shot_index == pin.out_shot), None)
    b = next((c for c in out.clips if c.shot_index == pin.in_shot), None)
    if a is None or b is None:
        return None, None, f"shot {pin.out_shot if a is None else pin.in_shot} is not in this cut"
    runs = _beat_runs(out.clips)
    reason = ""
    for moved, anchor, after in ((b, a, True), (a, b, False)):
        if moved.locked_in or moved.locked_out:
            reason = reason or f"shot {moved.shot_index} is held by another pin"
            continue
        if (after and anchor.locked_out) or (not after and anchor.locked_in):
            reason = reason or f"shot {anchor.shot_index} is held by another pin"
            continue
        order = [c for c in out.clips if c is not moved]
        at = order.index(anchor) + (1 if after else 0)
        order.insert(at, moved)
        if _beat_runs(order) != runs:
            reason = reason or f"moving shot {moved.shot_index} would split or reorder the beats"
            continue
        out.clips = order
        detail = f"moved shot {moved.shot_index} next to shot {anchor.shot_index} to hold the editor's pin"
        return (a, b), Decision("editor", "pin_moved", [moved.id, anchor.id], detail, None, pin.to_dict()), ""
    return None, None, reason
```

and in `apply_pins`:

```python
    for pin in pins:
        pair, moved, reason = _make_adjacent(out, pin)
        if pair is None:
            decisions.append(Decision("editor", "pin_unplaced", [], reason, None, pin.to_dict()))
            continue
        if moved is not None:
            decisions.append(moved)
        a, b = pair
        ...  # unchanged from here: room check, clamp, locks, tags, the "pinned" decision
```

Import `Clip` from `.timeline` and `Optional` from `typing` if needed. Update the module docstring: pins are applied to the rough cut; when the two shots are not adjacent, one clip is moved next to the other (never splitting or reordering beats, never moving a clip another pin holds).

Note on the "held by another pin" check: the expected test outcome for `test_a_clip_held_by_an_earlier_pin_is_never_moved` is `shot 0 is held by another pin` — the first candidate (move shot3 after shot0) is rejected because the anchor shot0's out-point is locked; the second candidate (move shot0) is rejected because shot0 itself is locked; the reported reason is the first one found.

In `matchcut/pipeline/junctions.py` `_adjacency_detail`, accept the editor's move as an explanation:

```python
    d = next((d for d in new if d.action in ("drop", "insert", "pin_moved") and named & set(d.clip_ids)), None)
```

In `matchcut/tui/views/edit.py` `_pin`, replace the appended note with:

```python
        if j.baseline.stage != j.first_stage:
            note += (f". these shots are not adjacent in the rough cut: the next run moves shot {j.in_shot} next to shot {j.out_shot}, "
                     "unless that would split or reorder the beats or another pin holds them")
```

(Keep whatever separator the existing code used before the note — match the current text's punctuation, only the sentence content changes; the test asserts the substring starting at `these shots`.)

Docs: in `docs/PIPELINE.md` and `docs/TUI.md`, replace every statement that a pin on a junction made by a later pass "cannot be placed until a regenerated rough cut pairs those shots" (grep for `not adjacent`, `regenerated rough cut pairs`, `not placed`) with the new behaviour and the three unplaced reasons. Also mention: a beat that holds a pinned clip is not reordered by the match-cut pass.

- [ ] **Step 4: Run the tests**

Run: `python3 -m pytest tests/pipeline/test_editorial.py tests/pipeline/test_junctions.py tests/tui/test_edit.py -q`, then both full suites. Expected: all pass.

- [ ] **Step 5: Commit and push** (title: "Place pins on pass-made junctions by moving a clip next to its partner")

---

### Task 3: Continuity softens a pinned jump cut

**Files:**
- Modify: `matchcut/pipeline/passes.py` (`ContinuityPass.apply` step 3)
- Test: `tests/pipeline/test_passes.py`

**Interfaces:**
- Consumes: `is_match_cut(a, b)` (already in `passes.py`).

- [ ] **Step 1: Write the failing tests** (append to `tests/pipeline/test_passes.py`; it has a module-scoped `ctx` fixture built on `matchcut_video` — read `test_continuity_merges_and_breaks_jump_cuts` and `test_continuity_inserts_a_separator_between_same_shot_clips` first and follow their construction of timelines and contexts)

Three tests:

1. `test_continuity_softens_a_pinned_jump_cut_without_moving_a_separator_in`: a timeline in one beat `[A shot0, B shot0 (non-contiguous, a jump cut), C shot1]` where A has `locked_out=True`, B has `locked_in=True` and both carry the `pinned` tag (an editor pin, not a match cut: no `match_out:`/`match_in:` tags). After `ContinuityPass().apply`: A and B are still adjacent in that order (no separator between them), B's `transition_in.kind == "dissolve"` with duration `min(ctx.cfg.transitions.dissolve_seconds, A.duration * 0.4, B.duration * 0.4)` rounded to 3 places, and a `dissolve` decision names both clips.
2. `test_continuity_leaves_a_match_cut_jump_risk_alone`: the same timeline but A carries `match_out:graphic` and B `match_in:graphic` (a match cut): B's transition stays `cut`, no `dissolve` decision for the pair, no separator inserted.
3. `test_continuity_still_inserts_a_separator_for_an_unpinned_jump_cut`: the existing behaviour is unchanged — if an existing test already proves this, do not duplicate it; name that test in the report instead.

Pick source ranges inside the fixture's shot bounds (the `ctx.shot_bounds`); make A and B non-contiguous within the same shot (a gap of at least 0.3 s) so the merge step does not join them.

- [ ] **Step 2: Run them to verify test 1 fails** (`python3 -m pytest tests/pipeline/test_passes.py -q`): today the pinned pair is skipped entirely, so no dissolve.

- [ ] **Step 3: Implement** — replace step 3's loop body in `ContinuityPass.apply`:

```python
        # 3. jump cuts: same shot back to back, or flagged by the match pass
        i = 1
        while i < len(tl.clips):
            a, b = tl.clips[i - 1], tl.clips[i]
            risky = a.shot_index == b.shot_index or "jump_cut_risk" in b.tags
            if not risky or is_match_cut(a, b):
                i += 1
                continue
            held = a.locked_out or b.locked_in  # an editor's pin holds these cut points: never split the pair
            sep = None if held else self._find_separator(tl, i)
            if sep is not None:
                clip = tl.clips.pop(sep)
                tl.clips.insert(i, clip)
                decisions.append(self.decision("insert", [a.id, clip.id, b.id], f"moved {clip.id} between {a.id} and {b.id} to break a jump cut"))
                i += 2
                continue
            if b.transition_in.is_cut:
                d = min(dissolve, a.duration * 0.4, b.duration * 0.4)
                b.transition_in = Transition("dissolve", round(d, 3))
                decisions.append(self.decision("dissolve", [a.id, b.id], f"softened jump cut with {d:.2f}s dissolve"))
            i += 1
```

- [ ] **Step 4: Run** `python3 -m pytest tests/pipeline/test_passes.py tests/pipeline/test_editorial.py -q`, then both full suites. Expected: all pass.

- [ ] **Step 5: Commit and push** (title: "Soften a pinned jump cut with a dissolve; never split a pinned pair")

---

### Task 4: End-to-end regression scenarios and the acceptance status

**Files:**
- Create: `tests/pipeline/test_regressions.py`
- Modify: `docs/M1-ACCEPTANCE.md`
- Possibly modify: `tests/tui/test_edit.py` (one TUI-level persistence check, below)

**Interfaces:**
- Consumes: everything above; fixtures `demo_video`, `matchcut_video` (session scoped, `tests/pipeline/conftest.py`), the mock backend, `Pipeline`, `PinStore`, `Pin`, `build_junctions`, `load_stage_timelines`, `junction_detail`, `render_inspector` is TUI-only (do not import it from pipeline tests).

Every scenario runs the real passes on the synthetic footage with the mock backend (`cfg.llm.backend = "mock"`, `cfg.render.enabled = False`). The analysis/describe/narrative stages are cached per workspace, so build one workspace per module (module-scoped fixture that runs `Pipeline(cfg).run()` once into `tmp_path_factory`) and copy it (`shutil.copytree`) for each scenario that writes pins.

- [ ] **Step 1: Write the scenarios** (each a separate test function; exact helper code is yours)

1. `test_cut_points_really_move_and_the_history_says_who_moved_them` — a mock run of `demo_video` with a tight `target_duration` (try 5.0; pacing trims). Assert: at least one junction of the final stage has `abs(current.out_time - baseline.out_time) >= 1/fps` or the same for `in_time`; for that junction, `history()` has a line naming a pass stage (not only the baseline), `why()` is not `"no pass changed this junction"`, and `junction_detail(j)` contains no `-0.00`.
2. `test_a_pin_on_shots_that_are_not_adjacent_is_placed_by_a_move` — take the workspace's `roughcut.json`, pick a beat with at least three clips (if the mock rough cut has none, build the rough cut by hand from `ctx.shot_bounds`: three clips of distinct shots in beat `b1`, one in `b2`, and pass it to `Pipeline(cfg).refine(timeline)`), pin the first clip's shot to the third clip's shot at their current points, run `refine`. Assert: the final cut has the two shots adjacent; the matchcut stage timeline's history contains a `pin_moved` then a `pinned` editor decision; the junction built from the final stage has `pinned` true, `pin_origin == "locked"`, and `why()` starts with `editor:`.
3. `test_a_pin_that_would_reorder_the_beats_reports_why` — pin a shot of the last beat to a shot of the first beat (reverse order). Assert the editor decision in the final history is `pin_unplaced` with detail `moving shot N would split or reorder the beats` (N the moved shot of the first candidate), and the clip order of the first pass equals the rough cut's.
4. `test_pins_keep_their_dissolves` — three sub-cases on the same workspace copy, each asserting on the final stage:
   - a pinned junction on a beat boundary: its `transition_in` in the final cut is the transitions pass's dissolve (kind `dissolve`, duration > 0) — the narrative's beats must ask for a dissolve; if the mock narrative's beat uses `cut`, set the beat's `transition_in` to `dissolve` in `narrative.json` before running (it is cached input), and say so in a comment;
   - a pinned jump cut (hand-built rough cut with two non-contiguous pieces of one shot back to back, pinned): continuity gives it a dissolve, and the pair stays adjacent;
   - a match cut on a beat boundary keeps its hard cut: build it with clips that carry `match_out:graphic`/`match_in:graphic` and locks in a hand-built rough cut handed to `refine(timeline, passes=["transitions"])`, and assert the transition is `cut` with a `keep_cut` decision.
5. `test_two_pins_survive_a_second_run_and_a_regenerated_rough_cut` — pin one junction `locked` at its current points and another `reverted` at its baseline points (use `build_junctions` to get them); run `Pipeline(cfg).run()`; then `Pipeline(cfg).invalidate_from("roughcut")` and run again. After each run assert, for both pins: `pin_outcome(final, pin).action == "pinned"`, the final junction of that shot pair has `pin_origin` equal to the pin's origin, and the junction's `history()` line for the pin stage ends with that origin word (`locked` / `reverted`).
6. `test_a_regenerated_rough_cut_without_the_shot_reports_it` — pin a pair, then hand-edit `roughcut.json` to drop the incoming shot's clip (it is a cache: refine reuses it), run `refine`, and assert the final history's editor decision for the pin is `pin_unplaced` with detail `shot N is not in this cut`.

One TUI-level check in `tests/tui/test_edit.py` — `test_a_reverted_pin_reads_reverted_everywhere_after_a_run`: on a copy of `mock_workspace`, pin a beat-boundary junction with origin `reverted` at its baseline points via `refine_in(ws, pin=Pin(..., origin="reverted"))`, open the Edit tab, select the pair, press Enter (expanded history), and assert the panel contains `reverted by the editor`, `pinned (reverted)`, `editor (before matchcut): reverted`, and contains neither `locked by the editor` nor `-0.00`.

- [ ] **Step 2: Run** `python3 -m pytest tests/pipeline/test_regressions.py tests/tui/test_edit.py -q`. Every scenario must pass on the code from Tasks 1-3. A scenario that fails exposes a defect: fix the defect in the pipeline code (with a focused unit test next to the existing ones) rather than bending the scenario, and describe it in the report. If the mock data cannot produce a scenario's precondition, build the timeline by hand as described — never skip or `xfail` a scenario.

- [ ] **Step 3: Acceptance status.** Add this section to `docs/M1-ACCEPTANCE.md`, directly under the title paragraph:

```markdown
## Status

- Automated checks: pass (`tests/pipeline`, `tests/tui`), including the
  regression scenarios in `tests/pipeline/test_regressions.py` (moved cut
  points, pins placed by a move, preserved dissolves, pins that persist across
  a regenerated rough cut).
- Mock-backend rehearsal (2026-10-18, 20 s Big Buck Bunny clip, headless):
  checklist items 4-6 behave as written; items 1-3 need a real backend.
- **Pending a human run:** the real model backend (item 1, and item 3 on real
  decisions) and an image-capable terminal (item 2). M1 is not accepted until
  the verdict table below is filled in by that run.
```

Also update checklist item 4's last sentences (they say a pass-made junction "cannot be placed until a regenerated rough cut pairs those shots") to the new behaviour: the next run moves one shot next to the other unless that would split or reorder the beats or another pin holds them, and the badge says which.

- [ ] **Step 4: Run both full suites**, then commit and push (title: "Add end-to-end regression scenarios for pins, moves and dissolves; mark M1 acceptance pending").
