"""Minimal clients for FilmCraft's agent interfaces (prototype only, standard library).

- ``Mcp``: JSON-RPC 2.0 over the stdio of ``filmcraft-cli mcp`` (headless engine, or ``--bridge``).
- ``Control``: the desktop app's JSON-lines control channel over TCP (``filmcraft --control PORT``),
  with the ``FILMCRAFT_CONTROL_TOKEN`` on every request.

Nothing here is a production adapter: there is no retry, reconnection or version negotiation beyond
what the proof needs.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

TICKS_PER_SECOND = 254_016_000_000


class McpError(RuntimeError):
    pass


class Mcp:
    """``filmcraft-cli mcp [args]`` as a subprocess; one request at a time."""

    def __init__(self, binary: str, args: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None, stderr_path: Optional[str] = None):
        self._stderr = open(stderr_path, "ab") if stderr_path else subprocess.DEVNULL
        self.proc = subprocess.Popen([binary, "mcp", *(args or [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr, env=env)
        self._id = 0
        self._lock = threading.Lock()
        self.server_info: Dict[str, Any] = {}

    def _send(self, msg: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 300.0) -> Dict[str, Any]:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            assert self.proc.stdout is not None
            deadline = time.time() + timeout
            while time.time() < deadline:
                line = self.proc.stdout.readline()
                if not line:
                    raise McpError(f"server closed stdout during {method} (exit {self.proc.poll()})")
                msg = json.loads(line)
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise McpError(f"{method}: {msg['error']}")
                    return msg["result"]
                # notifications (progress, logging) are skipped
            raise McpError(f"{method}: no reply in {timeout}s")

    def initialize(self) -> Dict[str, Any]:
        r = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "matchcut-filmcraft-probe", "version": "0"}})
        self.server_info = r
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return r

    def tools(self) -> List[Dict[str, Any]]:
        return self.request("tools/list")["tools"]

    def call(self, tool: str, arguments: Optional[Dict[str, Any]] = None, timeout: float = 300.0) -> Any:
        """Call a tool; return the parsed JSON text content (or the raw content list for images).

        Raises McpError when the tool reports ``isError``."""
        r = self.request("tools/call", {"name": tool, "arguments": arguments or {}}, timeout=timeout)
        content = r.get("content") or []
        if r.get("isError"):
            raise McpError(f"{tool}: " + " ".join(c.get("text", "") for c in content if c.get("type") == "text"))
        if any(c.get("type") == "image" for c in content):
            return content
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except ValueError:
                return texts[0]
        if "structuredContent" in r:
            return r["structuredContent"]
        return texts

    def run(self, command: str, params: Optional[Dict[str, Any]] = None, timeout: float = 300.0) -> Any:
        return self.call("command_run", {"id": command, "params": params or {}}, timeout=timeout)

    def png(self, tool: str, arguments: Dict[str, Any], path: str) -> str:
        content = self.call(tool, arguments)
        for c in content if isinstance(content, list) else []:
            if c.get("type") == "image":
                with open(path, "wb") as f:
                    f.write(base64.b64decode(c["data"]))
                return path
        raise McpError(f"{tool}: no image in the result: {str(content)[:200]}")

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()
        if self._stderr not in (None, subprocess.DEVNULL):
            self._stderr.close()


class Control:
    """The desktop app's control channel: one JSON request per line, one reply per line."""

    def __init__(self, port: int, token: Optional[str] = None, host: str = "127.0.0.1", timeout: float = 120.0):
        self.token = token or os.environ["FILMCRAFT_CONTROL_TOKEN"]
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.f = self.sock.makefile("rw", encoding="utf-8")
        self._id = 0

    def call(self, method: str, **params: Any) -> Dict[str, Any]:
        self._id += 1
        self.f.write(json.dumps({"id": self._id, "token": self.token, "method": method, "params": params}) + "\n")
        self.f.flush()
        line = self.f.readline()
        if not line:
            raise ConnectionError(f"control channel closed during {method}")
        return json.loads(line)

    def execute(self, command: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call("engine.execute", command=command, params=params or {})

    def close(self) -> None:
        try:
            self.f.close()
            self.sock.close()
        except OSError:
            pass
