"""A frame preview: an image rendered with the best protocol the terminal supports."""
from __future__ import annotations

from typing import Optional

from PIL import Image as PILImage
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from .. import images


class Frame(Vertical):
    DEFAULT_CSS = """
    Frame { height: auto; }
    Frame > .frame-caption { height: 1; color: $text-muted; }
    Frame > .frame-placeholder { height: 3; content-align: center middle; color: $text-muted; }
    """

    def __init__(self, caption: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._caption = caption
        self.image_mode = ""

    def compose(self) -> ComposeResult:
        yield Static("(no frame)", classes="frame-placeholder")
        yield Static(self._caption, classes="frame-caption")

    def placeholder_text(self) -> str:
        nodes = self.query(".frame-placeholder")
        return str(nodes.first().content) if nodes else ""

    def show(self, image: Optional[PILImage.Image], caption: str = "") -> None:
        self._caption = caption
        for old in list(self.query(".frame-image")):
            old.remove()
        placeholder = self.query(".frame-placeholder").first()
        if image is None:
            placeholder.display = True
        else:
            placeholder.display = False
            cls = images.widget_class(headless=self.app.is_headless)
            self.image_mode = images.protocol_label(cls)
            self.mount(cls(image, classes="frame-image"), before=placeholder)
        self.query(".frame-caption").first().update(caption)
