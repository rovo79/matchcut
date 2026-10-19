# M2c-0: the DaVinci Resolve Studio capability probe

**Status: not run against a live Resolve.** matchcut's authors have no Resolve Studio here. The probe has run only
against a fake of the documented scripting API (`tests/resolve/fake_resolve.py`), which is matchcut's model of the
documentation and not evidence about Resolve. This changes only when a real `resolve_probe.json` comes back.

- **M2c human gate: PENDING.** You run the probe once in Resolve Studio and send back that run's folder,
  `runs/<UTC time>/` (its `resolve_probe.json` and `exports/`; see below).
- **M2a human gate: PENDING** ([M2A-ACCEPTANCE.md](M2A-ACCEPTANCE.md)). **M2b human gate: PENDING**
  ([M2B-ACCEPTANCE.md](M2B-ACCEPTANCE.md)). **M1 human gate: PENDING** ([M1-ACCEPTANCE.md](M1-ACCEPTANCE.md)).

Design: [superpowers/specs/2026-10-19-m2c0-resolve-probe.md](superpowers/specs/2026-10-19-m2c0-resolve-probe.md).
Plan: [M2C-RESOLVE-PLAN.md](M2C-RESOLVE-PLAN.md).

## What it does

The research left open questions that only a real Resolve can answer: whether an imported OTIO or FCPXML keeps its
dissolves, whether markers and their `customData` survive, what start timecode an imported timeline gets, whether
`AppendToTimeline` places clips frame-accurately (and whether its `endFrame` is inclusive), what `AddTransition`
(21.1+) needs, and what Resolve's own OTIO export keeps. The probe measures them in one run, in a project of its own:

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
     documents it for TimelineItem**, with option keys and values taken only from AddTransition's own catalogue entry and
     the Options type it names.
3. **Read-back** of every timeline: start frame and timecode, the timeline's settings snapshot, track counts, every item's
   start, end, duration, offsets, source start/end frames and (where documented) type, the markers.
4. **Markers:** on `build` and `import-otio`, `AddMarker(frame, "Green", name, note, 1, junction id)` per junction, then
   `GetMarkerByCustomData(junction id)`, recorded as Resolve returns it. On every import route it first looks for the
   markers the file brought.
5. **Navigation:** for up to 5 junctions per timeline, `SetCurrentTimecode` (from the timeline's start timecode plus the
   junction's record frame, and from the marker's frame when Resolve gave one), `GetCurrentTimecode`, `GetCurrentVideoItem`;
   `OpenPage("edit")` once.
6. **Export round trip:** `Timeline.Export` of every timeline as OTIO, FCPXML 1.10 and EDL into the run's `exports/`.

It writes everything to the run's `resolve_probe.json` and interprets nothing. `python -m matchcut.resolve report`
reads it offline. For each route it:

- compares every timeline with `expected.json`;
- names a uniform one-frame end offset as the convention it is ("GetSourceEndFrame is inclusive on this install",
  "endFrame is inclusive on this install") instead of calling it lost frames;
- counts transition items apart from the clips;
- judges the dissolves from the read-back when the install lists transition items, otherwise from each exported file,
  which it re-imports with M2a's readers in a scratch copy of the workspace (never in the workspace). It says which
  source it used;
- gives the route a verdict.

It then recommends a default route for M2c-1. It prefers a route where junction markers are found by id and navigation
lands, and says so when no route manages that. If no route is usable, it says so.

## Safety

- **It never switches away from work of yours.** Before anything else it records the open project's name and contents,
  with documented reads only. It reads `Project.GetTimelineCount()`, then walks the media pool from its root folder with
  `Folder.GetSubFolderList()` and reads `Folder.GetClipList()` in every folder. The walk goes three levels of bins deep
  and covers at most 50 folders. It continues only from:
  - **a project that is verifiably empty, whatever its name** (so a localised Resolve's untitled project works too). No
    timeline, and no clip in any media pool folder; empty bins are fine;
  - **an earlier `matchcut-probe-*` project**. That project is allowed as it is: if you edited it, save it first,
    because the probe switches away from it.

  It also refuses when emptiness cannot be confirmed: a read returns nothing or fails, or the bins go deeper or number
  more than the walk covers. Its message ends:
  "Save your work, then open a new, empty project (or quit and reopen Resolve) and run the probe again".
  The README does not say what `CreateProject` does to the open project, and the API cannot tell whether that project has
  unsaved changes. The counts (timelines, clips, folders, depth) are recorded in the JSON (`safety`).
- **It asks first, then looks again.** Before it switches projects it prints the open project, the new project's name
  (a `-2` suffix is added if that name is taken) and the folder, and waits for you to type `yes`. Anything else, or a
  closed input, stops it with nothing created (exit 5). `--yes` answers in advance.
  `--dry-run` never asks and never switches.
  Right after your answer it runs the whole check again. If the open project or its contents changed while it waited,
  it stops with nothing created (exit 3) and says what changed.
- **Its own project:** `matchcut-probe-<UTC time>`, in a Project Manager folder `matchcut-probe` (created if missing).
  - After `CreateProject` it checks that the new project is the open one; otherwise it stops (exit 4).
  - It sets that project's `timelineFrameRate` to the cut's rate (with ` DF` for drop-frame footage, as the README's
    notes describe).
  - From then on it checks that its project is the open one before every call that changes something: imports, timeline
    building, markers, transitions, settings, the playhead, the page, exports and `SaveProject`. If you switch projects
    while it runs, it stops before the next such call and saves nothing (exit 4).
  - It never deletes, renames, saves, loads or exports any other project. Running it again makes a new project.
- **Shared databases:** on a shared or remote project database, the `matchcut-probe` folder and its projects are visible
  to everyone who uses that database. Use a local database if that matters (the JSON records which database was current).
- **Media:** it imports only the bundle's source footage, by its path, into its own project.
- **Files:** it writes only inside the bundle directory, or where `--out` says. Every run writes its own directory
  `runs/<UTC time>/` (a dry run `runs/<UTC time>-dry-run/`), holding `resolve_probe.json` and `exports/`, so no run
  replaces another's results.
- **Errors:** every call is wrapped. A failure is recorded with the call, its arguments, the exception text or the return
  value, and the probe moves on.
- **MCP:** never started or connected to; the probe only records whether the binary file exists.

## Documented calls only

The probe calls nothing but these. Source of record: the Resolve 21.0.4 scripting README ("Last Updated: 24 Jul 2026"),
full text of the community copy <https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8> (sha256
`eb2b4ebc578655a627baeccdc68b194afb0cd7c96f819acd331125e8d699e5b5`). The method names it documents per class are in
`tests/resolve/readme_21_0_4.json`. The tests check the script against that list, and against the full text when a copy
is available. No call is in the README's deprecated or unsupported lists. The README's own signatures are in
`README_CALLS` at the top of `resolve_probe.py`.

| class | README 21.0.4 calls the probe makes |
|---|---|
| DaVinciResolveScript | `scriptapp("Resolve")` (the README's example code) |
| Resolve | GetProjectManager, GetProductName, GetVersion, GetVersionString, OpenPage |
| ProjectManager | GetCurrentProject, GetCurrentDatabase, GotoRootFolder, GetFolderListInCurrentFolder, CreateFolder, OpenFolder, GetCurrentFolder, GetProjectListInCurrentFolder, CreateProject, SaveProject |
| Project | GetName, GetTimelineCount, GetMediaPool, GetSetting, SetSetting (`timelineFrameRate`), SetCurrentTimeline |
| MediaPool | GetRootFolder, ImportMedia, ImportTimelineFromFile, CreateEmptyTimeline, AppendToTimeline |
| Folder | GetClipList, GetSubFolderList |
| MediaPoolItem | GetName, GetClipProperty |
| Timeline | GetName, GetStartFrame, GetEndFrame, GetStartTimecode, GetSetting (without a name: the settings snapshot), GetTrackCount, GetItemListInTrack, GetMarkers, AddMarker, GetMarkerByCustomData, UpdateMarkerCustomData, SetCurrentTimecode, GetCurrentTimecode, GetCurrentVideoItem, Export |
| TimelineItem | GetName, GetStart, GetEnd, GetDuration, GetLeftOffset, GetRightOffset, GetSourceStartFrame, GetSourceEndFrame |

| gated: called only when the installed catalogue documents it for TimelineItem | source |
|---|---|
| TimelineItem.AddTransition | not in the 21.0.4 README; reported for 21.1+ by third parties. Option keys and values only as AddTransition's own catalogue entry and its Options type give them; `duration` only when that text says frames |
| TimelineItem.GetType | not in the 21.0.4 README; reported for 21.1+ by third parties |

How the catalogue gates these calls:

- **Where it is looked for:** in `RESOLVE_SCRIPT_API`, then the platform's default Scripting folder (`README.md`,
  `DaVinciResolveScript.pyi`, `README.txt`, `CHANGELOG.md`).
- **What counts as documented:**
  - in a `.pyi`, the `def` must sit inside `class TimelineItem`. The excerpt runs to the next `def` or `class` at the
    same or a lower indent;
  - in a text catalogue, the name must appear in the TimelineItem section, or be written `TimelineItem.Name`.
- **When it does not count:** a gated call the catalogue does not document is recorded as "not documented on this
  install: not called".
- **Looked up, never called:** `SetFades`, `GetFades`, `GetSourceStartTime` and `GetSourceEndTime` are looked up in the
  catalogue and recorded.

**Version note:** `GetStart(subframe_precision)`, `GetSourceStartFrame` and `GetSourceEndFrame` are in the 20.2 and
21.0.4 READMEs but not in the 18.5 README. On Resolve older than 20, the JSON and the report say so next to any read
that failed.

## Prerequisites

- **DaVinci Resolve Studio**, running. External scripting needs the Studio edition (certainly from 21.1).
- **Preferences > System > General > External scripting using = Local**, then restart Resolve.
- **Python 3.6 or later, 64-bit** (the README's minimum), on the machine running Resolve.
- matchcut, to make the bundle and read the result (not needed to run the script itself).
- **An empty project open** (for example the untitled project Resolve opens at start), with your own work saved.
- The source footage at the path the bundle names (the bundle's files and `expected.json` hold its absolute path). Make the
  bundle on the machine that runs Resolve, or put the footage at the same path there.

## Commands

Make the bundle (on the machine that runs Resolve: the command it prints suits that platform):

```shell
python -m matchcut.resolve probe WORKSPACE                 # stage final -> WORKSPACE/resolve_probe/
python -m matchcut.resolve probe WORKSPACE --stage roughcut --out DIR
python -m matchcut.resolve probe WORKSPACE --run           # also runs it, if Resolve's scripting module can be imported here
```

Run the probe with Resolve Studio open and an empty project as the open project. Try `--dry-run` first: it
connects, records the environment and the safety check, and creates nothing. The real run asks before it switches
projects; type `yes` (or pass `--yes`).

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

(The README says standard ISO Linux installs use `/home/resolve` instead of `/opt/resolve`; the script also looks
there.)

- **Finding the module:** the script also looks in these default folders by itself, and prints exactly these lines when
  it cannot find the module.
- **A module that fails to import:** when the module file is there but fails to import, the script prints `found <path>
  but it failed to import: <error>` and records it. This usually means a Python that does not match Resolve's, or a
  `RESOLVE_SCRIPT_LIB` that does not point at fusionscript. The setup lines then follow as "set these if Resolve is
  installed elsewhere".
- **`--out FILE`** writes the JSON elsewhere; the exports still go to the run's directory.

Read the result (offline, anywhere matchcut runs). Give it a run's directory or `resolve_probe.json`, or the bundle. Given
the bundle, it reports the newest run that is complete or recorded timelines. Without one, it reports the newest run that
is not a dry run (or the newest dry run), and it says which run it took and why. It writes `probe_report.md` in that
run's directory:

```shell
python -m matchcut.resolve report WORKSPACE WORKSPACE/resolve_probe
python -m matchcut.resolve report WORKSPACE WORKSPACE/resolve_probe/runs/<UTC time>/resolve_probe.json
```

Exit codes of the script:

| code | meaning |
|---|---|
| 0 | done (failed steps are measurements and are recorded) |
| 2 | the module was not found or failed to import, or Resolve did not answer |
| 3 | refused by the safety check (also when the open project changed while it waited for your answer) |
| 4 | the probe could not make its own project, or the open project changed while it ran (it stops before the next write) |
| 5 | not confirmed |
| 1 | an error of the probe itself (also recorded in the JSON) |

## What to send back

From the bundle directory: the run's folder **`runs/<UTC time>/`**, which holds `resolve_probe.json`, `exports/`, and
`probe_report.md` if you ran the report. It holds:

- paths on your machine (the footage, the bundle, the Resolve install);
- the project and database names Resolve reported;
- the counts of timelines, clips and folders of the project that was open when you started.

Nothing else about your projects is in it.

## Deleting the probe projects

The probe never deletes anything. In Resolve, open the Project Manager, open the folder **matchcut-probe**, right-click
each `matchcut-probe-<time>` project and choose Delete (open another project first: the README says `DeleteProject`
deletes a project only "if not currently loaded"). Once it is empty, delete the `matchcut-probe` folder the same way.
Deleting a project does not delete the media files.

## Verification so far (automated only)

`tests/resolve/` runs the generated script end to end against the fake, through a `DaVinciResolveScript` module the
test puts in `sys.modules`.

**The fake** holds a model:

- projects, folders and a media pool with folders;
- timelines with record and source frames, transitions, and markers with `customData`;
- a playhead;
- `Export` written from the model.

It rejects any call outside the 21.0.4 README's methods (parsed from the full text), plus the gated calls when its
catalogue documents them.

**Variants:**

- keeps everything, loses dissolves, shifts frames by 1, drops markers;
- AddTransition not documented, `AppendToTimeline` returns `[None]`, scripting not connected;
- refused: a user project with work, an untitled project with a timeline, with a pool clip, or with a clip in a nested bin;
- refused because emptiness cannot be confirmed: the timeline count cannot be read, the root folder cannot be read, bins
  past the depth cap, bins past the count cap;
- refused because emptiness cannot be confirmed: a bin whose clips or sub-folders cannot be read;
- refused after the yes: a project opened, or a clip added, while the prompt waits; a project opened just before
  `CreateProject`;
- allowed: an empty project with another name, empty nested bins;
- `CreateProject` does not switch, the open project changes between two calls of one part, or on the last export (no
  save);
- `GetSourceEndFrame` inclusive, `endFrame` inclusive;
- transition items in the track (centred, or starting at the cut), `GetMarkerByCustomData` without a frame;
- Resolve 18.5.

**What the tests check:**

- the safety guard, the confirmation, the project and folder names, and that no other project is touched or saved;
- the catalogue gating per class, the AddTransition option sources, and recorded failures;
- import errors, per-run directories, the JSON schema, and the report's verdicts, differences and recommendation;
- Python 3.6 syntax, and that the script imports nothing from matchcut.

None of this is evidence about Resolve.
