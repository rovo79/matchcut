"""The JSON CLI (``python -m matchcut.edit``): one JSON object on stdout in every case, exit codes 0 (success), 1 (refused or failed) and 2
(usage, including arguments that do not match the command's schema), no traceback for bad input, and an end-to-end session through
subprocesses."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest

from matchcut.edit import __main__ as cli
from matchcut.edit import session as S
from matchcut.edit import tools as T

from .test_session import TRIM, touch_final

REPO = Path(__file__).resolve().parents[2]


def run_cli(*argv: str, stdin: Optional[str] = None, env: Optional[Dict[str, str]] = None, cwd: Optional[Path] = None) -> Tuple[int, Dict[str, Any], str]:
    """Run the CLI in a subprocess: (exit code, the one JSON object on stdout, stderr).  Fails on anything else on stdout, or a traceback."""
    e = dict(os.environ, PYTHONPATH=str(REPO))
    e.pop("MATCHCUT_DEBUG", None)
    e.update(env or {})
    proc = subprocess.run([sys.executable, "-m", "matchcut.edit", *argv], input=stdin, capture_output=True, text=True, env=e,
                          cwd=str(cwd or REPO), timeout=300)
    lines = proc.stdout.splitlines()
    assert len(lines) == 1, proc.stdout[:2000]
    doc = json.loads(lines[0])
    assert isinstance(doc, dict)
    assert "Traceback" not in proc.stderr and "Traceback" not in proc.stdout, proc.stderr
    return proc.returncode, doc, proc.stderr


@pytest.fixture
def ws(make_ws) -> Path:
    w = make_ws()
    cfg = json.loads((w / "config.json").read_text())
    cfg["render"]["proxy_width"] = 64  # the test pattern's own width
    (w / "config.json").write_text(json.dumps(cfg))
    S.start(w)
    return w


def call(*argv: str) -> Tuple[int, Dict[str, Any]]:
    code, doc = cli.run(list(argv))
    json.dumps(doc)
    return code, doc


# ----------------------------------------------------------------------------------------------- in process: exit codes and errors
BAD = [
    # (command, arguments, exit code, in the error)
    ("bogus", None, 2, "unknown command 'bogus'"),
    ("propose", "{bad", 2, "not valid JSON"),
    ("propose", "", 2, "not valid JSON"),
    ("propose", "[1, 2]", 2, "one JSON object, not list"),
    ("propose", "null", 2, "one JSON object, not null"),
    ("propose", '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": NaN}]}', 2, "not valid JSON"),
    ("propose", "@missing.json", 2, "cannot read the arguments in missing.json"),
    ("propose", '{"ops": "trim"}', 2, 'ops must be a list, not "trim"'),
    ("propose", '{"ops": []}', 2, "ops needs at least 1 item"),
    ("propose", '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": 1.5}]}', 2, "ops[0].frames must be a whole number, not 1.5"),
    ("propose", '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": true}]}', 2, "ops[0].frames must be a whole number, not true"),
    ("propose", '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": "3"}]}', 2, 'ops[0].frames must be a whole number, not "3"'),
    ("propose", '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": 1' + "0" * 400 + "}]}", 2, "at most 1000000000, not ~1e400"),
    ("propose", '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": 1' + "0" * 5000 + "}]}", 2, "not valid JSON"),
    ("propose", '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5, "frame": 2}]}', 2, 'unknown argument "frame"'),
    ("propose", '{"ops": [{"op": "trim"}], "guards": {"keep": []}}', 2, 'unknown argument "keep"'),
    ("propose", "{}", 2, "ops is required"),
    ("apply", '{"proposal": "../../../etc/passwd"}', 2, "is not a proposal id"),
    ("apply", '{"proposal": "0123456789AB"}', 2, "is not a proposal id"),
    ("apply", "{}", 2, "proposal is required"),
    ("checkout", '{"revision": "r1"}', 2, "is not a revision id"),
    ("checkout", '{"revision": "../r000"}', 2, "is not a revision id"),
    ("checkout", '{"revision": "r000/../../x"}', 2, "is not a revision id"),
    ("inspect", '{"revision": 1}', 2, "revision must be a string, not 1"),
    ("frames", '{"junction": 1.5}', 2, "junction"),
    ("frames", "{}", 2, "junction is required"),
    ("render", '{"quality": "draft"}', 2, "quality must be one of proxy, full"),
    ("pin_transition", '{"pin": "s0->s1", "hold": true, "kind": "dissolve", "frames": 2.5}', 2, "frames must be a whole number"),
    ("pin_transition", '{"pin": "s0->s1"}', 2, "hold is required"),
    ("frames", '{"junction": "0"}', 2, "junction"),
    ("inspect", '{"revision": "\\ud800"}', 2, "lone UTF-16 surrogate (arguments.revision)"),
    ("inspect", '{"\\ud83d": 1}', 2, "lone UTF-16 surrogate (a key of arguments)"),
    ("propose", '{"ops": [{"op": "trim", "clip": "c\\ud800", "edge": "out", "frames": -3}]}', 2, "surrogate (arguments.ops[0].clip)"),
    ("frames", '{"junction": 2.0}', 0, ""),
    # well-formed requests the session refuses
    ("checkout", '{"revision": "r999"}', 1, "no revision r999"),
    ("apply", '{"proposal": "0123456789ab"}', 1, "no proposal 0123456789ab"),
    ("undo", None, 1, "nothing to undo"),
    ("redo", "{}", 1, "nothing to redo"),
    ("frames", '{"junction": 9}', 1, "no junction 9"),
    ("frames", '{"junction": "s7-s9"}', 1, "no junction s7-s9"),
    ("propose", '{"ops": [{"op": "trim", "clip": "c999", "edge": "out", "frames": -5}]}', 1, "no clip c999"),
    ("propose", '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": -45}]}', 1, "below the minimum clip length"),
    ("pin_transition", '{"pin": "s0->s1", "hold": true}', 1, "no pin s0->s1"),
    ("start", None, 1, "a session is open on final"),
]


@pytest.mark.parametrize("command, arguments, code, message", BAD, ids=[f"{b[0]}-{i}" for i, b in enumerate(BAD)])
def test_bad_input_is_one_json_error_with_its_exit_code(ws, command, arguments, code, message):
    before = sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file())
    argv = [str(ws), command] + ([arguments] if arguments is not None else [])
    got, doc = call(*argv)
    assert got == code, doc
    if code == 0:
        return
    assert message in doc["error"], doc
    assert doc["kind"] == ("usage" if code == 2 else "refused")
    if code == 2 and command in T.BY_NAME and "errors" in doc:
        assert doc["arguments"] == T.BY_NAME[command].schema and all(isinstance(x, str) for x in doc["errors"])
    assert sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file()) == before


def test_usage_errors_without_a_command(ws, tmp_path):
    assert call()[0] == 0 and "commands" in call()[1]  # no arguments at all: the help
    code, doc = call(str(ws))
    assert code == 2 and doc["error"] == "no command given" and set(doc["commands"]) == set(T.BY_NAME)
    code, doc = call(str(tmp_path / "nope"), "log")
    assert code == 2 and "no workspace" in doc["error"]
    code, doc = call(str(ws), "log", "{}", "{}")
    assert code == 2 and "one JSON object of arguments, not 2" in doc["error"]
    (tmp_path / "file").write_text("x")
    assert call(str(tmp_path / "file"), "log")[0] == 2


def test_a_workspace_without_a_session_is_a_refusal(make_ws):
    code, doc = call(str(make_ws("bare")), "inspect")
    assert code == 1 and "no edit session" in doc["error"] and doc["command"] == "inspect"


def test_a_proposal_the_rules_refuse_carries_its_problems_timing_and_changes(ws):
    args = json.dumps({"ops": TRIM, "guards": {"max_duration_change_frames": 2}})
    code, doc = call(str(ws), "propose", args)
    assert code == 1 and doc["kind"] == "refused" and doc["command"] == "propose"
    assert doc["problems"] == ["guard max_duration_change_frames: the running time changes by -5 frames (at most 2 allowed)"]
    assert doc["timing"]["duration_change"] == -5 and doc["changes"] == ["c002: out -5f"] and doc["details"]
    code, doc = call(str(ws), "propose", json.dumps({"ops": [TRIM[0], {"op": "remove", "clip": "c999"}]}))
    assert code == 1 and doc["index"] == 1 and "no clip c999" in doc["error"]


def test_a_stale_session(ws):
    touch_final(ws)
    code, doc = call(str(ws), "propose", json.dumps({"ops": TRIM}))
    assert code == 1 and "stale" in doc["error"]
    code, doc = call(str(ws), "inspect")
    assert code == 0 and doc["session"]["stale"] is True
    code, doc = call(str(ws), "log")
    assert code == 0 and doc["stale"] is True


def test_arguments_from_a_file_and_stdin(ws, tmp_path, monkeypatch):
    f = tmp_path / "args.json"
    f.write_text(json.dumps({"ops": TRIM}))
    code, doc = call(str(ws), "propose", f"@{f}")
    assert code == 0 and doc["changes"] == ["c002: out -5f"]
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"proposal": doc["proposal"]})))
    code, out = call(str(ws), "apply", "@-")
    assert code == 0 and out["revision"] == "r001"


def test_help(ws):
    code, doc = call("--help")
    assert code == 0 and [c["name"] for c in doc["commands"]] == [t.name for t in T.TOOLS]
    assert all(c["description"] and c["arguments"]["type"] == "object" for c in doc["commands"])
    assert set(doc["exit_codes"]) == {"0", "1", "2"}
    code, doc = call(str(ws), "propose", "--help")
    assert code == 0 and doc["name"] == "propose" and doc["arguments"] == T.BY_NAME["propose"].schema


def test_an_unexpected_error_is_json_and_its_traceback_only_with_debug(ws, monkeypatch, capsys):
    def boom(w, a):
        raise ZeroDivisionError("division by zero")
    monkeypatch.setitem(T.BY_NAME, "log", T.Tool("log", "x", T._obj(), boom))
    code, doc = call(str(ws), "log")
    assert code == 1 and doc == {"error": "internal error: ZeroDivisionError: division by zero", "kind": "internal", "command": "log"}
    assert "Traceback" not in capsys.readouterr().err
    monkeypatch.setenv("MATCHCUT_DEBUG", "1")
    assert call(str(ws), "log")[0] == 1
    assert "Traceback" in capsys.readouterr().err


def test_main_keeps_stdout_for_the_json(ws, monkeypatch, capfd):
    def noisy(w, a):
        print("chatter from a command")
        os.write(1, b"raw bytes on fd 1\n")
        return {"ok": True, "n": float("nan")}
    monkeypatch.setitem(T.BY_NAME, "log", T.Tool("log", "x", T._obj(), noisy))
    assert cli.main([str(ws), "log"]) == 0
    out, err = capfd.readouterr()
    assert out == '{"ok": true, "n": null}\n'
    assert "chatter from a command" in err and "raw bytes on fd 1" in err


# ----------------------------------------------------------------------------------------------- subprocesses
@pytest.mark.parametrize("argv, code, message", [
    (["WS", "propose", "{bad"], 2, "not valid JSON"),
    (["WS", "propose", "@missing.json"], 2, "cannot read the arguments"),
    (["WS", "nope"], 2, "unknown command"),
    (["WS", "propose", '{"ops": {"op": "trim"}}'], 2, "ops must be a list"),
    (["WS", "propose", '{"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": 2.5}]}'], 2, "whole number"),
    (["WS", "checkout", '{"revision": "r00x"}'], 2, "not a revision id"),
    (["WS", "preview", '{"proposal": "../../x/y/zzzz"}'], 2, "not a proposal id"),
    (["WS", "propose", '{"ops": []}'], 2, "at least 1 item"),
    (["/no/such/workspace", "log"], 2, "no workspace"),
    (["WS", "checkout", '{"revision": "r042"}'], 1, "no revision r042"),
    (["WS", "inspect", '{"revision": "\\ud800"}'], 2, "surrogate"),
    (["WS", "inspect", '{"\\ud83d": 1}'], 2, "surrogate"),
    (["WS", "propose", '{"ops":[{"op":"trim","clip":"c\\ud800","edge":"out","frames":-3}]}'], 2, "surrogate"),
])
def test_bad_input_in_a_subprocess(ws, argv, code, message):
    got, doc, err = run_cli(*[str(ws) if a == "WS" else a for a in argv])
    assert got == code and message in doc["error"], doc
    assert err == ""


def test_end_to_end_through_the_cli(ws, tmp_path):
    w = str(ws)
    code, doc, _ = run_cli(w, "start", '{"restart": true}')
    assert code == 0 and doc["head"] == "r000" and doc["moved_aside"].startswith("edits.replaced-")
    code, doc, _ = run_cli(w, "inspect")
    assert code == 0 and [c["id"] for c in doc["clips"]] == ["c001", "c002", "c003", "c004", "c005"] and doc["unused_shots"] == [2, 4, 7]
    args = tmp_path / "ops.json"
    args.write_text(json.dumps({"ops": TRIM + [{"op": "set_transition", "junction": 3, "kind": "dissolve", "frames": 6}]}))
    code, prop, _ = run_cli(w, "propose", f"@{args}")
    assert code == 0 and prop["changes"] == ["c002: out -5f", "c003|c004: cut -> dissolve 6f"] and prop["timing"]["duration_change"] == -11
    pid = prop["proposal"]
    code, pv, _ = run_cli(w, "preview", json.dumps({"proposal": pid}))
    assert code == 0 and Path(pv["path"]).is_file() and pv["frames"] == 262 - 11 and pv["proposal"] == pid
    code, doc, _ = run_cli(w, "apply", json.dumps({"proposal": pid}))
    assert code == 0 and doc["revision"] == "r001"
    code, doc, _ = run_cli(w, "undo")
    assert code == 0 and doc == {"head": "r000", "previous": "r001"}
    code, doc, _ = run_cli(w, "redo", "{}")
    assert code == 0 and doc["head"] == "r001" and doc["branches"] == ["r001"]
    code, doc, _ = run_cli(w, "compare", '{"a": "r000", "b": "r001"}')
    assert code == 0 and doc["changes"] == prop["changes"] and doc["timing"]["duration_frames"] == [262, 251]
    code, doc, _ = run_cli(w, "log")
    assert code == 0 and [r["id"] for r in doc["revisions"]] == ["r000", "r001"] and doc["head"] == "r001"
    code, rd, _ = run_cli(w, "render")
    assert code == 0 and rd["path"] == pv["path"] and rd["cached"]  # the preview is the revision's render
    code, rw, _ = run_cli(w, "render", '{"junction": "s3-s5", "quality": "proxy"}')
    assert code == 0 and Path(rw["path"]).is_file() and rw["junction"] == {"n": 3, "id": "s3-s5"} and rw["kind"] == "window"
    code, fr, _ = run_cli(w, "frames", '{"junction": 2}')
    assert code == 0 and fr["outgoing"]["source_frame"] == 164 and fr["incoming"]["source_frame"] == 320
    assert Path(fr["outgoing"]["path"]).is_file() and Path(fr["incoming"]["path"]).is_file()
    code, doc, _ = run_cli(w, "checkout", '{"revision": "r000"}')
    assert code == 0 and doc["head"] == "r000"
    code, doc, _ = run_cli(w, "apply", json.dumps({"proposal": pid}))
    assert code == 0 and doc["revision"] == "r002" and doc["parent"] == "r000"  # the same proposal, again from r000
    code, doc, _ = run_cli(w, "pin_transition", '{"pin": "s0->s1", "hold": true}')
    assert code == 1 and "no pin s0->s1" in doc["error"]
    renders = ws / "edits" / "renders"
    assert sorted(p.suffix for p in renders.iterdir() if p.is_file()) == [".jpg", ".jpg", ".mp4", ".mp4"]


@pytest.mark.parametrize("value, traced", [("1", True), ("true", True), ("YES", True), ("0", False), ("false", False), ("", False),
                                           ("no", False)])
def test_matchcut_debug_counts_only_when_on(ws, monkeypatch, capsys, value, traced):
    def boom(w, a):
        raise ZeroDivisionError("division by zero")
    monkeypatch.setitem(T.BY_NAME, "log", T.Tool("log", "x", T._obj(), boom))
    monkeypatch.setenv("MATCHCUT_DEBUG", value)
    assert call(str(ws), "log")[0] == 1
    assert ("Traceback" in capsys.readouterr().err) is traced


def test_a_message_with_a_lone_surrogate_is_still_one_json_object(ws, monkeypatch, capfd):
    """Review fix: a lone surrogate in the result (a file name, a message) made the final write raise; it is escaped now."""
    monkeypatch.setitem(T.BY_NAME, "log", T.Tool("log", "x", T._obj(), lambda w, a: {"name": "c\ud800"}))
    assert cli.main([str(ws), "log"]) == 0
    out, err = capfd.readouterr()
    assert json.loads(out) == {"name": "c\ud800"} and out.count("\n") == 1 and "Traceback" not in err

    def refuse(w, a):
        raise S.SessionError("no clip c\udc80")
    monkeypatch.setitem(T.BY_NAME, "log", T.Tool("log", "x", T._obj(), refuse))
    assert cli.main([str(ws), "log"]) == 1
    out, _ = capfd.readouterr()
    assert json.loads(out)["error"] == "no clip c\udc80"


def test_sigterm_during_a_command_is_one_json_error(ws, monkeypatch, capfd):
    import signal

    def killed(w, a):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("not reached")
    monkeypatch.setitem(T.BY_NAME, "log", T.Tool("log", "x", T._obj(), killed))
    before = signal.getsignal(signal.SIGTERM)
    assert cli.main([str(ws), "log"]) == 1
    out, _ = capfd.readouterr()
    assert json.loads(out) == {"error": "terminated", "kind": "error"}
    assert signal.getsignal(signal.SIGTERM) == before


def test_sigterm_mid_render_leaves_no_temporary_file(ws):
    """A real CLI render, terminated while ffmpeg runs: the CLI prints its JSON error and removes its temporary file."""
    import signal
    import time
    e = dict(os.environ, PYTHONPATH=str(REPO))
    cfg = json.loads((ws / "config.json").read_text())
    cfg["render"].update(width=1920, preset="veryslow", crf=0)  # slow enough to be caught in the act
    (ws / "config.json").write_text(json.dumps(cfg))
    proc = subprocess.Popen([sys.executable, "-m", "matchcut.edit", str(ws), "render", '{"quality": "full"}'], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=e, cwd=str(REPO))
    tmp = ws / "edits" / "renders" / ".tmp"
    deadline = time.time() + 60
    while time.time() < deadline and not (tmp.is_dir() and any(tmp.iterdir())):
        time.sleep(0.05)
    assert tmp.is_dir() and any(tmp.iterdir()), "the render never started"
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 1 and json.loads(out) == {"error": "terminated", "kind": "error"}
    assert [p for p in (ws / "edits" / "renders").rglob("*") if p.is_file()] == []
