"""Editorial interoperability: matchcut timelines <-> OpenTimelineIO, FCPXML 1.10 and CMX3600 EDL, with the fidelity report.

Standard library plus ``matchcut.pipeline`` only.  The engine never imports this package; the pipeline CLI imports it
lazily.  The canonical stage timelines are only ever read here, never rewritten.
"""
from __future__ import annotations


class InterchangeError(RuntimeError):
    """A one-line, user-facing failure of an export, an import or a validation."""


try:  # the installed distribution's version when there is one; a source checkout has none
    from importlib.metadata import version as _version

    MATCHCUT_VERSION = _version("matchcut")
except Exception:  # pragma: no cover - depends on how matchcut is installed
    MATCHCUT_VERSION = "dev"

from .common import LaidClip, SourceInfo, clip_key, export_timeline, junction_id, junction_ids, layout, rt, source_info, to_frames  # noqa: E402
from .edl import export_edl, import_edl  # noqa: E402
from .fcpxml import export_fcpxml, fcpxml_junction_ids, fcpxml_stages, import_fcpxml  # noqa: E402
from .fidelity import FidelityResult, check, write_report  # noqa: E402
from .otio import export_otio, import_otio  # noqa: E402
from .provenance import write_provenance  # noqa: E402

__all__ = [
    "InterchangeError", "MATCHCUT_VERSION", "LaidClip", "SourceInfo", "clip_key", "export_timeline", "junction_id", "junction_ids",
    "layout", "rt", "source_info", "to_frames", "export_otio", "import_otio", "export_fcpxml", "import_fcpxml", "fcpxml_junction_ids", "fcpxml_stages",
    "write_provenance", "export_edl", "import_edl", "FidelityResult", "check", "write_report",
]
