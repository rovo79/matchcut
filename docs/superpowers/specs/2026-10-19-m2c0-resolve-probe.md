# M2c-0: DaVinci Resolve Studio capability probe — design

Status: approved scope (user, 2026-10-19). The first step of M2c (see the M2c plan: an editable Resolve
timeline, junction navigation, human edits back). The probe measures what the research could not verify,
in a real Resolve Studio, before any bridge is built. M2c-1 is not part of this. Human gates (M1, M2b, M2a,
and now M2c) stay PENDING.

## What it is

One generated Python script, `resolve_probe.py`. You run it once on a machine with Resolve Studio running.
In a disposable project it creates, it:

- tries every timeline route;
- reads back what Resolve made;
- tests navigation;
- exports the result again.

It then writes `resolve_probe.json`. matchcut reads that file (and the files the probe exported) offline,
compares them with the canonical cut, and reports which routes work on that install, with the measured
fidelity differences.

## Commands

- `python -m matchcut.resolve probe WS [--stage final] [--out DIR] [--run]`
  - exports the stage with M2a's `export_workspace` (OTIO, FCPXML 1.10, EDL) into a probe bundle
    `DIR` (default `WS/resolve_probe/`);
  - writes `DIR/resolve_probe.py`, a self-contained script: stdlib plus Resolve's own module only;
    it never imports matchcut;
  - writes `DIR/expected.json`: the canonical clips in frames, the junctions with their ids and record
    frames, the rate and start timecode;
  - prints the one command to run.
  - With `--run`, it runs the script directly when Resolve's scripting module can be imported, and
    says plainly when it cannot.
- `python DIR/resolve_probe.py [--dry-run] [--yes] [--out FILE]`, with Resolve Studio running and Preferences > System >
  General > External scripting using = Local. The script finds Resolve's scripting module from the documented
  environment variables and default install paths, and says exactly what to set when it cannot (and names a module
  file that exists but fails to import). Every run writes its own `DIR/runs/<UTC time>/` (`resolve_probe.json`,
  `exports/`), so no run replaces another's (review M6).
- `python -m matchcut.resolve report WS DIR` (the latest run) or `... report WS DIR/runs/<UTC time>/resolve_probe.json`
  - prints, per route, what was preserved, approximated and lost, compared with `expected.json` and
    with the stage;
  - re-imports each OTIO/FCPXML/EDL the probe exported with M2a's `import_timeline` readers, in a
    scratch copy and never into the workspace;
  - recommends a default route for M2c-1, or says none is usable;
  - writes `probe_report.md` in the run's directory.

## Safety

- **The probe never modifies an existing user project.**
  - Before anything else it records the current project's name and contents, with documented reads:
    `Project.GetTimelineCount()`, and the media pool root folder's `GetClipList()` and `GetSubFolderList()`.
  - It refuses to continue unless that project is an **empty** "Untitled Project" (no timeline, no clip, no media pool
    folder) or an earlier `matchcut-probe-*` project (allowed as it is; the docs say to save it if it was edited). The
    message says to save your work, then open a new, empty project (or quit and reopen Resolve) and run again. The
    README does not say what `CreateProject` does to the open project, and the API cannot tell whether it has unsaved
    changes. The counts are recorded in `safety`.
  - Before switching it prints the open project, the new project and its folder, and asks for confirmation: the user
    types `yes`. `--yes` skips the prompt; `--dry-run` never prompts and never switches.
  - After `CreateProject` it checks that its project is the open one, and checks again before each part of the run
    and before `SaveProject`; otherwise it stops and saves nothing. It never calls `SaveProject` on a project it did not
    create.
- **Its own project:** it creates a fresh project, `matchcut-probe-<UTC timestamp>`, inside a project
  manager folder `matchcut-probe`, created if missing, and leaves it there for inspection. Re-running
  creates a new project. It never deletes, renames or exports any other project. Delete the probe
  projects by hand in the Project Manager. `docs/M2C-PROBE.md` says how.
- **Media:** it imports only the bundle's source footage, by path, into its own project's media pool.
- **Files:** it writes only inside the bundle directory (one `runs/<UTC time>/` directory per run), or where `--out` says.
- **Errors:** every step is wrapped. A failure is recorded, with the call, the exception text and its
  return value, and the probe moves on. It never leaves Resolve on a half-made user project, because
  it never touches one.

## Documented calls only

- **Allowed calls:** the probe calls only what the Resolve scripting documentation describes. The
  allow-list is the 21.0.4 README (community copy:
  https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8) plus, for calls added later, the
  installed API catalogue.
- **The installed catalogue:** the probe locates it at the standard path for the platform
  (`Developer/Scripting/README.txt`, or since 21.1 `README.md` / `DaVinciResolveScript.pyi`). It
  records its path and version line, and which of the probe's calls it mentions.
- **Newer calls are gated:** a call that is not in the 21.0.4 README, such as `TimelineItem.AddTransition`,
  `GetType` or `SetFades`, is made only if the installed catalogue documents it for its class (a `def` inside the class in
  a `.pyi`; the class's section, or `Class.Method`, in a text catalogue). Otherwise it is recorded as "not documented on
  this install: not called". (`GetSourceStartFrame`/`GetSourceEndFrame` are in the 21.0.4 README, and in 20.2, so they
  are not gated; they are not in 18.5, which the JSON and report note for Resolve below 20.)
- **Option keys are recorded, not guessed:** for `AddTransition`, the probe records the option keys and
  values exactly as tried, taken only from AddTransition's own catalogue entry and the body of the Options type it names
  (`duration` only when that text says frames). Each attempt is recorded
  separately: documented in the catalogue, called, returned.
- **MCP:** never started or connected to. The probe records whether the `ResolveMCP` binary exists at
  the reported install paths, as the observed file presence only, next to what is documented or
  reported (Studio 21.1, stdio, about 14 tools). Nothing about its behaviour is claimed.

## Experiments (each recorded with its steps)

1. **Environment:**
   - Resolve product name, version and version string; Python version and platform;
   - whether external scripting connected;
   - the project manager's current project and database (`GetCurrentDatabase` if documented).
2. **Routes to a timeline**, each into its own named timeline:
   - **import-otio:** `MediaPool.ImportTimelineFromFile(otio, {"timelineName", "importSourceClips": True})`.
   - **import-fcpxml:** the same with the FCPXML 1.10.
   - **import-edl:** the same with the EDL. Media linking for an EDL may need the source clip already in
     the pool; the probe imports it first and records what happened.
   - **build:** `ImportMedia([source])`, `CreateEmptyTimeline(name)`, then `AppendToTimeline` with
     `[{mediaPoolItem, startFrame, endFrame, recordFrame, trackIndex}]` in canonical frames. A `[None]`
     result is recorded as a failure.
   - **build + transitions:** on the built timeline, `AddTransition` per dissolve, only if the
     catalogue documents it, with the documented option keys. Each attempt is recorded.
3. **Read-back** for every timeline that exists:
   - start frame and start timecode, frame rate setting, duration;
   - track counts per type;
   - per video item: name, start, end, duration, left/right offsets, and source start/end frames only
     where documented;
   - transitions as the API exposes them;
   - markers (`GetMarkers`, including `customData`);
   - audio items on each audio track.
4. **Markers:** on the build and import-otio timelines, `AddMarker(frame, color, name, note, 1, junction_id)`
   per junction, then `GetMarkerByCustomData(junction_id)` for each. The probe records whether markers
   carried in the OTIO/FCPXML arrived, and whether the probe's own markers can be found by id.
5. **Navigation:** for up to 5 junctions per timeline,
   - `SetCurrentTimecode(tc)`, then `GetCurrentTimecode()`, and `GetCurrentVideoItem()` if documented;
   - the probe tries the timecode computed from the timeline's start timecode plus the junction's
     record frame, and also the marker's frame;
   - it records the returned timecodes and whether the item under the playhead is the incoming clip;
   - it also opens the Edit page with `OpenPage("edit")` once.
6. **Export round trip:** for each timeline,
   - `Timeline.Export(path, resolve.EXPORT_OTIO, resolve.EXPORT_NONE)`, plus FCPXML 1.10 and EDL with
     the documented subtype;
   - files go into the run's `DIR/runs/<UTC time>/exports/`;
   - the probe records success and file size. The offline report compares their content.

## `resolve_probe.json`

- `probe_version`, `created_utc`
- `bundle`: stage, the bundle's file hashes, and matchcut's version
- `resolve`: product, version, version_string
- `platform`
- `scripting`: connected, module path
- `catalogue`: path, version line, documented calls found
- `mcp`: binary paths checked and found, `observed: "file present / absent"`, `documented: "..."`
- `project`: the probe project name and folder, and the user's current project name (only as recorded
  before switching)
- `timelines`: per route, with these fields: steps (call, documented_in, ok, return or error),
  readback, markers, navigation, exports
- `failures`: every failed step, collected
- `notes`

Every value is observed or recorded; nothing is inferred in the script. Interpretation happens in
`matchcut.resolve report`.

## Verification

- **Deterministic offline tests:**
  - a fake of the documented Resolve API that holds a real model: projects, folders, media pool,
    timelines with items and source/record frames, markers with `customData`, a playhead, and
    `Export(EXPORT_OTIO)` written from the model;
  - the fake rejects every call that is not in the allow-list;
  - the generated script runs against it end to end. Assertions:
    - the safety guard refuses a non-default current project;
    - a new uniquely named project and folder are created;
    - no other project is touched;
    - catalogue gating holds: an undocumented call is never made;
    - failures are recorded and the run continues;
    - the JSON schema is complete;
    - the report command's route verdicts and fidelity differences are correct, for a fake that keeps
      everything, one that loses dissolves, one that shifts frames, and one that drops markers;
  - the script compiles under Python 3.6+ syntax (Resolve's documented minimum) and imports nothing
    from matchcut.
- **Honesty:**
  - The fake is matchcut's model of the documentation, not evidence about Resolve.
  - `docs/M2C-PROBE.md` and every output say "not run against a live Resolve" until a real
    `resolve_probe.json` comes back.
  - Human gate PENDING: you run the probe in Resolve Studio and send back the run's folder
    `runs/<UTC time>/` (its `resolve_probe.json` and `exports/`).
