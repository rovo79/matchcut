# Example exports of a synthetic mock-backend workspace

Made with `python -m matchcut.pipeline.cli export WORKSPACE --resolve-script` on a workspace built from
`matchcut.pipeline.demo.make_demo_footage` (an 18.5 s, 320x180, 25 fps synthetic clip with audio) and the mock backend,
as `tests/interchange/conftest.py` builds it (target 8 s, 5 clips, two dissolves in the final cut).

| File | What |
|---|---|
| `final.otio` | OpenTimelineIO, the final stage: video track V1 with clips, two transitions and a marker per junction; mirrored audio track A1 |
| `matchcut.fcpxml` | FCPXML 1.10: one event with two projects, the rough cut and the final cut |
| `final.edl` | CMX3600 EDL, final stage |
| `provenance.json` | the record behind the ids in the other files, for both stages (roughcut and final) |
| `fidelity.md` | what the exports keep, approximate and lose: both stages, all three formats |
| `open_in_resolve.py` | the script for Resolve Studio (matchcut never runs it) |

**Paths are placeholders.** The committed copies had the machine-specific scratch directory of the footage and of
the exports directory replaced by `/ABSOLUTE/PATH/TO` (in `source`, `target_url`, `media-rep src`, `MATCHCUT SOURCE`
and in the script's `TIMELINE_FILE`). Nothing else was changed, so the files do not open or import as they are: the
footage `demo.mp4` they name does not ship. The content key (`size`, `mtime_ns`) of the footage is as it was on the
machine that made them. To get usable files, run the command above on a workspace of your own.
`tests/interchange/test_docs.py` regenerates these files on the test fixture and fails if they differ from the code's output once the paths, the footage's
modification time and size, the path-derived uids, the stage-file hashes and the matchcut version are normalised away; if it fails, run the command on a workspace
built like the fixture's, replace the scratch directory of the footage and of the exports directory by `/ABSOLUTE/PATH/TO` and commit the files again.

**The footage is the one thing the test cannot control.** `make_demo_footage` encodes the clip with the local ffmpeg, and shot detection and the scores
run on the encoded video, so another ffmpeg build can find other shot boundaries. The line below records what the committed files were made from. The
test compares it with the fixture footage of the machine it runs on and, when they differ, **skips with the two fingerprints in its message**
instead of failing: the examples cannot be compared there, and nothing is wrong with the code. After regenerating the files on a machine whose
footage differs, replace the line.

Fixture footage (fingerprint): 462 frames at 25 fps; shots (s): 0.00-2.00 2.00-5.00 5.00-6.50 6.50-9.00 9.00-12.00 12.00-14.00 14.00-16.00 16.00-18.50

`simulate_editor.py` (one directory up) edits an exported `final.otio` the way an editor's re-export would be
changed (a 5-frame trim, a dissolve turned into a cut, a clip deleted), for the round trip described in
[../../../M2A-ACCEPTANCE.md](../../../M2A-ACCEPTANCE.md).
