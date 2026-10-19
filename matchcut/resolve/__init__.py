"""DaVinci Resolve Studio: the M2c-0 capability probe (``python -m matchcut.resolve probe|report``).

``probe`` writes a bundle: the stage exported by M2a (OTIO, FCPXML 1.10, EDL), ``expected.json`` (the canonical cut in frames) and
``resolve_probe.py``, a self-contained script (standard library plus Resolve's own ``DaVinciResolveScript``) that you run once with Resolve
Studio open.  ``report`` reads the ``resolve_probe.json`` it writes, offline, and says which routes to a Resolve timeline work on that
install.  Standard library plus ``matchcut`` only: it never imports the TUI or the review player, and the engine never imports this package.
Not run against a live Resolve by matchcut's authors: the tests use a fake of the documented API.
"""
from __future__ import annotations


class ProbeError(RuntimeError):
    """A one-line, user-facing failure of ``probe`` or ``report``."""
