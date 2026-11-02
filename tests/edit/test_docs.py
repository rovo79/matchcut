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


def test_the_two_m3a_gates_read_pending_each_with_its_own_results():
    """Two acceptance outcomes, reported independently: A (core editorial) and B (editor interoperability), each PENDING with its own
    pass/fail and results table; an editor failure is B's, never proof against A; a human gate never passes on automated checks."""
    text = ACCEPTANCE.read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "**A. M3a Core Editorial Acceptance: PENDING.**" in text and "**B. M3a Editor Interoperability Acceptance: PENDING.**" in text
    assert text.count("**Status: PENDING.**") == 2
    for gate in ("A", "B"):
        assert f"### {gate}: pass / fail" in text and f"### {gate}: results" in text and f"**Decision ({gate})**" in text
    assert "is recorded against B, as an editor or format issue. It is not proof that A (core editing) fails." in flat
    assert "Never mark a human gate passed on automated tests or ffprobe results." in flat
    assert "never pass `acknowledge_mismatches` or `supersede_human` to `publish` without the user's explicit approval" in flat
    assert "## Which checks run without the user / which need the user" in text
    # every numbered step is marked [auto] or [human]
    steps = re.findall(r"^(S\d+|A\d+|B\d+)\. (.*)$", text, re.M)
    assert len(steps) >= 20 and all("[auto]" in line or "[human]" in line for _, line in steps), [s for s in steps if "[" not in s[1]]
    assert "cp -cR" in text and "ditto" in text and "tools/m3a_acceptance.py snapshot" in text


@pytest.mark.parametrize("doc", [EDIT, ACCEPTANCE], ids=lambda p: p.name)
def test_the_docs_say_an_agent_never_acknowledges_or_supersedes_without_the_user(doc):
    flat = " ".join(doc.read_text(encoding="utf-8").split())
    assert "acknowledge_mismatches" in flat and "supersede_human" in flat
    assert re.search(r"(must not|never) pass (either|`acknowledge_mismatches` or `supersede_human`)", flat)
    assert "explicit approval" in flat


def test_edit_md_states_the_decided_behaviour():
    flat = " ".join(EDIT.read_text(encoding="utf-8").split())
    for decided in ("Superseding a human stage needs an explicit override bound to the file", "No human file is ever deleted or silently "
                    "replaced", "Only a session based on the latest canonical stage may publish", "A second `start` needs an explicit "
                    "`restart`", "A no-op proposal is refused, saying why", "A changed context needs a fresh proposal"):
        assert decided in flat, decided


def test_every_command_and_operation_is_documented():
    text = EDIT.read_text(encoding="utf-8")
    for t in T.TOOLS:
        assert f"| `{t.name}` |" in text or f"`{t.name}` /" in text or f"/ `{t.name}`" in text, t.name
    for op in OPS:
        assert f"| `{op}` |" in text, op
    for limitation in ("ignores `hold_transition`", "No persistent timing locks", "context change requires a re-propose"):
        assert limitation in text, limitation
