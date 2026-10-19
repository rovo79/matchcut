"""The interoperability docs agree with the code: the fidelity tables are the KNOWN table, no format is called lossless, and the human gates read PENDING."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from matchcut.interchange.fidelity import KNOWN

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


def test_the_example_exports_carry_no_machine_specific_paths():
    for path in (DOCS / "examples" / "m2a" / "synthetic").iterdir():
        text = path.read_text(encoding="utf-8")
        assert "scratchpad" not in text and "/tmp/" not in text and "/root/" not in text, path.name


def test_the_acceptance_doc_keeps_every_human_gate_pending():
    text = ACCEPTANCE.read_text(encoding="utf-8")
    for gate in ("M1", "M2b", "M2a"):
        assert re.search(rf"{gate}[^\n]*PENDING", text) or re.search(rf"PENDING[^\n]*{gate}", text), gate
    lowered = text.lower()
    for phrase in ("gate passed", "gate: pass", "human checklist: pass", "human acceptance: pass", "has been accepted", "was accepted"):
        assert phrase not in lowered, phrase
