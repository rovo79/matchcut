# Editorial interchange: feasibility proof

**Status: feasibility proof. Not wired into matchcut (no pipeline, TUI or CLI hook), not a product feature.**
Resolve parts are tested **only against a fake of the documented scripting API, never against a live Resolve**.
Nothing here was opened in Final Cut Pro, Resolve, Premiere or IINA.

Stdlib plus matchcut's own modules. OpenTimelineIO is **not** a dependency; the tests use a throwaway venv only to validate the files.

```
python3 -m pytest prototypes/interchange -q                      # from the repo root
python3 prototypes/interchange/demo.py WORKSPACE OUTDIR          # write the files for a finished workspace
```

Optional tools (tests skip without them): `OTIO_VENV_PYTHON` (a Python with opentimelineio 0.18.1, default: the research venv),
`FCPXML_DTD` (Apple's FCPXML 1.10 DTD, `xmllint` validates against it), `mpv`, `ffmpeg` (builds the synthetic footage).

## Files

| file | what |
|---|---|
| `interchange.py` | exporters/importers, provenance sidecar, human-edit diff, mpv and Resolve helpers (about 600 lines) |
| `test_interchange.py` | 15 tests |
| `demo.py` | writes `roughcut.otio`, `final.otio`, `matchcut.fcpxml`, `provenance.json`, `final.edl`, `resolve_open_junction.py`; prints junction timecodes |

## What each piece proves

| piece | proof | how |
|---|---|---|
| `export_otio` | one `.otio` per stage loads in the real OpenTimelineIO 0.18.1; V1 duration equals `Timeline.duration` within 1 frame; transitions, markers, metadata round-trip exactly | venv subprocess |
| `export_fcpxml` | FCPXML **1.10**, one event, one project per stage, valid against Apple's 1.10 DTD; re-imports to the same clips, transitions and record times (1 frame) | `xmllint --dtdvalid`, own reader |
| conversion of dissolves | exact, see below | unit test with hand-computed frames |
| `write_provenance` | `provenance.json` keyed by `s<out>-s<in>` (junctions) and `shot<idx>@<source_in>` (clips); full history, decisions, states, scores per stage. The interchange files carry only ids and a short note | tests |
| `import_*`, `diff_human_edits`, `human_stage` | simulated editor changes (trim 5 f, dissolve to cut, marker deleted, clip renamed) give exactly two human decisions; the canonical timeline, the files on disk and the sidecar are unchanged; `build_junctions` over `[... final, human]` shows `human: in +0.20s (+5f)` in the junction history | test |
| `mpv_edl`, `mpv_ipc_commands` | headless mpv plays the EDL, reports `duration` = sum of clip durations, and loops the A-B window around a junction | stdlib socket IPC |
| `resolve_script` | the generated script runs unchanged against a fake whose objects implement only the documented methods; the call order and arguments are asserted | fake module |

## Dissolve semantics (the one place a wrong guess would silently shift every later cut)

matchcut: a dissolve of `d` **overlaps** the last `d` s of the outgoing clip with the first `d` s of the incoming one
(`Timeline.record_start`, `render.py` xfade offset = `cur_dur - d`; record duration = sum - d).
OTIO and FCPXML put a transition **on the cut point**, with `in_offset` before it and `out_offset` after it. Exact conversion, in whole frames
(`d_f = round(d * fps)` with error diffusion so the total stays within half a frame; `in_off = d_f // 2`, `out_off = d_f - in_off`):

```
outgoing clip visible range ends at   source_out - out_off        incoming clip visible range starts at   source_in + in_off
transition = [cut - in_off, cut + out_off]                         cut = matchcut record_start(incoming) + in_off
```

Example (24 fps, A 1-3 s, 0.5 s dissolve, B 5-7 s): A = frames 24..66, transition 6+6, B = frames 126..168, record duration 132 f = 5.5 s.
The transition item starts at matchcut's `record_start(B)`. Every frame the transition shows lies inside `[source_in, source_out]`,
so (unlike the research's generic "handles" warning) no media outside matchcut's own clip ranges is needed. Import reverses it.
`junction_time()` returns the **cut point** (the centre of a dissolve), which is where the markers sit. Odd `d_f` gives 4+5 style offsets (OTIO and FCPXML both allow it).

## Verified offline vs needs a live app

| claim | status |
|---|---|
| OTIO JSON loads in OpenTimelineIO 0.18.1; durations, transitions, markers, metadata | verified offline (real library) |
| FCPXML 1.10 is DTD-valid and re-imports | verified offline (xmllint, own reader) |
| mpv plays the EDL, IPC seek/loop/show-text | verified offline (mpv 0.37 headless; `--vo=null`, the OSD text is not seen) |
| Resolve script logic and call sequence | **proven against a fake only** |
| Resolve imports the OTIO/FCPXML; keeps transitions, markers, `metadata`; honours `customData`; start timecode assumption | **needs a live Resolve Studio** (external scripting is Studio-only from 21.1; headless needs a GPU, unverified) |
| Final Cut Pro accepts the file; `Cross Dissolve` uid; no gap/warning on the frame grid | **needs a Mac with FCP** |
| `md` keys, marker notes, keywords survive an editor round trip | **unknown, needs both apps** (test plan: `integration/interchange.md` section 4) |
| IINA behaviour | not tested (macOS only) |

## Known limitations

- **mpv EDL has no transitions.** Clips are concatenated, so the EDL is longer than the timeline by the sum of dissolve durations
  (7.96 s vs 6.91 s in the synthetic test). Junction times for mpv use `junction_time(..., edl=True)`. Judge a dissolve in a rendered preview, not in mpv.
- **Dip to black is unverified.** OTIO has no such type: `Custom_Transition` + `metadata`. FCPXML gets `Fade To Color`
  (`FxPlug:F779C565-...`, colour param `0 0 0 1`) taken from secondary sources; every such transition carries an `unverified` note (OTIO metadata, FCPXML `md`) and the effect is only declared when used.
- **FCPXML `md` survival is unknown.** `md` on clips/sequence/transitions is DTD-valid, but FCP only applies `editable` values it has a metadata view for, and an editor's re-export may drop our keys (and Resolve maps a fixed field set). Import therefore falls back to the clip name (`... shot<N>`), then to source ranges against shot bounds; ids that are lost come back as new clips (`h001`, ...).
- **A marker cannot carry `md`** (DTD: `<marker/>` is empty). The marker holds only `value` (`J<n> s<out>->s<in>`) and a truncated `note`; the clip also gets a keyword `mc:<id>` and `md com.matchcut.junction`. The OTIO marker does carry full metadata, but nothing says an editor keeps it.
- Fades (`fade_in`, `fade_out`) are metadata only; neither format has a first-class fade and none is applied. Audio is a mirrored `A1` track in OTIO and the asset's own audio in FCPXML; there is no music lane.
- Human edits: gaps, extra tracks, nested items and asymmetric editor-made transition offsets are ignored or normalised (`in_off = d // 2` is assumed when comparing). A dissolve turned into a cut keeps the *cut point*, so in the `human` stage the junction's out/in times move by the transition halves; the `transition` decision is the cause, and trims are computed on the visible cut point so they are not reported twice.
- `Junction.why()` ranks editor decisions and match-cut verdicts above other decisions, so a `human` decision shows in `history()` and `decisions` but not necessarily in `why()`.
- FCPXML: `tcFormat="NDF"` always (no drop-frame for 29.97), sequence `format` size defaults to 1920x1080 unless given, one video format for the whole file.
- Resolve: markers and the playhead assume the imported timeline starts at 00:00:00:00 (unverified; Resolve's default start timecode is commonly 01:00:00:00). OTIO may already bring the markers in, in which case `AddMarker` returns False (harmless). `AddTransition`/`SetFades` (21.1, keys unverified) are deliberately not used. `Timeline.SetCurrentTimecode` placement is taken from the 21.0.4 README copy.
- Python 3.9 syntax; tested on 3.13 only.
