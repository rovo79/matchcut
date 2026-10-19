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

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from matchcut.pipeline.config import MusicConfig, PipelineConfig
from matchcut.pipeline.junctions import Junction, build_junctions, load_stage_timelines, scorer_for_workspace, stale_human, stage_timeline_paths
from matchcut.pipeline.shots import load_shots
from matchcut.pipeline.timeline import Clip, Decision, Timeline, Transition, clip_origin

from . import InterchangeError, fidelity
from .common import LaidClip, SourceInfo, clip_key, export_timeline, layout, plain, source_info
from .edl import export_edl, import_edl
from .fcpxml import export_fcpxml, fcpxml_stages, import_fcpxml
from .fidelity import FORMATS, FidelityResult
from .otio import export_otio, import_otio
from .provenance import write_provenance

HUMAN = "human"
DEFAULT_STAGES: Tuple[str, ...] = ("roughcut", "final")
IMPORT_SCHEMA = "matchcut.human_import/1"
_EXTENSIONS = {".otio": "otio", ".fcpxml": "fcpxml", ".xml": "fcpxml", ".edl": "edl"}


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


def _merge_provenance(path: Path, doc: Dict[str, Any], old: Optional[Dict[str, Any]], order: Sequence[str], hashes: Dict[str, Optional[str]],
                      stages: Sequence[str]) -> List[str]:
    """Stamp each exported stage of ``doc`` with the sha256 of its stage file and carry over, from the provenance.json that was there,
    the stages not exported now whose stage file is unchanged (same source and rate).  Rewrites ``path``; returns the carried stages."""
    for s in stages:
        doc["stages"][s]["canonical_sha256"] = hashes[s]
    kept: List[str] = []
    if old and old.get("source") == doc.get("source") and old.get("rate") == doc.get("rate") and isinstance(old.get("stages"), dict):
        for s, entry in old["stages"].items():
            if s not in doc["stages"] and isinstance(entry, dict) and hashes.get(s) and entry.get("canonical_sha256") == hashes[s]:
                doc["stages"][s] = entry
                kept.append(s)
    doc["stage_order"] = [s for s in order if s in doc["stages"]]
    doc["stages"] = {s: doc["stages"][s] for s in doc["stage_order"]}
    path.write_text(json.dumps(plain(doc), indent=1))
    return kept


def _carried_results(old: Optional[Dict[str, Any]], out: Path, hashes: Dict[str, Optional[str]], now: set) -> List[FidelityResult]:
    """The entries of an earlier fidelity.json that this export does not replace and that still hold: the stage file is unchanged and
    the exported file is still there (for FCPXML, still has the stage's project)."""
    carried: List[FidelityResult] = []
    stages = (old or {}).get("stages")
    if not isinstance(stages, dict):
        return carried
    for stage, by_fmt in stages.items():
        for fmt, d in (by_fmt.items() if isinstance(by_fmt, dict) else []):
            try:
                if (stage, fmt) in now or fmt not in FORMATS or not hashes.get(stage) or d.get("canonical_sha256") != hashes[stage]:
                    continue
                if fmt == "fcpxml":
                    if stage not in fcpxml_stages((out / "matchcut.fcpxml").read_text(encoding="utf-8")):
                        continue
                elif not (out / f"{stage}.{fmt}").exists():
                    continue
                carried.append(FidelityResult.from_dict(d))
            except (OSError, InterchangeError, KeyError, TypeError, AttributeError):
                continue
    return carried


# -- export ------------------------------------------------------------------------------------------
def export_workspace(ws: Path, cfg: PipelineConfig, stages: Sequence[str] = DEFAULT_STAGES, formats: Sequence[str] = FORMATS,
                     out_dir: Optional[Path] = None) -> ExportResult:
    """Write the interchange files of ``stages`` in ``formats`` (default ``<ws>/exports``), validate each by reading it back, write the
    reports, and fail with a one-line ``InterchangeError`` if any export does not match its canonical stage (after the reports exist)."""
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
    scorer = scorer_for_workspace(ws, cfg)
    junctions: Dict[str, List[Junction]] = {s: build_junctions(canonical, scorer, upto=s) for s in stages}
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
            doc = export_otio(snapped[s], s, src, junctions[s], music, music_src)
            path = out / f"{s}.otio"
            _atomic_write(path, json.dumps(doc, indent=1) + "\n")
            files.append(path)
            texts[(s, "otio")] = path.read_text(encoding="utf-8")
    if "fcpxml" in formats:
        path = out / "matchcut.fcpxml"
        _atomic_write(path, export_fcpxml([(s, snapped[s], junctions[s]) for s in stages], src, music, music_src, name=stem))
        files.append(path)
        for s in stages:
            texts[(s, "fcpxml")] = path.read_text(encoding="utf-8")
    if "edl" in formats:
        for s in stages:
            path = out / f"{s}.edl"
            _atomic_write(path, export_edl(snapped[s], s, src, f"{stem} - {s}", music))
            files.append(path)
            texts[(s, "edl")] = path.read_text(encoding="utf-8")
    prov = out / "provenance.json"
    hashes = {s: _sha256(stage_files[s]) for s in by_stage}
    old_prov, old_fidelity = _read_json(prov), _read_json(out / "fidelity.json")
    doc = write_provenance(prov, [(s, by_stage[s]) for s in stages], junctions, src)
    kept = _merge_provenance(prov, doc, old_prov, [s for s, _ in canonical], hashes, stages)
    files.append(prov)
    # validation: what was written, read back by matchcut's own readers, against the stage
    results: List[FidelityResult] = []
    for s in stages:
        for fmt in formats:
            try:
                if fmt == "otio":
                    back, _ = import_otio(json.loads(texts[(s, fmt)]), bounds, src)
                elif fmt == "fcpxml":
                    back, _ = import_fcpxml(texts[(s, fmt)], s, bounds, src)
                else:
                    back, _ = import_edl(texts[(s, fmt)], src.rate, src.path, bounds, src)
            except InterchangeError as exc:
                bad = FidelityResult(fmt, s, errors=[f"the file cannot be read back: {exc}"])
                bad.lost = list(fidelity.KNOWN[fmt]["lost"])
                results.append(bad)
                continue
            res = fidelity.check(by_stage[s], back, fmt, src.rate, snapped[s])
            res.stage = s
            results.append(res)
    for r in results:
        r.canonical_sha256 = hashes[r.stage] or ""
    carried = _carried_results(old_fidelity, out, hashes, {(r.stage, r.fmt) for r in results})
    order = {s: k for k, (s, _) in enumerate(canonical)}
    everything = sorted(results + carried, key=lambda r: (order.get(r.stage, len(order)), FORMATS.index(r.fmt)))
    files += list(fidelity.write_report(everything, out))
    if kept or carried:
        notes.append("the earlier records of this directory are kept where their stage file is unchanged: "
                     + ", ".join([f"provenance {s}" for s in kept] + [f"fidelity {r.stage}/{r.fmt}" for r in carried]))
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


def _match(canon: Timeline, edited: Timeline, crows: Sequence[LaidClip], erows: Sequence[LaidClip]) -> Tuple[Dict[int, int], int]:
    """canonical clip index -> edited clip index, one to one: by clip key (the lineage id the exports carry) first, then by lineage
    (same shot, overlapping source range, best overlap first) for what the key did not settle.  Also how many matches the range made."""
    out: Dict[int, int] = {}
    used: set = set()
    keyed: Dict[str, List[int]] = {}
    for j, e in enumerate(edited.clips):
        if e.origin:
            keyed.setdefault(e.origin, []).append(j)
    for i, c in enumerate(canon.clips):
        free = [j for j in keyed.get(clip_key(c), []) if j not in used]
        if free:
            j = max(free, key=lambda j: (_overlap(crows[i], erows[j]), -abs(i - j)))  # a copied clip keeps its key: the one nearest the original wins
            out[i] = j
            used.add(j)
    pairs = []
    for i, c in enumerate(canon.clips):
        if i in out:
            continue
        for j, e in enumerate(edited.clips):
            ov = _overlap(crows[i], erows[j])
            if j not in used and c.shot_index == e.shot_index and ov > 0:
                pairs.append((-ov, abs(i - j), i, j))
    by_range = 0
    for _, _, i, j in sorted(pairs):
        if i not in out and j not in used:
            out[i] = j
            used.add(j)
            by_range += 1
    return out, by_range


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
    match, by_range = _match(canonical, edited, crows, erows)
    ids = _fresh_ids(canonical, edited, match)
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
    return _Diff(match, ids, out, crows, erows, by_range)


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
    for j, e in enumerate(edited.clips):
        if j not in inv:
            c = Clip.from_dict(e.to_dict())
            c.id = diff.ids[j]
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
        text = path.read_text(encoding="utf-8", errors="replace")
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
    elif fmt == "otio":
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise InterchangeError(f"invalid OTIO document: {exc}".splitlines()[0]) from exc
        edited, inotes = import_otio(doc, bounds, src)
        claimed = edited.stage if edited.stage in names else _name_stage(edited.name, names)
        if edited.stage and edited.stage not in names:
            notes.append(f"the file says it was exported from stage {edited.stage}, which this workspace does not have")
    else:
        edited, inotes = import_edl(text, src.rate, src.path, bounds, src)
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
    snapped, _ = export_timeline(canon, bounds, src)
    human, diff = _build(canon, snapped, edited, src.rate, latest)
    if diff.by_range:
        notes.append(f"{diff.by_range} clip{'s' if diff.by_range != 1 else ''} matched by source range because the clip key was missing or unknown")
    if chosen != latest_name:
        notes.append(f"human stage is based on {chosen}, not {latest_name}: junction histories compare it with {latest_name}, "
                     "so changes the later stages made show as differences")
        notes.append(f"the human history continues the {latest_name} stage's history, so the decisions of every stage stay attributed")
    previous = stale_human(ws)
    if previous:
        notes.append(f"{previous}; this import replaces it")
    stage_files = dict(stage_timeline_paths(ws))
    stage_path, latest_path = stage_files[chosen], stage_files[latest_name]
    record = {
        "schema": IMPORT_SCHEMA, "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source_file": str(path.resolve()),
        "format": fmt, "stage": chosen, "stage_file": stage_path.name, "stage_sha256": _sha256(stage_path),
        # the stage whose history the human history copies: a change to either file makes the human stage stale (junctions.stale_human)
        "latest_stage": latest_name, "latest_stage_file": latest_path.name, "latest_stage_sha256": _sha256(latest_path),
        "decisions": len(diff.decisions), "notes": notes,
    }
    human_path = ws / "human.json"
    _atomic_write(human_path, json.dumps(human.to_dict(), indent=2))
    _atomic_write(ws / "human_import.json", json.dumps(record, indent=2) + "\n")
    return ImportResult(human_path, diff.decisions, notes, chosen, fmt)
