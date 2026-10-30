# FilmCraft feasibility proof

A throwaway probe, not an adapter. It answers one question: can FilmCraft (an open-source Rust NLE with a
CLI, an MCP server and a JSON control channel) serve as an optional visual editor for a matchcut cut and hand
the edit back through M2a's `import`? The findings are in [docs/FILMCRAFT-FEASIBILITY.md](../../docs/FILMCRAFT-FEASIBILITY.md).

Nothing here is imported by matchcut, and nothing changes matchcut's files. The scripts call matchcut only
through its public CLI (`python -m matchcut.pipeline.cli run | export | import`), and they import every
editor file into a **copy** of the workspace.

| File | What |
|---|---|
| `proof.py` | The headless proof. It drives `filmcraft-cli mcp` (MCP over stdio) and `filmcraft-cli` (one process per command); no window, no GPU |
| `control_channel.py` | The desktop app (`filmcraft --control PORT`) under Xvfb: the JSON-lines control channel and the MCP bridge |
| `fcclient.py` | Minimal stdlib clients: MCP JSON-RPC on stdio, and the control channel over TCP |
| `recorded-run/` | The run recorded in the report: `proof_results.md/.json`, `control_channel_results.md/.json` (scratch paths replaced by `$WORK`, `$APPWORK`, `$SCRATCH`), and two screenshots from the app under Xvfb |

## Requirements

- matchcut's own requirements, and `ffmpeg`/`ffprobe` (6.1 was used). `numpy` and `Pillow` are used for the frame and sample checks.
- A FilmCraft build. FilmCraft was tested at commit `fed0f42` (0.14.0), cloned shallow, unmodified, with Rust 1.97.0:

  ```sh
  git clone --depth 1 https://github.com/storytold/filmcraft
  cd filmcraft
  CARGO_TARGET_DIR=/path/to/target cargo build --release -p filmcraft-cli   # headless: CLI + MCP server
  CARGO_TARGET_DIR=/path/to/target cargo build --release -p filmcraft       # desktop app, only for control_channel.py
  ```

  On Ubuntu 24.04 the CLI needed nothing extra. The desktop app needed `libasound2-dev` to build. At run time it
  needed `libxkbcommon-x11-0`, without which winit panics at start-up. `mesa-vulkan-drivers` gives it a Vulkan
  software adapter (llvmpipe), and `xvfb` gives it a display.

## Run

From the root of this checkout:

```sh
# Headless proof: fixtures, matchcut runs, FilmCraft import / navigate / trim / render / export, matchcut import.
python prototypes/filmcraft/proof.py \
    --filmcraft-cli /path/to/target/release/filmcraft-cli \
    --filmcraft-src /path/to/filmcraft \
    --work /tmp/fc-proof
# -> /tmp/fc-proof/results/results.md (exit 1 if any check Failed)

# Desktop app + control channel + MCP bridge (needs the proof's workspace from the run above).
python prototypes/filmcraft/control_channel.py \
    --filmcraft /path/to/target/release/filmcraft \
    --filmcraft-cli /path/to/target/release/filmcraft-cli \
    --workspace /tmp/fc-proof/ws_music --work /tmp/fc-app --xvfb
# -> /tmp/fc-app/results.md, screenshots, app.log
```

`proof.py` takes about a minute on 4 CPUs once the fixtures exist (about 30 s more the first time). It reuses
the fixtures and workspaces under `--work`. Delete that directory to rebuild them.

`control_channel.py` starts the app with a fresh `FILMCRAFT_CONTROL_TOKEN` and `--control-input-only`. HOME and
the XDG directories point into `--work`, so the app's preferences, autosaves and logs never touch yours. The app
still tries its update check at launch, a request to GitHub; in the container the proxy refused it.

## What the proof does

1. **Fixtures.** matchcut's synthetic demo footage (H.264/AAC, 320x180, 25 fps), a 120 bpm click track (WAV, plus
   MP3 and M4A copies), a copy with an embedded 01:00:00:00 timecode, and 29.97 DF (start 00:59:59;00) and 23.976
   variants. Each gets `run --backend mock -t 8` (the music one with `--music click.wav`), then `export --stage final`.
   The result is 5 clips, a dissolve at junction 2 (8 frames) and at junction 3 (18 frames), and junction markers `J<n> s<out>->s<in>`.
2. **MCP**: `initialize` and `tools/list` on the headless server, then `command_list`.
3. **Import** of `exports/final.otio` with `file.import`. Clips, source ranges, dissolves, the music track and the
   markers are compared with matchcut's OTIO.
4. **Navigate**: `playhead.set` to marker J2, `timeline.selectClipAtPlayhead`, then `markers.goNext` through the junctions.
5. **Trim** c003's in point (the clip after junction 2) by 5 frames, as a ripple trim. The V1/A1 pairs are first
   linked and A2 (music) is taken out of sync lock: the import carries no links, and FilmCraft refuses a ripple
   that would break sync. Undo and redo follow.
6. **Preview**: `render_frame` (full size: `max_side: 0`) is compared with ffmpeg's decode of the expected source
   frames and with matchcut's `final.mp4`. A dissolve frame is fitted as a mix of its two source frames.
   `file.exportMedia` then renders H.264.
7. **Round trip**: `file.exportInterchange` to OTIO, FCPXML and EDL, then `cli import` of each into a copy of
   the workspace, checked against `final.json` frame for frame. Then the transitions (add, change, remove), a
   slip and a replaced shot.
8. **Untouched round trips** at 25 fps, with a timecoded source, at 29.97 DF and at 23.976, in all three formats.
9. **Audio**: with A1 muted, a WAV export before and after the trim, for WAV, MP3 and M4A music. Click onsets
   are compared with the source file at sample level, and the level is measured.
10. **CLI route**: `filmcraft-cli --save-as p.fcproj import`, `run -` (JSON lines) and `render`, without MCP.
11. **Media**: `filmcraft-cli probe` of every fixture (FilmCraft's own demuxers).
12. **Resources**: timings and the peak RSS of the child processes.

Outcomes use the probe's words: **Passed**, **Failed**, **Unsupported**, **Not preserved**, **Not tested**.
