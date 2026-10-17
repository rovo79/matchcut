# Control-room TUI

`python -m matchcut.tui footage.mp4 [-w workspace] [-b brief] [-t seconds] [--music track.mp3] [--backend mock]`

A terminal app that drives and watches pipeline runs and inspects the
workspace they produce. Install with `pip install -r requirements-tui.txt`
on top of the pipeline requirements. Open an existing workspace read-only
with `python -m matchcut.tui -w footage_edit`.

## Views

| Key | View | What it shows |
|---|---|---|
| `1` | Run | the stage ladder (pending / cached / running / done / waiting / failed), the live log of events and decisions (`f` cycles the filter), and the checkpoint panel |
| `2` | Shots | the shot catalog; selecting a row shows head / middle / tail frames and Claude's description; `u` toggles the shot's usable flag (written to `descriptions.json` and seen by a redo in the same run) |
| `3` | Story | logline, theme, beats as cards; with music, the energy curve with beat-change markers |
| `4` | Cut | the timeline as coloured spans by beat with transition glyphs (`\|` cut, `~` dissolve, `#` dip, `L` locked match cut) and the beat grid; `←`/`→` select a clip, `[`/`]` step through the rough cut and each pass |
| `5` | Config | the live config as a tree; Enter edits a value (Esc cancels), which applies to the next run or redo |

Global keys: `R` run, `S` stop (aborts a pending checkpoint), `o` open the
current image or render in the system viewer, `?` help, `q` quit (twice while
a run is active).

`o` opens the latest render (`final.mp4`, else `final_proxy.mp4`) from the Run
view, the highlighted shot's middle frame from Shots, and the selected clip's
first frame from Cut. On other views it says there is nothing to open.

## Checkpoints

A run pauses after describe, narrative, rough cut and every pass. The Run view
shows the stage's summary and three choices: `c` continue, `r` redo that stage
with the note typed in the field (type the note, press Enter or Esc to leave
the field, then `r`), `a` abort. A redo on describe, narrative or rough cut
clears the downstream caches and re-runs with the note added to the prompt as a
director's note; a redo on a deterministic pass only matters together with a
config change. Config edits made while paused are saved on the redo.

## Images

Frames render with the terminal's image protocol (Kitty, iTerm2, Sixel via
textual-image) and fall back to half-block mosaics elsewhere; a notice when the
first frame appears, and the status bar, say which mode is active. Set
`MATCHCUT_TUI_IMAGES=halfcell` to force the fallback.

## How it fits together

`matchcut/tui/bridge.py` runs `Pipeline.run()` on a thread and forwards its
events through a queue; the app applies them to `RunState` 20 times a second
and the views redraw from it. Checkpoints block the pipeline thread until the
Run view answers. Nothing in `matchcut/pipeline/` imports the TUI.

## Tests

`python3 -m pytest tests/tui` runs the TUI tests, including one SVG snapshot
per view (`tests/tui/__snapshots__/`). The snapshots show frames from the
generated demo footage, so they are tied to one ffmpeg build; regenerate with
`python3 -m pytest tests/tui/test_snapshots.py --snapshot-update` when they
drift.
