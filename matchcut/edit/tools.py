"""The edit tools: one table (``TOOLS``) of every command an agent can run on a session, each with its name, a one-line description, a
JSON Schema for its arguments and a handler.  The JSON CLI (``python -m matchcut.edit``) is its first transport; a later MCP transport
serves the same table unchanged (``name``, ``description`` and ``schema`` are what MCP calls a tool's name, description and input schema).

``call(ws, name, args)`` checks the arguments against the tool's schema with the small validator below (``validate``: the subset of JSON
Schema the table uses, so no dependency is needed) and runs the handler.  A schema failure raises ``ArgumentError`` (the CLI's usage
error, exit code 2); a refusal raises ``Refused`` (a ``session.SessionError`` / ``ProposalRefused`` or an operation's ``Refused``; exit code
1).  Handlers return plain JSON objects.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import publish as P
from . import session as S
from . import view as V
from .ops import ARGS, MAX_FRAMES, MAX_SHOT, OPS
from .rules import GUARDS

REVISION_ID = r"^r\d{3,}$"
PROPOSAL_ID = r"^[0-9a-f]{12}$"
PIN_LABEL = r"^s\d{1,7}->s\d{1,7}$"
JUNCTION_ID = r"^([1-9]\d{0,8}|s-?\d{1,7}-s-?\d{1,7}(#\d{1,7})?)$"


class ArgumentError(ValueError):
    """The arguments do not match the tool's schema (``errors``: one line each, naming the argument)."""

    def __init__(self, tool: str, errors: Sequence[str]):
        super().__init__(f"{tool}: invalid arguments: " + "; ".join(errors))
        self.tool = tool
        self.errors = list(errors)


# ----------------------------------------------------------------------------------------------- the validator
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def _shown(v: Any) -> str:
    """A value for an error message, as JSON writes it, cut short."""
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) >= 10 ** 15:
        text = f"~{'-' if v < 0 else ''}1e{int(math.log10(abs(v)))}"
    else:
        try:
            text = json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            text = repr(v)
    return text if len(text) <= 40 else text[:36] + "..."


def _is(v: Any, t: str) -> bool:
    if t == "integer":  # JSON Schema: a number with no fraction; never a bool or a string
        return not isinstance(v, bool) and (isinstance(v, int) or (isinstance(v, float) and math.isfinite(v) and v.is_integer()))
    if t == "number":
        return not isinstance(v, bool) and isinstance(v, (int, float)) and (not isinstance(v, float) or math.isfinite(v))
    if t == "null":
        return v is None
    return isinstance(v, _TYPES[t]) and not (t != "boolean" and isinstance(v, bool))


def _search(pattern: str, value: str) -> bool:
    """A JSON Schema ``pattern`` as ECMA-262 reads it: ``\\d`` is an ASCII digit and ``$`` is the end of the string (Python's ``$`` also
    matches before a final newline)."""
    if pattern.endswith("$") and not pattern.endswith("\\$"):
        pattern = pattern[:-1] + r"\Z"
    return re.search(pattern, value, re.ASCII) is not None


def validate(value: Any, schema: Dict[str, Any], path: str = "arguments") -> List[str]:
    """The ways ``value`` breaks ``schema``, one line each ([] when it is valid).  Supports what the tool table uses: ``type`` (one or a
    list), ``enum``, ``const``, ``pattern``, ``minLength``/``maxLength``, ``minimum``/``maximum``, ``properties``, ``required``,
    ``additionalProperties`` (false or a schema), ``items``, ``minItems``/``maxItems`` and ``anyOf``."""
    errors: List[str] = []
    if "anyOf" in schema:
        if all(validate(value, sub, path) for sub in schema["anyOf"]):
            errors.append(f"{path}: {schema.get('description') or 'matches none of the allowed forms'}, not {_shown(value)}")
        return errors
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else list(types)
        if not any(_is(value, t) for t in types):
            return [f"{path} must be {' or '.join(_article(t) for t in types)}, not {_shown(value)}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path} must be {schema['const']!r}")
    if "enum" in schema and not any(value == e and type(value) is type(e) for e in schema["enum"]):
        errors.append(f"{path} must be one of {', '.join(map(str, schema['enum']))}, not {_shown(value)}")
    if isinstance(value, str):
        if "pattern" in schema and not _search(schema["pattern"], value):
            errors.append(f"{path} {_shown(value)} is not {schema.get('description') or 'of the form ' + schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{path} must not be empty" if schema["minLength"] == 1 else f"{path} needs at least {schema['minLength']} characters")
        if len(value) > schema.get("maxLength", math.inf):
            errors.append(f"{path} has {len(value)} characters (at most {schema['maxLength']})")
    if _is(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} must be at least {schema['minimum']}, not {_shown(value)}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} must be at most {schema['maximum']}, not {_shown(value)}")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}: {key} is required")
        extra = schema.get("additionalProperties", True)
        for key, v in value.items():
            if key in props:
                errors += validate(v, props[key], f"{path}.{key}" if path != "arguments" else str(key))
            elif extra is False:
                allowed = ", ".join(props) or "none"
                errors.append(f"{path}: unknown argument {_shown(key)} (arguments: {allowed})")
            elif isinstance(extra, dict):
                errors += validate(v, extra, f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{path} needs at least {schema['minItems']} item{'s' if schema['minItems'] != 1 else ''}")
        if len(value) > schema.get("maxItems", math.inf):
            errors.append(f"{path} has more than {schema['maxItems']} items")
        if "items" in schema:
            for k, v in enumerate(value):
                errors += validate(v, schema["items"], f"{path}[{k}]")
    return errors


def _article(t: str) -> str:
    return {"integer": "a whole number", "number": "a number", "string": "a string", "boolean": "true or false", "object": "an object",
            "array": "a list", "null": "null"}.get(t, t)


# ----------------------------------------------------------------------------------------------- schemas
def _int(description: str, minimum: int = -MAX_FRAMES, maximum: int = MAX_FRAMES) -> Dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum, "description": description}


REVISION = {"type": "string", "pattern": REVISION_ID, "description": "a revision id (r000, r001, ...)"}
PROPOSAL = {"type": "string", "pattern": PROPOSAL_ID, "description": "a proposal id (12 lowercase hex characters)"}
JUNCTION = {"anyOf": [_int("a junction's 1-based number", 1, MAX_FRAMES),
                      {"type": "string", "pattern": JUNCTION_ID, "maxLength": 40}],
            "description": "a junction: its 1-based number, or its id (s1-s3, or s1-s3#2 for a repeated pair)"}
CLIP = {"type": "string", "minLength": 1, "maxLength": 200, "description": "a clip id (c001, e001) or its key"}
QUALITY = {"type": "string", "enum": list(V.QUALITIES), "description": "proxy (default) or full"}
KIND = {"type": "string", "enum": ["cut", "dissolve", "dip_to_black"]}

OPERATION = {
    "type": "object",
    "description": "one operation: op plus its arguments (frames are whole frames at the source's exact rate)",
    "required": ["op"],
    "additionalProperties": False,
    "properties": {
        "op": {"type": "string", "enum": list(OPS)},
        "clip": CLIP,
        "edge": {"type": "string", "enum": ["in", "out"]},
        "frames": _int("a whole number of frames (trim, roll, slip: signed; replace: the new length; set_transition: the length)"),
        "junction": JUNCTION,
        "shot": _int("a shot index", 0, MAX_SHOT),
        "in_frame": _int("a source frame", 0, MAX_FRAMES),
        "out_frame": _int("a source frame", 0, MAX_FRAMES),
        "before": CLIP,
        "after": CLIP,
        "kind": KIND,
        "mode": {"type": "string", "enum": ["roll", "ripple"]},
        "window_s": {"type": "number", "minimum": 0, "maximum": 3600, "description": "seconds a snap may move the cut"},
    },
}
GUARDS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "description": "timing guards: the proposal is refused if it would break one",
    "properties": {
        "max_duration_change_frames": _int("the most the running time may change, in frames either way", 0, MAX_FRAMES),
        "keep_record_frames": {"type": "array", "items": JUNCTION, "maxItems": 10000},
        "keep_on_beat": {"type": "array", "items": JUNCTION, "maxItems": 10000},
    },
}
assert set(GUARDS_SCHEMA["properties"]) == set(GUARDS)  # noqa: S101 - the table and the rules name the same guards
assert set(OPERATION["properties"]) - {"op"} == {a for args in ARGS.values() for a in args}  # noqa: S101


def _obj(properties: Optional[Dict[str, Any]] = None, required: Sequence[str] = ()) -> Dict[str, Any]:
    return {"type": "object", "properties": dict(properties or {}), "required": list(required), "additionalProperties": False}


# ----------------------------------------------------------------------------------------------- the table
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    schema: Dict[str, Any]
    handler: Callable[[Path, Dict[str, Any]], Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        """The tool as a transport lists it (MCP: name, description, inputSchema)."""
        return {"name": self.name, "description": self.description, "schema": self.schema}


def _start(ws: Path, a: Dict[str, Any]) -> Dict[str, Any]:
    return S.start(ws, stage=a.get("stage"), restart=bool(a.get("restart", False)))


def _propose(ws: Path, a: Dict[str, Any]) -> Dict[str, Any]:
    return S.propose(ws, a["ops"], a.get("guards"))


def _pin(ws: Path, a: Dict[str, Any]) -> Dict[str, Any]:
    return V.pin_transition(ws, pin=a.get("pin"), junction=a.get("junction"), hold=a["hold"], kind=a.get("kind"), frames=a.get("frames"))


TOOLS: List[Tool] = [
    Tool("start", "Start an edit session on a stage (default: the latest canonical stage; human or an earlier one by name; never edit); "
                  "r000 is that stage on whole frames. Only a session on the latest canonical stage can publish. A stale session is moved "
                  "aside; a fresh one only with restart. transition_mismatches: protected pins whose transition differs from the held one, "
                  "with the fix.",
         _obj({"stage": {"type": "string", "pattern": r"^[a-z][a-z0-9_]{0,40}$", "description": "a stage name (roughcut, final, human ...)"},
               "restart": {"type": "boolean"}}),
         _start),
    Tool("inspect", "The cut of a revision (default: the head) in frames: clips, junctions and their protections, shots (their use and "
                    "descriptions), story beats (actual and target length), music beats, editor pins (held or not, protected "
                    "transitions) and whether the session is stale.",
         _obj({"revision": REVISION}),
         lambda ws, a: V.inspect(ws, a.get("revision"))),
    Tool("log", "The session's revisions (id, parent, operations, summary), the head and whether the session is stale.",
         _obj(),
         lambda ws, a: S.log(ws)),
    Tool("propose", "Run operations (all or nothing) on the head and check every rule and guard. Accepted: the proposal id, its changes by "
                    "clip id and its timing; nothing changes until apply. Refused: the problems, timing and changes.",
         _obj({"ops": {"type": "array", "items": OPERATION, "minItems": 1, "maxItems": 1000}, "guards": GUARDS_SCHEMA}, ["ops"]),
         _propose),
    Tool("apply", "Apply an accepted proposal (re-validated against the head) as the next revision, and move the head to it.",
         _obj({"proposal": PROPOSAL}, ["proposal"]),
         lambda ws, a: S.apply(ws, a["proposal"])),
    Tool("undo", "Move the head to its parent revision (nothing is deleted).",
         _obj(),
         lambda ws, a: S.undo(ws)),
    Tool("redo", "Move the head to its latest child revision.",
         _obj(),
         lambda ws, a: S.redo(ws)),
    Tool("checkout", "Move the head to any revision of the session.",
         _obj({"revision": REVISION}, ["revision"]),
         lambda ws, a: S.checkout(ws, a["revision"])),
    Tool("compare", "The changes (by clip id) and timing from revision a to revision b (either omitted: the head).",
         _obj({"a": REVISION, "b": REVISION}),
         lambda ws, a: S.compare(ws, a.get("a"), a.get("b"))),
    Tool("preview", "Render an accepted proposal before it is applied (re-validated as apply would): the whole cut, or one junction's "
                    "review window. Returns the file's path.",
         _obj({"proposal": PROPOSAL, "junction": JUNCTION, "quality": QUALITY}, ["proposal"]),
         lambda ws, a: V.preview(ws, a["proposal"], a.get("junction"), a.get("quality", "proxy"))),
    Tool("render", "Render a revision (default: the head): the whole cut, or one junction's review window (proxy quality by default). "
                   "Files are named by a hash of what they render and reused.",
         _obj({"revision": REVISION, "junction": JUNCTION, "quality": QUALITY}),
         lambda ws, a: V.render(ws, a.get("revision"), a.get("junction"), a.get("quality", "proxy"))),
    Tool("frames", "A junction's outgoing last frame and incoming first frame as JPEG files (for looking at the cut), with their source "
                   "frame numbers.",
         _obj({"junction": JUNCTION, "revision": REVISION}, ["junction"]),
         lambda ws, a: V.frames(ws, a["junction"], a.get("revision"))),
    Tool("pin_transition", "Protect (hold: true) or release (hold: false) the transition at an editor pin's junction. The transition held is "
                           "the head's at that junction unless kind and frames are given. Changes editorial.json, so earlier proposals must "
                           "be proposed again.",
         _obj({"pin": {"type": "string", "pattern": PIN_LABEL, "description": "a pin's label (s1->s3)"}, "junction": JUNCTION,
               "hold": {"type": "boolean"}, "kind": KIND, "frames": _int("the transition's length in whole frames", 0, MAX_FRAMES)},
              ["hold"]),
         _pin),
    Tool("publish", "Publish a revision (default: the head; not r000) as the edit stage (edit.json, edit_publish.json, a copy in "
                    "edits/published/). Needs a fresh session on the latest canonical stage whose revision holds every pin. Refused over "
                    "another session's edit stage unless replace, and over a fresh human stage it would leave out unless supersede_human.",
         _obj({"revision": REVISION, "replace": {"type": "boolean"}, "supersede_human": {"type": "boolean"}}),
         lambda ws, a: P.publish(ws, a.get("revision"), bool(a.get("replace", False)), bool(a.get("supersede_human", False)))),
]

BY_NAME: Dict[str, Tool] = {t.name: t for t in TOOLS}


def get(name: str) -> Tool:
    """The tool ``name``; KeyError when there is none."""
    return BY_NAME[name]


def call(ws: Path, name: str, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run tool ``name`` on workspace ``ws`` with ``args`` (checked against its schema first: ``ArgumentError``)."""
    tool = BY_NAME[name]
    args = {} if args is None else args
    errors = validate(args, tool.schema)
    if errors:
        raise ArgumentError(name, errors)
    return tool.handler(Path(ws), args)
