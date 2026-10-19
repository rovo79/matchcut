# M2b: Temporal editorial review — design

Status: approved direction (user, 2026-10-18), detailed here. Supersedes
`docs/superpowers/specs/2026-10-18-m2-temporal-review.md` (custom previews played by
an external player), which is kept for its window and render design, reused below.
Context: `docs/M2-INTEGRATION-FEASIBILITY.md` (why mpv, why not Resolve's MCP).
M1's human acceptance gate stays open and is independent of M2b.

## Goal

From the Edit workbench, an editor watches the moving sequence around the selected
junction — not stills and scores — and flips between the first cut and the refined
cut of the same junction at the same moment relative to the cut. Playback is in
**mpv**, driven over its JSON IPC socket. Transitions and fades are shown exactly by
a short ffmpeg render of the window; hard cuts play straight from the source through
an mpv EDL, with no rendering at all.

## Architecture

A new package `matchcut/review/` sits between the engine and the UI:

- it reads the engine's canonical stage timelines (`junctions.load_stage_timelines`),
  `probe.json` and `shots.json`; it never writes them;
- it never imports `textual` and the engine (`matchcut/pipeline/`) never imports it;
- mpv is an optional adapter: without mpv the same windows are rendered and handed
  to the system opener.

| module | responsibility |
|---|---|
| `review/window.py` | the window around a junction on a stage timeline, the sub-timeline, whether a window needs a render, the mpv EDL text of a window (pure) |
| `review/render.py` | ffmpeg render of a window through `pipeline.render.render`, trimmed; cache; A/B concat for players without IPC |
| `review/items.py` | `prepare_review(ws, cfg, stage, index, ab)`: the A and B items to play (EDL or rendered file), labels, cut offsets; errors as `ReviewError` |
| `review/mpv.py` | an mpv process and a stdlib JSON-IPC client |
| `review/controller.py` | `ReviewController`: play an item looping, toggle A/B keeping the offset from the cut, stop, status text; falls back to the system opener without mpv |
| `review/__main__.py` | `python -m matchcut.review WORKSPACE --index N [--stage S] [--ab] [--play]` |

## The window

Unchanged from the superseded spec. For stage timeline `tl` and junction `j` (between
clip `j` and `j+1`): `cut = tl.record_start(j + 1)`; `d` = the incoming transition's
duration (0 for a cut); `start = max(0, cut - pre)`, `end = min(tl.duration, cut + d + post)`
with `pre = cfg.review.pre_seconds`, `post = cfg.review.post_seconds` (2.0 / 2.0).
Rendered clips `first..last`: `first` steps back from `j` while `record_start(first) > start`
or `start` falls inside `first`'s incoming transition; `last` steps forward from `j+1`
while the next clip starts before `end`. The sub-timeline copies those clips with the
first cut in, keeps the timeline fade-in only when `first == 0` and the fade-out only
when `last` is the last clip, and has no history. `offset = start - record_start(first)`.

## Exact playback: EDL or render

A window **needs a render** when any of these holds: a clip after the first in the
sub-timeline has a non-cut incoming transition; the window overlaps the timeline's
fade-in (`first == 0 and fade_in > 0 and offset < fade_in`) or fade-out
(`last` is the last clip `and fade_out > 0 and end > tl.duration - fade_out`); music
is configured (`cfg.music.path`). Otherwise the window is hard cuts of the source and
plays from an **mpv EDL**:

```
# mpv EDL v0
<abs source path>,<start>,<length>,title=<stage> s<shot> <clip id>
...
```

one line per sub-timeline clip; the first clip's start is `source_in + offset`, the
last clip's length ends at the window end; numbers with 4 decimals; a source path
containing `,` `;` `\n` or `!` uses mpv's `%<bytes>%<path>` length escape. Record time
in the EDL equals record time in the window (hard cuts only), so `cut - start` is where
the incoming clip appears. Rendered windows use the superseded spec's render (render
the sub-timeline through `pipeline.render.render` at `cfg.review.width`, trim to the
window, music offset by `record_start(first)`), without burned-in labels (labels come
from mpv's OSD; the A/B concat for players without IPC keeps burned-in labels).

`cfg.review.always_render = True` forces renders (for players that cannot read EDL).

## Review items

`prepare_review(ws, cfg, stage, index, ab=False) -> ReviewPair(b, a=None, note="")`.

- **B** is the junction at `stage` (default: the last stage); **A** (only with `ab`)
  is the same junction at its baseline stage: the first stage up to `stage` where the
  junction's two clips are adjacent (`junctions.locate_junction`). If that is `stage`
  itself, `a` is None and `note` is `nothing earlier to compare: this junction first appears at <stage>`.
- Each `ReviewItem`: `stage, index, label, path, kind ("edl" | "render"), cut`
  (seconds from the item's start to the incoming clip's start), `duration`,
  `transition` (`cut` or `dissolve 0.50s` / `dip to black 0.40s`), `cached`.
- Labels (ASCII): single `"<stage>  shot <o> -> shot <i>  <transition>"`; A/B halves
  prefixed `"A  "` / `"B  "`.
- Files: `workspace/review/<stage>_s<o>-s<i>_<key>.edl|.mp4`, `key` = 16 hex digits of
  the SHA-1 of the canonical JSON of the sub-timeline clips and fades, the window,
  the source path with size and mtime, the `review` config section, the `music`
  section with the music file's size and mtime, and a format version. Existing
  non-empty files with that name are reused; renders and EDLs are written to
  `workspace/review/.tmp/` and moved into place with `os.replace`.
- Errors (`ReviewError`, one line): `no timelines in <ws>: run the pipeline first`;
  `no stage <name> in <ws>`; `junction <n> is out of range 1..<m>`;
  `source footage not found: <path>`; `ffmpeg not found on PATH`;
  `ffmpeg failed: <last non-empty stderr line>`.

## Playback (mpv)

`MpvSession` starts `mpv --idle=yes --force-window=yes --keep-open=yes --no-terminal
--hr-seek=yes --no-resume-playback --input-ipc-server=<socket> [extra args]` once and reuses it; a headless session adds
`--vo=null --ao=null` (tests) or caller-supplied `--vo=image …` (the demo). The client
speaks the documented JSON IPC: one JSON object per line, integer `request_id`, replies
matched by id, events skipped; every request has a timeout (default 2 s; loading a file
waits up to 10 s for `file-loaded`). The socket lives in a private temporary directory;
on Windows mpv control is not implemented: `start()` raises `MpvError` and the controller
falls back to the system opener (a named pipe would be the way to add it).

`ReviewController`:

- `play(item, from_cut=None)`: open the item AT its target: set the `start` property
  to `item.cut + from_cut` (0 when `from_cut` is None, clamped into the item), load it
  (`loadfile <path> replace`), wait for `file-loaded`, reset `start` to `none` (so a
  looping file wraps to 0), set `loop-file=inf`, unpause, and show the label for 2 s
  (`show-text`); remembers the item as current. (Loading and then seeking showed one or two frames
  of the window's start before the seek; per-file `loadfile` options are avoided because their
  argument position changed in mpv 0.38.)
- `play_pair(pair)`: plays A when there is one, else B; `toggle()` switches between
  A and B, reading `time-pos` of the current item and seeking the other to the same
  offset from its own cut (clamped into the item), and shows the other label.
- `stop()`: pause and keep the window (`keep-open`); with no live mpv it sets the status
  `mpv is not running` and returns False; `close()`: quit mpv.
- A failed mpv start is remembered only for 30 s (injectable clock) or until `review.mpv`
  changes; then it is tried again.
- On Linux mpv is started with `PR_SET_PDEATHSIG(SIGTERM)` (ctypes, from a thread that lives as
  long as the session, since the signal follows the forking thread), so it dies with the app even
  when the app is killed; elsewhere an orphan may remain. The user's `mpv.conf` and scripts are honoured.
- When mpv is missing or fails to start: the controller renders (forcing
  `always_render`) and opens the file with the system opener (`open` / `xdg-open`);
  for an A/B it renders the A/B concat file (A, 0.5 s black and silence, B, labels
  burned in). Status says which happened. No player at all: status names the file.
- Status text (one line): `playing B  final  shot 3 -> shot 4  dissolve 0.50s (rendered, cached)`,
  `playing A  roughcut  shot 3 -> shot 4  cut (exact source)`, `paused`,
  `mpv not found: opened <file> in the system player (no A/B toggle)`,
  `review failed: <message>`.

## Edit workbench

Keys while the Edit view has focus (footer): `p` **Play** (B = the inspected stage,
looping), `a` **A/B** (first press: prepare and play A; later presses on the same
junction toggle A/B at the same offset from the cut), `s` **Pause**. One line
`#review-status` under the frames shows the controller's status, or when idle
`p play · a compare with <baseline stage>` (or `p play` when the junction first
appears at the inspected stage). Preparation and every IPC call run in a Textual
thread worker; junction and stage keys keep working meanwhile; a second request for
the same item while it is being prepared is ignored with `already preparing this junction`;
a result for a junction no longer selected still plays (the editor asked for it) but
does not overwrite the selected junction's status. The controller belongs to the app,
is created on first use and closed when the app exits.

## Configuration

`PipelineConfig.review` (new section): `pre_seconds 2.0`, `post_seconds 2.0`,
`width 0` (the source resolution; non-zero scales rendered windows), `crf 23`, `mpv "mpv"` (the binary), `always_render false`.

## Verification

Automated (deterministic, offline, synthetic footage; tests needing mpv skip without it):
window and EDL maths; EDL plays in mpv with the expected duration and chapter starts;
rendered dissolve window blends mid-transition; audio present when the source has it;
cache reuse without ffmpeg; A/B toggle keeps the offset from the cut (real mpv,
headless); fallback without mpv; errors leave no files; TUI keys, status, non-blocking
preparation and duplicate suppression.

Demonstration (headless): `tools/demo_review.py` drives the real controller against
mpv with `--vo=image`, playing A then toggling to B around a junction with a dissolve
and one with a hard cut, and builds contact sheets of the frames mpv actually
displayed; plus an Edit-tab screenshot with the review status. Committed as images.

Human acceptance (`docs/M2B-ACCEPTANCE.md`, pending a desktop): watch three junctions
in a real mpv window, toggle A/B, compare with `final.mp4`, judge whether motion and
timing can be assessed — the question M1's verdict table left open.

## Amendments after the whole-branch review

- Items are opened at their target (`start` property), never loaded and then seeked: no frame from before the target is shown after a toggle.
- A window that stops mid-cut renders its music with a 1 ms fade-out (`render.NO_FADE_SECONDS`; `afade` with `d=0` falls back to a default length) and the probed music length; only a window reaching the last clip keeps the cut's fade. `loudnorm` on a short excerpt can pump (documented).
- Rendered windows are derived from the frame-snapped stage timeline, so `cut` and `offset` are frame-exact; EDL windows keep the unsnapped timeline. The cache key covers the snapped sub-timeline and whether the window ends the cut.
- `workspace/review/.tmp/` files older than an hour are removed when a window is prepared.
- The `s` key is labelled Pause. `python -m matchcut.review`: Enter after the window was closed plays the pair again; unexpected data errors print `review failed: <type>: <message>` and exit 1.
- Rendered and EDL windows have the same pixel size: `review.width` defaults to 0 (the source resolution; renders use `RenderConfig(width=None)`), and the session sets mpv's `auto-window-resize` to `no` over IPC after its first file has loaded (once per session; an mpv without the option is tolerated). A real window therefore neither resizes on an A/B toggle nor shows a scaled render at a different size; measured under Xvfb with `--vo=x11` (scaled renders resized the window without the setting, never with it); a real desktop window stays on the human checklist.
