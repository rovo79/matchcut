"""The allow-list's source of record: the Resolve 21.0.4 scripting README (full text of the community copy).

``readme_21_0_4.json`` (committed) is what ``readme_calls.parse`` extracts from that text.  Every call of the probe script's ``README_CALLS``
is a documented, non-deprecated method of its class there, and ``GATED_CALLS`` are not in it.  When a copy of the text is available
(``RESOLVE_README_21_0_4``, else the research scratch copy when it exists), the JSON is re-derived from it and the script's signatures are
checked against its lines.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from matchcut.resolve import _probe_script as script

from . import readme_calls

SHA = "eb2b4ebc578655a627baeccdc68b194afb0cd7c96f819acd331125e8d699e5b5"


def _readme() -> Path:
    for p in (os.environ.get("RESOLVE_README_21_0_4"),
              "/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/probe-review/readmes/davinci-resolve-scripting-doc-txt.txt"):
        if p and Path(p).is_file():
            return Path(p)
    pytest.skip("no copy of the Resolve 21.0.4 README text (set RESOLVE_README_21_0_4)")


def test_the_allow_list_is_documented_in_the_21_0_4_readme():
    data = readme_calls.load()
    assert data["last_updated"] == "Last Updated: 24 Jul 2026" and data["sha256"] == SHA and data["source"] == readme_calls.SOURCE
    for name in script.README_CALLS:
        cls, method = name.split(".")
        if cls == "DaVinciResolveScript":
            continue  # scriptapp: the README's example code, not its reference
        assert method in data["classes"].get(cls, []), name
        assert method not in data["deprecated"].get(cls, []), name
    for name in script.GATED_CALLS:
        cls, method = name.split(".")
        assert method not in data["classes"].get(cls, []), name
    for name in script.LOOKED_UP:  # recorded from the catalogue only; two of them are in the README, two are not
        assert (name in data["classes"]["TimelineItem"]) == (name in ("GetSourceStartTime", "GetSourceEndTime")), name


def test_the_committed_list_is_what_the_full_readme_text_says():
    path = _readme()
    text = path.read_text(encoding="utf-8")
    assert readme_calls.parse(text) == readme_calls.load()
    # the signatures the script quotes are the README's own (the README wraps AddMarker's customData onto the next line)
    for name, sig in script.README_CALLS.items():
        cls, method = name.split(".")
        if cls == "DaVinciResolveScript":
            assert 'scriptapp("Resolve")' in text
            continue
        head = sig.split(" (")[0].split("-->")[0].strip()
        lines = [ln.strip() for ln in text.splitlines() if re.match(rf"^\s+{method}\(", ln)]
        assert any(ln.startswith(head) or head.startswith(ln.split("-->")[0].strip()) for ln in lines), (name, head, lines[:3])
