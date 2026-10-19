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
`source footage not found: ...`). Example outputs are in
[examples/m2a/synthetic/](examples/m2a/synthetic/README.md).

| File (in `WORKSPACE/exports/`) | What |
|---|---|
| `<stage>.otio` | OpenTimelineIO JSON, one per stage: the primary format and the one with the most metadata |
| `matchcut.fcpxml` | FCPXML 1.10, one `project` per exported stage in one event |
| `<stage>.edl` | CMX3600 EDL, one video track |
| `provenance.json` | matchcut's own record of every exported stage by stable id (never only in the interchange files) |
| `fidelity.json`, `fidelity.md` | what each export keeps, approximates and loses, per stage and format |
| `open_in_resolve.py` | only with `--resolve-script` |
| `.stages/<stage>-<12 hex>.json` | a copy of the stage file as exported (named by the first 12 hex digits of its sha256), so that an editor's file made from it can be imported after the pipeline has run again; written atomically, never deleted by matchcut |

Every exported file also records the sha256 of the stage file it was made from (its fingerprint): OTIO `metadata.matchcut.stage_sha256`,
the FCPXML sequence's `com.matchcut.stage_sha256` md key, and an EDL comment `* MATCHCUT STAGE_SHA256 <hex>`. A narrower export or `--out DIR` keeps its
snapshots in `DIR/.stages/`.

The `final.edl` that `run` writes in the workspace root is the older delivery EDL
(`Timeline.to_edl`, written by `pipeline.py` at the end of the run: source times from frame 0, no media timecode, no ids). For an editor
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
| Music file, start, gain | track `A2` + metadata; the start is placed on the video frame grid (see below) | connected clip on lane -1 with a volume adjustment; the start is placed on the video frame grid | not carried |
| Ducking, loudness normalisation | lost | lost | lost |
| Junction id (`s<out>-s<in>`) | marker metadata (the marker's name is `J<n> s<out>->s<in>`) | marker value (`s0-s1 (s0 -> s1)`), keyword `mc:<id>`, `md` key on the dissolve | `* MATCHCUT JUNCTION` comment |
| Why line | marker metadata (full) and marker `comment` (cut to 240 characters) | marker `note`, cut to 240 characters | not carried |
| History, pin state | marker metadata | not carried | not carried |
| Scores, per-pass states, decisions | `provenance.json` only | `provenance.json` only | `provenance.json` only |
| Clip key (lineage id) and clip id | clip metadata | `md` keys | `* MATCHCUT CLIP` and `ID` comments |
| Record start | 0 (`global_start_time`) | 0 (`tcStart`) | 00:00:00:00 |
| Source times | the media's own start timecode + frame | asset `start` = the media's start timecode | the media's start timecode + frame |

The complete lists of what each format approximates and loses, verbatim from
`matchcut/interchange/fidelity.py` (`KNOWN`, the table every export and every
report uses; the report adds what that export measured: clips moved by the frame
snap, dissolve lengths rounded, and for OTIO and FCPXML a music start that the frame grid moved). Entries marked UNVERIFIED have not been checked in an editor.

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

For media whose own start timecode is drop-frame, the EDL's NON-DROP entry is replaced by this one (the file says `FCM: DROP FRAME`):

- timecode is DROP FRAME, as the media's own start timecode is: the frame counts are exact and the timecode follows the wall clock; UNVERIFIED that Resolve and Final Cut Pro read the ';' separators of this EDL as drop-frame

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
  FCPXML `tcFormat="DF"`; the report's EDL row then carries the DROP FRAME entry below instead of the NON-DROP one. An EDL for non-drop 29.97 or 23.976
  counts non-drop and says so.
- **The frame rate the exports use.** The exports take the footage's `r_frame_rate` (the container's base rate), the pipeline's engine reads
  `avg_frame_rate`. They are the same for ordinary footage; for some streams (a raw H.264 stream: 50 and 25) they are not. When they differ the export prints one
  note naming both (`frame rate: ffprobe reports 50 fps (r_frame_rate) but the pipeline cut at 25 fps (avg_frame_rate): ...`); the files are written at the first.

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
  exported files are still in the directory: it is kept and flagged `"stale": true` in `provenance.json` (the stage as a whole)
  and in `fidelity.json` (per format; `fidelity.md` marks the row **STALE**), and the export prints one note naming the files whose record is stale, per JSON file
  (`stale in fidelity.json: roughcut.otio, matchcut.fcpxml (project roughcut), roughcut.edl; stale in provenance.json: roughcut (...)`). A fidelity entry is
  stale per format: if you export that stage again in one format only, `provenance.json` describes the new export, that format's entry is current, and
  the note says `provenance.json now describes the new export of roughcut` while the other formats' entries stay flagged. Export that stage again to
  replace the rest; if the stage file goes back to what was exported, the flag clears.
  A stale entry never fails an export and does not count in `fidelity.json`'s `ok`.
- If an editor drops every id, import falls back to the clip's name (`... shot<N>`, which gives the shot only), then to its
  source range against the shot bounds (`shots.json`), and says how many clips needed that.
- **Ambiguous clips are never guessed.** A clip that cannot be matched to exactly one canonical clip, or whose key points at other footage, is read as a **new clip** (`h001`, ...) with a one-line
  note naming the candidates, so the stage shows `removed` and `added` decisions instead of a trim that might be wrong:
  - a **clip key used by more than one clip of the file** (a copied clip keeps its key): none of them is taken for the original
    (`clip key 2@4.000-6.000 of c003 is used by clips 3 and 4 of the file: read as new clips (h001, h002), not matched to c003`). Their `origin` in
    `human.json` is `human:<id>`, not the canonical key, so they do not inherit anything;
  - a **clip with no key to decide that overlaps two canonical clips equally** (the same number of frames, one shot):
    `clip 2 of the file (shot 4, 9.40-10.20s) overlaps c002 and c003 equally: read as a new clip (h001), not matched to any of them`. A clip that overlaps one
    canonical clip more than the others still goes to it;
  - a **canonical clip whose best candidates tie** (a clip razored into two unkeyed halves of equal length; a candidate that fits another canonical clip better is
    left out of the tie, so a roll edit between two pieces is two trims): neither half is taken for it, both are new clips
    (`c003's range is shared equally by clips 3 and 4 of the file: read as new clips (h001, h002), not matched to c003`) and the canonical clip reads as removed.
    The new clips' `origin` is `human:<id>`;
  - a **known key on the footage of another shot**: a key is trusted only while the edited range still overlaps the shot of the canonical clip (`shots.json`). If
    the clip now lies in another shot, matchcut reads it as a new clip (its shot is the one its range is in) and the canonical clip as removed, unless another clip maps to it
    unambiguously: `clip key 3@6.520-9.000 of c003 now covers shot 7 (12.20-13.00s), not shot 3: read as a new clip (h001), not matched to c003`. An extreme slip that stays
    inside the same shot is still a trim of that clip. Without `shots.json` the key is trusted, and when its range no longer overlaps the clip's the import says so:
    `could not check that clip key 3@6.500-9.000 of c003 is still shot 3 (no shots.json): its range 12.20-13.00s no longer overlaps the clip's`.
  A clip whose key is unique and known (and still in its shot), or whose overlap has a clear winner, keeps its canonical id, key and history.

## The fidelity report

Every export is validated before it counts: matchcut reads each file it wrote back
with its own reader and compares the result with the stage (it is the frame-snapped
copy that was exported): clip count and order, shots, source in/out (exact), transition kind and
frames, clip keys, record duration (within half a frame plus a millisecond, the precision
matchcut stores times to), and **where the clips sit on the record side**, which is what an editor places them by: the EDL's record
timecodes against the layout (a gap or an overlap the reader notes is an error), the FCPXML spine's clip and transition offsets and the
sequence's duration and start, the OTIO video track and each audio track that mirrors the picture (clip durations and total length). A difference nothing explains fails the export with a one-line
error and exit 1, after `fidelity.json` and `fidelity.md` have been written. Otherwise the
report lists per stage and format what was **preserved**, what is **approximated** (with the
measured counts and largest deltas: clips moved by the snap, dissolve lengths rounded to whole
frames, dips to black in the cut) and what is **lost**.

**The music start is on the video frame grid.** The OTIO `A2` clip and the FCPXML lane -1 clip start at a whole video frame, as the cut does, so a
`music.start` that is not a whole number of frames moves, by at most half a frame (about 21 ms at 23.976 fps). When it moves, the OTIO and FCPXML
entries list it under **approximated** with the size of the move, for example `music start moved to the video frame grid: 2 s written as 2.002 s
(frame 48 at 23.976 fps), +2.0 ms (0.05 frame)`; the exact value stays in the metadata (`metadata.matchcut.start`, the `md` key). Nothing is listed when
the start is on the grid, and the EDL carries no music.

## Importing an editor's file

`import WORKSPACE FILE` reads an OTIO, FCPXML or EDL file (the format from the extension,
or `--format`; a Final Cut Pro `.fcpxmld` bundle, which is a directory, is read through the `Info.fcpxml` inside it). The stage it was exported from is read from the file (OTIO timeline metadata
or name, FCPXML project, EDL title) or given with `--stage`, else `final`. An FCPXML with exactly one project is read whatever the project is called (an editor may
rename it, and drop the metadata that names the stage); `--stage` or detection then only chooses the stage it is compared with, and a note says so (`the file's only project is named My Edit; compared with final`).
Projects are selected by name only in a file with several. Clips are matched
back to matchcut's clips by clip key, then by name, then by source range; arbitrary rational time bases and
renumbered ids are accepted. Anything it cannot map (other media, gaps, extra video tracks,
audio-only items, speed changes, effects, nested items) is listed in the **import report**
and ignored; nothing is dropped silently. A clip with **no length** (zero or negative duration) is damage, and is read the same way by OTIO and FCPXML:
listed (`clip <name>: source range duration is -20 frames (not positive), ignored` in OTIO, `no usable start, duration or offset, ignored` in FCPXML) and skipped,
so `human.json` never holds a clip whose source out is not after its in; the clip then reads as `removed`. An EDL event shorter than its transitions stops the
import with one line (`invalid EDL: event 001 is shorter than its transitions`).

- **Which file is the workspace's footage** (export and import alike): the one the stage timelines were made from, `probe.json`'s path, when that is a file. If
  `config.json` (the Config view edits `source`) now names a different existing file, the stages still describe the probed one, so it is used and a note says so:
  `config.json names <X>, but this workspace was analysed from <Y>: exported/imported against <Y>; re-run with --force (or force in Config) to analyse <X>` (printed by `export` and `import`,
  and kept in `human_import.json`). When the probed file is gone (the footage moved), the configured file is used only if it is the same footage by `probe.json`'s
  record, with a note listing the fields compared. The configured file is probed with the pipeline's own `media.probe` (the function that wrote `probe.json`), so the fields are compared like for like:
  the average frame rate (within 1e-3, so variable-frame-rate footage matches itself), the container's duration (within one frame, so footage with longer audio matches itself), width and height;
  a field `probe.json` does not record is skipped and not claimed; otherwise the command stops with one line naming both files and what differs.
  Without a `probe.json` the configured file is the footage.
- **The source footage** is the workspace's: a clip is the footage when its media path is the footage's exact path (symbolic links resolved); only when no
  clip has the exact path are clips with the same file name taken for it, and the report says so (camera names such as `C0001.MP4` repeat across cards, so
  a name never outvotes an exact path). A clip whose media is a generator, an offline or missing reference, or none (OTIO) is not footage: it is listed
  (`generator/missing media ignored`) and skipped, as are clips of other media. If no clip is the footage the import stops with `no clip of the OTIO video
  track references the source footage <path>`.
- **Times are the media's.** Source times and durations are converted at the footage's own frame rate, whatever the file's time base is: an OTIO on a 48000 base,
  25 fps media in a 24 fps FCPXML sequence (or an OTIO timeline of another rate). When the file's sequence or timeline rate differs from the media's the report says so
  (`the sequence runs at 24 fps and the media at 25 fps: times were converted at the media's rate`); an untouched file still gives no decisions.
- **Dip to black.** matchcut's own metadata says it. Without metadata, FCPXML reads a transition (or its effect) called a dip or Fade To Color, and so does OTIO
  (a `Custom_Transition` whose name does); an **EDL cannot say it: a dip written without matchcut's `TRANSITION` comment reads as a dissolve**, so the same untouched cut
  gives a `human changed transition dip_to_black Nf -> dissolve Nf` decision from an EDL that lost its comments, and none from the other two formats.
- **Which version of the stage.** The pipeline may have run again between the export and the editor's file coming back. Every export carries the fingerprint of the stage
  file it was made from (see above); the import compares it with the stage file now:
  - equal: the editor's file is diffed against the stage as it is;
  - different, and the snapshot of that version is there: the file is diffed against **the snapshot**, so the decisions are the editor's changes only (not the pipeline's), with the
    note `<file> was exported from an earlier <stage>: the editor's changes were diffed against that version; junction histories compare the human stage with the current <stage>`.
    `human_import.json` records `base_snapshot` and `base_snapshot_sha256`; its `stage_sha256` stays the current stage file's, so the human stage is not stale at once;
  - different, and the snapshot is missing: the import stops with one line naming the places it looked (`<file> was exported from an earlier <stage> (sha <12 hex>) and its snapshot <stage>-<12 hex>.json is missing (searched exports/.stages and <dir of the file>/.stages): re-export and redo the edit, or restore the snapshot`);
  - different, and the snapshot is there but **damaged** (its content does not hash to the fingerprint, or it is not readable JSON): its own one line, not the missing-snapshot one (`<file> was exported from an earlier <stage> (sha <12 hex>) and its snapshot exports/.stages/<stage>-<12 hex>.json is damaged (sha256 mismatch): re-export and redo the edit`; `(unreadable JSON)` in the second case);
  - no fingerprint (an application dropped the metadata, or the file is from an older matchcut or another tool): the file is read against the current stage, and the note says
    `<file> carries no stage fingerprint: if the pipeline ran after it was exported, the pipeline's changes show as human decisions`.

  The snapshot is looked up in `WORKSPACE/exports/.stages/`, then in `.stages/` next to the imported file (an export made with `--out DIR` keeps it in `DIR/.stages/`; save the editor's file there or copy the folder into `WORKSPACE/exports/`). A copy
  that does not hash to the fingerprint is not used; a good copy in the second place is found even if the first holds a damaged one.

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
- **Stale human stage.** `human_import.json` records the stage file it was compared with (its sha256 as it is now, also when the file was diffed against a snapshot) and the latest stage whose
  history it copies, each with a sha256. If either file changes (the pipeline ran again), `human.json` is
  **stale**: it is left out of the stage list (so it never becomes the latest stage and pins are judged
  against the real one), reported in one line by `junctions`, `export` and `import`, and **never
  deleted**. The line says what to do: re-export, or re-import the editor's file; matchcut diffs it against the stage it was exported from (the snapshot), so the
  re-import records the editor's changes only. A file with no fingerprint, or one whose snapshot is gone, cannot be checked that way (see above).
  A missing or unreadable `human_import.json` counts as stale.
- **Inherited decisions belong to the same clips, in the same order.** A junction of the human stage is followed through the earlier stages (history, pass
  decisions, the pass's "why") only when both its clips are instances the canonical stages have (the same `clip_origin` as the canonical clip: the
  clip key) **and** a canonical stage has those two instances adjacent in that order: the earlier stages are located by clip instance first (lineage only when no such pair is adjacent, as after a merge, and then only if each clip it finds is
  the wanted instance or one that neither the human cut nor the latest canonical stage has: a piece the pipeline absorbed, not one the editor deleted), so a clip slipped off its old range inside its shot keeps its junction's history. A junction with a clip the
  editor added, replaced or copied, with a new pairing, or with a pair in the reverse order shows only the `human` decisions and
  `made adjacent by human: ...` (`reordered c003, c002` for a reorder), never a pass's verdict about another pairing. An untouched junction keeps its inherited "why". When a removal made two clips
  adjacent, the human trim of either clip is listed under that junction's decisions (the "why" stays the removal).
- **Pins on the human stage.** The human history repeats the canonical stage's `editor` decision whatever the editor then did, so it never decides whether a
  pin still holds. The engine (`editorial.pin_outcome` / `pin_change`, no interchange code) judges the editor's cut: a pin counts as applied on `human`
  only if the cut still has the two clips the pin names, the same instances (the pin's `out_origin` / `in_origin`; for a pin without them, clips of the same two shots),
  next to each other, with both pinned cut points unchanged to within half a frame. Otherwise the human stage reports the pin as **changed by the editor**
  with a one-line reason: `clip removed (shot 5)`, `cut trimmed by 3 frames (out)`, `clips are no longer adjacent`, or `other footage (shot 2 is a different clip now)`.
  If the pair was razored or copied (a `human:` clip covers the pinned clip's range) the reason is `clip split or copied (shot N)`.
  **Every stored pin is reported once** on the human stage (the tag, the why and the badge are derived from the stored pins too, so a cleared pin leaves no `pin-changed` tag and a pin set again at new points reads pending), by one engine answer (`editorial.pin_states`, which calls `pin_outcome`): the `junctions` command ends with a
  line per pin (`pin s0->s1: applied`, `pin s0->s1: changed by the editor: clip removed (shot 1)`, `... not placed: <reason>`, `... pending (run again to apply)`), the Edit view's header
  lists the changed ones with their reasons (`pin s0->s1 changed by the editor: clip removed (shot 1)`), and the junction where the pin now sits (its pair, else the junction after its
  outgoing clip, else the one before its incoming clip) carries the tag `pin-changed` and a `pin: changed by the editor: <reason>` detail line in `junctions` and the badge
  `pin changed by the editor: <reason>` in the Edit view, instead of `pinned`. The junction's why then leads with `pin changed by the editor: <reason>` instead of the inherited `editor: locked by the editor: ...` text (that decision stays in the history, which is history); where the pin is applied the why is unchanged. Two pins that sit at one junction show one tag and one reason there; the footer and the header list both. The locks the pin put on the human stage's clips are cleared in `human.json`, so nothing reads them as matchcut's. A
  pin the editor did not touch still reads applied, and the canonical stages are judged by their own history as before. The cost of a wrong call: a pin the editor reproduced exactly by other means reads changed (still true).
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
   `Timeline.UpdateMarkerCustomData`, `SetCurrentTimecode`, `GetTimelineCount`, `GetTimelineByIndex`, `GetName`. It imports
   `final.otio` (else the last exported stage's), adds a marker per junction at the cut frame with the same name as the OTIO marker and
   the why line as its note, and parks the playhead on the first junction with a transition. It does not add
   transitions or fades, and sets no metadata. It needs **Resolve Studio** running with
   Preferences > System > General > External scripting using = **Local** (external scripting is Studio-only, certainly from 21.1). **matchcut
   never runs it**: you do, and it has not been run against a real Resolve (only against a fake of
   the documented API in `tests/interchange/test_resolve.py`).
   What the script cannot know is written at its top: the timeline's start timecode and the media conform
   (above). Its `START_TC` is the record start matchcut writes (00:00:00:00); marker frame ids are record
   frames from the timeline start and the playhead timecode is `START_TC` plus the junction's frame, worked out **when the script runs**. If Resolve starts the
   timeline elsewhere (its default is often 01:00:00:00), set the timeline's start timecode to `START_TC` in Timeline
   Settings, or edit `START_TC` at the top of the script to the timeline's start timecode and run it again: the playhead follows it. The script checks what Resolve returns. `AddMarker` is False when a marker already
   sits on that frame (Resolve may bring matchcut's own OTIO markers along): the script then gives that marker the junction id with
   `UpdateMarkerCustomData`, prints how many junctions carry their id and exits non-zero if any does not. If `SetCurrentTimecode` fails it
   prints the timecode it tried (`START_TC` plus the junction's frame), says to edit `START_TC` and run again, and exits non-zero. If the import returns nothing and a timeline of that name already exists, it says
   `timeline X already existed: markers added to it; delete it and re-run to import the new export` and uses that timeline. `TIMELINE_FILE` is the absolute path of the OTIO file at export time: edit it if you move the directory.
   `open_in_resolve.py` is derived output: any export that writes the OTIO of the stage the script targets regenerates it (flag or not) and says so; if
   that OTIO was not exported again and the stage file changed, the stale note names the script too.
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
has no scripting API; the markers (value `s0-s1 (s0 -> s1)`, the why line as the note) and the keywords `mc:<id>` are
the navigation aids. After editing, export XML again and `import` it: FCP renumbers resource ids and changes time
bases, which the reader accepts. Since Final Cut Pro 10.6 the default export is a `.fcpxmld` bundle (a directory): give `import` the bundle itself,
it reads the `Info.fcpxml` inside.

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
