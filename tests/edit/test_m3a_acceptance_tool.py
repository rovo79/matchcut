"""``tools/m3a_acceptance.py``: the [auto] checks of the M3a acceptance run.  It snapshots the canonical files and media outside the
workspace, verifies them after an edit session (``edit.json``, ``edit_publish.json`` and ``edits/`` are expected to change), reports the
session, the published record and its provenance, and never writes inside the workspace."""
from __future__ import annotations

import hashlib
import os
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple

from matchcut.edit import publish as P
from matchcut.edit import session as S
from matchcut.pipeline.editorial import Pin, PinStore
from matchcut.pipeline.timeline import Timeline, clip_origin

REPO = Path(__file__).resolve().parents[2]
TOOL = REPO / "tools" / "m3a_acceptance.py"


def run(*argv: str) -> Tuple[int, str, str]:
    proc = subprocess.run([sys.executable, str(TOOL), *argv], capture_output=True, text=True, cwd=str(REPO), timeout=120)
    assert "Traceback" not in proc.stderr, proc.stderr
    return proc.returncode, proc.stdout, proc.stderr


def tree(ws: Path) -> Dict[str, Tuple[str, int]]:
    """Every file under ``ws``: (sha256, mtime_ns), so a write of any kind shows."""
    return {p.relative_to(ws).as_posix(): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(ws.rglob("*")) if p.is_file()}


def test_snapshot_verify_and_report_around_an_edit_session_without_writing_in_the_workspace(make_ws, tmp_path):
    w = make_ws()
    tl = Timeline.load(w / "final.json")
    a, b = tl.clips[1], tl.clips[2]
    PinStore(w).put(Pin(a.shot_index, b.shot_index, round(a.source_out, 3), round(b.source_in, 3), "locked", "", clip_origin(a),
                        clip_origin(b)))  # made after the pipeline's last run: pending
    snap = tmp_path / "snap.json"
    before = tree(w)
    code, out, _ = run("snapshot", str(w), "-o", str(snap))
    assert code == 0 and "snapshot of 4 files and 1 media" in out and tree(w) == before
    doc = json.loads(snap.read_text())
    assert sorted(doc["files"]) == ["editorial.json", "final.json", "passes/01_tidy.json", "roughcut.json"]
    assert doc["media"]["source"]["stat"]["size"] > 0

    # an edit session and its publish: only edit.json, edit_publish.json and edits/ change
    S.start(w)
    rid = S.apply(w, S.propose(w, [{"op": "trim", "clip": "c005", "edge": "out", "frames": -3}])["proposal"])["revision"]
    P.publish(w)
    before = tree(w)
    code, out, _ = run("verify", str(w), str(snap))
    assert code == 0 and out.startswith("unchanged: all") and "present now: edit.json, edit_publish.json, edits/" in out
    code, out, _ = run("report", str(w))
    assert code == 0 and f"published: {rid} at " in out and "unchanged: True" in out and "matches the record: True" in out
    assert "unapplied pins at publish (not protected by this edit): 1" in out and "s1->s3 (pending)" in out
    assert "acknowledged mismatches: 0" in out and "the user must watch the original and the revised films" in out
    code, out, _ = run("report", str(w), "--json")
    rep = json.loads(out)
    v = rep["provenance"]
    assert v["revision"] == rid and v["session"] == S.load(w).session_id and v["session_is_current"] and v["base_unchanged"]
    assert v["latest_unchanged"] and v["edit_json_matches_record"] and v["published_copy_matches"]
    assert [e["pin"] for e in rep["unapplied_pins_at_publish"]] == ["s1->s3"] and rep["acknowledged_mismatches"] == []
    assert [r["id"] for r in rep["revisions"]] == ["r000", rid] and rep["edit_stage_stale"] is None
    assert tree(w) == before  # verify and report wrote nothing

    # a canonical file changed: named, exit 1
    (w / "final.json").write_text((w / "final.json").read_text() + " ")
    code, out, _ = run("verify", str(w), str(snap))
    assert code == 1 and "CHANGED: 1 canonical file(s)" in out and "  final.json: changed" in out
    (w / "human.json").write_text("{}")
    code, out, _ = run("verify", str(w), str(snap))
    assert code == 1 and "human.json: new since the snapshot" in out


def test_a_snapshot_is_never_written_inside_the_workspace(make_ws):
    w = make_ws()
    before = tree(w)
    code, _, err = run("snapshot", str(w), "-o", str(w / "snap.json"))
    assert code == 2 and "inside the workspace" in err and tree(w) == before
    code, out, _ = run("snapshot", str(w))
    default = w.resolve().parent / f"{w.name}.m3a-snapshot.json"
    assert code == 0 and str(default) in out and default.exists() and tree(w) == before


# ----------------------------------------------------------------------------------------------- strict mode (the original workspace)
def strict_snapshot(w: Path, snap: Path, *extra: str) -> None:
    code, out, err = run("snapshot", str(w), "--all", "-o", str(snap), *extra)
    assert code == 0 and out.startswith("strict snapshot of "), (out, err)


def test_strict_mode_catches_every_kind_of_change_the_canonical_mode_lets_through(make_ws, tmp_path):
    """The reviewer's probe: a session, a publish, config/probe/shots edits and a new exports/ on the ORIGINAL.  Canonical mode says
    "unchanged" (it checks only the canonical files); strict mode names every one."""
    w = make_ws()
    snap, canon = tmp_path / "strict.json", tmp_path / "canon.json"
    before = tree(w)
    strict_snapshot(w, snap)
    assert run("snapshot", str(w), "-o", str(canon))[0] == 0 and tree(w) == before  # nothing written inside
    code, out, _ = run("verify", str(w), str(snap), "--strict")
    assert code == 0 and out.startswith("unchanged: all ") and "nothing exempt" in out
    S.start(w)
    S.apply(w, S.propose(w, [{"op": "trim", "clip": "c005", "edge": "out", "frames": -3}])["proposal"])
    P.publish(w)
    (w / "shots.json").write_text('{"x": 1}')
    with open(w / "probe.json", "a") as fh:
        fh.write("tamper\n")
    cfg = json.loads((w / "config.json").read_text())
    cfg["brief"] = "tampered"
    (w / "config.json").write_text(json.dumps(cfg))
    (w / "exports").mkdir()
    (w / "exports" / "final.otio").write_text("x")
    (w / "passes" / "01_tidy.json").unlink()
    code, out, _ = run("verify", str(w), str(canon))
    assert code == 1 and "passes/01_tidy.json: missing now" in out and "config.json" not in out  # canonical: only the canonical files
    code, out, _ = run("verify", str(w), str(snap), "--strict")
    assert code == 1 and out.startswith("CHANGED: ")
    for line in ("edit.json: new since the snapshot", "edit_publish.json: new since the snapshot", "edits/: new since the snapshot",
                 "edits/session.json: new since the snapshot", "config.json: changed", "probe.json: changed", "shots.json: changed",
                 "exports/: new since the snapshot", "exports/final.otio: new since the snapshot",
                 "passes/01_tidy.json: removed since the snapshot", ".stage.lock: new since the snapshot"):
        assert f"  {line}" in out, (line, out)
    code, _, err = run("verify", str(w), str(canon), "--strict")
    assert code == 2 and "--strict needs one taken with snapshot --all" in err


def test_strict_mode_content_change_of_the_same_size_and_mtime_alone(make_ws, tmp_path):
    w = make_ws()
    snap = tmp_path / "strict.json"
    strict_snapshot(w, snap)
    path = w / "roughcut.json"
    st = path.stat()
    data = path.read_bytes()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # the modification time alone: a note, never a failure
    code, out, _ = run("verify", str(w), str(snap), "--strict")
    assert code == 0 and "note: roughcut.json: modification time changed, content identical" in out
    path.write_bytes(data.replace(b"c001", b"c00X", 1))  # the same size, another content
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    code, out, _ = run("verify", str(w), str(snap), "--strict")
    assert code == 1 and "roughcut.json: changed (content)" in out


def test_strict_mode_hashes_large_files_quickly_and_says_so(make_ws, tmp_path):
    w = make_ws()
    big = w / "final.mp4"
    big.write_bytes(b"\0" * (3 << 20))
    snap = tmp_path / "strict.json"
    code, out, _ = run("snapshot", str(w), "--all", "-o", str(snap), "--quick-above", "1")
    assert code == 0 and "1 file(s) above 1 MiB hashed quickly (size, first and last MiB" in out and "final.mp4" in out
    assert json.loads(snap.read_text())["tree"]["final.mp4"]["hash"] == "quick"
    with open(big, "r+b") as fh:  # the middle changes, size and both ends do not: only the modification time tells
        fh.seek(3 << 19)
        fh.write(b"x")
    code, out, _ = run("verify", str(w), str(snap), "--strict")
    assert code == 1 and "final.mp4: possibly changed" in out and "1 large file(s) compared by size and first and last MiB only" in out
    with open(big, "r+b") as fh:
        fh.write(b"y")  # the first MiB: a change
    code, out, _ = run("verify", str(w), str(snap), "--strict")
    assert code == 1 and "final.mp4: changed (content)" in out
    code, out, _ = run("snapshot", str(w), "--all", "--full", "-o", str(snap))
    assert code == 0 and "hashed quickly" not in out and json.loads(snap.read_text())["tree"]["final.mp4"]["hash"] == "sha256"


def test_a_missing_source_is_an_error_never_a_silent_pass(make_ws, tmp_path):
    w = make_ws()
    cfg = json.loads((w / "config.json").read_text())
    snap = tmp_path / "snap.json"
    assert run("snapshot", str(w), "-o", str(snap))[0] == 0
    cfg["source"] = "footage.mp4"  # relative, as `run footage.mp4` stores it, and not found from the repository root
    (w / "config.json").write_text(json.dumps(cfg))
    for argv in (("snapshot", str(w), "-o", str(tmp_path / "x.json")), ("snapshot", str(w), "--all", "-o", str(tmp_path / "y.json"))):
        code, out, err = run(*argv)
        assert code == 1 and "error: no snapshot written: the source footage 'footage.mp4' that config.json names is not found" in err
        assert "set an absolute source in the COPY's config.json" in err and not Path(argv[-1]).exists()
    code, out, err = run("source", str(w))
    assert code == 1 and "'footage.mp4'" in err and out == ""
    code, out, _ = run("verify", str(w), str(snap))  # the snapshot named the source; it is not found now
    assert code == 1 and "now: the source footage 'footage.mp4'" in out
    # an old snapshot that recorded a missing source can never pass
    doc = json.loads(snap.read_text())
    doc["media"]["source"]["stat"] = None
    snap.write_text(json.dumps(doc))
    code, out, _ = run("verify", str(w), str(snap))
    assert code == 1 and "was missing when the snapshot was taken" in out


def test_source_resolves_a_relative_path_from_the_directory_the_run_was_made_in(make_ws, tmp_path):
    w = make_ws()
    cfg = json.loads((w / "config.json").read_text())
    real = Path(cfg["source"])
    os.link(real, w.parent / "footage.mp4")
    cfg["source"], cfg["workspace"] = "footage.mp4", w.name
    (w / "config.json").write_text(json.dumps(cfg))
    code, out, _ = run("source", str(w))  # run from the repository root
    assert code == 0 and out.strip() == str((w.parent / "footage.mp4").resolve())
