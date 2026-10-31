# M2c-1: DaVinci Resolve bridge (push, goto, pull) — design

Status: approved (2026-10-31) as an optional integration design. Nothing here is implemented, and
implementation has not been scheduled. Resolve stays optional: matchcut's canonical timeline, FFmpeg renderer,
TUI and M2a file round trip never depend on it.

Basis: M2c-0, the probe, and its first live run (DaVinci Resolve 21.0.2.4 free, macOS, run from Resolve's
own console; `docs/M2C-PROBE.md`, "First live run"). That run established the following, on that one
installation with synthetic footage:
- importing matchcut's OTIO gives a timeline whose clips are frame-exact;
- both dissolves came through as 8- and 18-frame "Cross Dissolve" items centred on their cuts;
- junction markers added by script with `AddMarker` are found again by their `customData` (4 of 4). The
  markers carried in the OTIO were not found by their ids. That Resolve imported none of them is an
  inference: `AddMarker` succeeded at those same frames, 4 of 4, and the user's report showed no marker
  failure. Phase 1's read-back of `GetMarkers()` settles it;
- `SetCurrentTimecode(start timecode + record frame)` lands the playhead on the incoming clip of every
  junction;
- Resolve's own OTIO and EDL exports of that timeline read back through M2a with no clip or frame
  differences.

Not measured by the probe, and therefore questions for Phase 1's real run:
- where markers land: `GetMarkerByCustomData` returned no frame on that install, so the frame keys from
  `GetMarkers()` were never compared with the plan;
- `UpdateMarkerCustomData`, which was never called;
- `ProjectManager.LoadProject`;
- `Project.GetUniqueId` and `Timeline.GetUniqueId`. All three are documented in the 21.0.4 README but
  were never called.

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
  - Its `Project.GetUniqueId()` is recorded in the workspace on the first push.
  - When the owned project is already open, every later operation checks its name and that id before
    doing anything.
  - When it is not open, its id cannot be read without loading it, so push follows these steps:
    1. The open project must first pass the probe's empty-project guard, the confirmation, the recheck
       and the check just before the switch.
    2. `GotoRootFolder`, then `OpenFolder("matchcut")`, because `LoadProject` resolves names in the
       current folder.
    3. `LoadProject(name)`.
    4. Verify the id before any write. A mismatch stops the run with no writes, and the report says the
       switch happened. The project switched away from was verified empty, so nothing is at risk.
  - `CloseProject`, which closes without saving, is never called.
  - A project that has the name but not the recorded id is refused: "not this workspace's project".
  - If the record is lost, `--adopt` re-records the id. It works only for a project that matches by name
    and whose timelines all carry matchcut's names.
  - **Unmeasured until Phase 1's real run:** whether `GetUniqueId` is stable across a Resolve restart and
    a reopen on the free edition. A None or empty id makes the operation refuse with that finding.
- **Pushed timelines.** Named `matchcut <stage> <stage sha12>`, for example `matchcut final 3f9a1c2e0b44`,
  where the sha is the stage file's sha256 that M2a's snapshot already uses.
  - Each push records the timeline's `Timeline.GetUniqueId()`, its name, the stage, the stage sha, the
    plan's hash and the read-back.
  - goto and pull find the timeline by unique id, iterating `GetTimelineCount` and `GetTimelineByIndex`.
    The id is the identity. A renamed timeline is still the same timeline, so the operation proceeds and
    reports the new name. If no timeline has the id, the operation refuses.
- **Junction markers.** One per junction, at the frame where the incoming clip starts, relative to the
  timeline's start frame.
  - The marker name is the existing `J<n> s<out>->s<in>`.
  - The note is the junction's why line, truncated as M2a does.
  - The colour is fixed, for example Blue, for all matchcut markers. Pinned junctions get a second colour.
  - `customData` is `matchcut:<workspace id>:<stage sha12>:<junction id>`. One string per marker is all
    Resolve offers. The workspace id is a random id written once to `WS/resolve/workspace_id`. This
    makes a marker unambiguous across workspaces and pushes.
  - On the tested install, `AddMarker` succeeded for all 4 junctions, because Resolve imported no markers
    from the OTIO.
  - If `AddMarker` returns False because a marker already exists at that frame, the existing marker is
    left alone. That is the user's marker or one Resolve imported, and matchcut does not claim it. The
    junction is reported as "marker not placed: a marker already exists at frame F".
    `UpdateMarkerCustomData` is not used in Phase 1 (it was never tested).
  - Where a marker lands is measured, not assumed. `check` compares the frame keys of `GetMarkers()`
    with the plan, because `GetMarkerByCustomData` returned no frame on the tested install.
- **The push record: `WS/resolve/pushes.json`,** written by `matchcut resolve check` from the run results.
  It lists the owned project (name and id) and, per pushed timeline: name, unique id, stage, stage sha,
  plan hash, the verdict, and the time. A push run is "applied" only after `check` has read it.

## Commands (matchcut side)

All commands need only the workspace. Each one prints the console line, for the free edition, and the
Terminal command, for Studio.

- **`python -m matchcut.resolve push WS [--stage final]`**
  1. Exports the stage with M2a's `export_workspace(stages=[stage], formats=["otio"])` into the workspace's
     standard `WS/exports/`.
     - That writes the OTIO and the stage snapshot `WS/exports/.stages/<stage>-<sha12>.json`, which is
       where M2a's import looks for snapshots.
     - M2a merges provenance and fidelity per stage and format, so other exports there are kept.
     - Like any M2a export of that stage, push overwrites `WS/exports/<stage>.otio`. It also regenerates
       `open_in_resolve.py` if that script targets the stage. The command says so, and the OTIO's sha256
       is recorded in `plan.json` and `result.json`.
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
  - Uses the newest applied push of the stage. With none, it refuses with a message that says what is
    missing:
    - "push first" when there is no push run at all;
    - "run `python -m matchcut.resolve check WS` first" when a push run exists but has not been checked.
  - Writes `goto.json` (the timeline id and name, the junction's record frame, its `customData`, and the
    expected incoming clip's source in and record range) and generates `goto.py`.
  - Refuses an N out of range, or an id not in the pushed stage. If the stage has changed since the
    push, it refuses: "the pushed timeline is stage <sha12>, the stage is now <sha12>: push again".
- **`python -m matchcut.resolve check WS [RUN]`**
  - Reads the newest push or goto result (or RUN) and compares it with the plan, using the probe report's
    rules: transition items identified without GetType, the source end at the media's end, and the
    timeline start offset.
  - It also compares the frame keys of `GetMarkers()` with the plan's marker frames, and the markers'
    names, notes and `customData`.
  - All interpretation of Resolve's read-back happens here, in matchcut, never in a generated script.
  - Writes `report.md` next to the result, updates `pushes.json` for a push, and prints the verdict.
- **`python -m matchcut.resolve pull WS [--stage S]` (Phase 2)**
  - Generates `pull.py` for the pushed timeline.
  - After it has run, `python -m matchcut.resolve pull WS --import RUN` hands the exported OTIO to
    `import_timeline` (see Phase 2).
- **TUI (Phase 1):** in the Edit view, `g` shows the goto console line for the selected junction and copies
  it to the clipboard when the terminal supports it. On Studio with external scripting available, it can
  run the goto directly. The view never pushes or pulls.

Every generated script takes `--dry-run`. With it, in either mode, the script connects, checks targets and
safety, records, and writes nothing in Resolve.

## Resolve-side operations

### push.py

1. Connect (injected `resolve` in the console, or the module plus `scriptapp` from Terminal). Record the
   product, version and edition.
2. **Find the target project.**
   - If the owned project (by name and id) is open, use it.
   - If it exists but another project is open, follow the switch rule in "The owned project" above: the
     empty-project guard, confirmation, recheck, `GotoRootFolder` and `OpenFolder("matchcut")`, then
     `LoadProject`, then verify the id before any write.
   - If it does not exist, the open project must be empty. Then create the owned project in the `matchcut`
     folder, the same way the probe creates its project, and record its id.
     - If `GetUniqueId` returns None or empty right after `CreateProject`, push stops before importing
       (exit 4) and records the finding. The new, empty project is left in place for open question 3.
       Nothing else is written.
   - Otherwise refuse (exit 3) and say which project to open.
3. **A timeline of that name already exists.** If a timeline named `matchcut <stage> <sha12>` exists, the
   script imports nothing and changes nothing.
   - The pushed timeline is found by the unique id recorded in `pushes.json` first, then by name, so a
    timeline the user renamed is still recognised and not imported a second time.
  - It records the timeline's read-back (items, markers, unique id) and exits with 0 and the message
     "a timeline for this stage already exists: run `python -m matchcut.resolve check WS`".
   - The script makes no judgement about whether that timeline was edited. `check` decides, in matchcut,
     and reports one of:
     - whether the clips and transitions match the plan or were edited in Resolve since the push;
     - and, independently, which junction markers are missing or not at their planned frames.

     Both are reported, so an edited timeline with a deleted marker reports both.
   - Re-placing missing markers is a separate, explicit step: `push --add-missing-markers` after `check`
     has listed them. A marker the user deleted is therefore never put back silently.
     - On a timeline `check` reported as edited, `--add-missing-markers` places a marker only where that
       junction's two clips are unchanged. Other junctions are reported, not marked.
   - `--as-new` imports to `matchcut <stage> <sha12> (2)` and so on, never replacing the existing one.
4. **Import.** `MediaPool.ImportTimelineFromFile(otio, {"timelineName": name, "importSourceClips": True})`.
   - A None result fails (exit 4), and nothing is saved.
   - Then `SetCurrentTimeline` on the new timeline.
5. **Markers.** For each junction, `AddMarker(frame, colour, name, note, 1, customData)`, then
   `GetMarkerByCustomData` to confirm.
   - A False return is recorded and reported per junction. The existing marker at that frame is left
     alone (see "Junction markers").
   - Every step is recorded.
6. **Read-back.** Read the timeline as the probe does: start frame and timecode, every video and audio
   item, `GetMarkers()` with frame keys, and `GetUniqueId`.
7. **Save and record.** Save the owned project only if this run changed it, after the still-ours check.
   A run that changed nothing saves nothing, so the user's own unsaved edits in that project are never
   saved by matchcut. Write `result.json`.
   - If the import succeeded but some markers could not be placed, the project is still saved: the
     imported timeline is valid. The run exits with 4 and lists the junctions without a marker.
8. **Print the summary.** For example "pushed 'matchcut final 3f9a…' to matchcut-demo: 5 clips, 2
   transitions, 4 markers; now run: python -m matchcut.resolve check WS".

### goto.py

1. Connect.
2. The owned project must already be open (by name and id). goto never switches projects; otherwise it
   refuses with "open matchcut-<ws> first".
3. Find the timeline by unique id. If it has been renamed, proceed and report the new name.
   `SetCurrentTimeline` if it is not current.
4. Confirm the junction marker by `GetMarkerByCustomData(customData)`.
   - A missing marker is reported as Not preserved, because the user may have deleted it.
   - goto still proceeds by frame, but says so.
5. `SetCurrentTimecode(start timecode + record frame)`, with the timecode computed by the shared runtime at
   the timeline's rate, drop-frame aware. Then `OpenPage("edit")` and `GetCurrentTimecode()`.
6. `GetCurrentVideoItem()`: record the item under the playhead (name, record range, source frames). The
   script prints that item. `check` compares it with goto.json using the read-back rules and confirms
   whether it is the incoming clip.
7. For immediate feedback, the script makes one raw comparison computed in advance by matchcut.
   - goto.json holds the incoming item's expected record start and end, as absolute frames. matchcut
     computes them from the push's recorded timeline start frame and read-back.
   - The script prints "matches the plan" or "does not match the plan" on exact equality. No conventions
     are interpreted in the script.
   - Print the summary, for example "junction 3 (s3-s4) at 00:00:04:14: item under the playhead matches
     the plan (record 114-155)", and write `result.json`.
8. Nothing is saved. Changing the current timeline, the page and the playhead are the only effects.

### pull.py (Phase 2)

1. Connect. The owned project must be open (goto's rule). Find the timeline by unique id. If it has been
   renamed, proceed and report the new name.
2. `Timeline.Export(path, EXPORT_OTIO, EXPORT_NONE)` into `WS/resolve/runs/<UTC>-pull/timeline.otio`.
   Record the timeline's read-back alongside, as evidence.
3. Change nothing in Resolve, and save nothing.

## Pull into matchcut (Phase 2)

`python -m matchcut.resolve pull WS --import RUN`:

1. **Check the pulled file.** It must come from a timeline in `pushes.json`, matched by unique id.
   - The stage sha comes from the push record, not from the file, because Resolve does not keep
     matchcut's metadata.
   - The diff is taken against M2a's snapshot `WS/exports/.stages/<stage>-<sha12>.json`. Push exported
     into `WS/exports/`, so the snapshot is where M2a looks.
   - **This needs a small, specified change to M2a's `import_timeline`, made in Phase 2:**
     - **`base_sha256=`**: when given, diff against the snapshot of exactly that stage sha. A missing or
       damaged snapshot is M2a's existing one-line error. Today the snapshot is chosen only from a
       fingerprint inside the file, which Resolve's export drops.
     - **`record_extra=`**: a dict written into `human_import.json` in the same atomic write, under one
       key, `"resolve": {...}`, for the Resolve source and the pull run. It can never overwrite M2a's own
       keys.
     - Both parameters are optional, so existing calls behave as today. M2a's tests are extended.
2. **Protect the existing human stage.** If `human.json` exists:
   - **It came from this same pushed timeline** (its `human_import.json` records the push's timeline id
     under `record_extra`):
     the previous human stage is archived to `WS/resolve/human_history/<UTC>/` (human.json plus
     human_import.json), then replaced, with a printed note.
   - **It came from anything else** (another file, another timeline, a manual import): pull refuses, and
     says what exists and where it came from. `--replace-human` archives and replaces it; without that
     flag nothing changes.
   - Nothing is ever deleted.
3. **Import.** Run `import_timeline(ws, cfg, otio, fmt="otio", stage=<pushed stage>,
   base_sha256=<push's stage sha>, record_extra={"source": "resolve:<project id>/<timeline id>", "pull_run":
   ...})`.
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
  | 0 | done, including "already present, nothing changed" (the message says which), so `&&` chains work in Terminal mode |
  | 1 | error in the script itself |
  | 2 | not connected |
  | 3 | refused by a safety or target check (nothing written) |
  | 4 | Resolve refused an operation, or the open project changed mid-run. The message and `result.json` say what was written and whether the project was saved. Example: import succeeded but some markers could not be placed means saved, with the junctions listed. |
  | 5 | not confirmed |

## Verification

**Deterministic offline tests:**
- The fake Resolve gains timeline import from OTIO, markers, unique ids, `LoadProject`, `GetCurrentVideoItem`
  and Export. It includes a variant that reproduces the live 21.0.2.4 behaviour: transition items without
  GetType, the source end clamped at the media's end, and markers from files not kept.
- **Tests:**
  - push into a new owned project;
  - push again: nothing imported, nothing saved, and `check` reports "matches the plan";
  - push after a Resolve-side edit: nothing changed, and `check` reports it edited;
  - `--as-new`;
  - a deleted marker is reported missing and only re-placed with `--add-missing-markers`;
  - a marker already at the frame is left alone and reported;
  - push into an existing owned project via the `LoadProject` switch, including an id mismatch after
    loading (stopped, no writes) and a None or empty id (refused);
  - `check` compares the marker frames from `GetMarkers()`;
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
2. **Look at the timeline.** It should have 5 clips, with Cross Dissolves at the 2nd and 3rd junctions
   (8 frames at J2, 18 frames at J3) and 4 blue markers named `J1 …` to `J4 …`, each with a note.
3. **Check.** Run `python -m matchcut.resolve check WS`.
   - Expect frames exact, and dissolves exact and centred.
   - Expect markers 4 of 4 found by id and at the planned frames. This is the first time marker placement
     is measured.
   - Expect no Failed outcomes.
4. **Push again** (same line).
   - Expect "a timeline for this stage already exists", exit 0, and no new timeline or markers.
   - `check` should then say "matches the plan".
5. **Goto.** Run `python -m matchcut.resolve goto WS 3` and paste the line.
   - Expect the playhead on junction 3 on the Edit page, with the incoming clip under it.
   - Repeat for junctions 1 and 4.
6. **Bad goto.** Run `goto WS 9`. Expect a refusal from matchcut before anything runs.
7. **Wrong project.** Open another project with content, then paste the push line. Expect a refusal
   (exit 3), with that project unchanged.
8. **Edited timeline.** In Resolve, trim one clip of the pushed timeline, save, then paste the push line
   again.
   - Expect "already exists" with nothing changed.
   - Then `check` should report "edited in Resolve since it was pushed", with your trim intact.
   - Delete one junction marker, push again, and run `check`. Expect both "edited" and "marker J2
     missing". The marker is not put back unless you run `push --add-missing-markers`.
8a. **Switch into the owned project.** Open a new, empty project (the owned project exists), then paste the
    push line.
    - Expect the confirmation, then a switch into `matchcut-…` by `LoadProject` with its id verified,
      then "already exists".
    - This is the first real test of `LoadProject` and project ids.
8b. **Restart.** Quit and reopen Resolve, open the owned project, and run goto 2.
    - Expect the timeline found by its unique id.
    - This checks that the ids are stable across a restart.
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
2. Does `LoadProject` on the free edition switch projects as `CreateProject` did in the probe? (checklist
   8a)
3. Are `Project.GetUniqueId` and `Timeline.GetUniqueId` non-empty and stable across a reopen and a Resolve
   restart? (checklist 8b) If not, identity falls back to name plus the recorded read-back, which `check`
   compares. That fallback would need a spec amendment before Phase 2.
4. Where do markers land? (checklist 3)
5. Marker colour names accepted on 21.0.2: record `AddMarker`'s result. Use only colours the README
   lists.
6. Does the clipboard copy work in your terminal, for the TUI's `g`?
