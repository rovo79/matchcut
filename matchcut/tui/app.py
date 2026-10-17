"""The control-room application."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Footer, Static, TabbedContent, TabPane

from matchcut.pipeline import events
from matchcut.pipeline.config import PipelineConfig
from . import images
from .bridge import PipelineBridge
from .state import RunState
from .views.config import ConfigView
from .views.cut import CutView
from .views.run import RunView
from .views.shots import ShotsView
from .views.story import StoryView

QUIT_CONFIRM_SECONDS = 3.0  # how long a warned `q` stays armed

HELP_TEXT = """Control room keys

 1-5   Run / Shots / Story / Cut / Config        R  run or resume        S  stop
 c/r/a continue / redo with note / abort a checkpoint (Run view)
 f     cycle the log filter (Run view)           o  open the current image or render externally
 u     toggle a shot's usable flag (Shots)       [ ]  step through passes (Cut)    ←/→ select a clip (Cut)
 Enter edit a config value (Config)              ?  this help      Esc  close      q  quit

Press Escape to close."""


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT, id="help")


class MatchcutApp(App):
    CSS_PATH = "styles.tcss"
    TITLE = "matchcut control room"
    BINDINGS = [
        Binding("1", "show_tab('tab-run')", "Run"),
        Binding("2", "show_tab('tab-shots')", "Shots"),
        Binding("3", "show_tab('tab-story')", "Story"),
        Binding("4", "show_tab('tab-cut')", "Cut"),
        Binding("5", "show_tab('tab-config')", "Config"),
        Binding("R", "run", "Run"),
        Binding("S", "stop", "Stop"),
        Binding("o", "open", "Open"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: PipelineConfig, bridge_factory: Optional[Callable[[PipelineConfig], PipelineBridge]] = None) -> None:
        super().__init__()
        self.state = RunState(cfg)
        self.bridge: Optional[PipelineBridge] = None
        self._bridge_factory = bridge_factory or (lambda c: PipelineBridge(c, interactive=True))
        self._quit_armed = False
        self._quit_timer = None
        self._image_notice_shown = False

    # -- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with TabbedContent(initial="tab-run"):
            with TabPane("1 Run", id="tab-run"):
                yield RunView(id="run-view")
            with TabPane("2 Shots", id="tab-shots"):
                yield ShotsView(id="shots-view")
            with TabPane("3 Story", id="tab-story"):
                yield StoryView(id="story-view")
            with TabPane("4 Cut", id="tab-cut"):
                yield CutView(id="cut-view")
            with TabPane("5 Config", id="tab-config"):
                yield ConfigView(id="config-view")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.05, self._pump)
        self.refresh_views()
        self._focus_run_view()

    def _focus_run_view(self) -> None:
        # RunView's c/r/a/f bindings only apply while focus sits inside it.
        self.call_after_refresh(self._focus_if_active, "tab-run", "#ladder")

    def _focus_if_active(self, pane_id: str, selector: str) -> None:
        # A deferred focus must not pull the user back from a tab they already left.
        if self.query_one(TabbedContent).active == pane_id:
            self.query_one(selector).focus()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id != self.query_one(TabbedContent).active:
            return  # a stale event for a tab the user has already left
        if event.pane.id == "tab-run":
            self._focus_run_view()
        elif event.pane.id == "tab-shots":
            # The tab switch cleared focus; hand it to the table so down/u work at once.
            self.call_after_refresh(self._focus_if_active, "tab-shots", "#shots-table")
        elif event.pane.id == "tab-story":
            self.call_after_refresh(self._focus_if_active, "tab-story", "#story-view")
        elif event.pane.id == "tab-cut":
            self.call_after_refresh(self._focus_if_active, "tab-cut", "#cut-view")
        elif event.pane.id == "tab-config":
            self.call_after_refresh(self._focus_if_active, "tab-config", "#config-tree")

    # -- event pump -------------------------------------------------------------
    def _pump(self) -> None:
        changed = False
        if self.bridge is not None:
            for e in self.bridge.drain():
                self.state.apply(e)
                if isinstance(e, (events.RunStarted, events.RunFinished, events.RunFailed)):
                    self._disarm_quit()  # a warning about one run must not carry over to the next
                changed = True
        self._update_status()
        if self.state.image_mode and not self._image_notice_shown:
            self._image_notice_shown = True
            self.notify(f"images rendered as {self.state.image_mode}")
        if changed:
            self.refresh_views()

    def refresh_views(self) -> None:
        for view in self.query("RunView, ShotsView, StoryView, CutView, ConfigView"):
            view.refresh_from_state(self.state)
        self._update_status()

    def _update_status(self) -> None:
        st = self.state
        source = st.config.source.rsplit("/", 1)[-1]
        running = st.checkpoint.stage if st.checkpoint else next((n for n, s in st.stages.items() if s.state in ("running", "stopping")), "-")
        text = (
            f"{source}  |  {st.run_status}  |  stage {running}  |  {st.elapsed():6.1f}s  |  "
            f"{st.calls} calls  {st.input_tokens}/{st.output_tokens} tok  ~${st.cost_estimate():.3f}"
        )
        if st.image_mode:
            text += f"  |  images: {st.image_mode}"
        status = self.query("#status")
        if status:  # the timer can tick once more while the screen is being torn down
            status.first(Static).update(text)

    # -- actions ----------------------------------------------------------------
    def action_show_tab(self, tab: str) -> None:
        pane = tab if tab.startswith("tab-") else f"tab-{tab}"
        if pane != "tab-run":
            # A focused widget inside the Run pane would pull TabbedContent back to it.
            self.screen.set_focus(None)
        self.query_one(TabbedContent).active = pane
        if pane == "tab-run":
            self._focus_run_view()

    def action_run(self) -> None:
        if self.bridge is not None and self.bridge.is_running():
            self.notify("a run is already in progress", severity="warning")
            return
        self._disarm_quit()
        try:
            bridge = self._bridge_factory(self.state.config)
            bridge.start()
        except Exception as exc:  # empty source, unwritable workspace, unknown backend ...
            self.bridge = None
            self.state.last_error = str(exc)
            self.state.run_status = "failed"
            self.notify(str(exc), severity="error")
            self.refresh_views()
            return
        self.bridge = bridge
        self.action_show_tab("tab-run")
        self._focus_run_view()

    def action_stop(self) -> None:
        if self.bridge is None or not self.bridge.is_running():
            self.notify("nothing is running")
            return
        self.state.mark_stopping()
        self.bridge.stop()
        self.refresh_views()

    def current_open_target(self) -> Optional[Path]:
        """What `o` would open for the active tab, or None."""
        active = self.query_one(TabbedContent).active
        if active == "tab-run":
            for name in ("final.mp4", "final_proxy.mp4"):
                path = self.state.workspace / name
                if path.exists():
                    return path
            return None
        if active == "tab-shots":
            return self.query_one(ShotsView).open_target()
        if active == "tab-cut":
            return self.query_one(CutView).open_target()
        return None

    def action_open(self) -> None:
        target = self.current_open_target()
        if target is None:
            self.notify("nothing to open on this view")
            return
        if not images.open_externally(target):
            self.notify(f"no system opener found; file is {target}", severity="warning")
        else:
            self.notify(f"opened {target.name}")

    def action_help(self) -> None:
        self.push_screen(HelpScreen(name="help"))

    def _disarm_quit(self) -> None:
        self._quit_armed = False
        if self._quit_timer is not None:
            self._quit_timer.stop()
            self._quit_timer = None

    def action_quit(self) -> None:
        if self.bridge is not None and self.bridge.is_running() and not self._quit_armed:
            self._quit_armed = True
            self._quit_timer = self.set_timer(QUIT_CONFIRM_SECONDS, self._disarm_quit)
            self.notify("a run is active: press q again to quit anyway", severity="warning")
            return
        if self.bridge is not None:
            self.bridge.stop()
        self.exit()
