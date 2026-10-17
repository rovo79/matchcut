"""Snapshot tests of the five views at 120x40 on the mock workspace.

The config is rewritten to relative paths under a temporary working directory,
so the Config view does not print the machine-specific pytest temp path.

Regenerate with: python3 -m pytest tests/tui/test_snapshots.py --snapshot-update
"""
import pytest

from matchcut.pipeline.config import PipelineConfig
from matchcut.tui.app import MatchcutApp


@pytest.mark.parametrize("key,name", [("1", "run"), ("2", "shots"), ("3", "story"), ("4", "cut"), ("5", "config")])
def test_view_snapshots(snap_compare, mock_workspace, tmp_path, monkeypatch, key, name):
    cfg = PipelineConfig.load(mock_workspace / "config.json")
    (tmp_path / "ws").symlink_to(mock_workspace, target_is_directory=True)
    (tmp_path / "demo.mp4").symlink_to(cfg.source)
    cfg.source, cfg.workspace = "demo.mp4", "ws"
    monkeypatch.chdir(tmp_path)

    assert snap_compare(MatchcutApp(cfg), press=[key], terminal_size=(120, 40))
