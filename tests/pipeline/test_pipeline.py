import json
import subprocess

import numpy as np

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
    assert len(list((ws / "passes").glob("*.json"))) == 6
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


from matchcut.pipeline.llm import MockBackend


class RecordingMock(MockBackend):
    """Mock backend that keeps the prompt text of every call."""

    def __init__(self, cfg, log=None):
        super().__init__(cfg, log)
        self.texts = {}

    def structured(self, request):
        self.texts.setdefault(request.name, []).append(request.text)
        return super().structured(request)


def test_music_is_analysed_once_and_reaches_the_prompts(demo_video, click_track, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"), target_duration=8.0)
    cfg.llm.backend = "mock"
    cfg.music.path = str(click_track)
    cfg.music.start = 2.0
    pipe = Pipeline(cfg)
    pipe.llm = RecordingMock(cfg.llm)
    ctx = pipe.narrative()
    assert ctx.music is not None
    assert abs(ctx.music.tempo_bpm - 120.0) <= 10.0
    assert (tmp_path / "ws" / "music.json").exists()
    narrative_prompt = pipe.llm.texts["narrative"][0]
    assert "tempo_bpm" in narrative_prompt and "energy_curve" in narrative_prompt
    assert "starts 2.0s into the track" in narrative_prompt
    pipe.rough_cut()
    assert "tempo_bpm" in pipe.llm.texts["rough_cut"][0]
    # a second pipeline on the same workspace loads the cached analysis instead of recomputing
    mtime = (tmp_path / "ws" / "music.json").stat().st_mtime
    pipe2 = Pipeline(PipelineConfig.load(tmp_path / "ws" / "config.json"))
    assert pipe2.ingest().music is not None
    assert (tmp_path / "ws" / "music.json").stat().st_mtime == mtime


def test_no_music_means_no_music_text(demo_video, tmp_path):
    cfg = PipelineConfig(source=str(demo_video), workspace=str(tmp_path / "ws"))
    cfg.llm.backend = "mock"
    pipe = Pipeline(cfg)
    pipe.llm = RecordingMock(cfg.llm)
    ctx = pipe.narrative()
    assert ctx.music is None
    assert "Music track: none" in pipe.llm.texts["narrative"][0]


from matchcut.pipeline.audio import decode_audio
from matchcut.pipeline.config import MusicConfig
from matchcut.pipeline.render import render


def _three_clip_timeline():
    tl = Timeline(source="x.mp4", fps=25, fade_in=0.5, fade_out=0.5)
    tl.clips = [Clip("c001", 0, 0, 2, "b1"), Clip("c002", 1, 3, 5, "b1", transition_in=Transition("dissolve", 0.5)), Clip("c003", 2, 6, 8, "b2")]
    return tl


def test_filter_graph_with_music_ducks_mixes_and_normalises():
    media = MediaInfo(path="x.mp4", duration=10, width=320, height=180, fps=25, has_audio=True)
    music = MusicConfig(path="song.mp3", start=4.0, gain_db=-9.0, fade_out=2.0)
    graph = build_filter_graph(_three_clip_timeline(), media, PipelineConfig().render, music=music)
    assert "[1:a]atrim=start=4.000:end=9.500" in graph  # 5.5 s timeline, offset 4 s
    assert "afade=t=out:st=3.500:d=2.000" in graph
    assert "volume=-9.0dB[music]" in graph
    assert "[asrc]asplit[asrc0][asrc1]" in graph
    assert "[music][asrc1]sidechaincompress=threshold=0.05:ratio=6.0" in graph
    assert "[asrc0][ducked]amix=inputs=2:duration=first:normalize=0[amixed]" in graph
    assert "[amixed]loudnorm=I=-16.0:TP=-1.5:LRA=11[aout]" in graph
    assert "[aout]" in graph and graph.count("[aout]") == 1


def test_filter_graph_music_only_and_no_duck():
    media = MediaInfo(path="x.mp4", duration=10, width=320, height=180, fps=25, has_audio=True)
    only = build_filter_graph(_three_clip_timeline(), media, PipelineConfig().render, music=MusicConfig(path="s.mp3", only=True))
    assert "[0:a]" not in only and "[music]anull[amixed]" in only
    no_duck = build_filter_graph(_three_clip_timeline(), media, PipelineConfig().render, music=MusicConfig(path="s.mp3", duck=False, loudnorm=False))
    assert "sidechaincompress" not in no_duck
    assert "[asrc][music]amix=inputs=2:duration=first:normalize=0[amixed]" in no_duck
    assert "[amixed]anull[aout]" in no_duck
    silent = MediaInfo(path="x.mp4", duration=10, width=320, height=180, fps=25, has_audio=False)
    graph = build_filter_graph(_three_clip_timeline(), silent, PipelineConfig().render, music=MusicConfig(path="s.mp3"))
    assert "[0:a]" not in graph and "[music]anull[amixed]" in graph and "[aout]" in graph


def test_render_with_music_track(demo_video, click_track, tmp_path):
    media = probe(str(demo_video))
    tl = Timeline(source=str(demo_video), fps=media.fps, fade_in=0.5, fade_out=0.5)
    tl.clips = [Clip("c001", 0, 0.0, 2.0, "b1"), Clip("c002", 1, 2.0, 4.0, "b1"), Clip("c003", 3, 6.5, 8.5, "b2")]
    out = render(tl, media, PipelineConfig().render, tmp_path / "with_music.mp4", music=MusicConfig(path=str(click_track), start=1.0))
    info = probe(str(out))
    assert info.has_audio
    assert abs(info.duration - 6.0) < 0.35
    # a music-only render must carry the click track's sparse ticks, not the source's steady sine:
    # with ticks of 50 ms every 500 ms, the loudest 50 ms window is far above the median window
    only = render(
        tl,
        media,
        PipelineConfig().render,
        tmp_path / "music_only.mp4",
        music=MusicConfig(path=str(click_track), start=1.0, only=True, loudnorm=False, duck=False, fade_out=0.5),
    )
    y = decode_audio(str(only), 22050)
    win = 22050 // 20  # 50 ms
    core = y[22050 : 22050 * 5]  # 4 s from the middle, clear of fades
    rms = np.sqrt(np.mean(core[: len(core) // win * win].reshape(-1, win) ** 2, axis=1))
    assert float(rms.max()) > 5.0 * float(np.median(rms)), (float(rms.max()), float(np.median(rms)))
