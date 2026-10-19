# M2a Editorial Interoperability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Export any stage of a matchcut cut as OTIO, FCPXML 1.10 and CMX3600 EDL with frame-accurate source ranges, transitions, audio and stable provenance ids; validate every export against the canonical timeline and report what is preserved, approximated and lost; import an editor's re-export as a separate `human` stage; and generate a Resolve script against documented calls only.

**Architecture:** A new stdlib-only package `matchcut/interchange/` promotes the feasibility proof `prototypes/interchange/interchange.py` (read it: it holds working, tested code for most pieces — rationals, the dissolve conversion, OTIO/FCPXML writers and readers, provenance, the human-edit diff, the Resolve script template). It is split into focused modules, extended (music, EDL, fidelity report, clip-origin ids, exact source rate) and wired into the pipeline CLI. The prototype directory is removed in Task 5.

**Tech Stack:** Python 3.9+ syntax (runs on 3.13), stdlib (`json`, `xml.etree`, `fractions`, `hashlib`), ffprobe; optional in tests only: the OTIO venv at `/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/integration/venv` (`OTIO_VENV_PYTHON`), `xmllint` with `FCPXML_DTD` (the 1.10 DTD at `/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/integration/dtd/FCPXMLv1_10.dtd`, never committed).

**Spec:** `docs/superpowers/specs/2026-10-19-m2a-editorial-interop.md` (binding).

## Global Constraints

- `matchcut/interchange/` imports only the standard library and `matchcut.pipeline` modules; never `textual`, `matchcut.tui`, `matchcut.review`. The engine's passes/stages never import `matchcut.interchange`; only `matchcut/pipeline/cli.py` imports it, lazily inside the new commands.
- No new pip dependencies; no external editor, licence or MCP server is required at runtime or in non-optional tests.
- Canonical files (`roughcut.json`, `passes/*.json`, `final.json`, `provenance.json` once written) are never modified by import.
- Every error is an `interchange.InterchangeError` with a one-line message; the CLI prints it to stderr and exits 1.
- No format is described as lossless anywhere; every approximation and loss is listed in the fidelity report and the docs.
- Python 3.9 syntax (`from __future__ import annotations`, typing generics). Suites must pass after every task: `python3 -m pytest tests/pipeline -q`, `tests/tui`, `tests/review`, `tests/interchange` (new), and `prototypes/interchange` until Task 5 removes it.
- Branch: `claude/m2a-editorial-interop-bkqqay` (never push to any other branch). Commit with `git -c user.name="Claude" -c user.email="noreply@anthropic.com" commit -F <msgfile>` (message file in `/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/`), ending with a `Co-Authored-By:` line carrying your own environment's attribution, then `Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ`; push with `git push -u origin claude/m2a-editorial-interop-bkqqay`. Stage by explicit path; never stage `.superpowers/`; never rewrite history. No model identifier in code, docs or commit titles.
- M1's and M2b's human gates stay PENDING; M2a's human checklist is PENDING.

---

## File structure

| File | Responsibility |
|---|---|
| `matchcut/interchange/__init__.py` | `InterchangeError`, public API re-exports |
| `matchcut/interchange/common.py` | exact source rate, frames/rationals, snapping for export, stable ids (junction id, clip key), the dissolve conversion (`layout`), source descriptor |
| `matchcut/interchange/otio.py` | OTIO export/import (video, source-audio, music tracks; markers; metadata) |
| `matchcut/interchange/fcpxml.py` | FCPXML 1.10 export/import (asset, spine, transitions, music lane, markers, keywords, md) |
| `matchcut/interchange/edl.py` | CMX3600 EDL export/import (cuts, dissolves, comments) |
| `matchcut/interchange/provenance.py` | `provenance.json` |
| `matchcut/interchange/fidelity.py` | validation against the canonical timeline; preserved/approximated/lost report (`fidelity.json`, `fidelity.md`) |
| `matchcut/interchange/workspace.py` | `export_workspace`, `import_timeline` (human stage + import report) |
| `matchcut/interchange/resolve.py` | Resolve script generator (documented calls only) |
| `matchcut/pipeline/junctions.py` | `stage_timeline_paths` lists `human` after `final` |
| `matchcut/pipeline/cli.py` | `export` and `import` commands |
| `tests/interchange/` | tests (conftest with synthetic footage + mock workspace, fakes) |
| `docs/INTEROP.md`, `docs/M2A-ACCEPTANCE.md`, `docs/M2A-TEST-REPORT.md` (written by the independent tester), `docs/PIPELINE.md`, `README.md` | docs |

---

### Task 1: Common time model, OTIO export/import, provenance

**Files:** create `matchcut/interchange/{__init__,common,otio,provenance}.py`, `tests/interchange/{__init__,conftest,test_common,test_otio}.py`.

**Interfaces — produces:**
- `InterchangeError(RuntimeError)`.
- `common.SourceInfo` (frozen dataclass): `path: str` (absolute), `url: str`, `rate: Fraction` (exact), `duration_frames: int`, `width, height: int`, `has_audio: bool`, `size: int`, `mtime_ns: int`; `common.source_info(media_path: str) -> SourceInfo` (ffprobe `r_frame_rate` as `Fraction`, falling back to the nearest of 24000/1001, 24, 25, 30000/1001, 30, 50, 60000/1001, 60 when the probe gives a float only; `InterchangeError("source footage not found: <path>")`).
- `common.to_frames(t: float, rate: Fraction) -> int` (round half away from zero); `common.rt(frames: int, rate: Fraction) -> str` (FCPXML rational `"<n>/<d>s"`, reduced; `"0s"` for 0).
- `common.export_timeline(tl: Timeline, bounds) -> Tuple[Timeline, List[str]]` — a frame-snapped copy (`Timeline.snap_to_frames(bounds)`) plus one note per clip moved by the snap (`"c003: in moved +1 frame"`).
- `common.junction_id(a: Clip, b: Clip) -> str` = `f"s{a.shot_index}-s{b.shot_index}"`, made unique within a timeline by suffixing `#2`, `#3` on repeats (the prototype's `junction_ids`); `common.clip_key(c) = timeline.clip_origin(c)`.
- `common.layout(tl, rate) -> List[LaidClip]`: per clip the visible source in/out frames, record in/out frames, and the incoming transition `(kind, in_off, out_off)` after the exact dissolve conversion with error diffusion (copy the prototype's `_layout` and its tested arithmetic).
- `otio.export_otio(tl, stage, src: SourceInfo, junctions: Sequence[Junction], music: Optional[MusicConfig], music_src: Optional[SourceInfo]) -> dict` — `Timeline.1` with a video track (clips, `Transition.1` `SMPTE_Dissolve` or `Custom_Transition` for dip to black with `metadata.matchcut.kind`, markers per junction at the cut record frame), an audio track `A1` mirroring the video clips when the source has audio, and a music track `A2` (one clip of the music file from `music.start`, the cut's record duration, `metadata.matchcut.gain_db/duck`) when music is configured; metadata as in the spec (clip: `clip_id, clip_key, shot_index, beat_id, stage`; marker: `junction_id, why, history, pinned, pin_origin, transition`; timeline: `stage, source, rate [num, den], fade_in, fade_out, provenance_file, matchcut_version`).
- `otio.import_otio(doc, bounds=None) -> Tuple[Timeline, List[str]]` — the timeline (overlap semantics restored, `origin` from `clip_key` metadata) and import notes (ignored items). Accepts float rates by snapping to the exact rate list; rejects documents whose video track references another source with `InterchangeError` only if no clip maps.
- `provenance.write_provenance(path, stages: Sequence[Tuple[str, Timeline]], junctions_by_stage, src) -> dict` (the prototype's, keyed by junction id and clip key).

- [ ] **Step 1: Tests** — port the prototype tests that cover these pieces (`test_dissolve_overlap_to_centred_conversion_is_exact`, `test_dip_to_black_is_exported_but_labelled_unverified` (OTIO half), `test_ntsc_rates_use_exact_rationals`, `test_otio_documents_load_in_real_opentimelineio` (optional, skip without `OTIO_VENV_PYTHON`/the default venv), `test_otio_roundtrips_through_import_without_a_library`, `test_provenance_sidecar_is_keyed_by_stable_ids`) to the new API, and add: `source_info` on the demo footage gives `Fraction(25, 1)`; `to_frames`/`rt` table tests (0, 1 frame, 23.976 cases); `export_timeline` reports moved clips for an unsnapped rough cut and none for `final`; the music track (`A2`) exists with the right available range when `cfg.music.path` is set and is absent otherwise; repeated shot pairs get unique junction ids; clip keys round-trip through OTIO import into `Clip.origin`.
- [ ] **Step 2-4:** RED, implement (copy and adapt the prototype functions named above), GREEN; all suites.
- [ ] **Step 5:** commit and push (title: "Add the interchange package: exact time model, OTIO and provenance").

### Task 2: FCPXML 1.10

**Files:** `matchcut/interchange/fcpxml.py`, `tests/interchange/test_fcpxml.py`.

**Interfaces:** `fcpxml.export_fcpxml(stages: Sequence[Tuple[str, Timeline, Sequence[Junction]]], src, music, music_src, name) -> str` (one event, one project per stage; `format` from `src` with exact `frameDuration`; `asset` with `media-rep` URL, `uid` from the content key, `hasAudio`; spine `asset-clip`s; centred `transition`s referencing `Cross Dissolve` (`FxPlug:4731E73A-8DAC-4113-9A30-AE85B1761265`) or `Fade To Color` (`FxPlug:F779C565-486D-4633-8035-0374B4DB8F5C`, colour black, labelled unverified in an `md`); per junction a `marker` (value = junction id + shots, note = why truncated to 240 chars) and a `keyword` `mc:<junction id>` on the incoming clip; `md` keys `com.matchcut.clip_key`, `com.matchcut.clip_id`, `com.matchcut.shot_index`, `com.matchcut.stage`; music as a connected `asset-clip` on `lane="-1"` with `<adjust-volume amount="<gain>dB"/>` when configured). `fcpxml.import_fcpxml(xml, stage=None, bounds=None) -> Tuple[Timeline, List[str]]` (arbitrary rationals, renumbered ids, lane ≠ 0 items and unknown assets listed in notes).

- [ ] Tests: port `test_fcpxml_is_dtd_valid_and_reimports` (DTD part optional via `FCPXML_DTD`), `test_fcpxml_reimport_without_xmllint`, `test_fcpxml_import_survives_stripped_metadata`, `test_import_fcpxml_parses_arbitrary_rationals`, the dip-to-black FCPXML half; add: music lane present with the right offset/start/duration and volume; a file whose ids and time base were rewritten the way FCP does it (e.g. `/3000s` multiples, renumbered `r` ids) re-imports to the same clips; two stages produce two projects.
- [ ] RED → implement → GREEN → all suites → commit/push ("Add FCPXML 1.10 export and import").

### Task 3: EDL and the fidelity report

**Files:** `matchcut/interchange/edl.py`, `matchcut/interchange/fidelity.py`, `tests/interchange/test_edl.py`, `tests/interchange/test_fidelity.py`.

**Interfaces:**
- `edl.export_edl(tl, stage, src, title) -> str` — CMX3600, `FCM: NON-DROP FRAME` (or `DROP FRAME` for 29.97 when chosen — use NON-DROP and note it), reel `AX`, `AA/V` when the source has audio else `V`, `C` events, dissolves as a `C` + `D nnn` pair (centred semantics converted exactly as in OTIO), `* FROM CLIP NAME: <source file name>`, `* MATCHCUT CLIP <clip key>`, `* MATCHCUT JUNCTION <id>` comments, dip to black as a dissolve plus `* MATCHCUT TRANSITION dip_to_black`. `edl.import_edl(text, rate, src_path, bounds=None) -> Tuple[Timeline, List[str]]`.
- `fidelity.check(canonical: Timeline, reimported: Timeline, fmt: str, rate) -> FidelityResult` — compares clip count/order, shot, source in/out frames (exact), transition kind and frames, record duration (≤ ½ frame); `FidelityResult(fmt, stage, preserved: List[str], approximated: List[str], lost: List[str], errors: List[str])`. Known approximations/losses per format are a table in `fidelity.py` (`KNOWN = {"otio": {...}, "fcpxml": {...}, "edl": {...}}`) that the report always includes (e.g. EDL: music, metadata other than comments, fade in/out; all: ducking/loudness; FCPXML: md survival in third-party apps unverified; dip to black approximated) plus measured items (snapped clips with largest move; dissolve frame rounding with largest error).
- `fidelity.write_report(results, out_dir) -> Tuple[Path, Path]` → `fidelity.json`, `fidelity.md` (a table per stage: format × preserved/approximated/lost/errors).

- [ ] Tests: EDL round trip of the synthetic final cut equals the canonical (frames exact; dissolves); 23.976 and 25 fps timecodes; comments carry the ids; `fidelity.check` passes for OTIO/FCPXML/EDL exports of the synthetic workspace and fails with a specific error when a clip's source in is altered by one frame in the reimported copy; the report lists the known losses for each format and the measured snapping note for a rough cut.
- [ ] RED → implement → GREEN → all suites → commit/push ("Add CMX3600 EDL and the interchange fidelity report").

### Task 4: Workspace export/import, the human stage, CLI

**Files:** `matchcut/interchange/workspace.py`, `matchcut/pipeline/junctions.py` (stage paths), `matchcut/pipeline/cli.py`, `tests/interchange/test_workspace.py`, `tests/interchange/test_cli.py`.

**Interfaces:**
- `workspace.export_workspace(ws, cfg, stages=("roughcut", "final"), formats=("otio", "fcpxml", "edl"), out_dir=None) -> ExportResult(files: List[Path], results: List[FidelityResult], notes: List[str])` — loads stages (missing stage → `InterchangeError("no stage <s> in <ws>")`), source and music info, junctions per stage (`build_junctions` up to that stage), snaps (`export_timeline`), writes `<stage>.otio`, `matchcut.fcpxml`, `<stage>.edl`, `provenance.json`, validates each file by re-import (`fidelity.check`) and writes the reports; any `errors` → `InterchangeError("export of <fmt>/<stage> does not match the canonical timeline: <first error>")` after writing the reports.
- `workspace.import_timeline(ws, cfg, path, fmt=None) -> ImportResult(stage_path: Path, decisions: List[Decision], notes: List[str])` — reads the file, matches against the canonical stage it was exported from (OTIO timeline metadata `stage`, FCPXML project name suffix, EDL title; default `final`), builds the `human` stage with `pass_name="human"` decisions (port the prototype's `_match`, `diff_human_edits`, `human_stage`, extended to use `Clip.origin` keys first), writes `workspace/human.json` and `workspace/human_import.json` (notes, source file, format, time), never touching canonical files.
- `junctions.stage_timeline_paths(ws)` appends `("human", ws / "human.json")` after `final`.
- CLI `export` and `import` per the spec (lazy `from matchcut.interchange import workspace`).

- [ ] Tests: export of the mock workspace writes all files and reports; `fidelity.json` has no errors; canonical files' bytes unchanged after export and import; import of an OTIO edited like an editor would (trim 5 frames, dissolve→cut, delete a marker, rename a clip, renumber ids) gives exactly the expected human decisions and a `human` stage that `load_stage_timelines` lists last and `build_junctions` shows in a junction's history; an FCPXML and an EDL import of the same edit give the same decisions; an item referencing other media is listed in notes and ignored; CLI exit codes and output lines; the TUI is unaffected (run tests/tui).
- [ ] RED → implement → GREEN → all suites → commit/push ("Export and import workspaces; editor changes come back as a human stage").

### Task 5: Resolve preparation, docs, demonstration, remove the prototype

**Files:** `matchcut/interchange/resolve.py`, `tests/interchange/test_resolve.py`, `docs/INTEROP.md`, `docs/M2A-ACCEPTANCE.md`, `docs/PIPELINE.md`, `README.md`, delete `prototypes/interchange/` (and its tests), update `docs/M2-INTEGRATION-FEASIBILITY.md` with a status line pointing to INTEROP.md.

- [ ] `resolve.resolve_script(timeline_file, timeline_name, junction_frames, navigate_to, start_tc="00:00:00:00") -> str` — port the prototype's template and fake-API tests (`test_resolve_script_against_fake_documented_api`, `test_fake_resolve_rejects_undocumented_calls`); the CLI `export` gains `--resolve-script` to write `open_in_resolve.py` next to the exports.
- [ ] `docs/INTEROP.md`: what each format carries (the fidelity matrix), how ids and provenance travel, export/import usage, the human stage, Resolve paths (free GUI import of OTIO/FCPXML; Studio scripting; MCP not used and why), FCP.
- [ ] Demonstration: run `export` (+ `--resolve-script`) on a synthetic mock workspace and on the Big Buck Bunny mock workspace whose path the controller gives; commit nothing binary except small text outputs worth keeping as examples (`docs/examples/m2a/synthetic/final.otio`, `matchcut.fcpxml`, `final.edl`, `fidelity.md`, `provenance.json` trimmed if > 100 KB) — synthetic only (the BBB paths are machine-specific); simulate an editor round trip (edit the exported OTIO as a script would, import it) and include the resulting human decisions in the acceptance doc.
- [ ] `docs/M2A-ACCEPTANCE.md`: what M2a adds; the fidelity matrix (preserved/approximated/lost per format); automated evidence (test modules); demonstration; human checklist (PENDING: open `final.otio` and `matchcut.fcpxml` in Resolve free and Studio, FCP; check dissolves, markers, notes, start timecode; re-export after an edit and import; run the Resolve script in Studio); open questions from the feasibility study; status lines (M1, M2b, M2a human gates PENDING).
- [ ] All suites (prototypes suite removed), commit/push ("Prepare Resolve, document interoperability and its fidelity limits").

After Task 5 the controller runs the final whole-branch review and an independent tester who writes `docs/M2A-TEST-REPORT.md` from a fresh run of every suite and the CLI on fresh workspaces.
