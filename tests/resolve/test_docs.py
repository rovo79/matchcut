"""The probe's docs agree with the code: the allow-list, the commands, the safety rules, the status and the human gates."""
from __future__ import annotations

import re
from pathlib import Path

from matchcut.resolve import _probe_script as script

DOCS = Path(__file__).resolve().parents[2] / "docs"
PROBE = DOCS / "M2C-PROBE.md"
MODEL_IDS = re.compile(r"claude-(opus|sonnet|haiku)|\b(opus|sonnet|haiku)[- ]?\d", re.I)


def test_the_probe_doc_says_it_is_not_live_and_the_gates_are_pending():
    text = PROBE.read_text(encoding="utf-8")
    assert text.count("not run against a live Resolve") >= 1 and "**M2c human gate: PENDING.**" in text
    for gate in ("M2a human gate: PENDING", "M2b human gate: PENDING", "M1 human gate: PENDING"):
        assert gate in text, gate
    assert "lossless" not in text.lower() and not MODEL_IDS.search(text)
    assert "compatible with Resolve" not in text and "works in Resolve" not in text


def test_the_probe_doc_lists_every_call_the_script_can_make():
    text = PROBE.read_text(encoding="utf-8")
    rows = {ln.split("|")[1].strip(): ln for ln in text.splitlines() if ln.startswith("| ") and ln.count("|") >= 3}
    for name in script.README_CALLS:
        cls, method = name.split(".")
        assert cls in rows and re.search(rf"\b{method}\b", rows[cls]), name
    for name in script.GATED_CALLS:
        assert name in rows, name
    listed = {m for cls, ln in rows.items() if cls in {n.split(".")[0] for n in script.README_CALLS} for m in re.findall(r"\b([A-Z][A-Za-z]+)\b", ln.split("|")[2])}
    assert listed - {"README"} <= {n.split(".")[1] for n in script.README_CALLS} | {"Resolve"}, listed


def test_the_probe_doc_names_the_safety_rules_the_commands_and_what_to_send():
    text = PROBE.read_text(encoding="utf-8")
    for needle in ("Untitled Project", "matchcut-probe-<UTC time>", "folder `matchcut-probe`", "save and close your project first",
                   "External scripting using = Local", "Python 3.6 or later", "DaVinci Resolve Studio", "`resolve_probe.json`", "`exports/`",
                   "python -m matchcut.resolve probe WORKSPACE", "python -m matchcut.resolve report WORKSPACE", "--dry-run",
                   "## Deleting the probe projects", "py -3 WORKSPACE\\resolve_probe\\resolve_probe.py"):
        assert needle in text, needle
    # the per-OS setup lines are the ones the script prints
    for line in script.SETUP_HELP.splitlines():
        if line.strip().startswith(("export ", "set ")):
            assert line.strip() in text, line


def test_interop_and_readme_point_to_the_probe():
    interop = (DOCS / "INTEROP.md").read_text(encoding="utf-8")
    assert "### Resolve capability probe (M2c-0)" in interop and "[M2C-PROBE.md](M2C-PROBE.md)" in interop
    readme = (DOCS.parent / "README.md").read_text(encoding="utf-8")
    assert "python -m matchcut.resolve probe WORKSPACE" in readme and "docs/M2C-PROBE.md" in readme and "not run against a live Resolve" in readme
