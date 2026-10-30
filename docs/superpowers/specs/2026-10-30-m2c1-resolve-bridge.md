# M2c-1: DaVinci Resolve bridge (push, goto, pull) — design

Status: specification for approval (2026-10-30). Nothing here is implemented.

Basis: M2c-0, the probe, and its first live run (DaVinci Resolve 21.0.2.4 free, macOS, run from Resolve's
own console; `docs/M2C-PROBE.md`, "First live run"). That run established the following, on that one
installation with synthetic footage:
- importing matchcut's OTIO gives a timeline whose clips are frame-exact;
- both dissolves came through as 8- and 18-frame "Cross Dissolve" items centred on their cuts;
- junction markers added by script are found again by their `customData`;
- `SetCurrentTimecode(start timecode + record frame)` lands the playhead on the incoming clip of every
  junction;
- Resolve's own OTIO export of that timeline reads back through M2a with no differences.

Still unverified, and to be stated wherever results are reported:
- real camera footage, audio fidelity, and embedded or drop-frame timecode;
- Studio, other Resolve versions, Windows and Linux;
- the full push/goto/pull workflow.

The human gates for M1, M2b, M2a and M2c stay PENDING.

## Goal

From a matchcut workspace:

- **`resolve push`** puts an editable Resolve timeline of a stage, normally `final`, into a
  matchcut-owned Resolve project. It uses the measured import-otio route, so cuts and dissolves come
  across as Resolve's own items. It then adds one marker per junction carrying a stable identifier, and
  checks the result against matchcut's canonical timeline.
- **`resolve goto N`** (or `--junction s3-s4`) moves Resolve's playhead to junction N of a pushed timeline,
  with the measured mechanism, and confirms the incoming clip is under it.
- **`resolve pull`** (Phase 2) exports the pushed timeline, as edited in Resolve, through OTIO. It hands the
  file to M2a's import, which writes the human stage under M2a's rules: snapshots, stale handling, pins,
  inherited decisions and ambiguity notes.

## Phases

- **Phase 1: push and goto.** Implemented, reviewed, then exercised by you in Resolve against the Phase 1
  acceptance checklist below.
- **Phase 2: pull.** Specified here, so that Phase 1's identifiers and records serve it. Implementation
  starts only after Phase 1 has been exercised in Resolve and you approve.

## Principles

1. **matchcut stays independent of Resolve.** These all stay in `matchcut/interchange` and the engine,
   exactly as today:
   - the canonical timeline;
   - frame and timecode arithmetic;
   - the OTIO writer, validation and clip identity (`Clip.origin`, clip keys, junction ids);
   - provenance and the human stage.

   The Resolve side only executes and records. Interpretation happens in matchcut afterwards, as with the
   probe: **scripts record, matchcut decides.**
2. **Shared logic, not duplicated scripts.** One Resolve-side runtime serves every operation and both ways
   of running:
   - the free edition, from Resolve's console;
   - Studio, from Terminal (external scripting).

   The runtime covers module discovery or the injected console `resolve`, the call recorder and allow-list,
   the safety checks, timecode helpers, and result writing. The probe's existing runtime is factored into
   it, and the probe moves onto it, behaviour unchanged. Each generated script is that shared runtime plus
   one operation body, assembled from source templates. Each one stays self-contained: Python 3.6 syntax,
   stdlib plus Resolve's module, and no matchcut imports.
3. **Documented calls only.** The allow-list is the 21.0.4 README already used by the probe. Calls added
   later are gated on the installed catalogue. Phase 1 needs no gated call.
4. **Safety, identical to the probe's and checked the same way:**
   - only matchcut-owned projects are written;
   - before a project switch: an empty project or the owned project must be open, `--yes`, a recheck, and
     a check just before the switch;
   - a check that the owned project is still open before every write;
   - only the owned project is ever saved;
   - nothing is ever deleted.
5. **Never replace, never overwrite.**
   - A push never replaces, renames or edits an existing timeline's clips.
   - A pull never silently replaces an existing human stage.
6. **Idempotent where feasible.** Running push or goto twice has the same effect as running it once.
   Every run says what it found and what it changed.
7. **Clear failure reporting.**
   - Every run writes a result file and prints a short console summary.
   - Every outcome is classified in the probe's five categories: Passed, Failed, Unsupported, Not
     preserved by Resolve, Not tested.
   - Exit codes are documented.

## Resolve-side targets and identity

- **The owned project.** One per workspace: `matchcut-<workspace name>`, in the project-manager folder
  `matchcut`, created on the first push.
  - Its `Project.GetUniqueId()` is recorded in the workspace on the first push. Every later operation
    checks both the name and that id before doing anything.
  - A project that has the name but not the recorded id is refused: "not this workspace's project".
  - If the record is lost, `--adopt` re-records the id. It works only for a project that matches by name
    and whose timelines all carry matchcut's names.
- **Pushed timelines.** Named `matchcut <stage> <stage sha12>`, for example `matchcut final 3f9a1c2e0b44`,
  where the sha is the stage file's sha256 that M2a's snapshot already uses.
  - Each push records the timeline's `Timeline.GetUniqueId()`, its name, the stage, the stage sha, the
    plan's hash and the read-back.
  - goto and pull find the timeline by unique id first, then check its name. A mismatch is refused.
- **Junction markers.** One per junction, at the frame where the incoming clip starts, relative to the
  timeline's start frame.
  - The marker name is the existing `J<n> s<out>->s<in>`.
  - The note is the junction's why line, truncated as M2a does.
  - The colour is fixed, for example Blue, for all matchcut markers. Pinned junctions get a second colour.
  - `customData` is `matchcut:<workspace id>:<stage sha12>:<junction id>`. One string per marker is all
    Resolve offers. The workspace id is a random id written once to `WS/resolve/workspace_id`. This
    makes a marker unambiguous across workspaces and pushes.
  - If a marker already exists at that frame (for example one Resolve imported from the file),
    `UpdateMarkerCustomData` sets the id on it rather than adding a second marker. This behaviour was
    observed in the probe.
- **The push record: `WS/resolve/pushes.json`,** written by `matchcut resolve check` from the run results.
  It lists the owned project (name and id) and, per pushed timeline: name, unique id, stage, stage sha,
  plan hash, the verdict, and the time. A push run is "applied" only after `check` has read it.

## Commands (matchcut side)

All commands need only the workspace. Each one prints the console line, for the free edition, and the
Terminal command, for Studio.

- **`python -m matchcut.resolve push WS [--stage final]`**
  1. Exports the stage with M2a's `export_workspace` (OTIO plus the stage snapshot) into `WS/resolve/`.
  2. Writes `plan.json`, holding:
     - the expected clip list (frames), the junctions (id, record frame, name, note, colour, `customData`)
       and the rate;
     - the owned project's name and id (if known), the timeline name and the OTIO path;
     - the plan hash.
  3. Generates `push.py`.
  4. Refuses, with a one-line reason, when:
     - the workspace has no such stage;
     - the stage would fail M2a's export check;
     - the source footage is missing.
- **`python -m matchcut.resolve goto WS N | --junction ID [--stage S]`**
  - Uses the newest applied push of the stage. With none, it refuses: "push first".
  - Writes `goto.json` (the timeline id and name, the junction's record frame, its `customData`, and the
    expected incoming clip's source in and record range) and generates `goto.py`.
  - Refuses an N out of range, or an id not in the pushed stage. If the stage has changed since the
    push, it refuses: "the pushed timeline is stage <sha12>, the stage is now <sha12>: push again".
- **`python -m matchcut.resolve check WS [RUN]`**
  - Reads the newest push or goto result (or RUN) and compares it with the plan, using the probe report's
    rules: transition items identified without GetType, the source end at the media's end, and the
    timeline start offset.
  - Writes `report.md` next to the result, updates `pushes.json` for a push, and prints the verdict.
- **`python -m matchcut.resolve pull WS [--stage S]` (Phase 2)**
  - Generates `pull.py` for the pushed timeline.
  - After it has run, `python -m matchcut.resolve pull WS --import RUN` hands the exported OTIO to
    `import_timeline` (see Phase 2).
- **TUI (Phase 1):** in the Edit view, `g` shows the goto console line for the selected junction and copies
  it to the clipboard when the terminal supports it. On Studio with external scripting available, it can
  run the goto directly. The view never pushes or pulls.

Every generated script takes `--dry-run`. In console mode it connects, checks targets and safety, records,
and writes nothing in Resolve.

## Resolve-side operations

### push.py

1. Connect (injected `resolve` in the console, or the module plus `scriptapp` from Terminal). Record the
   product, version and edition.
2. **Find the target project.**
   - If the owned project (by name and id) is open, use it.
   - If it exists but another project is open, the open project must pass the probe's empty-project
     guard. Then `LoadProject` the owned project, using the same `--yes`, recheck and pre-switch check as
     the probe.
   - If it does not exist, the open project must be empty. Then create the owned project in the `matchcut`
     folder, the same way the probe creates its project.
   - Otherwise refuse (exit 3) and say which project to open.
3. **Find an existing timeline.**
   - If a timeline named `matchcut <stage> <sha12>` exists, read it back and compare it with the plan:
     - It matches: add any missing markers, set `customData` where it is missing, change nothing else.
       Report "already pushed: verified, N markers added".
     - It differs: change nothing. Report "the timeline <name> was edited in Resolve since it was pushed:
       not replaced (pull it first, or push to a new timeline with `--as-new`)".
   - `--as-new` imports to `matchcut <stage> <sha12> (2)` and so on, never replacing the existing one.
4. **Import.** `MediaPool.ImportTimelineFromFile(otio, {"timelineName": name, "importSourceClips": True})`.
   - A None result fails (exit 4).
   - Then `SetCurrentTimeline` on the new timeline.
5. **Markers.** For each junction: `AddMarker(frame, colour, name, note, 1, customData)`. If that returns
   False and a marker exists at that frame, `UpdateMarkerCustomData`. Then `GetMarkerByCustomData` to
   confirm. Record each step.
6. **Read-back.** Read the timeline as the probe does: start frame and timecode, every video and audio
   item, markers, and `GetUniqueId`.
7. **Save.** Save the owned project, after the still-ours check, and write `result.json`.
8. **Print the summary.** For example "pushed 'matchcut final 3f9a…' to matchcut-demo: 5 clips, 2
   transitions, 4 markers; now run: python -m matchcut.resolve check WS".

### goto.py

1. Connect.
2. The owned project must already be open (by name and id). goto never switches projects; otherwise it
   refuses with "open matchcut-<ws> first".
3. Find the timeline by unique id and check its name. `SetCurrentTimeline` if it is not current.
4. Confirm the junction marker by `GetMarkerByCustomData(customData)`.
   - A missing marker is reported as Not preserved, because the user may have deleted it.
   - goto still proceeds by frame, but says so.
5. `SetCurrentTimecode(start timecode + record frame)`, with the timecode computed by the shared runtime at
   the timeline's rate, drop-frame aware. Then `OpenPage("edit")` and `GetCurrentTimecode()`.
6. `GetCurrentVideoItem()`: the incoming clip must be under the playhead. Compare its source start and
   record range with goto.json, using the read-back rules.
7. Print the summary, for example "junction 3 (s3-s4) at 00:00:04:14: the incoming clip c004 is under the
   playhead", or the specific mismatch. Write `result.json`.
8. Nothing is saved. Changing the current timeline, the page and the playhead are the only effects.

### pull.py (Phase 2)

1. Connect. The owned project must be open (goto's rule). Find the timeline by unique id and check its
   name.
2. `Timeline.Export(path, EXPORT_OTIO, EXPORT_NONE)` into `WS/resolve/runs/<UTC>-pull/timeline.otio`.
   Record the timeline's read-back alongside, as evidence.
3. Change nothing in Resolve, and save nothing.

## Pull into matchcut (Phase 2)

`python -m matchcut.resolve pull WS --import RUN`:

1. **Check the pulled file.** It must come from a timeline in `pushes.json` (by unique id and name).
   - The stage sha comes from the push record, not from the file, because Resolve does not keep
     matchcut's metadata.
   - The diff is taken against M2a's snapshot `exports/.stages/<stage>-<sha12>.json`. Push guarantees the
     snapshot exists, because it exports through M2a.
2. **Protect the existing human stage.** If `human.json` exists:
   - **It came from this same pushed timeline** (its `human_import.json` records the push's timeline id):
     the previous human stage is archived to `WS/resolve/human_history/<UTC>/` (human.json plus
     human_import.json), then replaced, with a printed note.
   - **It came from anything else** (another file, another timeline, a manual import): pull refuses, and
     says what exists and where it came from. `--replace-human` archives and replaces it; without that
     flag nothing changes.
   - Nothing is ever deleted.
3. **Import.** Run `import_timeline(ws, cfg, otio, fmt="otio", stage=<pushed stage>)`, with the snapshot
   from step 1. Record `source = resolve:<project id>/<timeline id>` and the run directory in
   `human_import.json`.
4. **Report.** Print M2a's import report (the human decisions and notes) plus the pull's own
   classification, e.g. "the junction markers carried no ids back: matched by source range, as M2a
   does".

## Results and exit codes

- **Files.** Each script run writes `WS/resolve/runs/<UTC>-<op>[-dry-run]/result.json`, holding:
  - every call (name, arguments, return or error), the safety record and the read-back;
  - the edition and version, and the console or external mode.

  The probe's per-run directories and selection rules apply. `check` and `pull --import` read the newest
  complete run of their kind.
- **Exit codes** (the probe's, extended):

  | code | meaning |
  |---|---|
  | 0 | done |
  | 1 | error in the script itself |
  | 2 | not connected |
  | 3 | refused by a safety or target check |
  | 4 | Resolve refused an operation (import failed, marker could not be set, and so on), or the open project changed mid-run |
  | 5 | not confirmed |
  | 6 | already in the requested state (push verified, nothing changed) |

## Verification

**Deterministic offline tests:**
- The fake Resolve gains timeline import from OTIO, markers, unique ids, `LoadProject`, `GetCurrentVideoItem`
  and Export. It includes a variant that reproduces the live 21.0.2.4 behaviour: transition items without
  GetType, the source end clamped at the media's end, and markers from files not kept.
- **Tests:**
  - push into a new owned project;
  - push again (verified, nothing changed);
  - push after a Resolve-side edit (refused, not replaced), and `--as-new`;
  - push with a stage that changed (new timeline, old one kept);
  - project-id mismatch refused; non-empty user project refused;
  - mid-run project switch (stopped, not saved);
  - goto for every junction, with the playhead and incoming-clip check; goto on a stale push refused;
  - goto with the owned project not open refused; goto with a deleted marker proceeds by frame and
    reports it;
  - Phase 2: pull through a simulated Resolve edit (trim, dissolve to cut, delete) gives exactly those
    human decisions;
  - Phase 2: pull with a foreign human.json refused; `--replace-human` archives it.
- **Script properties:** every generated script compiles as Python 3.6, imports no matchcut module, and
  makes only allow-listed calls, with the WRITES-coverage test extended.
- **Shared runtime:** the probe's existing tests pass unchanged on top of it.

**Human acceptance (Phase 1)** is the checklist below. It runs on your Mac, Resolve 21.0.2.4 free, from
the console. Real camera footage, audio and timecoded media are separate items there, and stay unverified
until done.

## Phase 1 acceptance checklist (proposed)

Setup:
- `git pull` on the M2c-1 branch.
- Use the demo workspace (`~/Movies/matchcut-demo_edit`) unless an item says otherwise.
- Resolve 21.0.2.4 free, Workspace › Console › Py3, an empty project open.

1. **First push.** Run `python -m matchcut.resolve push WS` and paste the push line into the console.
   - Expect exit 0 and the summary line.
   - Expect a new project `matchcut-matchcut-demo_edit` in the `matchcut` folder, and a timeline
     `matchcut final <sha12>`.
2. **Look at the timeline.** It should have 5 clips, with Cross Dissolves at the 2nd and 4th junctions
   (8 and 18 frames) and 4 blue markers named `J1 …` to `J4 …`, each with a note.
3. **Check.** Run `python -m matchcut.resolve check WS`.
   - Expect: frames exact, dissolves exact and centred, markers 4 of 4 found by id.
   - Expect no Failed outcomes.
4. **Push again** (same line). Expect "already pushed: verified", exit 6, and no new timeline or markers.
5. **Goto.** Run `python -m matchcut.resolve goto WS 3` and paste the line.
   - Expect the playhead on junction 3 on the Edit page, with the incoming clip under it.
   - Repeat for junctions 1 and 4.
6. **Bad goto.** Run `goto WS 9`. Expect a refusal from matchcut before anything runs.
7. **Wrong project.** Open another project with content, then paste the push line. Expect a refusal
   (exit 3), with that project unchanged.
8. **Edited timeline.** In Resolve, trim one clip of the pushed timeline, save, then paste the push line
   again. Expect "edited in Resolve since it was pushed: not replaced", with your trim intact.
9. **New stage.** Re-run the pipeline so `final` changes (e.g. `-t 10`), push again. Expect a second
   timeline with the new sha. The first is untouched, and goto now uses the new one.
10. **Real footage (optional, separately reported).** Repeat 1–5 on a workspace made from one of your own
    camera clips. Note audio and any embedded timecode.

    Until this is done, real footage, audio and timecode stay UNVERIFIED.
11. **Clean-up knowledge.** The owned project stays. Delete it by hand if wanted; matchcut never deletes.

Send back the `WS/resolve/runs/` folders, or the `check` output, plus anything that looked wrong in
Resolve.

## Out of scope for M2c-1

- Building timelines by script, including `AddTransition`: the import route is the measured route.
- MCP.
- Rendering.
- Colour or Fusion work.
- Live sync.
- Editing a pushed timeline's clips in place.
- Studio-only features beyond running the same scripts externally. The Studio external mode is tested
  against the fake only, and stays UNVERIFIED until run on Studio.

## Open questions, settled by Phase 1's real run

1. Does `ImportTimelineFromFile` into a project with existing media duplicate the source clip in the media
   pool? Harmless either way; record it.
2. Does `LoadProject` on the free edition switch projects as `CreateProject` did in the probe?
3. Marker colour names accepted on 21.0.2: `AddMarker` returns False for an unknown colour. Record it, and
   fall back to the README's documented list.
4. Does the clipboard copy work in your terminal, for the TUI's `g`?
