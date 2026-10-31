"""A minimal MCP server (stdio, newline-delimited JSON-RPC 2.0) over the editing tools.  Prototype: no SDK, no dependencies.

    python mcp_server.py WORKSPACE

It implements ``initialize``, ``tools/list`` and ``tools/call``; notifications are accepted and ignored.  A tool's answer is its JSON
result as text; an ``EditError`` comes back as a tool result with ``isError: true``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the matchcut checkout

from editapi import EditError, EditSession  # noqa: E402
from tools import TOOLS  # noqa: E402

PROTOCOL = "2025-06-18"


def handle(session: EditSession, msg: dict):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # a notification
        return None
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}}, "serverInfo": {"name": "matchcut-edit", "version": "0.0"}}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "inputSchema": schema} for n, (d, schema, _) in TOOLS.items()]}
    elif method == "tools/call":
        name = msg.get("params", {}).get("name")
        args = msg.get("params", {}).get("arguments") or {}
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        try:
            if not isinstance(args, dict):
                raise EditError("arguments must be an object")
            out, is_error = TOOLS[name][2](session, args), False
        except EditError as exc:
            out, is_error = {"error": str(exc)}, True
        except Exception as exc:  # a bad request must never take the server down
            out, is_error = {"error": f"{type(exc).__name__}: {exc}"}, True
        result = {"content": [{"type": "text", "text": json.dumps(out)}], "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> int:
    session = EditSession(Path(sys.argv[1]))
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        reply = handle(session, msg) if isinstance(msg, dict) else {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
