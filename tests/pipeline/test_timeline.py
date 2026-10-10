from matchcut.pipeline.timeline import Clip, Timeline, Transition


def make_tl():
    tl = Timeline(source="x.mp4", fps=25.0)
    tl.clips = [
        Clip("c001", 0, 0.0, 2.0, "b1"),
        Clip("c002", 1, 3.0, 5.0, "b1"),
        Clip("c003", 2, 6.0, 8.0, "b2", transition_in=Transition("dissolve", 0.5)),
    ]
    return tl


def test_duration_accounts_for_overlapping_transitions():
    tl = make_tl()
    assert abs(tl.duration - 5.5) < 1e-9
    assert tl.record_start(1) == 2.0
    assert abs(tl.record_start(2) - 3.5) < 1e-9


def test_roundtrip_and_validation():
    tl = make_tl()
    again = Timeline.from_dict(tl.to_dict())
    assert again.to_dict() == tl.to_dict()
    assert tl.validate({0: (0, 2), 1: (3, 5), 2: (6, 8)}) == []
    tl.clips[1].source_out = 5.5
    assert any("outside shot" in p for p in tl.validate({0: (0, 2), 1: (3, 5), 2: (6, 8)}))


def test_edl_and_csv_exports():
    tl = make_tl()
    edl = tl.to_edl("demo")
    assert edl.startswith("TITLE: demo")
    assert "001  AX       V     C        00:00:00:00 00:00:02:00 00:00:00:00 00:00:02:00" in edl
    assert "D    012" in edl  # 0.5 s dissolve at 25 fps
    csv = tl.to_csv()
    assert csv.splitlines()[0].startswith("clip_id,shot_index")
    assert len(csv.splitlines()) == 4
