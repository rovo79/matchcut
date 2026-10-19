# M2c: DaVinci Resolve integration — proposed plan (for approval)

Status: proposal, 2026-10-19. Nothing is implemented. Builds on M2a (`matchcut/interchange/`) and on the
Resolve research in `docs/M2-INTEGRATION-FEASIBILITY.md`. Human gates for M1, M2b and M2a stay PENDING.

## What M2c is for

Three outcomes, in priority order:

1. **A real, editable Resolve timeline** of a matchcut stage, frame-accurate on the source, made
   by matchcut rather than by the editor clicking through import dialogs.
2. **Junction navigation**: from matchcut (CLI, then the TUI Edit view), jump Resolve's playhead to
   junction N, and find any junction in Resolve by its id.
3. **Human edits come back**: what the editor changes in Resolve returns to matchcut as the `human`
   stage, with the provenance rules M2a established (pins, inherited decisions, ambiguity notes).

## Ground rules

- Resolve stays **optional**. Without it, M2a's files and manual GUI import/export keep working
  (free edition included). The engine never imports the Resolve bridge.
- **Studio is required for the bridge.** External scripting is Studio-only in 21.1, and must be
  set to Local in Preferences. Free users keep the M2a file route.
- **Scripting API, not MCP.** Resolve's MCP server (Studio 21.1, stdio, about 14 tools, mostly
  `run_script`) only wraps the same API in a sandbox with a 60 s limit per call. It has no
  timeline tools of its own. MCP stays out unless a later milestone wants an agent driving Resolve.
- **Never trust, always read back.** Every timeline matchcut puts into Resolve is read back through
  the API: clip order, source in/out frames, record frames, transitions and markers. It is then
  compared with the canonical stage by the same rules as M2a's fidelity gate. A mismatch is
  reported, never ignored.
- **Unknowns are measured before they are built on.** The research left several questions open
  (below). They are answered by a probe run in a real Studio, not assumed.

## What the research left open (must be measured)

| Question | Why it matters |
|---|---|
| Does `ImportTimelineFromFile` turn OTIO / FCPXML 1.10 dissolves into native transitions, at the right length and place? | decides between the import route and the native-build route |
| Do OTIO markers (name, note, `customData`) survive import? | junction navigation without re-adding markers |
| What start timecode does an imported timeline get, and do script markers follow it? | marker and playhead frames |
| Does Resolve conform timecoded / drop-frame media from matchcut's source times? | frame accuracy on camera media |
| `AddTransition` (21.1+): exact option keys, whether a dip to black exists, handle needs | native-build route for dissolves |
| What does `Timeline.Export(EXPORT_OTIO)` keep: clip names, our metadata, transitions, markers? | how human edits map back (keys vs source-range matching) |
| `AppendToTimeline` returning `[None]` on 21.1 (third-party report) | native-build reliability |

## Proposed scope

### M2c-0: Resolve capability probe (small, first)

- `matchcut/resolve/probe.py` generates a self-contained script. You run it once in Resolve Studio
  (21.0.x or 21.1+) on a matchcut export.
- It imports the OTIO and the FCPXML, builds the same cut natively with `AppendToTimeline`, and
  tries `AddTransition` and `AddMarker`. It reads every timeline back and exports each one again
  as OTIO.
- It writes `resolve_probe.json`: the measured answer to each question above, plus the Resolve
  version and edition. It touches only a scratch project it creates and names.
- `matchcut resolve probe-report resolve_probe.json` reads the result and prints which route is
  supported on that install.
- Automated tests: the probe runs against an extended fake of the documented API. That fake is
  promoted from M2a's resolve tests and covers semantics as well as call names.
- **Human gate (needs you, once):** run the probe in Studio and send back `resolve_probe.json`.
  This is the first real-Resolve data the project gets.

### M2c-1: Resolve bridge (push, goto, pull)

- Package `matchcut/resolve/`, loaded lazily, stdlib plus Resolve's `DaVinciResolveScript`.
  Commands:
  - `matchcut resolve push WS [--stage final] [--route import|build]` creates (or replaces) a
    timeline named for the workspace and stage.
    - **Import route**, the default if the probe shows dissolves survive: `ImportTimelineFromFile`
      with M2a's OTIO.
    - **Build route**: `ImportMedia` (the one source file), `CreateEmptyTimeline` and
      `AppendToTimeline` with exact `startFrame`/`endFrame`/`recordFrame`, then `AddTransition`
      for dissolves where the probe says it works.
    - Either way: markers with `customData` set to the junction id, clip colour for pinned
      junctions, then a **read-back check** against the canonical stage (a "resolve" row in the
      fidelity report: preserved / approximated / lost, as measured).
  - `matchcut resolve goto WS N` (or `--junction s3-s4`) finds the marker by `customData`
    (`GetMarkerByCustomData`), sets the playhead with `SetCurrentTimecode`, and opens the Edit
    page.
  - `matchcut resolve pull WS` runs `Timeline.Export(EXPORT_OTIO)` to a temporary file and hands
    it to M2a's `import_timeline`. The human stage, snapshots and stale rules all apply unchanged.
    Pull refuses a timeline that matchcut did not push, unless forced.
- TUI: a `g` key in the Edit view ("show in Resolve") calls `goto` for the selected junction, with
  a one-line status. Push and pull stay CLI-only in M2c-1.
- Identity for pull: Resolve keeps no per-item metadata (`TimelineItem` has no `SetMetadata`).
  So the mapping uses whatever the probe shows survives: clip names, then M2a's source-range
  matching with ambiguity notes, which the 84-edit proof already covers. Junction markers help
  re-anchor junction ids.
- Automated tests: a fake Resolve that holds a real timeline model (items with source and record
  frames, markers, transitions) and implements `Export(EXPORT_OTIO)` from that model. Tests cover
  push, read-back, goto, pull, and a simulated editor edit in the fake round-tripping to the
  correct human decisions.
- **Human gate:** push, goto and pull on a real Studio install with real footage, against a
  checklist like M2a's.

### M2c-2: only if the probe demands it

- If import loses dissolves or frames: make the build route the default and harden it (handles,
  `AppendToTimeline` failures, timecoded media).
- If Resolve's OTIO export loses too much: pull through FCPXML 1.10 export instead.

## Out of scope for M2c

- MCP and agent-driven Resolve.
- Rendering in Resolve (render jobs, mark in/out).
- Colour, Fusion, Fairlight and multicam.
- Live two-way sync: push and pull are explicit commands.
- Editing a pushed timeline in place: the API has no clip setters, so a push replaces the timeline.

## Risks

- **Nothing here can run in this container**: no Resolve, no GPU, no Studio licence. Everything
  real waits on your Studio install. Automated tests prove matchcut's side against the documented
  API and a faithful fake, not Resolve's behaviour.
- **The 21.1 API is not public online.** The `.pyi` and README.md ship only with an install. The
  probe should also copy the installed API catalogue's version line, so the bridge can check the
  calls it uses.
- **Studio cost and setup**: $295 one-time, plus external scripting set to Local.

## Proposed order and review points

1. **M2c-0 probe**: about 1 task plus a review. Stop. You run it in Studio and return the JSON.
2. With the probe results, a short M2c-1 spec update choosing the default route. Stop for approval.
3. **M2c-1 bridge**: about 4 tasks (bridge core and fake, push with read-back, goto plus the TUI
   key, pull). Each task reviewed, then a whole-branch review and an independent test report.
   Stop for review, with the human gate PENDING.
4. **M2c-2** only if step 2 calls for it.

On a new branch, as M2a was, so it can be reviewed on its own.
