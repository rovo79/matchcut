"""Config view: the live PipelineConfig as an editable tree."""
from __future__ import annotations

import dataclasses
import math
from typing import Any, Dict, Optional, Tuple, Union, get_args, get_origin, get_type_hints

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Input, Static, Tree
from textual.widgets.tree import TreeNode

from matchcut.pipeline.config import PipelineConfig
from ..state import RunState

TOP_LEVEL_FIELDS = ("source", "workspace", "brief", "target_duration", "passes", "force", "seed")

Key = Tuple[Optional[str], str]


def _unwrap_optional(hint: Any) -> Tuple[Any, bool]:
    """Return (X, True) for Optional[X], else (hint, False)."""
    if get_origin(hint) is Union:
        args = get_args(hint)
        rest = [a for a in args if a is not type(None)]
        if len(rest) == 1 and len(rest) != len(args):
            return rest[0], True
    return hint, False


def _parse_bool(text: str) -> bool:
    low = text.lower()
    if low in ("true", "yes", "1", "on"):
        return True
    if low in ("false", "no", "0", "off"):
        return False
    raise ValueError(f"not a boolean: {text!r}")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"not a finite number: {text!r}")
    return value


def parse_value(current: Any, text: str, hint: Any = None) -> Any:
    """Parse ``text`` for a config field.

    ``hint`` is the field's declared annotation; with it the parse follows the declared
    type (so an Optional[float] that is currently None still takes a float). Without it
    the type is inferred from ``current`` (legacy behaviour).
    """
    text = text.strip()
    if hint is not None:
        inner, optional = _unwrap_optional(hint)
        if optional and text == "":
            return None
        if inner is bool:
            return _parse_bool(text)
        if inner is int:
            return int(text)
        if inner is float:
            return _parse_float(text)
        if inner is str:
            return text
        if get_origin(inner) is tuple:
            parts = tuple(p.strip() for p in text.split(",") if p.strip())
            if not parts:
                raise ValueError("a list value cannot be empty (comma-separated names)")
            return parts
        raise ValueError(f"cannot edit values of type {inner!r}")
    if isinstance(current, bool):
        return _parse_bool(text)
    if isinstance(current, int):
        return int(text)
    if isinstance(current, float):
        return float(text)
    if isinstance(current, tuple):
        return tuple(p.strip() for p in text.split(",") if p.strip())
    if current is None and text:
        for cast in (int, float):
            try:
                return cast(text)
            except ValueError:
                pass
        return text
    if current is None or isinstance(current, str):
        return text or None
    raise ValueError(f"cannot edit values of type {type(current).__name__}")


def set_value(cfg: PipelineConfig, section: Optional[str], field: str, value: Any) -> None:
    target = cfg if section is None else getattr(cfg, section)
    setattr(target, field, value)


def _hint_for(cfg: PipelineConfig, section: Optional[str], field: str) -> Any:
    target = cfg if section is None else getattr(cfg, section)
    return get_type_hints(type(target)).get(field)


def _get_value(cfg: PipelineConfig, section: Optional[str], field: str) -> Any:
    return getattr(cfg if section is None else getattr(cfg, section), field)


class ConfigView(Vertical):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._state: Optional[RunState] = None
        self._editing: Optional[Key] = None
        # refresh_views() runs on every pump batch; the tree is built once per config
        # object (memo keyed on identity, holding the object so the id cannot be reused)
        # and edits update the touched leaf in place, so cursor and expansion survive.
        self._built_for: Optional[PipelineConfig] = None
        self._leaves: Dict[Key, TreeNode] = {}

    def compose(self) -> ComposeResult:
        yield Static("Enter: edit the highlighted value.  Changes apply to the next run or redo (and are saved then).", id="config-hint")
        yield Tree("config", id="config-tree")
        yield Input(placeholder="new value, then Enter (Esc cancels)", id="config-edit")

    def on_mount(self) -> None:
        self.query_one("#config-edit").display = False
        if self._state is not None:  # a refresh that arrived before the tree existed
            self.refresh_from_state(self._state)

    # -- tree ------------------------------------------------------------------
    def refresh_from_state(self, state: RunState) -> None:
        self._state = state
        trees = self.query("#config-tree")
        if not trees:
            return
        cfg = state.config
        if self._built_for is cfg:
            return
        self._built_for = cfg
        self._editing = None
        self._leaves = {}
        edit = self.query_one("#config-edit", Input)
        edit.display = False
        tree = trees.first(Tree)
        tree.clear()
        for name in TOP_LEVEL_FIELDS:
            self._leaves[(None, name)] = tree.root.add_leaf(self._label(name, getattr(cfg, name)), data=(None, name))
        for f in dataclasses.fields(cfg):
            value = getattr(cfg, f.name)
            if dataclasses.is_dataclass(value):
                node = tree.root.add(f.name, data=None)
                for sf in dataclasses.fields(value):
                    sub = getattr(value, sf.name)
                    if isinstance(sub, dict):
                        continue  # pace_targets etc. are edited through --set
                    self._leaves[(f.name, sf.name)] = node.add_leaf(self._label(sf.name, sub), data=(f.name, sf.name))
        tree.root.expand()

    @staticmethod
    def _label(name: str, value: Any) -> str:
        shown = ", ".join(value) if isinstance(value, tuple) else repr(value) if isinstance(value, str) else str(value)
        return f"{name} = {shown}"

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        if event.node.data is None or self._state is None:
            return
        section, field = event.node.data
        if (section, field) == (None, "workspace"):
            # every view, pin and review reads the workspace the app was started on: changing it mid-session would split them
            self.app.notify("workspace is fixed for this session: restart with --workspace to use another", severity="warning")
            return
        self._editing = (section, field)
        current = _get_value(self._state.config, section, field)
        edit = self.query_one("#config-edit", Input)
        edit.value = "" if current is None else (", ".join(current) if isinstance(current, tuple) else str(current))
        edit.placeholder = f"{section + '.' if section else ''}{field} ({type(current).__name__}); Enter to apply, Esc to cancel"
        edit.display = True
        edit.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "config-edit" or self._editing is None or self._state is None:
            return
        section, field = self._editing
        cfg = self._state.config
        current = _get_value(cfg, section, field)
        try:
            value = parse_value(current, event.value, _hint_for(cfg, section, field))
        except ValueError as e:
            self.app.notify(f"{field}: {e}", severity="error")
            return
        set_value(cfg, section, field, value)
        self._editing = None
        event.input.display = False
        leaf = self._leaves.get((section, field))
        if leaf is not None:
            leaf.set_label(self._label(field, value))
        self.query_one("#config-tree", Tree).focus()
        self.app.notify(f"{section + '.' if section else ''}{field} = {value!r} (applies to the next run or redo)")

    def on_key(self, event) -> None:
        if event.key == "escape" and self._editing is not None:
            self._editing = None
            self.query_one("#config-edit", Input).display = False
            self.query_one("#config-tree", Tree).focus()
            event.stop()
