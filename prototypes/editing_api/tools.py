"""One table of editing tools, shared by the JSON CLI (``cli.py``) and the MCP server (``mcp_server.py``).

Each tool is a name, a one-line description, a JSON Schema for its arguments and a call on an ``EditSession``.  The transports add
nothing: an agent gets the same answers from either.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Tuple

from editapi import OPS, EditSession

_REV = {"type": "string", "description": "revision id, e.g. r003 (default: the head)"}
_JUNCTION = {"type": ["integer", "string"], "description": "1-based junction number or junction id such as s1-s3"}
_OP = {
    "type": "object",
    "description": "one operation; frames are whole frames at the source rate",
    "properties": {
        "op": {"enum": list(OPS)},
        "clip": {"type": "string", "description": "clip id (c003, e001) or clip key"},
        "junction": _JUNCTION,
        "edge": {"enum": ["in", "out"]},
        "frames": {"type": "integer"},
        "shot": {"type": "integer"},
        "in_frame": {"type": "integer"},
        "out_frame": {"type": "integer"},
        "before": {"type": "string"},
        "after": {"type": "string"},
        "kind": {"enum": ["cut", "dissolve", "dip_to_black"]},
        "mode": {"enum": ["roll", "ripple"]},
        "window_s": {"type": "number"},
    },
    "required": ["op"],
}

Tool = Tuple[str, Dict[str, Any], Callable[[EditSession, Dict[str, Any]], Any]]

TOOLS: Dict[str, Tool] = {
    "edit_start": ("Start (or resume) an edit session on a canonical stage of the workspace.",
                   {"type": "object", "properties": {"stage": {"type": "string", "default": "final"}}},
                   lambda s, a: s.start(a.get("stage", "final"))),
    "edit_inspect": ("The cut at a revision: clips, junctions, available shots, story beats, pins, music.",
                     {"type": "object", "properties": {"revision": _REV}},
                     lambda s, a: s.inspect(a.get("revision"))),
    "edit_propose": ("Check a list of operations against the head without changing anything; returns a proposal id and the exact changes.",
                     {"type": "object", "properties": {"ops": {"type": "array", "items": _OP}}, "required": ["ops"]},
                     lambda s, a: s.propose(a["ops"])),
    "edit_apply": ("Commit a proposal as a new revision (only if the head has not moved since it was proposed).",
                   {"type": "object", "properties": {"proposal": {"type": "string"}}, "required": ["proposal"]},
                   lambda s, a: s.apply(a["proposal"])),
    "edit_undo": ("Move the head back to its parent revision.", {"type": "object", "properties": {}}, lambda s, a: s.undo()),
    "edit_redo": ("Move the head forward to its latest child revision.", {"type": "object", "properties": {}}, lambda s, a: s.redo()),
    "edit_checkout": ("Move the head to any revision of the session.",
                      {"type": "object", "properties": {"revision": _REV}, "required": ["revision"]},
                      lambda s, a: s.checkout(a["revision"])),
    "edit_log": ("Every revision, its parent and the operations that made it.", {"type": "object", "properties": {}}, lambda s, a: s.log()),
    "edit_compare": ("The changes between two revisions.",
                     {"type": "object", "properties": {"a": _REV, "b": _REV}, "required": ["a", "b"]},
                     lambda s, a: s.compare(a["a"], a["b"])),
    "edit_render": ("Render a revision with matchcut's ffmpeg renderer: the whole cut (proxy), or one junction's review window.",
                    {"type": "object", "properties": {"revision": _REV, "junction": _JUNCTION}},
                    lambda s, a: s.render(a.get("revision"), a.get("junction"))),
    "edit_publish": ("Write the head as the workspace's human stage (as matchcut import does), for the TUI, junctions and export.",
                     {"type": "object", "properties": {}}, lambda s, a: s.publish()),
}
