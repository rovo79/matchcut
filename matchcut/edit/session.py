"""The edit session store, ``WS/edits/``: immutable revisions, deterministic proposals, apply, undo/redo/checkout, compare, staleness and
the lock.

Layout:

- ``session.json``: the session (``SESSION_SCHEMA``): its id (a UUID made at ``start``), the base stage, its file and sha256, the head
  revision, the next revision number and every clip id the session has ever issued (``reserved_ids``), so an id never names two clips;
- ``revisions/rNNN.json``: immutable once written; a whole timeline plus the parent revision and the operations, guards, details, changes
  and timing that made it.  ``r000`` is the base stage on whole frames (``normalise``);
- ``proposals/<id>.json``: an accepted proposal, its id the first 12 hex characters of the sha256 of the head revision id, the head
  timeline and the operations and guards (``proposal_id``), so proposing the same thing again gives the same id;
- ``renders/`` and ``published/`` (made by ``view`` and ``publish`` when they need them);
- ``.lock``: every command that writes holds an exclusive ``fcntl.flock`` on it; the read-only ones a shared one.

Everything is written atomically (a temporary file, then ``os.replace``) as deterministic JSON (sorted keys).  Timestamps live only in
``created``/``updated`` fields, which no hash covers, so the same commands on the same workspace give byte-identical revision timelines and
the same proposal and revision ids.

A session is stale when its base stage file has changed (or that stage is no longer listed, e.g. a ``human.json`` gone stale): every
command that changes it refuses, the read-only ones say ``stale: true``, and ``start`` moves it aside to ``WS/edits.stale-<sha12>/``.
Nothing in the store is ever deleted.

Expected user errors raise ``SessionError`` (a ``Refused``) with a one-line message; a proposal the rules refuse raises
``ProposalRefused`` with its problems, timing and changes.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from matchcut.pipeline.audio import MusicAnalysis
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.editorial import PinStore
from matchcut.pipeline.junctions import stage_timeline_paths
from matchcut.pipeline.shots import load_shots
from matchcut.pipeline.timeline import Decision, Timeline, Transition, clip_origin

from . import rules
from .model import EditContext, Refused, context_from, frame_rows
from .ops import apply_ops

SESSION_SCHEMA = "matchcut.edit_session/1"
REVISION_SCHEMA = "matchcut.edit_revision/1"
PROPOSAL_SCHEMA = "matchcut.edit_proposal/1"
EDIT_STAGE = "edit"  # the stage a revision's timeline carries (published as WS/edit.json)
AGENT = "agent"  # the pass name of the decisions a proposal appends to the history

_REVISION_ID = re.compile(r"r[0-9]{3,}")
_PROPOSAL_ID = re.compile(r"[0-9a-f]{12}")


class SessionError(Refused):
    """A command that cannot be served (no session, a stale session, an unknown or malformed id, a moved head ...); one line."""


class ProposalRefused(SessionError):
    """The operations applied, but the result breaks a rule (or changes nothing).  ``problems`` are the rules' lines; ``timing``,
    ``changes`` and ``details`` describe the refused result."""

    def __init__(self, message: str, problems: Sequence[str] = (), timing: Optional[Dict[str, Any]] = None,
                 changes: Sequence[str] = (), details: Sequence[str] = ()):
        super().__init__(message)
        self.problems = list(problems)
        self.timing = timing
        self.changes = list(changes)
        self.details = list(details)


# ----------------------------------------------------------------------------------------------- JSON, hashes, files
def canonical_json(obj: Any) -> str:
    """The canonical JSON every hash is taken over: sorted keys, no spaces, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _pretty(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def timeline_sha256(tl: Timeline) -> str:
    """The sha256 of a timeline's canonical JSON."""
    return _sha256_text(canonical_json(tl.to_dict()))


def proposal_id(head: str, head_timeline: Timeline, ops: Any, guards: Any) -> str:
    """The first 12 hex characters of the sha256 of the head revision id, the head timeline's canonical JSON and the canonical JSON of the
    operations and guards."""
    text = "\n".join([head, canonical_json(head_timeline.to_dict()), canonical_json({"ops": ops, "guards": guards})])
    return _sha256_text(text)[:12]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to a temporary file next to ``path``, flush it to disk, then ``os.replace`` it into place."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_json(path: Path, what: str) -> Dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SessionError(f"no {what}") from None
    except (OSError, ValueError) as exc:
        raise SessionError(f"{what} is unreadable ({path.name}: {exc})") from None
    if not isinstance(doc, dict):
        raise SessionError(f"{what} is unreadable ({path.name} does not hold an object)")
    return doc


# ----------------------------------------------------------------------------------------------- ids
def check_revision_id(rev: Any) -> str:
    """``rev`` if it is a revision id (``r000``, ``r001`` ... three digits or more), else ``SessionError``; checked before any path is
    built from it."""
    if not isinstance(rev, str) or not _REVISION_ID.fullmatch(rev):
        raise SessionError(f"not a revision id: {rev!r} (revision ids are r000, r001, ...)")
    return rev


def check_proposal_id(pid: Any) -> str:
    """``pid`` if it is a proposal id (12 lowercase hex characters), else ``SessionError``."""
    if not isinstance(pid, str) or not _PROPOSAL_ID.fullmatch(pid):
        raise SessionError(f"not a proposal id: {pid!r} (proposal ids are 12 lowercase hex characters)")
    return pid


def revision_name(n: int) -> str:
    return f"r{n:03d}"


def _revision_number(rev: str) -> int:
    return int(rev[1:])


# ----------------------------------------------------------------------------------------------- the workspace
def edits_dir(ws: Path) -> Path:
    return Path(ws) / "edits"


def workspace_context(ws: Path) -> EditContext:
    """The ``EditContext`` of a workspace: ``config.json``, the source footage's exact rate (as M2a exports at it: the analysed footage,
    ``probe.json``'s file), the shot bounds (``shots.json``), the editor's pins (``editorial.json``) and the music analysis
    (``music.json``, when the config names a track and the analysis is of that track)."""
    from matchcut.interchange import InterchangeError
    from matchcut.interchange.workspace import _source  # the footage M2a exports against; one copy of that choice

    ws = Path(ws)
    try:
        cfg = PipelineConfig.load(ws / "config.json")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SessionError(f"not a matchcut workspace: cannot read {ws / 'config.json'} ({exc})") from None
    try:
        bounds = {s.index: (s.start, s.end) for s in load_shots(ws / "shots.json")}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SessionError(f"not an analysed matchcut workspace: cannot read {ws / 'shots.json'} ({exc})") from None
    try:
        src, _notes = _source(ws, cfg)
    except InterchangeError as exc:
        raise SessionError(f"cannot read the source footage: {exc}") from None
    music: Optional[MusicAnalysis] = None
    if cfg.music.path and (ws / "music.json").exists():
        try:
            analysis = MusicAnalysis.load(ws / "music.json")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise SessionError(f"cannot read the music analysis {ws / 'music.json'} ({exc})") from None
        if analysis.path == str(cfg.music.path):  # an analysis of another track is not this cut's music (the pipeline re-analyses)
            music = analysis
    return context_from(cfg, src.rate, bounds, PinStore(ws).load(), music)


def _stage_paths(ws: Path) -> Dict[str, Path]:
    """Every stage that can be a session's base, present now, in stage order (``stage_timeline_paths``: a stale stage is left out)."""
    return {stage: path for stage, path in stage_timeline_paths(ws) if path.exists()}


def _base_stage(ws: Path, stage: Optional[str]) -> Tuple[str, Path]:
    paths = _stage_paths(ws)
    if not paths:
        raise SessionError(f"no stage timeline in {ws}: run the pipeline first")
    if stage is None:
        name = list(paths)[-1]
        return name, paths[name]
    if not isinstance(stage, str) or stage not in paths:
        raise SessionError(f"no stage {stage!r} in {ws} (stages: {', '.join(paths)})")
    return stage, paths[stage]


def stale_reason(ws: Path, state: Dict[str, Any]) -> Optional[str]:
    """Why the session ``state`` no longer fits the workspace (its base stage file changed or is gone, or that stage is no longer listed),
    or None when it is fresh."""
    ws = Path(ws)
    name = str(state.get("base_file", ""))
    path = ws / name
    hint = f"run start to begin a new session (this one is then kept in edits.stale-{str(state.get('base_sha256', ''))[:12]}/)"
    if not name or Path(name).is_absolute() or ".." in Path(name).parts:
        return f"the session's base file is not recorded: {hint}"
    now = _sha256_file(path)
    if now is None:
        return f"{name} (the base stage {state.get('base_stage')}) is gone: {hint}"
    if now != state.get("base_sha256"):
        return f"{name} (the base stage {state.get('base_stage')}) changed since this session started: {hint}"
    listed = dict(stage_timeline_paths(ws))
    if state.get("base_stage") not in listed:
        return f"{name} (the base stage {state.get('base_stage')}) is stale itself, so it is no longer a stage: {hint}"
    return None


# ----------------------------------------------------------------------------------------------- the lock
@contextlib.contextmanager
def _locked(ws: Path, shared: bool = False, create: bool = False) -> Iterator[Path]:
    """Hold ``WS/edits/.lock`` (exclusive, or shared for a read) for the block; yields ``WS/edits``.  Only ``start`` (``create``) makes
    ``WS/edits/``; the lock file is never moved, so every process locks the same file."""
    d = edits_dir(ws)
    if not create and not d.is_dir():
        raise SessionError(f"no edit session in {ws}: run start first")
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield d
    finally:
        os.close(fd)  # releases the lock


# ----------------------------------------------------------------------------------------------- r000
def normalise(tl: Timeline, ctx: EditContext) -> Tuple[Timeline, List[str]]:
    """A copy of ``tl`` on whole frames at ``ctx.rate`` (the session's ``r000``) and a note per point or transition that moved.

    - Every clip's identity is stamped first (``origin`` = ``clip_origin``), so pins and clip keys still name it once its points move.
    - Source points are rounded to the nearest frame; a rounding that would leave the clip's shot is pulled back in (as
      ``Timeline.snap_to_frames``), and a clip keeps at least one frame.
    - Transitions are laid out on whole frames as M2a and the renderer lay them out (``model.frame_rows``: rounded over the running
      total); one that rounds to no frame is a cut, and so is the first clip's.
    ``tl`` is not changed."""
    out = tl.copy()
    notes: List[str] = []
    rate = ctx.rate
    for c in out.clips:
        c.origin = clip_origin(c)
    for c in out.clips:
        in_f, out_f = ctx.f(c.source_in), ctx.f(c.source_out)
        if ctx.has_shot(c.shot_index):
            lo_s, hi_s = ctx.bounds[c.shot_index]
            lo, hi = ctx.shot_frames(c.shot_index)
            if c.source_in >= lo_s - 1e-9 and in_f < lo:
                in_f = lo
            if c.source_out <= hi_s + 1e-9 and out_f > hi:
                out_f = hi
        out_f = max(out_f, in_f + 1)
        parts = []
        for label, before, frames in (("in", c.source_in, in_f), ("out", c.source_out, out_f)):
            moved = frames - before * float(rate)
            if abs(moved) > 0.01:
                parts.append(f"{label} {before:.6g} s -> frame {frames} ({moved:+.2f}f)")
        if parts:
            notes.append(f"{c.id}: " + ", ".join(parts))
        c.source_in, c.source_out = ctx.s(in_f), ctx.s(out_f)
    rows = frame_rows(out, rate)
    for i, (c, r) in enumerate(zip(out.clips, rows)):
        old = c.transition_in
        new = Transition(r.kind, ctx.s(r.tr)) if i and r.tr else Transition()
        if not old.is_cut and (new.kind, new.duration) != (old.kind, old.duration):
            exact = old.duration * float(rate)
            if not i:
                notes.append(f"{c.id}: the first clip's incoming {old.kind} ({old.duration:.6g} s) is a cut")
            elif abs(exact - r.tr) > 0.01:
                notes.append(f"{c.id}: incoming {old.kind} {old.duration:.6g} s ({exact:.2f}f) -> "
                             + (f"{r.tr} frames" if r.tr else "no frame: a cut"))
        c.transition_in = new
    out.stage = EDIT_STAGE
    return out, notes


# ----------------------------------------------------------------------------------------------- the session
@dataclass
class Session:
    """An open session, read: its state (``session.json``), its revisions and (lazily) the workspace's ``EditContext``.  ``view`` and the
    CLI read the head timeline and the context through it; it never writes."""

    ws: Path
    state: Dict[str, Any]
    _ctx: Optional[EditContext] = field(default=None, repr=False)

    @property
    def dir(self) -> Path:
        return edits_dir(self.ws)

    @property
    def head(self) -> str:
        return str(self.state["head"])

    @property
    def session_id(self) -> str:
        return str(self.state["session"])

    @property
    def base_stage(self) -> str:
        return str(self.state["base_stage"])

    def stale(self) -> Optional[str]:
        """Why the session is stale, or None."""
        return stale_reason(self.ws, self.state)

    def context(self) -> EditContext:
        if self._ctx is None:
            self._ctx = workspace_context(self.ws)
        return self._ctx

    def revision_ids(self) -> List[str]:
        """Every revision id in the store, in creation order (by number)."""
        d = self.dir / "revisions"
        ids = [p.stem for p in d.glob("r*.json") if _REVISION_ID.fullmatch(p.stem)] if d.is_dir() else []
        return sorted(ids, key=_revision_number)

    def revision(self, rev: Optional[str] = None) -> Dict[str, Any]:
        """The revision document ``rev`` (default: the head)."""
        rev = check_revision_id(self.head if rev is None else rev)
        return _read_json(self.dir / "revisions" / f"{rev}.json", f"revision {rev}")

    def timeline(self, rev: Optional[str] = None) -> Timeline:
        """The timeline of revision ``rev`` (default: the head)."""
        doc = self.revision(rev)
        try:
            return Timeline.from_dict(doc["timeline"])
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise SessionError(f"revision {doc.get('id', rev)} holds no readable timeline ({exc!r})") from None

    def children(self, rev: str) -> List[str]:
        """The revisions whose parent is ``rev``, oldest first."""
        out = []
        for r in self.revision_ids():
            try:
                if self.revision(r).get("parent") == rev:
                    out.append(r)
            except SessionError:
                continue
        return out


def _read_state(ws: Path) -> Dict[str, Any]:
    d = edits_dir(ws)
    if not (d / "session.json").exists():
        raise SessionError(f"no edit session in {ws}: run start first")
    st = _read_json(d / "session.json", "edit session (edits/session.json)")
    try:
        if st.get("schema") != SESSION_SCHEMA:
            raise ValueError(f"schema {st.get('schema')!r}, not {SESSION_SCHEMA}")
        check_revision_id(st["head"])
        if not isinstance(st["next"], int) or isinstance(st["next"], bool) or st["next"] < 1:
            raise ValueError(f"next {st['next']!r}")
        if not isinstance(st["reserved_ids"], list):
            raise ValueError("reserved_ids is not a list")
        str(st["session"]), str(st["base_stage"]), str(st["base_sha256"]), str(st["base_file"])
    except (KeyError, ValueError, Refused) as exc:
        raise SessionError(f"edits/session.json is unreadable ({exc}): run start with restart to move it aside") from None
    return st


def _save_state(d: Path, st: Dict[str, Any]) -> None:
    st = dict(st, updated=_now())
    _atomic_write(d / "session.json", _pretty(st))


def load(ws: Path, ctx: Optional[EditContext] = None) -> Session:
    """The session of workspace ``ws`` (``SessionError`` when there is none).  ``ctx`` overrides the workspace's context (tests)."""
    ws = Path(ws)
    return Session(ws, _read_state(ws), ctx)


def _fresh(ws: Path, ctx: Optional[EditContext]) -> Session:
    """The session, refused when it is stale (every command that changes it calls this under the lock)."""
    s = load(ws, ctx)
    why = s.stale()
    if why:
        raise SessionError(f"the edit session is stale: {why}")
    return s


def _move_aside(ws: Path, d: Path, tag: str) -> Path:
    """Move everything in ``WS/edits/`` but the lock into a new ``WS/edits.stale-<tag>[-n]/`` (never over an existing one)."""
    base = Path(ws) / f"edits.stale-{tag}"
    target, n = base, 2
    while target.exists():
        target, n = base.with_name(f"{base.name}-{n}"), n + 1
    target.mkdir()
    for entry in sorted(d.iterdir()):
        if entry.name != ".lock":
            os.rename(entry, target / entry.name)
    return target


def _write_revision(d: Path, doc: Dict[str, Any]) -> None:
    path = d / "revisions" / f"{check_revision_id(doc['id'])}.json"
    if path.exists():
        raise SessionError(f"revision {doc['id']} exists already: revisions are never rewritten")
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, _pretty(doc))


# ----------------------------------------------------------------------------------------------- commands
def start(ws: Path, stage: Optional[str] = None, restart: bool = False, ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """Start a session on ``stage`` (default: the latest fresh stage) and return its summary.

    ``r000`` is the stage on whole frames (``normalise``); no workspace file is rewritten.  An existing session is moved aside to
    ``WS/edits.stale-<sha12>/`` when it is stale; a fresh one is refused unless ``restart``, which moves it aside the same way."""
    ws = Path(ws)
    if not ws.is_dir():
        raise SessionError(f"no workspace {ws}")
    name, path = _base_stage(ws, stage)
    ctx = ctx if ctx is not None else workspace_context(ws)
    with _locked(ws, create=True) as d:
        moved: Optional[Path] = None
        if (d / "session.json").exists():
            try:
                old: Optional[Dict[str, Any]] = _read_state(ws)
            except SessionError:
                old = None
            if old is None:
                if not restart:
                    raise SessionError("edits/session.json is unreadable: pass restart to move the session aside and start again")
                moved = _move_aside(ws, d, "unreadable")
            else:
                why = stale_reason(ws, old)
                if why is None and not restart:
                    raise SessionError(f"a session is open on {old['base_stage']} (head {old['head']}): pass restart to move it aside "
                                       "and start again")
                moved = _move_aside(ws, d, str(old["base_sha256"])[:12])
        elif any(e.name != ".lock" for e in d.iterdir()):
            moved = _move_aside(ws, d, "incomplete")  # what a start that never finished left behind
        # the stage is read under the lock, and its hash is of the bytes read
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        try:
            base = Timeline.from_dict(json.loads(data.decode("utf-8")))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SessionError(f"cannot read {path.name} ({exc!r})") from None
        r000, notes = normalise(base, ctx)
        (d / "revisions").mkdir(exist_ok=True)
        (d / "proposals").mkdir(exist_ok=True)
        created = _now()
        _write_revision(d, {"schema": REVISION_SCHEMA, "id": "r000", "parent": None, "proposal": None, "ops": [], "guards": None,
                            "details": notes, "touched": [], "changes": [], "timing": None,
                            "base": {"stage": name, "file": path.relative_to(ws).as_posix(), "sha256": sha},
                            "timeline_sha256": timeline_sha256(r000), "timeline": r000.to_dict(), "created": created})
        state = {"schema": SESSION_SCHEMA, "session": str(uuid.uuid4()), "base_stage": name, "base_file": path.relative_to(ws).as_posix(),
                 "base_sha256": sha, "rate": str(ctx.rate), "head": "r000", "next": 1, "reserved_ids": sorted({c.id for c in r000.clips}),
                 "created": created}
        _save_state(d, state)
    return {"session": state["session"], "base_stage": name, "base_sha256": sha, "head": "r000", "normalised": notes,
            "moved_aside": moved.name if moved is not None else None}


def _change_decision(line: str, before: Timeline, after: Timeline) -> Decision:
    """One ``agent`` decision for one line of ``rules.changes``, naming the clips it concerns."""
    m = re.match(r"(removed|added) (\S+) ", line)
    if m:
        return Decision(AGENT, m.group(1), [m.group(2)], line)
    m = re.match(r"(\S+)\|(\S+): ", line)
    if m:
        return Decision(AGENT, "transition", [m.group(1), m.group(2)], line)
    if line.startswith("order: "):
        order = line[len("order: "):].split()
        kept = set(order)
        old = [c.id for c in before.clips if c.id in kept]
        prev_b = {cid: (old[i - 1] if i else None) for i, cid in enumerate(old)}
        moved = [cid for i, cid in enumerate(order) if prev_b.get(cid) != (order[i - 1] if i else None)]
        return Decision(AGENT, "moved", moved, line, data={"order": order})
    m = re.match(r"(\S+): (slipped|incoming)?", line)
    if m:
        action = {"slipped": "slipped", "incoming": "transition"}.get(m.group(2) or "", "trimmed")
        return Decision(AGENT, action, [m.group(1)], line)
    return Decision(AGENT, "changed", [], line)


def _plain(value: Any, what: str) -> Any:
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise SessionError(f"{what} must be plain JSON ({exc})") from None


def _evaluate(s: Session, ops: Any, guards: Any) -> Dict[str, Any]:
    """Run ``ops`` on the head and check every rule; returns the proposal document (nothing is written).  Raises ``Refused`` (an operation
    cannot be applied), ``ProposalRefused`` (the result breaks a rule or changes nothing) or ``SessionError`` (malformed input)."""
    if isinstance(ops, (str, bytes, dict)) or not isinstance(ops, (list, tuple)):
        raise SessionError("ops must be a list of operations")
    if not ops:
        raise SessionError("ops is empty: a proposal needs at least one operation")
    if guards is not None and not isinstance(guards, dict):
        raise SessionError(f"guards must be an object (any of {', '.join(rules.GUARDS)}), not {guards!r}")
    ops = _plain(list(ops), "ops")
    guards = _plain(guards, "guards") if guards else None
    head = s.head
    before = s.timeline(head)
    ctx = s.context()
    pid = proposal_id(head, before, ops, guards)
    res = apply_ops(before, ops, ctx, s.state["reserved_ids"])
    after = res.timeline
    probs = rules.problems(before, after, res.touched, ctx, guards)
    tm = rules.timing(before, after, ctx)
    ch = rules.changes(before, after, ctx)
    if probs:
        raise ProposalRefused("refused: " + "; ".join(probs), probs, tm, ch, res.details)
    if [c.to_dict() for c in after.clips] == [c.to_dict() for c in before.clips]:
        raise ProposalRefused("refused: the operations change nothing", [], tm, ch, res.details)
    for line in ch:
        d = _change_decision(line, before, after)
        d.data = dict(d.data, proposal=pid)
        after.history.append(d)
    after.stage = EDIT_STAGE
    return {"schema": PROPOSAL_SCHEMA, "id": pid, "head": head, "head_sha256": timeline_sha256(before), "ops": ops, "guards": guards,
            "details": list(res.details), "touched": sorted(res.touched), "changes": ch, "timing": tm, "problems": [],
            "timeline_sha256": timeline_sha256(after), "timeline": after.to_dict()}


def _summary(doc: Dict[str, Any]) -> Dict[str, Any]:
    return {"proposal": doc["id"], "head": doc["head"], "changes": doc["changes"], "timing": doc["timing"], "problems": doc["problems"],
            "details": doc["details"], "touched": doc["touched"]}


def propose(ws: Path, ops: Any, guards: Optional[Dict[str, Any]] = None, ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """Run ``ops`` (all or nothing) on the head and check the proposal-wide rules and ``guards``.  Accepted: the proposal is stored under
    its deterministic id and its id, changes by clip id, timing and (empty) problems are returned.  Refused: ``Refused`` /
    ``ProposalRefused`` is raised and nothing is written.  Proposing the same thing again gives the same id and the same file."""
    with _locked(ws) as d:
        s = _fresh(ws, ctx)
        doc = _evaluate(s, ops, guards)
        path = d / "proposals" / f"{check_proposal_id(doc['id'])}.json"
        text = _pretty(doc)
        try:
            same = path.read_text(encoding="utf-8") == text
        except OSError:
            same = False
        if not same:
            path.parent.mkdir(exist_ok=True)
            _atomic_write(path, text)
    return _summary(doc)


def apply(ws: Path, proposal: str, ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """Apply the stored proposal ``proposal``: it must have been made at the current head; its operations are run again on the head and
    the result must be exactly the stored one (a changed proposal file is refused).  Writes the next immutable revision and moves the
    head to it."""
    pid = check_proposal_id(proposal)
    with _locked(ws) as d:
        s = _fresh(ws, ctx)
        stored = _read_json(d / "proposals" / f"{pid}.json", f"proposal {pid}")
        if stored.get("head") != s.head:
            raise SessionError(f"head moved: proposal {pid} was made at {stored.get('head')}, the head is now {s.head}: propose again")
        try:
            doc = _evaluate(s, stored.get("ops"), stored.get("guards"))
        except Refused as exc:
            raise SessionError(f"proposal {pid} no longer applies: {exc}") from None
        if doc["id"] != pid or json.loads(canonical_json(doc)) != stored:
            raise SessionError(f"proposal {pid} does not match what its operations give when run again (the file was changed, or the "
                               "workspace's pins, config or music changed since): propose again")
        ids = s.revision_ids()
        n = max([s.state["next"]] + [_revision_number(r) + 1 for r in ids])  # never over a revision a crash left behind
        rid = revision_name(n)
        tl = Timeline.from_dict(doc["timeline"])
        _write_revision(d, {"schema": REVISION_SCHEMA, "id": rid, "parent": s.head, "proposal": pid, "ops": doc["ops"],
                            "guards": doc["guards"], "details": doc["details"], "touched": doc["touched"], "changes": doc["changes"],
                            "timing": doc["timing"], "timeline_sha256": doc["timeline_sha256"], "timeline": doc["timeline"],
                            "created": _now()})
        st = dict(s.state, head=rid, next=n + 1, reserved_ids=sorted(set(s.state["reserved_ids"]) | {c.id for c in tl.clips}))
        _save_state(d, st)
    return {"revision": rid, "parent": s.head, "head": rid, "proposal": pid, "changes": doc["changes"], "timing": doc["timing"]}


def _move_head(ws: Path, ctx: Optional[EditContext], pick) -> Dict[str, Any]:
    with _locked(ws) as d:
        s = _fresh(ws, ctx)
        old = s.head
        new, extra = pick(s)
        _save_state(d, dict(s.state, head=new))
    return dict({"head": new, "previous": old}, **extra)


def undo(ws: Path, ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """Move the head to its parent (nothing is deleted)."""
    def pick(s: Session) -> Tuple[str, Dict[str, Any]]:
        parent = s.revision().get("parent")
        if parent is None:
            raise SessionError(f"nothing to undo: the head is {s.head}, the session's first revision")
        return check_revision_id(parent), {}
    return _move_head(ws, ctx, pick)


def redo(ws: Path, ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """Move the head to its latest child (the most recently made revision whose parent is the head)."""
    def pick(s: Session) -> Tuple[str, Dict[str, Any]]:
        kids = s.children(s.head)
        if not kids:
            raise SessionError(f"nothing to redo: no revision was made from {s.head}")
        return kids[-1], {"branches": kids}
    return _move_head(ws, ctx, pick)


def checkout(ws: Path, rev: str, ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """Move the head to revision ``rev`` (any revision of the session, e.g. on another branch)."""
    rev = check_revision_id(rev)

    def pick(s: Session) -> Tuple[str, Dict[str, Any]]:
        if not (s.dir / "revisions" / f"{rev}.json").exists():
            raise SessionError(f"no revision {rev} (revisions: {', '.join(s.revision_ids())})")
        s.revision(rev)  # readable
        return rev, {}
    return _move_head(ws, ctx, pick)


def compare(ws: Path, a: Optional[str], b: Optional[str] = None, ctx: Optional[EditContext] = None) -> Dict[str, Any]:
    """The changes (by clip id) and the timing from revision ``a`` to revision ``b`` (either None: the head).  Works on a stale session
    and says so."""
    a = None if a is None else check_revision_id(a)
    b = None if b is None else check_revision_id(b)
    with _locked(ws, shared=True):
        s = load(ws, ctx)
        a, b = a or s.head, b or s.head
        ta, tb = s.timeline(a), s.timeline(b)
        why = s.stale()
    c = s.context()
    return {"a": a, "b": b, "changes": rules.changes(ta, tb, c), "timing": rules.timing(ta, tb, c), "stale": why is not None,
            "stale_reason": why}


def log(ws: Path) -> Dict[str, Any]:
    """The session: its revisions (id, parent, operations, details, created), the head and whether it is stale."""
    with _locked(ws, shared=True):
        s = load(ws)
        revs = []
        for rid in s.revision_ids():
            doc = s.revision(rid)
            ops = doc.get("ops") or []
            revs.append({"id": rid, "parent": doc.get("parent"), "proposal": doc.get("proposal"),
                         "ops": [op.get("op") if isinstance(op, dict) else None for op in ops],
                         "summary": "; ".join(doc.get("details") or []) if doc.get("parent") is not None else "base",
                         "changes": len(doc.get("changes") or []), "created": doc.get("created")})
        why = s.stale()
    return {"session": s.session_id, "base_stage": s.base_stage, "base_sha256": s.state["base_sha256"], "head": s.head,
            "stale": why is not None, "stale_reason": why, "revisions": revs}
