"""``tools/m3a_acceptance.py``: the [auto] checks of the M3a acceptance run.  It snapshots the canonical files and media outside the
workspace, verifies them after an edit session (``edit.json``, ``edit_publish.json`` and ``edits/`` are expected to change), reports the
session, the published record and its provenance, and never writes inside the workspace."""
from __future__ import annotations

import hashlib
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
