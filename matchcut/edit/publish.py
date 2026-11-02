"""Publishing a session's revision as the workspace's ``edit`` stage (decision 1 of M3a).

``publish(ws, rev=None, replace=False, supersede_human=None, acknowledge_mismatches=None)`` writes, under the session lock and the workspace's stage lock
(``interchange.workspace.stage_lock``, which an import holds too), each atomically:

- ``WS/edits/published/<rev>-<sha12>.json``: an immutable copy of the bytes published (``<sha12>``: the first 12 hex characters of their
  sha256), never overwritten;
- ``WS/edit.json``: the revision's timeline (stage ``edit``), listed by the engine after the canonical stages and before ``human`` while
  it is fresh (``junctions.stale_edit``);
- ``WS/edit_publish.json`` (``matchcut.edit_publish/1``): the session id, the revision and its timeline sha256, the sha256 of
  ``edit.json``, the base stage and its sha256 when the session started, the latest canonical stage and its sha256, the time, the number
  of agent decisions, the published copy's path, ``transition_mismatches`` (``rules.transition_mismatches`` of the published cut: the
  protected pins whose transition differs from the one they hold), ``acknowledged_mismatches`` (the ones the user approved carrying
  forward), ``unapplied_pins`` (``rules.unapplied_pins``: pins the session did not protect, ``not protected by this edit``) and, when a
  human stage was superseded, its reason and ``superseded_human_sha256``; a copy of every record is kept in
  ``edits/published/records/``
  (``<UTC second>-<nnn>-<rev>-<sha12>.json``, ``nnn`` counting within the second, so the names sort in the order they were written);
- one line in ``WS/edits/published/log.jsonl`` (append-only): when the copy went live, from which session and revision, its sha256, the
  revision published before (``previous``), one whose interrupted publish this completes (``recovered``), and whether an edit stage was
  replaced or a human stage superseded.

They are written in that order, except that the log line goes just before ``edit_publish.json``: a crash between the two leaves a log line
(and a live ``edit.json``) without its record, which the session's next publish recovers and logs again; a record is never written without
its log line.

It never writes ``final.json``, a pass file, ``human.json`` or ``human_import.json``.

**What can be published.**  A revision of a fresh session (the head, or ``rev``), that is not ``r000``: ``r000`` is the base stage on whole
frames, with no agent edit, so there is nothing to publish (even when the frame normalisation moved a point, that is not an edit).  The
session must be based on the **latest canonical stage** (``session.publish_blocker``): the edit stage's history is that stage's history
plus one ``agent`` decision per change, and a session's revisions carry their base's history.  The revision must still hold every editor
pin the base holds under the pins stored **now** (clips, adjacency, cut points, and a protected transition: r000's, or exactly the one the
pin holds, as ``rules.pin_problems`` checks every proposal): a pin added or changed since the revision was made is checked here, and a
revision that does not hold it is refused (propose again, or release the pin).  A pin r000 does not hold (``rules.unapplied_pins``: made
after the pipeline's last run, or never placed) fails closed: a revision that changes its two shots' clips or junctions is refused
(``rules.unapplied_problems``, from r000, so a pin made after ``apply`` counts too); one that leaves them untouched publishes and lists it
as not protected.

**Protected-transition mismatches.**  While a protected pin's transition in the revision differs from the one it holds, the publish is
refused with the repair (the exact ``set_transition``) and the alternative: ``acknowledge_mismatches`` naming every mismatching pin
exactly, which only the user may approve (an agent never passes it on its own).  An acknowledged mismatch is recorded and its pin reads
``mismatch_acknowledged``, never held.

**Protection changes.**  ``start`` records every pin's protection in r000 (``pin_protections``, the session's baseline).  A protection
released, replaced or weakened since (or cleared with its pin: ``rules.protection_changes``) is refused the same way, even when the cut
has the transition the old protection held: the user restores it, or acknowledges it in the same ``acknowledge_mismatches`` list (one list
naming every pin whose protection the publish does not keep).  The result, the record (``acknowledged_protection_changes``, with
``pin_protections_at_start``) and the log line say so, and the pin reads ``protection_change_acknowledged``, never held.

**Never a silent overwrite.**  Publishing again from the same session replaces ``edit.json`` (the earlier copy stays in
``edits/published/``).  An ``edit.json`` published by another session, or one without a readable record that describes it (a record of
another file, a hand edit), is refused unless ``replace``: then the old pair (``edit.json``, ``edit_publish.json`` when there is one) is first
moved to ``WS/edits/replaced/<sha12 of the old edit.json>/`` (``-2``, ``-3`` ... when that exists).  A publish cut short after ``edit.json``
was written (its record not yet) is recognised: ``edit.json`` is then byte for byte what this session publishes for one of its revisions,
and the next publish from the session goes ahead (``recovered`` names that revision; ``previous`` the one the old record names, when it
is readable and this session's).  A record left without its ``edit.json`` is archived in ``edits/replaced/`` before a new
one is written.  Nothing is ever deleted.

**The human stage.**  A fresh ``human.json`` (an editor's re-export) is the latest stage.  A publish that would leave it out of the stage
order as stale (a human stage based on a canonical stage, whose history does not continue the edit stage's; or one based on the edit stage,
when ``edit.json`` changes) is refused unless ``supersede_human`` is the sha12 of that very ``human.json`` (the user's explicit approval,
bound to the file: ``true``, a wrong or stale value, or a value with nothing to supersede is refused); the result then says why the human
stage is left out (``human_left_out``).  ``human.json`` and ``human_import.json`` are never changed: re-import the editor's file to bring
it back.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from matchcut.interchange.common import junction_ids
from matchcut.interchange.workspace import stage_lock
from matchcut.pipeline.junctions import EDIT_FILE, EDIT_PUBLISH_SCHEMA, EDIT_RECORD, latest_canonical, stale_human
from matchcut.pipeline.timeline import AGENT_PASS, EDIT_STAGE, Timeline

from . import rules
from . import session as S
from .model import EditContext, frame_rows
from .ops import pin_label
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
    """What ``tl`` breaks of the pins stored now that ``base`` (r000) holds: their clips, adjacency and cut points, and a protected
    transition, which must be r000's or exactly the one the pin holds (an untouched mismatch publishes; a repair too).  It is
    ``rules.pin_problems``, the rule every proposal is checked with, applied from r000: one function, so publish never refuses what the
    proposals could not avoid."""
    return [(pin_label(p), line) for p, line in rules.pin_problems(base, tl, ctx)]


def _pin_states(ws: Path, tl: Timeline, ctx: EditContext, unapplied: List[Dict[str, Any]], acknowledged: Dict[str, Any],
                changes: Sequence[Dict[str, Any]] = ()) -> List[Dict[str, Any]]:
    """Every stored pin and where it stands in the published cut, worded for the edit stage: ``held``; ``mismatch_acknowledged`` (its clips
    and cut points held, its protected transition not: carried forward with the user's acknowledgement); ``protection_change_acknowledged``
    (the protection the pin had when the session started was released, replaced or weakened during it, and the user acknowledged that at
    publish; a pin cleared during the session is listed too); ``not_protected`` (a pin the session's base does not hold:
    ``rules.unapplied_pins``); never held when it is not."""
    from .view import _pins

    out = []
    for entry in _pins(tl, frame_rows(tl, ctx.rate), ctx, junction_ids(tl), unapplied, acknowledged, changes,
                       changes_acknowledged=True):
        e = {k: entry.get(k) for k in ("pin", "state", "reason", "junction", "hold_transition", "held_transition", "transition_mismatch",
                                       "protection_change")}
        if entry.get("protected") is False:
            e["state"] = "not_protected"
            e["reason"] = f"not protected by this edit: {entry['reason']}; {entry['action']}"
        out.append(e)
    return out


def _check_acknowledged(rid: str, mismatches: List[Dict[str, Any]], changes: List[Dict[str, Any]], acknowledge: Any,
                        labels: List[str]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(the mismatches, the protection changes) that ``acknowledge`` (the pins the user approved carrying forward) acknowledges: it must
    name every pin with a transition mismatch or a protection changed during the session exactly once, and nothing else.  Raises
    ``SessionError`` otherwise, saying how to repair each one or what the user may acknowledge."""
    if acknowledge is None:
        acknowledge = []
    if not isinstance(acknowledge, (list, tuple)) or not all(isinstance(x, str) for x in acknowledge):
        raise SessionError(f"acknowledge_mismatches must be a list of pin labels (e.g. [\"s1->s3\"]), not {str(acknowledge)[:60]!r}")
    if not mismatches and not changes and not acknowledge:
        return [], []
    wanted = list(dict.fromkeys([m["pin"] for m in mismatches] + [c["pin"] for c in changes]))
    issues: List[str] = []
    seen: set = set()
    for x in acknowledge:
        if x in seen:
            issues.append(f"it names {x} twice")
        seen.add(x)
        if x not in labels:
            issues.append(f"{x} is not a stored pin" + (f" (pins: {', '.join(labels)})" if labels else " (there are no pins)"))
        elif x not in wanted:
            issues.append(f"{x} has no transition mismatch and no protection change in {rid}")
    missing = [x for x in wanted if x not in seen]
    if missing:
        issues.append(f"it does not name {', '.join(missing)}")
    if not issues:
        return ([{"pin": m["pin"], "n": m["n"], "junction": m["junction"], "held_transition": m["held_transition"],
                  "transition": m["transition"]} for m in mismatches],
                [{k: c[k] for k in ("pin", "change", "original", "now", "n", "junction", "transition", "reason")} for c in changes])
    lines = []
    for m in mismatches:
        held, now = m["held_transition"], m["transition"]
        op = rules.repair_op(m["n"], (held["kind"], held["frames"]))
        lines.append(f"pin {m['pin']} holds a {_tr(held)} but junction {m['n']} ({m['junction']}) has a {_tr(now)}: repair it by "
                     f"proposing {json.dumps(op)}, applying it and publishing again")
    for c in changes:
        lines.append(f"{c['reason']}: publishing would not keep the protection the session started with; ask the user whether to restore "
                     "it (only they may change a protection) or to carry the change forward")
    n = len(mismatches) + len(changes)
    head = (f"{rid} cannot be published as protected: " + "; ".join(lines)) if n else \
        f"{rid} has no protected transition that differs from what its pin protects and no protection changed during the session"
    ack = json.dumps({"acknowledge_mismatches": wanted})
    return_how = (f". Only if the user explicitly approves carrying {'these' if len(wanted) > 1 else 'this'} forward, publish {ack} (it "
                  "must name every such pin, exactly); the record then says the protection was not kept. An agent must never acknowledge "
                  "a mismatch or a protection change without the user's explicit approval") if n else \
        ": publish without acknowledge_mismatches"
    given = f"acknowledge_mismatches {json.dumps(list(acknowledge))} is refused: {'; '.join(issues)}. " if acknowledge else ""
    raise SessionError(f"{given}{head}{return_how}")


def _tr(t: Dict[str, Any]) -> str:
    return "cut" if not t.get("frames") or t.get("kind") == "cut" else f"{t['kind']} {t['frames']}f"


SHA12 = re.compile(r"[0-9a-f]{12}")


def _check_supersede(supersede_human: Any, superseded: Optional[str], human_sha: Optional[str]) -> None:
    """``supersede_human`` must be the sha12 of the ``human.json`` this publish would leave out (the file the user approved superseding),
    given only when there is one; ``SessionError`` otherwise."""
    shown = f'"{human_sha[:12]}"' if human_sha else '"<sha12 of human.json>"'
    if supersede_human is False:  # (the old form's "no": nothing is superseded)
        supersede_human = None
    if supersede_human is True:
        raise SessionError("supersede_human: true is no longer accepted: it now names the very human.json it supersedes, as the first 12 "
                           f"hex characters of its sha256 (supersede_human: {shown}), and an agent must never pass it without the user's "
                           "explicit approval")
    if supersede_human is not None and (not isinstance(supersede_human, str) or not SHA12.fullmatch(supersede_human)):
        raise SessionError(f"supersede_human must be the first 12 hex characters of the sha256 of the human.json to supersede, not "
                           f"{str(supersede_human)[:40]!r}")
    if superseded is None:
        if supersede_human is not None:
            raise SessionError(f"supersede_human names {supersede_human}, but this publish leaves no fresh human stage out (human.json is "
                               "absent, stale already, or continues exactly this edit): there is nothing to supersede; publish without it")
        return
    if supersede_human is None:
        raise SessionError(f"{superseded}; human.json and human_import.json are never changed or deleted (re-import the editor's file after "
                           f"publishing to bring it back). human.json is {human_sha[:12] if human_sha else 'unreadable'}: to publish anyway, "
                           f"the user must explicitly approve superseding it, then pass supersede_human: {shown} (an agent must never "
                           "pass it without the user's explicit approval)")
    if human_sha is None or supersede_human != human_sha[:12]:
        raise SessionError(f"supersede_human names {supersede_human}, but human.json is {human_sha[:12] if human_sha else 'unreadable'} now "
                           "(a different file from the one named: it changed since, or the value is wrong): ask the user again before "
                           "superseding it")


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


def publish(ws: Path, rev: Optional[str] = None, replace: bool = False, supersede_human: Any = None,
            acknowledge_mismatches: Optional[Sequence[str]] = None, ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """Publish revision ``rev`` (default: the head) of the workspace's session as the ``edit`` stage; returns the record written, plus
    ``replaced`` (where an earlier edit stage was moved, or None), ``previous`` (the revision the session published before, or None),
    ``recovered`` (the revision whose publish this one completes, when an earlier publish was cut short before its record, or None),
    ``human_left_out`` (why a human stage this publish superseded is left out, or None) and ``pins`` (every stored pin and where it stands
    in the published cut, with ``transition_mismatch``).  The record's ``transition_mismatches`` lists the protected pins whose transition
    the published cut leaves different from the held one (``rules.transition_mismatches``)."""
    ws = Path(ws)
    rid = None if rev is None else S.check_revision_id(rev)
    with S._locked(ws) as d, stage_lock(ws, error=SessionError):
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
        r000 = s.timeline("r000")
        broken = _pin_problems(r000, tl, context)
        mismatches = rules.transition_mismatches(tl, context)  # untouched ones: any other transition there is refused just above
        if broken:
            label, line = broken[0]
            raise SessionError(f"pin {label} was added or changed since {rid} was made, and {rid} {line}: propose the edits again "
                               "(or release the pin)" + (f"; also: {'; '.join(x for _, x in broken[1:])}" if len(broken) > 1 else ""))
        # a pin stored now that r000 does not hold (made after the pipeline's last run, or one it could not place): the revision may not
        # change its clips or junctions (the same rule as every proposal, applied from r000, so a pin stored after apply counts too)
        unapplied = S.unapplied_pins(s)
        hits = rules.unapplied_problems(r000, tl, unapplied)
        if hits:
            raise SessionError(f"{rid} cannot be published: " + "; ".join(hits))
        changes = S.protection_changes(s, tl)  # a protection released, replaced or weakened since the session started
        baseline = S.baseline_protections(s) or {}
        labels = list(dict.fromkeys([pin_label(p) for p in context.pins] + [c["pin"] for c in changes]))
        acknowledged, acknowledged_changes = _check_acknowledged(rid, mismatches, changes, acknowledge_mismatches, labels)
        data = _timeline_bytes(tl)
        sha = _sha256(data)
        superseded = _human_superseded(ws, data)
        human_sha = S._sha256_file(ws / HUMAN_FILE) if superseded else None
        _check_supersede(supersede_human, superseded, human_sha)
        edit_path, record_path = ws / EDIT_FILE, ws / EDIT_RECORD
        moved: Optional[Path] = None
        previous: Optional[str] = None
        recovered: Optional[str] = None
        if edit_path.exists():
            current = edit_path.read_bytes()
            old_sha = _sha256(current)
            old = _read_record(record_path)
            if old is not None and old.get("edit_sha256") == old_sha and old.get("session") == s.session_id:
                previous = str(old.get("revision") or "") or None
            elif (old is None or old.get("edit_sha256") != old_sha) and (recovered := _interrupted_publish(s, d, current)) is not None:
                # this session's own publish, cut short before its record was written: the revision published before is the one the old
                # record names (when it is readable and this session's), never the interrupted one
                previous = (str(old.get("revision") or "") or None) if old is not None and old.get("session") == s.session_id else None
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
            "published_at": S._now(), "decisions": len(agent), "published": _rel(ws, copy), "transition_mismatches": mismatches,
            "acknowledged_mismatches": acknowledged, "acknowledged_protection_changes": acknowledged_changes,
            "pin_protections_at_start": baseline,
            "unapplied_pins": [dict(e, status="not protected by this edit") for e in unapplied],
        }
        if moved is not None:
            record["replaced"] = _rel(ws, moved)
        if superseded:
            record["superseded_human"] = superseded
            record["superseded_human_sha256"] = human_sha
        text = json.dumps(record, indent=2) + "\n"
        stamp = record["published_at"].replace(":", "").replace("-", "").replace("+0000", "Z")
        records = d / PUBLISHED_DIR / RECORDS_DIR
        # <stamp>-<nnn>-<rev>-<sha12>: a counter within the second, so the copies sort in the order they were written
        within = re.compile(rf"{re.escape(stamp)}-([0-9]{{3,}})-")
        same_second = [int(m.group(1)) for f in records.glob(f"{stamp}-*.json") if (m := within.match(f.name))]
        name = f"{stamp}-{max(same_second, default=0) + 1:03d}-{rid}-{sha[:12]}"
        _write_once(records / f"{name}.json", text.encode("utf-8"), f"edits/{PUBLISHED_DIR}/{RECORDS_DIR}/")
        # the log line goes before the record replace: a crash between the two leaves a line for a publish whose edit.json is live but
        # whose record is missing (the session's next publish recovers it, logging it again with "recovered"), never a record without one
        _log(d, {"published_at": record["published_at"], "session": s.session_id, "revision": rid, "edit_sha256": sha,
                 "published": record["published"], "replaced": record.get("replaced"), "superseded_human": bool(superseded),
                 "orphan_record": _rel(ws, orphan) if orphan is not None else None, "previous": previous, "recovered": recovered,
                 "transition_mismatches": len(mismatches), "acknowledged_mismatches": acknowledged,
                 "acknowledged_protection_changes": [{k: c[k] for k in ("pin", "change", "original", "now", "transition")}
                                                     for c in acknowledged_changes],
                 "superseded_human_sha256": human_sha, "unapplied_pins": [e["pin"] for e in unapplied]})
        S._atomic_write(record_path, text)
        left_out = stale_human(ws) if superseded else None
        pins = _pin_states(ws, tl, context, unapplied, {m["pin"]: m for m in acknowledged}, acknowledged_changes)
    return dict(record, replaced=_rel(ws, moved) if moved is not None else None, previous=previous, recovered=recovered,
                human_left_out=left_out, pins=pins)
