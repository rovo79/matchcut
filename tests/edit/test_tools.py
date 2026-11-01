"""The tool table (``matchcut.edit.tools``): one entry per command with a name, a description, a JSON Schema and a handler; the built-in
validator; ``call``."""
from __future__ import annotations

import json

import pytest

from matchcut.edit import session as S
from matchcut.edit import tools as T
from matchcut.edit.model import Refused
from matchcut.edit.ops import ARGS, whole

COMMANDS = {"start", "inspect", "log", "propose", "apply", "undo", "redo", "checkout", "compare", "preview", "render", "frames",
            "pin_transition"}


def test_the_table_covers_every_command_once():
    assert [t.name for t in T.TOOLS] == list(T.BY_NAME) and set(T.BY_NAME) == COMMANDS
    assert "publish" not in T.BY_NAME  # M3a task 4 (PUBLISH_HOOK)
    for t in T.TOOLS:
        assert t.description and len(t.description) < 400 and callable(t.handler)
        assert t.schema["type"] == "object" and t.schema["additionalProperties"] is False
        assert set(t.schema["required"]) <= set(t.schema["properties"])
        assert json.loads(json.dumps(t.to_dict())) == {"name": t.name, "description": t.description, "schema": t.schema}


def test_the_operation_schema_names_every_operation_argument():
    op = T.BY_NAME["propose"].schema["properties"]["ops"]["items"]
    assert set(op["properties"]["op"]["enum"]) == set(ARGS)
    assert set(op["properties"]) == {"op"} | {a for args in ARGS.values() for a in args}


@pytest.mark.parametrize("value, ok", [(3, True), (3.0, True), (-2, True), (1.5, False), (True, False), (False, False), ("3", False),
                                       (None, False), ([3], False), (float("nan"), False), (float("inf"), False), (10 ** 400, False),
                                       (10 ** 9, True), (10 ** 9 + 1, False)])
def test_frame_counts_must_be_whole_numbers(value, ok):
    schema = T._int("frames")
    assert (T.validate(value, schema) == []) is ok, T.validate(value, schema)


@pytest.mark.parametrize("value, ok", [("r000", True), ("r001", True), ("r1234", True), ("r1", False), ("r01", False), ("R001", False),
                                       ("r001 ", False), ("../r001", False), ("r001/../x", False), ("r001\n", False), ("r\u0660\u0660\u0661", False), (1, False)])
def test_revision_ids(value, ok):
    assert (T.validate(value, T.REVISION) == []) is ok


@pytest.mark.parametrize("value, ok", [("0123456789ab", True), ("0123456789AB", False), ("0123456789a", False), ("0123456789abc", False),
                                       ("../../etc/pa", False), ("0123456789ab\n", False), (123456789012, False)])
def test_proposal_ids(value, ok):
    assert (T.validate(value, T.PROPOSAL) == []) is ok


@pytest.mark.parametrize("value, ok", [(1, True), ("3", True), ("s1-s3", True), ("s1-s3#2", True), (0, False), (-1, False), (1.5, False),
                                       (True, False), ("s1->s3", False), ("../s1-s3", False), (None, False)])
def test_junctions(value, ok):
    assert (T.validate(value, T.JUNCTION) == []) is ok


def test_validation_messages_name_the_argument():
    schema = T.BY_NAME["propose"].schema
    errors = T.validate({"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": 1.5}, {"op": "jump"}], "guard": 1}, schema)
    assert errors == ["ops[0].frames must be a whole number, not 1.5", "ops[1].op must be one of " + ", ".join(ARGS) + ', not "jump"',
                      "arguments: unknown argument \"guard\" (arguments: ops, guards)"]
    assert T.validate({}, schema) == ["arguments: ops is required"]
    assert T.validate({"ops": []}, schema) == ["ops needs at least 1 item"]
    assert T.validate({"ops": "trim"}, schema) == ['ops must be a list, not "trim"']
    assert T.validate({"ops": [{"op": "trim", "frames": 10 ** 400}]}, schema) == ["ops[0].frames must be at most 1000000000, not ~1e400"]
    assert T.validate({"ops": [{"op": "trim"}], "guards": {"max_duration_change_frames": -1}}, schema) == [
        "guards.max_duration_change_frames must be at least 0, not -1"]
    assert T.validate("x", schema) == ['arguments must be an object, not "x"']


def test_call_checks_the_schema_before_the_handler(make_ws):
    w = make_ws()
    with pytest.raises(T.ArgumentError) as exc:
        T.call(w, "start", {"restart": "yes"})
    assert exc.value.errors == ['restart must be true or false, not "yes"'] and not (w / "edits").exists()
    out = T.call(w, "start")
    assert out["head"] == "r000"
    p = T.call(w, "propose", {"ops": [{"op": "trim", "clip": "c002", "edge": "out", "frames": -5}]})
    assert T.call(w, "apply", {"proposal": p["proposal"]})["revision"] == "r001"
    assert T.call(w, "log")["head"] == "r001"
    assert T.call(w, "inspect", {"revision": "r000"})["session"]["revision"] == "r000"
    with pytest.raises(Refused, match="no revision r005"):
        T.call(w, "checkout", {"revision": "r005"})
    with pytest.raises(KeyError):
        T.call(w, "publish")


def test_whole_reports_a_huge_count_without_overflowing():
    """Task 2 review: a frame count such as 10**400 was formatted as a float and raised OverflowError."""
    for v, shown in ((10 ** 400, "~1e400"), (-10 ** 5000, "~-1e5000"), (2 * 10 ** 9, "2000000000"), (1e300, "1e+300")):
        with pytest.raises(Refused, match=r"frames is out of range: at most 1000000000 frames either way, not " + shown.replace("+", r"\+")):
            whole({"frames": v}, "frames")


def test_a_huge_frame_count_is_refused_by_the_session(make_ws):
    w = make_ws()
    S.start(w)
    with pytest.raises(Refused, match="out of range"):
        S.propose(w, [{"op": "trim", "clip": "c002", "edge": "out", "frames": 10 ** 400}])


def test_the_engine_and_the_review_never_import_the_edit_package():
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    code = ("import sys, matchcut.pipeline.pipeline, matchcut.pipeline.render, matchcut.review.items, matchcut.review.render; "
            "print(sorted(m for m in sys.modules if m.startswith('matchcut.edit')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(repo), check=True).stdout.strip()
    assert out == "[]"
    for path in list((repo / "matchcut" / "pipeline").rglob("*.py")) + list((repo / "matchcut" / "review").rglob("*.py")):
        text = path.read_text()
        assert "matchcut.edit import" not in text and "import matchcut.edit" not in text and "from ..edit" not in text, path
