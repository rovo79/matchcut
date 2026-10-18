# M1 acceptance: can a cut be understood without logs?

M1 is accepted when an editor, on real footage with a real model backend in
their own terminal, can inspect cuts and say why matchcut made them without
reading a log file or a JSON document.

## Status

- Automated checks: pass (`tests/pipeline`, `tests/tui`), including the
  regression scenarios in `tests/pipeline/test_regressions.py` (moved cut
  points, pins placed by a move, preserved dissolves, pins that persist across
  a regenerated rough cut).
- Mock-backend rehearsal (2026-10-18, headless, 20 s Big Buck Bunny clip): it
  covered the mechanics behind checklist items 4-6 (lock and revert pins pending
  and then pinned after a re-run, pins held through a redo of the rough cut, the
  first visit's frame extraction at about 0.4 s). It ran before the stabilization
  work and found two defects, a reverted pin labelled "locked" and "-0.00" in
  printed deltas; both are fixed. Items 4-6 have **not** been re-rehearsed
  against the new pin-move behaviour (a pin on a junction a pass created is now
  placed by moving a clip), and item 4 now describes pin moves no rehearsal
  exercised. Items 1-3 need a real backend.
- **Pending a human run:** the real model backend (item 1, and item 3 on real
  decisions) and an image-capable terminal (item 2). M1 is not accepted until
  the verdict table below is filled in by that run.

## Set up
- Footage: any real clip of 15-60 s with several shots (the default is the
  20 s Big Buck Bunny clip, which has only three shots; a clip with more cuts
  gives more junctions to judge).
- A real backend: export `ANTHROPIC_API_KEY` (in a cloud environment, add it
  as an environment secret; never paste it into a chat).
- A terminal with an image protocol: Kitty, WezTerm, Ghostty or iTerm2
  (Sixel terminals also work). Plain terminals fall back to half-blocks; note
  which one the status bar reports.

## Run
    pip install -r requirements-pipeline.txt -r requirements-tui.txt
    python -m matchcut.tui footage.mp4 -w ws_m1 --backend anthropic
Press R, continue through the checkpoints, then press 4 for the Edit tab.

In the Edit tab: `←`/`→` (or a click on a junction glyph) select a junction,
`[`/`]` inspect an earlier stage, `Enter` expands the full history, `o` opens
the overlay image, and `l` / `v` / `x` lock, revert and clear a pin.

## Checklist
1. The status bar does **not** show a MOCK badge, and shows tokens and a cost
   estimate.
2. The status bar's images entry names a real protocol (kitty graphics, sixel)
   or half-block.
3. For three junctions, including at least one match cut and one beat
   boundary: from the frames, the overlay, the scores, the why line and the
   history alone, write one sentence on why the cut is there. Note any
   junction where you could not.
4. Lock one junction (`l`) and revert another (`v`). Each badge reads
   `pin pending: run again (R) to apply` and the header counts two pins. Press
   R: the run goes back through the describe, narrative and rough-cut
   checkpoints (press `c` at each to continue), then the passes. Both junctions
   now read `pinned (locked)` / `pinned (reverted)` with no `pin pending`,
   whichever stage you inspect. A pin holds the cut points, not the
   transition: a junction on a beat boundary keeps its dissolve (the history
   line reads `editor (before matchcut)`, not a pass). If a badge says
   `adjusted to out X / in Y to fit the clips` or `pin not placed: <reason>`,
   note it and whether the reason is right; the header counts the pins not
   placed (`N pin(s) not placed`) and the badge says why. A junction that a
   later pass created (its history starts after the rough cut) is placed by
   the next run: it moves one shot next to the other, unless that would split
   or reorder the beats or another pin holds a clip, and the badge says which
   (`pinned (...)` once placed, `pin not placed: <reason>` otherwise); the
   junction's why line then reads `editor: ...`.
5. Regenerate the rough cut: run again, choose redo with a note at the rough
   cut checkpoint. After the passes, both pins still hold, or the header
   says which could not be placed and why.
6. Latency: the first visit to a junction extracts its frames synchronously, so
   the tab pauses for a moment; later visits are cached and `←`/`→` feels
   immediate. Note the first-visit delay. It is an implementation limit of
   this view (the extraction runs on the UI thread), not a Textual limit.

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
