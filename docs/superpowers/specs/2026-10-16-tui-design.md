# Control-room TUI for the editing pipeline: design

Status: agreed in conversation on 2026-10-16. Approach A of three considered:
a Textual app with the pipeline running in-process, built so the pipeline
side can later move behind a socket (approach B) without changing screens.

## Goal

A terminal application that drives and watches pipeline runs on one clip:
launch stages and passes, see progress and Claude's decisions live, pause at
checkpoints between stages to read the result and steer with a note, tweak
the config, re-run a stage, and inspect everything the run produced (shots,
story, cut) with real frames rendered in the terminal.

Primary job: drive and watch runs. Secondary: inspect a finished workspace.
Not in scope: hand-editing in/out points or clip order (a separate "editor's
bench" project), remote or detached runs, batch/library management.

## Architecture

Two layers; the TUI depends on the pipeline, never the reverse.

### Pipeline additions (`matchcut/pipeline/`)

**Event bus.** `Pipeline(cfg, on_event=None, on_checkpoint=None, cancel=None)`.
`on_event` is a callable receiving one `Event` per occurrence, called on the
pipeline's thread. Event types (dataclasses in a new `events.py`):

| Event | Fields | Emitted when |
|---|---|---|
| `RunStarted` | `source, workspace, config` | `run()` begins |
| `StageStarted` | `stage` (ingest, describe, narrative, roughcut, pass:<name>, render, report) | a stage starts (not emitted for cached stages) |
| `StageCached` | `stage, path` | a stage result is reused from the workspace |
| `StageFinished` | `stage, seconds, path` (file written) | a stage completes |
| `Decision` | `pass_name, action, clip_ids, detail, score, data` (mirrors `timeline.Decision`) | each pass decision, as made |
| `ModelCall` | `name, images, seconds, input_tokens, output_tokens, model` | after each model call |
| `Log` | `level, message` | informational messages (replaces nothing; `logging` keeps working) |
| `Checkpoint` | `stage, summary` | a checkpoint is reached, before blocking |
| `RunFinished` | `final_path, render_path, report_path, seconds` | `run()` completes |
| `RunFailed` | `stage, error, traceback` | an exception escapes a stage |

`Decision` events are produced by `run_passes` from each `PassResult`;
`ModelCall` events by the `LLMBackend` wrapper (the backend gets an optional
`on_call` callback that `Pipeline` wires to the bus). Cached stages emit
`StageCached` so the ladder can show them as such.

**Checkpoints.** `on_checkpoint(stage: str, summary: dict) -> CheckpointAnswer`
is called after describe, narrative, roughcut, and after every pass, from the
pipeline thread, and blocks until it returns. `CheckpointAnswer` is one of
`Continue()`, `Redo(note: str)`, `Abort()`.

- `Continue` proceeds.
- `Redo(note)` deletes that stage's cached output (descriptions.json,
  narrative.json, roughcut.json, or the pass's timeline file), appends the
  note to `ctx.notes[stage]` (a new `Dict[str, List[str]]` on `Context`),
  re-runs the stage, and checkpoints again. Notes are rendered into the
  stage's prompt under a "Director's notes" heading (describe, narrative,
  rough cut, polish). For passes without a prompt (matchcut, continuity,
  pacing, transitions, beatsync) the note is recorded as a `Decision` with
  action `note` and the pass simply re-runs; the TUI tells the user those
  passes are deterministic and a redo only matters after a config change.
- `Abort` raises `RunAborted`, which `run()` turns into `RunFailed` with
  `stage` set and `error = "aborted"`.

The CLI passes no handler; the default handler returns `Continue()`.
`summary` is a small dict per stage: shot count and unusable count after
describe; logline, beats (id, title, pace, target) after narrative; clip
count, duration, per-beat durations after roughcut; decision counts by
action and timeline duration after a pass.

**Cancellation.** `cancel` is a `threading.Event`. `run_passes` and `run()`
check it between stages and between passes and raise `RunAborted`. Work inside
a stage finishes its current step.

**Director's notes in prompts.** `prompts.DESCRIBE`, `NARRATIVE`, `ROUGH_CUT`
and `POLISH` gain a `{notes}` placeholder filled with "Director's notes:
(none)" or a bulleted list. The mock backend ignores notes.

### TUI package (`matchcut/tui/`)

```
matchcut/tui/
  __init__.py
  __main__.py      python -m matchcut.tui footage.mp4 [--workspace W] [--config C] [--backend ...]
  app.py           MatchcutApp(App): layout, key bindings, worker, event pump
  state.py         RunState: config, stage statuses, event log, lazy workspace views
  bridge.py        PipelineBridge: runs Pipeline in a worker thread, queue of events,
                   checkpoint future, cancel flag
  views/
    run.py         stage ladder + live log + checkpoint panel
    shots.py       shot catalog table + frames + description
    story.py       narrative cards + music energy sparkline
    cut.py         timeline track + clip detail + pass stepper
    config.py      editable config tree
  widgets/
    ladder.py      StageLadder
    timeline.py    TimelineTrack (clips as coloured spans, beat grid, markers)
    frame.py       Frame: image widget with Pillow cache and mosaic fallback
    sparkline.py   EnergyCurve
  images.py        load/cache PIL images by path; pick textual-image protocol
  styles.tcss      Textual CSS
```

Dependencies: `textual>=8`, `textual-image>=0.8` (both import-guarded by the
launcher with an install hint). Pillow is already a pipeline dependency.

### Threading model

`PipelineBridge.start(cfg)` creates `Pipeline(cfg, on_event=queue.put,
on_checkpoint=self._checkpoint, cancel=self.cancel)` and runs `pipeline.run()`
in a Textual thread worker. The app pumps `queue` on a 50 ms timer onto the UI
thread and dispatches to `RunState` and the views. `_checkpoint` stores the
stage and summary, emits the `Checkpoint` event, and waits on a
`threading.Event` that the UI sets together with the answer. Only one run at a
time; `R` while running is ignored, `S` sets `cancel`.

## Screens

One app, five views switched with keys `1`-`5` or a tab bar. A persistent
status bar shows: source name, stage, elapsed time, model calls, tokens in/out,
and an estimated cost (from a small per-model price table in the TUI; shown as
"~$" and labelled an estimate).

1. **Run** (`1`). Left: `StageLadder` with one row per stage (ingest,
   describe, narrative, roughcut, each pass, render, report) showing state
   (pending, cached, running with spinner, done with seconds, waiting at
   checkpoint, failed). Right: live log of `Log`, `Decision` and `ModelCall`
   events, newest at bottom, filterable by kind with `f`. Bottom: checkpoint
   panel when paused, with the stage summary, a one-line note input, and
   `c` continue, `r` redo with the note, `a` abort.
2. **Shots** (`2`). `DataTable` of the catalog: index, start-end, duration,
   subject, shot size, camera movement, quality, usable. Selecting a row shows
   head/middle/tail frames (from `frames/` via `FrameCache` paths; extracted on
   demand if missing) and Claude's description. `u` toggles a shot's `usable`
   flag in `descriptions.json`, which affects the next narrative run.
3. **Story** (`3`). Logline, theme, structure notes; beats as cards (id,
   title, intent, tone, pace, target duration, shot ids). With music: tempo,
   duration and an `EnergyCurve` sparkline with beat boundaries marked where
   the current timeline places them.
4. **Cut** (`4`). `TimelineTrack`: clips as spans scaled to duration,
   coloured by beat, with transition glyphs, a lock glyph on match-cut
   junctions, and the beat grid underneath when there is music. `←`/`→`
   select clips; the detail pane shows the clip's first frame, shot, source
   range, duration, transition, tags, and every decision across passes that
   names this clip. `[`/`]` step the view through `roughcut.json` and
   `passes/NN_*.json` to see each pass's effect; the header names the stage
   shown.
5. **Config** (`5`). A `Tree` of `PipelineConfig` sections and fields with
   inline editing (Enter to edit, type-checked against the dataclass field
   type, Esc to cancel). Changes write to `RunState.config` and are saved to
   the workspace `config.json` on the next run or redo. `--set`-style paths are
   shown in the footer for reference.

Global keys: `R` run or resume (opens on the Run view), `S` stop, `o` open the
current image or the latest render in the system viewer (`xdg-open` /
`open`), `?` help overlay, `q` quit (confirms while a run is active).

## Data flow and state

`RunState` holds: `config: PipelineConfig`, `stages: Dict[str, StageStatus]`,
`events: List[Event]` (bounded to the last 5,000), `checkpoint:
Optional[PendingCheckpoint]`, usage totals, and lazy accessors `shots()`,
`descriptions()`, `narrative()`, `music()`, `timeline(stage)` that read the
workspace files and cache by file mtime. Views render from `RunState` only.
`StageFinished.path` triggers an invalidation of the matching accessor.

Opening the TUI on an existing workspace with no run populates `RunState`
from the files present, so it is also a viewer for past runs.

Images: `images.load(path, max_width)` returns a cached PIL image; `Frame`
wraps `textual_image.widget.Image`, which auto-selects the Kitty, iTerm2 or
Sixel protocol and falls back to half-block rendering. A one-time footer
notice says which mode is active.

## Error handling

- An exception in any stage becomes `RunFailed`; the ladder marks the stage
  failed, the log shows the traceback (collapsed, `t` expands), and the app
  stays up. Cached inputs remain, so fixing config and pressing `R` resumes
  from the failed stage.
- Model refusals and API errors (`LLMError`) are shown with their message in
  the checkpoint-style panel with `R` to retry.
- Stop (`S`) sets the cancel flag; the ladder shows "stopping" until the
  pipeline raises `RunAborted` at the next boundary.
- Missing `textual`/`textual-image`: the launcher prints
  `pip install -r requirements-tui.txt` and exits 1; the CLI is unaffected.
- Terminals without image support: mosaic fallback plus a one-time notice.
- A workspace from an older run without `music.json` or `passes/` simply
  shows fewer things; no view may crash on a missing file.

## Testing

Pipeline side (`tests/pipeline/`):
- `test_events.py`: with the mock backend on the demo footage, `run()` emits
  `RunStarted`, the stage events in order, a `Decision` per pass decision,
  `ModelCall` per mock call, `RunFinished`; a second run emits `StageCached`
  for cached stages.
- `test_checkpoints.py`: a scripted handler returns `Redo("end on the bird")`
  once at the narrative checkpoint, then `Continue`; assert the narrative
  stage ran twice, `narrative.json` was rewritten, the note appears in the
  second narrative prompt (recording mock), and the decision log records the
  note. A handler returning `Abort()` at roughcut yields `RunFailed` with
  `error == "aborted"` and no `final.json`. Cancel flag set during passes
  stops before the next pass.

TUI side (`tests/tui/`, Textual `App.run_test()` pilot, headless):
- Load a finished mock workspace (built by a session fixture running the
  pipeline once) and assert each view shows its data: shot count rows, beat
  cards, clip spans equal to clip count, config tree has the `music` section.
- Run the mock pipeline inside the app with a scripted checkpoint handler
  injected through the bridge; assert the ladder advances, the checkpoint
  panel appears at narrative, pressing `c` continues, and `RunFinished` sets
  the status bar.
- Snapshot tests (pytest-textual-snapshot) of the five views at 120x40 with
  the mock workspace; images render in mosaic mode under the pilot.
- `images.load` caching and the mosaic fallback path are unit-tested without
  a terminal.

## Dependencies and files

New: `requirements-tui.txt` (`textual>=8`, `textual-image>=0.8`,
`pytest-textual-snapshot` for tests). Docs: a `docs/TUI.md` with screenshots
(text captures) and the key map, plus a README line.

## Implementation split

Two plans, in order, each shippable on its own:

1. **Pipeline events, checkpoints and director's notes** (`events.py`, the
   `Pipeline` constructor arguments, `run_passes` decision events, backend
   `on_call`, `Context.notes`, prompt placeholders, cancel flag, and the two
   pipeline-side test files). The CLI behaves exactly as before.
2. **The TUI** (`matchcut/tui/`, views, widgets, images, launcher, docs,
   `requirements-tui.txt`, `tests/tui/`), built on plan 1's interfaces.

## Deferred from the first implementation

- `t` to expand a collapsed traceback: the Run log shows the last 800
  characters of a failure's traceback.
- An LLMError panel with `R` to retry: model errors currently go to the log and
  the status bar.
- A spinner on the running stage in the ladder.

## Open follow-ups (not in this spec)

Approach B (pipeline behind a local socket for detached/remote runs); the
editor's bench (manual trims, reorders, match-cut accept/reject) which would
reuse `TimelineTrack` and `Frame`; per-decision interactive approval.
