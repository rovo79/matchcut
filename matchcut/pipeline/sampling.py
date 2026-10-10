"""Footage sampling for the model: keyframes, contact sheets, storyboards.

The model never sees the full video. It sees compact, labelled contact sheets
so that a request covering dozens of shots costs a handful of images.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from .config import SamplingConfig
from .media import extract_frame
from .shots import Shot

LABEL_H = 18
PAD = 4


def _font() -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", 13)
    except OSError:
        return ImageFont.load_default()


def shot_sample_times(shot: Shot, n: int, margin: float = 0.15) -> List[float]:
    """Head / middle / tail sample positions, pulled in slightly from the edges."""
    if n <= 1:
        return [shot.middle]
    m = min(margin, shot.duration / 4)
    lo, hi = shot.start + m, shot.end - m
    return [lo + (hi - lo) * k / (n - 1) for k in range(n)]


class FrameCache:
    """Extracts frames on demand and keeps them on disk under ``root``."""

    def __init__(self, source: str, root: Path, cfg: SamplingConfig):
        self.source = source
        self.root = root
        self.cfg = cfg
        self.root.mkdir(parents=True, exist_ok=True)

    def frame(self, t: float, width: Optional[int] = None) -> Image.Image:
        width = width or self.cfg.tile_width
        key = self.root / f"t{int(round(t * 1000)):09d}_w{width}.jpg"
        if key.exists():
            return Image.open(key).convert("RGB")
        img = extract_frame(self.source, t, width=width)
        img.save(key, quality=self.cfg.jpeg_quality)
        return img

    def shot_frames(self, shot: Shot) -> List[Tuple[float, Image.Image]]:
        return [(t, self.frame(t)) for t in shot_sample_times(shot, self.cfg.frames_per_shot)]


# --------------------------------------------------------------------------
# sheets
# --------------------------------------------------------------------------
def _fit(img: Image.Image, w: int, h: int) -> Image.Image:
    """Letterbox ``img`` into a w x h tile."""
    tile = Image.new("RGB", (w, h), (16, 16, 16))
    scale = min(w / img.width, h / img.height)
    rw, rh = max(1, int(img.width * scale)), max(1, int(img.height * scale))
    resized = img.resize((rw, rh), Image.LANCZOS)
    tile.paste(resized, ((w - rw) // 2, (h - rh) // 2))
    return tile


def grid_sheet(
    rows: Sequence[Tuple[str, Sequence[Tuple[str, Image.Image]]]],
    tile_w: int,
    tile_h: int,
) -> Image.Image:
    """Rows of labelled tiles: each row is (row_label, [(tile_label, image), ...])."""
    cols = max(len(r[1]) for r in rows) if rows else 1
    label_w = 110
    width = label_w + cols * (tile_w + PAD) + PAD
    height = PAD + len(rows) * (tile_h + LABEL_H + PAD)
    sheet = Image.new("RGB", (width, height), (32, 32, 32))
    draw = ImageDraw.Draw(sheet)
    font = _font()
    y = PAD
    for row_label, tiles in rows:
        draw.text((PAD, y + tile_h // 2 - 7), row_label, fill=(255, 255, 255), font=font)
        x = label_w
        for tile_label, img in tiles:
            sheet.paste(_fit(img, tile_w, tile_h), (x, y))
            draw.text((x + 2, y + tile_h + 1), tile_label, fill=(220, 220, 220), font=font)
            x += tile_w + PAD
        y += tile_h + LABEL_H + PAD
    return sheet


def shot_sheet(frames: FrameCache, shots: Sequence[Shot], cfg: SamplingConfig) -> Image.Image:
    rows = []
    for shot in shots:
        tiles = [(f"{t:.1f}s", img) for t, img in frames.shot_frames(shot)]
        rows.append((f"shot {shot.index}\n{shot.duration:.1f}s", tiles))
    return grid_sheet(rows, cfg.tile_width, cfg.tile_height)


def pair_sheet(
    frames: FrameCache,
    pairs: Sequence[Tuple[str, float, float, int, int]],
    cfg: SamplingConfig,
) -> Image.Image:
    """Side-by-side junctions: (pair_id, out_time, in_time, shot_a, shot_b)."""
    rows = []
    for pair_id, out_t, in_t, a, b in pairs:
        tiles = [
            (f"OUT shot {a} @{out_t:.2f}s", frames.frame(out_t)),
            (f"IN shot {b} @{in_t:.2f}s", frames.frame(in_t)),
        ]
        rows.append((pair_id, tiles))
    return grid_sheet(rows, cfg.tile_width, cfg.tile_height)


def storyboard_sheet(
    frames: FrameCache,
    clips: Sequence[Tuple[str, int, float, float, str]],
    cfg: SamplingConfig,
) -> Image.Image:
    """One tile per clip: (clip_id, shot_index, in_time, duration, beat_id)."""
    cols = cfg.storyboard_columns
    rows = []
    for k in range(0, len(clips), cols):
        chunk = clips[k : k + cols]
        tiles = [
            (f"{cid} s{shot} {dur:.1f}s {beat}"[:34], frames.frame(t_in))
            for cid, shot, t_in, dur, beat in chunk
        ]
        rows.append(("", tiles))
    return grid_sheet(rows, cfg.tile_width, cfg.tile_height)


def encode_jpeg_b64(img: Image.Image, quality: int = 82, max_side: int = 1568) -> str:
    if max(img.size) > max_side:
        scale = max_side / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def chunked(items: Sequence, size: int) -> Iterable[Sequence]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
