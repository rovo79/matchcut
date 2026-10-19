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
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from matchcut.pipeline.config import MusicConfig, PipelineConfig
from matchcut.pipeline.junctions import Junction, build_junctions, load_stage_timelines, scorer_for_workspace, stale_human, stage_timeline_paths
from matchcut.pipeline.shots import load_shots
from matchcut.pipeline.timeline import Clip, Decision, Timeline, Transition, clip_origin

from . import InterchangeError, fidelity, resolve
from .common import LaidClip, SourceInfo, clip_key, export_timeline, is_drop_rate, junction_ids, layout, plain, source_info, truncate
from .edl import export_edl, import_edl, placement_errors as edl_placement, stage_fingerprint as edl_fingerprint
from .fcpxml import export_fcpxml, fcpxml_stages, import_fcpxml, placement_errors as fcpxml_placement, stage_fingerprint as fcpxml_fingerprint
from .fidelity import FORMATS, FidelityResult
from .otio import export_otio, import_otio, placement_errors as otio_placement, stage_fingerprint as otio_fingerprint
from .provenance import write_provenance

HUMAN = "human"
DEFAULT_STAGES: Tuple[str, ...] = ("roughcut", "final")
RESOLVE_SCRIPT = "open_in_resolve.py"
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
    """The canonical stage timelines (every stage but ``human``), in stage order."""
    return [(s, t) for s, t in load_stage_timelines(ws) if s != HUMAN]


def _bounds(ws: Path) -> Optional[Dict[int, Tuple[float, float]]]:
    try:
        return {s.index: (s.start, s.end) for s in load_shots(ws / "shots.json")}
    except Exception:
        return None


def _source(ws: Path, cfg: PipelineConfig) -> SourceInfo:
    """The source footage: the configured file, else the one the workspace was probed from."""
    probed = None
    try:
        probed = json.loads((ws / "probe.json").read_text()).get("path")
    except (OSError, ValueError, AttributeError):
        pass
    for candidate in (cfg.source, probed):
        if candidate and Path(candidate).is_file():
            return source_info(str(candidate))
    raise InterchangeError(f"source footage not found: {cfg.source or probed or '(no source configured)'}")


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
    canonical = _canonical(ws)
    by_stage = dict(canonical)
    for s in stages:
        if s == HUMAN:
            raise InterchangeError("the human stage is an import result, not a canonical stage: export one of the stages it was imported against")
        if s not in by_stage:
            raise InterchangeError(f"cannot read stage {s} ({stage_files[s]})" if s in stage_files and stage_files[s].exists() else f"no stage {s} in {ws}")
    src = _source(ws, cfg)
    bounds = _bounds(ws)
    music, music_src = _music(cfg)
    out = Path(out_dir) if out_dir else ws / "exports"
    notes: List[str] = []
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
    stale = stale_human(ws)
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


def _list(names: Sequence[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _match(canon: Timeline, edited: Timeline, crows: Sequence[LaidClip], erows: Sequence[LaidClip]) -> Tuple[Dict[int, int], int, List[Tuple[str, List[int]]]]:
    """canonical clip index -> edited clip index, one to one: by clip key (the lineage id the exports carry) first, then by lineage
    (same shot, overlapping source range, best overlap first) for what the key did not settle.

    Also how many matches the range made and the notes (text with a ``{new}`` placeholder for the ids the new clips get, and the edited clips
    they are about).  An ambiguous clip is read as new on purpose, never guessed: a key that several edited clips carry (a copied clip) does
    not say which one is the original, so none is taken for it; an edited clip with no key to decide that overlaps two canonical clips
    equally (the same number of frames) is not taken for either.
    """
    out: Dict[int, int] = {}
    used: set = set()
    blocked: set = set()
    notes: List[Tuple[str, List[int]]] = []
    keyed: Dict[str, List[int]] = {}
    for j, e in enumerate(edited.clips):
        if e.origin:
            keyed.setdefault(e.origin, []).append(j)
    for i, c in enumerate(canon.clips):
        carriers = keyed.get(clip_key(c), [])
        if len(carriers) == 1 and carriers[0] not in used:
            out[i] = carriers[0]
            used.add(carriers[0])
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
    return out, by_range, notes


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


def _compare(canonical: Timeline, edited: Timeline, rate: Fraction) -> _Diff:
    crows, erows = layout(canonical, rate), layout(edited, rate)
    match, by_range, found = _match(canonical, edited, crows, erows)
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
            decide("added", [ids[j]], f"human added {ids[j]} (shot {e.shot_index}, {e.source_in:.2f}-{e.source_out:.2f}s)",
                   shot_index=e.shot_index, source_in=e.source_in, source_out=e.source_out)
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
    return _Diff(match, ids, out, crows, erows, by_range, notes)


def diff_human_edits(canonical: Timeline, edited: Timeline, rate: Fraction) -> List[Decision]:
    """The human decisions that turn ``canonical`` into ``edited`` (trim, transition, removed, added, reorder).

    Both are on the frame grid of ``rate`` (``canonical`` is normally the frame-snapped copy that was exported).  Clip ids in the
    decisions are the canonical ids (an added clip: ``h<n>``).  Neither timeline is modified.
    """
    return _compare(canonical, edited, rate).decisions


def _build(canonical: Timeline, snapped: Timeline, edited: Timeline, rate: Fraction, history_from: Timeline) -> Tuple[Timeline, _Diff]:
    diff = _compare(snapped, edited, rate)
    inv = {j: i for i, j in diff.match.items()}
    clips: List[Clip] = []
    taken = {clip_key(c) for c in canonical.clips}
    for j, e in enumerate(edited.clips):
        if j not in inv:
            c = Clip.from_dict(e.to_dict())
            c.id = diff.ids[j]
            if c.origin and c.origin in taken:  # a copy carries the original's key: it is a new instance, not the canonical clip
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
    return out, diff


def human_stage(canonical: Timeline, edited: Timeline, rate: Fraction, snapped: Optional[Timeline] = None) -> Timeline:
    """The edited cut as a new stage ``human``: the canonical clip ids, canonical history + the human decisions.  Nothing is modified.

    ``snapped`` (default ``canonical``) is the frame-snapped copy that was exported, which the edit is compared with.
    """
    return _build(canonical, snapped if snapped is not None else canonical, edited, rate, canonical)[0]


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


def import_timeline(ws: Path, cfg: PipelineConfig, path: Path, fmt: Optional[str] = None, stage: Optional[str] = None) -> ImportResult:
    """Read an editor's file, write ``human.json`` and ``human_import.json`` (never a canonical file) and return the human decisions.

    The canonical stage compared with is ``stage`` when given, else the one the file says it was exported from (OTIO timeline metadata
    or name, FCPXML project, EDL title), else ``final``.
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
    if stage is not None:
        if stage == HUMAN:
            raise InterchangeError("the human stage is an import result: name one of the stages it is compared with")
        if stage not in names:
            raise InterchangeError(f"no stage {stage} in {ws}")
    src = _source(ws, cfg)
    bounds = _bounds(ws)
    notes: List[str] = []
    if bounds is None:
        notes.append("no shots.json: clips without a shot could not be placed by their source range")
    # read the file
    claimed = ""
    fingerprint = ""  # the sha256 of the stage file the editor's file was exported from, when it says
    if fmt == "fcpxml":
        keys = fcpxml_stages(text)
        known = [k for k in keys if k in names]
        pick = stage  # when the file has no such project the reader says so
        if pick is None and known:
            pick = "final" if "final" in known else known[-1]
        if len(keys) > 1:
            notes.append(f"the file has projects for {', '.join(keys)}; read the {pick or keys[0]} one" + ("" if stage else " (--stage reads another)"))
        edited, inotes = import_fcpxml(text, pick, bounds, src)
        claimed = pick if pick in names else ""
        fingerprint = fcpxml_fingerprint(text, pick)
    elif fmt == "otio":
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise InterchangeError(f"invalid OTIO document: {exc}".splitlines()[0]) from exc
        edited, inotes = import_otio(doc, bounds, src)
        fingerprint = otio_fingerprint(doc)
        claimed = edited.stage if edited.stage in names else _name_stage(edited.name, names)
        if edited.stage and edited.stage not in names:
            notes.append(f"the file says it was exported from stage {edited.stage}, which this workspace does not have")
    else:
        edited, inotes = import_edl(text, src.rate, src.path, bounds, src)
        fingerprint = edl_fingerprint(text)
        claimed = edited.stage if edited.stage in names else _name_stage(edited.name, names)
        if edited.stage and edited.stage not in names:
            notes.append(f"the file says it was exported from stage {edited.stage}, which this workspace does not have")
    notes = inotes + notes
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
    canon = dict(canonical)[chosen]
    latest_name, latest = canonical[-1]
    stage_files = dict(stage_timeline_paths(ws))
    stage_path, latest_path = stage_files[chosen], stage_files[latest_name]
    stage_hash = _sha256(stage_path)
    base_snapshot: Optional[Path] = None
    if fingerprint and claimed == chosen:  # (a stage chosen against the file's word has no fingerprint to check)
        if fingerprint != stage_hash:  # the pipeline ran again since the export: diff against the stage as it was exported
            base_snapshot, canon = _load_snapshot(ws, path, chosen, fingerprint)
            notes.append(f"{path.name} was exported from an earlier {chosen}: the editor's changes were diffed against that version; "
                         f"junction histories compare the human stage with the current {chosen}")
    elif not fingerprint:
        notes.append(f"{path.name} carries no stage fingerprint: if the pipeline ran after it was exported, the pipeline's changes show as human decisions")
    snapped, _ = export_timeline(canon, bounds, src)
    human, diff = _build(canon, snapped, edited, src.rate, latest)
    notes.extend(diff.notes)
    if diff.by_range:
        notes.append(f"{diff.by_range} clip{'s' if diff.by_range != 1 else ''} matched by source range because the clip key was missing or unknown")
    if chosen != latest_name:
        notes.append(f"human stage is based on {chosen}, not {latest_name}: junction histories compare it with {latest_name}, "
                     "so changes the later stages made show as differences")
        notes.append(f"the human history continues the {latest_name} stage's history, so the decisions of every stage stay attributed")
    previous = stale_human(ws)
    if previous:
        notes.append(f"{previous}; this import replaces it")
    record = {
        "schema": IMPORT_SCHEMA, "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source_file": str(path.resolve()),
        "format": fmt, "stage": chosen, "stage_file": stage_path.name, "stage_sha256": stage_hash,
        # the stage whose history the human history copies: a change to either file makes the human stage stale (junctions.stale_human)
        "latest_stage": latest_name, "latest_stage_file": latest_path.name, "latest_stage_sha256": _sha256(latest_path),
        "decisions": len(diff.decisions), "notes": notes,
    }
    if base_snapshot is not None:  # the file was diffed against this earlier version of `stage`; stage_sha256 above is the CURRENT stage file
        record["base_snapshot"] = str(base_snapshot)
        record["base_snapshot_sha256"] = fingerprint
    human_path = ws / "human.json"
    _atomic_write(human_path, json.dumps(human.to_dict(), indent=2))
    _atomic_write(ws / "human_import.json", json.dumps(record, indent=2) + "\n")
    return ImportResult(human_path, diff.decisions, notes, chosen, fmt)
