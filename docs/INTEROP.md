# Editorial interoperability

`matchcut.interchange` writes any stage of a cut as a timeline file a video editor
can open, and reads an editor's re-export back as a new stage. It never rewrites
matchcut's own stage files, and it says exactly what each format keeps, approximates
and loses. It is standard library only (`ffprobe` for the footage), and it does not
need an editor, a licence or an MCP server. Design:
[superpowers/specs/2026-10-19-m2a-editorial-interop.md](superpowers/specs/2026-10-19-m2a-editorial-interop.md).
What was verified, and what is still waiting for a human with an editor:
[M2A-ACCEPTANCE.md](M2A-ACCEPTANCE.md).

**Read this first.** Every statement below that an editor "opens", "keeps" or
"shows" something is either checked by matchcut's own readers (the files are valid,
internally consistent and read back to the same cut) or marked UNVERIFIED. Nothing has been opened in
DaVinci Resolve, Final Cut Pro or Premiere yet; that is the human gate in
M2A-ACCEPTANCE.md and it is pending. No format here is a complete copy of a matchcut cut.

## Usage

```shell
python -m matchcut.pipeline.cli export WORKSPACE                       # roughcut and final, all formats -> WORKSPACE/exports/
python -m matchcut.pipeline.cli export WORKSPACE --stage final --format otio --format fcpxml --out DIR
python -m matchcut.pipeline.cli export WORKSPACE --resolve-script      # also open_in_resolve.py (Resolve Studio, see below)
python -m matchcut.pipeline.cli import WORKSPACE FILE                  # FILE: the .otio, .fcpxml or .edl an editor saved
python -m matchcut.pipeline.cli import WORKSPACE FILE --format edl --stage roughcut
python -m matchcut.pipeline.cli junctions WORKSPACE                    # the human stage is listed after final
```

`--stage` is repeatable and names any canonical stage (`roughcut`, a pass such as
`polish`, `final`); `--format` is `otio`, `fcpxml` or `edl`. Both commands print one
line per file or decision and exit 1 with a one-line message on stderr for any
failure (`no stage X in WS`, `unknown format aaf: use otio, fcpxml or edl`,
`source footage not found: ...`). A sample run is in
[examples/m2a/synthetic/](examples/m2a/synthetic/README.md).

| File (in `WORKSPACE/exports/`) | What |
|---|---|
| `<stage>.otio` | OpenTimelineIO JSON, one per stage: the primary format and the one with the most metadata |
| `matchcut.fcpxml` | FCPXML 1.10, one `project` per exported stage in one event |
| `<stage>.edl` | CMX3600 EDL, one video track |
| `provenance.json` | matchcut's own record of every exported stage by stable id (never only in the interchange files) |
| `fidelity.json`, `fidelity.md` | what each export keeps, approximates and loses, per stage and format |
| `open_in_resolve.py` | only with `--resolve-script` |

The `final.edl` that `run` writes in the workspace root is the older delivery EDL
(`render.py`: source times from frame 0, no media timecode, no ids). For an editor
use `exports/final.edl`.

## What each format carries

"Preserved" means matchcut re-read its own file and the result matched the stage (frame
for frame) at export time; it says nothing about what an editor does with the file.

| | OpenTimelineIO | FCPXML 1.10 | CMX3600 EDL |
|---|---|---|---|
| Clip order, the shot of each clip | preserved | preserved | preserved |
| Source in/out, exact frames at the source's exact rate | preserved | preserved | preserved |
| Frame rate | exact rational in `metadata.matchcut.rate` (the OTIO rate field is only a float) | exact `frameDuration` | `RATE` comment; timecode counts on the nominal base |
| Hard cuts | preserved | preserved | preserved |
| Dissolves | preserved in whole frames (see "Dissolves") | preserved in whole frames | preserved in whole frames (`D nnn` event) |
| Dip to black | approximated: `Custom_Transition` + metadata, UNVERIFIED | approximated: "Fade To Color", UNVERIFIED | approximated: a dissolve plus a `TRANSITION` comment |
| Fade in/out of the whole cut | lost as an effect; seconds kept in timeline metadata | lost as an effect; seconds kept in `md` keys | lost as an effect; a comment |
| Source audio | follows the picture (track `A1` mirrors the clips) | the asset's own audio | `AA/V` events |
| Music file, start, gain | track `A2` + metadata | connected clip on lane -1 with a volume adjustment | not carried |
| Ducking, loudness normalisation | lost | lost | lost |
| Junction id (`s<out>-s<in>`) | marker name and metadata | marker value, keyword `mc:<id>`, `md` key | `* MATCHCUT JUNCTION` comment |
| Why line | marker metadata (full) and marker `comment` (cut to 240 characters) | marker `note`, cut to 240 characters | not carried |
| History, pin state | marker metadata | not carried | not carried |
| Scores, per-pass states, decisions | `provenance.json` only | `provenance.json` only | `provenance.json` only |
| Clip key (lineage id) and clip id | clip metadata | `md` keys | `* MATCHCUT CLIP` and `ID` comments |
| Record start | 0 (`global_start_time`) | 0 (`tcStart`) | 00:00:00:00 |
| Source times | the media's own start timecode + frame | asset `start` = the media's start timecode | the media's start timecode + frame |

The complete lists of what each format approximates and loses, verbatim from
`matchcut/interchange/fidelity.py` (`KNOWN`, the table every export and every
report uses; the report adds what that export measured: clips moved by the frame
snap, dissolve lengths rounded). Entries marked UNVERIFIED have not been checked in an editor.

**OpenTimelineIO, approximated**

- dissolve lengths are whole frames and a cut's record time is a whole number of frames (measured below)
- source times are written in the media's embedded start timecode (frame 0 = that timecode, 0 when the file has none); UNVERIFIED that Resolve and Final Cut Pro conform timecoded camera media from it (human gate)
- dip to black: a Custom_Transition with matchcut metadata (OTIO has no such transition); UNVERIFIED how an editor treats it

**OpenTimelineIO, lost**

- ducking: the render ducks the music under speech, no format carries that automation
- loudness normalisation: applied by the render only
- fade in/out of the whole cut: timeline metadata only, not rendered by the receiving editor
- music mix: the music file, its start and gain are carried (track A2 and metadata), the render's mix is not
- UNVERIFIED: whether a third-party editor keeps the matchcut metadata (clips, markers, timeline) when it saves the file again

**FCPXML 1.10, approximated**

- dissolve lengths are whole frames and a cut's record time is a whole number of frames (measured below)
- source times are written in the media's embedded start timecode (frame 0 = that timecode, 0 when the file has none); UNVERIFIED that Resolve and Final Cut Pro conform timecoded camera media from it (human gate)
- dip to black: the Fade To Color transition, its uid taken from secondary sources; UNVERIFIED in Final Cut Pro

**FCPXML 1.10, lost**

- ducking: the render ducks the music under speech, no format carries that automation
- loudness normalisation: applied by the render only
- fade in/out of the whole cut: metadata only, not rendered by the receiving editor
- music mix: the music file (lane -1), its start and gain are carried, the render's mix is not
- UNVERIFIED: whether the matchcut metadata (md keys, keywords, marker values) survives a round trip through a third-party application
- UNVERIFIED: that srcEnable="video" mutes the source audio under music.only in every application

**CMX3600 EDL, approximated**

- dissolve lengths are whole frames and a cut's record time is a whole number of frames (measured below)
- source times are written in the media's embedded start timecode (frame 0 = that timecode, 0 when the file has none); UNVERIFIED that Resolve and Final Cut Pro conform timecoded camera media from it (human gate)
- dip to black: written as a dissolve plus a MATCHCUT TRANSITION comment; an editor shows a plain dissolve
- timecode is NON-DROP: at 23.976 and 29.97 fps the frame counts are exact but the timecode runs slow against the wall clock

**CMX3600 EDL, lost**

- ducking: the render ducks the music under speech, no format carries that automation
- loudness normalisation: applied by the render only
- music: an EDL has no second track, so the music file is not carried
- metadata other than comments: only the MATCHCUT comments (clip key, junction id, shot, stage, fades) are written; editors may drop them
- fade in/out of the whole cut: a comment only, not rendered by the receiving editor
- tracks: one video track only; the source audio rides on the video events (AA/V; V when music.only drops it)

## Time model

- **Exact rates.** The footage's frame rate is read from `ffprobe` as an exact
  fraction (`r_frame_rate`, with common NTSC rates recognised: 24000/1001,
  30000/1001, ...). No file is written with a rounded float rate.
- **Frame-accurate ranges.** Every clip's source in/out is a whole number of frames.
  Stages saved before the final snap (the rough cut, the passes) are snapped to the frame
  grid on export, with the shot bounds; every export reports how many clips moved and
  the largest move (never more than about a frame). Nothing in the workspace is changed by that.
- **Source start timecode.** Camera media often starts at 01:00:00:00 and an editor
  conforms it by that timecode, so every exporter writes source times as the media's
  embedded start timecode plus the frame (OTIO `available_range`, FCPXML asset
  `start`, EDL source columns); 0 when the file has none. The importers subtract it
  again. Whether Resolve and Final Cut Pro conform the media from it is UNVERIFIED.
- **Record start.** Every exporter starts the record side at zero: OTIO
  `global_start_time` 0, FCPXML sequence `tcStart="0s"`, EDL record column
  00:00:00:00. This is not the editors' habit (Resolve's default start timecode is
  often 01:00:00:00), so the start timecode an editor gives the imported timeline is
  UNVERIFIED; the Resolve script below states it and defaults to the value matchcut writes.
- **Dissolves.** matchcut's dissolve overlaps the last `d` of the outgoing clip with
  the first `d` of the incoming one (record length = sum minus `d`). OTIO and FCPXML centre
  a transition on the cut. The conversion is exact with `d` in whole frames (running
  rounding error is carried forward, so the total stays within half a frame of matchcut's
  duration): the outgoing clip's visible range ends `d - d//2` frames early, the
  incoming one starts `d//2` late. Everything a transition shows is inside matchcut's own
  clip ranges, so no extra media is needed. The EDL's `D nnn` event is the same geometry.
- **Drop-frame.** Media with a drop-frame start timecode gets a drop-frame EDL and
  FCPXML `tcFormat="DF"`. An EDL for non-drop 29.97 or 23.976 counts non-drop and says so.

## Ids and provenance

Interchange files carry ids; the decisions live in `provenance.json`.

- **Junction id** `s<out shot>-s<in shot>` (a repeated pair within a stage gets `#2`,
  `#3`). **Clip key** `<shot>@<in>-<out>` (seconds, three decimals, as in `0@0.000-2.000`), the lineage key of the clip's rough-cut
  instance (`timeline.clip_origin`), and the clip's own id (`c003`).
- `provenance.json`: per exported stage, per junction id: the why line, history,
  decisions (with the pass that made them), per-pass states, scores and pin state; per clip key: the clip's
  ranges and ids; the stage's timeline settings and history; and `canonical_sha256`, the hash of the stage file
  the entry was made from.
- A narrower export (`--stage final --format edl`) rewrites only what it exports.
  The earlier records of other stages stay while their stage file is unchanged (and, for
  FCPXML, while `matchcut.fcpxml` still has that stage's project). If a stage file
  **changed** since (a re-run of the pipeline), the old record is not dropped while its
  exported files are still in the directory: it is kept and flagged `"stale": true` in both `provenance.json`
  and `fidelity.json` (`fidelity.md` marks the row **STALE**), and the export prints one note naming the files whose record is stale
  (`roughcut.otio, matchcut.fcpxml (project roughcut), roughcut.edl`). Export that stage again to
  replace it; if the stage file goes back to what was exported, the flag clears.
  A stale entry never fails an export and does not count in `fidelity.json`'s `ok`.
- If an editor drops every id, import falls back to the clip's name (`... shot<N>`), then to its
  source range against the shot bounds (`shots.json`), and says how many clips needed that.

## The fidelity report

Every export is validated before it counts: matchcut reads each file it wrote back
with its own reader and compares the result with the stage (it is the frame-snapped
copy that was exported): clip count and order, shots, source in/out (exact), transition kind and
frames, clip keys, record duration (within half a frame plus a millisecond, the precision
matchcut stores times to). A difference nothing explains fails the export with a one-line
error and exit 1, after `fidelity.json` and `fidelity.md` have been written. Otherwise the
report lists per stage and format what was **preserved**, what is **approximated** (with the
measured counts and largest deltas: clips moved by the snap, dissolve lengths rounded to whole
frames, dips to black in the cut) and what is **lost**.

## Importing an editor's file

`import WORKSPACE FILE` reads an OTIO, FCPXML or EDL file (the format from the extension,
or `--format`). The stage it was exported from is read from the file (OTIO timeline metadata
or name, FCPXML project, EDL title) or given with `--stage`, else `final`. Clips are matched
back to matchcut's clips by clip key, then by name, then by source range; arbitrary rational time bases and
renumbered ids are accepted. Anything it cannot map (other media, gaps, extra video tracks,
audio-only items, speed changes, effects, nested items) is listed in the **import report**
and ignored; nothing is dropped silently.

The result is the **human stage**: `human.json` (a stage timeline, listed after `final`
in `junctions`, the TUI's Edit view and the review package) and `human_import.json` (what was read and
the hashes it was compared with). Its history is the latest canonical stage's history plus one `human`
decision per change:

| decision | meaning |
|---|---|
| `trim` | the visible in or out of a clip moved by N frames (transition trims are not counted twice) |
| `transition` | the transition into a clip changed (`dissolve 18f -> cut`, or a new length) |
| `removed` | a matchcut clip is not in the file |
| `added` | a clip with no counterpart (new ids `h001`, ...) |
| `reorder` | the matched clips are in another order |

A dissolve turned into a cut keeps the cut point, so in the human stage the junction's out and in times move by the halves of the
dissolve (`human: out -0.16s (-4f), in +0.16s (+4f), cut` for an 8-frame dissolve); the `transition` decision is the cause and the trims are
measured on what the editor shows, so they are not reported twice.

The canonical stage files and `provenance.json` are only read. Rules worth knowing:

- **Base stage.** The recommended base is `final`. Importing a file exported from an earlier
  stage works but warns in one line (`human stage is based on roughcut, not final: junction
  histories compare it with final, so changes the later stages made show as differences`), because the human
  history continues the latest stage's history (so pins and the attribution of every earlier
  decision stay valid).
- **Stale human stage.** `human_import.json` records the stage file it was compared with and the latest stage whose
  history it copies, each with a sha256. If either file changes (the pipeline ran again), `human.json` is
  **stale**: it is left out of the stage list (so it never becomes the latest stage and pins are judged
  against the real one), reported in one line by `junctions`, `export` and `import`, and **never
  deleted**. Importing the editor's file again replaces it. A missing or unreadable `human_import.json` counts as stale.
- **Untouched dissolves.** A dissolve the editor did not change keeps matchcut's own value (for
  example 0.75 s), not the whole-frame length the file carried, so the junction shows no `human`
  line for it. The same holds when a clip is deleted: the untouched fractional dissolves
  after it keep matchcut's values, and `human.json`'s own frame layout of such a dissolve
  can differ by one frame from the editor's file. That is within the "dissolve frame rounding" the
  report lists, and not a change by the editor.
- **Round trip, not authority.** The editor's file is a proposal read back as decisions; the next
  pipeline run does not use it. Applying human decisions to a new cut is not implemented.

## DaVinci Resolve

Three routes, from least to most requirement. All of them are UNVERIFIED until the human
checklist in M2A-ACCEPTANCE.md is done.

1. **Resolve (free or Studio), GUI import.** File > Import > Timeline, then `final.otio` or
   `matchcut.fcpxml`. Resolve 18 and later import OTIO in the free edition according to the OpenTimelineIO
   project; FCPXML 1.10 is the highest version Resolve's own scripted export writes (1.12 files were rejected by Resolve 18 and 19 per
   third-party reports), which is why matchcut writes 1.10. To check: that dissolves become transitions, that the
   markers and their notes are there, what the start timecode is, that the timecoded media conforms.
   Do not rely on matchcut's metadata surviving a save.
2. **Resolve Studio, generated script.** `export --resolve-script` writes `open_in_resolve.py` next to the exports.
   It uses only scripting calls documented in the Resolve 21.0.4 README: `scriptapp`,
   `GetProjectManager`, `GetCurrentProject`, `GetMediaPool`, `ImportTimelineFromFile`,
   `SetCurrentTimeline`, `OpenPage`, `Timeline.AddMarker` (customData = the junction id),
   `SetCurrentTimecode`, `GetTimelineCount`, `GetTimelineByIndex`, `GetName`. It imports
   `final.otio` (else the last exported stage's), adds a marker per junction at the cut frame with the same name as the OTIO marker and
   the why line as its note, and parks the playhead on the first junction with a transition. It does not add
   transitions or fades, and sets no metadata. It needs **Resolve Studio** running with
   Preferences > System > General > External scripting using = **Local** (external scripting is Studio-only, certainly from 21.1). **matchcut
   never runs it**: you do, and it has not been run against a real Resolve (only against a fake of
   the documented API in `tests/interchange/test_resolve.py`).
   What the script cannot know is written at its top: the timeline's start timecode and the media conform
   (above). Its `START_TC` is the record start matchcut writes (00:00:00:00); marker frame ids are record
   frames from the timeline start and the playhead timecode is `START_TC` plus the junction's frame. If Resolve starts the
   timeline elsewhere (its default is often 01:00:00:00), set the timeline's start timecode to `START_TC` in Timeline
   Settings or edit `START_TC` and `NAVIGATE_TC`. `AddMarker` returns False when a marker already sits on
   that frame (the OTIO import may have brought one); that is harmless. `TIMELINE_FILE` is the absolute path of the OTIO file at export time: edit it if you move the directory.
3. **Resolve's native MCP server (21.1) is not used.** It has about 14 tools, mostly `run_script`
   around the same scripting API (third-party measurements; Blackmagic's own pages could not be read), and no
   verified frame-accurate editing tools. Anything it could do for matchcut a documented script does,
   deterministically and without an AI client in the loop. Community MCP servers are third-party wrappers of
   the same API and are not evaluated.

The 21.1 calls `AddTransition` and `SetFades` are deliberately not used: their option keys are
undocumented in anything that could be read. A later milestone (an opt-in Resolve adapter) would build on this
after someone with Studio has run the live checklist.

## Final Cut Pro

Import `matchcut.fcpxml` (File > Import > XML). The file is valid against Apple's FCPXML 1.10
DTD (`xmllint --dtdvalid`, an optional automated check). UNVERIFIED in the application: that FCP opens it without
warnings, that the dissolves are Cross Dissolves (the effect id is taken from real exports, secondary evidence), whether the
markers, keywords and `md` keys survive, and a dip to black ("Fade To Color" id from secondary sources). FCP
has no scripting API; the markers (`J<n>`/junction id and the why line as the note) and the keywords `mc:<id>` are
the navigation aids. After editing, export XML again and `import` it: FCP renumbers resource ids and changes time
bases, which the reader accepts.

## Optional external checks

Automated tests that need an outside tool skip without it: a Python with OpenTimelineIO (`OTIO_VENV_PYTHON`) loads
the OTIO files and compares durations, transitions, markers and metadata; `xmllint` with Apple's 1.10 DTD
(`FCPXML_DTD`, not committed) validates the FCPXML. Neither is a dependency of matchcut.

## Limits

- Not verified in any editor (see above); the matrix says what matchcut checked itself.
- One video track; no nested sequences, speed changes, effects, gaps or multiple sources (a second footage file is reported and ignored on import).
- The importers read the picture. An audio track that is not matchcut's own mirror (its clips carry no matchcut metadata) is listed in the import report and ignored; edits made to audio alone are not read.
- No format carries ducking, loudness normalisation or the fades as effects.
- Python 3.9 syntax; tested on 3.13.
