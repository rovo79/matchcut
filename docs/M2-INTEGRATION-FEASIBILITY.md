# M2 editorial integration: feasibility findings

Status: findings and a recommendation, awaiting approval. Nothing here is wired
into the pipeline or the TUI. The M2 custom-preview plan
(`docs/superpowers/plans/2026-10-18-m2-temporal-review.md`) is on hold until a
direction is chosen. M1's human acceptance gate is still open and unaffected.

Evidence: research notes (scratch, summarised here), and the offline proof in
`prototypes/interchange/` (15 tests: `python3 -m pytest prototypes/interchange -q`).

## Answer in one paragraph

Integrate through **files, not through an application**: export each stage of
the cut as OpenTimelineIO (primary) and FCPXML 1.10 (Final Cut Pro, and a
second route into Resolve), keep matchcut's decision provenance in a sidecar
keyed by stable junction ids, and read an editor's re-export back as a new
`human` stage that never overwrites the canonical timeline. Add two thin,
optional adapters on top: **mpv** for instant junction navigation and hard-cut
A/B (no rendering), and a **DaVinci Resolve Studio** adapter (a generated or
live script using only documented scripting calls) for editors who have
Studio. Keep a small ffmpeg-rendered junction preview, because no lightweight
player can show a dissolve. Do not build on Resolve's MCP server: it exposes no
editing tools of its own.

## The five questions

Status words: **offline** = proven here with real libraries or tools;
**fake** = proven only against a fake of the documented API; **live** = needs
the real application; **no** = not possible with that route.

| # | Question | OTIO / FCPXML files | mpv / IINA | Resolve Studio scripting | Final Cut Pro |
|---|---|---|---|---|---|
| 1 | Export an editable timeline with cuts, transitions and audio | **offline**: OTIO loads in OpenTimelineIO 0.18.1 with exact durations, dissolves and source audio; FCPXML 1.10 is valid against the 1.10 DTD and re-imports to the same record times. Music lane not exported yet. | **offline**, hard cuts only (EDL has no transitions) | **live**: `ImportTimelineFromFile` accepts OTIO and FCPXML (documented); whether dissolves arrive as Resolve transitions is untested | **live**: needs a Mac; the Cross Dissolve effect id comes from real exports, not Apple docs |
| 2 | Navigate an external editor to a junction | junction record times and timecodes computed (**offline**) | **offline**: JSON IPC seek + A-B loop around the junction, tested headless | **fake**: `SetCurrentTimecode`, `OpenPage("edit")`, markers via `AddMarker(..., customData=junction id)`; start-timecode assumption (00:00:00:00 vs Resolve's usual 01:00:00:00) unverified | **live**: no scripting API; markers in the FCPXML are the navigation aid |
| 3 | Preview and compare rough vs refined | one file per stage (**offline**) | **offline** for hard cuts: two EDLs, switch with `loadfile`; dissolves are played as cuts | **live**: two timelines; range render via `SetRenderSettings(MarkIn/MarkOut)` is documented, units unverified | **live**: two projects in one library |
| 4 | Attach AI decisions as inspectable metadata | **offline**: OTIO `metadata` on clips, markers and the timeline round-trips exactly; FCPXML carries marker notes, keywords and `md` keys (DTD-valid); full history lives in `provenance.json` | OSD text over IPC (**offline**, not visible headless) | **fake**: marker name/note/customData; no per-timeline-item metadata in the API | **live**: whether FCP keeps unregistered `md` keys is unknown |
| 5 | Accept human edits without losing the canonical timeline or provenance | **offline, simulated**: an edited OTIO (trim 5 frames, dissolve to cut, marker deleted, clip renamed) imports to exactly two `human` decisions; canonical files and sidecar unchanged; the junction history shows `human: in +0.20s (+5f)` | not applicable | **live**: Resolve's own re-export (`EXPORT_OTIO`, `EXPORT_FCPXML_1_10`) must be tested for id survival | **live**: FCP renumbers ids and changes timebases; the reader already accepts arbitrary rationals |

## What needs a live application, a licence or hardware

| Capability | Needs |
|---|---|
| Opening the exported OTIO/FCPXML in Resolve (GUI import) | Resolve (free or Studio). OTIO import/export since Resolve 18 per the OTIO project's tool list |
| Any script driving Resolve from outside (import, markers, playhead, range render) | **Resolve Studio** with Preferences > System > General > External scripting = Local. Studio-only, certainly from 21.1 |
| Resolve's MCP server | Resolve Studio 21.1; stdio only, local only; 14 tools, mostly `run_script` around the same API. Third-party evidence; Blackmagic's own pages were unreachable from here |
| `AddTransition`, `SetFades` (21.1) | Studio 21.1; option keys undocumented in anything we could read. Not used |
| Headless Resolve (`-nogui`) | Studio plus, almost certainly, a supported GPU (unverified) |
| Final Cut Pro import | macOS with FCP |
| IINA | macOS; URL scheme `iina://open?url=…` with whitelisted mpv options (`start`, `ab-loop-a/b`); IPC undocumented; JS plugins since 1.4.0 |
| mpv | nothing beyond the binary (Linux, macOS, Windows) |

Ordinary pipeline runs need none of these; every adapter is optional.

## Findings that change the plan

1. **Resolve's MCP server is not an integration surface.** Its tools are
   documentation lookups, LUT/DCTL helpers and `run_script`, which runs Python
   against the same scripting API. Anything it can do, a documented script can
   do, without an AI client in the loop. Community MCP servers with "88 tools"
   are third-party wrappers, not Blackmagic's.
2. **Dissolves need no extra media.** matchcut's dissolve overlaps the end of
   one clip with the start of the next; OTIO and FCPXML centre a transition on
   a cut. Converted exactly (the outgoing clip ends `d/2` early, the incoming
   one starts `d/2` late), every frame the transition shows is inside
   matchcut's own clip ranges, so no handle check is needed. Record times stay
   identical (verified to within a frame).
3. **Write FCPXML ourselves; don't use OTIO's FCPXML adapter.** It writes 1.8,
   drops transitions and metadata, and mis-rounds 23.976. Emit 1.10: it is
   Resolve's highest scripted export constant, and Resolve 18-19 rejected 1.12.
4. **Provenance travels by id, not by payload.** FCPXML markers cannot hold
   metadata, and no application documents keeping custom metadata through an
   edit. The interchange files carry stable ids (junction `s<out>-s<in>` in
   marker names, keywords, OTIO metadata, Resolve marker `customData`); the
   decision history stays in matchcut's `provenance.json`. If an editor drops
   every id, import falls back to clip names, then to source ranges.
5. **No lightweight player shows transitions.** mpv EDLs play exact source
   ranges (frame-accurate, with audio) but concatenate them. Judging a
   dissolve or a fade needs a rendered preview, either our ffmpeg graph or a
   real editor.
6. **Dip to black and fades have no common representation.** OTIO has only
   dissolves and custom transitions; FCPXML's "Fade To Color" id comes from
   secondary sources. Both are exported with an `unverified` label; fades are
   metadata only.

## Recommended architecture

```
matchcut pipeline ──► canonical stage timelines (JSON)  +  provenance.json (decisions by stable id)
                         │
                         ├─ interchange (stdlib, app-independent)
                         │     export: OTIO per stage, FCPXML 1.10, CMX EDL fallback
                         │     import: OTIO / FCPXML  ──► "human" stage + human decisions (diff by shot lineage)
                         │
                         ├─ adapters (optional, each behind one small interface)
                         │     mpv:      EDL per stage + JSON IPC (seek, A-B loop around a junction, OSD)
                         │     render:   ffmpeg junction preview with real transitions and audio (existing graph)
                         │     Resolve:  Studio-only script: import, markers with customData, playhead, range render
                         │
                         └─ TUI Edit workbench: "open in …" actions call an adapter; no app-specific code in the engine
```

Rules: the engine never imports an adapter; adapters read the interchange
files and the junction data, never the other way round; a human edit becomes a
stage with decisions (and, later, optionally pins), never a silent rewrite of
the canonical cut.

## Proposed next milestones (for approval)

1. **M2a, interchange:** move `prototypes/interchange` into
   `matchcut/pipeline/interchange.py` with an `export` and an `import` CLI
   command; add the music lane and fades as far as the formats allow; keep it
   stdlib-only.
2. **M2b, junction review:** mpv navigation from the Edit tab (`p` opens the
   cut at the selected junction with an A-B loop; `a` switches rough and
   refined), plus a trimmed version of the ffmpeg preview plan for junctions
   with a transition.
3. **M2c, Resolve Studio adapter (opt-in):** only once someone with Studio can
   run the live checklist below.

## Live checklist (needs a Mac or PC with the applications)

1. Resolve (free is enough): File > Import > Timeline with `final.otio` and with
   `matchcut.fcpxml` from `prototypes/interchange/demo.py`. Do the dissolves
   become transitions? Are the markers there with their notes? What is the
   start timecode?
2. Resolve Studio: run the generated `resolve_open_junction.py`. Does the
   playhead land on the junction? Are the markers' `customData` set?
3. In Resolve, trim one clip and export OTIO and FCPXML 1.10; import them with
   `prototypes/interchange` and check the `human` decisions.
4. Final Cut Pro: import `matchcut.fcpxml`; check the dissolves, markers and
   keywords; re-export and import as in step 3.
5. IINA: open the EDL via `iina://open?url=…` with `mpv_ab-loop-a/b`.

## Open questions

- Resolve's start timecode on import and the units of `MarkIn`/`MarkOut`.
- Whether Resolve or FCP keep OTIO metadata, FCPXML `md` keys, keywords or
  marker notes through import and re-export.
- `AddTransition` option keys (21.1) and a dip-to-black transition name.
- GPU and licence requirements for headless Resolve.
