# M2a independent test report

An independent test of M2a (editorial interoperability) on branch `claude/m2a-editorial-interop-bkqqay` at
`fc91027`, 2026-10-19. The tester did not build or review M2a. No code, test or other document was changed;
this file is the only change. Scratch workspaces, scripts and outputs were kept outside the repository and are
written below as `<scratch>/...`. `WS` is a workspace.

The claims under test are in [INTEROP.md](INTEROP.md), [M2A-ACCEPTANCE.md](M2A-ACCEPTANCE.md) and the
[spec](superpowers/specs/2026-10-19-m2a-editorial-interop.md).

## Summary

- **Automated verification: pass.** `python -m pytest tests/interchange tests/pipeline tests/tui tests/review -q`:
  763 passed, 0 failed, 0 skipped (352 s). Run per directory: interchange 360, pipeline 173, tui 143, review 87,
  all passed. A second interchange run gave the same 360 passes. The optional OpenTimelineIO and `xmllint`
  checks ran. Neither of the two known load-flaky TUI tests failed.
- **CLI workflow: pass.** I built three fresh workspaces: 25 fps, 23.976 fps with music, and 29.97 fps
  drop-frame footage with a start timecode of 01:00:55;00. Every export exited 0, wrote 9 files and passed
  6 checks with 0 errors. The real OpenTimelineIO 0.18.1 loaded every `.otio` and the FCPXML 1.10 DTD accepted
  every `matchcut.fcpxml`. My frame arithmetic agreed with the EDL and FCPXML times.
- **Editor round trips: pass.** I wrote my own editor simulation and did not use `simulate_editor.py`. Across
  the three workspaces I made 84 edited or control files: OTIO, FCPXML and EDL, each with matchcut's metadata
  kept and stripped. Every import gave exactly the edits I made, and the unedited controls gave 0 decisions.
  Canonical files were byte-identical after every import.
- **Re-run and re-import, foreign files, damaged files, Resolve script, docs:** all pass, with the defects below.
  No input produced a traceback, and every refusal was one line on stderr with exit 1.
- **Defects: 4 found, none high.** One is medium: the OTIO reader accepts a zero or negative clip duration
  without a word and writes an invalid clip to `human.json`. Three are low.
- **Human acceptance of M2a: PENDING** (see the last section).

## Environment

| Item | Version |
|---|---|
| OS | Linux container (kernel 6.18), 4 CPUs, 15 GB RAM, no display |
| Python | 3.13.16 (pytest 8.4.2, textual 8.2.8) |
| ffmpeg / ffprobe | 6.1.1-3ubuntu5 |
| mpv | 0.37.0 |
| OpenTimelineIO | 0.18.1, in a throwaway virtualenv outside the repo |
| xmllint | libxml 20914, with Apple's FCPXMLv1_10.dtd (a scratch copy, not committed) |

Machine-specific limits:

- No DaVinci Resolve (free or Studio), Final Cut Pro or Premiere, and no macOS. No GUI.
- `OTIO_VENV_PYTHON` and `FCPXML_DTD` were not set. `tests/interchange/conftest.py` found the venv and the DTD
  at its fallback scratch location. The runs report 0 skipped, so the optional checks executed.
- `test_docs`' comparison of the committed examples ran and was not skipped. This machine's ffmpeg encodes the
  fixture footage with the shot boundaries the examples were made from.

## What was tested and how

### 1. Automated suites, fresh run

Each directory was run on its own, one after another, with `-p no:cacheprovider` so that nothing is written to
the repository. The plan's combined command was then run once as given.

| Suite | Result | pytest time | wall time |
|---|---|---|---|
| `tests/interchange` | 360 passed, 0 skipped | 25.44 s | 28.2 s |
| `tests/pipeline` | 173 passed | 106.75 s | 109.4 s |
| `tests/tui` | 143 passed (5 snapshots passed) | 175.24 s | 177.8 s |
| `tests/review` | 87 passed | 36.08 s | 38.3 s |
| `tests/interchange`, second run | 360 passed, 0 skipped | 25.83 s | 27.4 s |
| combined command | 763 passed | 352.15 s | 357 s |

- **Per-module counts** (`--collect-only`) match M2A-ACCEPTANCE.md: `test_cli` 13, `test_common` 61, `test_docs` 10,
  `test_edl` 37, `test_fcpxml` 57, `test_fidelity` 22, `test_otio` 54, `test_resolve` 30, `test_timecode` 9 and
  `test_workspace` 67.
- **Determinism:** both interchange runs gave the same counts and outcomes.
- **Load-flaky tests:** `test_pass_checkpoints_say_they_are_deterministic` and
  `test_run_with_checkpoints_advances_the_ladder` passed in both TUI runs. In the combined run the machine was
  also busy with my imports. Neither test was rerun, because neither failed.
- **Optional checks:** both ran.
  - `test_otio_documents_load_in_real_opentimelineio` used the OpenTimelineIO venv.
  - `test_fcpxml_is_dtd_valid` and `test_fcpxml_is_dtd_valid_and_reimports` used `xmllint` and the DTD.

### 2. CLI workflow on fresh workspaces

The footage came from `make_demo_footage` (`matchcut/pipeline/demo.py`), with `fps` passed as a `Fraction` for
the NTSC rates. The music is a `make_click_track` WAV at 120 bpm. For the timecoded clip, the 29.97 file was
remuxed with `ffmpeg -c copy -timecode 01:00:55;00`; ffprobe reads its tag as `01:00:55;00`. Commands, run from
`<scratch>`:

    python -m matchcut.pipeline.cli run src/a25.mp4   -w ws/a25   --backend mock -t 8  --no-render
    python -m matchcut.pipeline.cli run src/b2398.mp4 -w ws/b2398 --backend mock -t 10 --no-render --music src/music.wav --music-start 2
    python -m matchcut.pipeline.cli run src/c2997df.mp4 -w ws/c2997 --backend mock -t 9 --no-render
    python -m matchcut.pipeline.cli export ws/<name> --resolve-script

| Workspace | Rate (ffprobe) | Final cut | Rough cut | Export |
|---|---|---|---|---|
| a25 | 25/1 | 5 clips, 173 frames, dissolves 8f and 18f | 199 frames; snap moved 3 of 5 clips | exit 0, 9 files, 6 checks, 0 errors |
| b2398 (music) | 24000/1001 | 6 clips, 210 frames, dissolves 13f (0.532 s) and 8f (0.332 s) | 235 frames; snap moved 6 of 6 | exit 0, 9 files, 6 checks, 0 errors |
| c2997 (DF, TC 01:00:55;00) | 30000/1001 | 6 clips, 245 frames, dissolves 12f (0.4 s) and 9f (0.3 s) | 273 frames; snap moved 6 of 6, largest 0.98 frame | exit 0, 9 files, 6 checks, 0 errors |

- **Output and file lists:** the output lists the same files as INTEROP.md: `roughcut.otio`, `final.otio`,
  `matchcut.fcpxml`, `roughcut.edl`, `final.edl`, `open_in_resolve.py`, `provenance.json`, `fidelity.json` and
  `fidelity.md`. Each `exports/.stages/` held two snapshots.
- **Fidelity summary:** every workspace gave the same counts as the acceptance document's demonstration.
  - OTIO: 6 preserved, 5 approximated, 5 lost.
  - FCPXML: 6 preserved, 5 approximated, 6 lost.
  - EDL: 6 preserved, 6 approximated, 6 lost.
- **a25 matches the demonstration:** its output is line for line the demonstration in M2A-ACCEPTANCE.md,
  including the marker frames 50, 65, 114 and 155.
- **c2997 (drop-frame):** the fidelity report carries the DROP FRAME EDL entry instead of the NON-DROP one. Both
  EDLs say `FCM: DROP FRAME` and all 14 `tcFormat` attributes in the FCPXML are `DF`. b2398 has `NDF` and
  `FCM: NON-DROP FRAME`.

**Real OpenTimelineIO** loaded all 6 `.otio` files. Durations in frames, tracks and markers:

| File | Duration | Tracks | Markers | Transitions |
|---|---|---|---|---|
| a25 final / roughcut | 173 / 199 | V1, A1 | 4 | 2 / 0 |
| b2398 final / roughcut | 210 / 235 | V1, A1, A2 | 5 | 2 / 0 |
| c2997 final / roughcut | 245 / 273 | V1, A1 | 5 | 2 / 0 |

Other values OpenTimelineIO read:

- `global_start_time` is 0 in every file.
- `metadata.matchcut.rate` is `[25, 1]`, `[24000, 1001]` or `[30000, 1001]`.
- In c2997, every `available_range` starts at frame 109542, which is 01:00:55;00 in drop-frame.

`xmllint --noout --dtdvalid FCPXMLv1_10.dtd` exited 0 for all three `matchcut.fcpxml` files.

**Hand checks by frame arithmetic** against `final.json`:

- **c2997 EDL, event 003 (`D 012`).**
  - c003 starts at 6.64 s × 30000/1001 = source frame 199. The media starts at 01:00:55;00 = frame 109542.
    109542 + 199 = 109741, which is `01:01:01;21` in drop-frame, because `01:01:00;00` and `;01` do not exist.
    The EDL says `01:01:01;21`.
  - The record in is `00:00:02;16` = frame 76. c001 is 59 frames (0–1.9686 s). c002 is 29 frames (120–149).
    OTIO puts that cut at frame 82, and 82 − in_off 6 = 76.
  - The record out is `00:00:04;22` = 142 = 76 + 66. c003 runs from frame 199 to 265 (8.8422 s).
  - The 12-frame length is 0.4 s × 29.97 = 11.99, rounded to 12.
- **c2997 EDL, event 006.**
  - Source `01:01:12;05`–`01:01:13;09` is media frames 513–547, the same as 17.1171 s and 18.2516 s.
  - Record 211–245. The total of 245 frames matches the stage's 8.176 s × 29.97 = 245.03.
- **c2997 OTIO, c003.** The source start is 109747 = 109542 + 199 + in_off 6.
- **b2398 FCPXML, final project.**
  - c002 has `start="17017/4800s"` = 85 frames, which is 3.5452 s × 23.976.
  - c002 has `duration` 19 frames. The stage clip is 26 frames, less out_off 7.
  - The transition has `offset="1001/400s"` = frame 60 and a duration of 13 frames: 0.532 s × 23.976 = 12.76,
    rounded to 13. That puts the 6 frames before the cut on c003's handle and the 7 after on c002's.
  - c003 has `offset` 66 and `start` 159 = 153 + in_off 6. Its source in is 6.3814 s = 153.0 frames.
  - The sequence `duration="7007/800s"` = 210 frames = 47 + 26 + 59 + 21 + 47 + 31 − 13 − 8.
- **b2398 music.**
  - `music.start` is 2.0 s in the config and in the metadata.
  - The OTIO A2 clip starts at 48 frames @ 23.976 and the FCPXML lane −1 clip has `start="1001/500s"`. Both are
    2.002 s (defect D4).

### 3. Editor round trips

`<scratch>/scripts/editor.py` is my own script. It does not import matchcut and is not committed. It reads an
exported `<stage>.otio` into a model of what an editor sees: visible clip ranges and transitions centred on the
cut. It applies edits on that model and writes six files.

- **With matchcut metadata:** `keep.otio`, the exported JSON edited in place. `keep.fcpxml`, the exported XML
  with the other project removed, times and offsets recomputed and `md` keys kept. `keep.edl`, CMX3600 with
  matchcut's per-event comments and the fingerprint.
- **Stripped, foreign style:**
  - `foreign.otio` is Resolve-like. It has no matchcut metadata, `Resolve_OTIO` metadata, and clip names that
    are the file name. Record start is 01:00:00:00, or 01:00:00;00 for drop-frame. For the 25 fps workspace the
    rate base is 600.
  - `foreign.fcpxml` is FCP-like. Ids are renumbered (asset `r7`, effect `r4`) and the uid and library location
    are new. It has no `md` keys, markers or keywords, and `tcStart` is 01:00:00:00, with spine offsets from
    there. At 25 fps the times are in 1/2500 s units.
  - `foreign.edl` is Avid-like. The reel is the file stem in capitals, the track is `V` and the record start is
    01:00:00:00. It has `FROM CLIP NAME` and `SOURCE FILE` comments, no MATCHCUT comment, and its own
    drop-frame timecode for c2997.

Every generated `.otio` loaded in the real OpenTimelineIO, and every generated `.fcpxml` passed the DTD.

Edits on the final stage, and what `import WS FILE` reported for every one of the six files:

| Workspace / edit set | Edits made | Decisions reported (all six files) |
|---|---|---|
| a25 `none` (control) | none | 0 decisions |
| a25 `trim` | c001 out −5f, c005 in +3f | `trim c001 out -5f`, `trim c005 in +3f` |
| a25 `cut` | the 8f dissolve into c003 turned into a cut, cut point kept | `transition c002 c003 dissolve 8f -> cut` |
| a25 `del` | c005 deleted | `removed c005` |
| a25 `swap` | c001 and c005 swapped | `reorder c005 c002 c003 c004 c001` |
| a25 `add` | shot 4, media frames 250–275, appended | `added h001 (shot 4, 10.00-11.00s)` |
| a25 `dlen` | the 18f dissolve into c004 made 12f | `transition c003 c004 dissolve 18f -> dissolve 12f` |
| a25 `combo` | trim c001 out −5f and c005 in +3f, dissolve into c003 cut, c004 deleted with its dissolve, shot 4 clip added | the 5 matching decisions, nothing else |
| b2398 `none` | none | 0 decisions |
| b2398 `combo` | c001 out −5f, c004 in +2f, the 13f dissolve cut, the 8f dissolve made 10f, c006 deleted, shot 2 clip added | the 6 matching decisions |
| b2398 `swap` | c001 and c006 swapped | `reorder c006 c002 c003 c004 c005 c001` |
| c2997 `none` | none | 0 decisions |
| c2997 `combo` | c001 out −7f, c004 in +4f, the 12f dissolve made 6f, the 9f dissolve cut, c006 deleted, shot 5 clip added | the 6 matching decisions |
| c2997 `swap` | c001 and c006 swapped | `reorder c006 c002 c003 c004 c005 c001` |

That is 84 imports with exit 0 and exactly the expected decisions. Without metadata the report said
`N clips matched by source range because the clip key was missing or unknown` and that the file `carries no
stage fingerprint`. For c2997 it said `source start timecode of 109542 frames subtracted ...` for OTIO and
FCPXML. Further checks:

- **`.fcpxmld` bundle.** The c2997 `combo` foreign FCPXML was saved as `Matchcut Edit.fcpxmld/Info.fcpxml`, a
  name with a space. Imported as the bundle, with and without a trailing slash, it gave the same 6 decisions. A
  bundle with metadata from b2398 also worked.
- **Name fallback.** I stripped the metadata but kept matchcut's clip names (`c004 shot4`) and trimmed c004 in
  +2f. The import gave `trim c004 in +2f`, with all 6 clips counted as matched by source range (see
  observations).
- **Untouched dissolves.** After the b2398 swap, `human.json` keeps 0.532 s and 0.332 s, not the whole-frame
  13f and 8f.
- **Canonical files.** Before the first import and after the last, I took the sha256 of every file in each
  workspace except `human*.json`, `frames/` and `sheets/`. The list covers the stage and pass files, analysis
  files, `exports/` with `provenance.json`, `fidelity.*` and `.stages/`. a25 has 29 files, b2398 30 and c2997 29.
  All are identical. The only new file was the `__pycache__` that my own `py_compile` of `open_in_resolve.py`
  wrote.
- **`junctions`.** After the a25 `combo` import, `junctions ws/a25` lists `4 junctions in human (9 stages)`.
  `--index 2` shows the history `... final: in +0.02s (+1f) · human: out -0.16s (-4f), in +0.16s (+4f), cut`, as
  INTEROP.md describes for a dissolve turned into a cut.
- **TUI.** `<scratch>/scripts/tui_check.py` uses `MatchcutApp(cfg).run_test()` and presses `4`, as
  `tests/tui/test_edit.py` does. It loads the Edit view and walks every junction.
  - On a25 and on c2997 the stages are `[roughcut, matchcut, continuity, pacing, transitions, polish, beatsync,
    final, human]` and the current stage is `human`. Each panel's `why:` line is the human decision.
  - On a workspace whose `human_import.json` was deleted, the stages end at `final` and the header carries
    `human.json is stale: human_import.json is missing or unreadable (...)`.

### 4. Pipeline re-run, then re-import

On a25, after importing the `combo` OTIO, I re-ran with another target:

    python -m matchcut.pipeline.cli run src/a25.mp4 -w ws/a25 --backend mock -t 12 --no-render

`final.json` changed (sha256 `19df9dbf...` became `250c8fd4...`). `roughcut.json` did not change.
`human.json` and `human_import.json` were kept.

- **`junctions`** lists `4 junctions in final (8 stages)` and the note `human.json is stale: final.json changed
  since the import (re-export, or re-import the editor's file: matchcut diffs it against the stage it was exported
  from)`.
- **`export`**, on a copy taken before the re-import: `export WS --stage roughcut --format edl` printed the stale
  note naming `final.otio, matchcut.fcpxml (project final), final.edl`, `stale in provenance.json: final (...)` and
  `open_in_resolve.py was made for the old final.otio`, plus the human-stage stale note. `provenance.json` has
  `"stale": true` for `final`, and `fidelity.md` marks the three final rows **STALE**. A full export afterwards
  printed `open_in_resolve.py regenerated for the new final.otio`.
- **`import`** in the stale state printed the human stale note with `this import replaces it`.
- **Re-import of the old editor file.** It gave exactly the same 5 decisions as before the re-run, with the note
  `edited_a25.otio was exported from an earlier final: the editor's changes were diffed against that version;
  ...`. `human_import.json` records `base_snapshot` (`exports/.stages/final-19df9dbfea78.json`) and
  `base_snapshot_sha256` `19df9dbf...`. Its `stage_sha256` is the new `250c8fd4...`. `junctions` then lists the
  human stage again.
- **Snapshot missing**, on a copy: `edited_a25.otio was exported from an earlier final (sha 19df9dbfea78) and
  its snapshot exports/.stages/final-19df9dbfea78.json is missing: re-export and redo the edit, or restore the
  snapshot`, exit 1, one line.
- **Snapshot damaged** (a changed copy under the same name): the same one-line refusal and exit 1. The message
  says "is missing" although the file is there (D3).
- **Snapshot next to the file** (`elsewhere/.stages/` beside `elsewhere/edited_a25.otio`): found, and the same
  5 decisions.
- **No fingerprint** (`stage_sha256` removed from the OTIO): 7 decisions. They include the pipeline's own
  changes, `trim c002 in +30f, out +6f`, `trim c003 in -5f, out +1f` and `trim c005 in +24f`, and the dissolve
  reads `19f -> cut`. The note says `... carries no stage fingerprint: if the pipeline ran after it was exported,
  the pipeline's changes show as human decisions`. That is the documented behaviour.

### 5. Foreign and bad input

**Foreign files with things matchcut cannot map.** These are the edit `trim c001 out -5f` plus `dissolve into
c003 -> cut`, on c2997 (drop-frame, timecoded, record start 01:00:00;00) and on b2398. Both workspaces gave the
same picture. Every file gave the 2 real decisions, and the report listed each foreign item:

| File | Extra content | Report |
|---|---|---|
| Resolve-like OTIO | 2nd video track, gap, `GeneratorReference` (Solid Color), clip of other media, `MissingReference`, an editor marker | `video track Video 2 ignored`, `audio track Audio 1 ignored`, `gap Gap (10 frames) ignored: the following clips close up`, `clip Solid Color: generator/missing media ignored`, `clip broll.mov: other media ... ignored`, `clip offline.mxf: generator/missing media ignored` |
| FCP-like FCPXML | `<gap>`, a generator `<video>`, an asset-clip of other media, a lane 1 title, a lane −2 audio-only clip, a marker | `lane 1 video Basic Title ignored`, `lane -2 asset-clip broll audio ignored`, `gap Gap (10 frames) ignored`, `video Custom ignored (nested or unsupported item)`, `clip broll: other media ... ignored` |
| Avid-like EDL | `BL` event, another reel, an audio-only `A` event, an `M2` speed change on a source event | `event 007: black (BL) ignored`, `event 008: other media broll.mov ignored`, `event 009: audio event (track A) ignored`, `event 010: speed change (M2 ...) ignored, the source range is used as written` (that event becomes `added h001`) |

**Damaged or invalid input.** 52 `import` and 5 `export` invocations. None printed a
traceback, and every refusal was one line on stderr with exit 1.

| Input | Result |
|---|---|
| empty `.otio`, `.fcpxml` and `.edl`; truncated OTIO and FCPXML; non-JSON; binary bytes | `invalid OTIO document: ...`, `invalid FCPXML: no element found ...`, `invalid EDL: no events`, `invalid FCPXML: unclosed token ...` |
| JSON list; JSON without tracks; OTIO with no video track or no clips | `invalid OTIO document: not a JSON object`, `OTIO document has no video track`, `OTIO video track has no clips` |
| HTML as `.fcpxml`; FCPXML without a project; asset `ref` that does not exist | `not an FCPXML document (...)`, `FCPXML has no project with a sequence`, `no clip of the FCPXML spine references the source footage ...` |
| FCPXML clip with `start="abc s"`, `start="17017/0s"` or a negative duration (in the project that is read) | exit 0; the note `clip c002 shot1: no usable start, duration or offset, ignored`, then `removed c002` |
| FCPXML clip without `duration` | exit 0; the clip runs to the asset's end; noted as `outside the media` |
| EDL timecode `00:00:61:23`; source out before in; zero-length event with no dissolve; one short event line; comments only | `invalid EDL: line 4: ... is not a valid timecode`, `... the source out is before its in`, `event 001 is shorter than its transitions`, `invalid EDL: no events` |
| EDL with CRLF line ends; cp1252 title and comment | exit 0, 0 decisions |
| OTIO transition longer than its clip | `clip c002 is shorter than its transitions` |
| OTIO rate 0 or a string value | exit 0; the note `no usable source range, ignored`, then `removed c001` |
| OTIO `source_range` null | exit 0; noted; the media's available range used (OTIO's own rule) |
| OTIO clip outside the media | exit 0; noted `outside the media ... check the source start timecode` |
| OTIO `metadata.matchcut.rate` `[0, 1]`; unknown stage name; fingerprint `zz` | exit 0; each noted |
| OTIO clip with duration 0 or −20, no transition | **exit 0, nothing noted, invalid clip written** (D1) |
| `.aaf`, `--format aaf`, missing file, `.fcpxmld` with no `Info.fcpxml`, missing workspace, `--stage nosuch` | `cannot tell the format of thing.aaf ...`, `unknown format aaf: use otio, fcpxml or edl`, `cannot read ...: No such file or directory`, `cannot read ...: no Info.fcpxml inside`, `no workspace at ... (config.json missing)`, `no stage nosuch in WS` |
| source footage renamed (export and import) | `source footage not found: src/b2398.mp4` |
| `export --stage nosuch`, `--format aaf`, a missing workspace, `--format edl --resolve-script` | one line each, including `--resolve-script needs the otio format: Resolve imports the OTIO file` |
| `import` of `roughcut.otio` | warning: `human stage is based on roughcut, not final: ...` |

### 6. Resolve script

- **Compiles:** `python3 -m py_compile` succeeds for the `open_in_resolve.py` of all three workspaces.
- **Calls:** I listed every attribute call with an AST walk and read the script in full. The Resolve API calls
  are `scriptapp`, `GetProjectManager`, `GetCurrentProject`, `GetMediaPool`, `ImportTimelineFromFile`,
  `SetCurrentTimeline`, `OpenPage`, `AddMarker`, `UpdateMarkerCustomData`, `SetCurrentTimecode`,
  `GetTimelineCount`, `GetTimelineByIndex` and `GetName`. That is exactly the spec's list from the Resolve 21.0.4
  README, with no other API call and no 21.1 call such as `AddTransition` or `SetFades`. The other calls are
  Python's own (`sys.exit`, `os.path.expandvars`, `str` and `dict` methods). The script imports only `os`, `sys`
  and `DaVinciResolveScript`.
- **Arguments and return values:** the arguments follow the README's signatures, for example `ImportTimelineFromFile(path,
  {"timelineName", "importSourceClips"})`, `AddMarker(frame, "Green", name, note, 1, junction_id)` and
  `UpdateMarkerCustomData(frame, id)`. Each return value is checked.
- **c2997 values:** `JUNCTIONS` holds frames 59, 82, 142, 159 and 211, the OTIO marker frames. `NAVIGATE_TO` is
  `s1-s3`, the first junction with a transition. `START_TC` is `00:00:00:00`.
- **Smoke test against my own fake.** I wrote a minimal fake `DaVinciResolveScript` with only those calls; it is
  not the repo's fake. The script made the calls in the expected order, put 5 markers on those frames and set the
  playhead to `00:00:02:22` (frame 82 at 29.97). It exited 0. When the fake made `AddMarker` fail on two frames
  that already had markers, the script used `UpdateMarkerCustomData` on those two and still reported `5 of 5
  junctions carry their id`.
- **Not run in DaVinci Resolve.** Nothing here shows how a real Resolve behaves.

### 7. Docs check: 10 claims

| # | Claim (source) | How checked | Result |
|---|---|---|---|
| 1 | Both commands exit 1 with a one-line message on stderr for any failure, for example `no stage X in WS`, `unknown format aaf: ...` and `source footage not found: ...` (INTEROP, Usage) | section 5 | holds; all three messages seen verbatim |
| 2 | Every exported file records the sha256 of its stage file: OTIO `metadata.matchcut.stage_sha256`, FCPXML `com.matchcut.stage_sha256`, EDL `* MATCHCUT STAGE_SHA256` (INTEROP) | `sha256sum ws/c2997/final.json` compared with all three | holds; all equal `a8052650...` |
| 3 | `.stages/<stage>-<12 hex>.json` is a copy of the stage file as exported, named by the first 12 hex digits of its sha256 (INTEROP) | `cmp` of both snapshots with their stage files | holds; byte-identical |
| 4 | Drop-frame media gives a drop-frame EDL and FCPXML `tcFormat="DF"`; non-drop 29.97 and 23.976 count non-drop (INTEROP, Time model) | c2997 and b2398 files | holds |
| 5 | `--out DIR` keeps its snapshots in `DIR/.stages/` (INTEROP) | `export ws/c2997 --stage final --format edl --out <scratch>/elsewhere/outc` | holds; `final-a8052650201a.json` there |
| 6 | Re-import after a re-run diffs against the snapshot, prints the note and records `base_snapshot`; a missing snapshot is a one-line error; a file with no fingerprint says so (INTEROP, ACCEPTANCE) | section 4 | holds (the damaged-snapshot message is D3) |
| 7 | An EDL cannot carry a dip to black without the `TRANSITION` comment: an untouched cut gives `human changed transition dip_to_black Nf -> dissolve Nf` from that EDL and nothing from OTIO or FCPXML (INTEROP) | b2398 copy with one `dip_to_black` in `final.json`, exported, metadata stripped from all three, each imported | holds; EDL `dip_to_black 8f -> dissolve 8f`, OTIO and FCPXML 0 decisions; the FCPXML with "Dip To Black" is DTD-valid |
| 8 | An untouched dissolve keeps matchcut's own value, not the whole-frame length (INTEROP) | b2398 swap import | holds; 0.532 s and 0.332 s kept |
| 9 | Importing a file from an earlier stage warns `human stage is based on roughcut, not final: ...` (INTEROP) | `import ws/c2997 exports/roughcut.otio` | holds, verbatim |
| 10 | A missing or unreadable `human_import.json` counts as stale: left out of the stage list, reported, never deleted (INTEROP) | deleted it, then replaced it with garbage; `junctions`; TUI | holds; `human.json` kept |

Further claims seen holding during the tests:

- Record start 0 in all three formats.
- The exact rational rate in `metadata.matchcut.rate`.
- `open_in_resolve.py` is regenerated by any export of the final OTIO.
- The `.fcpxmld` bundle is read through `Info.fcpxml`.
- Canonical files and `provenance.json` are only read.
- "Falls back to the clip's name (`... shot<N>`), then to its source range" holds only partly; see the
  observations.

## Defects found

**D1 (medium): the OTIO reader accepts a clip with zero or negative duration and writes an invalid clip to
`human.json` without a note.**

Reproduce, in any workspace, on a clip with cuts on both sides:

    python3 -c "import json; d=json.load(open('WS/exports/final.otio')); c=d['tracks']['children'][0]['children'][0]; c['source_range']['duration']['value']=-20; json.dump(d, open('negdur.otio','w'))"
    python -m matchcut.pipeline.cli import WS negdur.otio
    python -m matchcut.pipeline.cli junctions WS

Actual:

- Exit 0, with `trim c001  human trimmed c001: out -46f` and no import-report note.
- `human.json` holds c001 with `source_in` 0.0 and `source_out` −0.8342. With duration 0 it holds 0.0 and 0.0,
  and still reports `out -46f`; the true change is −47f.
- `junctions` lists junction 1 with `out -0.83` and the why `made adjacent by human`.

Expected: a one-line error with exit 1, or the clip listed as unusable in the import report, as the other readers
do for the same damage:

- FCPXML, negative duration: `clip c002 shot1: no usable start, duration or offset, ignored`.
- EDL, zero-length event: `invalid EDL: event 001 is shorter than its transitions`.
- OTIO, a clip touched by a transition: `clip c003 is shorter than its transitions`.

Only a damaged or hand-made file triggers it, but the result is an invalid human stage that the other commands
and the TUI then read.

**D2 (low): a junction made adjacent by a removal does not list the human trim of its incoming clip.**

Reproduce on a 25 fps workspace like a25: in `final.otio`, delete c004 together with the dissolve into it, and
move c005's source start 3 frames later, shortening it by 3. Then `import WS FILE` and
`junctions WS --index 3`.

Actual:

- The import prints `removed c004` and `trim c005 in +3f`, and `human.json`'s history has both.
- Junction 3 (`c003->c005`) shows `why: made adjacent by human: removed c004 ...`, history
  `human: cut at out 8.64s / in 17.88s` and `decisions: (none)`. The TUI panel shows the same.

Expected: the `human:trim` decision of c005 is listed at that junction, as it is at `c004->c005` when c004 is
kept. The cut times themselves are right.

**D3 (low): a damaged snapshot is reported as missing.**

Reproduce: change the contents of `WS/exports/.stages/final-<hex>.json` but keep its name. Re-run the pipeline
so that `final.json` changes, then import the editor file exported before.

Actual: `... its snapshot exports/.stages/final-<hex>.json is missing: re-export and redo the edit, or restore
the snapshot`, while the file exists. The refusal itself is right; INTEROP.md says a copy that does not hash to
the fingerprint is not used.

Expected: the message says the snapshot does not match the fingerprint.

**D4 (low): the music start is snapped to the video frame grid, and the fidelity report does not say so.**

Reproduce: export a 23.976 workspace with `--music-start 2`.

Actual: the OTIO A2 clip starts at 48 frames @ 23.976 and the FCPXML music clip has `start="1001/500s"`. Both
are 2.002 s. `metadata.matchcut.start` and the `md` key keep 2.0. `fidelity.md` lists the start as carried and
nothing under approximated. The error can be up to half a frame, about 21 ms at 23.976. Beat-synced cuts are
placed against the exact start.

Expected: the rounding is listed under approximated, or the music clip's start is written at audio resolution.

### Observations (not counted as defects)

- **Name fallback (INTEROP, Ids and provenance).** With metadata stripped, a clip name `... shot<N>` only
  supplies the shot index. Identity is always matched by overlapping source range within that shot
  (`_match` in `workspace.py`). The report counts every such clip as `matched by source range` and never says
  "by name". The behaviour is reasonable, but the sentence reads as if names were a separate matching step.
- **Leading transition.** An OTIO transition placed first on the track is dropped with the note
  `transition next to an ignored item dropped`, although nothing next to it was ignored.
- **Clips outside the media.** These are noted (`... outside the media ...; check the source start timecode`),
  but the import still exits 0 and writes the clip into `human.json`. This is documented ("clips outside the media
  reported").
- **FCPXML version.** A file that says `version="1.13"` is read without a note.
- **`human_import.json` paths.** `base_snapshot` is stored as typed on the command line, relative to the working
  directory (`ws/a25/exports/.stages/...`). `source_file` is absolute.
- **Resolve script and drop-frame.** For drop-frame media `START_TC` is written as `00:00:00:00`, with colons, so
  the script computes the playhead timecode as non-drop. If Resolve reads it against a drop-frame timeline, a
  junction after the first minute would be 2 frames per minute off. The marker frame ids are not affected. This
  is unverified and belongs to the human gate.
- **Outside M2a: relative footage paths.** A workspace made with `run src/x.mp4 -w ws/x` stores the relative
  footage path, so `export` and `import` work only from the same working directory. From another directory they
  give `source footage not found: src/b2398.mp4`. The exported files themselves carry the absolute path.
- **Outside M2a: raw H.264.** `run` fails on a raw H.264 stream (`could not extract frame at 0.150s`), so the
  frame-rate note (r_frame_rate 50 against avg_frame_rate 25) could not be exercised end to end. ffprobe does
  report 50/1 and 25/1 for such a stream. The unit test covers the note.

## Limits of this testing

- Synthetic footage only, 18.5 s and 320x180, and the mock backend only, so the why lines are mock text. No
  real camera media, long timelines, mixed rates, or footage with more than one source.
- The editor files were written by my script to look like Resolve, FCP and Avid output, from what those formats
  allow. They are not output of those applications, so real-world quirks are not covered. Examples are Resolve's
  own OTIO metadata layout, FCP's audio-sample time bases and Avid's reel-name rules.
- One footage file per workspace. The 50, 59.94 and 60 fps rates and media starting near or past 24:00:00:00
  were not tested.
- Stale-logic edge cases beyond those listed in section 4 were not explored, such as several narrower exports
  in a row or a stage file restored by hand.
- The TUI was driven headless through Textual's pilot. No real terminal was used and nothing was looked at.
- The Resolve script was compiled, read and run against two fakes: the repo's in the suite, and mine. It was not
  run in DaVinci Resolve.

## Human acceptance

Automated verification and human acceptance are separate. Human acceptance of M2a stays **PENDING**: nothing was
opened in DaVinci Resolve or Final Cut Pro, and the Resolve script was never run in Resolve. M1's and M2b's human
gates also remain **PENDING**.
