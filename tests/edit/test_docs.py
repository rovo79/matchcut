"""The M3a docs: the human gate reads PENDING, no model is named, every CLI command and operation is documented, and the docs that list
the stages name the edit stage."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from matchcut.edit import tools as T
from matchcut.edit.ops import OPS

DOCS = Path(__file__).resolve().parents[2] / "docs"
EDIT, ACCEPTANCE = DOCS / "EDIT.md", DOCS / "M3A-ACCEPTANCE.md"
MODEL_IDS = re.compile(r"claude-(opus|sonnet|haiku)|\b(opus|sonnet|haiku)[- ]?\d", re.I)


@pytest.mark.parametrize("doc", [EDIT, ACCEPTANCE, DOCS / "INTEROP.md", DOCS / "PIPELINE.md"], ids=lambda p: p.name)
def test_no_model_is_named_and_the_edit_stage_is_listed(doc):
    text = doc.read_text(encoding="utf-8")
    if doc != DOCS / "PIPELINE.md":  # (PIPELINE.md documents the pipeline's configurable default model, from before M3a)
        assert not MODEL_IDS.search(text), MODEL_IDS.search(text).group(0)
    assert "--stage edit" in text and "edit.json" in text


def test_the_m3a_human_gate_reads_pending():
    text = ACCEPTANCE.read_text(encoding="utf-8")
    assert re.search(r"M3a human acceptance: PENDING", text) and "## Human checklist: PENDING" in text
    assert "## Verdict" in text and "## Pass / fail" in text


def test_every_command_and_operation_is_documented():
    text = EDIT.read_text(encoding="utf-8")
    for t in T.TOOLS:
        assert f"| `{t.name}` |" in text or f"`{t.name}` /" in text or f"/ `{t.name}`" in text, t.name
    for op in OPS:
        assert f"| `{op}` |" in text, op
    for limitation in ("ignores `hold_transition`", "No persistent timing locks", "context change requires a re-propose"):
        assert limitation in text, limitation
