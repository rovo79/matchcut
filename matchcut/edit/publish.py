"""Publishing a session's revision as the workspace's ``edit`` stage (decision 1 of M3a).

``publish(ws, rev=None, replace=False, supersede_human=False)`` writes, under the session lock and the workspace's stage lock
(``interchange.workspace.stage_lock``, which an import holds too), each atomically:

- ``WS/edits/published/<rev>-<sha12>.json``: an immutable copy of the bytes published (``<sha12>``: the first 12 hex characters of their
  sha256), never overwritten;
- ``WS/edit.json``: the revision's timeline (stage ``edit``), listed by the engine after the canonical stages and before ``human`` while
  it is fresh (``junctions.stale_edit``);
- ``WS/edit_publish.json`` (``matchcut.edit_publish/1``): the session id, the revision and its timeline sha256, the sha256 of
  ``edit.json``, the base stage and its sha256 when the session started, the latest canonical stage and its sha256, the time, the number
  of agent decisions and the published copy's path; a copy of every record is kept in ``edits/published/records/``;
- one line in ``WS/edits/published/log.jsonl`` (append-only): when the copy went live, from which session and revision, its sha256, and
  whether an edit stage was replaced or a human stage superseded.

It never writes ``final.json``, a pass file, ``human.json`` or ``human_import.json``.

**What can be published.**  A revision of a fresh session (the head, or ``rev``), that is not ``r000``: ``r000`` is the base stage on whole
frames, with no agent edit, so there is nothing to publish (even when the frame normalisation moved a point, that is not an edit).  The
session must be based on the **latest canonical stage** (``session.publish_blocker``): the edit stage's history is that stage's history
plus one ``agent`` decision per change, and a session's revisions carry their base's history.  The revision must still hold every editor
pin the base holds under the pins stored **now** (clips, adjacency, cut points, and a protected transition): a pin added or changed since
the revision was made is checked here, and a revision that does not hold it is refused (propose again, or release the pin).

**Never a silent overwrite.**  Publishing again from the same session replaces ``edit.json`` (the earlier copy stays in
``edits/published/``).  An ``edit.json`` published by another session, or one without a readable record that describes it (a record of
another file, a hand edit), is refused unless ``replace``: then the old pair (``edit.json``, ``edit_publish.json`` when there is one) is first
moved to ``WS/edits/replaced/<sha12 of the old edit.json>/`` (``-2``, ``-3`` ... when that exists).  A publish cut short after ``edit.json``
was written (its record not yet) is recognised: ``edit.json`` is then byte for byte what this session publishes for one of its revisions,
and the next publish from the session goes ahead.  A record left without its ``edit.json`` is archived in ``edits/replaced/`` before a new
one is written.  Nothing is ever deleted.

**The human stage.**  A fresh ``human.json`` (an editor's re-export) is the latest stage.  A publish that would leave it out of the stage
order as stale (a human stage based on a canonical stage, whose history does not continue the edit stage's; or one based on the edit stage,
when ``edit.json`` changes) is refused unless ``supersede_human``; the result then says why the human stage is left out
(``human_left_out``).  ``human.json`` and ``human_import.json`` are never changed: re-import the editor's file to bring it back.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from matchcut.interchange.common import junction_ids
from matchcut.interchange.workspace import stage_lock
from matchcut.pipeline.junctions import EDIT_FILE, EDIT_PUBLISH_SCHEMA, EDIT_RECORD, latest_canonical, stale_human
from matchcut.pipeline.timeline import AGENT_PASS, EDIT_STAGE, Timeline

from . import rules
from . import session as S
from .model import EditContext, frame_rows
from .ops import held_pins, pin_label
from .session import SessionError

PUBLISHED_DIR = "published"
REPLACED_DIR = "replaced"
RECORDS_DIR = "records"  # edits/published/records/: a copy of every edit_publish.json written
LOG_FILE = "log.jsonl"  # edits/published/log.jsonl: one line per publish
HUMAN_FILE, HUMAN_RECORD = "human.json", "human_import.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == EDIT_PUBLISH_SCHEMA else None


def _timeline_bytes(tl: Timeline) -> bytes:
    """What ``edit.json`` holds for a revision's timeline (and its published copy)."""
    return (json.dumps(tl.to_dict(), indent=2) + "\n").encode("utf-8")


def _write_once(path: Path, data: bytes, what: str) -> None:
    """``data`` at ``path``, which is immutable: written when absent, left alone when it holds the same bytes, refused otherwise."""
    if path.exists():
        try:
            same = path.read_bytes() == data
        except OSError:
            same = False
        if not same:
            raise SessionError(f"{path.name} exists in {what} with other content (changed by hand?): published copies are never "
                               "overwritten; move it away to publish this revision")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    S._atomic_write_bytes(path, data)


def _unique_dir(base: Path) -> Path:
    target, n = base, 2
    while target.exists():
        target, n = base.with_name(f"{base.name}-{n}"), n + 1
    target.mkdir(parents=True)
    return target


def _move_old(ws: Path, d: Path, old_sha: str) -> Path:
    """Move ``edit.json`` and ``edit_publish.json`` (when there is one) into a new ``WS/edits/replaced/<old_sha[:12]>[-n]/``."""
    target = _unique_dir(d / REPLACED_DIR / old_sha[:12])
    for name in (EDIT_FILE, EDIT_RECORD):
        if (ws / name).exists():
            os.rename(ws / name, target / name)
    S._fsync_dir(target)
    S._fsync_dir(ws)
    return target


def _archive_orphan_record(ws: Path, d: Path) -> Optional[Path]:
    """An ``edit_publish.json`` with no ``edit.json`` (a move-aside cut short) is moved into ``edits/replaced/record-<sha12>[-n]/``."""
    path = ws / EDIT_RECORD
    if (ws / EDIT_FILE).exists() or not path.exists():
        return None
    target = _unique_dir(d / REPLACED_DIR / f"record-{(S._sha256_file(path) or '0' * 12)[:12]}")
    os.rename(path, target / EDIT_RECORD)
    S._fsync_dir(target)
    S._fsync_dir(ws)
    return target


def _interrupted_publish(s: S.Session, d: Path, data: bytes) -> Optional[str]:
    """The revision of this session that ``data`` (the current ``edit.json``) is the publish of, when it is one: a copy of it is in
    ``edits/published/`` under that revision's name, and that revision of this session serialises to exactly these bytes.  None otherwise."""
    sha = _sha256(data)
    for copy in sorted((d / PUBLISHED_DIR).glob(f"r*-{sha[:12]}.json")):
        rid = copy.stem.rsplit("-", 1)[0]
        try:
            if copy.read_bytes() == data and _timeline_bytes(s.timeline(rid)) == data:
                return rid
        except (OSError, SessionError):
            continue
    return None


def _pin_problems(base: Timeline, tl: Timeline, ctx: EditContext) -> List[Tuple[str, str]]:
    """What ``tl`` breaks of the pins stored now that ``base`` (r000) holds: their clips, adjacency and cut points (``rules.pin_problems``,
    the rule every proposal is checked with), and a protected transition: the ``held_transition`` the pin stores (else the base's)."""
    plain = dataclasses.replace(ctx, pins=tuple(dataclasses.replace(p, hold_transition=False, held_transition=None) for p in ctx.pins))
    out = [(pin_label(p), line) for p, line in rules.pin_problems(base, tl, plain)]
    rows_b, rows_t = frame_rows(base, ctx.rate), frame_rows(tl, ctx.rate)
    sites = {id(p): site for p, site in held_pins(tl, ctx)}
    for pin, site in held_pins(base, ctx):
        k = sites.get(id(pin))
        if not pin.hold_transition or k is None:
            continue
        want = pin.held_transition or {"kind": rows_b[site + 1].kind, "frames": rows_b[site + 1].tr}
        now = {"kind": rows_t[k + 1].kind if rows_t[k + 1].tr else "cut", "frames": rows_t[k + 1].tr}
        wanted = {"kind": want.get("kind") if want.get("frames") else "cut", "frames": int(want.get("frames") or 0)}
        if now != wanted:
            out.append((pin_label(pin), f"changes the transition the editor pin {pin_label(pin)} protects ({rules._label(wanted['kind'], wanted['frames'])} -> "
                       f"{rules._label(now['kind'], now['frames'])})"))
    return out


def _pin_states(ws: Path, tl: Timeline, ctx: EditContext) -> List[Dict[str, Any]]:
    """Every stored pin and where it stands in the published cut, worded for the edit stage."""
    from .view import _pins

    out = []
    for entry in _pins(tl, frame_rows(tl, ctx.rate), ctx, junction_ids(tl)):
        e = {k: entry[k] for k in ("pin", "state", "reason", "junction", "hold_transition", "held_transition")}
        if e["state"] == "pending":
            e["state"] = "not_held"
            e["reason"] = ("not held by this edit: the pin was stored after the pipeline's last run, so the base stage does not hold it "
                           "either (a pipeline run would apply it, and leave this edit stage out as stale)")
        out.append(e)
    return out


def _human_superseded(ws: Path, data: bytes) -> Optional[str]:
    """Why publishing ``data`` would leave a fresh ``human.json`` out of the stage order, or None (no human.json, a stale one already, or
    a human stage that continues exactly this edit stage)."""
    if not (ws / HUMAN_FILE).exists() or stale_human(ws) is not None:
        return None
    try:
        record = json.loads((ws / HUMAN_RECORD).read_text(encoding="utf-8"))
        history = str(record.get("history_stage") or record.get("latest_stage") or "")
        compared = str(record.get("stage") or "")
    except (OSError, ValueError, AttributeError):
        history, compared = "", ""
    current = S._sha256_file(ws / EDIT_FILE)
    if history == EDIT_STAGE and current == _sha256(data):
        return None  # the same edit.json again: the human stage still continues it
    if history == EDIT_STAGE:
        return (f"human.json (an editor's re-export, imported against {compared or 'the edit stage'}) continues the edit stage that is "
                "published now: a new edit.json would leave it out of the stage order as stale")
    return (f"human.json (an editor's re-export, imported against {compared or 'a canonical stage'}) is the latest stage and its history "
            "continues the canonical stages, not an edit stage: publishing would list the edit stage before it and leave it out of the "
            "stage order as stale")


def _log(d: Path, entry: Dict[str, Any]) -> None:
    path = d / PUBLISHED_DIR / LOG_FILE
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def _rel(ws: Path, path: Path) -> str:
    return path.relative_to(ws).as_posix()


def publish(ws: Path, rev: Optional[str] = None, replace: bool = False, supersede_human: bool = False,
            ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """Publish revision ``rev`` (default: the head) of the workspace's session as the ``edit`` stage; returns the record written, plus
    ``replaced`` (where an earlier edit stage was moved, or None), ``previous`` (the revision the session published before, or None),
    ``human_left_out`` (why a human stage this publish superseded is left out, or None) and ``pins`` (every stored pin and where it stands
    in the published cut)."""
    ws = Path(ws)
    rid = None if rev is None else S.check_revision_id(rev)
    with S._locked(ws) as d, stage_lock(ws):
        s = S._fresh(ws, ctx)
        rid = rid or s.head
        if not (s.dir / "revisions" / f"{rid}.json").exists():
            raise SessionError(f"no revision {rid} (revisions: {', '.join(s.revision_ids())})")
        doc = s.revision(rid)
        tl = s.timeline(rid)
        if doc.get("parent") is None:
            raise SessionError(f"nothing to publish: {rid} is the base stage on whole frames, with no agent edit (apply a proposal, or "
                               "check out a later revision)")
        agent = [x for x in tl.history if x.pass_name == AGENT_PASS]
        if not agent:
            raise SessionError(f"nothing to publish: {rid} holds no agent decision")
        blocker = S.publish_blocker(ws, s.base_stage)
        if blocker:
            raise SessionError(blocker)
        latest_name, latest_path = latest_canonical(ws)  # (not None: publish_blocker said so)
        latest_sha = S._sha256_file(latest_path)
        if latest_sha is None or latest_sha != s.state["base_sha256"]:  # (a fresh session's base file is unchanged: a race)
            raise SessionError(f"{latest_path.name} changed while publishing: run start to begin a new session")
        if tl.stage != EDIT_STAGE:
            raise SessionError(f"revision {rid} is not an edit stage timeline (stage {tl.stage!r})")  # pragma: no cover
        context = s.context()
        broken = _pin_problems(s.timeline("r000"), tl, context)
        if broken:
            label, line = broken[0]
            raise SessionError(f"pin {label} was added or changed since {rid} was made, and {rid} {line}: propose the edits again "
                               "(or release the pin)" + (f"; also: {'; '.join(x for _, x in broken[1:])}" if len(broken) > 1 else ""))
        data = _timeline_bytes(tl)
        sha = _sha256(data)
        superseded = _human_superseded(ws, data)
        if superseded and not supersede_human:
            raise SessionError(f"{superseded}; human.json and human_import.json are never changed (re-import the editor's file after "
                               "publishing to bring it back): pass supersede_human to publish anyway")
        edit_path, record_path = ws / EDIT_FILE, ws / EDIT_RECORD
        moved: Optional[Path] = None
        previous: Optional[str] = None
        if edit_path.exists():
            current = edit_path.read_bytes()
            old_sha = _sha256(current)
            old = _read_record(record_path)
            if old is not None and old.get("edit_sha256") == old_sha and old.get("session") == s.session_id:
                previous = str(old.get("revision") or "") or None
            elif (old is None or old.get("edit_sha256") != old_sha) and (recovered := _interrupted_publish(s, d, current)) is not None:
                previous = recovered  # this session's own publish, cut short before its record was written
            elif not replace:
                if old is None:
                    why = f"{EDIT_FILE} has no readable {EDIT_RECORD} (an interrupted publish?)"
                elif old.get("edit_sha256") != old_sha:
                    why = f"{EDIT_FILE} is not the file {EDIT_RECORD} describes (changed since it was published, or an interrupted publish?)"
                else:
                    why = f"{EDIT_FILE} was published by another session ({old.get('session')}, revision {old.get('revision')})"
                raise SessionError(f"{why}: publishing would replace it; pass replace to move it to edits/{REPLACED_DIR}/{old_sha[:12]}/ "
                                   "first")
            else:
                moved = _move_old(ws, d, old_sha)
        orphan = _archive_orphan_record(ws, d)
        copy = d / PUBLISHED_DIR / f"{rid}-{sha[:12]}.json"
        _write_once(copy, data, f"edits/{PUBLISHED_DIR}/")
        S._atomic_write_bytes(edit_path, data)
        record = {
            "schema": EDIT_PUBLISH_SCHEMA, "session": s.session_id, "revision": rid, "timeline_sha256": doc["timeline_sha256"],
            "edit_file": EDIT_FILE, "edit_sha256": sha,
            "base_stage": s.base_stage, "base_file": s.state["base_file"], "base_sha256": s.state["base_sha256"],
            "latest_stage": latest_name, "latest_stage_file": _rel(ws, latest_path), "latest_stage_sha256": latest_sha,
            "published_at": S._now(), "decisions": len(agent), "published": _rel(ws, copy),
        }
        if moved is not None:
            record["replaced"] = _rel(ws, moved)
        if superseded:
            record["superseded_human"] = superseded
        text = json.dumps(record, indent=2) + "\n"
        stamp = record["published_at"].replace(":", "").replace("-", "").replace("+0000", "Z")
        records = d / PUBLISHED_DIR / RECORDS_DIR
        name, n = f"{stamp}-{rid}-{sha[:12]}", 2
        while (records / f"{name}.json").exists():
            name, n = f"{stamp}-{rid}-{sha[:12]}-{n}", n + 1
        _write_once(records / f"{name}.json", text.encode("utf-8"), f"edits/{PUBLISHED_DIR}/{RECORDS_DIR}/")
        S._atomic_write(record_path, text)
        left_out = stale_human(ws) if superseded else None
        _log(d, {"published_at": record["published_at"], "session": s.session_id, "revision": rid, "edit_sha256": sha,
                 "published": record["published"], "replaced": record.get("replaced"), "superseded_human": bool(superseded),
                 "orphan_record": _rel(ws, orphan) if orphan is not None else None, "previous": previous})
        pins = _pin_states(ws, tl, context)
    return dict(record, replaced=_rel(ws, moved) if moved is not None else None, previous=previous, human_left_out=left_out, pins=pins)
