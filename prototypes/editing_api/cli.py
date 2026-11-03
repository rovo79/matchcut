"""The editing tools from a shell, JSON in and out:

    python cli.py WORKSPACE TOOL ['{"json": "arguments"}']

e.g. ``python cli.py ws edit_propose '{"ops": [{"op": "roll", "junction": 2, "frames": -3}]}'``.  Exit 0 with the result on stdout,
or 1 with ``{"error": ...}``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the matchcut checkout

from editapi import EditError, EditSession  # noqa: E402
from tools import TOOLS  # noqa: E402


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or argv[1] not in TOOLS:
        print("usage: cli.py WORKSPACE TOOL [JSON]; tools: " + ", ".join(TOOLS), file=sys.stderr)
        return 2
    try:
        out = TOOLS[argv[1]][2](EditSession(Path(argv[0])), json.loads(argv[2]) if len(argv) > 2 else {})
    except EditError as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    except Exception as exc:  # malformed arguments: one line, never a traceback
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 1
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
