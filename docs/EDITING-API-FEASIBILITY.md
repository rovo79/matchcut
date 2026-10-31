# Agent-native iterative editing on matchcut's own timeline: feasibility

**Status: experiment, run once (2026-10-31) in a headless Linux container.**

The question: can an agent inspect a matchcut cut and change it iteratively through small, structured, deterministic operations, using
matchcut's canonical timeline and its FFmpeg renderer, without a new GUI, a new NLE or a second editing engine? And can those operations be
offered through an editing API and eventually MCP?

The answer comes from a working prototype and a proof: [prototypes/editing_api/](../prototypes/editing_api/README.md).

- Nothing under `matchcut/` changed.
- **Human gates M1, M2b, M2a and M2c: PENDING** (unchanged).
- **Labels used below:**
  - **Observed** means the proof ran it here; the step is named.
  - **Inferred** means read from the code, not run.

## Verdict

**Feasible, and a better fit than any external editor.** About 690 lines of prototype on top of matchcut's existing modules gave an agent the whole loop:

- inspect the cut and the unused footage;
- propose operations;
- see the exact changes, or the exact reason for a refusal;
- apply, undo, branch and compare revisions;
- render the whole cut or one junction;
- hand the result back to the rest of matchcut as the human stage.

Every step ran on matchcut's own `Timeline`, pins, frame arithmetic, renderer and M2a builder. Nothing was translated.

- **What the proof showed (observed, 69 checks):**
  - 67 Passed;
  - 1 confirmed gap (an edited cut cannot be exported to an NLE yet);
  - 1 failure caused by a **defect in matchcut's existing renderer**, reproduced without the prototype (below).
- **Speed:**
  - proposals and applies take about 2 ms;
  - a render of the 7-second demo cut takes about 0.8 s.
- **Determinism:** the same edits on a fresh copy produced byte-identical revisions.

**Recommendation:**
- Fix the renderer defect first, as its own small change.
- Then build the editing API inside matchcut as `matchcut/edit/`, with a JSON CLI (milestone M3a below).
- Add MCP afterwards as a thin second transport over the same tool table. It adds discoverability, not capability.

## What already exists and was reused

The prototype adds sessions, revisions, addressing and rule checking. Everything else is matchcut code that was already there.

| Need | Existing code the prototype used |
|---|---|
| The cut, in seconds and in frames | `pipeline/timeline.py` (`Timeline`, `Clip`, `clip_origin`); `interchange/common.py` (`layout`, `to_frames`, `export_timeline`, `junction_ids`) |
| Locks | Editor pins: `pipeline/editorial.py` (`pin_outcome`, `pin_site`). Match-cut locks and tags: `passes.is_match_cut` |
| Shot bounds, minimum length, transition limits | `shots.json`; `PipelineConfig.pacing` and `.transitions` |
| Music beats | `pipeline/audio.py` (`MusicAnalysis.nearest_beat`). The semantics come from `BeatSyncPass` |
| Story beats, shot descriptions | `narrative.json`, `descriptions.json` |
| Render the cut, render one junction | `pipeline/render.render`; `review/render.render_window` with `review_window` (M2b) |
| Hand the edit back to matchcut | `interchange/workspace.human_stage` (M2a's builder), `IMPORT_SCHEMA`, `junctions.stale_human` |

matchcut already has an edit vocabulary, in the polish pass (`PolishPass._apply_edit`): trim and extend head/tail, drop, move, and set a transition, with lock checks. It is not usable by an agent as it stands:

- it works in seconds;
- it names clips by ids that are renumbered after every pass;
- it lives inside an LLM pass.

The prototype's operations are frame-native versions of the same ideas. They add `roll`, `slip`, `replace`, `insert` and `snap_to_beat`.

## The proof

**Fixtures:**
- synthetic demo footage (8 shots, 25 fps) and a click track;
- the pipeline run with the mock backend;
- junction 1 pinned the way the TUI's Edit view does it;
- the passes re-run, so the canonical `final` carries a real editor lock.

**Run it:** `python prototypes/editing_api/proof.py` (about 11 s; no model, network or GPU).

**Recorded run:** [recorded-run/proof.md](../prototypes/editing_api/recorded-run/proof.md).

| Area | What was observed |
|---|---|
| Inspect (step 2) | **Clips:** source and record frames, transitions and locks. **Junctions:** id, record frame, music-beat offset in frames, whether pinned, beat change. **Shots:** every shot with its description, and which clips use it (unused: 2, 4, 5). **Story beats:** target against actual length. |
| Refusals (2b) | 11 refusals, each with its reason and no revision made. **Locks and pins:** trimming, rolling, beat-snapping or changing the transition of the pinned junction; removing or moving a pinned clip (the pin would break). **Limits:** a roll past the end of the shot; a trim below the minimum length; a dissolve longer than 40% of the shorter clip (step 5). **Requests:** a batch with one bad operation (refused whole), an unknown clip, an unknown op, and applying a refused proposal. |
| Trim and roll (3) | `roll junction 3 +3f` changed exactly c003's out-point and c004's in-point, with the running time unchanged. Every other clip came out identical (checked field by field). |
| Replace (4) | `replace c004 with shot 4` made `e001`, the same length and from the middle of the shot, and kept c004's incoming dissolve. |
| Transitions (5) | A 6-frame dissolve at junction 4 started c005 **6 frames earlier on the record**, so the cut is 6 frames shorter. In matchcut's model a transition overlaps its two clips. |
| Music beats (6) | **Snap:** after the roll, junction 3 was 3 frames late, and `snap_to_beat` rolled it back onto the beat. No other junction moved, because a roll ripples nothing. **Off the beat:** the new dissolve had pulled junction 4 off its beat (step 5). **Unreachable:** junction 2 cannot reach its beat, since its outgoing clip already ends at the end of its shot (refused). |
| Batch (7) | An insert (shot 5) and a dip to black, in one proposal. |
| Undo, redo, branches (8) | **Head:** undo and redo; `checkout`. **Branches:** an edit after an undo made a branch, r006 from r004, and r005 was kept. That edit was a move, a remove and a slip in one proposal, each reported. **Concurrency:** a proposal made before the head moved was refused ("propose again"). |
| Compare (9) | r000 → r004 reports only the replace and the new dissolve: the roll and its beat snap cancel out. |
| Render (10) | **Frames:** the full render has exactly the cut's frame count (181). **Frame accuracy:** each moving clip shows its exact source frames, checked on three frames per clip against offsets −2..+2 at full size. Three clips were decisive; the other three were static test shots. **Junction preview:** a junction's review window renders (M2b's code). **Audio:** the r005 render lost its audio (see Findings 1). r004 has audio as long as its video. |
| Publish (11) | **Human stage:** the head became the workspace's human stage (not stale), and the editor pin is still applied there. `matchcut.pipeline junctions` lists it. **Export:** `matchcut export --stage human` refuses it (Findings 4). |
| MCP and CLI (12) | **MCP:** `initialize` and `tools/list` (11 tools). `edit_inspect` over MCP returned exactly what the Python call returns. A refusal carries its reason, and an error is a tool result with `isError`. **CLI:** the JSON CLI served the same tools. |
| Stale (13) | **Proposals:** re-running the passes (without the pin) changed `final.json`. The session saw it and refused to propose, and the published human stage turned stale by the engine's own rule. **New session:** it moved the old one aside, kept, and started again at r000. |
| Determinism (14) | The same edit script on a fresh copy of the workspace produced the same revision hashes, and every check passed again. |

### Performance and limits (observed in this container; 4 CPUs, ffmpeg 6.1.1)

- **Session commands:**
  - `start` takes 1.5 ms, `inspect` 0.8 ms, a `propose` 1.7 ms and an `apply` 0.9 ms;
  - each revision is a small JSON file (6 to 9 KB here, mostly the pipeline's decision history). A full timeline per revision is cheap at matchcut's scale (tens to hundreds of clips).
- **Renders:**
  - the full 7.2 s cut at source size (320×180) takes 0.83 s;
  - one junction window takes 0.89 s;
  - a cached render takes 0.5 ms.
  - Render time grows with the cut's length and resolution. Real footage was not measured.
- **MCP:** 5 requests, including starting the server, took 0.22 s.
- **Not measured:**
  - real camera footage;
  - long cuts;
  - variable frame rate;
  - more than one source file (matchcut cuts one source).

## Findings

1. **Defect in matchcut's existing renderer (observed in step 10, then reproduced with `matchcut.pipeline.render.render` alone).**
   - **What happens:** when a dissolve or a dip to black joins two clips whose source ranges are out of order (the incoming clip comes from earlier in the file than the outgoing one), the rendered file has **no audio stream**. In a longer chain, the audio stops early: 5.5 s of audio under 7.24 s of video in r005.
   - **Cause:** ffmpeg's `acrossfade` between two `atrim` branches of the same input outputs nothing when the second range is earlier in the file. A hard cut (`concat`) and the video `xfade` are not affected.
   - **The fix:** opening the source once per clip (one `-i` per segment) fixed it in a direct test. An explicit `asplit` did not.
   - **Who is affected:** the mock backend keeps shots in source order, so the existing tests never reach this. Real model cuts do reorder shots, and so does any agent edit. **Fix this before anything else**, with a regression test.
2. **Transitions ripple (observed in steps 5 and 6).**
   - **What happens:** in matchcut's model a dissolve overlaps its two clips. Adding one makes every later cut start earlier by its length, which can take them off the music's beats. Changing a transition is therefore not local to its junction.
   - **What the API does:** it says so. The proposal reports the running time before and after, and `inspect` reports each junction's beat offset.
   - **Not decided:** whether a transition change should be absorbed automatically (for example by rolling the next junction). That is an open design question.
3. **Identity is exact inside matchcut.**
   - **No matching needed:** within a session, clip ids never change, so the change report is computed by id. M2a's matching by source range is not needed; it exists because NLEs drop matchcut's ids.
   - **Where M2a's diff falls short (observed in step 11):** M2a's diff of the same edit is less precise. It read the two new transitions on c005 as trims ("in +3f, out −3f"), because c005's neighbours had changed.
   - **Ids change on publish:** M2a's builder renames the agent's new clips `e001`, `e002` to `h001`, `h002`. The clip keys are kept.
4. **The human stage is one slot, and it cannot be exported (observed in step 11).**
   - **Collision:** publishing an agent's revision writes the same `human.json` that `matchcut import` writes from an NLE, so the two would replace each other.
   - **No export:** M2a deliberately refuses to export the human stage. An agent-edited cut therefore cannot go to Resolve or another NLE through `export` today.
   - **A decision is needed:** either a separate `edit` stage with its own export path, or allowing `export` of the human stage.
5. **A pipeline re-run invalidates a session (observed in step 13).**
   - **Safe by design:** the session refuses stale work and keeps it aside. Nothing is lost silently.
   - **No carry-over:** the agent's edits are not carried into the new cut. Today the only channel from human decisions into the pipeline is pins.
   - **A later option:** a session could turn its kept junctions into pins before a re-run. Not built.
6. **Locks protect cut points, not transitions.**
   - **Pins:** a pin stores the two cut points. The prototype also refuses transition changes at a pinned junction, a conservative choice that needs a product decision.
   - **Match cuts:** they stay hard cuts, as in the polish pass.
7. **The pipeline can leave a clip under its own minimum length (observed).**
   - **Where:** c005 is 18 frames against a 19-frame minimum, after the beat-sync pass.
   - **What the API does:** it tolerates such a clip, letting it grow but never shorten. It does not treat the pipeline's own output as invalid.
8. **Story beats are reported, not edited.**
   - `inspect` gives each story beat's target and actual length.
   - There is no "fit this beat to N seconds" operation. `PacingPass._fit` could be reused for it.
9. **Agents need to see as well as edit (inferred).**
   - The proof checked frames numerically.
   - A real agent loop needs a frame tool that returns the out and in frames of a junction as images, for a vision model. The frame cache and the M1 inspector's images already exist.

## Does MCP offer a meaningful advantage?

- **What MCP adds:**
  - the same 11 tools, with their JSON Schemas, to any MCP client (Claude Desktop, other agents) without a shell;
  - typed arguments and discoverable tools.
- **What it does not add:**
  - any capability, rule or safety the API lacks. Both transports are thin layers over one tool table: the MCP server is 59 lines and the CLI 36.
- **Compared with FilmCraft:** the value of the FilmCraft route depended on its editor. Here the tools edit the canonical timeline directly, so there is nothing to translate and no metadata to lose.
- **Compared with an agent editing the timeline JSON directly:** the API gives what raw JSON edits cannot:
  - frame-exact arithmetic;
  - locks and pins enforced;
  - all-or-nothing proposals with exact change reports;
  - a guard against a moved head;
  - undo history;
  - staleness detection;
  - render and publish.
- **Recommendation:** ship the API and the JSON CLI first. Coding agents such as Claude Code can use the CLI directly. Add MCP when the tool set has settled (M3b), ideally with the official SDK.

## Recommended architecture

```
agent ─┬─ JSON CLI  (python -m matchcut.edit WS TOOL '{...}')
       └─ MCP stdio (later)               ── thin transports over one tool table
                    │
          matchcut/edit/
            ops.py      pure: (Timeline, op) -> Timeline, or Refused(reason); frame-native
            rules.py    shot bounds, minimum length, transition limits, locks, pins, match cuts, untouched-clip check
            session.py  WS/edits/: immutable revisions, proposals (hashed), head, undo/redo/checkout, staleness, concurrency
            view.py     inspect (clips, junctions, shots, story and music beats), compare by id, frames for vision
            publish.py  revision -> human stage via M2a's builder (or a separate edit stage, see decision 1)
                    │ reads only, plus WS/edits/ and the publish target
          existing: pipeline.timeline, editorial, interchange.common, pipeline.render, review.render, junctions
```

**Rules for the package:**
- **Dependencies:** the pipeline engine never imports `matchcut.edit`.
- **Writes:** `matchcut.edit` never writes a canonical stage file.
- **Revisions:** each one is a whole timeline, not a diff, so any revision can be rendered, compared or published on its own.
- **One vocabulary:** later, the polish pass could call the same operations.

## Missing capabilities, by priority

| Priority | Capability |
|---|---|
| P0 | Fix the renderer's audio defect (Finding 1), with a regression test. |
| P0 | Frame-native operations and rules in `matchcut/edit/`, with unit tests and property tests (untouched clips, running-time arithmetic, lock invariants). |
| P1 | The session store: revisions, propose/apply, undo/redo/checkout, compare, staleness. |
| P1 | A frame tool for vision models (junction out/in frames as images). |
| P1 | A decision on where an agent's edit lives, and how it reaches an NLE (Finding 4). |
| P2 | Carry an agent's edits across a pipeline re-run, e.g. as pins (Finding 5). |
| P2 | Story-beat fitting; ripple-free transition changes (Findings 2 and 8). |
| P2 | Music placement operations (start offset, gain). The render supports them; no operation does. |
| Later | MCP transport (M3b); a TUI view of the edit head (the Edit view already shows a published human stage). |

## Proposed next milestone: M3a, the edit core

**Before M3a**, a separate small change: fix the renderer audio defect, with its regression test.

**Scope.** These tasks are each reviewed, then the whole branch is reviewed:
1. `matchcut/edit/ops.py` and `rules.py`: the nine operations and the rules, frame-native and pure. Unit tests reuse the proof's scenarios and add property tests.
2. `session.py`: revisions, proposals, undo/redo/checkout, compare and staleness, with concurrency tests.
3. `view.py` and the JSON CLI `python -m matchcut.edit`:
   - inspect, compare, render (whole cut and junction window) and junction frames as images;
   - the tool table and the CLI.
4. Publishing (per decision 1), docs (`docs/EDIT.md`) and an acceptance kit.

**Out of scope:**
- MCP;
- a TUI editing UI, or any GUI;
- exporting revisions to an NLE (unless decision 1 says otherwise);
- carrying edits across re-runs;
- an LLM agent loop inside matchcut;
- a second renderer.

**Acceptance:**
- **Automated:** the proof's scenarios become tests.
- **Human gate (PENDING until run):** you, or Claude Code on your behalf, edit a real workspace through the CLI only, and you watch the renders.

**Decisions needed before M3a starts:**
1. Where a published agent edit lives: the human stage (shared with NLE imports) or a separate `edit` stage, and whether it can be exported.
2. Whether a pinned junction may change its transition.
3. Whether a transition change should keep the later cuts in place (absorbing the overlap) or ripple, as now.
