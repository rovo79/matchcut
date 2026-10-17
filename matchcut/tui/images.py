"""Image loading, terminal image protocol selection, external viewer."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image as PILImage

from matchcut.pipeline.sampling import FrameCache

_cache: Dict[Tuple[str, float, int], PILImage.Image] = {}


def clear_cache() -> None:
    _cache.clear()


def load_image(path: Path, max_width: int = 640) -> Optional[PILImage.Image]:
    """RGB image scaled to at most ``max_width`` wide, cached by path + mtime. None when unavailable."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        mtime = path.stat().st_mtime
        key = (str(path), mtime, max_width)
        if key in _cache:
            return _cache[key]
        img = PILImage.open(path).convert("RGB")
        if img.width > max_width:
            img = img.resize((max_width, max(1, int(img.height * max_width / img.width))), PILImage.LANCZOS)
        _cache[key] = img
        return img
    except Exception:
        return None


def widget_class(headless: bool) -> type:
    """textual-image widget to use: half-block under tests or when forced, else auto-detect."""
    from textual_image.widget import HalfcellImage, Image

    if headless or os.environ.get("MATCHCUT_TUI_IMAGES", "").lower() == "halfcell":
        return HalfcellImage
    return Image


def protocol_label(cls: type) -> str:
    name = cls.__name__.lower()
    if "halfcell" in name:
        return "half-block"
    if "unicode" in name:
        return "unicode blocks"
    if "sixel" in name:
        return "sixel"
    if "tgp" in name:
        return "kitty graphics"
    return "auto (kitty/iterm2/sixel)"


def open_externally(path: Path) -> bool:
    """Open a file in the system viewer. Returns False when no opener is available."""
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener) is None:
        return False
    try:
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    return True


def cached_frame_path(source: str, root: Path, sampling, t: float) -> Optional[Path]:
    """Path of the cached JPEG for time ``t``, extracting it first when the views have not yet. None on failure."""
    cache = FrameCache(source, Path(root), sampling)
    try:
        cache.frame(t)
    except Exception:
        return None
    path = cache.root / f"t{int(round(t * 1000)):09d}_w{sampling.tile_width}.jpg"
    return path if path.exists() else None
