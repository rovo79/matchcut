# M2b Temporal Editorial Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From the Edit workbench, play the moving sequence around the selected junction in mpv (exact source ranges through an EDL for hard cuts, a short ffmpeg render where a transition or fade must be seen), and flip between the first cut and the refined cut at the same offset from the cut — non-blocking, reusing the canonical stage timelines.

**Architecture:** A new package `matchcut/review/` (no Textual; the engine never imports it) computes the window around a junction, writes an mpv EDL or renders the window through the unchanged `pipeline.render.render`, and drives one mpv process over its JSON IPC socket through a `ReviewController`. The TUI's Edit view calls the controller from a thread worker. Without mpv, windows are rendered and opened in the system player.

**Tech Stack:** Python 3.9+ syntax (runs on 3.13), ffmpeg 6, mpv 0.37 (JSON IPC, EDL v0, `--vo=image` for the headless demo), Textual 8.2.8, pytest, pytest-textual-snapshot; synthetic footage from `matchcut.pipeline.demo.make_demo_footage`.

**Spec:** `docs/superpowers/specs/2026-10-18-m2b-temporal-review.md` (binding). It reuses the window and render design of `docs/superpowers/specs/2026-10-18-m2-temporal-review.md` (superseded) and the reference code in `docs/superpowers/plans/2026-10-18-m2-temporal-review.md` (superseded plan; tasks below point at exact line ranges in it to copy and adapt).

## Global Constraints

- `matchcut/review/` never imports `textual`, `textual_image` or `matchcut.tui`; `matchcut/pipeline/` never imports `matchcut.review`. The TUI may import both.
- Python 3.9 compatible syntax: modules start with `from __future__ import annotations`; `Optional`, `List`, `Dict`, `Tuple`, `Sequence` from `typing`; no `X | None` at runtime.
- No new pip dependencies. mpv and ffmpeg are external binaries; tests that need mpv skip when `shutil.which("mpv")` is None (it is installed in this container).
- `pipeline.render.render()` and `build_filter_graph()` are reused unchanged; `final.mp4` rendering is untouched. The canonical stage timelines are read, never written.
- A review failure never crashes the app or the CLI: it is a `review.items.ReviewError` (or `review.mpv.MpvError`, caught by the controller) with a one-line message.
- Partial files are never reused: write under `workspace/review/.tmp/`, then `os.replace`.
- Every mpv IPC request has a timeout; no thread may block forever; no mpv process may outlive the app, the CLI or a test (tests `close()` in `finally`).
- Both suites must pass after every task (`python3 -m pytest tests/pipeline -q`, `python3 -m pytest tests/tui -q`), plus the new `python3 -m pytest tests/review -q`. Counts at the start are in the controller's dispatch.
- Never put a model identifier in code, docs or commit titles. Commit with `git -c user.name="Claude" -c user.email="noreply@anthropic.com" commit -F <msgfile>` (message file in `/tmp/claude-0/-home-user-matchcut/b10444db-d5c0-5098-b105-b181b23029e7/scratchpad/`), ending with a `Co-Authored-By:` line carrying your own environment's attribution, then `Claude-Session: https://claude.ai/code/session_01TQzWuD79py68nDqZHf24SZ`. Push with `git push -u origin claude/ai-video-editing-pipeline-bkqqay` after each task. Stage files by explicit path. Never stage `.superpowers/`. Never rewrite history.
- Automated verification is never described as human acceptance; M1's and M2b's human gates stay pending.

---

## File structure

| File | Responsibility |
|---|---|
| `matchcut/pipeline/config.py` | new `ReviewConfig` section (`review`) |
| `matchcut/pipeline/junctions.py` | public `locate_junction` wrapper around `_locate` |
| `matchcut/review/__init__.py` | package docstring only |
| `matchcut/review/window.py` | `ReviewError`, `Window`, `review_window`, `sub_timeline`, `music_start`, `needs_render`, `window_edl` (pure) |
| `matchcut/review/render.py` | `render_window`, `render_ab` (ffmpeg), cache helpers |
| `matchcut/review/items.py` | `ReviewItem`, `ReviewPair`, `review_key`, `prepare_review` |
| `matchcut/review/mpv.py` | `MpvError`, `MpvSession` (process + JSON IPC client) |
| `matchcut/review/controller.py` | `ReviewController`, `open_with_system` |
| `matchcut/review/__main__.py` | the `python -m matchcut.review` CLI |
| `matchcut/tui/views/edit.py`, `matchcut/tui/app.py`, `matchcut/tui/styles.tcss` | `p` / `a` / `s`, `#review-status`, the app-owned controller |
| `tools/demo_review.py` | headless demonstration: frames mpv displayed, contact sheets |
| `tests/review/` (new: `conftest.py`, `test_window.py`, `test_items.py`, `test_mpv.py`, `test_controller.py`) | Tasks 1-3 |
| `tests/tui/test_review_ui.py` | Task 4 |
| `docs/M2B-ACCEPTANCE.md`, `docs/TUI.md`, `docs/PIPELINE.md`, `README.md` | Task 5 |

---

### Task 1: Review configuration, window, sub-timeline and EDL

**Files:**
- Modify: `matchcut/pipeline/config.py`
- Create: `matchcut/review/__init__.py`, `matchcut/review/window.py`
- Create: `tests/review/__init__.py` (empty), `tests/review/test_window.py`
- Snapshot: regenerate `tests/tui/__snapshots__/test_snapshots/test_view_snapshots[5-config].svg` (the Config view lists the new section); no other snapshot may change.

**Interfaces — produces:**
- `config.ReviewConfig(pre_seconds=2.0, post_seconds=2.0, width=640, crf=23, mpv="mpv", always_render=False)`; `PipelineConfig.review`.
- `window.ReviewError(RuntimeError)`.
- `window.Window` (frozen dataclass): `junction, first, last, start, cut, end, offset`, property `duration` — same fields and semantics as `PreviewWindow` in the superseded plan.
- `window.review_window(tl, junction, pre, post) -> Window` (same algorithm as `preview_window`).
- `window.sub_timeline(tl, w) -> Timeline`; `window.music_start(tl, w, music_start_seconds) -> float`.
- `window.needs_render(tl, w, cfg) -> bool` — the spec's rule (non-cut transition inside the sub-timeline after its first clip; window overlapping the fade-in or fade-out; `cfg.music.path` set; `cfg.review.always_render`).
- `window.window_edl(tl, w, source_path) -> str` — the spec's EDL text (raises `ReviewError("window needs a render: <reason>")` when `needs_render` is true for reasons other than `always_render`).

- [ ] **Step 1: Tests.** Copy the pure tests of the superseded plan's Task 1 (`docs/superpowers/plans/2026-10-18-m2-temporal-review.md`, the test module in Task 1 Step 1, lines 53-165) into `tests/review/test_window.py`, renaming `PreviewConfig`→`ReviewConfig` (defaults: `pre_seconds=2.0, post_seconds=2.0, width=640, crf=23, mpv="mpv", always_render=False`), `preview_window`→`review_window`, `PreviewWindow`→`Window`, `PreviewError`→`ReviewError`, `cfg.preview`→`cfg.review`, and dropping the cache-key test (it moves to Task 2). Keep every expected number. Then add:

```python
def test_a_hard_cut_window_needs_no_render_and_becomes_an_edl():
    w = review_window(CUTS, 1, 2.0, 2.0)  # clips 1 (10-13) and 2 (20-23); start 4.0, cut 6.0, end 8.0, offset 1.0
    cfg = PipelineConfig()
    assert not needs_render(CUTS, w, cfg)
    assert window_edl(CUTS, w, "/media/src.mp4") == (
        "# mpv EDL v0\n"
        "/media/src.mp4,11.0000,2.0000,title=final s1 c002\n"
        "/media/src.mp4,20.0000,2.0000,title=final s2 c003\n"
    )


def test_a_window_with_a_dissolve_or_a_fade_or_music_needs_a_render():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 0.5), (2, 20.0, 23.0, "cut", 0))
    cfg = PipelineConfig()
    assert needs_render(t, review_window(t, 0, 1.0, 1.0), cfg)        # the dissolve is inside
    assert not needs_render(t, review_window(t, 1, 0.5, 0.5), cfg)    # junction 1 is a hard cut, the dissolve is outside the window
    faded = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "cut", 0), fade_in=1.0)
    assert needs_render(faded, review_window(faded, 0, 2.5, 0.5), cfg)      # starts at 0.5, inside the 1.0 s fade-in
    assert not needs_render(faded, review_window(faded, 0, 1.5, 0.5), cfg)  # starts at 1.5, after the fade-in
    with_music = PipelineConfig()
    with_music.music.path = "/m.wav"
    assert needs_render(CUTS, review_window(CUTS, 1, 1.0, 1.0), with_music)
    forced = PipelineConfig()
    forced.review.always_render = True
    assert needs_render(CUTS, review_window(CUTS, 1, 1.0, 1.0), forced)


def test_window_edl_refuses_a_window_that_needs_a_render():
    t = tl((0, 0.0, 3.0, "cut", 0), (1, 10.0, 13.0, "dissolve", 0.5))
    with pytest.raises(ReviewError, match="window needs a render: dissolve"):
        window_edl(t, review_window(t, 0, 1.0, 1.0), "/s.mp4")


def test_window_edl_escapes_a_path_with_special_characters():
    w = review_window(CUTS, 1, 1.0, 1.0)
    text = window_edl(CUTS, w, "/media/a,b;c.mp4")
    path = "/media/a,b;c.mp4"
    assert f"%{len(path.encode('utf-8'))}%{path},12.0000" in text
```

(In the EDL test, check the arithmetic before relying on it: the window starts 1.0 s into clip 1, so its first line is source 11.0 for 2.0 s; the last line is clip 2's first 2.0 s. If your reading of the spec gives different numbers, stop and report — do not change the expectation silently.)

- [ ] **Step 2: Run** `python3 -m pytest tests/review/test_window.py -q` — failures expected.

- [ ] **Step 3: Implement.** `ReviewConfig` in `config.py` (registered after `render`, like the superseded plan's `PreviewConfig`, with the fields above and one comment per field). `window.py`: copy and adapt the superseded plan's Task 1 implementation (lines 167-300: `PreviewError`, `PreviewWindow`, `_incoming`, `preview_window`, `sub_timeline`, `music_start`; leave out `preview_key` and `_stat`), then add:

```python
def _fade_overlap(tl: Timeline, w: Window) -> str:
    if w.first == 0 and tl.fade_in > 0 and w.offset < tl.fade_in - _EPS:
        return "fade-in"
    if w.last == len(tl.clips) - 1 and tl.fade_out > 0 and w.end > tl.duration - tl.fade_out + _EPS:
        return "fade-out"
    return ""


def _render_reason(tl: Timeline, w: Window, cfg: PipelineConfig) -> str:
    for c in tl.clips[w.first + 1 : w.last + 1]:
        if not c.transition_in.is_cut:
            return c.transition_in.kind.replace("_", " ")
    fade = _fade_overlap(tl, w)
    if fade:
        return fade
    if cfg.music.path:
        return "music"
    return ""


def needs_render(tl: Timeline, w: Window, cfg: PipelineConfig) -> bool:
    return bool(_render_reason(tl, w, cfg)) or cfg.review.always_render


def _edl_path(path: str) -> str:
    return f"%{len(path.encode('utf-8'))}%{path}" if any(ch in path for ch in ",;\n!") else path


def window_edl(tl: Timeline, w: Window, source_path: str) -> str:
    """mpv EDL of a hard-cut window: one segment per clip, trimmed to the window (record time == window time)."""
    reason = _render_reason(tl, w, _NO_MUSIC)  # music and always_render never block an EDL; needs_render decides
    if reason:
        raise ReviewError(f"window needs a render: {reason}")
    path = _edl_path(source_path)  # callers pass an absolute path
    lines = ["# mpv EDL v0"]
    t = w.start
    for k in range(w.first, w.last + 1):
        c = tl.clips[k]
        clip_start = tl.record_start(k)
        a = c.source_in + max(0.0, t - clip_start)
        length = min(c.source_out, c.source_in + (w.end - clip_start)) - a
        if length <= _EPS:
            continue
        lines.append(f"{path},{a:.4f},{length:.4f},title={tl.stage or 'cut'} s{c.shot_index} {c.id}")
        t = clip_start + (a - c.source_in) + length
    return "\n".join(lines) + "\n"
```

with `_NO_MUSIC = PipelineConfig()` defined after the imports (so `window_edl` ignores music and `always_render`: the caller decides). Regenerate the Config snapshot as described and check that only `[5-config]` changed.

- [ ] **Step 4: Run** `python3 -m pytest tests/review -q`, then both full suites.
- [ ] **Step 5: Commit and push** (title: "Add the review config and the window, sub-timeline and EDL for junction review").

---

### Task 2: Render windows and prepare review items

**Files:**
- Create: `matchcut/review/render.py`, `matchcut/review/items.py`
- Modify: `matchcut/pipeline/junctions.py` (add `locate_junction`)
- Create: `tests/review/conftest.py`, `tests/review/test_items.py`

**Interfaces:**
- Consumes: Task 1; `pipeline.render.render`, `render.snap_to_frames`; `media.MediaInfo`, `media.probe`, `media.MediaError`, `media._require`, `media.run`; `shots.load_shots`; `junctions.load_stage_timelines`.
- Produces:
  - `junctions.locate_junction(timeline, a_ref, b_ref) -> Optional[int]` (public wrapper of `_locate`).
  - `render.render_window(tl, w, media, cfg, out_path: Path, label: str = "") -> None` — render the sub-timeline (snapped to frames with the shot bounds passed by the caller) via `pipeline.render.render` with `RenderConfig(width=cfg.review.width, crf=cfg.review.crf, preset="veryfast", audio_bitrate=cfg.render.audio_bitrate)` and music offset by `music_start`, then trim to the window with a second ffmpeg pass (`trim`/`atrim` filters, as in the superseded plan's `_trim_and_label`), burning in `label` only when given (the A/B concat uses labels; mpv items do not), writing to temporary files under `out_path.parent / ".tmp"` and `os.replace` into `out_path`; MediaError → `ReviewError` via the superseded plan's `_ffmpeg_error` rules.
  - `render.render_ab(a: "ReviewItem", b: "ReviewItem", out_path, has_audio, cfg) -> None` — A, 0.5 s black (and silence with audio), B; same ffmpeg recipe as the superseded plan's `render_ab`; inputs must be rendered files.
  - `items.ReviewItem` dataclass: `stage, index, label, path: Path, kind ("edl"|"render"), cut: float, duration: float, transition: str, cached: bool`.
  - `items.ReviewPair` dataclass: `b: ReviewItem, a: Optional[ReviewItem] = None, note: str = ""`.
  - `items.review_key(sub, w, source, cfg, kind) -> str` (16 hex, per the spec; adapt the superseded plan's `preview_key`, replacing `cfg.preview` by `cfg.review`, adding `kind`).
  - `items.prepare_review(ws, cfg, stage=None, index=0, ab=False, force_render=False) -> ReviewPair` (stage None = last stage; index 0-based).
  - `items.ab_file(pair, ws, cfg) -> Path` — for players without IPC: ensures both items are rendered (re-preparing EDL items with `force_render=True`), then `render_ab` into `workspace/review/ab_<keyA>_<keyB>.mp4` (cached).

`prepare_review` rules (binding, from the spec): load stages; errors exactly as the spec lists; media from `probe.json` (else `cfg.source` probed; missing file → `source footage not found: …`); bounds from `shots.json` (None if missing); for B and (with `ab`) A at the baseline stage found with `locate_junction` over the stages before `stage`; if none, `a=None` and the note; for each item: window from `cfg.review.pre/post_seconds`; `kind = "render" if force_render or needs_render(...) else "edl"`; file `workspace/review/<stage>_s<o>-s<i>_<key>.<edl|mp4>`, reused when it exists and is non-empty (`cached=True`); `cut = w.cut - w.start`; `duration = w.duration`; `transition` = `cut` or e.g. `dissolve 0.50s` / `dip to black 0.40s` (the incoming clip's); labels per the spec.

- [ ] **Step 1: Tests** — `tests/review/conftest.py`: a session `demo_video` fixture (copy the one in `tests/pipeline/conftest.py`) and a `silent_video` fixture (`make_demo_footage(..., audio=False)`). `tests/review/test_items.py`: adapt the superseded plan's Task 2 tests (lines 315-395) to this API — the helper section (`two_shots`, `duration`, `has_audio_stream`, `rgb`, `small_cfg` with `cfg.review.width = 160`), and:
  1. a hard-cut junction prepares an **EDL** item (no ffmpeg runs: monkeypatch `render.render` to fail if called); the EDL file's text equals `window_edl(...)`; `cut == 1.0`, `duration == 2.0`, `transition == "cut"`;
  2. the same junction with `force_render=True` renders an mp4 of ≈2.0 s (±0.1), blue before the cut and red after (the superseded plan's test 1 assertions);
  3. a dissolve junction renders (kind `render`) and blends mid-transition (the superseded plan's test 2, with its corrected arithmetic: cut 0.9, duration ≈ 2.4, mid-transition at 1.2);
  4. a silent source renders with no audio stream;
  5. a second identical call returns `cached=True` and runs no ffmpeg; changing `cfg.review.pre_seconds` gives a different path;
  6. `ab=True` on a workspace whose `roughcut.json` has the junction as a hard cut and `final.json` with a dissolve: `pair.a.kind == "edl"`, `pair.b.kind == "render"`, labels start with `A  roughcut` / `B  final`; a junction that first appears at `final` gives `a is None` and the note;
  7. `ab_file(pair, ws, cfg)`: the result's duration ≈ `a.duration + 0.5 + b.duration` (±0.2) and the frame 0.25 s after A ends is black;
  8. every error message in the spec's list, each leaving no `.mp4`/`.edl` under `workspace/review` (rglob);
  9. music offset: with `cfg.music.path` set (a small WAV made with `make_click_track`), a hard-cut window is rendered (kind `render`), and with `render.render` monkeypatched to record its `music` argument, `music.start == cfg.music.start + record_start(first)` while `cfg.music.start` is unchanged.
- [ ] **Step 2: Run** — failures expected.
- [ ] **Step 3: Implement** — adapt the superseded plan's Task 2 reference implementation (lines 396-640: `_has_drawtext`, `_ffmpeg_error`, `_label_filter`, `_trim_and_label`, `_tmp`, `_cached`, `render_preview`→`render_window`, `render_ab`, `_media`, `_bounds`, `build_preview`→`prepare_review`) into `render.py` / `items.py` per the interfaces above. The EDL is written the same way as a render (temp file + `os.replace`). Add `locate_junction` to `junctions.py` exactly as the superseded plan shows. Check for import cycles (`review` imports `pipeline`, never the reverse).
- [ ] **Step 4: Run** `python3 -m pytest tests/review -q`, then both full suites.
- [ ] **Step 5: Commit and push** (title: "Prepare junction review items: exact EDL windows and rendered transitions").

---

### Task 3: mpv session, review controller and the review CLI

**Files:**
- Create: `matchcut/review/mpv.py`, `matchcut/review/controller.py`, `matchcut/review/__main__.py`
- Create: `tests/review/test_mpv.py`, `tests/review/test_controller.py`

**Interfaces — produces:**
- `mpv.MpvError(RuntimeError)`.
- `mpv.MpvSession(binary="mpv", headless=False, video_args=None)` with `start()`, `command(*args, timeout=2.0)`, `get(name, timeout=2.0)`, `set(name, value, timeout=2.0)`, `load(path, timeout=10.0)` (sends `loadfile path replace` and waits for the next `file-loaded` event; an `end-file` event with `reason: "error"` first → `MpvError("mpv could not open <path>")`), `alive() -> bool`, `close()`. `video_args=None` means `["--vo=null", "--ao=null"]` when headless else `["--force-window=yes"]`; a list replaces them (the demo passes `--vo=image …`).
- `controller.ReviewController(cfg, session_factory=MpvSession, opener=open_with_system)` with `has_mpv() -> bool` (starts the session lazily; False when mpv is missing or fails to start; never raises), `play(item, from_cut=None)`, `play_pair(pair)`, `toggle() -> bool`, `stop()`, `close()`, `status: str`, `current: Optional[ReviewItem]`. Thread-safe (one `threading.Lock` around session use).
- `controller.open_with_system(path) -> Tuple[bool, str]` — `open` on macOS, `xdg-open` elsewhere, detached (`start_new_session=True`, DEVNULL streams); messages `opened <file> in the system player` / `no player found: the file is <path>`.

Implementation notes (binding):
- Unix only: the socket is `<tempfile.mkdtemp(prefix="matchcut-mpv-")>/ipc.sock`; on Windows `start()` raises `MpvError("mpv control is not supported on Windows yet")` (the controller then falls back). Spawn `mpv --idle=yes --keep-open=yes --no-terminal --input-ipc-server=<sock> <video_args>` with DEVNULL streams; connect with retries for up to 5 s; a daemon reader thread splits lines, routes `{"request_id": n, ...}` replies to waiting callers (a `threading.Condition` + dict) and appends events to a bounded deque that `load()` waits on. Requests: `{"command": [...], "request_id": n}`; a reply with `error != "success"` → `MpvError("<command>: <error>")`; no reply within the timeout → `MpvError("<command>: no reply from mpv")`. `close()`: send `quit` (ignore errors), wait 2 s, then kill; remove the temp dir; idempotent; also registered with `atexit`.
- `play(item, from_cut)`: `load(item.path)`; `set("loop-file", "inf")`; `command("seek", item.cut + from_cut if from_cut is not None else 0, "absolute+exact")` (clamped to `[0, item.duration - 0.05]`); `set("pause", False)`; `command("show-text", item.label, 2000)`; `status = "playing <label> (<'exact source' if edl else 'rendered'><', cached' if cached>)"`. Without mpv: if the item is an EDL, status `mpv not found: …` must still give the editor something to watch — the controller cannot render (it has no workspace); so the TUI/CLI call `prepare_review(..., force_render=True)` when `has_mpv()` is False, and `play()` then opens the rendered file with the opener, status `mpv not found: opened <file> in the system player (no A/B toggle)`.
- `play_pair(pair)`: remember the pair; play `pair.a` if present else `pair.b`. `toggle()`: needs a pair with both items and mpv; reads `time-pos` (`get`), computes `offset = time_pos - current.cut`, plays the other item with `from_cut=offset`; returns False (status unchanged) when there is nothing to toggle.
- `stop()`: `set("pause", True)`, status `paused`.

CLI `python -m matchcut.review WORKSPACE --index N [--stage S] [--ab] [--play] [--render]` (N 1-based): loads `config.json` if present else defaults; `prepare_review` (with `force_render=--render`); prints one line per item: `<A|B> <kind> <path> cut=<s> duration=<s> <transition>` (+ ` cached`), and the note; with `--play`: a `ReviewController`, `play_pair`, then reads stdin lines — empty line toggles A/B, `s` pauses, `q` quits — and closes the controller on exit or EOF; errors → stderr, exit 1. Without mpv `--play` uses the fallback (and for `--ab`, `items.ab_file`).

- [ ] **Step 1: Tests.**
  - `test_mpv.py` (skip all when mpv is missing; every test closes its session in `finally` and asserts `not s.alive()` afterwards):
    1. a headless session loads an EDL window from `prepare_review` on the demo footage, `get("duration")` equals the item's duration (±0.05), and `get("chapter-list")` has one entry per EDL line with titles from the EDL;
    2. `loop-file=inf` + seek: after seeking to `cut - 0.5` and playing for `duration` + 0.5 s of wall time, `time-pos` is again below `duration` (it looped);
    3. `load()` of a missing file raises `MpvError` matching `could not open`;
    4. an unknown property `get("no-such-property")` raises `MpvError`;
    5. `close()` twice is fine, and a session whose process was killed reports `alive() is False` and `command()` raises `MpvError` (no hang: the test has a 10 s pytest timeout guard via a thread or `signal.alarm`).
  - `test_controller.py`:
    1. with real mpv (skip if missing): `play_pair` of an A/B pair (A EDL, B rendered) plays A; after seeking A to `a.cut + 0.3`, `toggle()` makes `time-pos` of B ≈ `b.cut + 0.3` (±0.1) and `current is pair.b`; toggling back returns to A at the same offset;
    2. with a fake session factory raising `MpvError`: `has_mpv()` is False, `play(rendered_item)` calls the opener with the item's path and the status starts `mpv not found:`; `toggle()` returns False;
    3. the status strings for EDL/rendered/cached items exactly as the spec words them;
    4. the CLI: `main([ws, "--index", "1"])` prints one `B edl …` line and exits 0; `--ab` prints A and B lines; `--index 9` exits 1 with `junction 9 is out of range` on stderr.
- [ ] **Step 2: Run** — failures expected.
- [ ] **Step 3: Implement** per the notes above.
- [ ] **Step 4: Run** `python3 -m pytest tests/review -q` (check that no mpv process remains: `pgrep -x mpv` prints nothing), then both full suites.
- [ ] **Step 5: Commit and push** (title: "Drive mpv over IPC for junction review, with A/B at the same offset from the cut").

---

### Task 4: Review keys in the Edit workbench

**Files:**
- Modify: `matchcut/tui/views/edit.py`, `matchcut/tui/app.py`, `matchcut/tui/styles.tcss`
- Create: `tests/tui/test_review_ui.py`
- Snapshot: regenerate only `test_view_snapshots[4-edit].svg`.

**Interfaces:**
- Consumes: `review.items.prepare_review`, `ReviewPair`, `ReviewItem`, `ReviewError`; `review.controller.ReviewController` — always called through module objects (`items.prepare_review(...)`, `controller.ReviewController(...)`) so tests can monkeypatch them on `matchcut.tui.views.edit`.
- Produces: `MatchcutApp.review_controller()` — creates the controller on first call (from `self.state.config`), returns the same instance after; `MatchcutApp.on_unmount` (or the existing shutdown hook) closes it.

Behaviour (binding, from the spec): bindings `p` "Play", `a` "A/B", `s` "Stop" on `EditView`; a one-line `Static#review-status` directly under the frames row (`#review-status { height: 1; }`); idle text `p play · a compare with <baseline stage>` (dim) or `p play` when `j.baseline.stage == current stage`. `p`: worker prepares `ReviewPair` for (stage, j.index, ab=False, force_render=not controller.has_mpv()) and calls `controller.play_pair(pair)`. `a`: if the controller's current pair is for this (stage, out_shot, in_shot) and has both items, `toggle()` (in a worker); else prepare with `ab=True` and `play_pair`. `s`: `stop()` in a worker. Each worker ends by calling back into the UI thread (`self.app.call_from_thread`, guarded against the app having closed) to update the status keyed by (stage, out_shot, in_shot); statuses for other junctions are kept per key and shown when that junction is selected; a request whose key is already being prepared notifies `already preparing this junction`; any exception → status `review failed: <message>` in red and an error notification; a pair note is appended to the status (` · <note>`) and notified once. Clear stored statuses when the workspace data fingerprint changes, except keys being prepared.

- [ ] **Step 1: Tests** (`tests/tui/test_review_ui.py`, reuse `app_for`, `own_copy`, `wait_for`, `run_async` from `tests/tui/test_edit.py`): a fake controller class recording calls (`has_mpv` → True, `play_pair`, `toggle`, `stop`, `status`), installed by monkeypatching `matchcut.tui.views.edit.controller.ReviewController`; a fake `prepare_review` returning a pair built from temp files.
  1. `p` prepares (stage = current, index = selected junction's index, ab False) and calls `play_pair`; the status shows the fake controller's status text.
  2. `a` prepares with `ab=True` and plays; a second `a` on the same junction calls `toggle()` without preparing again; after `right`, `a` prepares the new junction.
  3. `s` calls `stop()`.
  4. `has_mpv()` False → `prepare_review` is called with `force_render=True`.
  5. a `ReviewError("source footage not found: /x.mp4")` → status `review failed: source footage not found: /x.mp4` and an `error` notification; the app keeps running.
  6. non-blocking: a fake `prepare_review` that waits on a `threading.Event`: while it waits, `right`/`left` change the selection and the idle text shows for the other junction; a second `p` on the same junction notifies `already preparing this junction` and prepare runs once; releasing the event plays once.
  7. idle text with and without an earlier stage (step back with `[` to the first stage).
  8. the app closes the controller on exit (fake records `close()`).
  9. one real end-to-end test (skip without mpv): on a copy of `mock_workspace` with `review.width = 160`, the real `prepare_review` and a real **headless** controller (monkeypatch `MatchcutApp.review_controller` to build `ReviewController(cfg, session_factory=lambda **kw: MpvSession(headless=True))`), press `p`, wait (≤ 120 s) for the status to start with `playing`, then quit and assert no mpv process is left.
- [ ] **Step 2: Run** — failures expected.
- [ ] **Step 3: Implement** per the behaviour above (follow the existing `EditView` patterns: `_show_junction` refreshes the status line; workers via `self.run_worker(fn, thread=True, group="review", exit_on_error=False)`).
- [ ] **Step 4: Run** `python3 -m pytest tests/tui/test_review_ui.py tests/tui/test_edit.py -q`, regenerate the Edit snapshot (check only `[4-edit]` changed and that the inspector's first lines are still visible at 120x40), then all three suites.
- [ ] **Step 5: Commit and push** (title: "Play junctions and flip A/B in mpv from the Edit workbench").

---

### Task 5: Headless demonstration, acceptance criteria and docs

**Files:**
- Create: `tools/demo_review.py`, `docs/M2B-ACCEPTANCE.md`
- Modify: `docs/TUI.md`, `docs/PIPELINE.md`, `README.md`, `docs/screenshots/README.md`, `docs/superpowers/specs/2026-10-18-m2-temporal-review.md` (one status line: superseded by the M2b spec)
- Create (generated): `docs/screenshots/m2b-review-<name>.jpg` contact sheets, `docs/screenshots/m2b-edit-review.svg`

- [ ] **Step 1: `tools/demo_review.py WORKSPACE OUTDIR [--index N ...] [--seconds S]`** (same `sys.path` bootstrap as `tools/capture_edit.py`): for each junction, `prepare_review(..., ab=True)`; a `ReviewController` whose session is `MpvSession(headless=True, video_args=["--vo=image", f"--vo-image-outdir={frames_dir}", "--vo-image-format=jpg", "--ao=null"])`; `play_pair` (A), let it run `S` seconds (default: A's duration), `toggle()` to B at the same offset, run again, close; then build one contact sheet from the captured frames (ffmpeg `tile`, every Nth frame so the sheet has ≤ 24 tiles, with a caption bar naming A/B, stage, transition and kind via `drawtext` when available) into `OUTDIR/review-j<N>.jpg`, and print the items, the frame counts and the paths. Check whether mpv's `--vo=image` includes the OSD label; say which in the docs (do not claim it if it does not).
- [ ] **Step 2: Generate the artifacts.** (a) Synthetic: a mock-backend workspace from `make_demo_footage` (`target_duration=8.0`, `render.enabled=False`), one junction with a dissolve in the final cut and a hard cut in the rough cut, and one hard-cut junction (an EDL item on both sides). (b) Real footage: the Big Buck Bunny mock-backend workspace whose path the controller gives in the dispatch (CC BY 3.0, already credited). Commit the contact sheets as `docs/screenshots/m2b-review-synthetic-j<N>.jpg` and `m2b-review-bbb-j1.jpg`, and an Edit-tab SVG after a `p` with a fake controller status (use `tools/capture_edit.py` patterns) as `m2b-edit-review.svg`. Open every image and confirm what it shows before committing.
- [ ] **Step 3: `docs/M2B-ACCEPTANCE.md`:** what M2b adds (three sentences); **Automated evidence** (each test module and what it proves); **Demonstration** (the images, the regenerate commands, and exactly what the headless run proves — frames mpv displayed, A then B at the same offset — and what it does not: on-screen window, sound, the editor's judgement); **Human checklist** (a desktop with mpv: `p`/`a`/`s` on three junctions incl. a dissolve and one a pass moved; first-prepare latency for EDL vs rendered windows; the transition and sound match `final.mp4` at the same record time; `←`/`→` responsive while preparing; uninstall/rename mpv → the system-player fallback message; rename the source → `review failed: source footage not found: …`); **Verdict** table (motion and timing judgeable: yes/no; A/B at the same offset useful: yes/no; decision); **Status**: automated checks pass, the human checklist is PENDING, M1's gate is still open; **Known limitations** (EDL shows hard cuts only — transitions/fades/music are rendered; loudness normalisation and ducking run on the excerpt; Windows mpv control not implemented; the fallback has no A/B toggle beyond the concat file; previews are never deleted automatically).
- [ ] **Step 4: Docs.** `docs/TUI.md` Edit section: `p`/`a`/`s`, the status line, mpv requirement and fallback, where files go. `docs/PIPELINE.md`: the `review` config section and the `python -m matchcut.review` CLI with a sample. `README.md`: one line. `docs/screenshots/README.md`: credits for the new images.
- [ ] **Step 5: Run all three suites**, commit (tool, docs, images) and push (title: "Add the M2b headless review demonstration, acceptance criteria and docs").
