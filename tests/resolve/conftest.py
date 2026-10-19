"""Fixtures for the Resolve probe tests: the finished mock workspace of the interchange tests, a bundle made from it, and a way to run the
generated script against the fake Resolve (``fake_resolve``) through an injected ``DaVinciResolveScript`` module."""
from __future__ import annotations

import json
import runpy
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Sequence

import pytest

from matchcut.interchange.common import source_info
from matchcut.pipeline.config import PipelineConfig
from matchcut.resolve.probe import build_bundle

from tests.interchange.conftest import footage, world  # noqa: F401 (the session fixtures, shared)

from .fake_resolve import VARIANTS, Variant, World, module


@pytest.fixture
def wsp(world, tmp_path) -> SimpleNamespace:  # noqa: F811
    """A private copy of the finished mock workspace."""
    ws = tmp_path / "ws"
    shutil.copytree(world.ws, ws)
    cfg = PipelineConfig.load(ws / "config.json")
    cfg.workspace = str(ws)
    return SimpleNamespace(ws=ws, cfg=cfg, world=world, tmp=tmp_path)


@pytest.fixture
def bundle(wsp):
    return build_bundle(wsp.ws, wsp.cfg)


def tree(root: Path) -> set:
    return {str(p.relative_to(root)) for p in Path(root).rglob("*")}


def run_probe(bundle, monkeypatch, variant: Optional[Variant] = None, args: Sequence[str] = (), world: Optional[World] = None,
              inject: bool = True, catalogue_dir: Optional[Path] = None):
    """Run ``resolve_probe.py`` in this process against the fake: (exit code, the JSON it wrote or None, the fake world)."""
    expected = json.loads(bundle.expected.read_text())
    if world is None:
        world = World(variant or VARIANTS["keeps everything"], source_info(expected["source"]["path"]))
    cat = catalogue_dir or bundle.dir.parent / "fake-install" / "Scripting"
    world.write_catalogue(cat)
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(cat))
    monkeypatch.delenv("RESOLVE_SCRIPT_LIB", raising=False)
    if inject:
        monkeypatch.setitem(sys.modules, "DaVinciResolveScript", module(world))
    else:
        monkeypatch.delitem(sys.modules, "DaVinciResolveScript", raising=False)
    monkeypatch.setattr(sys, "argv", [str(bundle.script)] + list(args))
    code = 0
    try:
        runpy.run_path(str(bundle.script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
    out = None
    for i, a in enumerate(args):
        if a == "--out":
            out = Path(args[i + 1])
    out = out or bundle.dir / ("resolve_probe_dry_run.json" if "--dry-run" in args else "resolve_probe.json")
    doc = json.loads(out.read_text()) if out.exists() else None
    return code, doc, world
