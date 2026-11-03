"""The automatic ([auto]) checks of the M3a acceptance run (docs/M3A-ACCEPTANCE.md).  It only reads the workspace: nothing is ever written
inside it.

    python3 tools/m3a_acceptance.py source WS                     # the source footage WS's config.json names, as an absolute path (exit 1: not found)
    python3 tools/m3a_acceptance.py snapshot WS [-o FILE]         # canonical mode: what an edit session must not change
    python3 tools/m3a_acceptance.py snapshot WS --all [-o FILE]   # strict mode: every file in the tree (for the ORIGINAL workspace)
    python3 tools/m3a_acceptance.py verify WS SNAPSHOT [--strict] # exit 0 when nothing recorded changed; else say what (exit 1)
    python3 tools/m3a_acceptance.py report WS [--json]            # the edit session, the published record, provenance, pins, mismatches, renders

**Canonical mode** (``snapshot``, for the working copy) records the sha256 of ``final.json``, every pass file, ``roughcut.json``,
``editorial.json`` and ``human*.json``, and the size and modification time of the source media the workspace uses (``config.json``'s
``source``, ``probe.json``'s file and the music track).  ``verify`` expects ``edit.json``, ``edit_publish.json`` and ``edits/`` to change
(an edit session and its publish write only those) and lists them apart.

**Strict mode** (``snapshot --all``, for the original workspace, which nothing may touch) records every file and directory in the tree:
its path, size and sha256 (and its modification time, which never fails a check on its own), with no exemption.  ``verify`` then reports
every new, removed or changed file or directory.  A file larger than ``--quick-above`` MiB (default 256) is hashed quickly, from its size
and its first and last MiB, and the output says so (``--full`` hashes everything); such a file whose modification time changed is
reported as possibly changed, since its middle was not read.  Source media outside the workspace are checked by size and modification
time in both modes.

A source footage (or music track) that ``config.json`` names but that cannot be found is an error (exit 1), never a silent pass: a
relative path (``python -m matchcut.pipeline run footage.mp4`` stores ``"footage.mp4"``) is looked for from the current directory and from
the directory the run was made in (when ``config.json``'s ``workspace`` says it).

``report`` summarises the session log, the published record, the provenance (base sha, latest canonical sha, session, revision, whether
each still matches), the pins the session does not protect, the acknowledged mismatches and protection changes and the render paths to
watch.

Passing these checks never passes a human gate: the videos must be watched and judged by the user.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA = "matchcut.m3a_acceptance_snapshot/1"
EXPECTED_TO_CHANGE = ("edit.json", "edit_publish.json", "edits/")
CANONICAL, ALL = "canonical", "all"
MIB = 1 << 20
QUICK_ABOVE_MIB = 256


class MediaMissing(Exception):
    """A medium ``config.json`` names (the source footage, the music track) cannot be found."""


def _sha256(path: Path) -> Optional[str]:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(MIB), b""):
                h.update(block)
        return h.hexdigest()
    except OSError:
        return None


def _quick(path: Path, size: int) -> Optional[str]:
    """The sha256 of a large file's size, first MiB and last MiB."""
    try:
        h = hashlib.sha256(str(size).encode())
        with open(path, "rb") as fh:
            h.update(fh.read(MIB))
            fh.seek(max(0, size - MIB))
            h.update(fh.read(MIB))
        return h.hexdigest()
    except OSError:
        return None


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _inside(path: Path, ws: Path) -> bool:
    p, w = path.resolve(), ws.resolve()
    return p == w or w in p.parents


def tracked_files(ws: Path) -> List[str]:
    """The canonical files (relative to ``ws``) that an edit session must never change."""
    names = ["roughcut.json", "final.json", "editorial.json"]
    names += [f"passes/{p.name}" for p in sorted((ws / "passes").glob("*.json"))]
    names += sorted(p.name for p in ws.glob("human*.json"))
    return names


def media_files(ws: Path) -> Tuple[Dict[str, str], List[str]]:
    """(role -> absolute path of each source medium the workspace uses: ``config.json``'s source and music track, ``probe.json``'s file;
    the problems: a source or music track that is named but not found, one line each saying what to do).  A probed file that is gone is
    left out (``config.json``'s source is what a session then reads)."""
    from matchcut.edit.session import resolve_path

    out: Dict[str, str] = {}
    problems: List[str] = []
    cfg = _json(ws / "config.json") or {}
    probe = _json(ws / "probe.json") or {}
    cwd = Path.cwd()
    if not isinstance(cfg, dict) or not cfg.get("source"):
        problems.append(f"{ws / 'config.json'} names no source footage")
    else:
        found = resolve_path(ws, str(cfg["source"]))
        if found is None:
            rel = not Path(str(cfg["source"])).expanduser().is_absolute()
            problems.append(f"the source footage {cfg['source']!r} that config.json names is not found"
                            + (f" (a relative path: looked for from {cwd} and from the directory the pipeline ran in, when config.json's "
                               "workspace says it). Find the footage, then either run from the directory where that path resolves, or "
                               "set an absolute source in the COPY's config.json (never the original's)" if rel else ""))
        else:
            out["source"] = str(found)
    if isinstance(probe, dict) and probe.get("path"):
        found = resolve_path(ws, str(probe["path"]))
        if found is not None and str(found) != out.get("source"):
            out["probed source"] = str(found)
    music = cfg.get("music") if isinstance(cfg, dict) else None
    if isinstance(music, dict) and music.get("path"):
        found = resolve_path(ws, str(music["path"]))
        if found is None:
            problems.append(f"the music track {music['path']!r} that config.json names is not found (from {cwd})")
        else:
            out["music"] = str(found)
    return out, problems


def _stat(path: str) -> Optional[Dict[str, Any]]:
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _entry(path: Path, quick_above: Optional[int]) -> Dict[str, Any]:
    """One file of a strict snapshot: size, mtime and a hash (``sha256``, or ``quick`` for a file above ``quick_above`` bytes)."""
    if path.is_symlink():
        return {"type": "symlink", "target": os.readlink(path)}
    st = path.stat()
    if quick_above is not None and st.st_size > quick_above:
        return {"type": "file", "size": st.st_size, "mtime_ns": st.st_mtime_ns, "hash": "quick", "digest": _quick(path, st.st_size)}
    return {"type": "file", "size": st.st_size, "mtime_ns": st.st_mtime_ns, "hash": "sha256", "digest": _sha256(path)}


def tree(ws: Path, quick_above: Optional[int]) -> Dict[str, Dict[str, Any]]:
    """Every file, symlink and directory under ``ws`` (relative POSIX path -> its entry), hidden ones included: nothing is exempt."""
    out: Dict[str, Dict[str, Any]] = {}
    for root, dirs, files in os.walk(ws, followlinks=False):
        r = Path(root)
        for d in dirs:
            p = r / d
            out[p.relative_to(ws).as_posix() + "/"] = {"type": "symlink", "target": os.readlink(p)} if p.is_symlink() else {"type": "dir"}
        for f in files:
            p = r / f
            out[p.relative_to(ws).as_posix()] = _entry(p, quick_above)
    return dict(sorted(out.items()))


def snapshot(ws: Path, mode: str = CANONICAL, quick_above_mib: Optional[float] = QUICK_ABOVE_MIB) -> Dict[str, Any]:
    """The snapshot of ``ws`` in ``mode``; raises ``MediaMissing`` when a medium config.json names cannot be found."""
    media, problems = media_files(ws)
    if problems:
        raise MediaMissing("; ".join(problems))
    doc: Dict[str, Any] = {"schema": SCHEMA, "mode": mode, "workspace": str(ws.resolve()),
                           "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "media": {role: {"path": path, "stat": _stat(path)} for role, path in media.items()}}
    if mode == ALL:
        quick = None if quick_above_mib is None else int(quick_above_mib * MIB)
        doc["quick_above_bytes"] = quick
        doc["tree"] = tree(ws, quick)
    else:
        doc["files"] = {name: _sha256(ws / name) for name in tracked_files(ws)}
    return doc


def _media_diffs(snap: Dict[str, Any]) -> List[str]:
    out = []
    for role, entry in sorted(snap.get("media", {}).items()):
        if entry.get("stat") is None:
            out.append(f"{role} {entry['path']}: was missing when the snapshot was taken: the snapshot cannot vouch for it")
            continue
        cur = _stat(entry["path"])
        if cur != entry.get("stat"):
            out.append(f"{role} {entry['path']}: " + ("missing now" if cur is None else "size or modification time changed"))
    return out


def verify(ws: Path, snap: Dict[str, Any]) -> List[str]:
    """One line per difference between ``snap`` and the workspace now; [] when every recorded file and medium is unchanged."""
    if snap.get("mode") == ALL:
        return verify_strict(ws, snap)[0]
    out: List[str] = []
    for name, sha in sorted(snap.get("files", {}).items()):
        cur = _sha256(ws / name)
        if cur != sha:
            out.append(f"{name}: " + ("missing now" if cur is None else "was missing, exists now" if sha is None else "changed"))
    for name in sorted(set(tracked_files(ws)) - set(snap.get("files", {}))):
        out.append(f"{name}: new since the snapshot")
    _, problems = media_files(ws)
    return out + _media_diffs(snap) + [f"now: {p}" for p in problems if "source footage" in p or "music track" in p]


def verify_strict(ws: Path, snap: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """(the differences, the notes) of a strict (``--all``) snapshot: every new, removed or changed file, symlink or directory, with no
    exemption.  A modification time that changed while the content hash did not is a note, not a difference; for a quickly hashed file
    (above the snapshot's threshold) it is a difference, since its middle was never read."""
    out: List[str] = []
    notes: List[str] = []
    then = snap.get("tree", {})
    quick = snap.get("quick_above_bytes")
    now = tree(ws, quick)
    for name in sorted(set(then) | set(now)):
        a, b = then.get(name), now.get(name)
        if a is None:
            out.append(f"{name}: new since the snapshot")
        elif b is None:
            out.append(f"{name}: removed since the snapshot")
        elif a.get("type") != b.get("type"):
            out.append(f"{name}: was a {a.get('type')}, is a {b.get('type')} now")
        elif a.get("type") == "symlink" and a.get("target") != b.get("target"):
            out.append(f"{name}: the symlink points elsewhere now")
        elif a.get("type") == "file":
            if a.get("size") != b.get("size") or a.get("digest") != b.get("digest"):
                out.append(f"{name}: changed ({'size' if a.get('size') != b.get('size') else 'content'})")
            elif a.get("mtime_ns") != b.get("mtime_ns"):
                if a.get("hash") == "quick":
                    out.append(f"{name}: possibly changed (its modification time changed; it is above the quick-hash threshold, so only its "
                               "size and first and last MiB were compared)")
                else:
                    notes.append(f"{name}: modification time changed, content identical (sha256)")
    return out + _media_diffs(snap), notes


def report(ws: Path) -> Dict[str, Any]:
    """The edit session, the published record and its provenance, the pins and mismatches, and the renders to watch; read-only."""
    from matchcut.pipeline.editorial import PinStore, pin_states
    from matchcut.pipeline.junctions import latest_canonical, stale_edit, stale_human
    from matchcut.pipeline.timeline import Timeline

    edits = ws / "edits"
    state = _json(edits / "session.json")
    revisions = []
    for p in sorted((edits / "revisions").glob("r*.json"), key=lambda p: int(p.stem[1:]) if p.stem[1:].isdigit() else -1):
        doc = _json(p) or {}
        revisions.append({"id": p.stem, "parent": doc.get("parent"), "ops": [o.get("op") for o in doc.get("ops") or [] if isinstance(o, dict)],
                          "changes": doc.get("changes") or [], "details": doc.get("details") or []})
    record = _json(ws / "edit_publish.json")
    latest = latest_canonical(ws)
    latest_sha = _sha256(latest[1]) if latest else None
    provenance = None
    if isinstance(record, dict):
        base_now = _sha256(ws / str(record.get("base_file", "")))
        provenance = {
            "session": record.get("session"), "revision": record.get("revision"),
            "session_is_current": bool(state) and state.get("session") == record.get("session"),
            "base_stage": record.get("base_stage"), "base_sha256": record.get("base_sha256"),
            "base_sha256_now": base_now, "base_unchanged": base_now == record.get("base_sha256"),
            "latest_stage": record.get("latest_stage"), "latest_stage_sha256": record.get("latest_stage_sha256"),
            "latest_canonical_now": latest[0] if latest else None, "latest_canonical_sha256_now": latest_sha,
            "latest_unchanged": latest_sha == record.get("latest_stage_sha256") and (latest[0] if latest else None) == record.get("latest_stage"),
            "edit_sha256": record.get("edit_sha256"), "edit_json_sha256_now": _sha256(ws / "edit.json"),
            "edit_json_matches_record": _sha256(ws / "edit.json") == record.get("edit_sha256"),
            "published_copy": record.get("published"),
            "published_copy_matches": _sha256(ws / str(record.get("published", ""))) == record.get("edit_sha256"),
        }
    log_lines = []
    try:
        log_lines = [json.loads(x) for x in (edits / "published" / "log.jsonl").read_text().splitlines() if x.strip()]
    except (OSError, ValueError):
        pass
    final = Timeline.load(latest[1]) if latest and latest[1].exists() else None
    pins = [{"pin": st.label, "state_in_latest_canonical": st.state, "reason": st.reason,
             "protected_by_a_session_on_it": st.state == "applied"} for st in pin_states(final, PinStore(ws).load())] if final else []
    renders = sorted((edits / "renders").glob("*.mp4"), key=lambda p: p.stat().st_mtime) if (edits / "renders").is_dir() else []
    films = [ws / n for n in ("final.mp4", "final_proxy.mp4") if (ws / n).exists()]
    imp = _json(ws / "human_import.json")
    human = None
    if isinstance(imp, dict):
        snap_name = str(imp.get("base_snapshot") or "")
        snap_path = None if not snap_name else Path(snap_name) if Path(snap_name).is_absolute() or Path(snap_name).exists() else ws / snap_name
        human = {k: imp.get(k) for k in ("stage", "stage_file", "stage_sha256", "base_snapshot", "base_snapshot_sha256", "history_stage",
                                         "latest_stage", "latest_stage_sha256", "source_file", "format", "decisions", "notes")}
        human["decisions_recorded"] = human.pop("decisions")
        human["base_snapshot_matches"] = (_sha256(snap_path) == imp.get("base_snapshot_sha256")) if snap_path is not None else None
        human["decisions"] = [f"{d.get('action')} {' '.join(d.get('clip_ids') or [])}  {d.get('detail')}"
                              for d in ((_json(ws / "human.json") or {}).get("history") or []) if isinstance(d, dict)
                              and d.get("pass_name") == "human"]
    return {
        "workspace": str(ws.resolve()),
        "human_import": human,
        "session": None if not isinstance(state, dict) else {k: state.get(k) for k in ("session", "base_stage", "base_file", "base_sha256",
                                                                                       "head", "next", "created", "updated")},
        "revisions": revisions,
        "published": None if not isinstance(record, dict) else {k: record.get(k) for k in (
            "revision", "published_at", "decisions", "published", "replaced", "superseded_human", "superseded_human_sha256")},
        "provenance": provenance,
        "edit_stage_stale": stale_edit(ws), "human_stage_stale": stale_human(ws) if (ws / "human.json").exists() else None,
        "publish_log": log_lines,
        "unapplied_pins_at_publish": (record or {}).get("unapplied_pins") if isinstance(record, dict) else None,
        "acknowledged_mismatches": (record or {}).get("acknowledged_mismatches") if isinstance(record, dict) else None,
        "acknowledged_protection_changes": (record or {}).get("acknowledged_protection_changes") if isinstance(record, dict) else None,
        "transition_mismatches_at_publish": (record or {}).get("transition_mismatches") if isinstance(record, dict) else None,
        "pins_now": pins,
        "renders_to_watch": {"original": [str(p) for p in films],
                             "session renders (oldest first)": [str(p) for p in renders],
                             "exports": [str(p) for p in sorted((ws / "exports").glob("edit.*"))] if (ws / "exports").is_dir() else []},
        "note": "automated checks never pass a human gate: the user must watch the original and the revised films and judge them",
    }


def _text(rep: Dict[str, Any]) -> str:
    lines = [f"workspace: {rep['workspace']}"]
    s = rep["session"]
    lines.append("edit session: none" if not s else f"edit session {s['session']} on {s['base_stage']} ({str(s['base_sha256'])[:12]}), head {s['head']}")
    for r in rep["revisions"]:
        lines.append(f"  {r['id']} <- {r['parent'] or '-'}  {', '.join(r['ops']) or 'base'}  {'; '.join(r['changes'])}")
    p = rep["published"]
    if not p:
        lines.append("published: nothing (no edit_publish.json)")
    else:
        lines.append(f"published: {p['revision']} at {p['published_at']} ({p['decisions']} agent decisions) -> {p['published']}")
        if p.get("superseded_human"):
            lines.append(f"  superseded human.json {str(p.get('superseded_human_sha256'))[:12]}: {p['superseded_human']}")
    v = rep["provenance"]
    if v:
        lines.append(f"provenance: session {v['session']} (current session: {v['session_is_current']}), revision {v['revision']}")
        lines.append(f"  base {v['base_stage']} {str(v['base_sha256'])[:12]} (unchanged: {v['base_unchanged']}); latest canonical "
                     f"{v['latest_stage']} {str(v['latest_stage_sha256'])[:12]} (unchanged: {v['latest_unchanged']})")
        lines.append(f"  edit.json {str(v['edit_sha256'])[:12]} matches the record: {v['edit_json_matches_record']}; published copy matches: "
                     f"{v['published_copy_matches']}")
    lines.append(f"edit stage: {(rep['edit_stage_stale'] or 'fresh') if v else 'none'}")
    if rep["human_stage_stale"]:
        lines.append(f"human stage: {rep['human_stage_stale']}")
    lines.append(f"publish log: {len(rep['publish_log'])} line(s)")
    h = rep["human_import"]
    if h:
        lines.append(f"human import: against {h['stage']} {str(h['stage_sha256'])[:12]}"
                     + (f", snapshot {h['base_snapshot']} (matches its sha256: {h['base_snapshot_matches']})" if h["base_snapshot"] else "")
                     + f", history continues {h['history_stage'] or h['latest_stage']}; {len(h['decisions'])} human decision(s)")
        for d in h["decisions"]:
            lines.append(f"  {d}")
    for title, key in (("unapplied pins at publish (not protected by this edit)", "unapplied_pins_at_publish"),
                       ("acknowledged mismatches", "acknowledged_mismatches"),
                       ("acknowledged protection changes (released or replaced during the session, NOT kept)",
                        "acknowledged_protection_changes")):
        items = rep[key] or []
        lines.append(f"{title}: {len(items)}")
        for e in items:
            if key == "acknowledged_mismatches":
                lines.append(f"  {e['pin']}: holds {e['held_transition']}, published {e['transition']}")
            elif key == "acknowledged_protection_changes":
                lines.append(f"  {e['pin']}: {e['change']}; at session start {e['original']}, now {e['now']}, published {e['transition']}")
            else:
                lines.append(f"  {e['pin']} ({e['kind']}): {e['reason']}")
    lines.append("pins now (in the latest canonical stage):")
    for e in rep["pins_now"]:
        lines.append(f"  {e['pin']}: {e['state_in_latest_canonical']}" + (f" ({e['reason']})" if e["reason"] else ""))
    lines.append("to watch:")
    for title, paths in rep["renders_to_watch"].items():
        lines.append(f"  {title}: " + (", ".join(paths) if paths else "none"))
    lines.append(rep["note"])
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="m3a_acceptance", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    sp = sub.add_parser("source", help="print the source footage config.json names, as an absolute path; exit 1 when it is not found")
    sp.add_argument("workspace")
    sp = sub.add_parser("snapshot", help="record what must not change, in a file outside the workspace")
    sp.add_argument("workspace")
    sp.add_argument("-o", "--out", default=None, help="the snapshot file (default: <WS>.m3a-snapshot.json next to WS)")
    sp.add_argument("--all", action="store_true", help="strict mode: every file and directory in the tree (for the original workspace)")
    sp.add_argument("--quick-above", type=float, default=QUICK_ABOVE_MIB, metavar="MIB",
                    help=f"--all: hash files above this many MiB from their size and first and last MiB (default {QUICK_ABOVE_MIB})")
    sp.add_argument("--full", action="store_true", help="--all: hash every file in full, however large")
    sp = sub.add_parser("verify", help="check nothing recorded in the snapshot changed")
    sp.add_argument("workspace")
    sp.add_argument("snapshot")
    sp.add_argument("--strict", action="store_true", help="require a strict (snapshot --all) snapshot: every file in the tree is checked")
    sp = sub.add_parser("report", help="summarise the session, the published record, provenance, pins, mismatches and renders")
    sp.add_argument("workspace")
    sp.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    ws = Path(args.workspace)
    if not (ws / "config.json").is_file():
        print(f"{ws} is not a matchcut workspace (no config.json)", file=sys.stderr)
        return 2
    if args.command == "source":
        media, problems = media_files(ws)
        if "source" not in media:
            print(f"error: {'; '.join(p for p in problems if 'source' in p)}", file=sys.stderr)
            return 1
        print(media["source"])
        return 0
    if args.command == "snapshot":
        out = Path(args.out) if args.out else ws.resolve().parent / f"{ws.resolve().name}.m3a-snapshot.json"
        if _inside(out, ws):
            print(f"refused: {out} is inside the workspace; a snapshot is kept outside it", file=sys.stderr)
            return 2
        try:
            snap = snapshot(ws, ALL if args.all else CANONICAL, None if args.full else args.quick_above)
        except MediaMissing as exc:
            print(f"error: no snapshot written: {exc}", file=sys.stderr)
            return 1
        out.write_text(json.dumps(snap, indent=2) + "\n")
        if args.all:
            files = [e for e in snap["tree"].values() if e.get("type") == "file"]
            quick = [n for n, e in snap["tree"].items() if e.get("hash") == "quick"]
            print(f"strict snapshot of {len(snap['tree'])} entries ({len(files)} files) and {len(snap['media'])} media -> {out}")
            if quick:
                print(f"  {len(quick)} file(s) above {args.quick_above:g} MiB hashed quickly (size, first and last MiB; --full hashes them in "
                      f"full): {', '.join(quick)}")
        else:
            print(f"snapshot of {len(snap['files'])} files and {len(snap['media'])} media -> {out}")
            for name, sha in snap["files"].items():
                print(f"  {(sha or 'missing')[:12]}  {name}")
        for role, e in snap["media"].items():
            print(f"  {role}: {e['path']} ({e['stat']['size']} bytes)")
        return 0
    if args.command == "verify":
        snap = _json(Path(args.snapshot))
        if not isinstance(snap, dict) or snap.get("schema") != SCHEMA:
            print(f"{args.snapshot} is not an M3a acceptance snapshot", file=sys.stderr)
            return 2
        strict = snap.get("mode") == ALL
        if args.strict and not strict:
            print(f"{args.snapshot} is a canonical snapshot: --strict needs one taken with snapshot --all", file=sys.stderr)
            return 2
        if strict:
            diffs, notes = verify_strict(ws, snap)
            entries = len(snap.get("tree", {}))
            if diffs:
                print(f"CHANGED: {len(diffs)} difference(s) from the strict snapshot of {snap.get('taken_at')} (every file checked):")
                for line in diffs:
                    print(f"  {line}")
            else:
                print(f"unchanged: all {entries} entries and {len(snap.get('media', {}))} media match the strict snapshot of "
                      f"{snap.get('taken_at')} (every file checked, nothing exempt)")
            for line in notes:
                print(f"  note: {line}")
            quick = [n for n, e in snap.get("tree", {}).items() if e.get("hash") == "quick"]
            if quick:
                print(f"  note: {len(quick)} large file(s) compared by size and first and last MiB only: {', '.join(quick)}")
            return 1 if diffs else 0
        diffs = verify(ws, snap)
        present = [n for n in EXPECTED_TO_CHANGE if (ws / n.rstrip("/")).exists()]
        if diffs:
            print(f"CHANGED: {len(diffs)} canonical file(s) or media differ from the snapshot of {snap.get('taken_at')}:")
            for line in diffs:
                print(f"  {line}")
        else:
            print(f"unchanged: all {len(snap['files'])} canonical files and {len(snap['media'])} media match the snapshot of "
                  f"{snap.get('taken_at')}")
        print(f"expected to change (not checked): {', '.join(EXPECTED_TO_CHANGE)}; present now: {', '.join(present) or 'none'}; "
              "canonical mode checks only the canonical files: use snapshot --all / verify --strict for a workspace nothing may touch")
        return 1 if diffs else 0
    rep = report(ws)
    print(json.dumps(rep, indent=2) if args.json else _text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
