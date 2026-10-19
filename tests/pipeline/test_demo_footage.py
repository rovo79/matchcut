"""The synthetic footage cuts where it says it does, and shot detection lands on those cuts.

Regression: lavfi ``-t 1.5`` and ``-t 2.5`` at 25 fps each produce a whole extra frame (38 and 63 frames,
because the half frame is rounded up), and ``concat`` places the next segment after the last whole frame.
The two half frames added up, so every shot after the fourth began one frame (0.04 s) late: the frame at
14.00 s was still the last frame of the 0x208040 shot although the shot list says 14.00 s.
"""
from __future__ import annotations

import numpy as np
import pytest

from matchcut.pipeline.config import AnalysisConfig, ShotConfig
from matchcut.pipeline.demo import DEFAULT_SHOTS
from matchcut.pipeline.features import analyse_footage
from matchcut.pipeline.media import extract_frame, iter_frames
from matchcut.pipeline.shots import detect_shots

FPS = 25
W, H = 80, 45
NOMINAL_CUTS = np.cumsum([d for _, d in DEFAULT_SHOTS])[:-1]  # seconds: 2, 5, 6.5, 9, 12, 14, 16
GREEN = (0x20, 0x80, 0x40)


@pytest.fixture(scope="module")
def frames(demo_video) -> np.ndarray:
    return np.stack(list(iter_frames(str(demo_video), fps=FPS, width=W, height=H))).astype(np.int16)


@pytest.fixture(scope="module")
def cut_frames(frames) -> list:
    """Indices of the first frame of each new shot, from the pixel difference of consecutive frames."""
    diff = np.abs(frames[1:] - frames[:-1]).mean(axis=(1, 2, 3))
    return [int(i) + 1 for i in np.nonzero(diff > 20.0)[0]]


def test_content_changes_at_the_nominal_shot_times(cut_frames):
    assert len(cut_frames) == len(NOMINAL_CUTS)
    for idx, nominal in zip(cut_frames, NOMINAL_CUTS):
        # a cut cannot sit between frames: the nominal time is at most half a frame away
        assert abs(idx / FPS - nominal) <= 0.5 / FPS + 1e-9, (idx, nominal)
    # the cuts after the half-frame durations are exact: no drift accumulates
    assert [c / FPS for c in cut_frames[3:]] == [9.0, 12.0, 14.0, 16.0]


def test_frame_at_14s_is_the_first_frame_of_the_new_shot(demo_video):
    before = np.asarray(extract_frame(str(demo_video), 13.96, 32, 18)).astype(int)
    at = np.asarray(extract_frame(str(demo_video), 14.0, 32, 18)).astype(int)
    assert np.abs(before - np.array(GREEN)).max() <= 3  # shot 5 is a solid 0x208040
    assert np.abs(at - np.array(GREEN)).max() > 30  # shot 6 (rgbtestsrc) has started


def test_detected_shot_starts_match_the_content_change(demo_video, cut_frames):
    analysis = AnalysisConfig()
    series, _ = analyse_footage(str(demo_video), analysis)
    shots = detect_shots(series, ShotConfig())
    starts = [s.start for s in shots[1:]]
    assert len(starts) == len(cut_frames)
    step = 1.0 / analysis.fps
    for start, idx in zip(starts, cut_frames):
        # quantised to the analysis frame rate, never more than one analysis step from the real cut
        assert abs(start * analysis.fps - round(start * analysis.fps)) < 1e-6
        assert abs(start - idx / FPS) <= step + 1e-9, (start, idx / FPS)
    assert starts[5] == pytest.approx(14.0)
