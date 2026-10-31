# Editing API prototype (feasibility only)

A small, deterministic editing API over matchcut's own canonical timeline, with a JSON CLI and a minimal MCP server on top. It exists to
answer one question: can an agent edit a matchcut cut iteratively through structured operations, reusing the pipeline's own timeline,
locks and FFmpeg renderer, without another editing engine? The answer and the recommendation are in
[docs/EDITING-API-FEASIBILITY.md](../../docs/EDITING-API-FEASIBILITY.md).

Nothing under `matchcut/` changes. The prototype reads a workspace through matchcut's modules and writes only `WS/edits/`. Its one other
write is `edit_publish`, which writes the human stage (`human.json`, `human_import.json`) through M2a's own builder.

## Files

| File | What it is |
|---|---|
| `editapi.py` | The model: `EditSession` (revisions, propose/apply, undo/redo/checkout, compare, render, publish) and `Ops` (the operations and their rules) |
| `tools.py` | One table of 11 tools (name, description, JSON Schema, call), shared by both transports |
| `cli.py` | `python cli.py WS TOOL '{json}'`: JSON in, JSON out |
| `mcp_server.py` | `python mcp_server.py WS`: MCP over stdio (newline-delimited JSON-RPC 2.0), no SDK, no dependencies |
| `proof.py` | The proof: deterministic fixtures and 69 checks |
| `recorded-run/` | `proof.md` and `proof.json` from the run on 2026-10-31 (work paths replaced by `<work>`) |

## Run the proof

```
python prototypes/editing_api/proof.py [--work DIR]
```

It needs ffmpeg and the repository's Python dependencies. No model, network or GPU is needed: the pipeline runs with the mock backend.
It takes about 11 s and writes only into `--work`, which defaults to a folder in the system temp directory.

## Operations

All operations are in whole frames at the source's exact rate.

- **Addressing:**
  - Clips are named by id. Ids are stable for the whole session: new clips get `e001`, `e002`, … and nothing is renumbered.
  - A clip can also be named by its instance key (`clip_origin`).
  - Junctions are named by their 1-based number or their id (`s1-s3`).

| Op | Arguments | Effect |
|---|---|---|
| `trim` | `clip`, `edge` (in/out), `frames` | Moves one edge. Later clips ripple. |
| `roll` | `junction`, `frames` | Moves a cut. The outgoing out-point and the incoming in-point move together, so nothing ripples. |
| `slip` | `clip`, `frames` | The same length, showing other frames of the same shot. |
| `replace` | `clip`, `shot`, optional `in_frame` / `frames` | Another shot in the clip's place: by default the same length, from the middle of the shot. The clip keeps its incoming transition. |
| `insert` | `shot`, `in_frame`, `out_frame`, `before` / `after` | Adds a new clip. |
| `remove` | `clip` | Removes a clip. |
| `move` | `clip`, `before` / `after` | Moves a clip. |
| `set_transition` | `junction`, `kind` (cut/dissolve/dip_to_black), `frames` | Changes the transition at a junction. |
| `snap_to_beat` | `junction`, optional `mode` (roll/ripple), `window_s` | Moves the cut onto the nearest music beat. |

## Rules

A proposal is all or nothing.

- Clips stay inside their shot.
- No clip may become shorter than `pacing.min_clip_seconds`. A clip that the pipeline already left shorter may still grow.
- Transitions must fit:
  - no longer than `transitions.max_fraction_of_clip` of the shorter clip;
  - M2a's `layout` must accept them.
- Locks:
  - An edge held by an editor pin or a match cut never moves.
  - A held pin is never broken.
  - A match cut stays a hard cut.
  - A pinned junction keeps its transition.
- Clips that no operation names must come out unchanged. This is checked, not assumed.
