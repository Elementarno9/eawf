"""The console app: one screen of three row widgets, one key dispatcher, one clock.

:func:`compose_frame` is the frame renderer. An armed go prefix or an open drawer keeps the
route frame and replaces its tail; a full-frame overlay replaces the frame; otherwise the
route's own frame is shown. The app never caches the frame size: it reads its own size at
render time, so a terminal resize re-lays the frame on the next render. A held clock
registers no timer; a live clock sweeps the rack and the go prefix four times a second
through the app's one interval.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from rich.segment import Segment
from rich.style import Style
from textual.app import App, ComposeResult
from textual.events import Key, Resize
from textual.strip import Strip
from textual.widget import Widget

from eawf.surfaces.tui.console.clock import (
    Clock,
    FakeClock,
    expire_prefix,
    notify,
    sweep_toasts,
)
from eawf.surfaces.tui.console.dispatch import dispatch
from eawf.surfaces.tui.console.drawers import DRAWERS
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, thin
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.keymap import DRAWER_PAIRS
from eawf.surfaces.tui.console.navigation import Ctx
from eawf.surfaces.tui.console.overlays import is_overlay, render_overlay
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.tokens import Severity
from eawf.surfaces.tui.console.width import cell_len, pad

TICK_SECONDS = 0.25
GO_DRAWER = "go"
# The toolkit's key names that differ from the dispatcher's.
TOOLKIT_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "up": "ArrowUp",
        "down": "ArrowDown",
        "left": "ArrowLeft",
        "right": "ArrowRight",
        "pageup": "PageUp",
        "pagedown": "PageDown",
        "home": "Home",
        "end": "End",
        "enter": "Enter",
        "escape": "Escape",
        "tab": "Tab",
        "backspace": "Backspace",
        "space": " ",
        "slash": "/",
        "backslash": "\\",
        "full_stop": ".",
        "minus": "-",
        "left_square_bracket": "[",
        "right_square_bracket": "]",
        "question_mark": "?",
        "asterisk": "*",
        "ctrl+f": "ctrl+f",
    }
)
_SHIFT = "shift+"


def dispatcher_key(toolkit_key: str, character: str | None) -> tuple[str, bool] | None:
    """Return the dispatcher's key name and the Shift flag for a toolkit key event.

    Args:
        toolkit_key: The toolkit's key name, such as ``down`` or ``shift+tab``.
        character: The printable character the key produced, if any.

    Returns:
        ``None`` for a key the dispatcher has no name for, such as a bare modifier.
    """
    if toolkit_key == f"{_SHIFT}tab":
        return ("Tab", True)
    if toolkit_key in TOOLKIT_KEYS:
        return (TOOLKIT_KEYS[toolkit_key], False)
    if len(toolkit_key) == 1:
        return (toolkit_key, False)
    if character and len(character) == 1 and character.isprintable():
        return (character, False)
    if toolkit_key.startswith(_SHIFT) and len(toolkit_key) == len(_SHIFT) + 1:
        return (toolkit_key[-1].upper(), True)
    return None


def _drawer_frame(view: View, name: str) -> list[str]:
    """Return the route frame cut to make room for drawer ``name`` below it."""
    s, w, h = view.session, view.w, view.h
    inline = DRAWERS[name](view)
    s.reserved = len(inline) + 2
    base = render_route(view)
    s.reserved = 0
    body_rows = base[: h - 1]
    last_content = max((i for i, row in enumerate(body_rows) if row.strip()), default=-1)
    budget = h - 2 - len(inline)
    hidden = max(0, last_content + 1 - budget)
    body = body_rows[: budget - 1 if hidden else budget]
    if hidden:
        hidden = last_content + 1 - len(body)
        rows = "row" if hidden == 1 else "rows"
        body.append(pad(f"   {hidden} more {rows} below · Esc closes the pane", w))
    return [
        *body,
        pad(thin(w), w),
        *(pad(line, w) for line in inline),
        keybar(DRAWER_PAIRS[name], w),
    ]


def compose_frame(view: View) -> list[str]:
    """Return the session's frame: exactly H rows of W cells.

    Raises:
        ValueError: the composed frame is not exactly H rows of W cells.
    """
    s, w, h = view.session, view.w, view.h
    s.record_facts = None
    s.record_nav = None
    s.renders += 1
    overlay = s.overlay
    if s.prefix == "g":
        rows = _drawer_frame(view, GO_DRAWER)
    elif overlay is not None and overlay in DRAWERS and overlay != GO_DRAWER:
        rows = _drawer_frame(view, overlay)
    elif overlay is not None and is_overlay(overlay):
        rows = render_overlay(overlay, view)
    else:
        rows = render_route(view)
    for i, row in enumerate(rows):
        if cell_len(row) != w:
            raise ValueError(f"frame row {i} is {cell_len(row)} cells, not {w}")
    if len(rows) != h:
        raise ValueError(f"frame has {len(rows)} rows, not {h}")
    return rows


class RowsWidget(Widget):
    """A widget that paints precomposed rows verbatim, one strip per line.

    A row is never wrapped and never parsed as markup, and nothing here takes focus: the
    app dispatches every key itself.
    """

    can_focus = False
    DEFAULT_CSS = """
    RowsWidget { padding: 0; margin: 0; border: none; }
    """
    ROW_STYLE = Style()

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.rows: list[str] = []

    def set_rows(self, rows: list[str]) -> None:
        """Replace the rows and repaint."""
        self.rows = rows
        self.refresh()

    def render_line(self, y: int) -> Strip:
        """Return row ``y`` as one strip of the widget's width."""
        text = self.rows[y] if y < len(self.rows) else ""
        return Strip([Segment(text, self.ROW_STYLE)]).adjust_cell_length(self.size.width)


class ProjectionHeader(RowsWidget):
    """The header row."""

    DEFAULT_CSS = """
    ProjectionHeader { height: 1; dock: top; }
    """


class Body(RowsWidget):
    """The rows between the header and the keybar."""

    DEFAULT_CSS = """
    Body { height: 1fr; }
    """


class KeybarRow(RowsWidget):
    """The keybar row, keys in bold."""

    DEFAULT_CSS = """
    KeybarRow { height: 1; dock: bottom; }
    """
    ROW_STYLE = Style(bold=True)


class ConsoleApp(App[None]):
    """The operator console over one fixture.

    Args:
        fixture: The registers the console renders.
        clock: The console clock; a :class:`FakeClock` holds every timed behaviour.
        verbose: Whether the trace row names the handler of every key.
    """

    CSS = """
    Screen { background: $background; }
    """

    def __init__(
        self, fixture: Fixture, clock: Clock | None = None, *, verbose: bool = False
    ) -> None:
        super().__init__()
        self.fixture = fixture
        self.console_clock: Clock = clock or Clock()
        self.verbose = verbose
        self.session = Session()
        self.reset(None)
        self.frame_rows: list[str] = []
        self.render_count = 0

    @property
    def clock(self) -> Clock:
        """Return the console clock."""
        return self.console_clock

    @property
    def held(self) -> bool:
        """Return whether the console clock is held."""
        return isinstance(self.console_clock, FakeClock)

    def compose(self) -> ComposeResult:
        """Yield the header, body and keybar widgets."""
        yield ProjectionHeader(id="header")
        yield Body(id="body")
        yield KeybarRow(id="keybar")

    def on_mount(self) -> None:
        """Paint the first frame and, under a live clock, start the sweep."""
        self.render_frame()
        if not self.held:
            self.set_interval(TICK_SECONDS, self.tick)

    def on_resize(self, event: Resize) -> None:
        """Re-lay the frame at the new size."""
        self.render_frame()

    def quit(self) -> None:
        """End the console session."""
        self.exit()

    @property
    def frame_size(self) -> tuple[int, int]:
        """Return the frame size: the terminal's, or the session's before one is known."""
        w, h = self.size.width, self.size.height
        if w <= 0 or h <= 0:
            return SIZES[self.session.size]
        return (w, h)

    def view(self) -> View:
        """Return the render view at the current frame size."""
        w, h = self.frame_size
        return View(
            session=self.session,
            fixture=self.fixture,
            w=w,
            h=h,
            verbose=self.verbose,
            held=self.held,
        )

    def reset(self, setup: SessionSetup | None) -> None:
        """Restore the session from ``setup``; the one canonical reset."""
        self.session.reset(
            setup,
            settings_section_order=self.fixture.settings.section_order,
            now=self.console_clock.now(),
        )

    def raise_toast(self, text: str, *, title: str = "done", sev: Severity = Severity.INFO) -> None:
        """Raise a toast on the rack through the console's notify path."""
        notify(self.session, self.console_clock, text=text, title=title, sev=sev)

    def render_frame(self) -> None:
        """Sweep the rack, compose the frame and paint it."""
        sweep_toasts(self.session, self.console_clock)
        view = self.view()
        rows = compose_frame(view)
        self.frame_rows = rows
        self.render_count += 1
        self.query_one("#header", ProjectionHeader).set_rows(rows[:1])
        self.query_one("#body", Body).set_rows(rows[1 : view.h - 1])
        self.query_one("#keybar", KeybarRow).set_rows(rows[view.h - 1 :])

    def tick(self) -> None:
        """Expire toasts and the go prefix on the live clock, repainting on a change."""
        changed = bool(sweep_toasts(self.session, self.console_clock))
        if expire_prefix(self.session, self.console_clock) or changed:
            self.render_frame()

    def press_key(self, key: str, *, shift: bool = False) -> None:
        """Dispatch one key by its dispatcher name and repaint."""
        view = self.view()
        ctx = Ctx(
            session=self.session,
            fixture=self.fixture,
            host=self,
            w=view.w,
            h=view.h,
            verbose=self.verbose,
        )
        dispatch(ctx, key, shift)
        self.render_frame()

    def on_key(self, event: Key) -> None:
        """Take every key from the toolkit and dispatch it."""
        event.stop()
        event.prevent_default()
        named = dispatcher_key(event.key, event.character)
        if named is not None:
            self.press_key(named[0], shift=named[1])
