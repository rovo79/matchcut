# M2a: Editorial interoperability — design

Status: approved direction (user, 2026-10-19). Promotes the feasibility proof in
`prototypes/interchange/` (see `docs/M2-INTEGRATION-FEASIBILITY.md`) into supported
pipeline functionality. Developed on its own branch, stacked on M2b, so it can be
reviewed independently. M1's and M2b's human acceptance gates stay open.

## Goal

Export any stage of a matchcut cut as an editable timeline that a real editor can
open, and read an editor's re-export back, without losing matchcut's canonical
timeline or its decision provenance — and say exactly what each format keeps,
approximates and loses.

## Boundaries

- New package `matchcut/interchange/`, standard library only. It reads the engine's
  canonical stage timelines, `probe.json`, `shots.json`, `config.json`; it never
  rewrites them. It never imports `textual`, `matchcut.tui` or `matchcut.review`.
  The engine's passes and stages never import it; the pipeline CLI imports it
  lazily for two new commands.
- No external editor, licence or MCP server is a dependency. OpenTimelineIO, an
  FCPXML DTD, `xmllint` and DaVinci Resolve are used only by optional tests and
  documented manual checks.
- Resolve's native MCP server (Studio 21.1, stdio, ~14 tools, mostly `run_script`)
  is not used: it adds nothing the documented scripting API cannot do and offers no
  verified frame-accurate editing tools. M2a prepares Resolve integration through
  documented interchange files and a generated script only (M2c builds on it).

## Formats

| Format | File | Role |
|---|---|---|
| OpenTimelineIO JSON (`Timeline.1`, `Stack.1`, `Track.1`, `Clip.2`, `ExternalReference.1`, `Transition.1`, `Marker.2`, `Gap.1`) | `<stage>.otio` | primary: best fidelity, full metadata; imported by Resolve (18+, GUI import on free) |
| FCPXML 1.10 | `matchcut.fcpxml` (one project per exported stage) | Final Cut Pro; second route into Resolve (its highest scripted FCPXML version) |
| CMX3600 EDL | `<stage>.edl` | lowest common denominator: one video track, cuts and dissolves, comments |

## Time and source fidelity

- **Exact frame rate:** the source's rate as an exact rational from ffprobe
  (`r_frame_rate`), with common NTSC rates recognised; never a rounded float.
- **Frame-accurate ranges:** every clip's source in/out is an integer frame count at
  that rate. Stages saved before the final snap (rough cut, passes) are snapped on
  export with the shot bounds; every clip moved by snapping is reported
  (count, largest move in frames).
- **Source references:** absolute path and `file://` URL, the media's duration,
  plus a content key (size, mtime) in metadata.
- **Order and record times:** clips in timeline order; record times equal
  matchcut's `Timeline.record_start` to the frame.

## Transitions, fades, audio

- **Dissolve:** matchcut overlaps the last `d` of the outgoing clip with the first `d`
  of the incoming one; OTIO/FCPXML centre a transition on a cut. Converted exactly
  (outgoing visible range ends `out_off` early, incoming starts `in_off` late,
  `d` in whole frames with error diffusion, `in_off = d_f // 2`,
  `out_off = d_f - in_off`); no media outside matchcut's clip ranges is needed.
  EDL: a `D nnn` dissolve event.
- **Dip to black:** OTIO `Custom_Transition` with metadata; FCPXML "Fade To Color"
  (uid from secondary sources, unverified); EDL: a dissolve plus a comment.
  Reported as approximated.
- **Fade in/out of the whole cut:** metadata only in OTIO and FCPXML; an EDL comment.
  Reported as lost (not rendered by the receiving editor).
- **Source audio:** follows the picture (OTIO: an audio track mirroring the video
  clips; FCPXML: the asset's audio; EDL: `AA/V` events).
- **Music:** OTIO: a second audio track with one clip of the music file
  (`music.start`, the cut's duration), gain in metadata; FCPXML: a connected audio
  clip on lane -1 with `<adjust-volume amount="…dB"/>`; EDL: not carried. Ducking
  and loudness normalisation are lost in every format (reported).

## Provenance

- Every interchange file carries stable ids: the junction id `s<out>-s<in>` and the
  clip-instance key (`timeline.clip_origin`, the M1 lineage key). OTIO: full
  metadata on clips, markers and the timeline. FCPXML: marker `value` (junction id)
  and a truncated `note` (the why line), a keyword `mc:<junction id>`, and
  `<metadata><md>` keys on clips. EDL: `* MATCHCUT JUNCTION <id>` and
  `* MATCHCUT CLIP <key>` comments.
- `provenance.json` next to the exports is the canonical record: per exported
  stage, per junction id: the decisions, history, scores, pin state; per clip key:
  the clip's stage ids and ranges. Interchange files are never the only copy.

## Fidelity report (validation)

Every export is validated against the canonical timeline by re-importing it with
matchcut's own readers: clip count and order, shot and source in/out frames
(exact), transition kind and frames, record duration (≤ ½ frame). Any difference
not explained by a documented approximation fails the export with a one-line
`InterchangeError`. `fidelity.json` and `fidelity.md` list, per format and stage:
**preserved**, **approximated** (with counts and largest deltas: snapped clips,
dissolve frame rounding, dip to black), **lost** (fades, music mix, md survival in
third-party apps unverified, EDL: music/metadata). No format is called lossless.

## Import (human edits)

`import_timeline(ws, cfg, path, format=None)` reads OTIO, FCPXML or EDL (format from
the extension when not given), maps clips back to matchcut clips (clip key from
metadata, else clip name `… shot<N>`, else source range against shot bounds), and
writes a **`human` stage** (`workspace/human.json`) whose history holds `human`
decisions per junction (trim N frames, transition changed, clip removed / added /
reordered). The canonical stage files and `provenance.json` are never modified.
`stage_timeline_paths` lists `human` after `final`, so the junction inspector and
the review package show it like any stage. Anything that cannot be mapped (other
media, gaps, extra video tracks, speed changes, effects, nested items) is listed
in an import report and ignored — never silently dropped. Reading a file exported
by FCP or Resolve accepts arbitrary rationals and renumbered ids.

## Resolve preparation

`interchange/resolve.py` generates a Python script that uses only scripting calls
documented in the Resolve 21.0.4 README (scriptapp, GetProjectManager,
GetCurrentProject, GetMediaPool, ImportTimelineFromFile, SetCurrentTimeline,
OpenPage, Timeline.AddMarker with customData = junction id, Timeline.UpdateMarkerCustomData
when a marker already sits on the frame, SetCurrentTimecode,
GetTimelineCount/GetTimelineByIndex/GetName). The script checks every return value: a
junction that ends up without its id as marker customData is counted and makes it exit
non-zero, a failed playhead prints the start-timecode hint and exits non-zero, and a
timeline that already existed under that name is said so. It needs Resolve Studio with external
scripting = Local; it is never run by matchcut. Assumptions it cannot verify (the
imported timeline's start timecode) are stated in the script and the docs. `open_in_resolve.py`
is derived output: an export that writes the OTIO of the stage it targets regenerates it, flag or not,
and a stale record names it.

## CLI

- `python -m matchcut.pipeline.cli export WORKSPACE [--stage S ...] [--format otio|fcpxml|edl ...] [--out DIR]`
  (default: stages `roughcut` and `final`, all formats, `WORKSPACE/exports/`); prints
  the files and the fidelity summary; exit 1 on validation failure.
- `python -m matchcut.pipeline.cli import WORKSPACE FILE [--format F]`; prints the
  human decisions and the import report; exit 1 on errors.

## Verification

Automated (deterministic, synthetic footage, mock backend): exact rationals; frame
conversions; dissolve conversion; OTIO/FCPXML/EDL export→import round trips equal
the canonical timeline within the documented approximations; music tracks; fidelity
report contents; human-edit import (trims, transition changes, removals, additions,
reorders, renumbered ids, rational time bases); stage listing; CLI; Resolve script
against a fake of the documented API. Optional (skip without the tool): real
OpenTimelineIO loads the files; FCPXML validates against the 1.10 DTD
(`FCPXML_DTD`). Demonstration: exports of a synthetic and the Big Buck Bunny
workspace with their fidelity reports and a simulated editor round trip, written up
in `docs/M2A-ACCEPTANCE.md` with an independent test report
(`docs/M2A-TEST-REPORT.md`). Human acceptance (opening the files in Resolve and FCP,
re-exporting, importing back) stays PENDING.
