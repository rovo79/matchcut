# AI editing pipeline

`matchcut.pipeline` turns raw footage into a polished cut in six stages. The
model (Claude) is used where judgement is needed - reading the footage, finding
the story, assembling the rough cut, verifying match cuts, final polish - and
cheap numpy/ffmpeg analysis is used everywhere else.

```
footage.mp4
   │
   ▼
 1. ingest      ffprobe → low-res decode → per-frame series → shot boundaries
   │            → per-cell features (structure / composition / colour / motion)
   ▼
 2. describe    keyframes per shot → contact sheets → Claude (vision)
   │            → per-shot description: subject, shot size, movement, shapes, mood …
   ▼
 3. narrative   shot catalog + brief → Claude → logline, theme, 3-7 beats
   │            (intent, tone, pace, target duration, candidate shots)
   ▼
 4. rough cut   beats + catalog → Claude → ordered clips with in/out points
   │            (validated and clamped against the real shot bounds)
   ▼
 5. passes      matchcut → continuity → pacing → transitions → polish
   │            each pass = pure function (timeline, context) → (timeline, decisions)
   ▼
 6. deliver     final.json / final.edl (CMX3600) / final.csv / final.mp4 / report.md
```

## Quick start

```shell
pip install -r requirements-pipeline.txt      # plus ffmpeg on PATH
export ANTHROPIC_API_KEY=...                   # or `ant auth login`

python -m matchcut.pipeline run footage.mp4 \
    --brief "a quiet morning in the workshop; end on the finished chair" \
    --target-duration 60 \
    --music track.mp3 --music-start 12
```

With `--music`, the track is analysed for tempo and beats, the narrative stage is told about its energy curve, the `beatsync` pass moves cuts onto beats, and the render mixes it under the source audio (ducked, loudness-normalised). `--music-gain` sets the music level in dB.

Outputs land in `footage_edit/` (override with `-w`). Run `--backend mock` to
exercise the whole pipeline without a model (deterministic heuristics stand in
for Claude) - handy for checking shot detection and render settings on a new
source before spending tokens.

Try it on synthetic footage:

```shell
python -m matchcut.pipeline.demo demo.mp4
python -m matchcut.pipeline run demo.mp4 --backend mock -t 10 --proxy
```

## Stages in detail

### 1. Ingest (`features.py`, `shots.py`)

The footage is decoded once by ffmpeg at 160x90 / 8 fps and streamed through
numpy. Two outputs:

* **Frame series** - colour-histogram chi-square distance, mean pixel
  difference and coarse-layout correlation between consecutive frames. Shot
  boundaries are local peaks that exceed both an absolute threshold and a
  rolling median + k·MAD, with a minimum shot length. A peak whose layout
  correlation stays high is a fade, flash or exposure change and is ignored.
* **Cell features** every 0.25 s - a zero-mean 32x18 luminance thumbnail
  (structure), a 3x3 grid of gradient-orientation histograms (composition),
  a 4x4x4 RGB histogram (colour), a 3x3 luminance grid, and motion: energy,
  dominant direction/speed from phase correlation, and a 3x3 "where it moves"
  grid. Frame pairs that straddle a probable cut are excluded from motion.

Everything is cached in `analysis.npz`; a one-hour source is ~40 MB.

### 2. Describe (`stages.describe_shots`)

Three keyframes per shot (head / middle / tail) are tiled into labelled contact
sheets, eight shots per sheet, three sheets per request. Claude returns a
structured description per shot (`schemas.ShotDescription`). Machine notes
(duration, brightness, motion energy, dominant direction) go in the prompt so
the model can combine what it sees with what was measured.

### 3. Narrative (`stages.find_narrative`)

Text only: the shot catalog (descriptions + stats) and the brief. The model
returns a logline, a theme, and beats with intent, emotional tone, pace,
target duration, candidate shots (best first) and the transition leading into
the beat. Shot ids are validated; beat durations are normalised to the
requested running time.

### 4. Rough cut (`stages.rough_cut`)

The model selects and orders clips per beat with explicit in/out points. The
pipeline clamps every clip to its shot, to the min/max clip length, drops
duplicates, and keeps beats in narrative order.

### 5. Passes (`passes.py`)

Passes run in the order given by `--passes` (default: all six). Each one
records `Decision`s that end up in `report.md`, and the timeline after every
pass is saved to `passes/NN_name.json` so you can diff them.

| Pass | What it does |
|---|---|
| `matchcut` | For every junction, searches up to 1.5 s around the out/in points for the best-scoring cut (structure + composition + luminance + colour, plus motion direction/speed agreement). Optionally reorders clips inside a beat when that creates match-cut opportunities. Candidates above the threshold are rendered as side-by-side OUT/IN sheets and judged by Claude (`match_cut` / `acceptable_cut` / `jump_cut` / `bad_cut`). Verified matches get their new in/out points and are **locked** so later passes keep them. |
| `continuity` | Merges contiguous pieces of the same shot, drops flash frames, and breaks jump cuts (same shot back-to-back or flagged by the match pass) by moving another clip in between, or softening with a short dissolve. |
| `pacing` | Applies each beat's pace (`slow`/`medium`/`fast` → typical clip length), fits each beat and the whole cut to the target durations by trimming unlocked ends toward the motion peak, extending into unused footage, or dropping the lowest-value clips. |
| `transitions` | Beat changes get the transition the narrative asked for (cut / dissolve / dip to black), clamped to a fraction of the neighbouring clips; a beat change that lands on a verified match cut stays a hard cut. Adds fade in/out. |
| `polish` | Claude reviews a storyboard sheet (first frame of every clip) plus the cut list and proposes up to N small edits: trim/extend by seconds, drop, move before, set transition. Edits are validated; anything touching a locked match-cut point is rejected and reported. |
| `beatsync` | With a music track: moves each cut onto the nearest beat (a downbeat at beat changes when one is within `music.snap_window`) by adjusting the outgoing clip's unlocked end. Locked match-cut points never move. Runs last so later passes cannot push cuts off the grid. No-op without music. |

Add your own pass by subclassing `passes.Pass`, implementing `apply`, and
registering it in `passes.PASSES`.

### 6. Deliver (`render.py`, `report.py`)

`final.json` is the timeline; `final.edl` is a CMX3600 EDL you can import into
Resolve / Premiere / Avid (cuts and dissolves against a single reel `AX`);
`final.csv` is the same for spreadsheets. `final.mp4` is rendered by one
ffmpeg filter graph (trim → concat / xfade / acrossfade → fades); `--proxy`
renders a quick 640 px preview. `report.md` has the narrative, the cut list
and every decision each pass made.

## Music

`--music track.mp3` (config section `music`) adds a prerecorded track:

* **Analysis** (`audio.py`): librosa beat tracking on an ffmpeg-decoded mono
  signal gives tempo, beat times and downbeats (every `beats_per_bar` beats,
  phase chosen by onset strength); an RMS curve every `hop_seconds` gives the
  energy. Cached in `music.json`.
* **Narrative**: the prompt gets tempo, length and the energy curve sampled
  every 5 s with the rises and drops, so the model can put the climax on the
  swell. Beat durations then decide where in the track each beat lands.
* **Beat sync**: see the `beatsync` pass above. `--music-start` offsets the
  grid; `music.snap_window` caps how far a cut may move.
* **Mix** (`render.py`): the track is trimmed to the cut, faded out over
  `music.fade_out`, set to `music.gain_db` (`--music-gain` on the command line), ducked under the source audio with
  `sidechaincompress` (`music.duck`), mixed with `amix`, and normalised with
  `loudnorm` to `music.target_lufs`. `music.only=true` drops the source audio.
* The report gains a "Music" section with the track, tempo and snap stats.

A dissolve starts on the beat: the cut point the pass aligns is where the
incoming clip begins fading in (the render starts a dissolve `duration`
seconds before the junction). Tempo is assumed constant across the track.

## Events and checkpoints

`Pipeline(cfg, on_event=None, on_checkpoint=None, cancel=None)` lets a UI
drive a run. Every callback runs on the pipeline's thread.

* **Events** (`matchcut.pipeline.events`): `RunStarted`, `StageStarted`,
  `StageCached`, `StageFinished`, `Decision` (one per pass decision),
  `ModelCall` (one per Claude API attempt, with tokens), `Log` (the pipeline's log
  lines), `Checkpoint`, `RunFinished`, `RunFailed`. Each has `to_dict()`.
  Stage names: `ingest`, `describe`, `narrative`, `roughcut`, `pass:<name>`,
  `render`, `report`.
* **Checkpoints**: after `describe`, `narrative`, `roughcut` and every pass,
  `on_checkpoint(stage, summary)` is called and the run waits for its answer:
  `Continue()`, `Redo(note)` or `Abort()`. `Redo` at describe, narrative or
  roughcut deletes that stage's cached output and everything downstream
  (`Pipeline.invalidate_from(stage)`), appends the note to the stage's
  director's notes and re-runs it; `Redo` at a pass re-runs that pass on the
  same input and overwrites its file. Notes are
  rendered into the describe, narrative, rough-cut and polish prompts; the
  deterministic passes record the note in the timeline history and simply
  re-run. `Abort` raises `RunAborted` after emitting `RunFailed(error="aborted")`.
* **Cancel**: a `threading.Event`; the run stops at the next stage or pass
  boundary with `RunAborted`.
* Callbacks must not raise: an exception from `on_event` or `on_checkpoint`
  aborts the run as a `RunFailed`. `Log` events are produced through the
  `matchcut.pipeline` logger only while a listener is attached.

Without handlers (the CLI) every checkpoint continues and nothing changes.

## Editorial pins

An editor can pin a junction (a cut between two clips) from the TUI's Edit
tab so that later runs keep it. Pins live in `workspace/editorial.json`,
written atomically:

```json
{"pins": [{"out_shot": 0, "in_shot": 1, "out_time": 4.0, "in_time": 11.88, "origin": "locked", "note": "", "out_origin": "0@0.000-4.500", "in_origin": "1@11.500-14.000"}]}
```

A pin names a junction by its two **shots**, not by clip ids: passes renumber
clips, shots do not. `out_time` and `in_time` are source seconds. `origin` is
`locked` (keep the points the editor was looking at) or `reverted` (go back to
the points of the first cut). `out_origin` / `in_origin` name the two clip
instances the pin was taken on (see below; `""` in older files). A malformed entry is skipped and logged; a
missing or corrupt file means no pins.

How a pin takes effect:

- **Rough cut.** When the rough cut is assembled, every pin is added to the
  model's constraints as "keep shot A immediately followed by shot B", so a
  regenerated rough cut still contains the junction.
- **Before the passes.** `Pipeline.refine` calls `editorial.apply_pins` on the
  rough cut before the first pass. For each pin whose two shots are adjacent,
  both clips get the pinned points (snapped to frames, clamped to the shot and
  to `pacing.min_clip_seconds`), the facing ends are locked (`locked_out` /
  `locked_in`) and both clips are tagged `pinned`. The passes already leave
  locked ends alone, and `pacing` never drops a clip tagged `pinned`. A beat
  that holds a pinned clip is not reordered by the match-cut pass.
- **Shots that are not adjacent.** A pin on a junction a later pass would have
  made (a pacing drop, a continuity insert, a match-cut reorder) names two
  shots that are not neighbours in the rough cut. `apply_pins` first chooses
  the two clips (see **How a pin finds its clips** below), never the same clip
  twice (a pinned jump cut takes two pieces of one shot). If the
  pinned points fit those clips (the room check comes first, so a pin that
  fails it leaves the order alone), `apply_pins` moves one clip next to the
  other: first the incoming clip to just after the outgoing one, otherwise the
  outgoing clip to just before the incoming one. A move never splits or
  reorders the beats and never moves a clip, or the end of an anchor clip, that
  another pin already holds. Pins are placed in two phases, each in store
  order: those whose chosen clips are already adjacent first, then those that
  need a move, so a move never breaks a pin that is already in place. The move
  is recorded as `editor:pin_moved` (detail `moved shot 3 next to shot 0 to
  hold the editor's pin`) before the pin's `editor:pinned` decision; both name
  rough-cut clip ids, and the junction inspector attributes them to the
  junction by the clips' lineage, so its why line is the editor's.
- **Decisions.** Each pin leaves a decision with pass name `editor`:
  `editor:pinned` (detail `locked by the editor: out 4.00s, in 11.88s`; the
  data holds the pin plus the points actually placed) or `editor:pin_unplaced`.
  The unplaced detail is exactly one of `shot N is not in this cut`, `shot N
  has only one clip in this cut` (a pinned jump cut of a shot the cut keeps
  once), `shot N is ambiguous: more than one piece fits the pinned point`,
  `shot N is held by another pin`, `moving shot N would split or
  reorder the beats`, or `not enough room in these clips for the pinned
  points`. These show in the event stream, in `report.md`, in the TUI log, and
  in the junction inspector's badge.

- **How a pin finds its clips.** A shot can appear as several pieces, and a
  later stage's points (the Edit tab pins the inspected stage) may have moved
  past the rough-cut range of the piece they belong to, for instance when
  pacing extends a clip's out-point into a sibling piece's range. So a clip
  carries an `origin`: `Pipeline.refine` stamps each rough-cut clip with
  `clip_origin(c)` (`"<shot>@<in>-<out>"` of its rough-cut range), every pass
  copies it forward (continuity's merge keeps the earlier piece's), and the
  Edit tab writes the origins of the junction's two clips into the pin. Per
  side, a pin first takes the one clip of that shot with its remembered origin;
  without a unique match it takes the piece whose boundary is nearest (the
  outgoing side compares `out_time` with each `source_out`, the incoming side
  `in_time` with each `source_in`), but only if the runner-up is at least
  0.5 s farther. Otherwise the pin is left unplaced with the ambiguity reason
  and nothing moves: a pin is never placed on a guess. Selection uses the
  rough-cut ranges as they were before any pin was applied.

A pin holds the cut points, not the transition: a pinned junction on a beat
boundary still gets the beat's dissolve, and only a match cut (tagged
`match_out:*` / `match_in:*` by the match-cut pass) is forced to a hard cut.
Because the pin is applied to the rough cut before the first pass, a junction's
history credits the move to `editor (before matchcut)`, not to the first pass.

A pin written during a run is read when the passes next start, so it applies
on the next run. The TUI shows `pin pending: run again (R) to apply` until
then.

## Explaining the cuts as text

`junctions` lists every junction of the final cut with its evidence, so a cut
can be understood without opening the TUI or the JSON files:

```shell
python -m matchcut.pipeline junctions WORKSPACE            # one line per junction
python -m matchcut.pipeline junctions WORKSPACE --index 1  # one junction in full
```

Each line gives the junction number, the two clips and shots, the cut points
in source seconds, the transition, the combined score and badges
(`match:<type>`, `locked`, `pinned`, `beat`). Junctions are followed across stages by
lineage (same shot, overlapping source range), so the scores before and after
the passes describe the same cut even when clips were renumbered. A junction a pass
created is credited with that pass's decisions about its clips by the same
lineage: a continuity insert reads `continuity: moved c003 between c001 and
c002 to break a jump cut`, and a match-cut reorder's verdicts about the new
neighbours belong to the junctions they made. The scores
are rebuilt from `analysis.npz` with the deterministic scorer, the same one the
match-cut pass uses. Output from a mock run on the 20 s Big Buck Bunny clip
(`-t 12`):

```text
$ python -m matchcut.pipeline junctions ws_bbb
2 junctions in final (8 stages)
  1  c001->c002  s0->s1  out    4.00  in   11.88  dissolve     score 0.23  beat
  2  c002->c003  s1->s2  out   15.75  in   15.88  dissolve     score 0.36  beat

$ python -m matchcut.pipeline junctions ws_bbb --index 1
  1  c001->c002  s0->s1  out    4.00  in   11.88  dissolve     score 0.23  beat
why: transitions: beat b1 -> b2: dissolve 0.75s
history:
  roughcut: cut at out 4.00s / in 11.88s
  transitions: dissolve 0.75s
scores (roughcut -> final):
  structure    0.000 -> 0.000
  composition  0.595 -> 0.595
  luminance    0.370 -> 0.370
  color        0.199 -> 0.199
  frame        0.245 -> 0.245
  motion       0.200 -> 0.200
  combined     0.232 -> 0.232
decisions:
  transitions:dissolve  beat b1 -> b2: dissolve 0.75s
```

## Working iteratively

Every stage is cached in the workspace, so you can change something and re-run
only what follows:

```shell
python -m matchcut.pipeline analyze footage.mp4            # shots + descriptions
python -m matchcut.pipeline narrative footage.mp4 -b "..."  # re-think the story (uses cached descriptions)
python -m matchcut.pipeline roughcut footage.mp4 -t 45
python -m matchcut.pipeline refine footage.mp4 --passes matchcut,pacing,transitions
python -m matchcut.pipeline refine footage.mp4 --from my_edit.json   # refine a hand-made timeline
python -m matchcut.pipeline render footage.mp4 --timeline footage_edit/passes/01_matchcut.json -o preview.mp4
```

`--force` ignores the caches; `--set section.key=value` overrides any config
value (`--set matchcut.candidate_threshold=0.6 --set pacing.max_clip_seconds=5`).
The full config is written to `workspace/config.json`.

## Model usage and cost

Default model: `claude-opus-5-5` with structured outputs and server-side
refusal fallbacks (`llm.refusal_fallbacks`). Images dominate the cost: for N
shots expect roughly N/24 describe requests (three sheets each), one narrative
and one rough-cut request, N_candidates/6 verification requests, and one polish
request. Every call is logged to `workspace/llm_calls.jsonl` with usage.
`--model claude-sonnet-5-5` and `--effort low` are the cheap knobs.

## Limitations

* Shot detection handles hard cuts and ignores fades to/from black; long
  dissolves between two bright shots are not split.
* The match-cut scorer is intentionally cheap. It finds compositional and
  motion rhymes; semantic matches (e.g. a bone cut to a spaceship) rely on the
  model's verdict on the side-by-side sheets, so keep `matchcut.verify_with_llm`
  on for real work.
* Audio: a music track can be mixed and cut to; dialogue is not transcribed, so there are no J/L cuts and a cut may land mid-sentence. Sound effects and generated music are future work (see docs/superpowers/specs/2026-10-15-audio-music.md).
