# M3a acceptance: can an agent edit a real cut through the CLI alone?

M3a adds the edit core, `matchcut.edit`: a frame-native, transactional and reversible way to edit a cut through a JSON CLI
(`python -m matchcut.edit WS COMMAND '{...}'`). An agent proposes operations, every rule is checked on the whole result, previews are
rendered by matchcut's own renderer, accepted proposals become immutable revisions, and a finished revision is published as the
workspace's `edit` stage, which M2a exports to an editor like any stage. How it works: [EDIT.md](EDIT.md). Design:
[superpowers/specs/2026-11-01-m3a-edit-core.md](superpowers/specs/2026-11-01-m3a-edit-core.md).

## Status

- **Automated verification: pass** (see below). It proves the operations, rules, store, renders, publish and the `edit` stage's
  round trip through matchcut's own readers; it does not prove that an agent can do useful editorial work with the CLI, that the renders
  look right, or that an editor opens the `edit` export as intended.
- **M3a human acceptance: PENDING.** Nobody has yet watched an agent session edit a real workspace through the CLI only, watched its
  previews, or opened its `edit` export in an editor.
- **M2a, M2b, M2c and M1 human gates: PENDING** ([M2A-ACCEPTANCE.md](M2A-ACCEPTANCE.md), [M2B-ACCEPTANCE.md](M2B-ACCEPTANCE.md),
  [M2C-PROBE.md](M2C-PROBE.md), [M1-ACCEPTANCE.md](M1-ACCEPTANCE.md)). This gate leans on M2a's: if an editor cannot open M2a's exports,
  step 7 below fails for the same reason.

## Automated verification

Offline and deterministic: synthetic footage (ffmpeg's test pattern at 25 and 30000/1001 fps), small hand-built workspaces and a mock-backend
pipeline run for the TUI; no network, no model, no editor. All under `tests/edit/` unless named:

| Module | What it proves |
|---|---|
| `test_ops.py`, `test_rules.py`, `test_model.py` | The nine operations in frames, refusals in one line, the proposal-wide rules: shot bounds and minimum length, transition kinds, whole frames and limits, pins (clips, adjacency and cut points fixed; the transition free unless protected), match cuts, only new violations counted, the timing report and the guards |
| `test_properties.py`, `test_scenarios.py` | Random edit sequences on demo cuts: untouched clips and junctions identical in frames, the running time changing exactly by the trims and overlaps, every pin and match-cut invariant held, no proposal adding a violation; the prototype proof's scenarios re-expressed (pins, match cuts, the first clip, transition limits, 29.97 fps) |
| `test_session.py`, `test_session_pipeline.py`, `test_session_export.py` | The store: transactions, apply re-validation (a tampered proposal), a moved head, two processes under the lock, undo/redo/checkout over branches, staleness and the move-aside, deterministic revisions, r000 on the frames M2a's export uses |
| `test_view.py`, `test_view_render.py` | `inspect`, renders and previews checked with identifiable media (frames exact, audio present and aligned), `frames` images, render names, the `renders/.tmp` sweep (a file another process holds locked is never swept, whatever its age) |
| `test_tools.py`, `test_cli.py` | The tool table and its schemas, exit codes 0/1/2 and one JSON object on stdout in every case (SIGTERM included), `@file` and `@-`, and that the engine, the review package, the interchange package and the TUI never import `matchcut.edit` |
| `test_publish.py` | Publish: `r000` refused; the record, the immutable copy, the history (the latest canonical stage's plus one `agent` decision per change); the same session replacing `edit.json` with earlier copies kept; another session refused, then moved aside with `replace`, a second move-aside never overwriting the first; `final`, the pass files and `human*.json` byte-identical; a session on an earlier stage or on `human` refused; `edit` never a session base; a stale `edit` for each cause (latest canonical stage, base stage, a later canonical stage, a missing, unreadable or foreign record, a hand-edited `edit.json`) left out and never deleted; the agent's decisions attributed on junctions the edit made by removal, insertion and move; the pin badge on the edit stage; the `junctions` CLI listing `edit` and saying when it is stale |
| `test_edit_interchange.py` | `export --stage edit` in OTIO, FCPXML and EDL at 25 and 29.97 fps with fidelity, fingerprints and snapshot; never exported by default; a stale or missing edit stage refused; a re-import diffed against the exact snapshot after a later publish (every format, both rates) with the record (`stage: "edit"`, the snapshot, the sha256 of `edit.json` and of the latest canonical stage, the history stage); the human stage stale when `edit.json` or the latest canonical stage changes; the refusals (unresolvable fingerprint, no fingerprint, ambiguous base, `--stage` alone) and `--stage`/`--base`; a canonical import continuing the edit history; an end-to-end run through the CLIs only (start, propose, apply, publish, export, re-import, `--base`) |
| `tests/tui/test_edit.py` | The Edit view lists a published `edit` stage after `final` and lands on it; a stale one is left out with its reason in the header; the TUI watches `edit.json` and its record |
| `tests/pipeline/test_junctions.py`, `tests/interchange/` | Every junction and interchange test from M2a stays green: canonical stages behave as before |

## Human checklist: PENDING

Needs a desktop with ffmpeg and a video player, an agent that can run shell commands (a coding agent), and an editor
for step 7 (DaVinci Resolve free or Studio, or Final Cut Pro). Write what you see in the Verdict.

**Preparation.**

1. Make a workspace from a real run, with music if you can:
   `python -m matchcut.tui footage.mp4 -w ws_m3a --backend anthropic` (or `python -m matchcut.pipeline run footage.mp4 -w ws_m3a --music track.mp3`).
   Check `python -m matchcut.pipeline.cli junctions ws_m3a` lists `N junctions in final`. Optionally pin one junction in the Edit view (`4`,
   then `l`), so the session meets a pin.
2. Note the hashes of what must never change: `sha256sum ws_m3a/final.json ws_m3a/roughcut.json ws_m3a/passes/*.json ws_m3a/editorial.json > before.sha`.

**The agent session (CLI only).** Give the agent [EDIT.md](EDIT.md) and a brief, for example: *"Tighten the first minute by about two
seconds without moving any cut that lands on a downbeat, soften one hard cut that jars, and publish. Use only
`python -m matchcut.edit ws_m3a ...`; look at frames and previews before you apply anything."* Do not touch the workspace yourself.

3. The agent runs `start`, `inspect`, then works in rounds of `propose` (with `guards` where the brief asks for them), `frames` and
   `preview`, `apply`, and `undo` where a result is not what it wanted. Watch the transcript:
   - every command printed one JSON object; refusals named the rule, the clip or the junction, and the agent could act on them;
   - frame counts, junction ids (`s1-s3`) and clip ids stayed consistent across commands;
   - nothing outside the CLI was used to change the workspace.
4. **Watch the previews.** Open two or three `window-*.mp4` files the agent previewed (under `ws_m3a/edits/renders/`) and one full
   `render` of the head. Does each show the change the proposal described (the trim, the dissolve, the move), with the sound in place and in
   sync? Compare one junction with the same junction in `final.mp4`.
5. The agent runs `publish`. Check:
   - `python -m matchcut.pipeline.cli junctions ws_m3a` now reads `N junctions in edit (... stages)`, and
     `--index K` on an edited junction says why (`agent: ...` or `made adjacent by edit: ...`);
   - the TUI's Edit view (`4`) lands on `edit` and `[`/`]` steps between `final` and `edit`; a pinned junction still shows its pin;
   - `sha256sum -c before.sha` passes: `final`, the passes and the pins are untouched (only `editorial.json` may differ, and only if the
     agent ran `pin_transition`).
6. Ask the agent to publish once more after one more edit: `edit.json` is replaced, `edits/published/` holds both copies.

**To an editor and back.**

7. `python -m matchcut.pipeline.cli export ws_m3a --stage edit` (add `--resolve-script` for Resolve Studio). Open `exports/edit.otio` (and
   `exports/matchcut.fcpxml`, project `edit`) in the editor. Does the timeline match the agent's last published revision: the clips,
   the cut points (compare a frame at two edited cuts with the agent's render), the dissolves' lengths and positions, the music?
8. In the editor trim one clip and turn one dissolve into a cut; re-export OTIO (or FCPXML, or an EDL). Before importing, have the agent
   publish one more change. Then `python -m matchcut.pipeline.cli import ws_m3a FILE`. Are the human decisions exactly the two you made
   (not the agent's later change), and does the report say it was diffed against an earlier `edit`? Rename the EDL's title, import it again,
   and check it is refused until you add `--stage edit --base edit-<12 hex>` (the snapshot's name in `exports/.stages/`).
9. **Stale handling.** Re-run the passes (`python -m matchcut.pipeline refine ...`, or `R` in the TUI). Does `junctions` say
   `edit.json is stale` (and `human.json is stale`) and show the new `final`? Are `edit.json` and `human.json` still on disk?

## Pass / fail

The gate **passes** when all of these hold:

- the agent completed the brief through the CLI alone, without a traceback, a hand edit or a command whose output it could not parse;
- every preview watched showed the change its proposal described, with audio present and in sync;
- after `publish`, the `edit` stage is listed and explained in `junctions` and the TUI, and `final`, the passes and the rough cut are
  byte-identical (`sha256sum -c` passes);
- the editor opens `edit.otio` or the `edit` FCPXML project and it matches the published revision at the cuts checked (within the
  limits M2a's fidelity report lists);
- the editor's re-export imports back as exactly the editor's changes, against the exact snapshot, and the fingerprint-less file is refused
  until `--stage`/`--base` name the base;
- a pipeline re-run leaves the `edit` stage out as stale and deletes nothing.

It **fails** on any wrong frame in a preview or an export at an edited cut, a lost or changed canonical file, a silent overwrite of an
`edit.json`, an import that guesses its base, or an agent that could not get through the brief because a refusal or a report was unclear
(write down which).

## Verdict

Record the run here (date, footage, agent, editor and version), and attach the transcript and the files you watched.

| Question | Answer |
|---|---|
| The agent finished the brief through the CLI alone (yes/no) | |
| The refusals and timing reports were clear enough to act on (yes/no, examples) | |
| The previews showed the proposed changes, with audio in sync (yes/no) | |
| `junctions` and the TUI list and explain the `edit` stage; canonical files untouched (yes/no) | |
| The editor opens the `edit` export and it matches the published revision (yes/no, per editor) | |
| The editor's re-export imports against the exact snapshot; the fingerprint-less file is refused until named (yes/no) | |
| A pipeline re-run leaves `edit` out as stale, nothing deleted (yes/no) | |
| Notes: what was slow, unclear or missing | |
| Decision | accept M3a / change the approach |

## Known limitations

- The pipeline ignores `hold_transition`: a protected transition is honoured by edit sessions only.
- There are no persistent timing locks: guards are per proposal.
- A context change (config, shots, pins, music, source rate) requires proposing again.
- Only a session on the latest canonical stage can publish; the edit stage is never a session base.
- An agent's edits are not carried across a pipeline re-run: start a new session on the new `final`.
- No MCP transport (M3b), no GUI or TUI editing, no editor automation.

More in [EDIT.md](EDIT.md#known-limitations).
