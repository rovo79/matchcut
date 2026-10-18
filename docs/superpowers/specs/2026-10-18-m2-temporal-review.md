# M2: Temporal editorial review — design

Status: proposed. Builds on M1 (`docs/superpowers/specs/2026-10-17-m1-junction-inspector.md`
and `docs/superpowers/specs/2026-10-18-m1-stabilization.md`). M1's human
acceptance gate stays open and is not affected by M2.

## Why

M1's verdict table asks whether "motion judgement [is] possible without
playback". It is not: three still frames and a blend cannot show whether a
cut lands on an action, whether a dissolve is too long, or whether the audio
jumps. M2 lets the editor **watch** a junction: a short clip a few seconds
either side of the cut, rendered with the real transition, timing and audio,
and an A/B clip that plays the first cut and the refined cut of the same
junction back to back. M2 does not try to play video inside the terminal; it
renders a file and hands it to an external player.

## Scope

In scope:

- a pipeline module that renders a junction preview from any stage timeline,
  through the existing `render.render()` filter graph (cuts, dissolves, dips,
  fades, source audio, music), cached on disk;
- an A/B preview: the junction as the first cut had it, a half-second of
  black, then the junction at the inspected stage, each labelled;
- an external player launcher;
- two keys in the Edit workbench, a one-line preview status, non-blocking
  generation and graceful failures;
- a `preview` CLI command;
- tests on synthetic footage, a demonstration artifact, an acceptance
  document.

Out of scope: playback inside the terminal; scrubbing or looping controls of
our own; rendering previews for every junction ahead of time; changing any
pass.

## Preview window

For a stage timeline `tl` and junction `j` (between clip `j` and clip `j+1`,
0-based, the same index `Junction.index` uses):

- `cut = tl.record_start(j + 1)`: the record time where the incoming clip
  starts (for a dissolve or dip, where the transition starts);
- `d` = the incoming clip's transition duration (0 for a cut);
- `start = max(0, cut - pre)`, `end = min(tl.duration, cut + d + post)`, with
  `pre = cfg.preview.pre_seconds` and `post = cfg.preview.post_seconds`
  (defaults 2.0 and 2.0).

The rendered clips are `first..last`:

- `first` starts at `j` and steps back while `record_start(first) > start`, and
  also while `start` falls inside `first`'s own incoming transition (it is a
  blend with the clip before it);
- `last` starts at `j + 1` and steps forward while the next clip starts before
  `end`.

The sub-timeline is a copy of those clips with the first clip's incoming
transition set to a cut, the timeline fade-in kept only when `first == 0`,
the fade-out kept only when `last` is the last clip, and an empty history.
The preview is that sub-timeline rendered, then cut to
`[start - record_start(first), end - record_start(first)]`. Music, when
configured, starts at `music.start + record_start(first)`, so it lines up with
the full render.

What a preview reproduces exactly: picture, transitions, the source audio and
its crossfades, fades at the ends of the cut, and music alignment.
Documented approximations: music loudness normalisation and ducking run on
the excerpt, not the whole programme, so loudness can differ slightly from
`final.mp4`; previews render at `cfg.preview.width` (default 640 px).

## Labels

When ffmpeg has the `drawtext` filter, each preview carries a label in the
top-left corner for its whole length, and a `CUT` tag next to it for 0.3 s
starting at `cut`. Label texts (ASCII only):

- single preview: `<stage>  shot <o> -> shot <i>`;
- A/B halves: `A  <stage>  shot <o> -> shot <i>` and `B  <stage>  shot <o> -> shot <i>`.

`cfg.preview.labels = False` turns labels off. If `drawtext` is missing, or a
labelled render fails, the preview is rendered without labels; that is not an
error.

## A/B

A is the junction at its baseline stage: the first stage, up to and including
the inspected one, where the two clips are adjacent (the same lineage rule as
the junction inspector: same shot, overlapping source range). B is the
junction at the inspected stage. The A/B file is A, 0.5 s of black (and
silence, when there is audio), then B. When A's stage is B's stage (the
junction first appears at the inspected stage), there is nothing to compare:
the result is B alone with the note
`nothing earlier to compare: this junction first appears at <stage>`.

## Cache

Previews live in `workspace/previews/`. A file name is
`<stage>_s<out>-s<in>_<key>.mp4` (A/B: `ab_<keyA>_<keyB>.mp4`), where `key`
is the first 16 hex digits of the SHA-1 of a canonical JSON document holding:
a preview format version, the sub-timeline's clips and fades, the window, the
source path with its size and mtime, `cfg.render.audio_bitrate`, the whole
`cfg.preview` section, the `cfg.music` section with the music file's size and
mtime when set, and the label text. An existing non-empty file with that name
is reused without running ffmpeg. Files are written to a temporary name in
`workspace/previews/.tmp/` and moved into place with `os.replace`, so an
interrupted or failed render never leaves a file that would be reused.
Nothing ever deletes previews automatically.

## Failures

All preview failures raise `preview.PreviewError` with a one-line message:

- `no timelines in <ws>: run the pipeline first`
- `no stage <name> in <ws>`
- `junction <n> is out of range 1..<m>` (n is 1-based in messages)
- `source footage not found: <path>` (from `probe.json`, or the config's source when `probe.json` is missing)
- `ffmpeg not found on PATH`
- `ffmpeg failed: <last non-empty line of its stderr>`

## Player

`matchcut/pipeline/player.py` (no Textual) chooses a command:

1. `cfg.preview.player` when set: split with `shlex`, the file path appended;
2. `mpv` on PATH: `mpv --force-window=yes --really-quiet <path>`;
3. `ffplay` on PATH: `ffplay -autoexit -loglevel error -window_title <file name> <path>`;
4. the system opener: `open` on macOS, `xdg-open` elsewhere, when on PATH;
5. none: not launched.

It starts the player detached (`subprocess.Popen`, stdin/stdout/stderr to
`DEVNULL`, `start_new_session=True`) and returns whether it launched plus a
message for the user. A configured player that fails to start (`OSError`) is
reported as not launched with the reason; there is no fallback from an
explicitly configured player.

## Edit workbench

Two keys while the Edit view has focus, shown in the footer:

- `p` **Play**: render (or reuse) the preview of the selected junction at the
  inspected stage, then play it.
- `a` **A/B**: render (or reuse) A and B and the A/B file, then play it.

One new line, `#preview-status`, sits directly under the three frames:

- idle: `p play this cut · a compare with <baseline stage>` (dim), or
  `p play this cut` when the junction first appears at the inspected stage;
- working: `rendering <label>…`;
- done: `playing <file name>` with ` (cached)` when no render ran;
  `preview saved: <path> (no video player found: set preview.player)` when
  nothing could be launched;
- failed: `preview failed: <message>` (red), and the same text as an error
  notification.

Generation runs in a Textual thread worker. The UI stays responsive: junction
and stage keys keep working while a preview renders, and a result arriving for
a junction that is no longer selected still plays (the editor asked for it)
but does not overwrite the status of the junction now selected. A second
request for the same preview while it is rendering is ignored with the
notification `already rendering this preview`. Previews of different
junctions may render concurrently.

## CLI

`python -m matchcut.pipeline.cli preview WORKSPACE --index N [--stage STAGE] [--ab] [--play]`

N is 1-based (as in `junctions --index`), the stage defaults to the last
stage. It prints `preview: <path>` (`cached` appended when reused), the note
when there is one, and with `--play` launches the player and prints its
message. Errors print to stderr and exit with 1.

## Configuration

A new `preview` section in `PipelineConfig`:

| key | default | meaning |
|---|---|---|
| `pre_seconds` | 2.0 | seconds shown before the cut |
| `post_seconds` | 2.0 | seconds shown after the transition |
| `width` | 640 | preview width in pixels |
| `crf` | 23 | x264 quality |
| `labels` | true | burn in labels and the CUT tag |
| `player` | "" | player command; empty = automatic |

## Acceptance

`docs/M2-ACCEPTANCE.md` holds the automated evidence and a human checklist:
play three junctions (including a dissolve and a junction a pass moved);
confirm the preview matches `final.mp4` at the same record time, picture and
sound; A/B shows the difference a pass made; the TUI stays responsive while
rendering; a missing player and a missing source give clear messages. The
verdict question: does watching a junction let the editor judge motion and
timing that the M1 stills could not?
