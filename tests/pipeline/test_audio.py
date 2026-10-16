import numpy as np

from matchcut.pipeline.audio import MusicAnalysis, analyse_music, decode_audio
from matchcut.pipeline.config import MusicConfig


def test_decode_audio_returns_mono_float32(click_track):
    y = decode_audio(str(click_track), 22050)
    assert y.dtype == np.float32
    assert abs(len(y) / 22050 - 40.0) < 0.1
    assert 0.3 < float(np.abs(y).max()) <= 1.0
    tail = decode_audio(str(click_track), 22050, start=35.0)
    assert abs(len(tail) / 22050 - 5.0) < 0.1


def test_analyse_music_finds_tempo_beats_and_downbeats(click_track):
    m = analyse_music(str(click_track), MusicConfig())
    assert abs(m.tempo_bpm - 120.0) <= 120.0 * 0.08
    assert len(m.beats) >= 70  # 40 s at 120 BPM = 80 beats, allow for trimmed ends
    gaps = np.diff(m.beats)
    assert abs(float(np.median(gaps)) - 0.5) < 0.03
    # beats sit on the half-second grid of the click track
    offsets = np.abs(m.beats - np.round(m.beats * 2) / 2)
    assert float(np.median(offsets)) < 0.06
    assert m.beats_per_bar == 4
    assert abs(float(np.median(np.diff(m.downbeats))) - 2.0) < 0.1
    assert abs(m.duration - 40.0) < 0.1
    assert abs(len(m.energy) - int(40.0 / m.hop_seconds)) <= 1
    assert 0.99 <= float(m.energy.max()) <= 1.0


def test_nearest_beat_energy_and_summary(click_track):
    m = analyse_music(str(click_track), MusicConfig())
    b = m.nearest_beat(10.2)
    assert b is not None and abs(b - 10.0) < 0.06
    db = m.nearest_beat(10.2, downbeats=True)
    assert db is not None and abs(db - 10.2) <= 1.0
    assert any(abs(db - x) < 1e-6 for x in m.downbeats)
    assert 0.0 <= m.energy_at(5.0) <= 1.0
    assert m.energy_at(-1.0) == m.energy_at(0.0)
    s = m.summary(every=5.0)
    assert s["tempo_bpm"] == round(m.tempo_bpm, 1)
    assert s["duration_s"] == 40.0
    assert len(s["energy_curve"]) == 9
    assert isinstance(s["rises_at_s"], list) and isinstance(s["drops_at_s"], list)


def test_music_analysis_roundtrip(click_track, tmp_path):
    m = analyse_music(str(click_track), MusicConfig())
    m.save(tmp_path / "music.json")
    again = MusicAnalysis.load(tmp_path / "music.json")
    assert again.tempo_bpm == m.tempo_bpm
    assert np.allclose(again.beats, m.beats)
    assert np.allclose(again.downbeats, m.downbeats)
    assert np.allclose(again.energy, m.energy)
    assert again.path == m.path and again.hop_seconds == m.hop_seconds


def test_empty_analysis_has_no_beats():
    m = MusicAnalysis(path="x", duration=0.0, sample_rate=22050, tempo_bpm=0.0, beats_per_bar=4, beats=np.zeros(0), downbeats=np.zeros(0), energy=np.zeros(0), hop_seconds=0.5)
    assert m.nearest_beat(1.0) is None
    assert m.energy_at(1.0) == 0.0
