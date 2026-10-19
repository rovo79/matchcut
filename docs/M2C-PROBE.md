# M2c-0: the DaVinci Resolve Studio capability probe

**Status: not run against a live Resolve.** matchcut's authors have no Resolve Studio here. The probe has run only
against a fake of the documented scripting API (`tests/resolve/fake_resolve.py`), which is matchcut's model of the
documentation and not evidence about Resolve. This changes only when a real `resolve_probe.json` comes back.

- **M2c human gate: PENDING.** You run the probe once in Resolve Studio and send back the bundle's
  `resolve_probe.json` and `exports/` folder (below).
- **M2a human gate: PENDING** ([M2A-ACCEPTANCE.md](M2A-ACCEPTANCE.md)). **M2b human gate: PENDING**
  ([M2B-ACCEPTANCE.md](M2B-ACCEPTANCE.md)). **M1 human gate: PENDING** ([M1-ACCEPTANCE.md](M1-ACCEPTANCE.md)).

Design: [superpowers/specs/2026-10-19-m2c0-resolve-probe.md](superpowers/specs/2026-10-19-m2c0-resolve-probe.md).
Plan: [M2C-RESOLVE-PLAN.md](M2C-RESOLVE-PLAN.md).

## What it does

The research left open questions that only a real Resolve can answer: whether an imported OTIO or FCPXML keeps its
dissolves, whether markers and their `customData` survive, what start timecode an imported timeline gets, whether
`AppendToTimeline` places clips frame-accurately, what `AddTransition` (21.1+) needs, and what Resolve's own
OTIO export keeps. The probe measures them in one run, in a project of its own:

1. **Environment:** Resolve's product name and version, Python and platform, whether external scripting connected, the
   current database and project, the installed API catalogue (path and version line) and whether a `ResolveMCP`
   binary file is present.
2. **Five routes to a timeline**, each into its own timeline (`matchcut-probe <route>`):
   - `import-otio`, `import-fcpxml`, `import-edl`: `MediaPool.ImportTimelineFromFile(file, {"timelineName", "importSourceClips": True})`.
     The source footage is imported into the pool before the EDL.
   - `build`: `ImportMedia([source])`, `CreateEmptyTimeline`, then `AppendToTimeline` with `startFrame`, `endFrame`,
     `recordFrame` and `trackIndex` in the cut's frames (`endFrame` written as matchcut's exclusive end; the read-back measures
     what Resolve does with it). A `[None]` result is a recorded failure.
   - `build+transitions`: the same, then `TimelineItem.AddTransition` per dissolve, **only if the installed catalogue
     documents it**, with option keys and values taken from the catalogue's own text.
3. **Read-back** of every timeline: start frame and timecode, frame rate setting, track counts, every item's start, end,
   duration, offsets and source start/end frames, the markers.
4. **Markers:** on `build` and `import-otio`, `AddMarker(frame, "Green", name, note, 1, junction id)` per junction, then
   `GetMarkerByCustomData(junction id)`. On every import route it first looks for the markers the file brought.
5. **Navigation:** for up to 5 junctions per timeline, `SetCurrentTimecode` (from the timeline's start timecode plus the
   junction's record frame, and from the marker's frame), `GetCurrentTimecode`, `GetCurrentVideoItem`; `OpenPage("edit")` once.
6. **Export round trip:** `Timeline.Export` of every timeline as OTIO, FCPXML 1.10 and EDL into the bundle's `exports/`.

It writes everything to `resolve_probe.json` and interprets nothing. `python -m matchcut.resolve report` reads it
offline: it compares every timeline with `expected.json`, re-imports each exported file with M2a's readers in a scratch
copy of the workspace (never in the workspace), gives each route a verdict and recommends a default route for M2c-1, or
says that none is usable.

## Safety

- **It never modifies a project of yours.** Before anything else it records the name of the open project. It refuses to
  continue unless that is Resolve's empty default, **Untitled Project**, or an earlier `matchcut-probe-*` project, and
  tells you to save and close your project first. (Creating a project switches projects, and the API cannot tell whether
  the open one has unsaved changes. If you have work in an Untitled Project, save it under a name first.)
- **Its own project:** `matchcut-probe-<UTC time>`, in a Project Manager folder `matchcut-probe` (created if missing). It
  sets that project's timeline frame rate to the cut's, and saves only that project, after checking it is the open one.
  It never deletes, renames, saves, loads or exports any other project. Running it again makes a new project.
- **Media:** it imports only the bundle's source footage, by its path, into its own project.
- **Files:** it writes only inside the bundle directory (`resolve_probe.json`, `exports/`), or where `--out` says. A dry
  run writes `resolve_probe_dry_run.json`, and a run that stops early never replaces a complete `resolve_probe.json`
  (it writes `resolve_probe_last_attempt.json` instead).
- **Errors:** every call is wrapped. A failure is recorded with the call, its arguments, the exception text or the return
  value, and the probe moves on.
- **MCP:** never started or connected to; the probe only records whether the binary file exists.

## Documented calls only

The probe calls nothing but these. Source: the Resolve 21.0.4 scripting README, community copy
<https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8> ("Last Updated: 24 Jul 2026"). It was read through a
web reader that returns an extracted listing, not a byte copy; the README's own signatures are in `README_CALLS` at the top
of `resolve_probe.py`.

| class | README 21.0.4 calls the probe makes |
|---|---|
| DaVinciResolveScript | `scriptapp("Resolve")` (the README's example code) |
| Resolve | GetProjectManager, GetProductName, GetVersion, GetVersionString, OpenPage |
| ProjectManager | GetCurrentProject, GetCurrentDatabase, GotoRootFolder, GetFolderListInCurrentFolder, CreateFolder, OpenFolder, GetCurrentFolder, GetProjectListInCurrentFolder, CreateProject, SaveProject |
| Project | GetName, GetMediaPool, GetSetting, SetSetting (`timelineFrameRate`), SetCurrentTimeline |
| MediaPool | GetRootFolder, ImportMedia, ImportTimelineFromFile, CreateEmptyTimeline, AppendToTimeline |
| Folder | GetClipList |
| MediaPoolItem | GetName, GetClipProperty |
| Timeline | GetName, GetStartFrame, GetEndFrame, GetStartTimecode, GetSetting, GetTrackCount, GetItemListInTrack, GetMarkers, AddMarker, GetMarkerByCustomData, UpdateMarkerCustomData, SetCurrentTimecode, GetCurrentTimecode, GetCurrentVideoItem, Export |
| TimelineItem | GetName, GetStart, GetEnd, GetDuration, GetLeftOffset, GetRightOffset, GetSourceStartFrame, GetSourceEndFrame |

| gated: called only when the installed catalogue names it | source |
|---|---|
| TimelineItem.AddTransition | not in the 21.0.4 README; reported for 21.1+ by third parties. Option keys and values only as the installed catalogue's text gives them |
| TimelineItem.GetType | not in the 21.0.4 README; reported for 21.1+ by third parties |

`SetFades`, `GetFades`, `GetSourceStartTime` and `GetSourceEndTime` are looked up in the catalogue and recorded, never
called. The catalogue is looked for in `RESOLVE_SCRIPT_API`, then the default Scripting folder of the platform
(`README.md`, `DaVinciResolveScript.pyi`, `README.txt`, `CHANGELOG.md`). A gated call that the catalogue does not name is
recorded as "not documented on this install: not called". `GetSourceStartFrame` and `GetSourceEndFrame` are in the
21.0.4 README, so they are not gated.

## Prerequisites

- **DaVinci Resolve Studio**, running. External scripting needs the Studio edition (certainly from 21.1).
- **Preferences > System > General > External scripting using = Local**, then restart Resolve.
- **Python 3.6 or later, 64-bit** (the README's minimum), on the machine running Resolve.
- matchcut, to make the bundle and read the result (not needed to run the script itself).
- The source footage at the path the bundle names (the bundle's files and `expected.json` hold its absolute path). Make the
  bundle on the machine that runs Resolve, or put the footage at the same path there.

## Commands

Make the bundle (anywhere matchcut runs):

```shell
python -m matchcut.resolve probe WORKSPACE                 # stage final -> WORKSPACE/resolve_probe/
python -m matchcut.resolve probe WORKSPACE --stage roughcut --out DIR
python -m matchcut.resolve probe WORKSPACE --run           # also runs it, if Resolve's scripting module can be imported here
```

Run the probe with Resolve Studio open and Untitled Project as the open project. Try `--dry-run` first: it connects,
records the environment and the safety check, and creates nothing.

**macOS** (Terminal):

```shell
export RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
export PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules/"
python3 WORKSPACE/resolve_probe/resolve_probe.py --dry-run
python3 WORKSPACE/resolve_probe/resolve_probe.py
```

**Windows** (Command Prompt):

```bat
set RESOLVE_SCRIPT_API=%PROGRAMDATA%\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting
set RESOLVE_SCRIPT_LIB=C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll
set PYTHONPATH=%PYTHONPATH%;%RESOLVE_SCRIPT_API%\Modules\
py -3 WORKSPACE\resolve_probe\resolve_probe.py --dry-run
py -3 WORKSPACE\resolve_probe\resolve_probe.py
```

**Linux:**

```shell
export RESOLVE_SCRIPT_API="/opt/resolve/Developer/Scripting"
export RESOLVE_SCRIPT_LIB="/opt/resolve/libs/Fusion/fusionscript.so"
export PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules/"
python3 WORKSPACE/resolve_probe/resolve_probe.py --dry-run
python3 WORKSPACE/resolve_probe/resolve_probe.py
```

(Some Linux installs use `/home/resolve` instead of `/opt/resolve`.) The script also looks in these default folders by
itself, and prints exactly these lines when it cannot find the module. `--out FILE` writes the JSON elsewhere.

Read the result (offline, anywhere matchcut runs; it writes `probe_report.md` next to the JSON):

```shell
python -m matchcut.resolve report WORKSPACE WORKSPACE/resolve_probe/resolve_probe.json
```

Exit codes of the script: 0 done (failed steps are measurements and are recorded), 2 the module was not found or Resolve
did not answer, 3 refused by the safety check, 4 it could not make its own project, 1 an error of the probe itself (also
recorded in the JSON).

## What to send back

From the bundle directory: **`resolve_probe.json`** and the **`exports/`** folder (and `probe_report.md` if you ran the
report). They hold paths on your machine (the footage, the bundle, the Resolve install) and the project and database
names Resolve reported; nothing else about your projects.

## Deleting the probe projects

The probe never deletes anything. In Resolve, open the Project Manager, open the folder **matchcut-probe**, right-click
each `matchcut-probe-<time>` project and choose Delete (open another project first: Resolve does not delete the open
one). Once it is empty, delete the `matchcut-probe` folder the same way. Deleting a project does not delete the media files.

## Verification so far (automated only)

`tests/resolve/` runs the generated script end to end against the fake, through a `DaVinciResolveScript` module the
test puts in `sys.modules`. The fake holds a model (projects, folders, media pool, timelines with record and source frames,
transitions, markers with `customData`, a playhead, `Export` written from the model) and rejects any call outside its
own transcription of the 21.0.4 README, plus the gated calls when its catalogue names them. Variants: keeps everything,
loses dissolves, shifts frames by 1, drops markers, AddTransition not documented, `AppendToTimeline` returns `[None]`,
scripting not connected, a user project open (refused). The tests check the safety guard, the project and folder names,
that no other project is touched, the catalogue gating, recorded failures, the JSON schema, the report's verdicts and
differences, Python 3.6 syntax, and that the script imports nothing from matchcut. None of this is evidence about Resolve.
