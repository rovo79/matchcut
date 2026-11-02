"""The automatic ([auto]) checks of the M3a acceptance run (docs/M3A-ACCEPTANCE.md).  It only reads the workspace: nothing is ever written
inside it.

    python3 tools/m3a_acceptance.py snapshot WS [-o FILE]   # record what must not change, in FILE (default: next to WS, never inside it)
    python3 tools/m3a_acceptance.py verify WS SNAPSHOT      # exit 0 when every recorded file is unchanged; else say which changed (exit 1)
    python3 tools/m3a_acceptance.py report WS [--json]      # the edit session, the published record, provenance, pins, mismatches, renders

``snapshot`` records the sha256 of ``final.json``, every pass file, ``roughcut.json``, ``editorial.json`` and ``human*.json``, and the size
and modification time of the source media the workspace uses (``config.json``'s ``source``, ``probe.json``'s file and the music track).
``verify`` expects ``edit.json``, ``edit_publish.json`` and ``edits/`` to change (an edit session and its publish write only those) and
lists them apart.  ``report`` summarises the session log, the published record, the provenance (base sha, latest canonical sha, session,
revision, whether each still matches), the pins the session does not protect, the acknowledged mismatches and the render paths to watch.

Passing these checks never passes a human gate: the videos must be watched and judged by the user.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA = "matchcut.m3a_acceptance_snapshot/1"
EXPECTED_TO_CHANGE = ("edit.json", "edit_publish.json", "edits/")


def _sha256(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
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


def media_files(ws: Path) -> Dict[str, str]:
    """role -> absolute path of the source media the workspace uses (``config.json``'s source and music track, ``probe.json``'s file)."""
    out: Dict[str, str] = {}
    cfg = _json(ws / "config.json") or {}
    probe = _json(ws / "probe.json") or {}
    if isinstance(cfg, dict) and cfg.get("source"):
        out["source"] = str(Path(cfg["source"]).expanduser())
    if isinstance(probe, dict) and probe.get("path") and str(Path(probe["path"]).expanduser()) != out.get("source"):
        out["probed source"] = str(Path(probe["path"]).expanduser())
    music = cfg.get("music") if isinstance(cfg, dict) else None
    if isinstance(music, dict) and music.get("path"):
        out["music"] = str(Path(music["path"]).expanduser())
    return out


def _stat(path: str) -> Optional[Dict[str, Any]]:
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def snapshot(ws: Path) -> Dict[str, Any]:
    return {"schema": SCHEMA, "workspace": str(ws.resolve()), "taken_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "files": {name: _sha256(ws / name) for name in tracked_files(ws)},
            "media": {role: {"path": path, "stat": _stat(path)} for role, path in media_files(ws).items()}}


def verify(ws: Path, snap: Dict[str, Any]) -> List[str]:
    """One line per difference between ``snap`` and the workspace now; [] when every recorded file and medium is unchanged."""
    out: List[str] = []
    now = snapshot(ws)
    for name, sha in sorted(snap.get("files", {}).items()):
        cur = now["files"].get(name, _sha256(ws / name))
        if cur != sha:
            out.append(f"{name}: " + ("missing now" if cur is None else "was missing, exists now" if sha is None else "changed"))
    for name in sorted(set(now["files"]) - set(snap.get("files", {}))):
        out.append(f"{name}: new since the snapshot")
    for role, entry in sorted(snap.get("media", {}).items()):
        cur = _stat(entry["path"])
        if cur != entry.get("stat"):
            out.append(f"{role} {entry['path']}: " + ("missing now" if cur is None else "size or modification time changed"))
    return out


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
                       ("acknowledged mismatches", "acknowledged_mismatches")):
        items = rep[key] or []
        lines.append(f"{title}: {len(items)}")
        for e in items:
            if key == "acknowledged_mismatches":
                lines.append(f"  {e['pin']}: holds {e['held_transition']}, published {e['transition']}")
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
    sp = sub.add_parser("snapshot", help="record the canonical files and the media in a file outside the workspace")
    sp.add_argument("workspace")
    sp.add_argument("-o", "--out", default=None, help="the snapshot file (default: <WS>.m3a-snapshot.json next to WS)")
    sp = sub.add_parser("verify", help="check the canonical files and media are unchanged since the snapshot")
    sp.add_argument("workspace")
    sp.add_argument("snapshot")
    sp = sub.add_parser("report", help="summarise the session, the published record, provenance, pins, mismatches and renders")
    sp.add_argument("workspace")
    sp.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    ws = Path(args.workspace)
    if not (ws / "config.json").is_file():
        print(f"{ws} is not a matchcut workspace (no config.json)", file=sys.stderr)
        return 2
    if args.command == "snapshot":
        out = Path(args.out) if args.out else ws.resolve().parent / f"{ws.resolve().name}.m3a-snapshot.json"
        if _inside(out, ws):
            print(f"refused: {out} is inside the workspace; a snapshot is kept outside it", file=sys.stderr)
            return 2
        snap = snapshot(ws)
        out.write_text(json.dumps(snap, indent=2) + "\n")
        print(f"snapshot of {len(snap['files'])} files and {len(snap['media'])} media -> {out}")
        for name, sha in snap["files"].items():
            print(f"  {(sha or 'missing')[:12]}  {name}")
        for role, e in snap["media"].items():
            print(f"  {role}: {e['path']} ({'missing' if e['stat'] is None else str(e['stat']['size']) + ' bytes'})")
        return 0
    if args.command == "verify":
        snap = _json(Path(args.snapshot))
        if not isinstance(snap, dict) or snap.get("schema") != SCHEMA:
            print(f"{args.snapshot} is not an M3a acceptance snapshot", file=sys.stderr)
            return 2
        diffs = verify(ws, snap)
        present = [n for n in EXPECTED_TO_CHANGE if (ws / n.rstrip("/")).exists()]
        if diffs:
            print(f"CHANGED: {len(diffs)} canonical file(s) or media differ from the snapshot of {snap.get('taken_at')}:")
            for line in diffs:
                print(f"  {line}")
        else:
            print(f"unchanged: all {len(snap['files'])} canonical files and {len(snap['media'])} media match the snapshot of "
                  f"{snap.get('taken_at')}")
        print(f"expected to change (not checked): {', '.join(EXPECTED_TO_CHANGE)}; present now: {', '.join(present) or 'none'}")
        return 1 if diffs else 0
    rep = report(ws)
    print(json.dumps(rep, indent=2) if args.json else _text(rep))
    return 0


if __name__ == "__main__":
    sys.exit(main())
