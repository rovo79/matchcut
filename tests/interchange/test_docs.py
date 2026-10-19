"""The interoperability docs agree with the code: the fidelity tables are the KNOWN table, no format is called lossless, and the human gates read PENDING."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from matchcut.interchange.fidelity import KNOWN
from matchcut.interchange.workspace import export_workspace

DOCS = Path(__file__).resolve().parents[2] / "docs"
INTEROP = DOCS / "INTEROP.md"
ACCEPTANCE = DOCS / "M2A-ACCEPTANCE.md"
FIDELITY_DOCS = (INTEROP, ACCEPTANCE)
NEW_DOCS = (INTEROP, ACCEPTANCE, DOCS / "examples" / "m2a" / "synthetic" / "README.md", DOCS / "examples" / "m2a" / "simulate_editor.py")
MODEL_IDS = re.compile(r"claude-(opus|sonnet|haiku)|\b(opus|sonnet|haiku)[- ]?\d", re.I)


@pytest.mark.parametrize("doc", FIDELITY_DOCS, ids=lambda p: p.name)
def test_the_fidelity_tables_list_every_known_entry_verbatim(doc):
    text = doc.read_text(encoding="utf-8")
    missing = [f"{fmt}/{kind}: {entry}" for fmt, by_kind in KNOWN.items() for kind, entries in by_kind.items() for entry in entries if f"- {entry}" not in text]
    assert not missing, missing


@pytest.mark.parametrize("doc", NEW_DOCS, ids=lambda p: p.name)
def test_no_format_is_called_lossless_and_no_model_is_named(doc):
    text = doc.read_text(encoding="utf-8")
    assert "lossless" not in text.lower()
    assert not MODEL_IDS.search(text), MODEL_IDS.search(text).group(0)


EXAMPLES = DOCS / "examples" / "m2a" / "synthetic"
PLACEHOLDER = "/ABSOLUTE/PATH/TO"
_PATH = re.compile(r"(?:^|[\s\"'=(>]|file://)(/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]*)+)", re.M)
_INSTALL_PATHS = ("/Library/Application", "/opt/resolve/Developer/Scripting")  # where Resolve keeps its scripting modules (in open_in_resolve.py)


def example_files():
    return sorted(p for p in EXAMPLES.iterdir() if p.is_file() and p.suffix in (".json", ".otio", ".fcpxml", ".edl", ".md", ".py"))


def test_the_example_exports_carry_no_absolute_path_but_the_placeholder():
    assert len(example_files()) == 7
    for path in example_files():
        for found in _PATH.findall(path.read_text(encoding="utf-8")):
            assert found.startswith((PLACEHOLDER,) + _INSTALL_PATHS), (path.name, found)


def test_the_path_check_catches_home_and_tmp_paths():
    assert _PATH.findall('"source": "/home/someone/demo.mp4", url file:///tmp/x/y.otio and /root/a/b') == ["/home/someone/demo.mp4", "/tmp/x/y.otio", "/root/a/b"]
    assert _PATH.findall('TIMELINE_FILE = \'/ABSOLUTE/PATH/TO/exports/final.otio\' 106/25s AA/V') == ["/ABSOLUTE/PATH/TO/exports/final.otio"]


def _normalise(text: str, footage: Path, exports: Path) -> str:
    """What differs legitimately between the committed examples and a fresh run: where the footage and the exports directory are, the
    footage's modification time, the uids and hashes derived from paths and file contents, and the installed matchcut version."""
    text = text.replace(str(exports), PLACEHOLDER + "/exports").replace(str(footage.parent), PLACEHOLDER)
    text = text.replace(footage.parent.as_uri(), "file://" + PLACEHOLDER)
    text = re.sub(r'"mtime_ns": \d+', '"mtime_ns": 0', text)
    text = re.sub(r'(com\.matchcut\.source_mtime_ns" value=)"\d+"', r'\1"0"', text)
    text = re.sub(r'"canonical_sha256": "[0-9a-f]*"', '"canonical_sha256": "-"', text)
    text = re.sub(r'(uid="com\.matchcut\.source\.)[0-9a-f]+"', r'\1-"', text)
    text = re.sub(r'("matchcut_version": )"[^"]*"', r'\1"-"', text)
    return re.sub(r'(com\.matchcut\.matchcut_version" value=)"[^"]*"', r'\1"-"', text)


def test_the_committed_examples_are_what_the_code_writes_now(wsx):
    """Regenerates the examples on the fixture workspace and compares them with docs/examples/m2a/synthetic/ after normalising the paths,
    the footage's mtime, the path-derived uids, the stage-file hashes and the matchcut version (see ``_normalise``)."""
    export_workspace(wsx.ws, wsx.cfg, resolve_script=True)
    out = wsx.ws / "exports"
    footage = Path(wsx.world.video)
    for name in ("final.otio", "matchcut.fcpxml", "final.edl", "fidelity.md", "provenance.json", "open_in_resolve.py"):
        fresh = _normalise((out / name).read_text(encoding="utf-8"), footage, out)
        committed = _normalise((EXAMPLES / name).read_text(encoding="utf-8"), Path(PLACEHOLDER) / "x.mp4", Path(PLACEHOLDER) / "exports")
        assert fresh == committed, f"{name} differs from the code's output: regenerate docs/examples/m2a/synthetic/ (see its README)"


def test_the_acceptance_doc_keeps_every_human_gate_pending():
    text = ACCEPTANCE.read_text(encoding="utf-8")
    for gate in ("M1", "M2b", "M2a"):
        assert re.search(rf"{gate}[^\n]*PENDING", text) or re.search(rf"PENDING[^\n]*{gate}", text), gate
    lowered = text.lower()
    for phrase in ("gate passed", "gate: pass", "human checklist: pass", "human acceptance: pass", "has been accepted", "was accepted"):
        assert phrase not in lowered, phrase
