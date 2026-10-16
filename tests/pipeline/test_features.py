import numpy as np

from matchcut.pipeline.config import AnalysisConfig, ShotConfig
from matchcut.pipeline.features import (
    analyse_frames,
    analyse_footage,
    color_histogram,
    phase_shift,
    structure_vector,
)
from matchcut.pipeline.shots import detect_shots


def test_phase_shift_sign_and_axis():
    rng = np.random.default_rng(0)
    img = (rng.random((90, 160)) * 255).astype(np.float32)
    for dx, dy in [(3, 0), (-3, 0), (0, 2), (4, -3)]:
        moved = np.roll(np.roll(img, dx, axis=1), dy, axis=0)
        assert phase_shift(img, moved) == (float(dx), float(dy))


def test_color_histogram_is_normalised():
    frame = np.zeros((90, 160, 3), dtype=np.uint8)
    frame[..., 0] = 255
    h = color_histogram(frame)
    assert abs(h.sum() - 1.0) < 1e-6
    assert h.max() == 1.0  # a single colour lands in one bin


def test_structure_vector_is_position_sensitive():
    a = np.full((90, 160), 60, np.float32)
    a[30:55, 20:45] = 255
    b = a.copy()
    c = np.full((90, 160), 60, np.float32)
    c[30:55, 120:145] = 255
    sa, sb, sc = structure_vector(a), structure_vector(b), structure_vector(c)
    assert np.dot(sa, sb) > 0.99
    assert np.dot(sa, sc) < 0.2


def test_analyse_frames_synthetic_cut():
    cfg = AnalysisConfig(fps=8, cell_seconds=0.5, width=64, height=36)
    frames = []
    for i in range(40):  # 5 s: 2.5 s blue then 2.5 s red
        f = np.zeros((36, 64, 3), dtype=np.uint8)
        f[..., 2 if i < 20 else 0] = 200
        frames.append(f)
    series, cells = analyse_frames(frames, cfg)
    assert series.n_frames == 40
    assert int(np.argmax(series.hist_dist)) == 20
    assert len(cells) == 10
    shots = detect_shots(series, ShotConfig(), duration=5.0)
    assert [round(s.start, 2) for s in shots] == [0.0, 2.5]


def test_shot_detection_on_demo_footage(demo_video):
    series, _ = analyse_footage(str(demo_video), AnalysisConfig())
    shots = detect_shots(series, ShotConfig(), duration=18.5)
    starts = [round(s.start, 2) for s in shots]
    assert starts == [0.0, 2.0, 5.0, 6.5, 9.0, 12.0, 14.0, 16.0]
    assert abs(shots[-1].end - 18.5) < 1e-6
