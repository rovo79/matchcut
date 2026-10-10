import json
import subprocess

from matchcut.pipeline.cli import main
from matchcut.pipeline.config import PipelineConfig
from matchcut.pipeline.media import MediaInfo, probe
from matchcut.pipeline.pipeline import Pipeline
from matchcut.pipeline.render import build_filter_graph
from matchcut.pipeline.timeline import Clip, Timeline, Transition


def test_full_run_with_mock_backend(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.render.proxy = True
    pipe = Pipeline(cfg)
    final = pipe.run()
    ws = tmp_path / "ws"
    for name in ["config.json", "probe.json", "analysis.npz", "shots.json", "descriptions.json", "narrative.json", "roughcut.json", "final.json", "final.edl", "final.csv", "report.md", "final_proxy.mp4", "llm_calls.jsonl"]:
        assert (ws / name).exists(), name
    assert len(json.loads((ws / "shots.json").read_text())) == 8
    assert len(list((ws / "passes").glob("*.json"))) == 5
    assert final.clips
    assert abs(final.duration - 8.0) < 8.0 * 0.15 + 0.5
    rendered = probe(str(ws / "final_proxy.mp4"))
    assert abs(rendered.duration - final.duration) < 0.35
    assert rendered.has_audio
    report = (ws / "report.md").read_text()
    assert "## Final cut list" in report and "## Pass decisions" in report
    # second run reuses the cache and does not call the model again
    pipe2 = Pipeline(PipelineConfig.load(ws / "config.json"))
    pipe2.run()
    assert pipe2.llm.calls == 1  # only the polish pass (not cached) hits the backend


def test_cli_subcommands(demo_video, tmp_path):
    ws = str(tmp_path / "cli_ws")
    common = [str(demo_video), "-w", ws, "--backend", "mock", "-t", "6", "--no-render"]
    assert main(["analyze", *common]) == 0
    assert main(["narrative", *common]) == 0
    assert main(["roughcut", *common]) == 0
    assert main(["refine", *common, "--passes", "matchcut,pacing,transitions"]) == 0
    assert main(["report", *common]) == 0
    tl = Timeline.load(tmp_path / "cli_ws" / "final.json")
    assert tl.clips
    assert main(["render", *common, "--proxy", "-o", str(tmp_path / "out.mp4")]) == 0
    assert (tmp_path / "out.mp4").exists()


def test_filter_graph_shapes():
    media = MediaInfo(path="x.mp4", duration=10, width=320, height=180, fps=25, has_audio=True)
    tl = Timeline(source="x.mp4", fps=25, fade_in=0.5, fade_out=0.5)
    tl.clips = [Clip("c001", 0, 0, 2, "b1"), Clip("c002", 1, 3, 5, "b1", transition_in=Transition("dissolve", 0.5)), Clip("c003", 2, 6, 8, "b2", transition_in=Transition("dip_to_black", 0.4))]
    cfg = PipelineConfig().render
    graph = build_filter_graph(tl, media, cfg)
    assert "xfade=transition=fade:duration=0.500:offset=1.500" in graph
    assert "xfade=transition=fadeblack:duration=0.400:offset=3.100" in graph
    assert "acrossfade=d=0.500" in graph
    assert graph.endswith("[aout]") and "[vout]" in graph
    assert "fade=t=out:st=4.600:d=0.500" in graph
