# M3a: the edit core (`matchcut/edit/`) — design

**Status:** specification for approval (2026-11-01). Nothing here is implemented.

- **Evidence:** the feasibility report (`docs/EDITING-API-FEASIBILITY.md`) and the prototype (`prototypes/editing_api/`). The prototype's 91-check
  proof, re-run with the renderer fix merged, gives 90 Passed, 1 known gap (an edited cut cannot be exported; this milestone closes it),
  and no core-defect failure.
- **The prototype is evidence, not code to copy:** every module below is written anew against this spec, with its own tests.
- **Human gates M1, M2b, M2a and M2c: PENDING.**

## Prerequisites

1. **The renderer audio repair** (`claude/renderer-audio-fix-bkqqay`) merged. M3a renders revisions, previews and junction windows through
   it.
2. **The transition frame normalisation** merged, or at least agreed.
   - It stores canonical transition lengths in whole frames and makes the renderer place transitions on whole frames.
   - M3a also puts transitions on whole frames when a session starts, so an older workspace edits correctly without being rewritten.

## Goal

Give an agent (and a person at a shell) a small, frame-native, transactional, inspectable and reversible way to edit a matchcut cut,
on the canonical timeline, with matchcut's own renderer.

- **First transport:** the JSON CLI.
- **Not in M3a:** MCP, any GUI, any editor (NLE) automation.

## Decisions (approved 2026-11-01) and what they mean here

### 1. Published agent edits get their own stage, `edit`

- **The three stages:**
  - `final` stays the canonical pipeline output;
  - `edit` holds agent-directed revisions;
  - `human` is reserved for editor (NLE) imports.
- **Publishing writes three things:**
  - `WS/edit.json`, the `edit` stage timeline (stage `edit`);
  - `WS/edit_publish.json`, the provenance record;
  - an immutable copy, `WS/edits/published/<revision>-<sha12>.json`.
- **The record (`matchcut.edit_publish/1`) holds:**
  - the session id (a UUID made at `start`);
  - the revision id and its timeline sha256;
  - the base stage and its sha256 when the session started;
  - the latest canonical stage and its sha256;
  - the time;
  - the decisions count.
- **History:** the `edit` timeline's history is the latest canonical stage's history plus one `agent` decision per change. Junction
  attribution and pin badges therefore keep working, as M2a's builder does for `human`.
- **Never silently overwritten:**
  - Publishing again from the **same session** replaces `edit.json` with the newer revision. The earlier one stays in `edits/published/`.
  - Publishing from **another session**, or over an `edit.json` without a readable record, is refused unless `replace` is given. With it,
    the old pair is moved to `edits/replaced/<sha12>/` first.
  - Publishing never touches `final`, any pass file, `human.json` or `human_import.json`.
- **Stale:**
  - `edit` is stale when its base stage file, or the latest canonical stage file, has changed since publishing (`junctions.stale_edit`, like
    `stale_human`).
  - A stale `edit` is left out of `stage_timeline_paths`, never deleted.
  - The stage order becomes: canonical stages, then `edit` when fresh, then `human` when fresh.
- **Exportable through M2a:**
  - `export_workspace` accepts `edit`. It writes `edit.otio`, the `edit` project in `matchcut.fcpxml` and `edit.edl`.
  - Each file carries a stage fingerprint, the sha256 of `edit.json`. A snapshot is saved in `exports/.stages/edit-<sha12>.json`, as for
    canonical stages.
  - The fidelity report covers `edit` like any other stage.
- **Importing an editor's re-export of an edit:**
  - **Fingerprint:** the file names stage `edit` and carries a fingerprint, so `import_timeline` compares it with **that exact exported
    snapshot**. This works even when `edit.json` has been published again since.
  - **Unresolvable fingerprint:** if the fingerprint matches no snapshot, the import is refused. It is never guessed.
  - **No fingerprint, or ambiguous:** a file without a fingerprint (an EDL whose title was changed; *implementation note: an EDL's stage and fingerprint are its
    `* MATCHCUT` comment lines, so the example is an EDL whose comments were dropped, as most editors do; a renamed title alone still
    imports*), or whose base is ambiguous (names `edit`
    but a `final` snapshot matches, or the reverse), is refused unless `--stage` and `--base SNAPSHOT` name the base explicitly.
  - **Result:** the `human` stage made from such an import records `stage: "edit"`, the snapshot used, and the sha256 of `edit.json` and of
    the latest canonical stage. It is stale if either changes.

### 2. Pinned junctions may change their transition

- **What stays fixed at a pinned junction**, checked on the whole proposal:
  - the pinned clip instances (`clip_origin`);
  - their adjacency;
  - both cut points (`pin_change` is empty).
- **What may change:** the transition (kind and length), **unless the transition is explicitly protected**.
- **Protection:**
  - A pin may carry `hold_transition: true`, plus the transition it holds (`{"kind", "frames"}`). It is stored in `editorial.json` as
    optional fields; older pins read as unprotected.
  - The edit CLI sets and clears it (`pin_transition`). The TUI's `l` and `r` keys keep making unprotected pins.
  - The pipeline engine ignores the field in M3a, and the docs say so.
- **Match cuts:** a match cut stays a hard cut, with adjacency and cut points, always.
- **Every transition** in the resulting proposal must satisfy:
  - kind is cut, dissolve or dip to black;
  - whole frames;
  - at most `transitions.max_fraction_of_clip` of the shorter of its two clips;
  - M2a's `layout` accepts it (the clip is longer than its transitions).
- **Source boundaries:** in matchcut's model a transition overlaps material inside both clips' own source ranges, so each clip's range must
  stay inside its shot. This is checked for every clip the proposal produces.
- **Only new violations count:** a violation the base already had (made by the pipeline) is reported, not refused. A proposal may not add
  one, or make an existing one worse.

### 3. Transition overlap keeps the current timing

- **The semantics stay:** a new or longer dissolve shortens the cut and moves every later record position earlier. Nothing is compensated
  automatically by moving unrelated source points.
- **Every proposal reports its timing:**
  - each junction whose record frame changes (id, frame before and after, delta);
  - the running time in frames before and after;
  - with music, each junction's beat offset in frames before and after, and which junctions left or reached a beat.
- **Protected timing constraints:** a proposal may carry `guards`, and is refused if it would break one:
  - `max_duration_change_frames` (absolute);
  - `keep_record_frames: [junction ids]`;
  - `keep_on_beat: [junction ids]`.
  - Constraints stored in the workspace (persistent timing locks) are future work. The refusal names the guard and the junction.
- **Future work, not M3a:** a `preserve_downstream_timing` operation that absorbs an overlap explicitly.

## Model

- **Frames:** every operation and report is in whole frames at the source's exact rate (M2a's `SourceInfo.rate`).
- **Addressing clips:** by id, stable for a session (new clips `e<n>`, nothing renumbered), or by `clip_origin`.
- **Addressing junctions:** by 1-based number or by junction id (`s1-s3`, `s1-s3#2`).
- **Session store** `WS/edits/`:
  - `session.json`: session id, base stage and sha256, head, next revision number;
  - `revisions/rNNN.json`: immutable, a whole timeline plus the operations, results and changes that made it;
  - `proposals/<id>.json`;
  - `renders/`;
  - `published/`;
  - `.lock`.
- **Starting a session:**
  - It reads the base stage. Source points and transitions are put on whole frames **in r000 only**; the workspace's files are not
    rewritten.
  - A session whose base stage changed is stale. Every command refuses to change it, and `start` moves it to `edits.stale-<sha12>/`.
- **Operations:**
  - `trim` (one edge, ripple);
  - `roll` (a cut point, no ripple);
  - `slip`;
  - `replace` (another shot, same length by default, keeps the incoming transition);
  - `insert`, `remove`, `move`;
  - `set_transition`;
  - `snap_to_beat` (roll by default, ripple on request; it honours the beat-sync pass's downbeat preference at story-beat changes).
- **Proposals:**
  - **All or nothing.** A proposal runs its operations on a copy and checks every rule on the result. Rules are proposal-wide, whichever
    operation caused the change:
    - shot bounds and minimum length;
    - transitions (decision 2);
    - pins and match cuts (decision 2);
    - guards (decision 3);
    - untouched clips and junctions identical in frames.
  - **Ids:** proposal ids are deterministic: sha256 of the head revision, its timeline and the canonical JSON of the operations and guards.
  - **Output:** an accepted proposal returns its changes by clip id and its timing report.
- **Apply:**
  - It **re-runs** the proposal's operations against the head, and refuses if the head moved or the result differs from the stored one.
  - It then writes the next immutable revision under the session lock.
- **Moving the head:** undo, redo (latest branch) and checkout move the head only.
- **Compare:** compares any two revisions by clip id, with timing.
- **Preview and render:**
  - `preview` renders an accepted proposal before it is applied;
  - `render` renders a revision (proxy by default) or one junction's M2b review window;
  - `frames` returns a junction's outgoing and incoming frames as JPEG files, for vision models.
  - Render files are named by a hash of everything rendered.

## The JSON CLI

- **Usage:** `python -m matchcut.edit WS COMMAND ['{"json": "arguments"}']`, with the arguments also accepted from a file (`@args.json`).
- **Commands:**
  - session: `start`, `inspect`, `log`;
  - edits: `propose`, `apply`, `undo`, `redo`, `checkout`, `compare`;
  - output: `preview`, `render`, `frames`, `publish`;
  - pins: `pin_transition`.
- **Output:**
  - one JSON object on stdout, with an exit code of 0 for success, 1 for a refusal or error (`{"error": ...}`), 2 for usage;
  - no tracebacks for bad input.
- **One tool table** defines each command (name, description, JSON Schema, handler). A later MCP transport (M3b) adds nothing to it.
- **Validation:**
  - arguments are checked against the schema;
  - ids are matched against `^r\d{3,}$` and `^[0-9a-f]{12}$`;
  - frame counts must be integers.

## Package

| Module | What it holds |
|---|---|
| `matchcut/edit/ops.py` | The nine operations. Pure functions of a timeline and an operation, frame-native, raising `Refused`. |
| `matchcut/edit/rules.py` | The proposal-wide rules, the timing report and the guards. |
| `matchcut/edit/session.py` | The store, proposals, apply, undo/redo/checkout, compare, staleness and the lock. |
| `matchcut/edit/view.py` | `inspect` (clips, junctions, shots and their use, story and music beats, pins and protections), `frames`, `preview`/`render` through `pipeline.render` and `review.render_window`. |
| `matchcut/edit/publish.py` | The `edit` stage and its record. |
| `matchcut/edit/tools.py`, `__main__.py` | The tool table and the CLI. |

**Changes outside the package**, all small:
- `junctions.py`: the `edit` stage in the stage order, and `stale_edit`.
- `interchange/workspace.py`:
  - export of `edit`;
  - import against an `edit` snapshot;
  - the ambiguity refusals.
- `editorial.py`: the optional `hold_transition` fields.
- **Not changed:** the engine never imports `matchcut.edit`.

## Tasks

Each task is reviewed on its own, then the whole branch gets a review and an independent test report.

**1. Operations and rules.**
- `ops.py` and `rules.py`, including decisions 2 and 3 (protected transitions, pins, match cuts, transition limits, timing report and
  guards).
- Unit tests, including the prototype proof's scenarios: pins, match cuts, the first clip, transition limits, a 29.97 fps source.
- Property tests over random edit sequences on demo cuts:
  - untouched clips and junctions are identical in frames;
  - the running time changes exactly by the trims and overlaps;
  - every pin and match-cut invariant holds;
  - no proposal adds a violation.

**2. Session store.**
- `session.py`.
- Tests:
  - transactions (a refused batch writes nothing);
  - apply re-validation (a tampered proposal file);
  - optimistic concurrency (a moved head);
  - two processes under the lock;
  - undo/redo/checkout over branches;
  - staleness and the stale move-aside;
  - deterministic revisions across two runs.

**3. View, render and CLI.**
- `view.py`, `tools.py` and `__main__.py`.
- Tests:
  - `inspect` content;
  - previews and renders checked with the renderer suite's identifiable media (frames exact, audio present and aligned);
  - `frames` images;
  - CLI exit codes and JSON errors for malformed input.

**4. The `edit` stage through M2a.**
- `publish.py`, and the `junctions`/`interchange`/`editorial` changes.
- Tests:
  - publish from one session, then from another (refused, then replaced and moved aside);
  - stale `edit`;
  - export of `edit` in OTIO/FCPXML/EDL with fidelity;
  - re-import against the exact snapshot after a later publish;
  - unresolvable and ambiguous fingerprints refused;
  - `human` made from an `edit` import, and its staleness;
  - the TUI and `junctions` listing `edit`.
- Docs: `docs/EDIT.md`, and the acceptance kit `docs/M3A-ACCEPTANCE.md`.

## Acceptance

- **Automated:**
  - the tasks' tests;
  - the prototype's 91-check scenarios re-expressed as tests against `matchcut.edit`, with every known gap classified as a known gap,
    never as a pass.
- **Human gate (PENDING until run):** an agent session (Claude Code) edits a real workspace through the CLI only, previews and publishes,
  and exports the `edit` stage to an editor. You watch the renders and check the export in the editor.

## Out of scope

- MCP (M3b);
- any GUI or TUI editing;
- editor (NLE) automation;
- carrying an agent's edits across a pipeline re-run;
- a `preserve_downstream_timing` operation;
- persistent timing locks;
- making the pipeline honour `hold_transition`;
- story-beat fitting;
- music placement operations.
