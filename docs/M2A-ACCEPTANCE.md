# M2a acceptance: can a cut go to an editor and come back?

M2a adds editorial interoperability. `python -m matchcut.pipeline.cli export WORKSPACE`
writes any stage of a cut as OpenTimelineIO, FCPXML 1.10 and a CMX3600 EDL, with a
`provenance.json` that keeps the decisions behind the ids the files carry and a
fidelity report that says what each format keeps, approximates and loses.
`import WORKSPACE FILE` reads an editor's re-export back as a `human` stage that never
overwrites the canonical timeline. `export --resolve-script` also writes a script for
DaVinci Resolve Studio that matchcut itself never runs. How it works and what each
format carries: [INTEROP.md](INTEROP.md). Design:
[superpowers/specs/2026-10-19-m2a-editorial-interop.md](superpowers/specs/2026-10-19-m2a-editorial-interop.md).
It promotes the feasibility proof in [M2-INTEGRATION-FEASIBILITY.md](M2-INTEGRATION-FEASIBILITY.md).

## Status

- **Automated verification: pass** (see below). It proves the files are internally
  consistent and that matchcut reads back what it wrote; it does not prove that any editor
  opens them as intended.
- **M2a human acceptance: PENDING.** Nobody has opened `final.otio` or `matchcut.fcpxml`
  in DaVinci Resolve (free or Studio) or Final Cut Pro, run `open_in_resolve.py`, or
  re-exported from an editor and imported it. Everything under "Demonstration" is matchcut
  reading its own files and a scripted simulation of an editor.
- **M2b human gate: PENDING** ([M2B-ACCEPTANCE.md](M2B-ACCEPTANCE.md)).
- **M1 human gate: PENDING** ([M1-ACCEPTANCE.md](M1-ACCEPTANCE.md)).

## Automated verification

Offline and deterministic: synthetic footage (`make_demo_footage`, ffmpeg) and the mock
backend, no network, no editor. The modules, all under `tests/interchange/`:

| Module | What it proves |
|---|---|
| `test_common.py` | Exact frame rates (ffprobe rationals, a float-only probe snapped to the nearest common rate, NTSC), half-away rounding to frames, FCPXML rationals, the frame snap of a stage with a note per clip it moved (none for `final`), stable junction and clip ids with repeats made unique, the dissolve conversion (odd lengths, error diffusion, a clip swallowed by its transitions), real stages keeping the record length within half a frame |
| `test_otio.py` | OTIO export (tracks, `Transition.1`, markers with metadata, mirrored `A1`, music track `A2` clamped like the renderer, `music.only`, the dip to black label) and import (round trip of every stage, clip keys, float rates, stripped metadata, source-range matching, ignored items listed, a clip with zero or negative duration ignored with a note, foreign documents and malformed input as one-line errors, media start timecode, the workspace's exact source path first, b-roll or a generator, offline or media-less clip on the video track never read as footage, an OTIO on a 48000 base or a timeline rate other than the media's read at the media's rate); `provenance.json` keyed by stable ids; the real OpenTimelineIO loads the files (optional) |
| `test_fcpxml.py` | FCPXML structure and metadata, the dissolve geometry, dip to black as Fade To Color, NTSC, junction markers, keywords and 240-character notes, a music lane, DTD validity (optional), re-import without metadata, arbitrary rationals, rewritten ids and time bases, several projects, the declared source, b-roll and audio-only clips, split edits, a clip of the same file name from another folder never taking the exact path's place, 25 fps media in a 24 fps sequence, malformed input |
| `test_edl.py` | CMX3600 events and dissolve pairs on the frame grid, columns and comments, round trips of the synthetic cut and every workspace stage at several rates, 23.976/25/29.97 timecode and drop-frame, a foreign EDL mapped by source range, other media (the exact path before the same file name), wipes, speed changes, reel and clip names, overlapping events, malformed input |
| `test_timecode.py` | The media's start timecode (25 fps non-drop, 29.97 drop-frame) written by all three exporters and subtracted by all three importers, the drop-frame EDL, clips outside the media reported, junction markers on a timecoded file |
| `test_fidelity.py` | The music start moved by the video frame grid, listed under approximated with the size of the move (OTIO and FCPXML; nothing when it is on the grid); the validation gate: it re-reads each export and fails on a dropped clip, a moved edge beyond what snapping explains, a changed transition, key or duration; `KNOWN` entries and measured approximations in every report; the record-placement check passing every legitimate random stage at six rates (the sweep behind the gate); the report files; stale entries (`"stale": true`) round trip, are marked and do not decide `ok` |
| `test_workspace.py` | `export_workspace` (every file and report, subsets, `--out`, a failing validation writing the reports before raising, music and `music.only`, snap notes); records of a narrower export kept while their stage file holds, **kept flagged stale with a note naming the files** once it changed, cleared by a new export or a restored stage, dropped when their files are gone, stale per format when a stage is exported again in one format only; `open_in_resolve.py` regenerated whenever the OTIO it targets is exported again (flag or not) and named in the stale note when it is not; `simulate_editor.py` run on a workspace with a music track; `import_timeline` (exactly the expected human decisions for an edited OTIO, FCPXML and EDL, edited by hand in their text too; removed, added and reordered clips; clip-key and range matching, a clip key carried onto another shot's footage read as a new clip (a slip inside its own shot still a trim), a clip key used twice and a clip over two canonical clips equally read as new clips with a note naming the candidates (OTIO and EDL), a clear winner still matched; other media; stage detection; one-line errors); the human stage (listed last, shown in junction histories, pins and badges unchanged for an untouched pin, a pin whose clip was deleted, whose cut was trimmed by 3 frames or whose pair was separated reading **changed by the editor** in `pin_outcome`, the Edit badge and the `junctions` CLI, with and without clip instances in the pin, a human trim listed under the junction a removal made adjacent, no pass verdict borrowed by a replaced, copied or re-paired junction, history continuing the latest stage, untouched dissolves keeping matchcut's value, a stale human stage left out and reported, never deleted, a base other than `final` warned); canonical files and `provenance.json` byte-identical after an import; **the stage fingerprint and snapshot** (every export records the stage file's sha256 and keeps a copy; a re-import after a pipeline re-run gives exactly the editor's decisions in all three formats, with the note and `base_snapshot` in `human_import.json`; a missing snapshot is a one-line error naming where it looked, a damaged one (hash mismatch, unreadable JSON) its own one-line error, a good copy next to the file winning over a damaged one in the workspace; the snapshot is found next to the imported file for `--out` exports; a file without a fingerprint says so); the gate failing on a shifted EDL record timecode, a moved spine offset or transition, a 1-frame sequence duration and a truncated or shortened A1; a dip to black read the same from OTIO and FCPXML without metadata (the EDL exception asserted); the DROP FRAME EDL entry for drop-frame media; a `.fcpxmld` bundle; the frame-rate note |
| `test_cli.py` | `export` and `import` output lines, options, exit codes and one-line stderr errors, notes, the stale and earlier-stage warnings, `junctions` saying a stale human stage is left out; the interchange package imported lazily by the CLI and never importing `textual`, `matchcut.tui` or `matchcut.review` |
| `test_resolve.py` | The generated Resolve script run against a **fake of the documented scripting API** (the fake implements only the documented methods and rejects the others): call order and arguments, markers on the OTIO's cut frames with the junction id as customData, the playhead set last; only documented method names appear in the script (an AST check) and no 21.1 call; the playhead timecode is worked out by the script from `START_TC` plus the junction's frame (non-drop and drop-frame, and editing `START_TC` in the script moves it); every return value is checked against fake failures: no Resolve, no project, an import that returns nothing (with and without a timeline of that name already there, which is said in one line), markers that came with the import (`AddMarker` False, so `UpdateMarkerCustomData` sets the id), a junction left without its id (counted, non-zero exit), a playhead that cannot be set (the hint says to edit `START_TC`, non-zero exit); odd names stay one literal each; bad input is a one-line error; `--resolve-script` writes `open_in_resolve.py` next to the exports and fails in one line without the otio format; the exporters' record start is zero |
| `test_docs.py` | `INTEROP.md` and this file list every `KNOWN` fidelity entry verbatim, no document claims a format is a complete copy of a cut, or names a model, the example exports carry no absolute path but the placeholder and equal what the code writes now (regenerated on the fixture and diffed after normalising paths, the footage's mtime and size, path-derived uids, stage-file hashes and the version; skipped with both fingerprints in the message when the machine's ffmpeg encodes the fixture footage with other shot boundaries than the committed examples), and the human gates read PENDING |

Optional automated checks (skipped when the tool is missing; both ran for the figures below):

- The real **OpenTimelineIO 0.18.1** (a throwaway virtualenv, `OTIO_VENV_PYTHON`) loads every exported `.otio`
  with the same durations, transitions, markers and metadata.
- **`xmllint --dtdvalid`** with Apple's FCPXML 1.10 DTD (`FCPXML_DTD`; not committed) accepts `matchcut.fcpxml`.

Suites at the commit that added this file: see the last section.

## Fidelity matrix

Per format, from `matchcut/interchange/fidelity.py` (`KNOWN`), the table every export
and report uses. **Preserved** on every export: clip count and order and each clip's shot,
source in/out frames (exact at the source's rate), transition kind and length in whole
frames, clip keys, the record duration (within half a frame plus a millisecond) and where each clip sits on the record side (EDL record timecodes, FCPXML
spine offsets and sequence duration, the length of the OTIO video and audio tracks). Every
export is read back by matchcut's own reader to check exactly this; an editor's behaviour is
not covered. The report adds what each export measured (clips moved by the frame snap, dissolve
lengths rounded, and, for OTIO and FCPXML with `--music-start` not on the video frame grid, `music start moved to the video frame grid: 2 s written as 2.002 s
(frame 48 at 23.976 fps), +2.0 ms (0.05 frame)`). UNVERIFIED marks what has not been checked in an editor.

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

## Demonstration

Three runs, all with the mock backend (the why lines are mock text; no model was called). None of it
involves an editor.

### Synthetic workspace

Built the way `tests/interchange/conftest.py` builds `world`: `make_demo_footage` (18.5 s, 320x180, 25 fps, with audio),
target 8 s, mock backend, no render. 5 clips; rough cut 199 frames, final 173 frames (two dissolves, 8 and
18 frames, matchcut's 0.30 s and 0.75 s).

    python -m matchcut.pipeline.cli export WS --resolve-script
    exported 9 files to WS/exports
    fidelity: 6 checks, 0 errors (WS/exports/fidelity.md)
      roughcut  otio  ok: 6 preserved, 5 approximated, 5 lost
      roughcut  fcpxml  ok: 6 preserved, 5 approximated, 6 lost
      roughcut  edl  ok: 6 preserved, 6 approximated, 6 lost
      final  otio  ok: 6 preserved, 5 approximated, 5 lost
      final  fcpxml  ok: 6 preserved, 5 approximated, 6 lost
      final  edl  ok: 6 preserved, 6 approximated, 6 lost
    note: roughcut: 3 of 5 clips moved by the frame snap (see fidelity.md)
    note: open_in_resolve.py imports final.otio into Resolve Studio (external scripting = Local); matchcut does not run it

Measured by the report: the rough cut's snap moved 3 of 5 clips, the largest move 0.50 frame (c003); the
final cut's dissolve lengths were rounded to whole frames by at most 0.75 frame. Optional checks on these files:
OpenTimelineIO 0.18.1 loads both `.otio` files (rough cut 199 frames, final 173 frames, 4 markers each, one audio
track, 0 and 2 transitions); `xmllint --dtdvalid` accepts `matchcut.fcpxml`. The generated script has the markers at record
frames 50, 65, 114 and 155 and the playhead at `00:00:02:15` (the first junction with a transition).

Small text outputs are committed under [examples/m2a/synthetic/](examples/m2a/synthetic/README.md): `final.otio`,
`matchcut.fcpxml`, `final.edl`, `fidelity.md`, `provenance.json` (22 KB, untrimmed) and `open_in_resolve.py`.
**The committed copies have their machine-specific paths rewritten to the placeholder `/ABSOLUTE/PATH/TO`**
(the footage and the exports directory); nothing else was changed, so they are examples to read, not files to import.

### Simulated editor round trip (synthetic)

[`docs/examples/m2a/simulate_editor.py`](examples/m2a/simulate_editor.py) edits the exported `final.otio` the way a
script would, in the video track and the audio track that mirrors it: the second clip trimmed 5 frames at its head, the
first dissolve turned into a cut (the Transition item deleted), the last clip deleted with its junction marker.

    python docs/examples/m2a/simulate_editor.py WS/exports/final.otio edited.otio
    V1: trimmed the head of c002 shot1 by 5 frames
    V1: deleted the transition before c003 shot3 (Cross Dissolve)
    V1: deleted the clip c005 shot7
    V1: deleted the last junction marker
    A1: (the same three edits)
    python -m matchcut.pipeline.cli import WS edited.otio
    imported edited.otio (otio) against final: 3 human decisions -> WS/human.json
      removed c005  human removed c005 (shot 7, 17.76-18.48s)
      trim c002  human trimmed c002: in +5f
      transition c002 c003  human changed transition dissolve 8f -> cut

The three decisions are exactly the three edits. `junctions WS` then lists 3 junctions in the `human` stage; junction 1 reads
`why: human: human trimmed c002: in +5f` with history `roughcut: cut at out 2.00s / in 4.25s` and `human: in +0.20s (+5f)`; junction 2 reads
`human: out -0.16s (-4f), in +0.16s (+4f), cut` (a dissolve turned into a cut keeps the cut point, so the junction's out and in times
move by the halves of the dissolve). `roughcut.json`, `final.json`, every `passes/*.json` and `exports/provenance.json` have the same sha256 after the
import as before. With `--strip-metadata` (every matchcut metadata block removed and the clips renamed, as an application that does not keep
third-party metadata would) the same three decisions come out, and the import report says
`4 clips matched by source range because the clip key was missing or unknown` and that the file `carries no stage fingerprint`. This simulates what an editor does to a file; it does not show
what Resolve or Final Cut Pro do (they may renumber ids, change time bases, or drop metadata).

### Re-import after the pipeline ran again (synthetic)

The export records the sha256 of the stage file it was made from in every file and keeps a copy of that stage file in `exports/.stages/`. On the same
synthetic workspace, with the three edits above saved as `edited.otio`, the pipeline is run again with a longer target (`run FOOTAGE -w WS -t 12`, which changes
`final.json`) and the file is imported:

    python -m matchcut.pipeline.cli import WS edited.otio
    imported edited.otio (otio) against final: 3 human decisions -> WS/human.json
      removed c005  human removed c005 (shot 7, 17.76-18.48s)
      trim c002  human trimmed c002: in +5f
      transition c002 c003  human changed transition dissolve 8f -> cut
    import report: 1 note
      - edited.otio was exported from an earlier final: the editor's changes were diffed against that version; junction histories compare the human stage with the current final

Still exactly the three edits: the re-run's own changes to the cut are not among them. Before the fingerprint, the same import after the
re-run listed the pipeline's changes as the editor's decisions, and the stale hint told the user to do exactly that. With the snapshot deleted the import stops with
one line naming the fix; a file without the fingerprint is read against the current stage and says so.
(In the run above `human.json` from the earlier import was stale and the report had a second note saying this import replaces it.)

### Big Buck Bunny workspace (mock backend)

The 20 s, 1280x720, 24 fps Big Buck Bunny clip, a mock-backend workspace from the M1 rehearsal, copied to a scratch directory before
exporting (the original was not touched). 3 clips; rough cut 240 frames with 2 cuts; final 204 frames with two 0.75 s dissolves
(18 frames each: no rounding); fade in 1.0 s, fade out 1.333 s (metadata only in the exports); both junctions carry editor pins. Footage credit:
[screenshots/README.md](screenshots/README.md). The workspace is machine-specific, so no BBB file is committed.

    python -m matchcut.pipeline.cli export WS --resolve-script
    exported 9 files to WS/exports
    fidelity: 6 checks, 0 errors
      roughcut  otio/fcpxml/edl  ok: 5 preserved, 5/5/6 approximated, 5/6/6 lost
      final     otio/fcpxml/edl  ok: 5 preserved, 4/4/5 approximated, 5/6/6 lost
    note: roughcut: 3 of 3 clips moved by the frame snap (see fidelity.md)

(Figures of that run, made before the gate also checked the record placement: each row would now list one more preserved item, "record placement of every clip
and transition".) The rough cut's snap moved all 3 clips by at most 0.08 frame; the final cut needed no snap and no dissolve rounding. OpenTimelineIO 0.18.1 loads
both `.otio` files (rough cut 240 frames, 2 markers, 0 transitions; final 204 frames, 2 markers, 2 transitions) and `xmllint --dtdvalid` accepts
`matchcut.fcpxml`. The script puts markers at frames 71 and 133 and the playhead at `00:00:02:23`. The same simulated editor edits (a 5-frame trim of the
second clip, the first dissolve turned into a cut, the last clip deleted) give

    imported edited.otio (otio) against final: 3 human decisions -> WS/human.json
      removed c003  human removed c003 (shot 2, 16.21-19.54s)
      trim c002  human trimmed c002: in +5f
      transition c001 c002  human changed transition dissolve 18f -> cut
    import report: 1 note
      - transition next to an ignored item dropped

(The note is the second dissolve, left behind by the deleted last clip: the importer reports it instead of dropping it silently.)

### What the demonstration proves, and what it does not

Proves: the exports of two different workspaces are read back by matchcut's readers to the stage they came from, within the stated
approximations; two independent tools that are not matchcut (OpenTimelineIO, the FCPXML DTD) accept the files; a scripted edit of the OTIO is
read back as exactly the decisions it contains, with and without matchcut's metadata, and leaves every canonical file byte-identical.

Does not prove: that DaVinci Resolve, Final Cut Pro or Premiere import the files, show the dissolves, keep markers, notes or metadata, conform the
timecoded media, or write a file matchcut can read; that the Resolve script runs against a real Resolve (it ran only against a fake of the
documented API); that the editors' start timecode matches matchcut's 00:00:00:00; anything about a dip to black, fades or music in an editor; real
(non-mock) model decisions.

## Human checklist: PENDING

Needs the applications. Use a workspace from a real run (`python -m matchcut.tui footage.mp4 -w ws_m2a --backend anthropic`)
and `export WS --resolve-script`. Write what you see in the Verdict.

1. **Resolve (free), GUI import.** File > Import > Timeline with `exports/final.otio`, then with `exports/matchcut.fcpxml`. Is the media found, or
   relinked? Does it conform from the source timecode (compare a frame at a cut with `final.mp4`)? Are the dissolves transitions of the right length
   and in the right place? Are the markers there with names and notes? What is the timeline's start timecode? Is the audio there?
2. **Resolve Studio.** Set Preferences > System > General > External scripting using = Local, open a project, and run `python exports/open_in_resolve.py`.
   Does the timeline import, do the markers land on the junctions (with customData = the junction id), and does the playhead land on the
   first dissolve? If the start timecode is not 00:00:00:00, do the markers and the playhead follow the note at the top of the script?
3. **Final Cut Pro.** Import `exports/matchcut.fcpxml`. Any warning? Are the dissolves Cross Dissolves, the markers and keywords (`mc:<junction id>`) present?
4. **Round trip from Resolve.** Trim one clip, turn one dissolve into a cut and delete one clip; export OTIO (and FCPXML 1.10, and an EDL) from Resolve; run
   `python -m matchcut.pipeline.cli import WS FILE`. Are the human decisions the ones you made? Did the clip ids survive or did matching fall back to
   names and source ranges (the import report says which)? Which of matchcut's metadata did Resolve keep?
5. **Round trip from Final Cut Pro.** The same with an FCPXML export.
6. **Music.** With `--music` configured, does the music clip arrive at the right start with the gain (OTIO track A2, FCPXML lane -1)? Does a `music.only` cut mute the
   source audio?
7. **Dip to black and fades.** Export a cut with a dip to black transition and a fade in/out. What does each editor show?
8. **Stale handling.** Re-run the pipeline after an import: does `junctions` say `human.json is stale` and keep showing the real final stage? Import the same
   editor's file again: are the decisions still only the ones you made, and does the report say it was diffed against an earlier `final`?

## Verdict

| Question | Answer |
|---|---|
| Resolve (free) imports the OTIO and FCPXML with dissolves and markers (yes/no) | |
| Resolve Studio runs `open_in_resolve.py` and lands the playhead on the junction (yes/no) | |
| Final Cut Pro imports the FCPXML (yes/no) | |
| An editor's re-export imports back to the edits made (yes/no, per editor) | |
| Start timecode and media conform behave as the files assume (yes/no) | |
| Notes: what was lost, what was hard | |
| Decision | accept M2a interchange / change the approach |

## Open questions (from the feasibility study)

- Resolve's start timecode on import, and the units of `MarkIn`/`MarkOut` (the script uses neither render setting).
- Whether Resolve or Final Cut Pro keep OTIO metadata, FCPXML `md` keys, keywords or marker notes through import and re-export.
- Whether dissolves arrive as native transitions in Resolve from OTIO, FCPXML and EDL.
- `AddTransition` option keys (21.1) and a dip-to-black transition name; not used.
- Which FCPXML versions Resolve's importer accepts beyond 1.10.
- Whether Resolve and Final Cut Pro conform timecoded camera media from the source timecode matchcut writes.
- GPU and licence requirements for headless Resolve (outside M2a).

## Known limitations

- See [INTEROP.md](INTEROP.md#limits). In short: one video track and one source; picture only on import; no format carries ducking,
  loudness normalisation or the fades as effects; a dip to black is approximated everywhere.
- A stale record (see INTEROP.md) is kept only while exported files of that stage are still in the directory.
- The snapshot that makes a re-import after a pipeline re-run exact (`exports/.stages/`, one small copy per exported version of a stage, never deleted by matchcut) is
  looked up in `WORKSPACE/exports/.stages/`, then next to the imported file; an export made with `--out DIR` elsewhere is found only when the editor's file is saved in
  `DIR` too (or the folder is copied back). A file that lost its fingerprint cannot be checked against the stage it came from.
- An EDL cannot carry a dip to black without matchcut's comment: a dip read back from an EDL that lost its comments is a dissolve (OTIO and FCPXML read it by name).
- Applying human decisions to a new cut is not implemented: the `human` stage is a record, and the next pipeline run does not read it.
- The mock backend produces no real why lines: in the examples six of the eight junction notes say "no pass changed this junction", and the two dissolves read
  "transitions: beat b1 -> b2: dissolve 0.30s" and "transitions: beat b2 -> b3: dissolve 0.75s" (the rule-based transitions pass).

## Reproduce

    python -m pytest tests/interchange -q                       # add OTIO_VENV_PYTHON and FCPXML_DTD for the optional checks
    python -m matchcut.pipeline.cli export WORKSPACE --resolve-script
    python docs/examples/m2a/simulate_editor.py WORKSPACE/exports/final.otio edited.otio [--strip-metadata]
    python -m matchcut.pipeline.cli import WORKSPACE edited.otio

## Suites at the commit that added this file

`python -m pytest tests/interchange tests/pipeline tests/tui tests/review -q`, run per directory: interchange 360 passed
(`test_cli` 13, `test_common` 61, `test_docs` 10, `test_edl` 37, `test_fcpxml` 57, `test_fidelity` 22, `test_otio` 54, `test_resolve` 30,
`test_timecode` 9, `test_workspace` 67), pipeline 173, tui 143, review 87: 763 passed, 0 skipped (the optional OpenTimelineIO and
`xmllint` checks ran; `test_docs`' example comparison ran because the fixture footage's fingerprint equals the one recorded in the examples' README). The `prototypes/interchange` suite no longer exists. The fixtures build their own synthetic footage and
workspace, so the numbers do not depend on the demonstration data. This is automated verification only; the human gates above are PENDING.

## Stabilization (after the test report)

[M2A-TEST-REPORT.md](M2A-TEST-REPORT.md) is the independent tester's record (kept as written) and lists four defects, D1 to D4. This wave fixes them, closes three
ways the human stage could say something untrue, and re-runs the tester's 84-edit proof. Automated verification only; the human gates stay **PENDING**, and
Resolve, Final Cut Pro and Avid compatibility stays **UNVERIFIED**.

| | Defect | Change | Tests |
|---|---|---|---|
| D1 | The OTIO reader accepted a clip with zero or negative duration and wrote `source_out <= source_in` to `human.json` without a note | The clip is ignored with a one-line note (`clip c001 shot0: source range duration is -20 frames (not positive), ignored`), as the FCPXML reader does; it reads as `removed`. An EDL event shorter than its transitions was already a one-line error | `test_otio.py` (duration 0 and -20 at the start, middle and end of the track; every clip empty is `no usable clips`), `test_workspace.py` (the tester's reproduction: no clip of `human.json` has an out at or before its in) |
| D2 | At a junction a removal made adjacent, the human trim of the incoming (or outgoing) clip was not listed | The human stage's own trim of one of the two clips is attributed to that junction (a pass's trim in the stage that joined the clips still is not, so the adjacency story is kept); the "why" stays `made adjacent by human: removed ...` | `test_workspace.py` (trim of the incoming clip after the removed one, of the outgoing clip before it, and both; a trim of another clip not listed; a kept neighbour still lists it), the existing pacing test in `test_junctions.py` |
| D3 | A damaged snapshot was reported as missing, and the message always said `exports/.stages` | Damaged (hash mismatch, or unreadable JSON) has its own one-line error; the missing-snapshot error names every place it looked; a good copy next to the file wins over a damaged one in the workspace | `test_workspace.py` (missing with the file outside and inside the exports, damaged, cut short, unreadable JSON with a matching hash, good copy elsewhere), `test_cli.py` (stderr, exit 1, one line) |
| D4 | The music start was moved to the video frame grid (2.0 s to 2.002 s at 23.976 fps) and the fidelity report said nothing | OTIO and FCPXML list `music start moved to the video frame grid: 2 s written as 2.002 s (frame 48 at 23.976 fps), +2.0 ms (0.05 frame)` under approximated, only when the start moves; no static `KNOWN` line was needed, so the fidelity tables in INTEROP.md and this file are unchanged | `test_fidelity.py` (the note at 23.976 and 25 fps, nothing on the grid or for a negative start, extra entries after the measured ones), `test_workspace.py` (the report of a workspace export, per format) |

Human-stage integrity (the rulings of this wave):

- **Pins.** The human history repeats the canonical stage's `editor` decision whatever the editor did, so it used to make a deleted or trimmed pin read applied. The engine now judges the
  editor's cut (`editorial.pin_change` / `pin_outcome`, no interchange import): a pin is applied on `human` only while the cut has the same two clips (the pin's clip instances;
  clips of the same shots for a pin without them), adjacent, with both cut points unchanged to within half a frame; otherwise it is **changed by the editor** with a one-line
  reason (`clip removed (shot N)`, `cut trimmed by 3 frames (out)`, `clips are no longer adjacent`, `other footage (shot N is a different clip now)`). Used by the Edit view
  (badge and header), the `junctions` CLI (tag `pin-changed`, a detail line) and `Junction.pinned`. Tests: the editor deletes a pinned clip, trims the pinned cut by 3 frames, separates
  the pair by inserting a clip, swaps it, leaves it untouched (still applied; a move under half a frame too); a pin with and without clip instances; the Edit view panel and header on an imported workspace;
  the CLI listing.
- **Inherited decisions.** A human junction follows the earlier stages (states, pass decisions, the pass's "why") only when both its clips are instances the canonical stages
  have (`clip_origin`). A junction with an added, replaced or copied clip, or a new pairing, shows only the human decisions and `made adjacent by human: ...`. Tests (`test_junctions.py`):
  a replaced clip does not borrow the verdict of the clip it overlaps; a reorder shows no verdict about the old pairings; untouched junctions keep their inherited "why".
- **Ambiguous mapping.** On import a clip key carried by more than one clip of the file, and a keyless clip that overlaps two canonical clips by the same number of frames, are read as
  new clips (`hNNN`, origin `human:hNNN`) with a one-line note naming the candidates; a clear winner still gets the match. Tests: both through OTIO and EDL, and the winner case.
- **A key on other footage.** A known clip key is trusted only while the edited range still overlaps the shot of the canonical clip. A key (with its shot metadata) carried onto the footage of
  another shot is a new clip (`clip key ... now covers shot 7 (12.20-13.00s), not shot 3: read as a new clip (h001), ...`, its shot taken from the range) and the canonical clip reads as removed;
  a slip anywhere inside the same shot is still a trim. Tests: OTIO, FCPXML and EDL, both cases.
- **Valid provenance is preserved.** Everything that maps unambiguously keeps its id, key and history; canonical files and `provenance.json` stay byte-identical after an import
  (the existing tests, and the hash comparison in the 84-edit proof).

Verification at the stabilization commit: `python -m pytest tests/interchange tests/pipeline tests/tui tests/review -q`, run per directory:
interchange 401 passed (`test_cli` 14, `test_common` 61, `test_docs` 10, `test_edl` 37, `test_fcpxml` 57, `test_fidelity` 24, `test_otio` 61, `test_resolve` 30,
`test_timecode` 9, `test_workspace` 98), pipeline 186, tui 147, review 87: 821 passed, 0 failed (the two known load-flaky TUI tests passed on the first run; the optional OpenTimelineIO and
`xmllint` checks ran). The new pin, inherited-decision and ambiguity tests are in `tests/pipeline/test_editorial.py`, `tests/pipeline/test_junctions.py`, `tests/interchange/test_workspace.py` and `tests/tui/test_edit.py`.

The tester's 84-edit proof was re-run against this commit with the tester's own `scripts/editor.py`: fresh workspaces made with the report's commands (a25 at 25 fps, b2398 at 23.976 fps
with music, c2997 at 29.97 fps drop-frame with a start timecode), 14 edit sets, six files each (OTIO, FCPXML, EDL, with matchcut's metadata kept and stripped): 84 imports, all exit 0, the
header, every decision line and the note count equal to what the tester recorded (the control sets give 0 decisions), and every canonical file (stage and pass files, analysis,
`exports/` with `provenance.json`, `fidelity.*` and `.stages/`) byte-identical before and after every import.

Limits that remain: without `shots.json` a known key is trusted whatever its range; two equal range candidates the other way round
(one canonical clip over two unkeyed halves) still go to the nearer by position; the pin check compares source times, not what an editor shows after a transition trim. Nothing here
was opened in an editor.
