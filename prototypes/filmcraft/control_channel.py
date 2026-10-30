#!/usr/bin/env python3
"""FilmCraft's desktop app driven over its JSON-lines control channel and the MCP bridge (prototype).

Starts the app (``filmcraft --control PORT --control-input-only``) with a fresh token and an isolated HOME/XDG
directory, under Xvfb when ``--xvfb`` is given, then:

1. control channel (TCP, ``engine.execute`` etc.): import matchcut's OTIO, inspect, put the playhead on junction 2,
   locate the clip, take screenshots, trim, export OTIO, undo;
2. MCP bridge (``filmcraft-cli mcp --bridge``): tools/list, sequence_inspect, render_frame, ui_screenshot;
3. matchcut ``import`` of the OTIO the app wrote (on a copy of the workspace).

    python prototypes/filmcraft/control_channel.py --filmcraft APP --filmcraft-cli CLI --workspace WS --work DIR --xvfb
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fcclient import TICKS_PER_SECOND, Control, Mcp, McpError  # noqa: E402
from proof import FAILED, NOT_PRESERVED, PASSED, UNSUPPORTED, Log, import_into_copy, write_results  # noqa: E402


def wait_port(port: int, seconds: float) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.25)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--filmcraft", required=True, help="the desktop app binary")
    ap.add_argument("--filmcraft-cli", required=True)
    ap.add_argument("--workspace", required=True, help="a matchcut workspace with exports/final.otio (the proof's ws_music)")
    ap.add_argument("--work", required=True)
    ap.add_argument("--port", type=int, default=9876)
    ap.add_argument("--xvfb", action="store_true", help="start Xvfb on :97 for the app")
    a = ap.parse_args()
    work = Path(a.work).resolve()
    ws = Path(a.workspace).resolve()
    otio = ws / "exports" / "final.otio"
    home = work / "home"
    for d in ("", ".config", ".local/share", ".cache", "run"):
        (home / d).mkdir(parents=True, exist_ok=True)
    os.chmod(home / "run", 0o700)
    token = secrets.token_hex(32)
    env = dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local/share"),
               XDG_CACHE_HOME=str(home / ".cache"), XDG_RUNTIME_DIR=str(home / "run"), FILMCRAFT_CONTROL_TOKEN=token)
    xvfb = None
    if a.xvfb:
        xvfb = subprocess.Popen(["Xvfb", ":97", "-screen", "0", "1920x1080x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env["DISPLAY"] = ":97"
        time.sleep(1)
    log = Log()
    log.env = {"app": a.filmcraft, "display": env.get("DISPLAY"), "port": a.port}
    app_log = open(work / "app.log", "wb")
    t0 = time.time()
    app = subprocess.Popen([a.filmcraft, "--control", str(a.port), "--control-input-only"], env=env, stdout=app_log, stderr=subprocess.STDOUT)
    up = wait_port(a.port, 60)
    log.time("app_start_to_control_port", time.time() - t0)
    S = "C control channel"
    if not log.check(S, "app starts and opens the control port", up and app.poll() is None, f"exit {app.poll()}; see app.log"):
        write_results(log, work)
        return 1
    try:
        c = Control(a.port, token)
        bad = Control.__new__(Control)
        bad.token, bad.sock = "0" * 64, socket.create_connection(("127.0.0.1", a.port))
        bad.f, bad._id = bad.sock.makefile("rw", encoding="utf-8"), 0
        r = bad.call("engine.commands")
        log.check(S, "a wrong token is refused", r.get("ok") is False, str(r.get("error"))[:120])
        bad.close()
        r = c.call("engine.commands")
        n = len(r["result"]) if isinstance(r.get("result"), list) else len(r.get("result", {}).get("commands", []))
        log.check(S, "engine.commands", r.get("ok"), f"{n} commands")
        t0 = time.time()
        r = c.execute("file.import", {"paths": [str(otio)]})
        log.time("control_import_otio", time.time() - t0)
        log.check(S, "engine.execute file.import final.otio", r.get("ok"), json.dumps(r.get("result", r.get("error")))[:240])
        seq = c.execute("sequence.inspect")["result"]
        tpf = TICKS_PER_SECOND / 25
        v1 = seq["video"][0]["items"]
        log.check(S, "sequence.inspect (same engine as headless)", [i["startFrame"] for i in v1] == [0, 50, 67, 110, 151],
                  f"V1 starts {[i['startFrame'] for i in v1]}, markers {[m['name'] for m in seq['markers']]}")
        j2 = next(m for m in seq["markers"] if m["name"].startswith("J2 "))
        c.execute("playhead.set", {"time": j2["start"]})
        ins = c.call("ui.inspect")
        ph = c.execute("sequence.inspect")["result"]["playhead"]
        log.check(S, "playhead.set to marker J2", ph == j2["start"], f"playhead frame {ph / tpf:.0f}; ui.inspect keys {sorted(ins.get('result', {}).keys())[:12]}")
        c003 = next(i for i in v1 if i["name"].startswith("c003"))
        loc = c.call("ui.timeline.locate", clip=c003["clip"], edge="In")
        log.check(S, "ui.timeline.locate c003's In edge", loc.get("ok"), json.dumps(loc.get("result", loc.get("error")))[:200])
        shots = {}
        for name, params in (("window", {}), ("timeline", {"panel": "Timeline"}), ("program", {"panel": "Program"})):
            path = work / f"screenshot_{name}.png"
            t0 = time.time()
            r = c.call("ui.screenshot", path=str(path), **params)
            shots[name] = (r.get("ok"), round(time.time() - t0, 2), path.exists() and path.stat().st_size)
        log.check(S, "ui.screenshot (window, Timeline, Program)", all(v[0] for v in shots.values()), json.dumps(shots))
        for v, au in zip(v1, seq["audio"][0]["items"]):
            c.execute("clip.link", {"clips": [v["clip"], au["clip"]]})
        c.execute("timeline.setTrack", {"track": "A2", "syncLock": False})
        r = c.execute("timeline.trim", {"clip": c003["clip"], "edge": "in", "mode": "ripple", "deltaFrames": 5})
        log.check(S, "engine.execute timeline.trim (ripple, c003 in +5f)", r.get("ok"), json.dumps(r.get("result", r.get("error")))[:160])
        c.call("ui.screenshot", path=str(work / "screenshot_timeline_trimmed.png"), panel="Timeline")
        out = work / "app_trimmed.otio"
        r = c.execute("file.exportInterchange", {"format": "otio", "path": str(out)})
        log.check(S, "engine.execute file.exportInterchange otio", r.get("ok") and out.exists(), str(out.name))
        res = import_into_copy(ws, out, work / "rt_app")
        log.check(S, "matchcut import of the app's OTIO", res.get("decisions") == ["human trimmed c003: in +5f"], f"{res.get('decisions')}")

        S = "B MCP bridge"
        m = Mcp(a.filmcraft_cli, ["--bridge", f"127.0.0.1:{a.port}"], env=env, stderr_path=str(work / "bridge_stderr.log"))
        m.initialize()
        tools = [t["name"] for t in m.tools()]
        log.check(S, "tools/list over the bridge", len(tools) > 0, f"{len(tools)} tools")
        bseq = m.call("sequence_inspect")
        log.check(S, "sequence_inspect sees the app's trimmed sequence", [i["startFrame"] for i in bseq["video"][0]["items"]] == [0, 50, 67, 105, 146],
                  f"{[i['startFrame'] for i in bseq['video'][0]['items']]}")
        try:
            m.png("render_frame", {"seconds": 3.2}, str(work / "bridge_render_frame.png"))
            log.add(S, "render_frame (bridge: a Program monitor screenshot)", PASSED, "bridge_render_frame.png")
        except McpError as e:
            log.add(S, "render_frame (bridge)", FAILED, str(e)[:200])
        try:
            m.png("ui_screenshot", {"panel": "Timeline"}, str(work / "bridge_timeline.png"))
            log.add(S, "ui_screenshot Timeline", PASSED, "bridge_timeline.png")
        except McpError as e:
            log.add(S, "ui_screenshot Timeline", FAILED, str(e)[:200])
        r = m.run("edit.undo")
        log.check(S, "command_run edit.undo through the bridge", isinstance(r, dict) and r.get("undone") == "Ripple Trim", json.dumps(r)[:120])
        m.close()
        c.call("app.quit")
        c.close()
    except Exception as e:  # record and stop the app
        log.add("C control channel", "unexpected error", FAILED, f"{type(e).__name__}: {e}"[:300])
    finally:
        try:
            app.wait(timeout=15)
        except subprocess.TimeoutExpired:
            app.kill()
        app_log.close()
        if xvfb:
            xvfb.terminate()
    log.add("C control channel", "app.quit closes the app", PASSED if app.returncode == 0 else FAILED, f"exit status {app.returncode}")
    write_results(log, work)
    print(f"results: {work / 'results.md'}")
    return 1 if any(x["outcome"] == FAILED for x in log.steps) else 0


if __name__ == "__main__":
    sys.exit(main())
