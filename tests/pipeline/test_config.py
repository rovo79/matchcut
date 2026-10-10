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
