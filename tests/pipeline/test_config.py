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
