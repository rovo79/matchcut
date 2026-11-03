# M3a acceptance: can an agent edit a real cut through the CLI alone, and does the edit travel to an editor?

M3a adds the edit core, `matchcut.edit`: a frame-native, transactional and reversible way to edit a cut through a JSON CLI
(`python -m matchcut.edit WS COMMAND '{...}'`). An agent proposes operations, every rule is checked on the whole result, previews are
rendered by matchcut's own renderer, accepted proposals become immutable revisions, and a finished revision is published as the
workspace's `edit` stage, which M2a exports to an editor like any stage. How it works: [EDIT.md](EDIT.md). Design:
[superpowers/specs/2026-11-01-m3a-edit-core.md](superpowers/specs/2026-11-01-m3a-edit-core.md).

M3a has **two acceptance outcomes, reported independently**:

- **A. M3a Core Editorial Acceptance**: an agent, through matchcut's JSON CLI only, makes a useful revision of a real cut to a real brief,
  keeps the editor's pins, publishes it, and the user judges the films.
- **B. M3a Editor Interoperability Acceptance**: the published `edit` stage goes to an external editor and back with exact provenance.

**An external editor failing to import an otherwise valid timeline is recorded against B, as an editor or format issue. It is not proof
that A (core editing) fails.** A can pass while B fails, and the reverse.

## Status

- **Automated verification: pass** ([M3A-TEST-REPORT.md](M3A-TEST-REPORT.md)). It proves the operations, rules, store, renders, publish,
  the pin and mismatch safety rules and the `edit` stage's round trip through matchcut's own readers. It does not prove that an agent can
  do useful editorial work with the CLI, that the renders look right, or that an editor opens the `edit` export as intended.
- **A. M3a Core Editorial Acceptance: PENDING.**
- **B. M3a Editor Interoperability Acceptance: PENDING.**
- **M2a, M2b, M2c and M1 human gates: PENDING** ([M2A-ACCEPTANCE.md](M2A-ACCEPTANCE.md), [M2B-ACCEPTANCE.md](M2B-ACCEPTANCE.md),
  [M2C-PROBE.md](M2C-PROBE.md), [M1-ACCEPTANCE.md](M1-ACCEPTANCE.md)). B leans on M2a's gate: if an editor cannot open M2a's exports, B
  fails for the same reason.

## Which checks run without the user / which need the user

Every step below is marked:

- **[auto]**: the agent (or a script) runs it and checks the result without the user: a command, its exit code and its JSON, a file's
  sha256, a refusal's wording. `tools/m3a_acceptance.py` does the bookkeeping ([below](#the-helper-script)).
- **[human]**: it needs the user's visual or editorial judgement (watching a film, judging a cut against the brief), or an action in the
  TUI or in the external editor.

| | [auto] | [human] |
|---|---|---|
| Set-up | venv, install, ffmpeg check, the workspace copy, the snapshots | choosing the workspace and the brief |
| A | `start`/`inspect`/`log` results, the refusals and their wording, `publish`, provenance, canonical files unchanged (`verify`), the original workspace unchanged | making the pins in the TUI, re-running the pipeline from the TUI (or [auto] from the shell), watching the previews and both films, judging the revision against the brief |
| B | the export and its fidelity report, the import's decisions and provenance, the EDL refusal, staleness after a re-run | opening the export in the editor, checking timing, transitions and music there, making the human edit and re-exporting |

**A gate passes only when the user has watched and approved the videos** (A), or checked the timeline in the editor (B). **Never mark a
human gate passed on automated tests or ffprobe results.** The tests, the helper script and ffprobe show that files are what they claim
to be; only the user can say the cut is good, the brief is met, or the editor shows the right thing.

## What the agent must never do without the user

Give these rules to the agent with the brief:

- edit only through `python -m matchcut.edit "$WS" ...` (reading `python -m matchcut.pipeline.cli junctions "$WS" ...` is allowed); never
  write a workspace file by hand;
- **never re-run the pipeline and never restart a session the user is working in, unless the user asks.** A refusal that needs either
  says "ask the user to ...": a re-run costs model calls and leaves the edit and human stages out as stale;
- **never pass `acknowledge_mismatches` or `supersede_human` to `publish` without the user's explicit approval.** When `publish` refuses
  and names one of them, stop, show the user the refusal and wait. Both are editorial decisions: carrying a protected-transition
  mismatch or a protection changed during the session forward, or leaving an editor's re-import out of the stage order;
- **never release (`pin_transition {"hold": false}`) or replace a transition protection the editor set, and never pass `user_approved`,
  without the user's explicit approval.** While a session exists `pin_transition` refuses it without `user_approved` naming the pin, and
  even an approved change makes `publish` refuse until the user also acknowledges it. The agent's own way out of a mismatch is the repair
  (`set_transition` to the held transition).

## Automated verification

Offline and deterministic: synthetic footage (ffmpeg's test pattern at 25 and 30000/1001 fps), small hand-built workspaces and a mock-backend
pipeline run; no network, no model, no editor. All under `tests/edit/` unless named:

| Module | What it proves |
|---|---|
| `test_ops.py`, `test_rules.py`, `test_model.py` | The nine operations in frames, refusals in one line, the proposal-wide rules: shot bounds and minimum length, transition kinds, whole frames and limits, pins (clips, adjacency and cut points fixed; the transition free unless protected), match cuts, only new violations counted, the timing report and the guards |
| `test_properties.py`, `test_scenarios.py` | Random edit sequences on demo cuts: untouched clips and junctions identical in frames, the running time changing exactly by the trims and overlaps, every pin and match-cut invariant held, no proposal adding a violation; the prototype proof's scenarios re-expressed |
| `test_acceptance_proof.py` | The prototype's 91-check proof re-expressed against `matchcut.edit`, every check mapped and classified ([M3A-TEST-REPORT.md](M3A-TEST-REPORT.md)) |
| `test_session.py`, `test_session_pipeline.py`, `test_session_export.py` | The store: transactions, apply re-validation, a moved head, two processes under the lock, undo/redo/checkout over branches, staleness and the move-aside, deterministic revisions, r000 on the frames M2a's export uses |
| `test_view.py`, `test_view_render.py` | `inspect`, renders and previews checked with identifiable media, `frames` images, render names, the `renders/.tmp` sweep |
| `test_tools.py`, `test_cli.py` | The tool table and its schemas, exit codes 0/1/2 and one JSON object on stdout in every case, `@file` and `@-`, and that the engine, the review package, the interchange package and the TUI never import `matchcut.edit` |
| `test_publish.py`, `test_publish_review.py` | Publish, its record, archive and log; the same and another session; the human stage; staleness; pins re-checked at publish; the stage lock |
| `test_pin_safety.py` | A pin the session's base does not hold fails closed: every operation type touching its shots (trim, roll, slip, replace, insert, remove, move, set_transition, snap_to_beat) and indirect changes refused with what to do; edits elsewhere allowed; `start`/`inspect`/`log` list it; a pin made after apply makes publish refuse; an untouched revision publishes with the pin not protected; the exact re-run command it names, run, then `start` with restart: the pin held and protecting; the not-placed variant; CLI exit 1 with the JSON refusal. A protected-transition mismatch: refused at publish, repaired, acknowledged with the exact list and recorded, partial/extra/unknown lists refused, unrelated proposals accepted, never worded held (inspect, publish, `junctions`, the TUI). `supersede_human` bound to the file's sha12 (`true`, wrong, stale and needless values refused) |
| `test_protection_changes.py` | A protection the session started with (recorded in `r000`) and released, replaced or cleared during the session: `pin_transition` refuses it without `user_approved` naming the pin, `publish` refuses it until the user acknowledges it (API and CLI), the result, record and log say it, and nothing reads `held` (inspect, publish, `junctions` line and footer, the TUI); restoring the protection publishes; `restart` takes a new baseline; the re-run command (absolute, `--keep-config`, runnable from any directory); a relative source (resolved, or refused with the path and directory) |
| `test_m3a_acceptance_tool.py` | `tools/m3a_acceptance.py`: snapshot, verify and report around a session and a publish, a changed or new canonical file named, and nothing ever written inside the workspace; strict mode (`--all`/`--strict`) naming every new, removed or changed file a session, a publish and hand edits leave on an original, quick hashing of large files, a missing source an error |
| `test_edit_interchange.py` | `export --stage edit` in OTIO, FCPXML and EDL at 25 and 29.97 fps with fidelity, fingerprints and snapshot; re-import against the exact snapshot after a later publish; the refusals and `--stage`/`--base`; an end-to-end run through the CLIs only |
| `tests/tui/test_edit.py` | The Edit view lists a published `edit` stage after `final`; a stale one is left out with its reason; a pin made while an edit session exists says the session will not protect it until the pipeline is re-run and the session restarted |
| `tests/pipeline/test_junctions.py`, `tests/interchange/` | Every junction and interchange test from M2a stays green |

## Set-up (Mac desktop, real footage)

Needs: macOS with Python 3.10 or later, ffmpeg and ffprobe on `PATH` (from Homebrew, S2), a terminal with an image protocol for the TUI
(Kitty, WezTerm, Ghostty or iTerm2), a video player, an agent that can run shell commands (a coding agent), an `ANTHROPIC_API_KEY` if the
workspace's pipeline uses the real backend (a re-run of the passes may call the model), and for B an editor (DaVinci Resolve free or
Studio, or Final Cut Pro).

**Never modify the original project.** Everything below runs on a **copy** of an existing workspace; the original workspace and the
source footage are only read, and step S5 (a strict snapshot of every file in the original) with A12 and B7 proves it.

**Run every command from the repository root** (`cd ~/matchcut`, S1): `python -m matchcut.*` finds the package there. From anywhere else,
set `PYTHONPATH=~/matchcut` first. The commands the edit CLI prints for a re-run carry `PYTHONPATH` themselves.

S1. **[human]** Choose a workspace from an earlier real run whose cut has at least six junctions (music if you can), and a real creative
brief for it. Set the variables (every later command uses them):

```shell
cd ~/matchcut                                         # the repository
SRC_WS=~/Movies/my_project_edit                       # the existing workspace: only ever read
WS=~/m3a-run/ws                                       # the copy every step works on
OUT=~/m3a-run/out                                     # snapshots, films and editor files: outside the workspace
mkdir -p ~/m3a-run "$OUT"
```

S2. **[auto]** The environment (ffmpeg from Homebrew when it is missing):

```shell
command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || brew install ffmpeg
ffmpeg -version | head -1 && ffprobe -version | head -1      # both must print a version
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-pipeline.txt -r requirements-tui.txt
python -m matchcut.edit --help > /dev/null && echo "matchcut.edit OK"    # from the repository root
```

S3. **[auto]** Copy the workspace (an APFS clone: instant, and a change to the copy never touches the original), or with `ditto`. The
copy must not exist yet: if `$WS` is there, stop and choose another path (or remove that old copy yourself); never copy over it.

```shell
if [ -e "$WS" ]; then echo "STOP: $WS already exists; choose another WS (or remove that old copy) before going on"; else
  cp -cR "$SRC_WS" "$WS" && diff -rq "$SRC_WS" "$WS" && echo "copy identical"; fi   # or: ditto "$SRC_WS" "$WS"
```

S4. **[auto]** The source footage (and music) the workspace uses; both are only read. `config.json` may name the footage by a relative
path (`python -m matchcut.pipeline run footage.mp4` stores `"footage.mp4"`, relative to the directory the pipeline ran in), which no
command run from the repository root would find. The helper resolves it against the current directory and the directory the original run
was made in, and prints the absolute path; the **copy's** `config.json` (never the original's) then names it absolutely, so every later
step finds it:

```shell
SRC=$(python tools/m3a_acceptance.py source "$SRC_WS") && [ -e "$SRC" ] && echo "footage: $SRC"
python -c 'import json,sys; p=sys.argv[1]; d=json.load(open(p)); d["source"]=sys.argv[2]; open(p,"w").write(json.dumps(d, indent=2))' "$WS/config.json" "$SRC"
MUSIC=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["music"]["path"] or "")' "$WS/config.json")
ls -l "$SRC" ${MUSIC:+"$MUSIC"}
```

If `source` exits 1 (`the source footage 'footage.mp4' that config.json names is not found ...`), stop before the second line: find the
footage (`mdfind -name footage.mp4`), set `SRC=/absolute/path/to/footage.mp4`, check `[ -e "$SRC" ] && echo ok`, then run the second line
(it writes the copy's config only). Alternatively run every pipeline and edit command from the directory where the relative path
resolves, with `PYTHONPATH=~/matchcut`.

S5. **[auto]** A **strict** snapshot of the **original** workspace: every file in its tree (path, size, sha256), to prove at the end that
nothing touched it. Files above 256 MiB (renders) are hashed from their size and first and last MiB, and the output lists them (`--full`
hashes them in full); the source footage outside the workspace is checked by size and modification time. A source it cannot find is an
error (exit 1), never a silent pass:

```shell
python tools/m3a_acceptance.py snapshot "$SRC_WS" --all -o "$OUT/original-workspace.snapshot.json"
```

(If `$SRC_WS`'s footage is relative and not found from the repository root, run this one command from the directory where it resolves,
with `python ~/matchcut/tools/m3a_acceptance.py ...`.)

### The helper script

`tools/m3a_acceptance.py` runs the [auto] bookkeeping and **never writes inside the workspace** (a snapshot is refused there):

| command | what it does |
|---|---|
| `source WS` | prints the source footage `config.json` names, as an absolute path (a relative one resolved from the current directory and the directory the run was made in); exit 1 with what to do when it is not found |
| `snapshot WS [-o FILE]` | canonical mode, for the working copy: records the sha256 of `final.json`, every pass file, `roughcut.json`, `editorial.json` and `human*.json`, and the source media's size and modification time (the footage, the probed file, the music), in a file outside the workspace (default `<WS>.m3a-snapshot.json` next to it) |
| `snapshot WS --all [--quick-above MIB \| --full] [-o FILE]` | strict mode, for the original workspace: every file, symlink and directory in the tree (path, size, sha256, modification time), nothing exempt; files above 256 MiB hashed from their size and first and last MiB (listed in the output) |
| `verify WS SNAPSHOT [--strict]` | exit 0 when everything recorded is unchanged; otherwise exit 1 and one line per change. Canonical: changed, missing or new canonical files and media; `edit.json`, `edit_publish.json` and `edits/` are expected to change and are listed apart. Strict (`--strict` requires a `--all` snapshot): every new, removed or changed file or directory; a modification time alone never fails it, except on a quickly hashed file |
| (both) | a source footage or music track that `config.json` names but that cannot be found is an error (exit 1), never a pass |
| `report WS [--json]` | the session and its revisions, the published record, the provenance (base sha, latest canonical sha, session, revision, whether each still matches `edit.json`, the published copy and the stage files), the human import (stage, snapshot, decisions), the pins not protected by the edit, the acknowledged mismatches, and the films and renders to watch |

## A. M3a Core Editorial Acceptance

**Status: PENDING.**

**The pin first (pin acceptance).** A pin made in the TUI is applied by the pipeline's next run; an edit session protects only the pins
its base stage holds. So the user makes the pin, re-runs the pipeline so the canonical stage applies it, and only then starts the edit
session. A pin made after the session started is shown failing closed (A8).

A1. **[human]** Open the TUI on the copy, go to the Edit view and pin one junction:

```shell
python -m matchcut.tui "$SRC" -w "$WS"
```

Press `4`, pick a junction with `←`/`→` (one that matters to the brief, e.g. a match cut you like), press `l`. The badge reads
`pin pending: run again (R) to apply`. Note the junction (`s<out>->s<in>`, shown as `shot N → shot M`).

A2. **[human]** Re-run the pipeline so the canonical stage applies the pin: press `R` in the TUI and press `c` at each checkpoint (the
describe, narrative and rough-cut stages are reused; the passes run again). The badge now reads `pinned (locked)`. Quit the TUI (`q`).
**[auto]** alternative, from the shell (the same thing without the TUI):

```shell
python -m matchcut.pipeline refine "$SRC" -w "$WS" --keep-config     # --keep-config: WS/config.json is used as it is, never rewritten
```

A3. **[auto]** Snapshot the canonical files the session must not change:

```shell
python tools/m3a_acceptance.py snapshot "$WS" -o "$OUT/before-session.snapshot.json"
```

A4. **[human]** Give the agent [EDIT.md](EDIT.md), the rules in [What the agent must never do](#what-the-agent-must-never-do-without-the-user),
the variables `WS` and `OUT`, and the brief. For example: *"Tighten the first minute by about two seconds without moving any cut that
lands on a downbeat, soften one hard cut that jars, and publish. Look at frames and previews before you apply anything."* Do not touch the
workspace yourself, except in A8.

A5. **[auto]** The agent starts the session and inspects. The pin is held and nothing is unapplied:

```shell
python -m matchcut.edit "$WS" start | tee "$OUT/start.json"                       # "unapplied_pins": [], "can_publish": true
python -m matchcut.edit "$WS" inspect > "$OUT/inspect-r000.json"                 # pins[]: the A1 pin has "state": "held"
python -m matchcut.edit "$WS" render '{"revision": "r000", "quality": "full"}' | tee "$OUT/original-render.json"
cp "$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["path"])' "$OUT/original-render.json")" "$OUT/original.mp4"
```

`original.mp4` is the cut the session starts from (the canonical `final` with the pin applied, on whole frames).

A6. **[auto]** The agent looks at shots, frames, music beats and decisions before it edits: `inspect` (clips, junctions, shots with
descriptions and unused shots, story beats with target lengths, music beats and offsets, pins and protections), `frames` at the junctions
it considers, `python -m matchcut.pipeline.cli junctions "$WS" --index N` for why a cut is there:

```shell
python -m matchcut.edit "$WS" frames '{"junction": 3}'
python -m matchcut.pipeline.cli junctions "$WS" --index 3
```

A7. **[auto]** The pin protects: the agent proposes moving the pinned cut, and is refused (exit 1, `held by an editor pin` or `breaks the
editor pin`):

```shell
python -m matchcut.edit "$WS" propose '{"ops": [{"op": "roll", "junction": "s1-s3", "frames": 2}]}'; echo "exit $?"   # the A1 junction's id
```

A8. **[human]** then **[auto]**: fail closed. While the session is open, the user opens the TUI again (`python -m matchcut.tui "$SRC" -w "$WS"`),
pins **another** junction (`l`) that the brief does not need, and reads the notification: it ends `an active edit session will not
protect this pin until you re-run the pipeline and restart the session (it refuses edits to these shots until then)`. The agent then
proposes a change to one of that pin's clips (`log` lists the pin under `unapplied_pins`, with its `affected_clips`; `c004` below
stands for one of them) and is refused (exit 1) with the pin, what the edit changes and what to do:

```shell
python -m matchcut.edit "$WS" propose '{"ops": [{"op": "trim", "clip": "c004", "edge": "in", "frames": 2}]}'; echo "exit $?"
# refused: pin s5->s6 was made or changed after the pipeline's last run, so this edit session does not protect it yet ...
#   to protect it, ask the user to re-run the pipeline so the canonical stage applies it (PYTHONPATH=... python -m matchcut.pipeline
#   refine /abs/footage -w /abs/ws --keep-config, ...), then to restart the edit session (start {"restart": true}); ...
python -m matchcut.edit "$WS" log | python -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["unapplied_pins"], indent=1))'
python tools/m3a_acceptance.py snapshot "$WS" -o "$OUT/after-second-pin.snapshot.json"
```

Leave that pin in place: the agent edits elsewhere, and the publish will list it as **not protected by this edit**.

A9. **[auto]** The agent works in rounds: `propose` (with `guards` where the brief asks), `frames`, `preview` (a junction window or the
whole cut), `apply`, `undo` when a result is not what it wanted, `compare`. Besides A7 and A8 it should meet at least one refusal of the
rules (a guard, a transition limit, a match cut) and act on it. Every command prints one JSON object; nothing outside the CLI changes the
workspace.

A10. **[human]** Watch two or three previews the agent made (`window-*.mp4` under `$WS/edits/renders/`; `report` lists them): does each
show the change its proposal described, with sound in place and in sync?

A11. **[auto]** The agent publishes:

```shell
python -m matchcut.edit "$WS" publish | tee "$OUT/publish.json"
```

The result lists the A8 pin under `unapplied_pins` and in `pins` as `not_protected`, and the A1 pin as `held`. If `publish` refuses with
`acknowledge_mismatches` or `supersede_human`, the agent **stops and asks the user [human]**; it never passes either on its own. The
same holds for a protection: if the agent wants to release one (`pin_transition {"hold": false}`) or protect another transition at a pin
the editor protected, `pin_transition` refuses without `user_approved` and the agent asks the user; a change the user approves still
makes `publish` refuse until the user acknowledges it, and the record lists it under `acknowledged_protection_changes`, never as held.

A12. **[auto]** Render the published revision, check provenance, and check that nothing canonical changed:

```shell
REV=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["revision"])' "$WS/edit_publish.json")
python -m matchcut.edit "$WS" render "{\"revision\": \"$REV\", \"quality\": \"full\"}" | tee "$OUT/revised-render.json"
cp "$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["path"])' "$OUT/revised-render.json")" "$OUT/revised.mp4"
python tools/m3a_acceptance.py report "$WS" | tee "$OUT/report-A.txt"
python tools/m3a_acceptance.py verify "$WS" "$OUT/after-second-pin.snapshot.json"   # exit 0: every canonical file unchanged
python tools/m3a_acceptance.py verify "$WS" "$OUT/before-session.snapshot.json"     # only "editorial.json: changed" (the A8 pin)
python tools/m3a_acceptance.py verify "$SRC_WS" "$OUT/original-workspace.snapshot.json" --strict   # exit 0: not one file of the original changed
python -m matchcut.pipeline.cli junctions "$WS"      # "N junctions in edit"; the footer: the A1 pin held, the A8 pin not protected
```

`report` must show provenance all `True` (base and latest canonical unchanged, `edit.json` and the published copy match the record,
the current session), the A8 pin among the pins not protected, `acknowledged mismatches: 0` and `acknowledged protection changes ...: 0`
(or exactly the ones the user approved). If the agent ran `pin_transition`, `editorial.json` changes too: `verify` names it; check it was
the user's decision (a release or a replacement needs `user_approved`, and shows in the report as an acknowledged protection change).

A13. **[human]** Watch `$OUT/original.mp4` and `$OUT/revised.mp4` in full. Does the revision do what the brief asked, is it better (or
at least not worse) elsewhere, is the pinned junction where you pinned it, and is the sound in sync? Open the TUI (`4`): it lands on
`edit`, `[`/`]` step between `final` and `edit`, and the pinned junction still shows its pin.

### A: pass / fail

A **passes** when all of these hold, and the user has watched both films:

- the agent completed the brief through the JSON CLI alone, inspecting shots, frames, music beats and decisions, proposing and previewing
  before applying, without a traceback, a hand edit or an output it could not parse;
- the revision is useful: the user judges `revised.mp4` against `original.mp4` and the brief and accepts it;
- at least one refusal was met and acted on; the A1 pin was held (A7) and the A8 pin failed closed with a message the agent could act on;
- the publish recorded exact provenance; the A8 pin is listed as not protected, never held;
- every canonical file is unchanged after the session (`verify` against A8's snapshot exits 0), and the original workspace is untouched.

It **fails** on a wrong frame in a preview or the revised film at an edited cut, a lost or changed canonical file, a pinned cut that moved,
an edit at an unapplied pin that was not refused, a mismatch or human stage superseded without the user's approval, or a brief the agent
could not get through because a refusal or a report was unclear (write down which).

### A: results

Record the run: date, footage, brief, agent, the commit of matchcut.

| Question | Answer |
|---|---|
| The agent finished the brief through the JSON CLI alone (yes/no) | |
| It inspected shots, frames, music beats and decisions before editing (yes/no, examples) | |
| The refusals and timing reports were clear enough to act on (yes/no, examples) | |
| The A1 pin was applied by the re-run, held by the session and protected (A5, A7) (yes/no) | |
| The A8 pin failed closed, its notification and refusal said what to do (yes/no) | |
| The previews showed the proposed changes, with audio in sync (yes/no) | |
| The publish recorded provenance; the A8 pin was listed as not protected (yes/no) | |
| Canonical files unchanged (`verify`), original workspace untouched (yes/no) | |
| **[human]** The revised film is a useful revision for the brief, judged against the original (yes/no, notes) | |
| Notes: what was slow, unclear or missing | |
| **Decision (A)** | accept core editing / change the approach |

## B. M3a Editor Interoperability Acceptance

**Status: PENDING.**

B uses the workspace A published (or any copy with a fresh published `edit` stage). It is recorded on its own: an editor that cannot
import an otherwise valid timeline fails B as an editor or format issue, and says nothing about A.

B1. **[auto]** Export the edit stage (every format, with the fidelity report and the snapshot):

```shell
python -m matchcut.pipeline.cli export "$WS" --stage edit      # edit.otio, the edit project in matchcut.fcpxml, edit.edl
cat "$WS/exports/fidelity.md"; ls "$WS/exports/.stages/"       # the snapshot edit-<12 hex>.json
B1_SNAP=edit-$(python -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest()[:12])' "$WS/edit.json")
ls -l "$WS/exports/.stages/$B1_SNAP.json"                       # this export's snapshot, named by the sha256 of edit.json; B6 uses it
```

B2. **[human]** Open `$WS/exports/edit.otio` (and `matchcut.fcpxml`, project `edit`) in the editor. Compare with `$OUT/revised.mp4`:

- frame timing: a frame at two edited cuts, and the total length;
- transitions: each dissolve's length and position, every cut a cut;
- music: the track, its start and its level.

B3. **[human]** In the editor, trim one clip and turn one dissolve into a cut; export OTIO (or FCPXML, or an EDL) to
`$OUT/editor/edit-roundtrip.otio` (`mkdir -p "$OUT/editor"` first).

B4. **[auto]** Before importing, the agent publishes one more change, so the import must find the **exact earlier snapshot**:

```shell
python -m matchcut.edit "$WS" propose '{"ops": [{"op": "trim", "clip": "c005", "edge": "out", "frames": -2}]}'   # any small change
python -m matchcut.edit "$WS" apply '{"proposal": "<id>"}'
python -m matchcut.edit "$WS" publish
```

B5. **[auto]** Import the editor's file and check provenance and the diff:

```shell
python -m matchcut.pipeline.cli import "$WS" "$OUT/editor/edit-roundtrip.otio"
python tools/m3a_acceptance.py report "$WS" | tee "$OUT/report-B.txt"
```

The import prints `against edit: N human decisions`, which are the trim and the dissolve made a cut (not the agent's B4 change), and the
note `edit-roundtrip.otio was exported from an earlier edit ...`; `report` shows `human import: against edit`, the snapshot `edit-<12 hex>` of B1 with
`matches its sha256: True`, and the two decisions. **[human]** Confirm they are the two you made.

B6. **[auto]** A file without a fingerprint is refused until its base is named. Delete the EDL's `* MATCHCUT` comment lines (as most
editors do), import it, then name the base:

```shell
grep -v '^\* MATCHCUT' "$WS/exports/edit.edl" > "$OUT/editor/edit-nocomments.edl"
python -m matchcut.pipeline.cli import "$WS" "$OUT/editor/edit-nocomments.edl"; echo "exit $?"     # refused, exit 1
python -m matchcut.pipeline.cli import "$WS" "$OUT/editor/edit-nocomments.edl" --stage edit --base "$B1_SNAP"   # the B1 export's snapshot, by name
python -m matchcut.pipeline.cli import "$WS" "$OUT/editor/edit-roundtrip.otio"                    # bring the B3 human stage back
```

(An EDL straight from your editor has most likely lost the comments already: note whether it needed `--stage`/`--base`.)

B7. **[auto]** Stale stages. Re-run the pipeline last (it rewrites `final.json`):

```shell
python -m matchcut.pipeline refine "$SRC" -w "$WS" --keep-config
python -m matchcut.pipeline.cli junctions "$WS" | tail -4     # "note: edit.json is stale: ...", "note: human.json is stale: ..."
ls -l "$WS/edit.json" "$WS/edit_publish.json" "$WS/human.json" "$WS/human_import.json"   # all still there
python tools/m3a_acceptance.py verify "$SRC_WS" "$OUT/original-workspace.snapshot.json" --strict  # exit 0: not one file of the original changed
```

### B: pass / fail

B **passes** when the editor opens the `edit` export and it matches the published revision at the cuts checked (within the limits M2a's
fidelity report lists), the re-export imports back as exactly the editor's changes against the exact snapshot, the fingerprint-less file
is refused until `--stage`/`--base` name the base, and a pipeline re-run leaves `edit` and `human` out as stale without deleting them.

It **fails** on a wrong frame, transition or music placement in the editor at a checked cut, an import that guesses its base or reports
changes the editor did not make, or a deleted stage file. An editor that cannot import a timeline whose fidelity report is clean fails B
as an editor or format issue: record the editor, its version and the message; it is not evidence against A.

### B: results

Record the run: date, editor and version, format(s) used.

| Question | Answer |
|---|---|
| **[human]** The editor opens the `edit` export (OTIO / FCPXML / EDL; yes/no per format) | |
| **[human]** Frame timing at two edited cuts and the length match the published revision (yes/no) | |
| **[human]** Transitions (kinds, lengths, positions) match (yes/no) | |
| **[human]** Music arrives at the right start and level (yes/no) | |
| The re-export imports as exactly the editor's changes, against the exact B1 snapshot (yes/no) | |
| The fingerprint-less EDL is refused until `--stage`/`--base` (yes/no; did the editor's own EDL need them?) | |
| A pipeline re-run leaves `edit` and `human` out as stale, nothing deleted (yes/no) | |
| Notes: editor or format issues (recorded against B only) | |
| **Decision (B)** | accept interoperability / editor or format issue to fix |

## Known limitations

- The pipeline ignores `hold_transition`: a protected transition is honoured by edit sessions only. At a protected pin a proposal may
  set exactly the held transition (a repair) or leave it; `start` and `inspect` list mismatches with how to resolve them, and `publish`
  refuses one until it is repaired or the user approves `acknowledge_mismatches`. A protection released (`hold: false`), replaced or
  weakened after `start` is refused at publish the same way; `pin_transition` itself refuses such a change without `user_approved`.
- A relative source path in `config.json` resolves from the directory a command runs in (S4).
- A pin made in the TUI is not held by a running session until the pipeline runs again and the session is restarted from that new stage;
  until then the session fails closed at the pin's shots (it refuses every change to their clips and junctions).
- The workspace's stage lock and the session lock wait at most 30 seconds, then refuse.
- There are no persistent timing locks: guards are per proposal.
- A context change (config, shots, pins, music, source rate) requires proposing again.
- Only a session on the latest canonical stage can publish (the default base); a session started on `human` or an earlier stage says
  `can_publish: false`. The edit stage is never a session base.
- Publishing is refused while a fresh `human.json` exists that it would leave out as stale, unless `supersede_human` names that file's
  sha12 (only with the user's approval); `human.json` is never changed or deleted.
- Editors usually drop an EDL's `* MATCHCUT` comments, which carry its stage and fingerprint: once a workspace has an edit stage, such an
  EDL needs `--stage` and `--base` to import.
- An agent's edits are not carried across a pipeline re-run: start a new session on the new `final`.
- No MCP transport (M3b), no GUI or TUI editing, no editor automation.

More in [EDIT.md](EDIT.md#known-limitations).
