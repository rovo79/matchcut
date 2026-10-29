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
    assert "**Status: run once against a live Resolve: DaVinci Resolve 21.0.2.4 free, macOS, from the Py3 console (2026-10-29).**" in text
    assert "**M2c human gate: PENDING** for the bridge itself" in text and "## First live run (2026-10-29)" in text
    for needle in ("one machine and one footage file", "Studio, other versions,\nreal camera footage, timecoded media and audio are not verified",
                   "FCPXML 1.10 `Timeline.Export` failed", "0 of 4", "4 of 4", "90000 / 01:00:00:00", "frame 0 / 00:00:00:00",
                   "reports the media's last frame", "no `ResolveMCP` binary file"):
        assert needle in text, needle
    for gate in ("M2a human gate: PENDING", "M2b human gate: PENDING", "M1 human gate: PENDING"):
        assert gate in text, gate
    assert "lossless" not in text.lower() and not MODEL_IDS.search(text) and "File > Close Project" not in text
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
    for needle in ("verifiably empty, whatever its name", "empty bins are fine", "runs the whole check again", "before every call that changes something", "matchcut-probe-<UTC time>", "folder `matchcut-probe`", "open a new, empty project (or quit and reopen Resolve)",
                   "earlier `matchcut-probe-*` project", "type `yes`", "`--yes`", "never asks and never switches", "shared or remote project database",
                   "External scripting using = Local", "Python 3.6 or later", "DaVinci Resolve Studio", "`runs/<UTC time>/`", "`exports/`",
                   "python -m matchcut.resolve probe WORKSPACE", "python -m matchcut.resolve report WORKSPACE WORKSPACE/resolve_probe", "--dry-run",
                   "## Deleting the probe projects", "py -3 WORKSPACE\\resolve_probe\\resolve_probe.py", "found <path>\n  but it failed to import",
                   "eb2b4ebc578655a627baeccdc68b194afb0cd7c96f819acd331125e8d699e5b5", "not in the 18.5 README",
                   "## Free edition: run it from Resolve's console", "https://www.python.org/downloads/macos/", "universal2",
                   "Workspace > Console", "print(resolve.GetVersionString())", "this switches to a new probe project",
                   "did\nnot connect", "UNVERIFIED for\n     Resolve 21.0.2", "`runs/<UTC time>/` folder"):
        assert needle in text, needle
    # the per-OS setup lines are the ones the script prints
    for line in script.SETUP_HELP.splitlines():
        if line.strip().startswith(("export ", "set ")):
            assert line.strip() in text, line


def test_interop_and_readme_point_to_the_probe():
    interop = (DOCS / "INTEROP.md").read_text(encoding="utf-8")
    assert "### Resolve capability probe (M2c-0)" in interop and "[M2C-PROBE.md](M2C-PROBE.md)" in interop
    readme = (DOCS.parent / "README.md").read_text(encoding="utf-8")
    assert "python -m matchcut.resolve probe WORKSPACE" in readme and "docs/M2C-PROBE.md" in readme and "run once against a live Resolve, 21.0.2.4 free" in readme


def test_the_console_line_in_the_doc_is_what_probe_prints():
    from matchcut.resolve.probe import console_lines

    dry, _ = console_lines(Path("/Users/you/…/resolve_probe/resolve_probe.py"))
    assert dry in PROBE.read_text(encoding="utf-8")
