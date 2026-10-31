"""The Resolve 21.0.4 scripting README as an allow-list: the method names it documents, per class.

``readme_21_0_4.json`` (committed) holds what ``parse`` extracts from the full text of the community copy
https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8 ("Last Updated: 24 Jul 2026", sha256 below), which is not committed:
the main "DaVinci Resolve API" reference (every class section up to "List and Dict Data Structures") and, separately, the
"Deprecated Resolve API Functions".  ``python -m tests.resolve.readme_calls README.txt`` regenerates the JSON from a copy of the text;
``test_readme_calls`` re-parses the text when a copy is available (``RESOLVE_README_21_0_4``) and checks it against the JSON.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

JSON = Path(__file__).with_name("readme_21_0_4.json")
SOURCE = "https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8"
_CLASS = re.compile(r"^([A-Z][A-Za-z]+)\s*$")
_METHOD = re.compile(r"^\s+(\w+)\(")


def _section(lines: List[str], start: str, stop: str) -> Dict[str, List[str]]:
    out: Dict[str, set] = {}
    inside, cls = False, None
    for ln in lines:
        if ln.strip() == start:
            inside = True
            continue
        if inside and ln.strip() == stop:
            break
        if not inside:
            continue
        m = _CLASS.match(ln)
        if m:
            cls = m.group(1)
            continue
        m = _METHOD.match(ln)
        if m and cls:
            out.setdefault(cls, set()).add(m.group(1))
    return {k: sorted(v) for k, v in sorted(out.items())}


def parse(text: str) -> Dict[str, object]:
    lines = text.splitlines()
    return {"source": SOURCE, "last_updated": lines[0].strip(), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "classes": _section(lines, "DaVinci Resolve API", "List and Dict Data Structures"),
            "deprecated": _section(lines, "Deprecated Resolve API Functions", "Unsupported Resolve API Functions")}


def load() -> Dict[str, object]:
    return json.loads(JSON.read_text(encoding="utf-8"))


if __name__ == "__main__":
    JSON.write_text(json.dumps(parse(Path(sys.argv[1]).read_text(encoding="utf-8")), indent=1) + "\n", encoding="utf-8")
