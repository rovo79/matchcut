# M2c-0: the DaVinci Resolve Studio capability probe

**Status: run once against a live Resolve: DaVinci Resolve 21.0.2.4 free, macOS, from the Py3 console (2026-10-29).** That is one machine and one footage file; see "First live run" below. Studio, other versions,
real camera footage, timecoded media and audio are not verified. Everything else rests on a fake of the documented
scripting API (`tests/resolve/fake_resolve.py`). The fake is matchcut's model of the documentation and not evidence about
Resolve, apart from the variant "resolve 21.0.2 free (observed)", which reproduces that run's read-back.

- **M2c human gate: PENDING** for the bridge itself (M2c-1). The probe has answered its questions once. More runs
  (Studio, other versions, camera footage) are welcome: send back the run's folder `runs/<UTC time>/` (its
  `resolve_probe.json` and `exports/`; see below).
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
| 0 | done (steps that returned nothing or failed are recorded; the report sorts them into outcomes) |
| 2 | the module was not found or failed to import, or Resolve did not answer |
| 3 | refused by the safety check (also when the open project changed while it waited for your answer) |
| 4 | the probe could not make its own project, or the open project changed while it ran (it stops before the next write) |
| 5 | not confirmed |
| 1 | an error of the probe itself (also recorded in the JSON) |

## Free edition: run it from Resolve's console

**Observed on one machine, not a guarantee:** on DaVinci Resolve 21.0.2.4 (free, macOS), external scripting from Terminal did
not connect: the module loaded, but `scriptapp("Resolve")` returned None. In Resolve's own console, Lua
`print(resolve:GetVersionString())` printed `21.0.2.4`, so in-app scripting works there. The probe can therefore run inside
Resolve's console with the same safety rules. It is handed Resolve's `resolve` object instead of connecting from outside,
and records `scripting.mode = "in-app console"`.

1. **Install Python 3 from python.org.** Use <https://www.python.org/downloads/macos/>: the "macOS 64-bit universal2
   installer". Resolve's Py3 console looks for a python.org framework install, so Homebrew or a venv is not enough (the
   console said "Python 3 was not found" with only those). The README asks for Python 3.6 or later, 64-bit.
   - **Which version:** 3.10, 3.11 or 3.12 are the likeliest to work, and 3.11 is the safe choice. This is UNVERIFIED for
     Resolve 21.0.2. A third-party report says Resolve before 21 failed with 3.12 for external scripting.
   - **If the Py3 tab still says Python 3 was not found,** try 3.11.
   - **The probe itself** needs only Python 3.6 syntax and the standard library.
2. **Restart Resolve**, open **Workspace > Console**, and select **Py3**.
3. **Check:** `print(resolve.GetVersionString())`. It should print the version. (UNVERIFIED: that the Py3 console defines
   `resolve` as the Lua console does. If it says `resolve` is not defined, send that back.)
4. **Have an empty project open**: no timeline, no clip.
   - First paste the **dry-run line** that `python -m matchcut.resolve probe WORKSPACE` printed. It creates nothing.
   - Then paste the **real-run line** ("this switches to a new probe project"). Paste each line whole, without leading
     spaces.
   - Each prints `matchcut probe exit <code>` (the codes below).

   The lines look like this, with your bundle's path:

   ```python
   _mc_p = '/Users/you/…/resolve_probe/resolve_probe.py'; _mc_g = {"__file__": _mc_p, "__name__": "resolve_probe_console"}; exec(compile(open(_mc_p, encoding="utf-8").read(), _mc_p, "exec"), _mc_g); print("matchcut probe exit", _mc_g["console"](resolve, ['--dry-run']))
   ```

   - **No question is asked:** the console has no input, so the real-run line passes `--yes`. Without it the probe
     refuses (exit 5) and never asks.
   - **Every guard still runs:** the empty-project check, the check again right before `CreateProject`, the check
     before every write, and saving only its own project.
   - **Nothing escapes into Resolve:** a SystemExit is turned into the printed exit code.
5. **Send back** the `runs/<UTC time>/` folder from the bundle, as for the Terminal run.

Studio-only calls may return False or None on the free edition. The report's Outcomes section sorts them, and the report says
"free edition" when `GetProductName()` does not contain "Studio".

## First live run (2026-10-29)

**What was run:**

- DaVinci Resolve **21.0.2.4, free edition**, macOS.
- The probe ran from Resolve's own **Py3 console** (in-app mode). From Terminal the module loaded, but
  `scriptapp("Resolve")` returned None: external scripting did not connect on this free install.
- The installed catalogue was a README.txt, "Last Updated: 26 May 2026", without `GetType` or `AddTransition`.
- Status complete, on matchcut's synthetic demo cut: 5 clips, 2 dissolves (8 and 18 frames), 25 fps, 462 frames of media.

**What came back, in the report's five categories.** Every finding and every recorded call is in exactly one of them.
The rules are one table in `matchcut/resolve/outcomes.py`.

- **Passed:**
  - **Imports (OTIO, FCPXML 1.10, EDL): frame-exact, with the dissolves.** All three gave the same video track 1, and
    every clip's record start, duration and source in/out matched `expected.json`.
  - **Dissolves:** each came back as an item of its own, named "Cross Dissolve", with no source frames. It has the
    dissolve's exact length and is centred on the cut, as matchcut writes it: 61-69 across the cut at 65, 105-123 across
    114. With no `GetType` on this install, the report identifies these items by having no source frames and spanning an
    expected cut (the name is recorded, not relied on).
  - **Build:** `AppendToTimeline` placed every clip exactly.
  - **Exports:** OTIO and EDL `Timeline.Export` worked for every timeline, and read back through M2a with no clip or frame
    differences.
  - **Markers:** the probe's own `AddMarker` (junction id as `customData`) was found by id 4 of 4, on build and
    import-otio.
  - **Navigation:** the playhead landed on the incoming clip 4 of 4 on every route.
  - **Conventions it measured:**
    - `GetSourceEndFrame` is exclusive (start + duration), except for the clip that ends at the media's end, where it
      reports the media's last frame (461 for 462 frames);
    - imported timelines start at frame 0 / 00:00:00:00, timelines made with `CreateEmptyTimeline` at 90000 /
      01:00:00:00;
    - `GetMarkerByCustomData` returns the marker's information without its frame.
  - **The negative control:** the 4 `GetMarkerByCustomData` lookups on build, before the probe added its own markers,
    returned None. A built timeline carries no file markers, so there was nothing to find.
- **Failed:** FCPXML 1.10 `Timeline.Export` on all 5 routes. It returned True, but no file was at the path given. The
  reason is not reported by Resolve; it may be edition- or version-specific (unverified). The probe now records which
  files with that route's name are in the exports folder, to see where it went.
- **Unsupported:** 32 reads returned None on transition items: `GetLeftOffset`, `GetRightOffset`,
  `GetSourceStartFrame` and `GetSourceEndFrame`, 8 each. Those are 2 transition items on video track 1 of each import,
  plus 2 on import-otio's audio track: matchcut's OTIO puts the same transitions on A1. These reads do not apply to a
  transition. On clips they returned numbers.
- **Not preserved by Resolve:** Resolve imported no markers from the OTIO: the read-back shows none, and its 4 junction
  ids were not found by `GetMarkerByCustomData` (4 null lookups). M2c-1 adds its own markers by id after the import.
- **Not tested:**
  - `TimelineItem.AddTransition` and `TimelineItem.GetType`: not documented on this install (README.txt "Last Updated: 26
    May 2026"), so never called. The build routes' dissolves therefore stay untested.
  - The MCP server: never started; no `ResolveMCP` binary file.
  - The FCPXML exports: not re-read, since no file came back.
  - The file markers of the FCPXML and the EDL, 8 null lookups, 4 each. FCPXML markers have no customData field
    (matchcut writes the junction id as the marker's value), and a CMX3600 EDL carries no markers, only comments. Neither
    could be found by id, by design.

The raw steps stay in `resolve_probe.json` unchanged, and the report's appendix "Raw steps" lists each of the 48 null
steps exactly as observed.

**Not verified by this run:**

- Studio, and Resolve versions other than 21.0.2.4.
- Windows and Linux.
- Real camera footage, timecoded or drop-frame media, other frame rates.
- Audio (only read back, not compared).
- FCPXML export by other means, and whether the free edition's in-app result holds for external scripting on Studio.

The fake variant "resolve 21.0.2 free (observed)" reproduces this read-back, and `tests/resolve/test_live_run.py` holds
the report to these verdicts.

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
