import json

from matchcut.pipeline.config import PipelineConfig


def test_overrides_and_roundtrip(tmp_path):
    cfg = PipelineConfig(source="a.mp4", workspace=str(tmp_path))
    cfg.apply_overrides(["matchcut.candidate_threshold=0.6", "llm.backend=mock", "passes=[\"matchcut\",\"pacing\"]"])
    assert cfg.matchcut.candidate_threshold == 0.6
    assert cfg.llm.backend == "mock"
    assert tuple(cfg.passes) == ("matchcut", "pacing")
    path = tmp_path / "config.json"
    cfg.save(path)
    loaded = PipelineConfig.load(path)
    assert loaded.matchcut.candidate_threshold == 0.6
    assert loaded.passes == ("matchcut", "pacing")
    assert json.loads(path.read_text())["llm"]["backend"] == "mock"


from matchcut.pipeline.config import DEFAULT_PASSES, MusicConfig
from matchcut.pipeline.cli import build_config, parse_args


def test_music_config_defaults_and_roundtrip(tmp_path):
    cfg = PipelineConfig(source="a.mp4", workspace=str(tmp_path))
    assert isinstance(cfg.music, MusicConfig)
    assert cfg.music.path is None
    assert cfg.music.snap is True
    assert DEFAULT_PASSES[-1] == "beatsync"  # last: nothing may move a cut after it is on the beat
    cfg.apply_overrides(["music.path=track.mp3", "music.start=12.5", "music.gain_db=-10"])
    path = tmp_path / "config.json"
    cfg.save(path)
    loaded = PipelineConfig.load(path)
    assert loaded.music.path == "track.mp3"
    assert loaded.music.start == 12.5
    assert loaded.music.gain_db == -10


def test_cli_music_flags(tmp_path):
    args = parse_args(["run", "clip.mp4", "-w", str(tmp_path), "--backend", "mock", "--music", "song.mp3", "--music-start", "3", "--music-gain", "-12"])
    cfg = build_config(args)
    assert cfg.music.path == "song.mp3"
    assert cfg.music.start == 3.0
    assert cfg.music.gain_db == -12.0
    cfg2 = build_config(parse_args(["run", "clip.mp4", "-w", str(tmp_path), "--backend", "mock"]))
    assert cfg2.music.path is None


def test_keep_config_runs_with_the_workspace_config_as_it_is_and_never_rewrites_it(tmp_path, monkeypatch):
    """``--keep-config``: the edit CLI's re-run command must not change config.json (a plain command turns render.enabled back on, resets
    render.proxy and force, and stores the source and workspace as given)."""
    import pytest

    import matchcut.pipeline.cli as cli

    ws = tmp_path / "ws"
    cfg = PipelineConfig(source="footage.mp4", workspace="ws", brief="keep me")
    cfg.render.enabled, cfg.render.proxy, cfg.force = False, True, True
    cfg.save(ws / "config.json")
    data = (ws / "config.json").read_bytes()
    kept = build_config(parse_args(["refine", "/abs/footage.mp4", "-w", str(ws), "--keep-config"]))
    assert (kept.source, kept.workspace, kept.brief) == ("/abs/footage.mp4", str(ws), "keep me")
    assert (kept.render.enabled, kept.render.proxy, kept.force) == (False, True, True)
    # the default behaviour is unchanged
    plain = build_config(parse_args(["refine", "/abs/footage.mp4", "-w", str(ws), "--config", str(ws / "config.json")]))
    assert (plain.render.enabled, plain.render.proxy, plain.force) == (True, False, False)
    for extra in (["--brief", "x"], ["--no-render"], ["--config", str(ws / "config.json")], ["--set", "seed=3"], ["--force"]):
        with pytest.raises(SystemExit, match="--keep-config uses WORKSPACE/config.json as it is"):
            build_config(parse_args(["refine", "/abs/footage.mp4", "-w", str(ws), "--keep-config", *extra]))
    with pytest.raises(SystemExit, match="--keep-config needs -w WORKSPACE"):
        build_config(parse_args(["refine", "/abs/footage.mp4", "--keep-config"]))
    # main hands Pipeline save_config=False: config.json is never written
    made = {}

    class FakePipeline:
        def __init__(self, cfg, save_config=True):
            made.update(cfg=cfg, save_config=save_config)
            self.ws = ws
            if save_config:
                cfg.save(ws / "config.json")

        def refine(self, start=None):
            from matchcut.pipeline.timeline import Timeline
            return Timeline(source="", fps=25.0, name="x")

        def report(self, final=None):
            return ""

    monkeypatch.setattr(cli, "Pipeline", FakePipeline)
    assert cli.main(["refine", "/abs/footage.mp4", "-w", str(ws), "--keep-config"]) == 0
    assert made["save_config"] is False and (ws / "config.json").read_bytes() == data
    assert cli.main(["refine", "/abs/footage.mp4", "-w", str(ws), "--config", str(ws / "config.json")]) == 0
    assert made["save_config"] is True and (ws / "config.json").read_bytes() != data  # (what --keep-config avoids)
