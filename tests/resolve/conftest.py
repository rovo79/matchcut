"""Fixtures for the Resolve probe tests: the finished mock workspace of the interchange tests, a bundle made from it, and a way to run the
generated script against the fake Resolve (``fake_resolve``) through an injected ``DaVinciResolveScript`` module."""
from __future__ import annotations

import builtins
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


def last_run(bundle) -> Optional[Path]:
    """The newest run's resolve_probe.json in the bundle (runs/<UTC time>[-dry-run][-n]/), or None."""
    runs = sorted((bundle.dir / "runs").glob("*/resolve_probe.json"), key=lambda p: p.stat().st_mtime_ns)
    return runs[-1] if runs else None


def run_probe(bundle, monkeypatch, variant: Optional[Variant] = None, args: Sequence[str] = (), world: Optional[World] = None,
              inject: bool = True, catalogue_dir: Optional[Path] = None, answer: Optional[str] = None):
    """Run ``resolve_probe.py`` in this process against the fake: (exit code, the JSON it wrote or None, the fake world).

    ``answer`` is what the person types at the confirmation (None: the run gets ``--yes``; EOFError: stdin is closed; a function: input itself)."""
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
    args = list(args)
    if answer is None:
        args.append("--yes")
    elif callable(answer) and answer is not EOFError:  # a test's own input(): it can act on the fake while the question waits
        monkeypatch.setattr(builtins, "input", answer)
    else:
        def ask(prompt=""):
            world.log.append(("input", prompt))
            if answer is EOFError:
                raise EOFError
            return answer
        monkeypatch.setattr(builtins, "input", ask)
    monkeypatch.setattr(sys, "argv", [str(bundle.script)] + args)
    before = last_run(bundle)
    code = 0
    try:
        runpy.run_path(str(bundle.script), run_name="__main__")
    except SystemExit as exc:
        code = exc.code if exc.code is not None else 0
    out = None
    for i, a in enumerate(args):
        if a == "--out":
            out = Path(args[i + 1])
    if out is None:
        out = last_run(bundle)
        out = out if out != before else None
    doc = json.loads(out.read_text()) if out is not None and out.exists() else None
    return code, doc, world
