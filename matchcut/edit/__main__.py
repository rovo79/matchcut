"""The edit JSON CLI: ``python -m matchcut.edit WS COMMAND ['{"json": "arguments"}' | @args.json | @-]``.

Every run prints exactly one JSON object on stdout:

- the command's result, exit code 0;
- ``{"error": "...", "kind": "refused" | "error" | "internal", ...}``, exit code 1: the command ran and refused (a stale session, an
  unknown revision, a proposal the rules refuse: then with its ``problems``, ``timing``, ``changes`` and ``details``; an operation that
  cannot be applied: with its ``index``), or failed (a render), or hit an unexpected error (``"internal error: ..."``);
- ``{"error": "...", "kind": "usage", ...}``, exit code 2: the command never ran.  That is: no or an unknown command, an extra argument,
  arguments that are not a JSON object (malformed JSON, an ``@file`` that cannot be read), a workspace that is not a directory, and
  arguments that do not match the command's JSON Schema (then with ``errors``, one line per argument: a wrong type, a fractional frame
  count, a malformed revision or proposal id, an empty ``ops`` list ...).  A schema failure is a usage error because the arguments do not
  fit the command's declared interface, exactly like an unknown command; a well-formed request the session refuses is exit code 1.

``--help`` (alone, or after ``WS COMMAND``) prints the commands, their descriptions and argument schemas.  No traceback is ever printed for
bad input; an unexpected exception's traceback goes to stderr only with ``MATCHCUT_DEBUG=1``.  Anything the commands themselves print
(ffmpeg, a stray print) goes to stderr: stdout carries only the JSON.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import signal
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

USAGE = "python -m matchcut.edit WS COMMAND ['{\"json\": \"arguments\"}' | @args.json | @-]"
EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
MAX_ARGS_BYTES = 16 * 1024 * 1024


class Usage(Exception):
    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.extra = extra


def _help(tools) -> Dict[str, Any]:
    return {"usage": USAGE,
            "commands": [{"name": t.name, "description": t.description, "arguments": t.schema} for t in tools],
            "exit_codes": {"0": "success", "1": "refused or failed ({\"error\": ...})", "2": "usage: unknown command, bad JSON, missing workspace "
                           "or arguments that do not match the command's schema"},
            "notes": ["arguments are one JSON object, given inline, from a file (@args.json) or from stdin (@-)",
                      "frames are whole frames at the source's exact rate; revision ids are r000, r001 ...; proposal ids are 12 hex characters",
                      "set MATCHCUT_DEBUG=1 to get an unexpected error's traceback on stderr"]}


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _arguments(text: str) -> Dict[str, Any]:
    """The arguments: inline JSON, ``@file`` or ``@-`` (stdin); a JSON object."""
    where = ""
    if text.startswith("@"):
        name = text[1:]
        where = " from stdin" if name == "-" else f" in {name}"
        try:
            if name == "-":
                text = sys.stdin.read(MAX_ARGS_BYTES + 1)
            else:
                with open(name, encoding="utf-8") as fh:
                    text = fh.read(MAX_ARGS_BYTES + 1)
        except (OSError, UnicodeDecodeError) as exc:
            raise Usage(f"cannot read the arguments{where}: {exc.strerror if isinstance(exc, OSError) and exc.strerror else exc}") from None
    if len(text) > MAX_ARGS_BYTES:
        raise Usage(f"the arguments{where} are longer than {MAX_ARGS_BYTES} bytes")
    try:
        args = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise Usage(f"the arguments{where} are not valid JSON: {str(exc)[:200]}") from None
    if not isinstance(args, dict):
        raise Usage(f"the arguments must be one JSON object, not {type(args).__name__ if args is not None else 'null'}")
    where = _surrogate(args)
    if where is not None:
        raise Usage(f"the arguments hold a lone UTF-16 surrogate ({where}): every key and string must be valid Unicode")
    return args


def _surrogate(doc: Any) -> Optional[str]:
    """Where ``doc`` holds a key or a string with a lone surrogate (JSON's ``\\ud800`` escapes parse to one; nothing can encode it), or
    None.  Iterative: a deeply nested document cannot exhaust the stack."""
    stack: List[Tuple[str, Any]] = [("arguments", doc)]
    while stack:
        path, x = stack.pop()
        if isinstance(x, str):
            if any("\ud800" <= ch <= "\udfff" for ch in x):
                return path
        elif isinstance(x, dict):
            for k, v in x.items():
                if any("\ud800" <= ch <= "\udfff" for ch in k):
                    return f"a key of {path}"
                stack.append((f"{path}.{k}", v))
        elif isinstance(x, list):
            stack.extend((f"{path}[{i}]", v) for i, v in enumerate(x))
    return None


def _refusal(exc: Exception) -> Dict[str, Any]:
    from .session import ProposalRefused

    out: Dict[str, Any] = {"error": str(exc) or type(exc).__name__, "kind": "refused"}
    if isinstance(exc, ProposalRefused):
        out.update(problems=exc.problems, timing=exc.timing, changes=exc.changes, details=exc.details)
    index = getattr(exc, "index", None)
    if index is not None:
        out["index"] = index
    return out


def run(argv: List[str]) -> Tuple[int, Dict[str, Any]]:
    """(exit code, the JSON object to print) for ``argv`` (without the program name).  Never raises (but KeyboardInterrupt)."""
    command: Optional[str] = None
    try:
        from . import tools

        if not argv or argv[0] in ("-h", "--help"):
            return EXIT_OK, _help(tools.TOOLS)
        if len(argv) < 2:
            raise Usage("no command given", commands=[t.name for t in tools.TOOLS])
        ws_arg, command, rest = argv[0], argv[1], argv[2:]
        if command not in tools.BY_NAME:
            raise Usage(f"unknown command {command[:40]!r}", commands=[t.name for t in tools.TOOLS])
        tool = tools.BY_NAME[command]
        if rest and rest[0] in ("-h", "--help"):
            return EXIT_OK, {"usage": USAGE.replace("COMMAND", command), "name": tool.name, "description": tool.description,
                             "arguments": tool.schema}
        if len(rest) > 1:
            raise Usage(f"{command} takes one JSON object of arguments, not {len(rest)} arguments")
        args = _arguments(rest[0]) if rest else {}
        ws = Path(ws_arg)
        if not ws.is_dir():
            raise Usage(f"no workspace {ws_arg[:200]}: the first argument is the workspace directory")
        try:
            return EXIT_OK, tools.call(ws, command, args)
        except tools.ArgumentError as exc:
            raise Usage(str(exc), errors=exc.errors, arguments=tool.schema) from None
    except Usage as exc:
        return EXIT_USAGE, dict({"error": str(exc), "kind": "usage", "usage": USAGE}, **exc.extra)
    except Exception as exc:  # noqa: BLE001 - every failure is reported as JSON
        from matchcut.interchange import InterchangeError
        from matchcut.pipeline.media import MediaError

        from .model import Refused

        if isinstance(exc, Refused):
            return EXIT_REFUSED, dict(_refusal(exc), command=command)
        if os.environ.get("MATCHCUT_DEBUG", "").strip().lower() in ("1", "true", "yes"):
            traceback.print_exc(file=sys.stderr)
        if isinstance(exc, (MediaError, InterchangeError, OSError)):
            text = " ".join(str(exc).split())[:500] or type(exc).__name__
            return EXIT_REFUSED, {"error": text, "kind": "error", "command": command}
        text = " ".join(str(exc).split())[:300]
        return EXIT_REFUSED, {"error": f"internal error: {type(exc).__name__}" + (f": {text}" if text else ""), "kind": "internal",
                              "command": command}


def _plain(o: Any) -> Any:
    if hasattr(o, "item"):  # numpy scalars
        return o.item()
    if isinstance(o, Path):
        return str(o)
    return str(o)


def _dumps(doc: Dict[str, Any]) -> str:
    def clean(x: Any) -> Any:
        if isinstance(x, float) and not math.isfinite(x):
            return None
        if isinstance(x, dict):
            return {str(k): clean(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [clean(v) for v in x]
        return x
    return json.dumps(clean(doc), ensure_ascii=False, default=_plain, allow_nan=False)


class _Terminated(BaseException):
    """SIGTERM while a command runs: raised like KeyboardInterrupt, so every ``finally`` runs (temporary render files are removed, the
    ffmpeg child is killed by ``subprocess.run``) and the CLI still prints its one JSON object."""


def _on_term(signum: int, frame: Any) -> None:
    raise _Terminated()


def _write(text: str) -> None:
    """Write the result to stdout as UTF-8; a character no encoding can carry (a lone surrogate in a message) is escaped, which keeps the
    JSON valid.  If even that fails, a fixed JSON error is written: stdout always gets one JSON object."""
    data = (text + "\n").encode("utf-8", "backslashreplace")
    try:
        sys.stdout.flush()
        buf = getattr(sys.stdout, "buffer", None)
        if buf is not None:
            buf.write(data)
            buf.flush()
        else:
            sys.stdout.write(data.decode("utf-8"))
            sys.stdout.flush()
    except Exception:  # noqa: BLE001
        try:
            os.write(1, b'{"error": "internal error: the result could not be written", "kind": "internal"}\n')
        except OSError:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    sys.stdout.flush()
    old_term = None
    if threading.current_thread() is threading.main_thread():
        old_term = signal.signal(signal.SIGTERM, _on_term)
    saved = os.dup(1)
    os.dup2(2, 1)  # whatever the command prints (ffmpeg, a stray print) goes to stderr; only the result goes to stdout
    text = ""
    code = EXIT_REFUSED
    try:
        try:
            with contextlib.redirect_stdout(sys.stderr):
                code, doc = run(argv)
        except KeyboardInterrupt:
            code, doc = EXIT_REFUSED, {"error": "interrupted", "kind": "error"}
        except _Terminated:
            code, doc = EXIT_REFUSED, {"error": "terminated", "kind": "error"}
        try:
            text = _dumps(doc)
        except Exception as exc:  # noqa: BLE001
            code, text = EXIT_REFUSED, json.dumps({"error": f"internal error: the result is not JSON ({type(exc).__name__})", "kind": "internal"})
        sys.stdout.flush()
    finally:
        os.dup2(saved, 1)
        os.close(saved)
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
    _write(text)
    return code


if __name__ == "__main__":
    sys.exit(main())
