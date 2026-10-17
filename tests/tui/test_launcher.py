"""The launcher: argument parsing, config building, import guard, exit status."""
from __future__ import annotations

import builtins
import sys

import pytest

from matchcut.tui import __main__ as launcher
from matchcut.tui.__main__ import INSTALL_HINT, build_config, main, parse_args


def test_parse_args_defaults():
    a = parse_args(["clip.mp4"])
    assert a.source == "clip.mp4"
    assert a.workspace is None and a.brief == "" and a.target_duration is None
    assert a.backend is None and a.model is None and a.music is None and a.music_start is None
    assert a.config is None and a.set == []
    assert parse_args([]).source is None


def test_parse_args_options():
    a = parse_args(["clip.mp4", "-w", "out", "-b", "moody", "-t", "12.5", "--backend", "mock", "--music", "m.mp3", "--music-start", "3", "--model", "x", "--set", "a=1", "--set", "b=2"])
    assert (a.workspace, a.brief, a.target_duration, a.backend, a.music, a.music_start, a.model) == ("out", "moody", 12.5, "mock", "m.mp3", 3.0, "x")
    assert a.set == ["a=1", "b=2"]
    with pytest.raises(SystemExit):
        parse_args(["clip.mp4", "--backend", "nope"])


def test_build_config_applies_flags_and_set_overrides(tmp_path):
    a = parse_args(["clip.mp4", "-w", str(tmp_path), "-b", "moody", "-t", "9", "--backend", "mock", "--music", "m.mp3", "--set", "render.enabled=false", "--set", "seed=7"])
    cfg = build_config(a)
    assert cfg.source == "clip.mp4" and cfg.workspace == str(tmp_path)
    assert cfg.brief == "moody" and cfg.target_duration == 9.0
    assert cfg.llm.backend == "mock" and cfg.music.path == "m.mp3"
    assert cfg.render.enabled is False and cfg.seed == 7


def test_build_config_needs_a_source():
    with pytest.raises(SystemExit):
        build_config(parse_args([]))


def _stub_run(monkeypatch, seen=None, return_code=None):
    from matchcut.tui.app import MatchcutApp

    def fake_run(self, *a, **k):
        if seen is not None:
            seen.append("textual_image.widget" in sys.modules)
        self._exit_return_code = return_code  # what Textual records when the app exits
        return None

    monkeypatch.setattr(MatchcutApp, "run", fake_run)


def test_textual_image_is_imported_before_the_app_runs(demo_video, tmp_path, monkeypatch):
    import textual_image

    original = sys.modules["textual_image.widget"] if "textual_image.widget" in sys.modules else None
    monkeypatch.delitem(sys.modules, "textual_image.widget", raising=False)  # force main() to import it itself
    if original is not None:
        monkeypatch.setattr(textual_image, "widget", original, raising=False)
    seen = []
    _stub_run(monkeypatch, seen)
    assert main([str(demo_video), "-w", str(tmp_path), "--backend", "mock"]) == 0
    assert seen == [True]


def test_main_returns_the_apps_exit_status(demo_video, tmp_path, monkeypatch):
    from matchcut.tui.app import MatchcutApp

    _stub_run(monkeypatch)
    monkeypatch.setattr(MatchcutApp, "return_code", property(lambda self: 3))
    assert main([str(demo_video), "-w", str(tmp_path), "--backend", "mock"]) == 3


def _fail_import(monkeypatch, missing):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "textual_image.widget":
            raise ModuleNotFoundError(f"No module named {missing!r}", name=missing)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


@pytest.mark.parametrize("missing", ["textual", "textual_image", "textual.app", "textual_image.widget"])
def test_missing_textual_prints_the_install_hint(demo_video, tmp_path, monkeypatch, capsys, missing):
    _fail_import(monkeypatch, missing)
    assert main([str(demo_video), "-w", str(tmp_path), "--backend", "mock"]) == 1
    assert INSTALL_HINT in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["PIL", "textualish", "numpy"])
def test_other_missing_modules_propagate(demo_video, tmp_path, monkeypatch, missing):
    _fail_import(monkeypatch, missing)
    with pytest.raises(ModuleNotFoundError):
        main([str(demo_video), "-w", str(tmp_path), "--backend", "mock"])


def test_install_hint_names_the_requirements_file():
    assert "requirements-tui.txt" in launcher.INSTALL_HINT
