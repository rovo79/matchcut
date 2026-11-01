"""Publishing a session's revision as the workspace's ``edit`` stage (decision 1 of M3a).

``publish(ws, rev=None, replace=False)`` writes, under the session lock and each atomically:

- ``WS/edit.json``: the revision's timeline (stage ``edit``), listed by the engine after the canonical stages and before ``human``
  while it is fresh (``junctions.stale_edit``);
- ``WS/edits/published/<rev>-<sha12>.json``: an immutable copy of exactly those bytes (``<sha12>``: the first 12 hex characters of their
  sha256), never overwritten;
- ``WS/edit_publish.json`` (``matchcut.edit_publish/1``): the session id, the revision and its timeline sha256, the sha256 of
  ``edit.json``, the base stage and its sha256 when the session started, the latest canonical stage and its sha256, the time, the number
  of agent decisions and the published copy's path.

It never writes ``final.json``, a pass file, ``human.json`` or ``human_import.json``.

**What can be published.**  A revision of a fresh session (the head, or ``rev``), that is not ``r000``: ``r000`` is the base stage on whole
frames, with no agent edit, so there is nothing to publish (even when the frame normalisation moved a point, that is not an edit).  The
session must be based on the **latest canonical stage** (``final`` once the pipeline has finished): the edit stage's history is that stage's
history plus one ``agent`` decision per change, and a session's revisions carry their base's history.  A session on an earlier canonical
stage, or on ``human``, is refused (start a session on the latest canonical stage): splicing its edits onto another stage's history would
attribute the passes' or the editor's work to nothing, and a cut made from ``human`` listed before ``human`` would undo the editor's changes
in every junction's history.

**Never a silent overwrite.**  Publishing again from the same session replaces ``edit.json`` (the earlier copy stays in
``edits/published/``).  An ``edit.json`` published by another session, or one without a readable record that describes it (a record of
another file, a hand edit, a publish cut short between the two writes), is refused unless ``replace``: then the old pair (``edit.json``,
``edit_publish.json`` when there is one) is first moved to ``WS/edits/replaced/<sha12 of the old edit.json>/`` (``-2``, ``-3`` ... when
that exists).  Nothing is ever deleted.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from matchcut.pipeline.junctions import EDIT_FILE, EDIT_PUBLISH_SCHEMA, EDIT_RECORD, latest_canonical
from matchcut.pipeline.timeline import AGENT_PASS, EDIT_STAGE

from . import session as S
from .model import EditContext
from .session import SessionError

PUBLISHED_DIR = "published"
REPLACED_DIR = "replaced"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == EDIT_PUBLISH_SCHEMA else None


def _write_once(path: Path, data: bytes) -> None:
    """``data`` at ``path``, which is immutable: written when absent, left alone when it holds the same bytes, refused otherwise."""
    if path.exists():
        try:
            same = path.read_bytes() == data
        except OSError:
            same = False
        if not same:
            raise SessionError(f"{path.name} exists in edits/{PUBLISHED_DIR}/ with other content (changed by hand?): published copies are "
                               "never overwritten; move it away to publish this revision")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    S._atomic_write_bytes(path, data)


def _move_old(ws: Path, d: Path, old_sha: str) -> Path:
    """Move ``edit.json`` and ``edit_publish.json`` (when there is one) into a new ``WS/edits/replaced/<old_sha[:12]>[-n]/``."""
    base = d / REPLACED_DIR / old_sha[:12]
    target, n = base, 2
    while target.exists():
        target, n = base.with_name(f"{base.name}-{n}"), n + 1
    target.mkdir(parents=True)
    for name in (EDIT_FILE, EDIT_RECORD):
        if (ws / name).exists():
            os.rename(ws / name, target / name)
    S._fsync_dir(target)
    S._fsync_dir(ws)
    return target


def _rel(ws: Path, path: Path) -> str:
    return path.relative_to(ws).as_posix()


def publish(ws: Path, rev: Optional[str] = None, replace: bool = False, ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """Publish revision ``rev`` (default: the head) of the workspace's session as the ``edit`` stage; returns the record written, plus
    ``replaced`` (where an earlier edit stage was moved, or None) and ``previous`` (the revision the session published before, or None)."""
    ws = Path(ws)
    rid = None if rev is None else S.check_revision_id(rev)
    with S._locked(ws) as d:
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
        latest = latest_canonical(ws)
        if latest is None:
            raise SessionError(f"no canonical stage in {ws}: run the pipeline first")
        latest_name, latest_path = latest
        if s.base_stage != latest_name:
            raise SessionError(f"the session is based on {s.base_stage}, not on the latest canonical stage {latest_name}: the edit stage "
                               f"continues the history of {latest_name}, so only a session started on it can publish (start "
                               f'{{"stage": "{latest_name}", "restart": true}} and redo the edits)')
        latest_sha = S._sha256_file(latest_path)
        if latest_sha is None or latest_sha != s.state["base_sha256"]:  # (a fresh session's base file is unchanged: a race)
            raise SessionError(f"{latest_path.name} changed while publishing: run start to begin a new session")
        if tl.stage != EDIT_STAGE:
            raise SessionError(f"revision {rid} is not an edit stage timeline (stage {tl.stage!r})")  # pragma: no cover - revisions are
        data = (json.dumps(tl.to_dict(), indent=2) + "\n").encode("utf-8")
        sha = _sha256(data)
        edit_path, record_path = ws / EDIT_FILE, ws / EDIT_RECORD
        moved: Optional[Path] = None
        previous: Optional[str] = None
        if edit_path.exists():
            old_sha = S._sha256_file(edit_path) or ""
            old = _read_record(record_path)
            if old is not None and old.get("edit_sha256") == old_sha and old.get("session") == s.session_id:
                previous = str(old.get("revision") or "") or None
            elif not replace:
                if old is None:
                    why = f"{EDIT_FILE} has no readable {EDIT_RECORD}"
                elif old.get("edit_sha256") != old_sha:
                    why = f"{EDIT_FILE} is not the file {EDIT_RECORD} describes (changed since it was published?)"
                else:
                    why = f"{EDIT_FILE} was published by another session ({old.get('session')}, revision {old.get('revision')})"
                raise SessionError(f"{why}: publishing would replace it; pass replace to move it to edits/{REPLACED_DIR}/{old_sha[:12]}/ "
                                   "first")
            else:
                moved = _move_old(ws, d, old_sha)
        copy = d / PUBLISHED_DIR / f"{rid}-{sha[:12]}.json"
        _write_once(copy, data)
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
        S._atomic_write(record_path, json.dumps(record, indent=2) + "\n")
    return dict(record, replaced=_rel(ws, moved) if moved is not None else None, previous=previous)
