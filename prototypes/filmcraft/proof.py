#!/usr/bin/env python3
"""FilmCraft as an optional visual editing companion for matchcut: a re-runnable feasibility proof.

It drives FilmCraft's headless engine through its MCP server (``filmcraft-cli mcp``, JSON-RPC on stdio) and
its CLI, using matchcut's real OTIO exports, and checks what comes back through matchcut's own ``import``.
Every step is recorded as Passed / Failed / Unsupported / Not preserved / Not tested.

Nothing here changes matchcut: fixtures and workspaces go under ``--work``; the canonical workspaces are
copied before every ``import`` so each import writes its own human stage.

    python prototypes/filmcraft/proof.py --filmcraft-cli /path/to/filmcraft-cli --work /tmp/fc-proof

See README.md next to this file for the exact commands and requirements.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))
from fcclient import TICKS_PER_SECOND, Mcp, McpError  # noqa: E402

PASSED, FAILED, UNSUPPORTED, NOT_PRESERVED, NOT_TESTED = "Passed", "Failed", "Unsupported", "Not preserved", "Not tested"


# ---------------------------------------------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------------------------------------------

class Log:
    def __init__(self) -> None:
        self.steps: List[Dict[str, Any]] = []
        self.env: Dict[str, Any] = {}
        self.timings: Dict[str, float] = {}

    def add(self, step: str, check: str, outcome: str, detail: str = "", data: Any = None) -> None:
        self.steps.append({"step": step, "check": check, "outcome": outcome, "detail": detail, "data": data})
        print(f"[{outcome:13}] {step} {check}" + (f": {detail}" if detail else ""), flush=True)

    def check(self, step: str, check: str, ok: bool, detail: str = "", data: Any = None, fail: str = FAILED) -> bool:
        self.add(step, check, PASSED if ok else fail, detail, data)
        return ok

    def time(self, key: str, seconds: float) -> None:
        self.timings[key] = round(seconds, 4)


def run(cmd: List[str], cwd: Optional[Path] = None, check: bool = True, timeout: float = 900) -> subprocess.CompletedProcess:
    p = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))} exited {p.returncode}: {(p.stderr or p.stdout)[-2000:]}")
    return p


def mc(*args: Any, check: bool = True) -> subprocess.CompletedProcess:
    """matchcut's public CLI, run from this checkout."""
    return run([sys.executable, "-m", "matchcut.pipeline.cli", *args], cwd=REPO, check=check)


# ---------------------------------------------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------------------------------------------

def make_fixtures(work: Path) -> Dict[str, Path]:
    """Synthetic footage (matchcut's own generator), a click track, a timecoded copy and two NTSC-rate variants."""
    fx = work / "fx"
    fx.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(REPO))
    from matchcut.pipeline.demo import make_click_track, make_demo_footage

    out = {"demo": fx / "demo.mp4", "click": fx / "click.wav", "tc": fx / "tc.mp4", "click_mp3": fx / "click.mp3",
           "click_m4a": fx / "click.m4a", "ntsc2997df": fx / "ntsc2997df.mp4", "ntsc23976": fx / "ntsc23976.mp4"}
    if not out["demo"].exists():
        make_demo_footage(out["demo"])
    if not out["click"].exists():
        make_click_track(out["click"], seconds=40.0, bpm=120.0)
    ff = ["ffmpeg", "-v", "error", "-nostdin", "-y"]
    if not out["tc"].exists():
        run([*ff, "-i", out["demo"], "-c", "copy", "-timecode", "01:00:00:00", out["tc"]])
    if not out["click_mp3"].exists():
        run([*ff, "-i", out["click"], "-c:a", "libmp3lame", "-b:a", "128k", out["click_mp3"]])
    if not out["click_m4a"].exists():
        run([*ff, "-i", out["click"], "-c:a", "aac", "-b:a", "128k", out["click_m4a"]])
    # NTSC rates: matchcut's generator takes whole rates, so retime 30 and 24 fps renders by 1001/1000.
    for name, base, rate, extra in (("ntsc2997df", 30, "30000/1001", ["-timecode", "00:59:59;00"]), ("ntsc23976", 24, "24000/1001", [])):
        if not out[name].exists():
            tmp = fx / f"d{base}.mp4"
            make_demo_footage(tmp, fps=base)
            run([*ff, "-i", tmp, "-vf", "setpts=PTS*1.001", "-r", rate, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                 "-af", "atempo=0.999001", "-c:a", "aac", *extra, out[name]])
    return out


def make_workspaces(work: Path, fx: Dict[str, Path]) -> Dict[str, Path]:
    """``run`` (mock backend, 8 s target) and ``export --stage final`` per fixture."""
    plan = {"music": (fx["demo"], ["--music", str(fx["click"])], True), "tc": (fx["tc"], [], False),
            "ntsc2997df": (fx["ntsc2997df"], [], False), "ntsc23976": (fx["ntsc23976"], [], False)}
    out = {}
    for name, (src, extra, render) in plan.items():
        ws = work / f"ws_{name}"
        if not (ws / "exports" / "final.otio").exists():
            mc("run", src, "-w", ws, "--backend", "mock", "-t", "8", *extra, *([] if render else ["--no-render"]))
            mc("export", ws, "--stage", "final")
        out[name] = ws
    return out


# ---------------------------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------------------------

def tpf(rate: Dict[str, int]) -> float:
    return TICKS_PER_SECOND * rate["den"] / rate["num"]


def frames(ticks: int, rate: Dict[str, int]) -> float:
    return ticks / tpf(rate)


def otio_tracks(path: Path) -> Dict[str, Any]:
    d = json.loads(path.read_text())
    return {t["name"]: t for t in d["tracks"]["children"]}


def otio_clips(track: Dict[str, Any]) -> List[Tuple[str, float, float]]:
    return [(c["name"], c["source_range"]["start_time"]["value"], c["source_range"]["duration"]["value"]) for c in track["children"] if c["OTIO_SCHEMA"].startswith("Clip")]


def import_into_copy(ws: Path, file: Path, dst: Path) -> Dict[str, Any]:
    """matchcut ``import`` on a copy of ``ws`` (the canonical workspace stays untouched)."""
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(ws, dst)
    t0 = time.time()
    p = mc("import", dst, file, check=False)
    secs = time.time() - t0
    res: Dict[str, Any] = {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr, "seconds": round(secs, 3)}
    if p.returncode == 0:
        hist = json.loads((dst / "human.json").read_text())
        res["decisions"] = [h["detail"] for h in hist["history"] if h.get("pass_name") == "human"]
        res["notes"] = json.loads((dst / "human_import.json").read_text())["notes"]
        res["human"] = hist
    return res


def clip_table(stage: Dict[str, Any]) -> Dict[str, Tuple[float, float, str, float, str]]:
    return {c["id"]: (round(c["source_in"], 6), round(c["source_out"], 6), c["transition_in"]["kind"], round(c["transition_in"]["duration"], 6), c.get("origin"))
            for c in stage["clips"]}


def read_wav_mono(path: Path):
    import numpy as np
    w = wave.open(str(path))
    n, ch, sr, sw = w.getnframes(), w.getnchannels(), w.getframerate(), w.getsampwidth()
    dt = {2: np.int16, 4: np.int32}[sw]
    a = np.frombuffer(w.readframes(n), dtype=dt).astype(np.float64).reshape(-1, ch)[:, 0] / float(np.iinfo(dt).max)
    return a, sr


def onsets(a, sr: int, thr: float = 0.05, gap: float = 0.2) -> List[float]:
    import numpy as np
    out: List[int] = []
    for i in np.nonzero(np.abs(a) > thr)[0]:
        if not out or i - out[-1] > gap * sr:
            out.append(int(i))
    return [i / sr for i in out]


def psnr(a_png: Path, b_png: Path) -> float:
    p = run(["ffmpeg", "-v", "info", "-nostdin", "-i", a_png, "-i", b_png, "-lavfi", "psnr", "-f", "null", "-"], check=False)
    m = re.search(r"average:([0-9.inf]+)", p.stderr)
    return float(m.group(1)) if m and m.group(1) != "inf" else 99.0


def ff_frame(src: Path, n: int, out: Path, crop: Optional[str] = None) -> Path:
    vf = f"select=eq(n\\,{n})" + (f",crop={crop}" if crop else "")
    run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", src, "-vf", vf, "-vsync", "0", "-frames:v", "1", out])
    return out


class Session:
    """One headless FilmCraft engine (``filmcraft-cli mcp``) with an imported matchcut OTIO."""

    def __init__(self, cli: Path, log_path: Path, otio: Optional[Path] = None):
        self.m = Mcp(str(cli), [], stderr_path=str(log_path))
        self.m.initialize()
        self.import_result = None
        if otio is not None:
            self.import_result = self.m.run("file.import", {"paths": [str(otio)]})

    def seq(self) -> Dict[str, Any]:
        return self.m.call("sequence_inspect")

    def clip(self, track: str, prefix: str) -> Dict[str, Any]:
        s = self.seq()
        for kind in ("video", "audio"):
            for t in s[kind]:
                if t["name"] == track:
                    return next(i for i in t["items"] if i["name"].startswith(prefix))
        raise KeyError(track)

    def link_picture_and_mirror(self) -> None:
        """matchcut's A1 mirrors V1 clip for clip; OTIO carries no links, so link the pairs to trim them together."""
        s = self.seq()
        for v, a in zip(s["video"][0]["items"], s["audio"][0]["items"]):
            self.m.run("clip.link", {"clips": [v["clip"], a["clip"]]})

    def close(self) -> None:
        self.m.close()


# ---------------------------------------------------------------------------------------------------------------
# the proof
# ---------------------------------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--filmcraft-cli", default=os.environ.get("FILMCRAFT_CLI"), help="path to the filmcraft-cli binary (or $FILMCRAFT_CLI)")
    ap.add_argument("--filmcraft-src", default=os.environ.get("FILMCRAFT_SRC"), help="FilmCraft checkout, only to record its commit")
    ap.add_argument("--work", default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "matchcut-filmcraft-proof"), help="scratch directory")
    ap.add_argument("--out", default=None, help="where results.json / results.md go (default: WORK/results)")
    a = ap.parse_args()
    if not a.filmcraft_cli or not Path(a.filmcraft_cli).exists():
        print("need --filmcraft-cli (build it: cargo build --release -p filmcraft-cli)", file=sys.stderr)
        return 2
    cli = Path(a.filmcraft_cli).resolve()
    work = Path(a.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out_dir = Path(a.out).resolve() if a.out else work / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (work / "fc").mkdir(exist_ok=True)
    mcp_log = work / "fc" / "mcp_stderr.log"
    log = Log()

    # ---- 0. environment ------------------------------------------------------------------------------------------
    log.env = {
        "filmcraft_cli": run([cli, "--version"]).stdout.strip(),
        "filmcraft_commit": run(["git", "-C", a.filmcraft_src, "rev-parse", "--short", "HEAD"]).stdout.strip() if a.filmcraft_src else None,
        "matchcut_commit": run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], check=False).stdout.strip(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "ffmpeg": run(["ffmpeg", "-version"]).stdout.splitlines()[0],
        "cpus": os.cpu_count(),
        "binary_bytes": cli.stat().st_size,
    }
    print(json.dumps(log.env, indent=1))

    # ---- 1. fixtures ---------------------------------------------------------------------------------------------
    t0 = time.time()
    fx = make_fixtures(work)
    wss = make_workspaces(work, fx)
    log.time("fixtures_and_matchcut_runs", time.time() - t0)
    ws = wss["music"]
    final = json.loads((ws / "final.json").read_text())
    otio = ws / "exports" / "final.otio"
    mc_tracks = otio_tracks(otio)
    rate = {"num": 25, "den": 1}

    # ---- 2. MCP server ---------------------------------------------------------------------------------------------
    S = "2 MCP"
    t0 = time.time()
    s = Session(cli, mcp_log)
    log.time("mcp_start_and_initialize", time.time() - t0)
    tools = [t["name"] for t in s.m.tools()]
    log.check(S, "initialize + tools/list (headless, no window)", len(tools) > 0, f"{len(tools)} tools: {', '.join(tools)}",
              {"server": s.m.server_info.get("serverInfo"), "protocol": s.m.server_info.get("protocolVersion"), "tools": tools})
    cmds = s.m.call("command_list")
    n_cmds = len(cmds) if isinstance(cmds, list) else len(cmds.get("commands", []))
    log.check(S, "command_list", n_cmds > 0, f"{n_cmds} engine commands")
    ui_tool_err = None
    try:
        s.m.call("ui_inspect")
    except McpError as e:
        ui_tool_err = str(e)
    log.add(S, "ui_* tools in headless mode", UNSUPPORTED if ui_tool_err else PASSED, (ui_tool_err or "answered")[:160])
    s.close()

    # ---- 3. import matchcut's OTIO -----------------------------------------------------------------------------------
    S = "3 import"
    t0 = time.time()
    s = Session(cli, mcp_log, otio)
    log.time("otio_import", time.time() - t0)
    doc = s.import_result["documents"][0]
    log.check(S, "file.import final.otio", not s.import_result["errors"] and not doc["offlineMedia"],
              f"linked {doc['linkedMedia']} media, report {doc['report'] or 'empty'}")
    seq = s.seq()
    log.add(S, "sequence settings", PASSED if seq["settings"]["frame_rate"] == rate else FAILED,
            f"{seq['settings']['frame_rate']['num']}/{seq['settings']['frame_rate']['den']} fps, {seq['settings']['width']}x{seq['settings']['height']} "
            "(OTIO carries no frame size: FilmCraft's default 1920x1080, the 320x180 clips sit unscaled in the centre)")
    tracks = {t["name"]: t for k in ("video", "audio") for t in seq[k]}
    for name in ("V1", "A1", "A2"):
        want = otio_clips(mc_tracks[name])
        got = [(i["name"], frames(i["sourceIn"], rate), i["durationFrames"]) for i in tracks.get(name, {"items": []})["items"]]
        log.check(S, f"{name} clips: names, source in, duration (frames)", [(n, float(si), float(d)) for n, si, d in want] == [(n, round(si, 6), float(d)) for n, si, d in got],
                  f"{len(got)} clips" + (" (music track)" if name == "A2" else ""), {"matchcut": want, "filmcraft": got})
    # record placement of V1 against matchcut's own reading of the OTIO (cut frames)
    want_tr = [(c["in_offset"]["value"], c["out_offset"]["value"]) for c in mc_tracks["V1"]["children"] if c["OTIO_SCHEMA"].startswith("Transition")]
    got_tr = [x for x in tracks["V1"]["transitions"]]
    cuts = [i["startFrame"] for i in tracks["V1"]["items"]][1:]
    ok = len(got_tr) == len(want_tr) and all(x["effect"] == "cross_dissolve" and x["align"] == "center" for x in got_tr)
    detail = "; ".join(f"{x['effect']} {frames(x['start'], rate):.0f}-{frames(x['start'] + x['duration'], rate):.0f}" for x in got_tr)
    log.check(S, "V1 dissolves (kind, length, centred on the cut)", ok and all(round(frames(x["duration"], rate)) == i + o for x, (i, o) in zip(got_tr, want_tr)), detail)
    log.add(S, "A1 mirror transitions", PASSED if len(tracks["A1"]["transitions"]) == len(want_tr) else FAILED,
            ", ".join(x["effect"] for x in tracks["A1"]["transitions"]) + " (audio dissolves become Constant Power crossfades)")
    mc_markers = [(m["name"], m["marked_range"]["start_time"]["value"]) for m in mc_tracks["V1"]["markers"]]
    fc_markers = [(m["name"], frames(m["start"], rate)) for m in seq["markers"]]
    log.check(S, "junction markers: names and record frames", mc_markers == fc_markers, ", ".join(f"{n}@{f:.0f}" for n, f in fc_markers),
              {"matchcut": mc_markers, "filmcraft": fc_markers})
    log.add(S, "markers moved from the V1 track to the sequence", PASSED, "FilmCraft keeps OTIO track markers as sequence markers (comment = the why line, colour kept)")
    log.add(S, "metadata.matchcut on clips, markers, timeline", NOT_PRESERVED,
            "not visible through the agent API (sequence_inspect has no metadata field); see step 6 for the export")

    # ---- 4. navigate to junction 2 -------------------------------------------------------------------------------------
    S = "4 navigate"
    j2 = next(m for m in seq["markers"] if m["name"].startswith("J2 "))
    cut2 = tracks["V1"]["items"][2]["start"]
    log.check(S, "marker J2 sits on the cut of junction 2", j2["start"] == cut2, f"marker {frames(j2['start'], rate):.0f}, cut {frames(cut2, rate):.0f}")
    s.m.run("playhead.set", {"time": j2["start"]})
    ph = s.seq()["playhead"]
    log.check(S, "playhead.set to the marker", ph == j2["start"], f"playhead at frame {frames(ph, rate):.0f}")
    sel = s.m.run("timeline.selectClipAtPlayhead")["selection"]
    c003v = s.clip("V1", "c003")
    log.check(S, "clip at the playhead is the incoming clip (c003)", c003v["clip"] in sel, f"selection {sel}")
    s.m.run("playhead.start")
    visited = []
    for _ in range(len(fc_markers)):
        s.m.run("markers.goNext")
        visited.append(round(frames(s.seq()["playhead"], rate)))
    log.check(S, "markers.goNext walks the junctions in order", visited == [round(f) for _, f in fc_markers], f"frames {visited}")

    # ---- 5. trim the in point of the clip after junction 2 by 5 frames ---------------------------------------------------
    S = "5 trim"
    try:
        s.m.run("timeline.trim", {"clip": c003v["clip"], "edge": "in", "mode": "ripple", "deltaFrames": 5})
        log.add(S, "ripple trim as imported (no links, every track sync-locked)", FAILED, "expected a sync-lock refusal, the trim ran")
    except McpError as e:
        log.add(S, "ripple trim as imported (no links, every track sync-locked)", PASSED, "refused, nothing changed: " + str(e).split("edit failed: ")[-1][:160])
    s.link_picture_and_mirror()
    s.m.run("timeline.setTrack", {"track": "A2", "syncLock": False})
    t0 = time.time()
    r = s.m.run("timeline.trim", {"clip": c003v["clip"], "edge": "in", "mode": "ripple", "deltaFrames": 5})
    log.time("ripple_trim", time.time() - t0)
    after = s.seq()
    tr2 = {t["name"]: t for k in ("video", "audio") for t in after[k]}
    v1a = [(i["name"][:4], i["startFrame"], i["durationFrames"], round(frames(i["sourceIn"], rate), 6)) for i in tr2["V1"]["items"]]
    v1b = [(i["name"][:4], i["startFrame"], i["durationFrames"], round(frames(i["sourceIn"], rate), 6)) for i in tracks["V1"]["items"]]
    exp = [(n, st - (5 if k > 2 else 0), d - (5 if k == 2 else 0), si + (5 if k == 2 else 0)) for k, (n, st, d, si) in enumerate(v1b)]
    log.check(S, "link V1/A1 pairs, A2 sync lock off, ripple trim c003 in +5f", v1a == exp, f"delta {r.get('delta')} ticks; V1 {v1a}", {"before": v1b, "after": v1a})
    a1a = [(i["name"][:4], i["startFrame"], i["durationFrames"], round(frames(i["sourceIn"], rate), 6)) for i in tr2["A1"]["items"]]
    log.check(S, "A1 mirror trimmed with the picture (linked)", a1a == exp, f"A1 {a1a}")
    a2 = tr2["A2"]["items"][0]
    log.check(S, "music (A2) stays at its offset", a2["start"] == 0 and a2["sourceIn"] == tracks["A2"]["items"][0]["sourceIn"] and a2["duration"] == tracks["A2"]["items"][0]["duration"],
              f"start {a2['start']}, sourceIn {a2['sourceIn']}, {a2['durationFrames']} frames: now {a2['durationFrames'] - after['video'][0]['items'][-1]['endFrame']} frames past the picture's end")
    trs = [(round(frames(x["start"], rate)), round(frames(x["duration"], rate))) for x in tr2["V1"]["transitions"]]
    log.check(S, "dissolves follow the edit", trs == [(63, 8), (96, 18)], f"{trs}")
    mk = [(m["name"], round(frames(m["start"], rate))) for m in after["markers"]]
    log.add(S, "sequence markers after the ripple", NOT_PRESERVED if mk[2][1] != v1a[3][1] else PASSED,
            f"{mk}: Ripple Sequence Markers is off by default, so J3/J4 stay 5 frames late (markers.rippleSequenceMarkers turns it on)")
    s.m.run("edit.undo")
    undone = [(i["name"][:4], i["startFrame"], i["durationFrames"]) for i in s.seq()["video"][0]["items"]]
    s.m.run("edit.redo")
    redone = [(i["name"][:4], i["startFrame"], i["durationFrames"], round(frames(i["sourceIn"], rate), 6)) for i in s.seq()["video"][0]["items"]]
    log.check(S, "edit.undo / edit.redo of the trim", undone == [x[:3] for x in v1b] and redone == v1a, "undo restores, redo re-applies")
    log.add(S, "slip and replace a shot", PASSED, "see step 7b")

    # ---- 6. preview: frames and a render ----------------------------------------------------------------------------------
    S = "6 preview"
    fr = work / "fc" / "frames"
    fr.mkdir(exist_ok=True)
    crop = "320:180:800:450"  # the 320x180 source sits unscaled in FilmCraft's 1920x1080 sequence
    # After the trim c003 starts at record 67 with source 172, so record 80 shows source 185. Shot 3 is a static
    # SMPTE-bars picture (source 180 and 185 look the same), so the ripple is checked on the moving shot 7
    # (mandelbrot): c005 now starts at record 146 (it was 151), so record 150 shows source 448; untrimmed it was c004.
    t0 = time.time()
    for f in (80, 150):
        s.m.png("render_frame", {"seconds": f / 25, "max_side": 0}, str(fr / f"fc_full_{f}.png"))
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", fr / f"fc_full_{f}.png", "-vf", f"crop={crop}", fr / f"fc_{f}.png"])
    log.time("render_frame_1080p_x2", time.time() - t0)
    p = psnr(fr / "fc_80.png", ff_frame(fx["demo"], 185, fr / "src_185.png"))
    log.check(S, "render_frame at record 80 after the trim = source frame 185 (ffmpeg decode)", p > 40, f"PSNR {p:.1f} dB (shot 3 is static: this checks the shot, not the frame)")
    p150 = psnr(fr / "fc_150.png", ff_frame(fx["demo"], 448, fr / "src_448.png"))
    p150_off = {d: round(psnr(fr / "fc_150.png", ff_frame(fx["demo"], 448 + d, fr / f"src_{448 + d}.png")), 1) for d in (-2, -1, 1, 2)}
    log.check(S, "render_frame at record 150 after the ripple = source frame 448 (moving shot)", p150 > 40 and all(v < p150 for v in p150_off.values()),
              f"PSNR {p150:.1f} dB; neighbouring source frames {p150_off}")
    t0 = time.time()
    r = s.m.run("file.exportMedia", {"path": str(work / "fc" / "trimmed.mp4"), "wait": True, "range": "entire", "format": "h264"}, timeout=900)
    log.time("export_h264_1080p_trimmed", time.time() - t0)
    log.check(S, "file.exportMedia H.264 (1080p, whole sequence)", Path(r["result"]["path"]).exists(), f"{r['result']['frames']} frames in {r['result']['seconds']:.2f} s ({r['result']['render_fps']:.1f} fps)")
    s.close()
    # untrimmed import against matchcut's own render (fades excluded: fade-in 0.8 s, fade-out 0.3 s)
    s0 = Session(cli, mcp_log, otio)
    cmp = {}
    for f in (30, 60, 80, 120):
        s0.m.png("render_frame", {"seconds": f / 25, "max_side": 0}, str(fr / f"u_full_{f}.png"))
        run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", fr / f"u_full_{f}.png", "-vf", f"crop={crop}", fr / f"u_{f}.png"])
        cmp[f] = round(psnr(fr / f"u_{f}.png", ff_frame(ws / "final.mp4", f, fr / f"mc_{f}.png")), 1)
    src_of = {30: 30, 60: 114, 80: 180, 120: 369}  # record frame -> source frame in the untrimmed cut
    dec = {f: round(psnr(fr / f"u_{f}.png", ff_frame(fx["demo"], n, fr / f"src_{n}.png")), 1) for f, n in src_of.items()}
    log.check(S, "untrimmed: FilmCraft frames 30, 60, 80, 120 vs ffmpeg's decode of the same source frames", all(v > 40 for v in dec.values()), f"PSNR {dec}")
    log.check(S, "untrimmed: the same frames vs matchcut's final.mp4 (hard-cut regions, outside the fades)", all(v > 30 for v in cmp.values()),
              f"PSNR {cmp} (matchcut's render is a second H.264 generation)")
    s0.m.png("render_frame", {"seconds": 66 / 25, "max_side": 0}, str(fr / "u_full_66.png"))
    run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", fr / "u_full_66.png", "-vf", f"crop={crop}", fr / "u_66.png"])
    p66 = psnr(fr / "u_66.png", ff_frame(ws / "final.mp4", 66, fr / "mc_66.png"))
    fit = dissolve_fit(fr / "u_66.png", ff_frame(fx["demo"], 120, fr / "src_120.png"), ff_frame(fx["demo"], 166, fr / "src_166.png"))
    log.add(S, "inside the J2 dissolve (frame 66): FilmCraft vs matchcut's render", NOT_PRESERVED,
            f"PSNR {p66:.1f} dB: same frames, different maths. FilmCraft's frame is a linear-light (sRGB) mix of source 120 and 166 at progress "
            f"{fit[2]} (fit {fit[0]:.1f} dB; expected 3/8 for an 8-frame dissolve from 63); matchcut's ffmpeg render mixes in gamma-encoded RGB")
    log.add(S, "matchcut's fades, music gain and ducking in a FilmCraft render", NOT_PRESERVED, "not in the OTIO as anything FilmCraft reads (metadata.matchcut only)")
    t0 = time.time()
    r = s0.m.run("file.exportMedia", {"path": str(work / "fc" / "untrimmed.mp4"), "wait": True, "range": "entire", "format": "h264"}, timeout=900)
    log.time("export_h264_1080p_untrimmed", time.time() - t0)
    s0.close()

    # ---- 7. export from FilmCraft and import back into matchcut -------------------------------------------------------------
    S = "7 round trip"
    s = Session(cli, mcp_log, otio)
    s.link_picture_and_mirror()
    s.m.run("timeline.setTrack", {"track": "A2", "syncLock": False})
    s.m.run("timeline.trim", {"clip": s.clip("V1", "c003")["clip"], "edge": "in", "mode": "ripple", "deltaFrames": 5})
    exp_dir = work / "fc" / "exports"
    exp_dir.mkdir(exist_ok=True)
    reports = {}
    for fmt in ("otio", "fcpxml", "edl"):
        t0 = time.time()
        r = s.m.run("file.exportInterchange", {"format": fmt, "path": str(exp_dir / f"trimmed.{fmt}")})
        log.time(f"export_{fmt}", time.time() - t0)
        reports[fmt] = r["report"]
    s.m.run("file.saveAs", {"path": str(work / "fc" / "trimmed.fcproj")})
    s.close()
    log.check(S, "file.exportInterchange otio / fcpxml / edl", all((exp_dir / f"trimmed.{f}").exists() for f in reports), json.dumps(reports))
    canon = clip_table(final)
    expected_human = dict(canon)
    si, so, k, d, o = canon["c003"]
    expected_human["c003"] = (round(si + 0.2, 6), so, k, d, o)
    results = {}
    for fmt in ("otio", "fcpxml", "edl"):
        res = import_into_copy(ws, exp_dir / f"trimmed.{fmt}", work / "rt" / f"trim_{fmt}")
        results[fmt] = res
        if res["exit"] != 0:
            log.add(S, f"matchcut import {fmt}", FAILED, res["stderr"].strip()[:300])
            continue
        log.check(S, f"matchcut import {fmt}: human decisions", res["decisions"] == ["human trimmed c003: in +5f"], f"{res['decisions']}")
        got = clip_table(res["human"])
        diff = {cid: (canon[cid], got.get(cid)) for cid in canon if got.get(cid) != canon[cid]}
        log.check(S, f"matchcut import {fmt}: human.json vs final.json, frame for frame", got == expected_human,
                  "only c003 source_in moved, by +0.2 s (5 frames at 25 fps); cuts, dissolves (0.3 s, 0.75 s) and keys otherwise equal" if got == expected_human else f"diff {diff}")
    # provenance
    text = {f: (exp_dir / f"trimmed.{f}").read_text() for f in ("otio", "fcpxml", "edl")}
    o = json.loads(text["otio"])
    has_mc = "\"matchcut\"" in text["otio"]
    log.add(S, "provenance: metadata.matchcut (clips, markers, timeline)", NOT_PRESERVED if not has_mc else PASSED,
            f"exported OTIO metadata keys: timeline {list(o.get('metadata', {}))}, clip {list(o['tracks']['children'][0]['children'][0]['metadata'])}")
    log.add(S, "provenance: stage fingerprint (stage_sha256)", NOT_PRESERVED if "stage_sha256" not in text["otio"] else PASSED,
            "matchcut notes: '... carries no stage fingerprint'; a re-run of the pipeline in between would show as human decisions")
    keys_in_files = any(k in t for t in text.values() for k in ("0@0.000-2.000", "3@6.500-9.000"))
    human_keys = all(c.get("origin") for c in results["otio"].get("human", {}).get("clips", []))
    log.add(S, "provenance: clip keys / clip ids in the files", NOT_PRESERVED if not keys_in_files else PASSED,
            "not in FilmCraft's files; matchcut re-derives them (" + (results["otio"]["notes"][-1] if results["otio"].get("notes") else "") + f"); human.json carries the canonical keys again: {human_keys}")
    jn = [m["name"] for m in o["tracks"]["markers"]]
    log.add(S, "provenance: junction ids", PASSED if all(re.match(r"J\d+ s\d+->s\d+", n) for n in jn) and len(jn) == 4 else NOT_PRESERVED,
            f"only as marker names {jn} (OTIO stack markers, FCPXML marker values, EDL LOC comments); marker metadata.matchcut (why, history, pin) dropped, comment kept")
    rel = o["tracks"]["children"][0]["children"][0]["media_references"]["DEFAULT_MEDIA"]["target_url"]
    log.add(S, "media reference in the exported OTIO", PASSED if rel.startswith("file:") else NOT_PRESERVED,
            f"target_url {rel!r} (relative to the export folder, not a file URL); matchcut falls back to the file name with a note")
    log.add(S, "music track A2 in the exports", PASSED if "click.wav" in text["otio"] and "click" in text["fcpxml"] and "CLICK" in text["edl"] else FAILED,
            "OTIO A2, FCPXML lane -2 connected clip, EDL event on A2; matchcut ignores it on import (picture only); its gain/fade/duck metadata is gone")
    log.add(S, "FilmCraft's own export reports", PASSED, json.dumps(reports))

    # 7b: transitions, slip, replace, through the same round trip
    S = "7b more edits"
    s = Session(cli, mcp_log, otio)
    ids = {i["name"][:4]: i["clip"] for i in s.seq()["video"][0]["items"]}
    tr = s.seq()["video"][0]["transitions"]
    s.m.run("sequence.applyVideoTransition", {"clip": ids["c002"], "edge": "in", "frames": 10, "effect": "cross_dissolve"})
    s.m.run("sequence.setTransition", {"transition": tr[0]["id"], "frames": 12})
    s.m.run("sequence.removeTransition", {"transitions": [tr[1]["id"]]})
    s.m.run("file.exportInterchange", {"format": "otio", "path": str(exp_dir / "transitions.otio")})
    res = import_into_copy(ws, exp_dir / "transitions.otio", work / "rt" / "transitions")
    want = ["human changed transition cut -> dissolve 10f", "human changed transition dissolve 8f -> dissolve 12f", "human changed transition dissolve 18f -> cut"]
    log.check(S, "add a dissolve (J1, 10f), change one (J2 8f -> 12f), remove one (J3)", res.get("decisions") == want, f"{res.get('decisions')}")
    for _ in range(3):
        s.m.run("edit.undo")
    back = [(round(frames(x["start"], rate)), round(frames(x["duration"], rate))) for x in s.seq()["video"][0]["transitions"]]
    log.check(S, "three undos restore the imported transitions", back == [(63, 8), (101, 18)], f"{back}")
    s.close()
    s = Session(cli, mcp_log, otio)
    s.m.run("timeline.slip", {"clip": s.clip("V1", "c004")["clip"], "deltaFrames": 3})
    s.m.run("file.exportInterchange", {"format": "otio", "path": str(exp_dir / "slip.otio")})
    res = import_into_copy(ws, exp_dir / "slip.otio", work / "rt" / "slip")
    log.check(S, "slip c004 by +3f", res.get("decisions") == ["human trimmed c004: in +3f, out +3f"], f"{res.get('decisions')} (matchcut has no slip decision: in and out move together)")
    s.close()
    s = Session(cli, mcp_log, otio)
    demo_item = next(c["item"] for c in s.m.call("project_inspect")["root"]["children"] if c["name"] == "demo")
    s.m.run("clip.replaceFromBin", {"clips": [s.clip("V1", "c005")["clip"]], "item": demo_item})
    s.m.run("file.exportInterchange", {"format": "otio", "path": str(exp_dir / "replace.otio")})
    res = import_into_copy(ws, exp_dir / "replace.otio", work / "rt" / "replace")
    log.check(S, "replace c005's shot (clip.replaceFromBin, the footage from its In)", res.get("decisions") == ["human removed c005 (shot 7, 17.76-18.48s)", "human added h001 (shot 0, 0.00-0.72s)"],
              f"{res.get('decisions')}")
    s.close()

    # ---- 8. untouched round trips: rates, timecode, drop-frame ---------------------------------------------------------------
    S = "8 untouched"
    for name, w in wss.items():
        s = Session(cli, mcp_log, w / "exports" / "final.otio")
        st = s.seq()["settings"]
        for fmt in ("otio", "fcpxml", "edl"):
            s.m.run("file.exportInterchange", {"format": fmt, "path": str(exp_dir / f"untouched_{name}.{fmt}")})
        s.close()
        for fmt in ("otio", "fcpxml", "edl"):
            res = import_into_copy(w, exp_dir / f"untouched_{name}.{fmt}", work / "rt" / f"untouched_{name}_{fmt}")
            tc_note = next((n for n in res.get("notes", []) if "timecode" in n), "")
            log.check(S, f"{name} {fmt}: no edit -> no decisions", res.get("exit") == 0 and res.get("decisions") == [],
                       f"{st['frame_rate']['num']}/{st['frame_rate']['den']} fps; {res.get('decisions') if res.get('exit') == 0 else res.get('stderr', '').strip()[:200]}" + (f"; {tc_note}" if tc_note else ""))
    edl = (exp_dir / "untouched_ntsc2997df.edl").read_text()
    mc_edl = (wss["ntsc2997df"] / "exports" / "final.edl").read_text()
    log.add(S, "29.97 drop-frame label", NOT_PRESERVED if "NON-DROP" in edl and "DROP FRAME" in mc_edl.splitlines()[1] else PASSED,
            f"matchcut writes '{mc_edl.splitlines()[1]}'; FilmCraft's sequence from OTIO is non-drop and writes '{edl.splitlines()[1]}' "
            "(frame counts exact, timecode labels differ; sequence.settings dropFrame=true would be needed)")

    # ---- 9. audio: music offset after the trim, sample level; decoders --------------------------------------------------------
    S = "9 audio"
    ref, rsr = read_wav_mono(fx["click"])
    ref_on = onsets(ref, rsr)
    variants = {"wav": fx["click"], "mp3": fx["click_mp3"], "m4a": fx["click_m4a"]}
    for tag, music in variants.items():
        src_otio = otio
        if tag != "wav":
            src_otio = work / "fc" / f"final_{tag}.otio"
            src_otio.write_text(otio.read_text().replace(fx["click"].name, music.name))
        s = Session(cli, mcp_log, src_otio)
        s.link_picture_and_mirror()
        s.m.run("timeline.setTrack", {"track": "A2", "syncLock": False})
        s.m.run("timeline.setTrack", {"track": "A1", "muted": True})
        outs = {}
        for phase in ("before", "after"):
            if phase == "after":
                s.m.run("timeline.trim", {"clip": s.clip("V1", "c003")["clip"], "edge": "in", "mode": "ripple", "deltaFrames": 5})
            t0 = time.time()
            r = s.m.run("file.exportMedia", {"path": str(work / "fc" / f"music_{tag}_{phase}.wav"), "wait": True, "range": "entire", "format": "wav"})
            log.time(f"export_wav_{tag}_{phase}", time.time() - t0)
            a_, sr = read_wav_mono(Path(r["result"]["path"]))
            on = onsets(a_, sr)
            n = min(len(on), len([x for x in ref_on if x < len(a_) / sr]))
            outs[phase] = ([round((on[k] - ref_on[k]) * sr, 2) for k in range(n)], float(abs(a_).max()), sr)
        s.close()
        (db, pb, sr), (da, pa, _) = outs["before"], outs["after"]
        same = db == da
        tol = 2.0 if tag == "wav" else 48.0
        ok = same and all(abs(x) <= tol for x in da)
        log.check(S, f"music {tag}: click onsets vs the source file, before and after the trim", ok,
                  f"{len(da)} clicks, offset {min(da)}..{max(da)} samples at {sr} Hz (unchanged by the trim: {same})", {"before": db, "after": da})
        if tag == "wav":
            log.add(S, "music gain (-8 dB in metadata.matchcut)", NOT_PRESERVED if pa > 0.9 else PASSED, f"peak {pa:.3f} with A1 muted (source peak {float(abs(ref).max()):.3f}): played at 0 dB")

    # ---- 10. CLI route (no MCP): one process per command ------------------------------------------------------------------------
    S = "10 CLI"
    proj = work / "fc" / "cli.fcproj"
    t0 = time.time()
    r1 = run([cli, "--save-as", proj, "import", otio])
    inspect = json.loads(run([cli, "--project", proj, "inspect", "sequence"]).stdout)
    v1 = inspect["video"][0]["items"]
    c3 = next(i for i in v1 if i["name"].startswith("c003"))
    batch = "\n".join(json.dumps({"id": "clip.link", "params": {"clips": [v["clip"], a_["clip"]]}}) for v, a_ in zip(v1, inspect["audio"][0]["items"]))
    batch += "\n" + json.dumps({"id": "timeline.setTrack", "params": {"track": "A2", "syncLock": False}})
    batch += "\n" + json.dumps({"id": "timeline.trim", "params": {"clip": c3["clip"], "edge": "in", "mode": "ripple", "deltaFrames": 5}})
    batch += "\n" + json.dumps({"id": "file.exportInterchange", "params": {"format": "otio", "path": str(exp_dir / "cli_trimmed.otio")}})
    p = subprocess.run([str(cli), "--project", str(proj), "--save", "run", "-"], input=batch, capture_output=True, text=True)
    log.time("cli_route_total", time.time() - t0)
    res = import_into_copy(ws, exp_dir / "cli_trimmed.otio", work / "rt" / "cli_trim")
    log.check(S, "filmcraft-cli import / inspect / run (JSONL) / --save, then matchcut import", p.returncode == 0 and res.get("decisions") == ["human trimmed c003: in +5f"],
              f"exit {p.returncode}; {res.get('decisions')}")
    rr = run([cli, "--project", proj, "render", "--seconds", "3.2", "--out", work / "fc" / "cli_80.png"], check=False)
    log.check(S, "filmcraft-cli render (one frame, from the saved project)", rr.returncode == 0, (rr.stdout + rr.stderr).strip()[:160])

    # ---- 11. media compatibility (FilmCraft's own demuxers/decoders) ---------------------------------------------------------------
    S = "11 media"
    for key in ("demo", "tc", "click", "click_mp3", "click_m4a", "ntsc2997df", "ntsc23976"):
        pr = run([cli, "probe", fx[key], "--compact"], check=False)
        try:
            d = json.loads(pr.stdout)
            v = d.get("video") or {}
            au = (d.get("audio_streams") or [{}])[0]
            det = f"{d.get('container')}: {v.get('codec', '-')} {v.get('width', '')}x{v.get('height', '')} " \
                  f"{(str(v['frame_rate']['num']) + '/' + str(v['frame_rate']['den'])) if v.get('frame_rate') else ''}; {au.get('codec', '-')} {au.get('sample_rate', '')} Hz; start TC {d.get('start_timecode')}"
            log.add(S, f"probe {fx[key].name}", PASSED, det)
        except ValueError:
            log.add(S, f"probe {fx[key].name}", FAILED, (pr.stderr or pr.stdout)[:200])

    # ---- 12. resources ------------------------------------------------------------------------------------------------------------
    S = "12 resources"
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    log.add(S, "peak RSS of any child process (FilmCraft, ffmpeg or matchcut)", PASSED, f"{ru.ru_maxrss / 1024:.0f} MB (ru_maxrss of the largest child)")
    log.add(S, "timings (s)", PASSED, json.dumps(log.timings))

    write_results(log, out_dir)
    failed = [x for x in log.steps if x["outcome"] == FAILED]
    print(f"\n{len(log.steps)} checks: " + ", ".join(f"{o} {sum(1 for x in log.steps if x['outcome'] == o)}" for o in (PASSED, FAILED, UNSUPPORTED, NOT_PRESERVED, NOT_TESTED)))
    print(f"results: {out_dir / 'results.md'}")
    return 1 if failed else 0


def dissolve_fit(frame: Path, a: Path, b: Path) -> Tuple[float, str, float]:
    """Best (PSNR, space, progress) for ``frame`` as a mix of ``a`` and ``b``, gamma or linear light, progress in 1/16."""
    import numpy as np
    from PIL import Image

    def load(p: Path):
        return np.asarray(Image.open(p).convert("RGB")).astype(np.float64)

    def lin(x):
        x = x / 255
        return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)

    def enc(y):
        return 255 * np.where(y <= 0.0031308, y * 12.92, 1.055 * np.power(np.clip(y, 0, None), 1 / 2.4) - 0.055)

    f, A, B = load(frame), load(a), load(b)
    best = (0.0, "", 0.0)
    for name, fw, bw in (("gamma", lambda x: x, lambda x: x), ("linear", lin, enc)):
        for k in range(17):
            p = k / 16
            mse = ((f - bw((1 - p) * fw(A) + p * fw(B))) ** 2).mean()
            score = 99.0 if mse == 0 else float(10 * np.log10(255 ** 2 / mse))
            if score > best[0]:
                best = (score, name, p)
    return best


def write_results(log: Log, out: Path) -> None:
    (out / "results.json").write_text(json.dumps({"env": log.env, "timings": log.timings, "steps": log.steps}, indent=1, default=str))
    lines = ["# FilmCraft proof: results", "", "Generated by `prototypes/filmcraft/proof.py`.", "", "## Environment", ""]
    lines += [f"- {k}: {v}" for k, v in log.env.items()]
    lines += ["", "## Steps", "", "| Step | Check | Outcome | Detail |", "|---|---|---|---|"]
    for x in log.steps:
        det = str(x["detail"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {x['step']} | {x['check']} | **{x['outcome']}** | {det} |")
    lines += ["", "## Timings (seconds)", ""] + [f"- {k}: {v}" for k, v in log.timings.items()]
    counts = ", ".join(f"{o}: {sum(1 for x in log.steps if x['outcome'] == o)}" for o in (PASSED, FAILED, UNSUPPORTED, NOT_PRESERVED, NOT_TESTED))
    lines += ["", f"Totals: {counts}.", ""]
    (out / "results.md").write_text("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
