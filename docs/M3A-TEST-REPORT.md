# M3a automated acceptance report

The automated acceptance of M3a (the edit core, `matchcut.edit`) on branch `claude/ai-video-editing-pipeline-bkqqay`, 2026-11-01, from
`c4c8fd2` plus the changes committed with this report. The spec's acceptance asks for the tasks' tests and for

> the prototype's 91-check scenarios re-expressed as tests against `matchcut.edit`, with every known gap classified as a known gap, never
> as a pass.

That is `tests/edit/test_acceptance_proof.py`. Scratch files were kept outside the repository. `WS` is a workspace.

The claims under test are in [EDIT.md](EDIT.md), [M3A-ACCEPTANCE.md](M3A-ACCEPTANCE.md) and the
[spec](superpowers/specs/2026-11-01-m3a-edit-core.md). The evidence re-expressed is `prototypes/editing_api/proof.py` and its latest
recorded run, `prototypes/editing_api/recorded-run-renderer-fix/proof.json` (90 passed, 1 known gap).

## Summary

- **The prototype's 91 checks:** every one is mapped, by id, to tests against the real API and CLIs, and classified:

  | class | checks | what it means |
  |---|---|---|
  | covered | 75 | the same behaviour is asserted |
  | superseded | 10 | M3a deliberately changed it; the test asserts the new behaviour and the mapping names the decision |
  | closed | 1 | the prototype's one known gap (an edited cut could not be exported), now exported and re-imported |
  | known gap | 0 | (none left; see the findings) |
  | out of scope | 5 | the MCP transport (M3b), skipped with the reason |

- **Acceptance tests:** `tests/edit/test_acceptance_proof.py`, 93 tests: 88 passed, 5 skipped (the MCP checks), 0 failed.
- **Findings: 2 shortfalls, both fixed here with tests.** M3a's `inspect` gave no shot descriptions and no story-beat target length, both of
  which the prototype checked (P05, P07). One undocumented behaviour (a proposal that changes nothing is refused) is now in EDIT.md.
- **`tests/edit`:** 761 passed, 5 skipped.
- **Full suite** (without the two research files, `-k "not perf"`): 1835 passed, 5 skipped, 31 deselected, 0 failed (837 s). The
  filter deselects more than the perf case: 30 of the 31 are `test_properties.py::test_random_edits_on_an_imperfect_base_make_nothing_worse`
  ("im*perf*ect"). They ran, and passed, in the `tests/edit` run.
- **Collection errors, not passes:** `tests/test_data.py` and `tests/test_experiments.py` fail to collect (they need `torch` and `toolz`).
  Run without the ignores, pytest stops at collection: `2 errors in 1.68s`, **no test runs**. These are collection errors, not passes or
  failures of M3a.
- **Performance case** (`MATCHCUT_PERF=1`): 100 x 0.4 s clips at 1080p rendered in **34.3 s**, peak 3020 MB, against a budget of 60 s
  (passed; the reviewer's recorded figure was 28.6 s on another machine).
- **Human gates: PENDING.** M3a ([M3A-ACCEPTANCE.md](M3A-ACCEPTANCE.md)), M1, M2b, M2a and M2c.

## Environment

| Item | Version |
|---|---|
| OS | Linux container (kernel 6.18), 4 CPUs, 15 GB RAM, no display |
| Python | 3.13.16, pytest 8.4.2 |
| ffmpeg / ffprobe | 6.1.1-3ubuntu5 |

No editor (Resolve, Final Cut Pro), no network, no model: the pipeline runs with the mock backend.

## How the proof was re-expressed

**The fixture is the proof's own**, built by the tests the way `proof.py` built it, and not imported from the prototype:

- `make_demo_footage` and `make_click_track`;
- `python -m matchcut.pipeline run demo.mp4 -w ws --backend mock -t 8 --music click.wav --no-render`;
- a copy before the pin, in which c004|c005 is marked a match cut the way the match-cut pass marks one;
- junction 1 pinned the way the Edit view's `l` does (`PinStore.put`), then `refine` re-runs the passes.

The pipeline produces the same cut as in the recorded run (5 clips; c001 0-50, c002 104-125, c003 163-219 with an 8-frame dissolve, c004
350-400 with a 19-frame dissolve, c005 444-462), so most checks keep the prototype's own numbers.

**The session is driven through `matchcut.edit`:** `session` (start, propose, apply, undo, redo, checkout, compare, log), `view` (inspect,
preview, render, pin_transition), `publish`, the edit JSON CLI (`python -m matchcut.edit`) and, where the prototype used it, the pipeline
CLI (`refine`, `export`, `import`, `junctions`). The edits (steps 3 to 7) are one script run on two fresh copies: the first run's
observations are checked one test per check, and the second run is checked by the same functions (P91) and compared byte for byte (P90).

**The mapping** (`MAPPING` in the test module) lists the 91 checks, each with its id (`P01` to `P91`: its position in the recorded run),
step, text, class, tests and, where needed, a note. Two tests guard it:

- `test_every_prototype_check_is_mapped` reads `proof.json` and fails unless the mapping has exactly its 91 checks, in order, with the same
  step and text; it checks the recorded summary (90 passed, 1 known gap), that the one gap is P79 and is classed closed, and that each
  check's text appears in `proof.py`.
- `test_the_mapping_names_real_tests_with_marks_that_match_their_class` checks that every named test exists, that a covered, superseded or
  closed test is not skipped or xfailed, that a known gap is `xfail(strict=True)` or skipped, that an out-of-scope test is skipped, and the
  counts per class.

### Superseded: 10 checks

| id | prototype check | M3a behaviour asserted | decision |
|---|---|---|---|
| P03 | starting again resumes it | `start` over a fresh session is refused (`pass restart`); the session in `WS/edits/` continues through every other command (API and CLI) | the approved spec's session model (EDIT.md, `start`); not one of the three numbered decisions |
| P10 | a pinned junction keeps its transition | its transition may change; once `pin_transition` protects it, a change is refused; the cut points stay held | decision 2 |
| P54 | snapping a junction already on its beat changes nothing | the proposal is refused, `the operations change nothing`, with `junction 2 (...) is already on the ...` in its details; nothing is stored | M3a's proposal rule (`session._evaluate`); not one of the numbered decisions, and undocumented until this report (now in EDIT.md) |
| P73 | publishing over an editor's import is refused | a publish that would leave a fresh `human.json` stale is refused, naming it, unless `supersede_human` | decision 1 |
| P74 | with `replace_import` the import is moved aside | `human.json` and `human_import.json` are never moved or changed; `supersede_human` leaves the human stage out as stale and says why | decision 1 |
| P75 | the head becomes the human stage | the head becomes the `edit` stage (`edit.json`, fresh, last in the stage order) | decision 1 |
| P76 | the pin is still applied in the human stage | the pin is applied in the `edit` stage; publish reports it `held` | decision 1 |
| P77 | `junctions` lists the human stage, clips renamed h001, h002 | `5 junctions in edit`, the session's ids e001, e002 kept, the pin badge and `held by this edit` | decision 1 |
| P78 | M2a's diff reads the new transitions on c005 as trims | the edit stage's history is final's plus one `agent` decision per change, the dissolve named as a transition, no trim of c005 | decision 1 |
| P88 | the published human stage is stale too | the published `edit` stage is stale (`stale_edit`), left out of the stage order, not deleted | decision 1 |

### Closed: the prototype's known gap (P79)

The recorded run's one non-pass: `matchcut export refuses the human stage ..., so an edited cut cannot yet go to an NLE`. Two tests now
cover it, through the CLIs only:

- `test_p79_the_edit_stage_exports_to_otio_fcpxml_and_edl_and_reimports`: r005 published with `python -m matchcut.edit WS publish`;
  `python -m matchcut.pipeline export WS --stage edit` exits 0 with `fidelity: 3 checks, 0 errors`; `fidelity.json` is ok for `edit` in
  every format; each file carries the sha256 of `edit.json`; the snapshot `exports/.stages/edit-<sha12>.json` is `edit.json` byte for byte;
  each of `edit.otio`, `matchcut.fcpxml` and `edit.edl` re-imports `against edit: 0 human decisions`, frame for frame the exported stage;
  an editor's OTIO with the last clip 5 frames shorter re-imports as exactly that one trim.
- `test_p79_a_reimport_compares_with_the_exact_snapshot_after_a_later_publish`: after the session edits on and publishes again, the
  earlier export still re-imports against its own snapshot with no decision.

### Out of scope: 5 checks

P80 to P84 are the prototype's MCP server (initialize and tools/list, inspect over MCP, a refusal, an error, a malformed request). MCP is
M3b. Each is a skipped test with that reason. Their CLI analogues are asserted under P85 (the CLI's `inspect` and `log` equal the Python
calls, a refusal's reason with exit 1, an unknown proposal with exit 1, malformed `ops` with exit 2 and the CLI answering after it, and
`--help` listing exactly the tool table), but they are not counted as covering the MCP checks.

### Covered, with a note

- **P05, P07:** pass only with the fix below.
- **P20** (a refused proposal cannot be applied): M3a never stores a refused proposal, so the test computes its would-be id and shows
  `apply` refuses it (`no proposal ...`), through the API and the CLI.
- **P24** (changing one transition moves no other junction): asserted as no other transition and no clip's source frames change, and every
  record frame from junction 2 on moves by exactly the 8 frames the dissolve overlapped (no rounding drift).
- **P66** (compare r000 -> r004): on M3a's whole-frame grid junction 3 starts on its beat (offset 0; the prototype read +1), so the roll
  (+1f) and the snap (-1f) cancel on c003 and the combined edge move shows as e001 starting one frame earlier than the replace put it.
  The compare lists the replace and the new dissolve, as in the prototype.
- **P51:** the detail says `onto the downbeat` where the prototype said `onto the beat` (M3a honours the beat-sync pass's downbeat
  preference); the frame count is the same.

## Findings

1. **`inspect` listed no shot descriptions (P05).** The prototype's inspect gave each shot its subject, tags, quality and usability, and the
   feasibility report counts "the unused footage" among what an agent inspects. M3a's gave only frames and use, so an agent choosing a
   `replace` or `insert` shot had nothing to go on. **Fixed:** each `shots[]` entry carries `description`, the describe stage's entry from
   `descriptions.json` (without its `shot_id`), or null. Test: `test_view.py::test_inspect_gives_each_shot_its_description_and_each_story_beat_its_target_length`.
2. **`inspect` gave story beats no target length (P07).** The prototype gave each beat's target and actual length; M3a gave the actual one
   only. **Fixed:** each `story_beats[]` entry carries `pace`, `target_seconds` and `target_frames` from `narrative.json` (null when absent
   or not a positive number). Same test.
3. **Undocumented: a proposal that changes nothing is refused (P54).** Deliberate and tested (`test_session.py`), but EDIT.md did not say
   so. **Documented** in EDIT.md's Proposals paragraph.

No other prototype check falls short. In particular every refusal, the transition limits, match cuts, the first clip, the atomic batch,
apply re-validation, the moved head, branches, the renders (exact frame counts, audio within 2 frames of video, exact source frames for
every decisive clip), staleness and byte-identical determinism hold as the prototype recorded them.

## Commands and results

| command | result | time |
|---|---|---|
| `python -m pytest tests/edit/test_acceptance_proof.py -q` | 88 passed, 5 skipped | 39 s |
| `python -m pytest tests/edit -q` | 761 passed, 5 skipped | 245 s |
| `python -m pytest tests -q --ignore=tests/test_data.py --ignore=tests/test_experiments.py -k "not perf"` | 1835 passed, 5 skipped, 31 deselected (snapshots: 5 passed) | 837 s |
| `python -m pytest tests -q` | interrupted: 2 errors during collection, 0 tests run | 2 s |
| `MATCHCUT_PERF=1 python -m pytest tests/pipeline/test_render_perf.py -q -s` | 1 passed: `100 x 0.4 s clips at 1080p: 34.3 s, peak 3020 MB` (budget 60 s) | 46 s |

The 5 skipped tests, in `tests/edit` and in the full suite, are the MCP checks. The 31 deselected are the perf case and the 30
`imperfect` property cases (see the summary); all 30 passed in the `tests/edit` run.

**Collection errors, reported apart from the passes.** Without the ignores, pytest cannot import two research test modules and runs
nothing:

- `tests/test_data.py`: `matchcut/data.py` imports `torch` (`ModuleNotFoundError: No module named 'torch'`);
- `tests/test_experiments.py`: `matchcut/experiments.py` imports `toolz` (`ModuleNotFoundError: No module named 'toolz'`).

Neither module touches the pipeline or `matchcut.edit`; they need the research environment (`conda_env.yml`). They are counted as
neither passed nor failed.

## Limits of this testing

- One fixture: the proof's synthetic demo footage (25 fps, 8 shots) and the mock backend. Other rates are covered by the tasks' own tests
  (`test_scenarios.py`, `test_edit_interchange.py` at 29.97 fps), not by the re-expressed proof.
- The editor files in P79 are written by matchcut's own exporters with one change, not by an editor; nothing was opened in Resolve or
  Final Cut Pro.
- The renders were checked by ffprobe and frame comparison against the source, not looked at.
- The MCP checks are not exercised at all: M3a has no MCP server.

## Human acceptance

Automated verification and human acceptance are separate. The **M3a human acceptance gate ([M3A-ACCEPTANCE.md](M3A-ACCEPTANCE.md)) is
PENDING**: no agent session has yet edited a real workspace through the CLI only with a person watching its previews, and no `edit` export
has been opened in an editor. The **M1, M2b, M2a and M2c human gates are also PENDING**.
