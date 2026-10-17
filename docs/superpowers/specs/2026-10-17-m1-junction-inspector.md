# M1: Visual editorial inspection — design

Status: proposed. Follows the control-room TUI (`docs/superpowers/specs/2026-10-16-tui-design.md`).

## Why

The control room shows what the pipeline did, not what it made. Its Cut tab
draws coloured spans and lists decisions as log lines such as
`transitions:dissolve c002 c003`. The pipeline knows far more about every
junction than that: the match-cut pass scores structure, composition,
luminance, colour and motion at the cut, searches for better cut points
within a slack window, records the baseline and the improved score, and locks
the junction so later passes leave it alone. None of that reaches the screen.

M1 is a deliberately narrow milestone. It proves, or disproves, that a
terminal UI can support real editorial judgement by building one interaction
well: **inspect a cut, understand why it was made, and keep or undo it.** The
other tabs are not redesigned in M1.

## Decision gate

After M1, an editor opens a real cut of real footage made with a real model
backend, in their own terminal, and asks one question:

> Can I inspect a cut and understand why matchcut made it, without reading a
> log file or a JSON document?

- **Yes:** continue with Textual. The next milestones turn the other tabs into
  the Footage Library (contact sheets), Story Architect and Control Room.
- **No, because of image fidelity, latency or the lack of motion preview:**
  keep Textual as the control room and build the review surface separately
  (for example a local HTML review page fed by the same junction data, which
  M1 already exposes through a CLI command).

The acceptance procedure for this gate is written down in `docs/M1-ACCEPTANCE.md`.

## What the editor sees: the Edit workbench

Tab `4` becomes **Edit** and replaces the Cut tab. Layout at 120x40:

```
stage final (8/8) · 5 clips · 6.9s · 4 junctions · 1 pinned                    [ ] stage   ←/→ junction
1:s0 ▏2:s1 ~3:s3 ║ 4:s6 ▏5:s7                                       (track; selected junction highlighted)
beats  b1 Setup ─────── b2 Develop ──────── b3 Resolve                (beat strip under the track)
┌ Junction 2 · c002 → c003 · shot 1 → shot 3 · beat b1 → b2 ───────────────────────────────────────────┐
│ [outgoing: last frame of c002]   [incoming: first frame of c003]   [overlay: 50% blend]               │
│  4.99s                            7.12s                              shape / position alignment       │
│                                                                                                       │
│ MATCH CUT (graphic) · locked by matchcut · beat boundary · pinned by you                              │
│            rough cut   final    Δ                                                                     │
│ structure    0.41      0.63   +0.22                                                                   │
│ composition  0.38      0.71   +0.33                                                                   │
│ luminance …  colour …  motion …  combined 0.44 → 0.69                                                 │
│ cut points  out 5.00s → 4.88s (−3f)   in 7.00s → 7.12s (+3f)                                          │
│ why: graphic match cut (trimmed to best junction): accepted on feature score alone                    │
│ history: roughcut cut · matchcut trimmed + locked · transitions dissolve 0.30s · polish unchanged      │
└ l lock these points · v revert to rough cut · x clear pin · o open overlay · Enter full history ──────┘
```

### Selection
- `←`/`→` move between junctions (junction *j* sits between clip *j* and *j+1*).
- A mouse click on a junction glyph in the track selects it.
- `[`/`]` choose the stage being inspected (default: final). The comparison is
  always "first stage this junction exists in" versus "the selected stage".

### Evidence
- **Frames.** Outgoing = the last frame shown before the cut
  (`out_time − 1/fps`); incoming = the first frame after it (`in_time`). Both
  come from the workspace frame cache (the same cache the Shots view uses), at
  the selected stage's cut points. A third image is the 50% blend of the two:
  the classic match-cut check for shape and position alignment.
- **Scores.** The match-cut scorer is rebuilt from the workspace's cached
  `analysis.npz` and evaluated at each state's cut points, so the numbers are
  exactly what the pass compared: structure, composition, luminance, colour,
  frame, motion, combined, motion type, jump-cut risk. If `analysis.npz` is
  missing the panel says so instead of showing numbers.
- **Cut points.** Out and in times at the first stage and at the selected
  stage, with the difference in seconds and in frames.
- **Why.** The latest decision that names both clips (match cut, rejected,
  jump-cut risk, transition, polish edit, beat sync, pin) as one sentence;
  `Enter` expands every decision across every stage.
- **History.** One compact line per stage where the junction changed (cut
  points, transition, locks), so the effect of each pass is visible.
- **Badges.** Match cut (type), locked (by which pass or by the editor),
  beat boundary, jump-cut risk, pinned, pin pending (pinned but not yet
  reflected in the stage shown).

### Junction identity across stages
A junction is the cut between two consecutive clips of the reference stage.
Passes drop, merge, insert and renumber clips, so ids do not carry across
stages. An earlier stage's clip is the same clip when it comes from the same
source shot and its source range overlaps (the best overlap wins), and the
junction has a state at every stage where its two clips are adjacent; the first
such stage is its baseline.

Decisions are attributed per stage: the ones a stage added (its history beyond
the previous stage's) are read against the junction's ids in the previous stage
first, because a pass records ids before it renumbers, and against the stage's
own ids only for clips the pass created. Decisions naming the junction's two
clips (at most three ids) are junction-level; single-id decisions (trim, drop,
set_transition) on one of its clips are clip-level and only explain it when
nothing else does; whole-timeline records (assessment, fit, reorder) are not
attributed.

## Keeping or undoing a decision: editorial pins

- `l` **lock** pins the junction at the selected stage's cut points.
- `v` **revert** pins it at its baseline (first-stage) cut points: the editor
  rejects what the passes did to it.
- `x` clears the pin.

Pins live in `workspace/editorial.json`, keyed by the pair of **shot
indices**, not clip ids, because a regenerated rough cut gets new clip ids
while the shots stay the same. A pin stores the out and in times and its
origin (`locked` or `reverted`).

Pins survive regeneration:
1. `Pipeline.refine` applies the pins to the rough cut before any pass runs:
   wherever shot A is immediately followed by shot B, the outgoing clip's
   out-point and the incoming clip's in-point are set to the pinned times
   (clamped to the shots and the minimum clip length) and both ends are
   locked. Every pass already honours locks, so the junction comes through
   unchanged. Each applied pin is recorded as a decision by the pass
   `editor`; a pin whose shots are not adjacent in the new cut is recorded as
   `pin_unplaced` and listed in the Edit header.
2. The rough-cut prompt carries the pins as editor constraints ("keep shot A
   immediately followed by shot B"), so a regenerated rough cut keeps the
   pairing when the model can.

Pins take effect on the next run (`R`): cached analysis, descriptions,
narrative and rough cut are reused and the passes re-run. A pin made while
paused at a pass checkpoint takes effect on that run's next redo of the rough
cut or on the next run; the notification says which.

## Honest status (fixes found in the screenshots)

- **Mock runs are labelled.** With the mock backend the status bar shows a
  `MOCK` badge, the token and cost fields read `mock` instead of `0/0 tok
  ~$0.000`, and model lines in the log end with `(mock)`.
- **Skipped stages read as skipped.** When a run finishes, stages that never
  ran become `skipped` (render: "skipped: render disabled"), and the status
  says `finished · analysis only` or `finished · rendered <file>`.

## Text access for the same data

`python -m matchcut.pipeline.cli junctions WORKSPACE [--index N]` prints the
junction table (one line per junction) or one junction's full record. It
proves the data layer independently of the TUI and is the starting point for
a separate review surface if the decision gate says so.

## Architecture

- `matchcut/pipeline/junctions.py` (new, no UI imports): stage timeline
  layout (`stage_timeline_paths`, the single definition the TUI's `RunState`
  then delegates to), `JunctionState`, `Junction`, `build_junctions(stages,
  scorer, fps)`, `scorer_for_workspace(ws, cfg)`.
- `matchcut/pipeline/editorial.py` (new): `Pin`, `PinStore`, `apply_pins`,
  `pin_constraints`.
- `matchcut/pipeline/pipeline.py`, `context.py`, `prompts.py`, `cli.py`: apply
  pins in `refine`, constraints in the rough-cut prompt, `junctions` command.
- `matchcut/tui/views/edit.py` (new) replaces `views/cut.py`;
  `matchcut/tui/widgets/inspector.py` (new) renders the junction panel text;
  `widgets/timeline.py` gains junction selection and click hit-testing;
  `images.py` gains `blend`; `state.py`, `app.py`, `views/run.py` get the
  honest-status changes.

The TUI still depends on the pipeline and never the reverse.

## Testing

- Unit: junction building across stages (adjacency, reorder, baseline,
  deltas in frames), scorer reconstruction equals the pass's recorded scores
  for an accepted match cut, pin application (placement, clamping, locks,
  unplaced), pin store round-trip, constraint text, track hit-testing.
- Integration: a mock run on the match-cut fixture footage; pin a junction
  with altered points; invalidate and regenerate the rough cut; the final cut
  has the pinned points, both ends locked, and an `editor:pinned` decision.
- TUI pilot: select a junction by key and by click; the three frames render
  with real height; the score table and the why line show the recorded
  values; `l`, `v`, `x` write `editorial.json`; badges; idempotent refresh.
- Snapshot of the Edit tab replaces the Cut snapshot.
- Acceptance (human, outside CI): `docs/M1-ACCEPTANCE.md` on real footage
  with a real model backend in a real terminal.

## Out of scope for M1

Contact-sheet Shots view, Story Architect, a slimmed Config, accepting or
rejecting each pass's refinements individually (beyond revert-to-baseline),
motion preview or video playback, per-beat regeneration.

## Assumptions

- Real-backend acceptance needs an `ANTHROPIC_API_KEY` in the environment
  where the editor runs it; this cloud session has none, so the automated
  part of M1 runs on the mock backend and the human gate runs locally or in
  an environment with the key.
- The 20 s Big Buck Bunny clip used earlier is the default acceptance
  footage; any real footage works.
