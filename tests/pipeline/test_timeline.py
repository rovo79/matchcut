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
    assert "D    013" in edl  # 0.5 s dissolve at 25 fps = 12.5 frames, rounded half up as the M2a exports and the render round it
    csv = tl.to_csv()
    assert csv.splitlines()[0].startswith("clip_id,shot_index")
    assert len(csv.splitlines()) == 4


def test_snap_to_frames_stays_inside_shot_bounds():
    tl = Timeline(source="x.mp4", fps=25.0)
    tl.clips = [Clip("c001", 3, 6.5, 8.5, "b1"), Clip("c002", 0, 0.3, 1.5, "b1")]
    snapped = tl.snap_to_frames({3: (6.5, 9.0), 0: (0.0, 1.5)})
    assert snapped.clips[0].source_in == 6.52  # 162.5 frames rounds down to 6.48, outside the shot: pulled up
    assert snapped.clips[0].source_out == 8.48
    assert snapped.clips[1].source_out == 1.48  # 37.5 frames rounds up to 1.52, past the shot end: pulled down
    plain = tl.snap_to_frames()
    assert plain.clips[0].source_in == 6.48  # without bounds, plain rounding
    assert tl.clips[0].source_in == 6.5  # input untouched
