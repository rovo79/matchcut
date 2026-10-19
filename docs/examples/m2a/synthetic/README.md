# Example exports of a synthetic mock-backend workspace

Made with `python -m matchcut.pipeline.cli export WORKSPACE --resolve-script` on a workspace built from
`matchcut.pipeline.demo.make_demo_footage` (a 20 s, 25 fps synthetic clip with audio) and the mock backend,
as `tests/interchange/conftest.py` builds it (target 8 s, 5 clips, two dissolves in the final cut).

| File | What |
|---|---|
| `final.otio` | OpenTimelineIO, the final stage: video track V1 with clips, two transitions and a marker per junction; mirrored audio track A1 |
| `matchcut.fcpxml` | FCPXML 1.10, final stage |
| `final.edl` | CMX3600 EDL, final stage |
| `provenance.json` | the record behind the ids in the other files |
| `fidelity.md` | what this export keeps, approximates and loses |
| `open_in_resolve.py` | the script for Resolve Studio (matchcut never runs it) |

**Paths are placeholders.** The committed copies had the machine-specific scratch directory of the footage and of
the exports directory replaced by `/ABSOLUTE/PATH/TO` (in `source`, `target_url`, `media-rep src`, `MATCHCUT SOURCE`
and in the script's `TIMELINE_FILE`). Nothing else was changed, so the files do not open or import as they are: the
footage `demo.mp4` they name does not ship. The content key (`size`, `mtime_ns`) of the footage is as it was on the
machine that made them. To get usable files, run the command above on a workspace of your own.

`simulate_editor.py` (one directory up) edits an exported `final.otio` the way an editor's re-export would be
changed (a 5-frame trim, a dissolve turned into a cut, a clip deleted), for the round trip described in
[../../../M2A-ACCEPTANCE.md](../../../M2A-ACCEPTANCE.md).
