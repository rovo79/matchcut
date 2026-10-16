import pytest

from matchcut.pipeline.config import AnalysisConfig, MatchCutConfig, ShotConfig
from matchcut.pipeline.features import analyse_footage
from matchcut.pipeline.matching import MatchScorer
from matchcut.pipeline.shots import detect_shots


@pytest.fixture(scope="module")
def analysed(matchcut_video):
    series, cells = analyse_footage(str(matchcut_video), AnalysisConfig())
    shots = detect_shots(series, ShotConfig(), duration=10.0)
    return cells, shots


def test_designed_match_outscores_unrelated_junctions(analysed):
    cells, shots = analysed
    assert [s.index for s in shots] == [0, 1, 2, 3, 4]
    scorer = MatchScorer(cells, MatchCutConfig())
    a, filler, b, static, busy = shots
    match = scorer.score_cut(a.end, b.start)
    assert match.combined > 0.6
    assert match.motion_type == "moving"
    assert match.structure > 0.5
    for bad in (scorer.score_cut(a.end, static.start), scorer.score_cut(filler.end, b.start), scorer.score_cut(a.end, busy.start)):
        assert bad.combined < 0.4
        assert bad.combined < match.combined - 0.25


def test_best_junction_search_stays_in_range(analysed):
    cells, shots = analysed
    scorer = MatchScorer(cells, MatchCutConfig())
    a, _, b, _, _ = shots
    out_t, in_t, score = scorer.best_junction((a.end - 1.0, a.end), (b.start, b.start + 1.0))
    assert a.end - 1.0 <= out_t <= a.end
    assert b.start <= in_t <= b.start + 1.0
    assert score.combined >= scorer.score_cut(a.end, b.start).combined - 1e-6


def test_identical_frames_flag_jump_cut(analysed):
    cells, shots = analysed
    scorer = MatchScorer(cells, MatchCutConfig())
    static = shots[3]
    s = scorer.score_cut(static.start + 1.0, static.start + 1.1)
    assert s.jump_cut_risk
