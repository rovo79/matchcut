# Editing a cut through the JSON CLI (`matchcut.edit`)

`matchcut.edit` lets an agent, or a person at a shell, edit a matchcut cut in a small, frame-native, transactional and reversible way:
every change is proposed, checked against every rule, previewed if you like, and only then applied as a new immutable revision. A
finished revision is **published** as the workspace's `edit` stage, which the junction tools, the TUI and the editor exports
(`matchcut.interchange`) all treat like any other stage.

Design: [superpowers/specs/2026-11-01-m3a-edit-core.md](superpowers/specs/2026-11-01-m3a-edit-core.md). The human gate is in
[M3A-ACCEPTANCE.md](M3A-ACCEPTANCE.md) and is **PENDING**.

- [The model](#the-model)
- [The CLI](#the-cli): [commands](#commands), [operations](#operations), [guards](#guards), [exit codes](#exit-codes-and-errors)
- [The three decisions](#the-three-decisions-as-they-behave-now)
- [Pins and `pin_transition`](#pins-and-pin_transition)
- [Rendering and naming](#rendering-and-naming)
- [Staleness](#staleness)
- [Out of scope, and known limitations](#out-of-scope)

## The model

**Stages.** A workspace holds the pipeline's stages and, after them, two that the engine only reads:

| stage | file | written by |
|---|---|---|
| `roughcut`, the passes (`passes/NN_<name>.json`), `final` | the canonical stages | the pipeline |
| `edit` | `edit.json` + `edit_publish.json` | `python -m matchcut.edit WS publish` |
| `human` | `human.json` + `human_import.json` | `python -m matchcut.pipeline.cli import WS FILE` (an editor's re-export) |

The stage order is: the canonical stages, then `edit` when `edit.json` is there and fresh, then `human` when it is fresh. A stale stage is
left out of the order (and so of `junctions`, the TUI and the exports), never deleted.

**Sessions.** `start` opens a session on a **base stage** (default: the latest canonical stage, `final`, the only base that can publish;
`human` or an earlier canonical stage by name; `edit` is never a base, see below). `start`, `log` and `inspect` report `can_publish` and,
when it is false, `publish_blocker` (why). One session at a time lives in `WS/edits/`:

| path | what |
|---|---|
| `edits/session.json` | the session id (a UUID made at `start`), the base stage, its file and sha256, the head revision, the next revision number, every clip id the session has issued |
| `edits/revisions/rNNN.json` | immutable: a whole timeline plus the operations, results and changes that made it |
| `edits/proposals/<id>.json` | accepted proposals |
| `edits/renders/` | renders, previews and junction frames (named by a hash of what they show) |
| `edits/published/<rev>-<sha12>.json` | an immutable copy of every published revision |
| `edits/published/records/` | a copy of every `edit_publish.json` written |
| `edits/published/log.jsonl` | append-only: one line per publish (time, session, revision, sha256, replaced / superseded flags) |
| `edits/replaced/<sha12>/` | an `edit.json` (and its record) that another session's publish moved aside; `record-<sha12>/`: a record left without its `edit.json` |
| `edits/.lock` | every writing command holds it exclusively, the read-only ones shared |

`start` with `restart` (or on a stale session) moves the session to `WS/edits.replaced-<sha12>/` (`edits.stale-<sha12>/`); the lock and the
edit stage's archive (`published/`, `replaced/`) stay in `edits/`. Nothing is ever deleted.

**Revisions.** `r000` is the base stage put on whole frames (source points and transitions on the frame grid M2a's export and the renderer
use; the workspace's files are not rewritten; `start` lists what moved in `normalised`). Every `apply` writes the next revision (`r001`,
`r002`, ...) whose parent is the head. `undo`, `redo` (the latest child) and `checkout` only move the head; nothing is deleted, so a new
edit after an undo starts a branch.

**Proposals.** `propose` runs a list of operations, **all or nothing**, on a copy of the head and checks every rule on the result. An
accepted proposal is stored under a deterministic id (12 hex characters: the sha256 of the head, its timeline, the operations and guards,
the session's issued ids and the workspace context) and nothing changes until `apply`. `apply` re-runs the operations against the head and
refuses if the head moved, the context changed (config, shots, pins, music, source rate) or the result differs from the stored one.

**Frames.** Every operation and report is in whole frames at the source's exact rate (`30000/1001`, never `29.97`). Clips are named by
id (stable for the session: new clips are `e001`, `e002`, ...; nothing is renumbered) or by their key (`clip_origin`, e.g. `1@4.800-6.800`).
Junctions are named by their 1-based number or their id (`s1-s3`: out shot 1, in shot 3; `s1-s3#2` for a repeated pair).

## The CLI

```shell
python -m matchcut.edit WS COMMAND ['{"json": "arguments"}']
python -m matchcut.edit WS COMMAND @args.json      # the arguments from a file
echo '{"revision": "r001"}' | python -m matchcut.edit WS inspect @-   # ... or from stdin
python -m matchcut.edit --help                     # every command, its description and its JSON Schema
python -m matchcut.edit WS COMMAND --help          # one command
```

Every run prints **exactly one JSON object** on stdout; anything a command itself prints (ffmpeg) goes to stderr. One table
(`matchcut/edit/tools.py`, `TOOLS`) defines every command's name, description, JSON Schema and handler; a later MCP transport serves the
same table unchanged.

### Commands

| command | arguments | what it does |
|---|---|---|
| `start` | `{stage?, restart?}` | start a session (default base: the latest canonical stage); a stale session is moved aside, a fresh one only with `restart: true`; says `can_publish` |
| `inspect` | `{revision?}` | the cut of a revision (default: the head) in frames: clips, junctions and their protections, shots and their use, story beats, music beats, pins, staleness |
| `log` | `{}` | the revisions (id, parent, operations, summary), the head, staleness |
| `propose` | `{ops, guards?}` | run operations on the head and check every rule; accepted: the proposal id, its changes by clip id and its timing |
| `apply` | `{proposal}` | apply an accepted proposal as the next revision (re-validated against the head) |
| `undo` / `redo` | `{}` | move the head to its parent / its latest child |
| `checkout` | `{revision}` | move the head to any revision |
| `compare` | `{a?, b?}` | changes and timing from revision `a` to `b` (either omitted: the head) |
| `preview` | `{proposal, junction?, quality?}` | render an accepted proposal before applying it: the whole cut, or one junction's review window |
| `render` | `{revision?, junction?, quality?}` | render a revision (default: the head), whole or one junction's window; `quality` is `proxy` (default) or `full` |
| `frames` | `{junction, revision?}` | the outgoing clip's last frame and the incoming clip's first frame as JPEG files, with their source frame numbers |
| `pin_transition` | `{pin? \| junction?, hold, kind?, frames?}` | protect (`hold: true`) or release the transition at an editor pin's junction |
| `publish` | `{revision?, replace?, supersede_human?}` | publish a revision (default: the head; never `r000`) as the `edit` stage |

A session's whole life, as the CLI prints it (a 25 fps test workspace):

```shell
$ python -m matchcut.edit WS start
{"session": "dc8f9a5e-...", "base_stage": "final", "base_sha256": "0fe5cf...", "head": "r000", "normalised": [], "moved_aside": null,
 "can_publish": true, "publish_blocker": null}

$ python -m matchcut.edit WS propose '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5},
      {"op": "set_transition", "junction": 3, "kind": "dissolve", "frames": 6}], "guards": {"max_duration_change_frames": 12}}'
{"proposal": "3186353224ae", "head": "r000", "changes": ["c002: out -5f", "c003|c004: cut -> dissolve 6f"],
 "timing": {"duration_frames": [262, 251], "duration_change": -11, "moved": [{"id": "s1-s3", "clips": ["c002", "c003"],
 "before": 92, "after": 87, "delta": -5}, ...], "beat_offsets": null, "left_beat": [], "reached_beat": []},
 "problems": [], "details": ["trim c002 out -5f", "junction 3 (c003|c004): cut -> dissolve 6f"], "touched": ["c002", "c004"], "issued": []}

$ python -m matchcut.edit WS preview '{"proposal": "3186353224ae", "junction": 3}'
$ python -m matchcut.edit WS apply '{"proposal": "3186353224ae"}'
{"revision": "r001", "parent": "r000", "head": "r001", "proposal": "3186353224ae", "changes": [...], "timing": {...}}

$ python -m matchcut.edit WS publish
{"schema": "matchcut.edit_publish/1", "session": "dc8f9a5e-...", "revision": "r001", "timeline_sha256": "a477ae...",
 "edit_file": "edit.json", "edit_sha256": "9f5b4c...", "base_stage": "final", "base_file": "final.json", "base_sha256": "0fe5cf...",
 "latest_stage": "final", "latest_stage_file": "final.json", "latest_stage_sha256": "0fe5cf...", "published_at": "2026-11-01T16:24:20+00:00",
 "decisions": 2, "published": "edits/published/r001-9f5b4c501634.json", "replaced": null, "previous": null, "human_left_out": null,
 "pins": []}

$ python -m matchcut.pipeline.cli junctions WS
4 junctions in edit (4 stages)
  1  c001->c002  s0->s1  out    2.40  in    4.80  cut          score   -
  2  c002->c003  s1->s3  out    6.60  in   12.80  dissolve     score   -   beat
  ...
$ python -m matchcut.pipeline.cli export WS --stage edit     # edit.otio, the edit project in matchcut.fcpxml, edit.edl
```

`inspect` returns `session` (id, base stage, head, the revision shown, staleness), `rate`/`fps`, `duration_frames`, `clips` (id, key,
shot, beat, source and record frames, incoming transition, locks, purpose), `junctions` (number, id, clips, record frame, transition, story
beat change, match cut, the pin that holds it, its protection, the beat offset with music), `shots` with their use and `unused_shots`, the
`story_beats`, the `music` beats, the stored `pins` (held or not, protected transition) and the `protections` that apply.

### Operations

`ops` is a list; each item is `{"op": ..., ...}` with only that operation's arguments (an unknown or misspelt argument is a usage error).
`frames` counts are whole frames, signed where it makes sense.

| op | arguments | what it does |
|---|---|---|
| `trim` | `clip`, `edge` (`in`/`out`), `frames` | move one edge of a clip (positive = later in the source); later clips ripple |
| `roll` | `junction`, `frames` | move a cut point: the outgoing out-point and the incoming in-point together; nothing ripples |
| `slip` | `clip`, `frames` | other frames of the same shot for the same length |
| `replace` | `clip`, `shot`, `in_frame?`, `frames?` | footage of another shot in a clip's place (default: the same length, from the middle of the shot); keeps the incoming transition and story beat |
| `insert` | `shot`, `in_frame`, `out_frame`, `before` \| `after` | a new clip, with a cut |
| `remove` | `clip` | take a clip out; later clips ripple |
| `move` | `clip`, `before` \| `after` | move a clip; it keeps its incoming transition (a cut when it becomes the first clip) |
| `set_transition` | `junction`, `kind` (`cut`, `dissolve`, `dip_to_black`), `frames` | change a junction's transition |
| `snap_to_beat` | `junction`, `mode?` (`roll` default, `ripple`), `window_s?` | put a junction on the nearest music beat in the window (a downbeat at a story-beat change, as the beat-sync pass prefers) |

Every proposal is checked as a whole, whichever operation caused a change: shot bounds and the minimum clip length; transitions (below);
pins and match cuts (below); the guards; and every clip and junction the operations did not touch stays identical in frames. A violation
the base already had (made by the pipeline) is reported, not refused; a proposal may not add one or make one worse.

### Guards

A proposal may carry `guards`; it is refused, naming the guard and the junction, if it would break one:

- `max_duration_change_frames`: the most the running time may change, in frames either way;
- `keep_record_frames: [junctions]`: these junctions keep their record frame;
- `keep_on_beat: [junctions]`: these junctions stay on their music beat.

Guards live in the proposal only: there are no persistent timing locks (see the limitations).

### Exit codes and errors

| code | when | stdout |
|---|---|---|
| 0 | success | the command's result |
| 1 | the command ran and refused or failed: a stale session, an unknown revision, a proposal the rules refuse (with `problems`, `timing`, `changes`, `details`), an operation that cannot apply (with its `index`), a failed render, an unexpected error | `{"error": "...", "kind": "refused" \| "error" \| "internal", "command": ...}` |
| 2 | usage: the command never ran. No or an unknown command, an extra argument, arguments that are not one JSON object (bad JSON, an unreadable `@file`), a workspace that is not a directory, and **arguments that do not match the command's JSON Schema** (a wrong type, a fractional frame count, a malformed revision or proposal id, an empty `ops`); a schema failure lists one line per argument in `errors` | `{"error": "...", "kind": "usage", "usage": ..., "errors"?, "arguments"?}` |

No traceback is ever printed for bad input; `MATCHCUT_DEBUG=1` sends an unexpected error's traceback to stderr. SIGTERM or Ctrl-C during a
command still prints one JSON object (`{"error": "terminated", "kind": "error"}`) and removes temporary render files.

## The three decisions as they behave now

### 1. Published agent edits are the `edit` stage

`publish` writes, atomically, under the session lock and the workspace's stage lock (`WS/.stage.lock`, which an import holds too):

- `WS/edit.json`: the revision's timeline, stage `edit`. Its history is the latest canonical stage's history plus one `agent` decision per
  change (`trimmed`, `slipped`, `transition`, `removed`, `added`, `moved`), each naming its proposal, so junction attribution and pin
  badges keep working;
- `WS/edit_publish.json` (`matchcut.edit_publish/1`): the session id, the revision and its timeline sha256, the sha256 of `edit.json`, the
  base stage and its sha256 when the session started, the latest canonical stage and its sha256, the time, the number of agent decisions,
  and the path of the published copy;
- `WS/edits/published/<rev>-<sha12>.json`: an immutable copy of the same bytes;
- a copy of the record in `edits/published/records/`, and one line in `edits/published/log.jsonl`.

It never touches `final.json`, a pass file, `human.json` or `human_import.json`. The result is the record plus `replaced`, `previous`,
`human_left_out` and `pins` (every stored pin and where it stands in the published cut: `held`, or `not_held` with the reason).

What can be published, and when publishing is refused:

- **`r000` is never published** ("nothing to publish"): it is the base stage on whole frames, with no agent edit, even when the
  normalisation moved a point.
- **Only a session on the latest canonical stage** (`final` once the pipeline has finished) can publish. The edit stage continues that
  stage's history, while a session's revisions carry their base's history: a session on `roughcut`, a pass or `human` is refused, with the
  fix (`start {"stage": "final", "restart": true}` and redo the edits). Splicing its edits onto final's history would leave the passes' or
  the editor's work unattributed, and a cut made from `human` listed before `human` would undo the editor's changes in every junction's
  history.
- A stale session is refused, like every command that changes something.
- **Pins are checked again.** The revision must hold every editor pin the base holds under the pins stored **now** (clips, adjacency, cut
  points, and a protected transition: the one the pin holds). A pin stored after the revision was made, which it does not hold, is refused:
  `pin s0->s1 was added or changed since r001 was made, and r001 breaks the editor pin s0->s1: c002 removed: propose the edits again (or
  release the pin)`. A pin stored after the pipeline's last run (the base does not hold it either) is reported `not_held` ("not held by
  this edit"), never "pending": a pipeline run would apply it, and make the edit stage stale.
- **A fresh human stage is not left out silently.** While a fresh `human.json` exists that the publish would make stale (one imported
  against a canonical stage, whose history does not continue an edit stage; or one imported against the edit stage, when `edit.json`
  changes), the publish is refused, naming the human stage. `supersede_human: true` publishes anyway; the result's `human_left_out` says why
  the human stage is now left out. `human.json` and `human_import.json` are never changed: re-import the editor's file to bring it back.
- **Never a silent overwrite.** Publishing again from the same session replaces `edit.json` (the earlier copy stays in `published/`;
  `previous` names the revision it replaced). An `edit.json` another session published, or one without a readable record that describes it
  (no record, a record of another file, a hand edit), is refused unless `replace: true`, which first moves the old pair to
  `edits/replaced/<sha12 of the old edit.json>/` (`-2`, `-3` ... when that exists); the refusal asks whether a publish was interrupted. A
  publish cut short after `edit.json` was written but before its record is recognised (`edit.json` is byte for byte this session's publish
  of one of its revisions) and the session's next publish goes ahead. A record left without its `edit.json` is archived in
  `edits/replaced/record-<sha12>/` before a new one is written.

**The edit stage is never a session base**, by default or by name: publishing replaces `edit.json`, so a session on it would be stale the
moment it published. Start from `final` (or `human`, though that session cannot publish).

The edit stage through M2a (`matchcut.interchange`, see [INTEROP.md](INTEROP.md#the-edit-stage)):

- `python -m matchcut.pipeline.cli export WS --stage edit` writes `edit.otio`, the `edit` project of `matchcut.fcpxml` and `edit.edl`,
  each fingerprinted with the sha256 of `edit.json`, keeps the snapshot `exports/.stages/edit-<sha12>.json`, and covers `edit` in the
  fidelity report. It is never exported by default (`roughcut` and `final` are), like `human`.
- `import` of an editor's re-export compares it with **that exact snapshot**, even after `edit.json` was published again. A fingerprint
  that matches nothing is refused; a file without one, or one whose fingerprint names another stage than the file does, is refused unless
  `--stage` and `--base SNAPSHOT` name the base. **Once any edit stage or edit snapshot exists, a fingerprint-less import needs `--base`.**
  An EDL's stage and fingerprint are its `* MATCHCUT` comment lines (renaming its title changes nothing); editors usually drop those
  comments, so an EDL re-export will usually need `--stage edit --base edit-<12 hex>`. A workspace that never had an edit stage imports
  exactly as before. The `human` stage it writes records `stage: "edit"`, the snapshot, and the sha256 of
  `edit.json` and of the latest canonical stage; it is stale once either changes.

### 2. Pinned junctions may change their transition

At an editor pin the pinned clip instances, their adjacency and both cut points stay fixed, checked on the whole proposal; the transition
(kind and length) may change, **unless the pin protects it** (`hold_transition`, see below). A match cut stays a hard cut with its
adjacency and cut points. Every transition in a result must be a cut, dissolve or dip to black of whole frames, at most
`transitions.max_fraction_of_clip` of the shorter of its two clips, and laid out by M2a (each clip longer than its transitions); every
clip's range must stay inside its shot (a transition overlaps material inside both clips' ranges).

### 3. A transition overlap keeps the current timing

A new or longer dissolve shortens the cut and moves every later record position earlier; nothing is compensated by moving unrelated source
points. Every proposal reports its timing: each junction whose record frame changes (`moved`: id, before, after, delta), the running time
before and after (`duration_frames`, `duration_change`), and with music each junction's beat offset before and after and which junctions
left or reached a beat. Use the [guards](#guards) to refuse a proposal that moves what must stay.

## Pins and `pin_transition`

Editor pins come from the TUI's Edit view (`l` locks the inspected cut, `r` reverts it to the first cut's points) and live in
`editorial.json`. An edit session reads them as part of its context: a pinned junction keeps its clips, their adjacency and both cut points.

`pin_transition` protects or releases the transition at a pin's junction:

```shell
python -m matchcut.edit WS pin_transition '{"pin": "s0->s1", "hold": true}'          # hold the head's transition (a pin the head holds)
python -m matchcut.edit WS pin_transition '{"junction": 1, "hold": false}'           # release it (junction 1 holds a pin)
python -m matchcut.edit WS pin_transition '{"pin": "s3->s6", "hold": true, "kind": "dissolve", "frames": 12}'  # a pin the head does not hold yet
```

It sets or clears the pin's optional `hold_transition` and `held_transition` (`{"kind", "frames"}`) in `editorial.json`, changing nothing
else in the file; older pins read as unprotected, and the TUI's `l`/`r` keep making unprotected pins. Because the pins are part of the
context, every proposal made before the change must be proposed again (the result lists them). **The pipeline ignores the protection**
(see the limitations).

On the published `edit` stage a pinned junction keeps its `pinned` badge in `junctions` and the TUI (a publish refuses a revision that
releases a pin its base holds), and `junctions` ends with a line per stored pin: `held by this edit`, or `not held by this edit (...)` for a
pin stored after the pipeline's last run.

## Rendering and naming

`render`, `preview` and `frames` write under `WS/edits/renders/`:

- a whole cut through the pipeline's renderer: `cut-<key>.mp4`;
- one junction's review window through M2b's renderer: `window-<key>.mp4` (from `review.pre_seconds` before the cut to
  `review.post_seconds` after its transition, laid out in whole frames; the result gives its `record_frames` and `cut_offset_frames`);
- a junction's outgoing and incoming frames, for vision models: `frame-<key>.jpg` (at most 1280 px wide).

`<key>` is the first 16 hex characters of the sha256 of everything the file depends on (what the timeline renders, the window, the source
file and its identity, the render and review settings, the quality, the music and its settings): a file with the same name is the same
render and is reused (`"cached": true`), and a preview of a proposal is the very file the render of its revision would be. Files are written
to `renders/.tmp/` under an exclusive advisory lock and moved into place; a file left by a killed render is swept later, never one a
running render holds. `proxy` renders at `render.proxy_width`; `full` at the source size. Renders are never deleted automatically.

## Staleness

| what | stale when | then |
|---|---|---|
| the session | its base stage file changed or is gone, that stage is no longer listed, or the source's frame rate changed | every command that changes it refuses; read-only ones say `"stale": true`; `start` moves it to `edits.stale-<sha12>/` |
| `edit` | `edit_publish.json` is missing, unreadable or describes another `edit.json`; the base stage file or the latest canonical stage file changed or is gone; another stage is the latest canonical one | left out of the stage order (`junctions` and the TUI say why), never deleted; export refuses it |
| `human` imported against `edit` | `edit.json` or the latest canonical stage changed, or the edit stage is stale | left out, never deleted |
| `human` imported against a canonical stage | that stage or the latest canonical stage changed, or **an `edit.json` was published after the import** (the human stage is listed after the edit stage and its history would not continue it; a publish does this only with `supersede_human`) | left out, never deleted |

A pipeline re-run rewrites `final.json`, so it makes the session, the edit stage and a human stage stale together. Start a new session on
the new `final` and redo the edits; matchcut does not carry an agent's edits across a pipeline run.

## Out of scope

- An MCP transport (M3b; it will serve the same tool table), any GUI or TUI editing, and editor (NLE) automation.
- Carrying an agent's edits across a pipeline re-run.
- A `preserve_downstream_timing` operation, story-beat fitting and music placement operations.

## Known limitations

- **The pipeline ignores `hold_transition`.** A protected transition is honoured by edit sessions only; a pipeline run may still change
  it.
- **No persistent timing locks.** Guards are per proposal; nothing in the workspace keeps a junction's record frame or beat for later
  proposals.
- **A context change requires a re-propose.** A change to the config, shots, pins (including `pin_transition`), music or the source's rate
  makes every stored proposal unusable: `apply` and `preview` refuse it until it is proposed again.
- One session per workspace; a second agent must wait for the lock or work in a copy.
- **Only a session on the latest canonical stage can publish** (the default base). A session on `human` or an earlier stage can edit,
  preview and render but never publish (`can_publish: false`); its edits must be redone on `final`.
- **Publishing over a fresh human stage is refused** unless `supersede_human: true`, which leaves the human stage out as stale (never
  deleted): re-import the editor's file afterwards.
- **Editors drop EDL comments.** An EDL's stage and fingerprint are its `* MATCHCUT` comment lines; once a workspace has an edit stage, an
  EDL without them needs `--stage` and `--base` to import.
- A pin stored after the pipeline's last run is not held by the edit stage (`not_held`); only a pipeline run applies it, and that makes the
  edit stage stale.
- Another session meeting an `edit.json` its record does not describe (a hand edit, or another session's interrupted publish) needs
  `replace` (which moves it aside, so nothing is lost).
