"""A whole workspace out to editors and an editor's re-export back in.

``export_workspace`` writes ``<stage>.otio``, ``matchcut.fcpxml`` (one project per stage), ``<stage>.edl``, ``provenance.json`` and the
fidelity report, and validates every file by reading it back with matchcut's own readers.

``import_timeline`` reads an OTIO, FCPXML or EDL file, finds the canonical stage it was exported from, and writes the **human stage**:
``human.json`` (a stage timeline, always listed after ``final``) whose history is the canonical history plus one ``human`` decision per
change the editor made (trim, transition, removed, added, reorder), and ``human_import.json`` (what was read, the import report, the
stage it was compared with and the latest canonical stage whose history it copies, each with the sha256 of its file: the engine's
``junctions.stale_human`` leaves the human stage out once either changes).  Canonical stage files and ``provenance.json`` are only ever read.

The human stage continues the history of the LATEST canonical stage, which is the stage it is listed after: the junction code reads a
stage's new decisions as ``history[len(previous stage's history):]``, so editor pins and the attribution of every earlier decision stay
valid, and the pin badge of an untouched junction does not change.  For the usual import (against ``final``) that is exactly the history of
the stage that was imported against.

The **edit stage** (``edit.json``, an agent's published edit session: ``matchcut.edit``) is exported when named (``stages=("edit",)``;
never by default, like ``human``, which is never exported): ``edit.otio``, the ``edit`` project of ``matchcut.fcpxml`` and ``edit.edl``,
each with the stage fingerprint (the sha256 of ``edit.json``) and a snapshot ``exports/.stages/edit-<sha12>.json``.  A fresh edit stage is
listed between the canonical stages and ``human``, so the human stage then continues the edit stage's history (``history_stage`` in
``human_import.json``).  Once a workspace has an edit stage, an import's base is never assumed (``import_timeline``): the file's
fingerprint names the exact version, else ``--stage`` and ``--base`` must.
"""
from __future__ import annotations

import ast
import contextlib
import fcntl
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from matchcut.pipeline.config import MusicConfig, PipelineConfig
from matchcut.pipeline.editorial import Pin, pin_change
from matchcut.pipeline.media import MediaError, probe as probe_media
from matchcut.pipeline.junctions import (EDIT_FILE, Junction, build_junctions, load_stage_timelines, scorer_for_workspace, stale_edit,
                                       stale_human, stage_timeline_paths)
from matchcut.pipeline.shots import load_shots
from matchcut.pipeline.timeline import EDIT_STAGE, Clip, Decision, Timeline, Transition, clip_origin

from . import InterchangeError, fidelity, resolve
from .common import LaidClip, SourceInfo, clip_key, export_timeline, is_drop_rate, junction_ids, layout, plain, source_info, truncate
from .edl import export_edl, import_edl, placement_errors as edl_placement, stage_fingerprint as edl_fingerprint
from .fcpxml import export_fcpxml, fcpxml_project_names, fcpxml_stages, import_fcpxml, placement_errors as fcpxml_placement, stage_fingerprint as fcpxml_fingerprint
from .fidelity import FORMATS, FidelityResult
from .otio import export_otio, import_otio, placement_errors as otio_placement, stage_fingerprint as otio_fingerprint
from .provenance import write_provenance

HUMAN = "human"
EDIT = EDIT_STAGE  # an agent's published edit session (matchcut.edit writes edit.json): exported when named, never by default
DEFAULT_STAGES: Tuple[str, ...] = ("roughcut", "final")
RESOLVE_SCRIPT = "open_in_resolve.py"
STAGE_LOCK = ".stage.lock"  # WS/.stage.lock: held while the edit or the human stage is written (publish) or read and written (import)
SNAPSHOT_DIR = ".stages"  # <exports>/.stages/<stage>-<first 12 hex of the stage file's sha256>.json: the stage as it was exported
IMPORT_SCHEMA = "matchcut.human_import/1"
_PLACEMENT_NOTE = re.compile(r"^(gap\b|event \S+ starts \d+ frames before)")  # the readers' notes about a gap or an overlap on the record side
_EXTENSIONS = {".otio": "otio", ".fcpxml": "fcpxml", ".fcpxmld": "fcpxml", ".xml": "fcpxml", ".edl": "edl"}
BUNDLE_FILE = "Info.fcpxml"  # inside a Final Cut Pro .fcpxmld bundle (a directory)


@dataclass
class ExportResult:
    files: List[Path]
    results: List[FidelityResult]
    notes: List[str] = field(default_factory=list)


@dataclass
class ImportResult:
    stage_path: Path
    decisions: List[Decision]
    notes: List[str] = field(default_factory=list)
    stage: str = ""  # the canonical stage the file was compared with
    fmt: str = ""


# -- reading the workspace -------------------------------------------------------------------------
def _canonical(ws: Path) -> List[Tuple[str, Timeline]]:
    """The canonical stage timelines (every stage but ``edit`` and ``human``), in stage order."""
    return [(s, t) for s, t in load_stage_timelines(ws) if s not in (HUMAN, EDIT)]


def _edit(ws: Path) -> Optional[Timeline]:
    """The edit stage's timeline while it is listed (``edit.json`` present and fresh) and readable, else None."""
    return next((t for s, t in load_stage_timelines(ws) if s == EDIT), None)


def _no_edit(ws: Path, what: str) -> str:
    """Why the edit stage cannot be ``what`` (exported, imported against): stale, unreadable or never published."""
    why = stale_edit(ws)
    if why:
        return f"cannot {what} the edit stage: {why}"
    if (Path(ws) / EDIT_FILE).exists():
        return f"cannot {what} the edit stage: {EDIT_FILE} is unreadable"
    return f"cannot {what} the edit stage: no {EDIT_FILE} in {ws} (publish an edit session first: python -m matchcut.edit WS publish)"


def _bounds(ws: Path) -> Optional[Dict[int, Tuple[float, float]]]:
    try:
        return {s.index: (s.start, s.end) for s in load_shots(ws / "shots.json")}
    except Exception:
        return None


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _source(ws: Path, cfg: PipelineConfig) -> Tuple[SourceInfo, List[str]]:
    """The source footage and the notes about choosing it.

    The media the workspace's stage timelines were made from wins: ``probe.json``'s path when that is a file, even if ``config.json`` now names another
    file (its ranges describe the probed footage; a note says so).  When the probed file is gone (the footage moved), ``config.json``'s file is used
    only if it has the probed footage's identity (frame rate, duration within a frame, size when ``probe.json`` records it); otherwise a one-line
    error names both.  Without ``probe.json`` the configured file is the footage.
    """
    probe: Dict[str, Any] = {}
    try:
        doc = json.loads((ws / "probe.json").read_text())
        probe = doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        pass
    probed, configured = probe.get("path"), cfg.source
    if not probed:
        if configured and Path(configured).is_file():
            return source_info(str(configured)), []
        raise InterchangeError(f"source footage not found: {configured or '(no source configured)'}")
    if Path(probed).is_file():
        notes: List[str] = []
        if configured and Path(configured).is_file() and not _same_file(configured, probed):
            notes.append(f"config.json names {configured}, but this workspace was analysed from {probed}: exported/imported against {probed}; "
                         f"re-run with --force (or force in Config) to analyse {configured}")
        return source_info(str(probed)), notes
    if not configured or not Path(configured).is_file():
        raise InterchangeError(f"source footage not found: {configured or probed}")
    try:  # the same probe that wrote probe.json, so every field is compared like for like (average frame rate, the container's duration)
        found = probe_media(str(configured))
    except (MediaError, ValueError, KeyError, OSError) as exc:
        raise InterchangeError(f"source footage not found: {probed} (the analysed footage); config.json names {configured}, which cannot be probed: "
                               f"{str(exc).splitlines()[0] if str(exc) else type(exc).__name__}") from exc
    compared: List[str] = []
    differs: List[str] = []
    try:
        fps, seconds = probe.get("fps"), probe.get("duration")
        if fps is not None:
            compared.append("frame rate")
            if abs(found.fps - float(fps)) > 1e-3 * max(float(fps), 1.0):
                differs.append(f"frame rate {found.fps:g} fps, analysed at {float(fps):g}")
        if seconds is not None:
            compared.append("duration")
            if abs(found.duration - float(seconds)) > 1.0 / max(float(fps) if fps else found.fps, 1.0) + 1e-6:
                differs.append(f"duration {found.duration:.2f} s, analysed at {float(seconds):.2f}")
        if probe.get("width") is not None and probe.get("height") is not None:
            compared.append("size")
            if (found.width, found.height) != (int(probe["width"]), int(probe["height"])):
                differs.append(f"size {found.width}x{found.height}, analysed at {probe['width']}x{probe['height']}")
    except (TypeError, ValueError, ZeroDivisionError):
        differs.append("probe.json is not readable")
    if differs:
        raise InterchangeError(f"source footage not found: {probed} (the analysed footage); config.json names {configured}, which is not the same footage: " + "; ".join(differs))
    info = source_info(str(configured))
    what = ("has its " + ", ".join(compared[:-1]) + (" and " if len(compared) > 1 else "") + compared[-1]) if compared else "could not be compared (probe.json records no frame rate, duration or size)"
    return info, [f"the analysed footage {probed} is missing; using {configured}, which {what}"]


def _music(cfg: PipelineConfig) -> Tuple[Optional[MusicConfig], Optional[SourceInfo]]:
    if not cfg.music.path:
        return None, None
    if not Path(cfg.music.path).is_file():
        raise InterchangeError(f"music file not found: {cfg.music.path}")
    return cfg.music, source_info(cfg.music.path, audio_only=True)


def _fps(rate: Fraction) -> str:
    return f"{float(rate):.3f}".rstrip("0").rstrip(".")


def _unique(items: Sequence[str]) -> List[str]:
    return list(dict.fromkeys(items))


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise InterchangeError(f"cannot write {path}: {exc.strerror or exc}") from exc


def _sha256(path: Optional[Path]) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest() if path is not None else None
    except OSError:
        return None


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(Path(path).read_text())
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def _snapshot_name(stage: str, sha: str) -> str:
    return f"{stage}-{sha[:12]}.json"


def _save_snapshot(out: Path, stage: str, data: bytes) -> None:
    """Keep a copy of the stage file as exported in ``<out>/.stages`` (atomically; matchcut never deletes it): an editor's file made from this
    version of the stage is diffed against the copy even after the pipeline has run again."""
    target = out / SNAPSHOT_DIR / _snapshot_name(stage, hashlib.sha256(data).hexdigest())
    try:
        if target.exists() and target.read_bytes() == data:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    except OSError as exc:
        raise InterchangeError(f"cannot write {target}: {exc.strerror or exc}") from exc


def _shown(ws: Path, path: Path) -> str:
    """``path`` as the workspace-relative text a message names, else as it is."""
    try:
        return str(path.resolve().relative_to(Path(ws).resolve()))
    except ValueError:
        return str(path)


def _load_snapshot(ws: Path, imported: Path, stage: str, sha: str) -> Tuple[Path, Timeline]:
    """The snapshot of ``stage`` as exported (``sha``), loaded: in the workspace's ``exports/.stages``, else in ``.stages`` next to the imported
    file (an export made with ``--out DIR`` keeps its snapshots in ``DIR/.stages``).  A copy whose content does not hash to ``sha`` is not
    used; a good copy in the second place is found even when the first holds a damaged one.  Raises a one-line ``InterchangeError``: missing
    (naming where it looked) or damaged (the hash does not match, or the JSON cannot be read)."""
    name = _snapshot_name(stage, sha)
    folders = list(dict.fromkeys([ws / "exports" / SNAPSHOT_DIR, imported.resolve().parent / SNAPSHOT_DIR]))
    head = f"{imported.name} was exported from an earlier {stage} (sha {sha[:12]}) and its snapshot "
    damaged: Optional[str] = None
    for folder in folders:
        candidate = folder / name
        if not candidate.is_file():
            continue
        if _sha256(candidate) != sha:
            damaged = damaged or f"{_shown(ws, candidate)} is damaged (sha256 mismatch)"
            continue
        try:
            return candidate, Timeline.load(candidate)
        except Exception:
            damaged = damaged or f"{_shown(ws, candidate)} is damaged (unreadable JSON)"
    if damaged:
        raise InterchangeError(f"{head}{damaged}: re-export and redo the edit")
    raise InterchangeError(f"{head}{name} is missing (searched {' and '.join(_shown(ws, f) for f in folders)}): re-export and redo the edit, or restore the snapshot")


def _record_files(out: Path, stage: str, fmt: Optional[str] = None) -> List[str]:
    """The exported files of ``stage`` (of ``fmt``, when given) that are in ``out``; for FCPXML, only while ``matchcut.fcpxml`` still has the
    stage's project (named ``matchcut.fcpxml (project <stage>)``)."""
    names: List[str] = []
    for f in (FORMATS if fmt is None else (fmt,)):
        if f == "fcpxml":
            try:
                if stage in fcpxml_stages((out / "matchcut.fcpxml").read_text(encoding="utf-8")):
                    names.append(f"matchcut.fcpxml (project {stage})")
            except (OSError, InterchangeError):
                pass
        elif (out / f"{stage}.{f}").exists():
            names.append(f"{stage}.{f}")
    return names


def _merge_provenance(path: Path, doc: Dict[str, Any], old: Optional[Dict[str, Any]], order: Sequence[str], hashes: Dict[str, Optional[str]],
                      stages: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Stamp each exported stage of ``doc`` with the sha256 of its stage file and carry over, from the provenance.json that was there, the
    stages not exported now (same source and rate): kept as they are while their stage file is unchanged, and kept flagged ``"stale": true``
    once it changed, for as long as exported files of that stage are still in the directory.  Rewrites ``path``; returns (kept, stale)."""
    for s in stages:
        doc["stages"][s]["canonical_sha256"] = hashes[s]
    kept: List[str] = []
    stale: List[str] = []
    extra: List[str] = []
    if old and old.get("source") == doc.get("source") and old.get("rate") == doc.get("rate") and isinstance(old.get("stages"), dict):
        for s, entry in old["stages"].items():
            if s in doc["stages"] or not isinstance(entry, dict):
                continue
            if hashes.get(s) and entry.get("canonical_sha256") == hashes[s]:
                doc["stages"][s] = {k: v for k, v in entry.items() if k != "stale"}
                kept.append(s)
            elif _record_files(path.parent, s):
                doc["stages"][s] = dict(entry, stale=True)
                stale.append(s)
            else:
                continue
            if s not in order:
                extra.append(s)
    doc["stage_order"] = [s for s in list(order) + extra if s in doc["stages"]]
    doc["stages"] = {s: doc["stages"][s] for s in doc["stage_order"]}
    path.write_text(json.dumps(plain(doc), indent=1))
    return kept, stale


def _carried_results(old: Optional[Dict[str, Any]], out: Path, hashes: Dict[str, Optional[str]], now: set) -> List[FidelityResult]:
    """The entries of an earlier fidelity.json that this export does not replace and whose exported file is still there (for FCPXML, still
    has the stage's project).  An entry whose stage file is unchanged is carried as it was; one whose stage file changed since is carried
    flagged ``stale`` (it describes a file that no longer matches the stage)."""
    carried: List[FidelityResult] = []
    stages = (old or {}).get("stages")
    if not isinstance(stages, dict):
        return carried
    for stage, by_fmt in stages.items():
        for fmt, d in (by_fmt.items() if isinstance(by_fmt, dict) else []):
            try:
                if (stage, fmt) in now or fmt not in FORMATS or not _record_files(out, stage, fmt):
                    continue
                res = FidelityResult.from_dict(d)
                res.stale = not (hashes.get(stage) and d.get("canonical_sha256") == hashes[stage])
                carried.append(res)
            except (OSError, InterchangeError, KeyError, TypeError, AttributeError):
                continue
    return carried


# -- export ------------------------------------------------------------------------------------------
def _script_target(out: Path) -> Optional[str]:
    """The stage an existing ``open_in_resolve.py`` in ``out`` imports (the stem of its ``TIMELINE_FILE``), or None."""
    try:
        text = (out / RESOLVE_SCRIPT).read_text(encoding="utf-8")
        found = re.search(r"^TIMELINE_FILE = (.+)$", text, re.M)
        name = Path(ast.literal_eval(found.group(1))).name if found else ""
    except (OSError, ValueError, SyntaxError):
        return None
    return name[: -len(".otio")] if name.endswith(".otio") and len(name) > len(".otio") else None


def _resolve_script_text(out: Path, stage: str, tl: Timeline, junctions: Sequence[Junction], src: SourceInfo, name: str) -> str:
    """The Resolve script for ``<out>/<stage>.otio``: a marker per junction at the cut frame the OTIO file carries, the playhead at the first
    junction with a transition (else the first).  The timeline starts at 00:00:00:00, which is what every exporter writes."""
    rows = layout(tl, src.rate)
    ids = junction_ids(tl)
    frames = {ids[j.index]: rows[j.index + 1].cut for j in junctions if j.index + 1 < len(rows)}
    notes = {ids[j.index]: (f"J{j.index + 1} s{j.out_shot}->s{j.in_shot}", truncate(j.why())) for j in junctions if ids[j.index] in frames}
    nav = next((ids[j.index] for j in junctions if ids[j.index] in frames and rows[j.index + 1].tr), next(iter(frames), ""))
    return resolve.resolve_script(str((out / f"{stage}.otio").resolve()), f"{name} - {stage}", frames, nav, "00:00:00:00", src.rate, notes)


def export_workspace(ws: Path, cfg: PipelineConfig, stages: Sequence[str] = DEFAULT_STAGES, formats: Sequence[str] = FORMATS,
                     out_dir: Optional[Path] = None, resolve_script: bool = False) -> ExportResult:
    """Write the interchange files of ``stages`` in ``formats`` (default ``<ws>/exports``), validate each by reading it back, write the
    reports, and fail with a one-line ``InterchangeError`` if any export does not match its canonical stage (after the reports exist).

    ``resolve_script`` also writes ``open_in_resolve.py`` (``resolve.resolve_script``) for the ``final`` stage's OTIO file, else the last
    stage exported; it needs the otio format.  matchcut never runs it."""
    ws = Path(ws)
    stages, formats = _unique(stages), _unique(formats)
    if not stages:
        raise InterchangeError("no stage to export")
    if not formats:
        raise InterchangeError("no format to export")
    for f in formats:
        if f not in FORMATS:
            raise InterchangeError(f"unknown format {f}: use otio, fcpxml or edl")
    formats = [f for f in FORMATS if f in formats]
    if resolve_script and "otio" not in formats:
        raise InterchangeError("--resolve-script needs the otio format: Resolve imports the OTIO file")
    stage_files = dict(stage_timeline_paths(ws))
    # the edit stage (while listed) comes after the canonical stages: its junctions follow them, and its earlier records are kept while
    # edit.json is unchanged, as a canonical stage's are
    edit = _edit(ws)
    if EDIT in stages and edit is None:
        raise InterchangeError(_no_edit(ws, "export"))
    canonical = _canonical(ws) + ([(EDIT, edit)] if edit is not None else [])
    by_stage = dict(canonical)
    for s in stages:
        if s == HUMAN:
            raise InterchangeError("the human stage is an import result, not a canonical stage: export one of the stages it was imported against")
        if s not in by_stage:
            raise InterchangeError(f"cannot read stage {s} ({stage_files[s]})" if s in stage_files and stage_files[s].exists() else f"no stage {s} in {ws}")
    src, notes = _source(ws, cfg)
    bounds = _bounds(ws)
    music, music_src = _music(cfg)
    out = Path(out_dir) if out_dir else ws / "exports"
    if bounds is None:
        notes.append("no shots.json: clips were snapped to the frame grid without shot bounds")
    if music is not None and music.only:
        notes.append("music.only: the source audio is muted in the render, so the exports mute it (OTIO track A1 disabled, FCPXML srcEnable=video, "
                     f"EDL V events); the music file itself is carried by OTIO and FCPXML only ({Path(music.path).name})")
    for fps in _unique([f"{by_stage[s].fps:g}" for s in stages if abs(by_stage[s].fps - float(src.rate)) > 1e-3 * float(src.rate)]):
        # the pipeline reads avg_frame_rate, the exports r_frame_rate (the container's base rate): they differ for some streams (raw H.264, variable rate)
        notes.append(f"frame rate: ffprobe reports {_fps(src.rate)} fps (r_frame_rate) but the pipeline cut at {fps} fps (avg_frame_rate): "
                     f"the exports are written at {_fps(src.rate)} fps, so a timecode base or frame count may not match the pipeline's")
    scorer = scorer_for_workspace(ws, cfg)
    junctions: Dict[str, List[Junction]] = {s: build_junctions(canonical, scorer, upto=s) for s in stages}
    # the stage files as they are now: their hash is the fingerprint every export carries, and a copy of each is kept (see _save_snapshot)
    stage_bytes: Dict[str, bytes] = {}
    for s in stages:
        try:
            stage_bytes[s] = stage_files[s].read_bytes()
        except OSError as exc:
            raise InterchangeError(f"cannot read stage {s} ({stage_files[s]}): {exc.strerror or exc}") from exc
    fingerprint = {s: hashlib.sha256(b).hexdigest() for s, b in stage_bytes.items()}
    for s in stages:
        _save_snapshot(out, s, stage_bytes[s])
    snapped: Dict[str, Timeline] = {}
    for s in stages:
        snapped[s], moved = export_timeline(by_stage[s], bounds, src)
        if moved:
            notes.append(f"{s}: {len(moved)} of {len(by_stage[s].clips)} clips moved by the frame snap (see fidelity.md)")
    stem = Path(src.path).stem
    files: List[Path] = []
    texts: Dict[Tuple[str, str], str] = {}
    if "otio" in formats:
        for s in stages:
            doc = export_otio(snapped[s], s, src, junctions[s], music, music_src, fingerprint[s])
            path = out / f"{s}.otio"
            _atomic_write(path, json.dumps(doc, indent=1) + "\n")
            files.append(path)
            texts[(s, "otio")] = path.read_text(encoding="utf-8")
    if "fcpxml" in formats:
        path = out / "matchcut.fcpxml"
        _atomic_write(path, export_fcpxml([(s, snapped[s], junctions[s]) for s in stages], src, music, music_src, name=stem, stage_sha256=fingerprint))
        files.append(path)
        for s in stages:
            texts[(s, "fcpxml")] = path.read_text(encoding="utf-8")
    if "edl" in formats:
        for s in stages:
            path = out / f"{s}.edl"
            _atomic_write(path, export_edl(snapped[s], s, src, f"{stem} - {s}", music, fingerprint[s]))
            files.append(path)
            texts[(s, "edl")] = path.read_text(encoding="utf-8")
    # open_in_resolve.py is derived output: with the flag it is written for the final stage (else the last exported); without it an existing
    # script is regenerated whenever the OTIO of the stage it targets is exported again, so it never points at frames the file no longer has
    target = _script_target(out)
    pick = ("final" if "final" in stages else stages[-1]) if resolve_script else (target if target in stages and "otio" in formats else None)
    if pick:
        path = out / RESOLVE_SCRIPT
        _atomic_write(path, _resolve_script_text(out, pick, snapped[pick], junctions[pick], src, stem))
        files.append(path)
        if resolve_script:
            notes.append(f"{RESOLVE_SCRIPT} imports {pick}.otio into Resolve Studio (external scripting = Local); matchcut does not run it")
        else:
            notes.append(f"{RESOLVE_SCRIPT} regenerated for the new {pick}.otio")
    prov = out / "provenance.json"
    hashes = {s: fingerprint.get(s) or _sha256(stage_files[s]) for s in by_stage}  # an exported stage: the hash its files carry
    old_prov, old_fidelity = _read_json(prov), _read_json(out / "fidelity.json")
    doc = write_provenance(prov, [(s, by_stage[s]) for s in stages], junctions, src)
    kept, stale_prov = _merge_provenance(prov, doc, old_prov, [s for s, _ in canonical], hashes, stages)
    files.append(prov)
    # validation: what was written, read back by matchcut's own readers, against the stage
    results: List[FidelityResult] = []
    for s in stages:
        for fmt in formats:
            try:
                rows = layout(snapped[s], src.rate)
                if fmt == "otio":
                    doc_back = json.loads(texts[(s, fmt)])
                    back, rnotes = import_otio(doc_back, bounds, src)
                    where = otio_placement(doc_back, rows, src.rate)
                elif fmt == "fcpxml":
                    back, rnotes = import_fcpxml(texts[(s, fmt)], s, bounds, src)
                    where = fcpxml_placement(texts[(s, fmt)], s, rows, src.rate)
                else:
                    back, rnotes = import_edl(texts[(s, fmt)], src.rate, src.path, bounds, src)
                    where = edl_placement(texts[(s, fmt)], rows, src.rate)
                # matchcut's own file has no gap and no overlap: a reader note about one is a placement error
                where = [f"the file reads back with a {n}" for n in rnotes if _PLACEMENT_NOTE.match(n)] + where
            except InterchangeError as exc:
                bad = FidelityResult(fmt, s, errors=[f"the file cannot be read back: {exc}"])
                bad.lost = fidelity.known(fmt)["lost"]
                results.append(bad)
                continue
            # OTIO and FCPXML carry the music clip, placed on the video frame grid; an EDL carries no music
            moved = fidelity.music_start_note(music, src.rate) if music is not None and fmt != "edl" else None
            res = fidelity.check(by_stage[s], back, fmt, src.rate, snapped[s], drop_frame=bool(src.start_tc_drop and is_drop_rate(src.rate)), placement=where,
                                 extra_approximated=[moved] if moved else [])
            res.stage = s
            results.append(res)
    for r in results:
        r.canonical_sha256 = hashes[r.stage] or ""
    carried = _carried_results(old_fidelity, out, hashes, {(r.stage, r.fmt) for r in results})
    order = {s: k for k, (s, _) in enumerate(canonical)}
    everything = sorted(results + carried, key=lambda r: (order.get(r.stage, len(order)), FORMATS.index(r.fmt)))
    files += list(fidelity.write_report(everything, out))
    current = [r for r in carried if not r.stale]
    if kept or current:
        notes.append("the earlier records of this directory are kept where their stage file is unchanged: "
                     + ", ".join([f"provenance {s}" for s in kept] + [f"fidelity {r.stage}/{r.fmt}" for r in current]))
    # a stage's provenance record is stale as a whole; a fidelity entry is stale for its own format's file only (the stage may have been
    # exported again in another format since: that file is current, and provenance.json describes the new export)
    fid_files = _unique([n for r in carried if r.stale for n in _record_files(out, r.stage, r.fmt)])
    prov_files = _unique([n for s in stale_prov for n in _record_files(out, s)])
    script_stale = bool(target) and (out / RESOLVE_SCRIPT).exists() and any(r.stale and r.stage == target and r.fmt == "otio" for r in carried)
    if fid_files or prov_files or script_stale:
        parts = []
        if fid_files:
            parts.append("stale in fidelity.json: " + ", ".join(fid_files))
        if prov_files:
            parts.append("stale in provenance.json: " + ", ".join(_unique(stale_prov)) + f" ({', '.join(prov_files)})")
        again = _unique([r.stage for r in carried if r.stale and r.stage in stages and r.stage not in stale_prov])
        if again:
            parts.append("provenance.json now describes the new export of " + ", ".join(again))
        if script_stale:
            parts.append(f"{RESOLVE_SCRIPT} was made for the old {target}.otio")
        notes.append("the stage file changed since these were exported (export the stage again to refresh them); " + "; ".join(parts))
    for stale in (stale_edit(ws), stale_human(ws)):
        if stale:
            notes.append(stale)
    for r in results:
        if r.errors:
            raise InterchangeError(f"export of {r.fmt}/{r.stage} does not match the canonical timeline: {r.errors[0]}")
    return ExportResult(files, results, notes)


# -- import: the human edits ------------------------------------------------------------------------------
def _overlap(a: LaidClip, b: LaidClip) -> int:
    return min(a.out_f, b.out_f) - max(a.in_f, b.in_f)


@dataclass
class _Diff:
    match: Dict[int, int]  # canonical clip index -> edited clip index
    ids: List[str]  # id of every edited clip: the canonical id of its match, else a fresh h<n>
    decisions: List[Decision]
    crows: List[LaidClip]
    erows: List[LaidClip]
    by_range: int  # matches made by source range because no clip key told
    notes: List[str] = field(default_factory=list)  # ambiguous clips read as new (see _match), the new clips' ids filled in
    reshot: Dict[int, int] = field(default_factory=dict)  # edited clip index -> the shot its range lies in, for a key that went to other footage
    blocked: set = field(default_factory=set)  # edited clips read as new on purpose (see _match)


def _list(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _shot_at(bounds: Dict[int, Sequence[float]], start: float, end: float) -> int:
    """The shot a source range overlaps most (-1 when it lies in none)."""
    best = max(((max(0.0, min(hi, end) - max(lo, start)), s) for s, (lo, hi) in bounds.items()), default=(0.0, -1))
    return best[1] if best[0] > 0 else -1


def _match(canon: Timeline, edited: Timeline, crows: Sequence[LaidClip], erows: Sequence[LaidClip],
           bounds: Optional[Dict[int, Sequence[float]]] = None) -> Tuple[Dict[int, int], int, List[Tuple[str, List[int]]], Dict[int, int], set]:
    """canonical clip index -> edited clip index, one to one: by clip key (the lineage id the exports carry) first, then by lineage
    (same shot, overlapping source range, best overlap first) for what the key did not settle.

    Also how many matches the range made and the notes (text with a ``{new}`` placeholder for the ids the new clips get, and the edited clips
    they are about).  An ambiguous clip is read as new on purpose, never guessed: a key that several edited clips carry (a copied clip) does
    not say which one is the original, so none is taken for it; an edited clip with no key to decide that overlaps two canonical clips
    equally (the same number of frames) is not taken for either.  A known key is trusted only while the edited range still overlaps the
    canonical clip's shot (``bounds``): a key carried onto the footage of another shot is a new clip (a slip inside the shot is a trim).
    Then a map from such a clip to the shot its range lies in, and the set of edited clips read as new on purpose.  A canonical clip whose
    best unkeyed candidates share its range equally (a razored clip) matches none of them.
    """
    out: Dict[int, int] = {}
    used: set = set()
    blocked: set = set()
    notes: List[Tuple[str, List[int]]] = []
    reshot: Dict[int, int] = {}
    keyed: Dict[str, List[int]] = {}
    for j, e in enumerate(edited.clips):
        if e.origin:
            keyed.setdefault(e.origin, []).append(j)
    for i, c in enumerate(canon.clips):
        carriers = keyed.get(clip_key(c), [])
        if len(carriers) == 1 and carriers[0] not in used:
            j = carriers[0]
            e = edited.clips[j]
            lo_hi = bounds.get(c.shot_index) if bounds else None
            if lo_hi is not None and min(e.source_out, lo_hi[1]) - max(e.source_in, lo_hi[0]) <= 0:
                where = _shot_at(bounds, e.source_in, e.source_out)
                reshot[j] = where
                blocked.add(j)
                notes.append((f"clip key {clip_key(c)} of {c.id} now covers " + (f"shot {where}" if where >= 0 else "no shot")
                              + f" ({e.source_in:.2f}-{e.source_out:.2f}s), not shot {c.shot_index}: read as a new clip ({{new}}), not matched to {c.id}", [j]))
                continue
            if lo_hi is None and min(e.source_out, c.source_out) - max(e.source_in, c.source_in) <= 0:
                notes.append((f"could not check that clip key {clip_key(c)} of {c.id} is still shot {c.shot_index} (no shots.json): its range "
                              f"{e.source_in:.2f}-{e.source_out:.2f}s no longer overlaps the clip's", []))
            out[i] = j
            used.add(j)
        elif len(carriers) > 1:
            blocked.update(carriers)
            notes.append((f"clip key {clip_key(c)} of {c.id} is used by clips {_list([str(j + 1) for j in carriers])} of the file: "
                          f"read as new clips ({{new}}), not matched to {c.id}", list(carriers)))
    pending = [i for i in range(len(canon.clips)) if i not in out]
    for j, e in enumerate(edited.clips):
        if j in used or j in blocked:
            continue
        overlaps = [(_overlap(crows[i], erows[j]), i) for i in pending if canon.clips[i].shot_index == e.shot_index]
        best = max((ov for ov, _ in overlaps), default=0)
        tied = [i for ov, i in overlaps if ov == best] if best > 0 else []
        if len(tied) > 1:
            blocked.add(j)
            notes.append((f"clip {j + 1} of the file (shot {e.shot_index}, {e.source_in:.2f}-{e.source_out:.2f}s) overlaps "
                          f"{_list([canon.clips[i].id for i in tied])} equally: read as a new clip ({{new}}), not matched to any of them", [j]))
    for i in pending:  # a canonical clip whose best candidates tie (a clip razored into equal halves): none of them is taken for it
        c = canon.clips[i]
        found = [(_overlap(crows[i], erows[j]), j) for j, e in enumerate(edited.clips) if j not in used and j not in blocked and e.shot_index == c.shot_index
                 and all(_overlap(crows[i], erows[j]) >= _overlap(crows[o], erows[j]) for o in pending)]  # (a candidate that fits another canonical clip better is that clip's)
        best = max((ov for ov, _ in found), default=0)
        tied = [j for ov, j in found if ov == best] if best > 0 else []
        if len(tied) > 1:
            blocked.update(tied)
            notes.append((f"{c.id}'s range is shared equally by clips {_list([str(j + 1) for j in tied])} of the file: "
                          f"read as new clips ({{new}}), not matched to {c.id}", tied))
    pairs = []
    for i in pending:
        c = canon.clips[i]
        for j, e in enumerate(edited.clips):
            ov = _overlap(crows[i], erows[j])
            if j not in used and j not in blocked and c.shot_index == e.shot_index and ov > 0:
                pairs.append((-ov, abs(i - j), i, j))
    by_range = 0
    for _, _, i, j in sorted(pairs):
        if i not in out and j not in used:
            out[i] = j
            used.add(j)
            by_range += 1
    return out, by_range, notes, reshot, blocked


def _fresh_ids(canon: Timeline, edited: Timeline, match: Dict[int, int]) -> List[str]:
    ids: List[Optional[str]] = [None] * len(edited.clips)
    for i, j in match.items():
        ids[j] = canon.clips[i].id
    taken = {c.id for c in canon.clips}
    n = 0
    for j in range(len(ids)):
        if ids[j] is None:
            n += 1
            while f"h{n:03d}" in taken:
                n += 1
            ids[j] = f"h{n:03d}"
            taken.add(ids[j])
    return [str(x) for x in ids]


def _compare(canonical: Timeline, edited: Timeline, rate: Fraction, bounds: Optional[Dict[int, Sequence[float]]] = None) -> _Diff:
    crows, erows = layout(canonical, rate), layout(edited, rate)
    match, by_range, found, reshot, blocked = _match(canonical, edited, crows, erows, bounds)
    ids = _fresh_ids(canonical, edited, match)
    notes = [text.replace("{new}", ", ".join(ids[j] for j in js)) for text, js in found]
    inv = {j: i for i, j in match.items()}
    out: List[Decision] = []

    def decide(action: str, cids: List[str], detail: str, **data: Any) -> None:
        out.append(Decision("human", action, cids, detail, data=data))

    for i, c in enumerate(canonical.clips):
        if i not in match:
            decide("removed", [c.id], f"human removed {c.id} (shot {c.shot_index}, {c.source_in:.2f}-{c.source_out:.2f}s)",
                   shot_index=c.shot_index, source_in=c.source_in, source_out=c.source_out)
    for j, e in enumerate(edited.clips):
        if j not in inv:
            shot = reshot.get(j, e.shot_index)
            decide("added", [ids[j]], f"human added {ids[j]} (shot {shot}, {e.source_in:.2f}-{e.source_out:.2f}s)",
                   shot_index=shot, source_in=e.source_in, source_out=e.source_out)
    order = [inv[j] for j in range(len(edited.clips)) if j in inv]
    if order != sorted(order):
        decide("reorder", [canonical.clips[i].id for i in order], "human reordered clips: " + " ".join(canonical.clips[i].id for i in order))
    for j in range(len(edited.clips)):
        if j not in inv:
            continue
        i, cid = inv[j], ids[j]
        # what the editor shows (source range after the transition trims), so that turning a dissolve into a cut is not also a trim
        d_in, d_out = erows[j].vis_in - crows[i].vis_in, erows[j].vis_out - crows[i].vis_out
        if d_in or d_out:
            parts = ([f"in {d_in:+d}f"] if d_in else []) + ([f"out {d_out:+d}f"] if d_out else [])
            decide("trim", [cid], f"human trimmed {cid}: " + ", ".join(parts), in_frames=d_in, out_frames=d_out)
        if j and inv.get(j - 1) == i - 1 and i > 0:  # the same neighbour as before: compare the junction's transition
            before, after = (crows[i].kind, crows[i].tr), (erows[j].kind, erows[j].tr)
            if before != after:
                label = lambda t: "cut" if not t[1] else f"{t[0]} {t[1]}f"  # noqa: E731
                decide("transition", [ids[j - 1], cid], f"human changed transition {label(before)} -> {label(after)}",
                       before={"kind": before[0], "frames": before[1]}, after={"kind": after[0], "frames": after[1]})
    return _Diff(match, ids, out, crows, erows, by_range, notes, reshot, blocked)


def diff_human_edits(canonical: Timeline, edited: Timeline, rate: Fraction, bounds: Optional[Dict[int, Sequence[float]]] = None) -> List[Decision]:
    """The human decisions that turn ``canonical`` into ``edited`` (trim, transition, removed, added, reorder).

    Both are on the frame grid of ``rate`` (``canonical`` is normally the frame-snapped copy that was exported).  Clip ids in the
    decisions are the canonical ids (an added clip: ``h<n>``).  Neither timeline is modified.  ``bounds`` (shot -> (start, end), as in
    ``shots.json``) lets a clip key that went to the footage of another shot be told from a trim.
    """
    return _compare(canonical, edited, rate, bounds).decisions


def _build(canonical: Timeline, snapped: Timeline, edited: Timeline, rate: Fraction, history_from: Timeline,
           bounds: Optional[Dict[int, Sequence[float]]] = None) -> Tuple[Timeline, _Diff]:
    diff = _compare(snapped, edited, rate, bounds)
    inv = {j: i for i, j in diff.match.items()}
    clips: List[Clip] = []
    taken = {clip_key(c) for c in canonical.clips}
    for j, e in enumerate(edited.clips):
        if j not in inv:
            c = Clip.from_dict(e.to_dict())
            c.id = diff.ids[j]
            if j in diff.reshot:  # the key went to other footage: the shot is where the range is, not the key's
                c.shot_index = diff.reshot[j]
            if j in diff.blocked or (c.origin and c.origin in taken):  # a copy carries the original's key: it is a new instance, not the canonical clip
                c.origin = f"human:{c.id}"
            clips.append(c)
            continue
        i = inv[j]
        c0 = canonical.clips[i]
        er, sr = diff.erows[j], diff.crows[i]
        c = Clip.from_dict(c0.to_dict())  # id, shot, beat, purpose, locks, tags, notes of the canonical clip
        c.origin = clip_origin(c0)
        # an edge the editor did not move keeps matchcut's own (unsnapped) value; a moved one takes the editor's frame
        if er.in_f != sr.in_f:
            c.source_in = e.source_in
        if er.out_f != sr.out_f:
            c.source_out = e.source_out
        if not j:
            c.transition_in = Transition()
        elif (er.kind, er.tr) != (sr.kind, sr.tr):
            c.transition_in = Transition.from_dict(e.transition_in.to_dict())
        clips.append(c)
    out = Timeline(source=canonical.source, fps=canonical.fps, clips=clips, fade_in=canonical.fade_in, fade_out=canonical.fade_out,
                   name="human edit", stage=HUMAN)
    out.history = [Decision.from_dict(d.to_dict()) for d in history_from.history] + diff.decisions
    _release_changed_pins(out)
    return out, diff


def _release_changed_pins(tl: Timeline) -> None:
    """Clear, on the human stage's clips, the lock a pin put there when the editor's cut no longer holds that pin (the clips were copied from the
    canonical stage with their locks; a lock nobody keeps would read as matchcut's own, and the timeline strip would draw it)."""
    latest: Dict[Tuple[Any, Any], Decision] = {}
    for d in tl.history:
        if d.pass_name == "editor" and d.action == "pinned":
            latest[(d.data.get("out_shot"), d.data.get("in_shot"))] = d
    for d in latest.values():
        try:
            pin = Pin.from_dict(d.data)
            if not pin_change(tl, pin, float(d.data["out"]), float(d.data["in_"])):
                continue
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            continue
        by_origin = bool(pin.out_origin and pin.in_origin)
        for c in tl.clips:
            out_side = clip_origin(c) == pin.out_origin if by_origin else (c.shot_index == pin.out_shot and "pinned" in c.tags)
            in_side = clip_origin(c) == pin.in_origin if by_origin else (c.shot_index == pin.in_shot and "pinned" in c.tags)
            if out_side:
                c.locked_out = False
            if in_side:
                c.locked_in = False
            if (out_side or in_side) and not (c.locked_in or c.locked_out) and "pinned" in c.tags:
                c.tags.remove("pinned")


def human_stage(canonical: Timeline, edited: Timeline, rate: Fraction, snapped: Optional[Timeline] = None,
                bounds: Optional[Dict[int, Sequence[float]]] = None) -> Timeline:
    """The edited cut as a new stage ``human``: the canonical clip ids, canonical history + the human decisions.  Nothing is modified.

    ``snapped`` (default ``canonical``) is the frame-snapped copy that was exported, which the edit is compared with.
    """
    return _build(canonical, snapped if snapped is not None else canonical, edited, rate, canonical, bounds)[0]


def _format_of(path: Path, fmt: Optional[str]) -> str:
    if fmt is not None:
        if fmt not in FORMATS:
            raise InterchangeError(f"unknown format {fmt}: use otio, fcpxml or edl")
        return fmt
    found = _EXTENSIONS.get(path.suffix.lower())
    if found is None:
        raise InterchangeError(f"cannot tell the format of {path.name} from its extension (use --format {'|'.join(FORMATS)})")
    return found


def _name_stage(name: str, names: Sequence[str]) -> str:
    """The stage a timeline/project name ends with (``demo - final``), if it is one of ``names``."""
    for s in sorted(names, key=len, reverse=True):
        if name == s or name.endswith(f" - {s}"):
            return s
    return ""


@contextlib.contextmanager
def stage_lock(ws: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on ``WS/.stage.lock`` for the block.  ``import_timeline`` holds it while it reads ``edit.json`` and its
    record and writes ``human.json``; ``matchcut.edit``'s publish holds it (inside the session lock, which import never takes) while it
    writes the edit stage, so neither sees the other half done.  One lock, always taken last: no deadlock."""
    fd = os.open(Path(ws) / STAGE_LOCK, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _stage_file(ws: Path, stage: str) -> Path:
    return Path(ws) / EDIT_FILE if stage == EDIT else dict(stage_timeline_paths(ws)).get(stage, Path(ws) / f"{stage}.json")


def _snapshot_folders(ws: Path, imported: Path) -> List[Path]:
    return list(dict.fromkeys([Path(ws) / "exports" / SNAPSHOT_DIR, imported.resolve().parent / SNAPSHOT_DIR]))


def _edit_in_play(ws: Path) -> str:
    """What makes the edit stage a possible base of an editor's file in this workspace ("" when nothing does): its ``edit.json`` (fresh or
    not), or a snapshot of one in its ``exports/.stages``.  A file's base is then never assumed: it must be named by the file's
    fingerprint, or by ``--stage`` and ``--base``.  (A stray snapshot next to the imported file does not count: a workspace that never had
    an edit stage imports exactly as before M3a.)"""
    if (Path(ws) / EDIT_FILE).exists():
        return "an edit stage"
    folder = Path(ws) / "exports" / SNAPSHOT_DIR
    return "an exported edit stage" if folder.is_dir() and any(folder.glob(f"{EDIT}-*.json")) else ""


def _fingerprint_stages(ws: Path, imported: Path, sha: str, stages: Sequence[str]) -> List[str]:
    """The stages of ``stages`` that have a file whose sha256 is ``sha``: the stage file now, or a good snapshot of it."""
    out: List[str] = []
    for st in stages:
        if _sha256(_stage_file(ws, st)) == sha or any(_sha256(f / _snapshot_name(st, sha)) == sha for f in _snapshot_folders(ws, imported)):
            out.append(st)
    return out


def _load_base(ws: Path, base: str, stage: str) -> Tuple[Path, Timeline, str]:
    """The base an import is diffed against, named by ``--base``: a path, or a snapshot's name in ``exports/.stages`` (with or without
    ``.json``).  (its path, its timeline, the sha256 of its bytes); a one-line ``InterchangeError`` when it cannot be read or is a timeline
    of another stage than ``stage``."""
    folder = Path(ws) / "exports" / SNAPSHOT_DIR
    named = re.fullmatch(r"([a-z][a-z0-9_]*)-([0-9a-f]{12})(\.json)?", base)
    candidates: List[Path] = []
    if named and not Path(base).is_absolute():  # a snapshot's name: exports/.stages first, whatever the current directory holds
        candidates.append(folder / f"{named.group(1)}-{named.group(2)}.json")
    candidates.append(Path(base))
    if not Path(base).is_absolute() and not named:
        candidates += [folder / base, folder / f"{base}.json"]
    found = next((c for c in candidates if c.is_file()), None)
    if found is None:
        raise InterchangeError(f"--base {base}: no such file (looked for {' and '.join(_shown(ws, c) for c in dict.fromkeys(candidates))})")
    found = found.resolve()
    try:
        data = found.read_bytes()
        tl = Timeline.from_dict(json.loads(data.decode("utf-8")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise InterchangeError(f"--base {_shown(ws, found)} is not a stage timeline: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}") from exc
    digest = hashlib.sha256(data).hexdigest()
    snap = re.fullmatch(r"[a-z][a-z0-9_]*-([0-9a-f]{12})\.json", found.name)
    if snap and found.parent.name == SNAPSHOT_DIR and not digest.startswith(snap.group(1)):
        raise InterchangeError(f"--base {_shown(ws, found)} is damaged (its content does not hash to the {snap.group(1)} in its name): "
                               "re-export, or restore the snapshot")
    if tl.stage and tl.stage != stage:
        raise InterchangeError(f"--base {_shown(ws, found)} is a {tl.stage} stage timeline, not {stage}: name the base the file was exported from")
    return found, tl, digest


def import_timeline(ws: Path, cfg: PipelineConfig, path: Path, fmt: Optional[str] = None, stage: Optional[str] = None,
                    base: Optional[str] = None) -> ImportResult:
    """``_import_timeline`` under the workspace's stage lock (``stage_lock``): a publish never changes ``edit.json`` halfway through."""
    if not Path(ws).is_dir():
        return _import_timeline(ws, cfg, path, fmt, stage, base)
    with stage_lock(ws):
        return _import_timeline(ws, cfg, path, fmt, stage, base)


def _import_timeline(ws: Path, cfg: PipelineConfig, path: Path, fmt: Optional[str] = None, stage: Optional[str] = None,
                     base: Optional[str] = None) -> ImportResult:
    """Read an editor's file, write ``human.json`` and ``human_import.json`` (never a canonical file) and return the human decisions.

    The stage compared with is ``stage`` when given, else the one the file says it was exported from (OTIO timeline metadata or name,
    FCPXML project, EDL title), else ``final``.  It may be ``edit`` (an agent's published edit session, ``edit.json``).

    Once the workspace has an edit stage (an ``edit.json``, or an exported snapshot of one), the base is never assumed:

    - a file with a fingerprint is compared with exactly the version it was exported from (the stage file when it still has that sha256,
      else its snapshot in ``exports/.stages``), even after ``edit.json`` was published again; a fingerprint that matches no file of the
      stage the file names is refused, and so is one that matches a file of another stage only (the file names ``edit`` but a ``final``
      snapshot matches, or the reverse): the base is ambiguous;
    - a file without a fingerprint (an EDL whose title was changed) is refused;

    unless ``stage`` and ``base`` (a stage timeline file: a snapshot's path or name, or a stage file) name the base explicitly.  Without an
    edit stage, ``base`` is optional and everything else is as before.

    The human history continues the history of the stage the human stage is listed after: the edit stage while it is fresh, else the
    latest canonical stage.  ``human_import.json`` records the stage compared with, the latest canonical stage and (``history_stage``) the
    edit stage whose history it continues, each with its file's sha256: ``junctions.stale_human`` leaves the human stage out once any of
    them changes.
    """
    ws, path = Path(ws), Path(path)
    fmt = _format_of(path, fmt)
    try:
        # a .fcpxmld is a bundle (a directory) with the document inside
        text = (path / BUNDLE_FILE if fmt == "fcpxml" and path.is_dir() else path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise InterchangeError(f"cannot read {path}: " + (f"no {BUNDLE_FILE} inside" if path.is_dir() else (exc.strerror or str(exc)))) from exc
    except OSError as exc:
        raise InterchangeError(f"cannot read {path}: {exc.strerror or exc}") from exc
    canonical = _canonical(ws)
    if not canonical:
        raise InterchangeError(f"no timelines in {ws}: run the pipeline first")
    names = [s for s, _ in canonical]
    has_edit = _edit_in_play(ws)
    # the stages a file can say it was exported from: edit only in a workspace that has (or exported) an edit stage, so a title such as
    # "Interview - edit" means nothing new where there never was one
    claimable = names + ([EDIT] if has_edit else [])
    if stage is not None:
        if stage == HUMAN:
            raise InterchangeError("the human stage is an import result: name one of the stages it is compared with")
        if stage not in claimable:
            raise InterchangeError(f"no stage {stage} in {ws}")
    if base is not None and stage is None:
        raise InterchangeError("--base names the version of a stage: give the stage too (--stage)")
    src, notes = _source(ws, cfg)
    bounds = _bounds(ws)
    if bounds is None:
        notes.append("no shots.json: clips without a shot could not be placed by their source range")
    # read the file
    claimed = ""
    only_project: Optional[Tuple[str, str]] = None  # FCPXML with a single project: (its stage as the reader names it, its name)
    fingerprint = ""  # the sha256 of the stage file the editor's file was exported from, when it says
    if fmt == "fcpxml":
        keys = fcpxml_stages(text)
        known = [k for k in keys if k in names]
        pick = stage  # when the file has no such project the reader says so
        if pick is None and known:
            pick = "final" if "final" in known else known[-1]
        elif pick is None and EDIT in keys and has_edit:
            pick = EDIT
        if len(keys) == 1:
            pick = None  # the only project is read whatever it is called: --stage (or detection) only picks the stage it is compared with
            only_project = (keys[0], fcpxml_project_names(text)[0])
        if len(keys) > 1:
            notes.append(f"the file has projects for {', '.join(keys)}; read the {pick or keys[0]} one" + ("" if stage else " (--stage reads another)"))
        edited, inotes = import_fcpxml(text, pick, bounds, src)
        claimed = (pick or keys[0]) if (pick or keys[0]) in claimable else ""
        fingerprint = fcpxml_fingerprint(text, pick)
    elif fmt == "otio":
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise InterchangeError(f"invalid OTIO document: {exc}".splitlines()[0]) from exc
        edited, inotes = import_otio(doc, bounds, src)
        fingerprint = otio_fingerprint(doc)
        claimed = edited.stage if edited.stage in claimable else _name_stage(edited.name, claimable)
        if edited.stage and edited.stage not in claimable:
            notes.append(f"the file says it was exported from stage {edited.stage}, which this workspace does not have")
    else:
        edited, inotes = import_edl(text, src.rate, src.path, bounds, src)
        fingerprint = edl_fingerprint(text)
        claimed = edited.stage if edited.stage in claimable else _name_stage(edited.name, claimable)
        if edited.stage and edited.stage not in claimable:
            notes.append(f"the file says it was exported from stage {edited.stage}, which this workspace does not have")
    notes = inotes + notes
    in_play = bool(has_edit)
    edit_now = _edit(ws)
    current: Dict[str, Timeline] = dict(canonical) | ({EDIT: edit_now} if edit_now is not None else {})
    base_snapshot: Optional[Path] = None
    base_sha = ""
    if base is not None:  # the base named explicitly: the file's own word is only noted
        chosen = str(stage)
        base_snapshot, canon, base_sha = _load_base(ws, base, chosen)
        if claimed and claimed != chosen:
            notes.append(f"the file says it was exported from stage {claimed}; compared with {chosen} as asked")
        if fingerprint and fingerprint != base_sha:
            notes.append(f"the file's fingerprint (sha {fingerprint[:12]}) is not that of --base {_shown(ws, base_snapshot)} (sha {base_sha[:12]}): "
                         "compared with the base as asked")
    elif not in_play:  # no edit stage anywhere: the base is the stage the file names (or final), as before M3a
        if stage is not None:
            chosen = stage
            if claimed and claimed != stage:
                notes.append(f"the file says it was exported from stage {claimed}; compared with {stage} as asked")
        elif claimed:
            chosen = claimed
        elif "final" in names:
            chosen = "final"
            notes.append("the file does not say which stage it was exported from; compared with the final stage")
        else:
            raise InterchangeError(f"no stage final in {ws}: name the stage the file was exported from (--stage)")
        canon = current[chosen]
        if fingerprint and claimed == chosen:  # (a stage chosen against the file's word has no fingerprint to check)
            if fingerprint != _sha256(_stage_file(ws, chosen)):  # the pipeline ran again since the export: diff against the stage as it was exported
                base_snapshot, canon = _load_snapshot(ws, path, chosen, fingerprint)
                base_sha = fingerprint
                notes.append(f"{path.name} was exported from an earlier {chosen}: the editor's changes were diffed against that version; "
                             f"junction histories compare the human stage with the current {chosen}")
        elif not fingerprint:
            notes.append(f"{path.name} carries no stage fingerprint: if the pipeline ran after it was exported, the pipeline's changes show as human decisions")
    else:  # an edit stage is in play: the base is what the fingerprint names, exactly, or nothing
        fix = "name the base with --stage and --base SNAPSHOT (a file in exports/.stages)"
        if not fingerprint:
            raise InterchangeError(f"{path.name} carries no stage fingerprint, and this workspace has {has_edit} as well as {names[-1]}: "
                                   f"the base is ambiguous; {fix}")
        target = stage or claimed
        matches = _fingerprint_stages(ws, path, fingerprint, claimable)
        if not target:
            if len(matches) != 1:
                raise InterchangeError(f"{path.name} does not say which stage it was exported from and its fingerprint (sha {fingerprint[:12]}) "
                                       + ("matches no stage file or snapshot" if not matches else f"matches stages {_list(matches)}") + f"; {fix}")
            target = matches[0]
            notes.append(f"the file does not say which stage it was exported from; its fingerprint is that of stage {target}")
        if target not in matches:
            if matches:
                raise InterchangeError(f"{path.name} names stage {target}, but its fingerprint (sha {fingerprint[:12]}) is that of stage "
                                       f"{_list(matches)}: the base is ambiguous; {fix}")
            if target != EDIT:
                _load_snapshot(ws, path, target, fingerprint)  # raises: missing (where it looked) or damaged
            raise InterchangeError(f"{path.name} was exported from an edit stage (sha {fingerprint[:12]}) that matches no snapshot "
                                   f"({_snapshot_name(EDIT, fingerprint)} is not in {' or '.join(_shown(ws, f) for f in _snapshot_folders(ws, path))}) "
                                   "and is not edit.json now: matchcut does not guess the base; restore the snapshot, or " + fix)
        if stage is not None and claimed and claimed != stage:
            notes.append(f"the file says it was exported from stage {claimed}; compared with {stage} as asked")
        chosen = target
        if chosen in current and _sha256(_stage_file(ws, chosen)) == fingerprint:
            canon = current[chosen]
        else:
            base_snapshot, canon = _load_snapshot(ws, path, chosen, fingerprint)
            base_sha = fingerprint
            again = "edit.json was published again since" if chosen == EDIT else "the pipeline ran again since"
            notes.append(f"{path.name} was exported from an earlier {chosen} ({again}): the editor's changes were diffed against that version; "
                         f"junction histories compare the human stage with the current {chosen}")
    if chosen == EDIT and edit_now is None:
        raise InterchangeError(_no_edit(ws, "import against") + ": the human stage would be stale at once")
    if only_project is not None and only_project[0] not in claimable and only_project[0] != chosen:
        notes.append(f"the file's only project is named {only_project[1]}; compared with {chosen}")
    latest_name, latest = canonical[-1]
    # the stage the human stage is listed after, whose history the human history continues: the edit stage while it is fresh
    prev_name, prev = (EDIT, edit_now) if edit_now is not None else (latest_name, latest)
    stage_path, latest_path = _stage_file(ws, chosen), _stage_file(ws, latest_name)
    stage_hash = _sha256(stage_path)
    snapped, _ = export_timeline(canon, bounds, src)
    human, diff = _build(canon, snapped, edited, src.rate, prev, bounds)
    notes.extend(diff.notes)
    if diff.by_range:
        notes.append(f"{diff.by_range} clip{'s' if diff.by_range != 1 else ''} matched by source range because the clip key was missing or unknown")
    if chosen != prev_name:
        notes.append(f"human stage is based on {chosen}, not {prev_name}: junction histories compare it with {prev_name}, "
                     "so changes the later stages made show as differences")
        notes.append(f"the human history continues the {prev_name} stage's history, so the decisions of every stage stay attributed")
    previous = stale_human(ws)
    if previous:
        notes.append(f"{previous}; this import replaces it")
    record = {
        "schema": IMPORT_SCHEMA, "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source_file": str(path.resolve()),
        "format": fmt, "stage": chosen, "stage_file": stage_path.name, "stage_sha256": stage_hash,
        # the latest canonical stage: a change to it, or to the stage compared with, makes the human stage stale (junctions.stale_human)
        "latest_stage": latest_name, "latest_stage_file": latest_path.name, "latest_stage_sha256": _sha256(latest_path),
        "decisions": len(diff.decisions), "notes": notes,
    }
    if prev_name == EDIT:  # the human history continues the edit stage's: a new publish makes the human stage stale too
        record.update(history_stage=EDIT, history_stage_file=EDIT_FILE, history_stage_sha256=_sha256(ws / EDIT_FILE))
    if base_snapshot is not None:  # the file was diffed against this version of `stage`; stage_sha256 above is the CURRENT stage file
        record["base_snapshot"] = str(base_snapshot)
        record["base_snapshot_sha256"] = base_sha
    human_path = ws / "human.json"
    _atomic_write(human_path, json.dumps(human.to_dict(), indent=2))
    _atomic_write(ws / "human_import.json", json.dumps(record, indent=2) + "\n")
    return ImportResult(human_path, diff.decisions, notes, chosen, fmt)
