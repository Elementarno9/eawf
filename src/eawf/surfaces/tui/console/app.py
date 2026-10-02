"""The console app: one screen of three row widgets, one key dispatcher, one clock.

:func:`compose_frame` is the frame renderer. An armed go prefix or an open drawer keeps the
route frame and replaces its tail; a full-frame overlay replaces the frame; otherwise the
route's own frame is shown. The app never caches the frame size: it reads its own size at
render time, so a terminal resize re-lays the frame on the next render. A held clock
registers no timer; a live clock sweeps the rack and the go prefix four times a second
through the app's one interval.

An overlay or drawer holds the frame on any console. An overlay that has read nothing it
draws shows its own crumb and says its subject is not held; a drawer keeps the route frame
above it, which is the route's unknown frame only when the route itself is unheld.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, ClassVar

from rich.segment import Segment
from rich.style import Style
from textual._time import get_time
from textual.app import App, ComposeResult
from textual.constants import ESCAPE_DELAY
from textual.events import Click, Key, Resize
from textual.strip import Strip
from textual.widget import Widget

from eawf.kernel.config.schema import ToastVerbosity
from eawf.kernel.projection.attention import delivered_revisions, deliveries
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.integration import INTEGRATION_ROUTES, build_integration_view
from eawf.kernel.projection.liveness import HeldLiveness
from eawf.kernel.projection.operations import OPERATIONS_ROUTES, build_operations_view
from eawf.kernel.projection.registers import (
    ATTENTION_ROUTE,
    REGISTER_ROUTES,
    RegisterView,
    build_register_view,
)
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.run_timeline import RunTimeline
from eawf.kernel.projection.settings import (
    SETTINGS_ROUTES,
    EffectiveSettingsView,
    catalog_section_order,
)
from eawf.kernel.projection.spine import NATIVE_ROUTES, SpineView, build_spine_view
from eawf.kernel.projection.transcript import TRANSCRIPT_ROUTE, build_transcript_view
from eawf.kernel.projection.verification import (
    VERIFICATION_ROUTES,
    build_verification_view,
)
from eawf.kernel.runtime.control import ControlDisposition
from eawf.platform.registry import RegistryReadError
from eawf.runtime.daemon.native_guard import EA_DIRNAME
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.tui.chassis.theme import (
    DEFAULT_THEME,
    EA_THEMES,
    THEME_POLL_INTERVAL_S,
    detect_auto_theme,
    detect_os_appearance,
    resolve_theme_name,
)
from eawf.surfaces.tui.console.attach import OFFLINE, ONBOARDING, with_entry_state
from eawf.surfaces.tui.console.bulk import BulkRequest
from eawf.surfaces.tui.console.cards import settle
from eawf.surfaces.tui.console.chrome import ConsoleChrome, load_chrome
from eawf.surfaces.tui.console.clock import (
    Clock,
    FakeClock,
    QuitStep,
    expire_prefix,
    notify,
    prompt_quit,
    quit_step,
    sweep_toasts,
)
from eawf.surfaces.tui.console.dispatch import activate_crumb, dispatch
from eawf.surfaces.tui.console.drawers import DRAWERS
from eawf.surfaces.tui.console.drill import say_why
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View, paint_rack, thin, unheld
from eawf.surfaces.tui.console.header import CrumbRun, crumb_at
from eawf.surfaces.tui.console.keybar import keybar
from eawf.surfaces.tui.console.keymap import DRAWER_PAIRS, ENTRY_ROUTE
from eawf.surfaces.tui.console.navigation import Ctx, open_overlay
from eawf.surfaces.tui.console.onboarding import FirstRun, register_workspace, registered_state
from eawf.surfaces.tui.console.operations import (
    NO_PRINCIPAL_REASON,
    OperationResult,
    VerbRequest,
)
from eawf.surfaces.tui.console.overlays import is_overlay, render_overlay
from eawf.surfaces.tui.console.paint import Part, Stroke, paint
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup, conn_label
from eawf.surfaces.tui.console.token_map import SURFACES, TOKEN_MAP, render_css
from eawf.surfaces.tui.console.tokens import CARET, Severity
from eawf.surfaces.tui.console.width import cell_len, pad
from eawf.workflow.projection.acceptance import ACCEPTANCE_ROUTES, build_acceptance_view

if TYPE_CHECKING:
    # imported for the type alone: the seam pulls the daemon method registry in, and a
    # console that is drawing the prototype registers has no business registering verbs
    from eawf.surfaces.tui.console.seam import ProjectionSeam

logger = logging.getLogger(__name__)

TICK_SECONDS = 0.25
# The blank cells an operator's console keeps clear at each side of every row, so the
# bands and rules stop short of the window edge the way the packet's canvas padding does.
OUTER_GUTTER = 1
#: The two glyph allocations a frame can be drawn in; ``ui.glyphs`` picks one.
GLYPH_ALLOCATIONS: tuple[str, ...] = ("unicode", "ascii")
GO_DRAWER = "go"
# The worker group the seam's route reads run in.
SEAM_WORKERS = "seam"
# The worker group the live reads' re-reads run in; one at a time, the latest wins.
LIVE_WORKERS = "live"
# How often the live reads of the route on screen are read again. What they return is
# appended with no patch published, so the frame follows it by reading, not by being told.
LIVE_REFRESH_SECONDS = 1.0
# The worker group the console's writes run in, apart from the reads so neither waits.
WRITE_WORKERS = "writes"
# The key-log key a daemon answer to a sent verb is recorded under.
DAEMON_KEY = "daemon"
# The style-metadata key a painted span's mark travels under.
MARK_META = "mark"
# How loudly each control outcome is announced; the toast title is the outcome's own word.
# Only a confirmed effect speaks as success, because requesting and accepted are facts
# about the request channel; a superseded answer is a loss, never a warning or an error.
WRITE_TOAST_SEVERITY: Mapping[ControlDisposition, Severity] = MappingProxyType(
    {
        ControlDisposition.IDLE: Severity.INFO,
        ControlDisposition.REQUESTING: Severity.INFO,
        ControlDisposition.ACCEPTED: Severity.INFO,
        ControlDisposition.CONFIRMED: Severity.OK,
        ControlDisposition.REJECTED: Severity.ERR,
        ControlDisposition.INVALIDATED: Severity.WARN,
        ControlDisposition.UNKNOWN: Severity.WARN,
        ControlDisposition.RECOVERY: Severity.WARN,
        ControlDisposition.SUPERSEDED: Severity.INFO,
    }
)
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
# The Shift-held arrows the dispatcher tells apart from the bare ones.
_SHIFTED_ARROWS = frozenset({f"{_SHIFT}up", f"{_SHIFT}down"})
# How far past the toolkit's escape delay a second Escape's stamp may land and still be
# the pair the parser held back together: the scheduling jitter of emitting it.
ESCAPE_SLACK = 0.02
# The macOS clipboard command. macOS Terminal ignores the OSC 52 copy sequence, so a
# console running there also hands the text to the system clipboard directly.
PBCOPY = "pbcopy"
PBCOPY_TIMEOUT = 1.0


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
    if toolkit_key in _SHIFTED_ARROWS:
        # an ordered list moves its entry with Shift and an arrow
        return (TOOLKIT_KEYS[toolkit_key.removeprefix(_SHIFT)], True)
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
    """Return the route frame cut to make room for drawer ``name`` below it.

    The rack is the console's, not the route's: it is left out of the route frame, so the
    cut never hides it or counts it as a hidden row, and is painted over the kept rows just
    above the drawer's rule.
    """
    s, w, h = view.session, view.w, view.h
    inline = DRAWERS[name](view)
    s.reserved = len(inline) + 2
    toasts, s.toasts = s.toasts, []
    base = render_route(view)
    s.toasts = toasts
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
    paint_rack(s, body, w, verbose=False)
    return [
        *body,
        pad(thin(w), w),
        # a full-width row is kept as drawn, so a mark such as Disabled reaches the painter
        *(line if cell_len(line) == w else pad(line, w) for line in inline),
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
    s.bucket_keys = None
    s.windowed = False
    s.nav_rows = None
    s.bar_keys = None
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
        s.route_windowed = s.windowed
        s.route_bar_keys = s.bar_keys
    for i, row in enumerate(rows):
        if cell_len(row) != w:
            raise ValueError(f"frame row {i} is {cell_len(row)} cells, not {w}")
    if len(rows) != h:
        raise ValueError(f"frame has {len(rows)} rows, not {h}")
    return rows


# What only the operator may change: the route and its subject, what is open, where the
# focus is, and whether a prefix or a text input holds the keyboard.
_STANCE_FIELDS: tuple[str, ...] = ("route", "subj_id", "overlay", "region", "prefix", "typing")


# Where a key can move the focus without changing a character of the frame: the region,
# group, section or bucket a Tab cycles, which the frame shows by colour alone.
_FOCUS_FIELDS: tuple[str, ...] = (
    "region",
    "home_region",
    "track_group",
    "rel_reg",
    "tl_reg",
    "section",
    "bucket",
    "set_sec",
)


def focus_of(session: Session) -> tuple[object, ...]:
    """Return where the session's focus stands, colour-only moves included."""
    return tuple(getattr(session, name) for name in _FOCUS_FIELDS)


def stance(session: Session) -> dict[str, object]:
    """Return the session's stance: the fields no projection event may move."""
    return {name: getattr(session, name) for name in _STANCE_FIELDS}


def _classes(*surfaces: str) -> str:
    """Return the stylesheet classes that give a widget these surfaces' colours."""
    return " ".join(SURFACES[surface].css_class for surface in surfaces)


class RowsWidget(Widget):
    """A widget that paints precomposed rows verbatim, one strip per line.

    A row is never wrapped and never parsed as markup, and nothing here takes focus: the
    app dispatches every key itself. Each run of a row is drawn as the surface the row
    painter reads off its words, in the colour the token map's stylesheet gives that
    surface under the active theme.
    """

    can_focus = False
    DEFAULT_CSS = """
    RowsWidget { padding: 0; margin: 0; border: none; }
    """
    COMPONENT_CLASSES: ClassVar[set[str]] = {row.css_class for row in TOKEN_MAP}
    ROW_STYLE = Style()
    PART: ClassVar[Part] = Part.BODY

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.rows: list[str] = []

    def set_rows(self, rows: list[str]) -> None:
        """Replace the rows and repaint."""
        self.rows = rows
        self.refresh()

    def render_line(self, y: int) -> Strip:
        """Return row ``y`` as one strip of the widget's width, one segment per span.

        A marked span carries its mark in the segment's style metadata under
        :data:`MARK_META`, so the theme that colours a token reads what the cell is rather
        than guessing it from the glyph.
        """
        text = self.rows[y] if y < len(self.rows) else ""
        base = self.rich_style + self.ROW_STYLE
        segments = [
            Segment(stroke.text, self._stroke_style(base, stroke))
            for stroke in paint(text, self.PART)
        ]
        return Strip(segments).adjust_cell_length(self.size.width)

    def _stroke_style(self, base: Style, stroke: Stroke) -> Style:
        """Return the style one run is drawn in: the band's, then its surfaces over it."""
        style = base
        for surface in (stroke.ground, stroke.surface):
            if surface is not None:
                style += self.get_component_rich_style(SURFACES[surface].css_class, partial=True)
        if stroke.bold or stroke.underline:
            style += Style(bold=stroke.bold or None, underline=stroke.underline or None)
        if stroke.mark is not None:
            style += Style(meta={MARK_META: stroke.mark.value})
        return style


class ProjectionHeader(RowsWidget):
    """The header row: the row painter styles its typed crumb runs, and a click walks them.

    The styling rides on the segments only, so the row's text is the frame's text, and a
    click on a linked step walks the breadcrumb to it.
    """

    DEFAULT_CSS = """
    ProjectionHeader { height: 1; dock: top; }
    """
    DEFAULT_CLASSES = _classes("band", "text")
    PART = Part.HEADER

    def on_click(self, event: Click) -> None:
        """Walk the breadcrumb to the step under the pointer, if it is a link."""
        if isinstance(self.app, ConsoleApp) and self.rows:
            step = crumb_at(self.rows[0], event.x)
            if step is not None:
                self.app.activate_crumb(step)


class Body(RowsWidget):
    """The rows between the header and the keybar; a single left click on a row selects it."""

    DEFAULT_CSS = """
    Body { height: 1fr; }
    """
    DEFAULT_CLASSES = _classes("canvas", "text")

    def on_click(self, event: Click) -> None:
        """Select the row under the pointer, as the arrow keys would; any other click is none."""
        if isinstance(self.app, ConsoleApp) and event.button == 1 and event.chain == 1:
            # the header is the frame's first row, so body line ``y`` is frame row ``y + 1``
            self.app.select_row_at(event.y + 1)


class KeybarRow(RowsWidget):
    """The keybar row, keys in bold."""

    DEFAULT_CSS = """
    KeybarRow { height: 1; dock: bottom; }
    """
    DEFAULT_CLASSES = _classes("band", "text")
    PART = Part.KEYBAR


class ConsoleApp(App[None]):
    """The operator console over the packaged chrome, or over one prototype fixture.

    The console registers the themes bound from the design packet's palette and opens on
    the operator's ``ui.theme``; under ``auto`` it opens on the terminal's background and
    then follows the system's light or dark appearance while it runs. ``NO_COLOR`` still
    reduces every cell to grey through the toolkit.

    Args:
        fixture: The prototype registers the golden contract replays. A console given
            none holds the chrome alone and draws the unknown token wherever its seam
            holds no read model.
        clock: The console clock; a :class:`FakeClock` holds every timed behaviour.
        verbose: Whether the trace row names the handler of every key.
        seam: The console's one link to the daemon projection. A console given one draws
            the bound spine, register, verification, operations, integration and
            transcript routes from the read model it holds; a console given none draws
            the prototype registers, which is the mode the tracked golden contract
            replays.
        chrome: The static tables a console given no fixture draws; the packaged chrome
            when omitted. A fixture carries its own chrome, so passing both is refused.
        gutter: The blank cells kept clear at each side of every row; the frame is laid
            out at the terminal's width less both. ``0`` fills the terminal, which is
            what the tracked golden contract records: each golden is the content grid.
        theme: The logical theme: ``dark``, ``light``, ``cb`` or ``auto``. ``auto`` reads
            the terminal's background here, before the toolkit takes the terminal, and
            is refined from the system appearance once the console runs.
        toast_verbosity: The operator's ``ui.toasts`` level. Under ``off`` arriving
            attention raises no toast; the answer to a key or a sent write, and the quit
            prompt, always do.
        glyphs: The glyph allocation the frame is drawn in. Under ``ascii`` every row
            is drawn through the plain-mode twins, so no glyph outside ASCII reaches
            the terminal.
        first_run: The tree a first run stands in and the daemon link its workspace
            step writes through; ``None`` for any launch that is not a first run.

    Raises:
        ValueError: both a fixture and a chrome were given, ``gutter`` is negative, or
            ``theme`` is not a logical theme name, or ``glyphs`` is not ``unicode``
            or ``ascii``.
    """

    # The whole stylesheet is the token map rendered: no colour is chosen in this file.
    CSS = render_css(TOKEN_MAP)

    def __init__(
        self,
        fixture: Fixture | None = None,
        clock: Clock | None = None,
        *,
        chrome: ConsoleChrome | None = None,
        verbose: bool = False,
        seam: ProjectionSeam | None = None,
        gutter: int = 0,
        theme: str = DEFAULT_THEME,
        toast_verbosity: ToastVerbosity = "important",
        glyphs: str = "unicode",
        first_run: FirstRun | None = None,
    ) -> None:
        if fixture is not None and chrome is not None:
            raise ValueError("a fixture carries its own chrome; pass a fixture or a chrome")
        if gutter < 0:
            raise ValueError(f"an outer gutter cannot be negative, got {gutter}")
        if resolve_theme_name(theme) is None:
            raise ValueError(f"{theme!r} is not a logical theme name")
        if glyphs not in GLYPH_ALLOCATIONS:
            raise ValueError(f"{glyphs!r} is not a glyph allocation")
        super().__init__()
        self.gutter = gutter
        for registered in EA_THEMES:
            self.register_theme(registered)
        self.follows_system = theme == "auto"
        self.appearance = detect_auto_theme() if self.follows_system else theme
        self.theme = str(resolve_theme_name(self.appearance))
        self.fixture = fixture or Fixture.from_chrome(chrome or load_chrome())
        self.console_clock: Clock = clock or Clock()
        self.verbose = verbose
        self.toast_verbosity = toast_verbosity
        self.glyphs = glyphs
        self.seam = seam
        self.first_run = first_run
        if seam is not None:
            seam.watch(self._on_seam_patched)
        # when the live reads of the route on screen were last read, on the console clock
        self._live_read_at = 0.0
        self.session = Session()
        # the attention revisions already announced to this principal; ``None`` until the
        # first read, which seeds it so nothing already open is toasted after a restart
        self._delivered: set[tuple[str, int]] | None = None
        # the arrival of the key before this one, while that key was an Escape
        self._escape_at: float | None = None
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
        """Paint the first frame, read the routes it owes and, under a live clock, sweep."""
        # the gutter is canvas, never band: the header and keybar grounds stop inside it
        self.screen.add_class(SURFACES["canvas"].css_class)
        self.screen.styles.padding = (0, self.gutter)
        self.render_frame()
        self._follow_route()
        if not self.held:
            self.set_interval(TICK_SECONDS, self.tick)
            if self.follows_system:
                self.set_interval(THEME_POLL_INTERVAL_S, self.follow_appearance)

    async def follow_appearance(self) -> None:
        """Take the system's light or dark appearance when it differs from the one shown.

        The appearance is read off-thread from the platform setting, never from the
        terminal the toolkit owns; an appearance that cannot be read changes nothing.
        """
        appearance = await asyncio.to_thread(detect_os_appearance)
        if appearance is None or appearance == self.appearance:
            return
        self.appearance = appearance
        self.theme = str(resolve_theme_name(appearance))

    def _follow_route(self) -> None:
        """Point the seam at the session's route and read whatever it now owes.

        The read runs off the key path, so the frame shows what is held until the
        answer arrives and then repaints; a console not yet running only retargets.
        """
        seam = self.seam
        if seam is None:
            return
        seam.retarget(self.route_key)
        seam.about(self.subject)
        if self.is_running and seam.owed():
            self.run_worker(self._load_owed(), group=SEAM_WORKERS)

    async def _load_owed(self) -> None:
        """Read the owed routes and repaint once any of them arrived."""
        seam = self.seam
        assert seam is not None, "only started with a seam"
        if loaded := await seam.sync():
            self.deliver_attention()
            self._open_resolution(loaded)
            if self.is_running:
                self.arrive()
        elif not seam.held_routes and seam.settings is None and self.is_running:
            self._land_offline()

    def _open_resolution(self, loaded: tuple[str, ...]) -> None:
        """Open the resolution card when the key the operator navigated to names nothing.

        The card is the answer to the navigation that asked for the key, so it opens only
        from the read that navigation owed, and only over a bare route.
        """
        from eawf.surfaces.tui.console.live_reads import RESOLUTION_READ

        seam = self.seam
        ending = seam.live(RESOLUTION_READ) if seam is not None else None
        if RESOLUTION_READ not in loaded or ending is None or self.session.overlay is not None:
            return
        open_overlay(self.session, "resolution", subject=self.subject)
        self.session.resolution_ending = ending
        logger.info(f"resolution card opened ending={ending}")

    def _land_offline(self) -> None:
        """Open the offline entry frame: the daemon answered no read, so nothing is live.

        The frame shows the snapshot the launch read from disk, or says none is held; it
        is only ever opened over a console that has read nothing, so it never replaces a
        frame an answer drew.
        """
        entry = self.fixture.proto.entry
        index = next((i for i, state in enumerate(entry) if state.id == OFFLINE), None)
        if index is None or self.session.route == ENTRY_ROUTE:
            return
        self.reset(SessionSetup(route=ENTRY_ROUTE, entrySel=index))
        self.render_frame()

    def _on_seam_patched(self, routes: tuple[str, ...]) -> None:
        """Announce new attention, then repaint when the route on screen or the count moved.

        A patch only ever adds a toast to the rack: it opens no overlay, moves no focus and
        changes no route, whatever it carried.
        """
        if ATTENTION_ROUTE in routes:
            self.deliver_attention()
        if not self.is_running:
            return
        if self.route_key in routes or ATTENTION_ROUTE in routes:
            self.arrive()
        # a patch may have dropped a per-subject read the frame draws; read it again
        self._follow_route()

    def arrive(self) -> None:
        """Repaint for a projection event, which may never open, focus or navigate.

        An event that moved the session's stance is put back and counted in
        ``session.auto_opens``, which the golden journeys assert stays zero: the console
        interrupts only when the operator asked it to.
        """
        before = stance(self.session)
        self.render_frame()
        if stance(self.session) == before:
            return
        self.session.auto_opens += 1
        for name, value in before.items():
            setattr(self.session, name, value)
        self.session.log_key(DAEMON_KEY, "an arriving event moved the focus · put back")
        self.render_frame()

    def deliver_attention(self) -> None:
        """Toast each attention revision newly addressed to this principal, once.

        The first register read seeds the record of what was delivered, so an item that
        was already open when the console attached, or before a restart, is not toasted.
        """
        register = self.attention_view()
        if register is None:
            return
        if self._delivered is None:
            self._delivered = set(delivered_revisions(register))
            return
        for item in deliveries(register, principal=self.principal(), delivered=self._delivered):
            self._delivered.add((item.source_ref, item.revision))
            if self.toast_verbosity == "off":
                continue
            self.raise_toast(
                f"{item.key} {item.notification_class.value.replace('_', ' ')}",
                title="needs you",
                sev=Severity.WARN,
            )

    def on_resize(self, event: Resize) -> None:
        """Re-lay the frame at the new size.

        This handler runs before the app records the new size, so ``self.size`` still
        answers the old one here; the frame is laid once the new size is recorded.
        """
        self.call_next(self.render_frame)

    def quit(self) -> None:
        """End the console session."""
        self.exit()

    def _ctrl_c_quit(self, at: float | None = None) -> None:
        """Apply the guarded quit to Ctrl+C, honoured on every route, overlay and drawer.

        Ctrl+C is the operator's interrupt reflex, so unlike Escape (whose first meaning
        is "back", contextual to the route) it is never swallowed: it runs the same
        double-press guard Esc Esc uses at scope home, so a stray Ctrl+C from a terminal
        burst cannot end the session on its own, and the two guards share one arming
        timestamp so a Ctrl+C followed by an Esc Esc (or the reverse) still completes it.

        Args:
            at: When the press arrived, on the console clock; ``None`` reads the clock.
        """
        self.session.keys += 1
        check = quit_step(self.session, self.console_clock, at=at)
        if check.step is QuitStep.QUIT:
            self.session.log_key("Ctrl+C", f"quit - guarded, {check.gap_ms}ms apart")
            self.quit()
        elif check.step is QuitStep.BURST:
            self.session.log_key("Ctrl+C", "too fast to be two presses - still armed")
        else:
            self.session.log_key("Ctrl+C", "press again within 1.5s to quit")
            prompt_quit(self.session, self.console_clock)
        self.render_frame()

    @property
    def frame_size(self) -> tuple[int, int]:
        """Return the frame size: the terminal's less both gutters, else the session's.

        The session's size stands in only before the terminal has reported one.
        """
        w, h = self.size.width - 2 * self.gutter, self.size.height
        if w <= 0 or h <= 0:
            return SIZES[self.session.size]
        return (w, h)

    @property
    def route_key(self) -> str:
        """Return the port key of the session's route.

        The registry addresses a row by the pack's id and the seam by the port's key,
        and the normalisation map renames one route between them; comparing the wrong
        one would leave that route drawing its prototype registers forever.
        """
        spec = REGISTRY.by_id.get(self.session.route)
        return spec.key if spec is not None else self.session.route

    @property
    def subject(self) -> str | None:
        """Return the record the session's route is about: its subject, else the caret's row."""
        return self.session.subj_id or self.session.sel_id

    def _held_projection(self) -> RouteProjection | None:
        """Return the projection the seam holds for the session's own route.

        Only the session's own route answers it, so a frame never draws another route's
        rows. Each family states its own read model, and a route no family names draws
        nothing from it.
        """
        seam = self.seam
        if seam is None:
            return None
        return seam.projection_for(self.route_key)

    def attention_view(self) -> RegisterView | None:
        """Return the Attention register the header counts from, whatever route is drawn."""
        seam = self.seam
        held = seam.projection_for(ATTENTION_ROUTE) if seam is not None else None
        return build_register_view(held) if held is not None else None

    def route_view(self) -> SpineView | RouteReadModel | None:
        """Return the read model the session's route draws from, if one is held."""
        projection = self._held_projection()
        if projection is None:
            return None
        route = projection.route
        if route in NATIVE_ROUTES:
            return build_spine_view(projection)
        # the live reads pull the daemon's method registry in, as the seam does
        from eawf.surfaces.tui.console import live_reads

        def live(name: str) -> Any:
            return self.seam.live(name) if self.seam is not None else None

        if route in VERIFICATION_ROUTES:
            verdicts = live(live_reads.HEALTH_VERDICTS_READ) or ()
            return build_verification_view(projection, verdicts=verdicts)
        if route in OPERATIONS_ROUTES:
            return build_operations_view(projection, last_start=live(live_reads.BOOT_RECOVERY_READ))
        if route in INTEGRATION_ROUTES:
            return build_integration_view(
                projection,
                generations=live(live_reads.GENERATIONS_READ) or (),
                conflicts=live(live_reads.CONFLICTS_READ) or (),
                repository=live(live_reads.REPOSITORY_READ),
            )
        if route == TRANSCRIPT_ROUTE:
            lines = live(live_reads.TRANSCRIPT_READ)
            if not isinstance(lines, live_reads.HeldTranscript):
                return build_transcript_view(projection)
            return build_transcript_view(
                projection,
                events=lines.events,
                children=lines.children,
                contents=lines.contents,
                deadlines=lines.deadlines,
            )
        if route in ACCEPTANCE_ROUTES:
            # the Milestone's bundle and approval are read for the subject on screen alone
            held = self.seam.acceptance_for(self.subject) if self.seam is not None else None
            return build_acceptance_view(
                projection,
                bundle=held.bundle if held is not None else None,
                approval=held.approval if held is not None else None,
                receipts=live(live_reads.RECEIPTS_READ) or (),
            )
        return None

    def register_view(self) -> RegisterView | None:
        """Return the register read model the session's route draws from, if one is held."""
        projection = self._held_projection()
        if projection is None or projection.route not in REGISTER_ROUTES:
            return None
        return build_register_view(projection)

    def settings_view(self) -> EffectiveSettingsView | None:
        """Return the effective-settings view the settings routes draw from.

        Config is not read per route, so this one answer serves the settings list and the
        stack card it opens; every other route draws nothing from it.
        """
        seam = self.seam
        if seam is None or self.route_key not in SETTINGS_ROUTES:
            return None
        return seam.settings

    def _sync_conn(self) -> None:
        """Set the session's connection value from the seam, when the console has one.

        A console holding the prototype registers has no seam and keeps whatever value
        its setup gave it -- that is the tracked golden contract. A console holding a
        seam draws its connection value from it on every render, so the header can
        never be left showing a value nothing produced.
        """
        seam = self.seam
        if seam is not None:
            self.session.conn = conn_label(seam.connection)

    def view(self) -> View:
        """Return the render view at the current frame size."""
        self._sync_conn()
        w, h = self.frame_size
        register = self.register_view()
        liveness, timeline = self._liveness_and_timeline()
        return View(
            session=self.session,
            fixture=self.fixture,
            w=w,
            h=h,
            verbose=self.verbose,
            held=self.held,
            projection=self.route_view(),
            register=register,
            attention=self.attention_view(),
            settings=self.settings_view(),
            linked=self.seam is not None,
            principal_refusal=self.principal_refusal(),
            replay=self.seam.replay_note if self.seam is not None else None,
            rows=self.seam.held_rows() if self.seam is not None else (),
            notices=self.seam.notices if self.seam is not None else (),
            decisions=self.seam.decisions if self.seam is not None else None,
            liveness=liveness,
            timeline=timeline,
            principal=self.principal(),
            # a held clock reads no wall time, so a held frame is its authored instant
            now=self.console_clock.wall() if self.seam is not None and not self.held else None,
            scope_name=self.seam.scope_name if self.seam is not None else "",
            gutter=self.gutter,
            live=self._live_answers(),
        )

    def _live_answers(self) -> dict[str, object]:
        """Return the answers of the live reads the route on screen holds, by read name."""
        seam = self.seam
        if seam is None:
            return {}
        return {name: seam.live(name) for name in seam.live_on_screen()}

    def _liveness_and_timeline(self) -> tuple[HeldLiveness | None, RunTimeline | None]:
        """Return the stall read and the Run's timeline rows the route on screen holds."""
        if self.seam is None:
            return None, None
        # the live reads pull the daemon's method registry in, as the seam does
        from eawf.surfaces.tui.console.live_reads import LIVENESS_READ, RUN_TIMELINE_READ

        liveness, timeline = self.seam.live(LIVENESS_READ), self.seam.live(RUN_TIMELINE_READ)
        return (
            liveness if isinstance(liveness, HeldLiveness) else None,
            timeline if isinstance(timeline, RunTimeline) else None,
        )

    def principal(self) -> str | None:
        """Return who the console acts as, or ``None`` when it acts as nobody."""
        operator = self.seam.operator if self.seam is not None else None
        return operator.principal if operator is not None else None

    def principal_refusal(self) -> str:
        """Return why every bound write is refused before it is chosen, or nothing.

        A linked console acting as nobody could attribute no write, so each bound verb
        is shown refused ahead of time rather than refused once confirmed. A console
        with no link sends nothing anyway and says so when a verb is confirmed.
        """
        seam = self.seam
        return NO_PRINCIPAL_REASON if seam is not None and seam.operator is None else ""

    def reset(self, setup: SessionSetup | None) -> None:
        """Restore the session from ``setup``; the one canonical reset.

        A linked console draws the kernel catalog's rail, whose sections differ from the
        prototype catalog's, so the section cursor is placed in the order it will draw.
        """
        order = (
            catalog_section_order()
            if self.seam is not None
            else self.fixture.settings.section_order
        )
        self.session.reset(
            setup,
            settings_section_order=order,
            now=self.console_clock.now(),
        )
        self._follow_route()

    def raise_toast(self, text: str, *, title: str = "done", sev: Severity = Severity.INFO) -> None:
        """Raise a toast on the rack through the console's notify path."""
        notify(self.session, self.console_clock, text=text, title=title, sev=sev)

    def render_frame(self) -> None:
        """Sweep the rack, compose the frame and paint it."""
        sweep_toasts(self.session, self.console_clock)
        view = self.view()
        rows = compose_frame(view)
        if self.glyphs == "ascii":
            # plain mode renders through compose_frame, so it can only be imported here
            from eawf.surfaces.tui.console.plain import plain_rows

            rows = plain_rows(rows)
        self.frame_rows = rows
        self.render_count += 1
        self.query_one("#header", ProjectionHeader).set_rows(rows[:1])
        self.query_one("#body", Body).set_rows(rows[1 : view.h - 1])
        self.query_one("#keybar", KeybarRow).set_rows(rows[view.h - 1 :])

    def tick(self) -> None:
        """Expire toasts and the go prefix on the live clock, repainting on a change.

        The live reads of the route on screen are read again every
        :data:`LIVE_REFRESH_SECONDS`, so what lands behind them -- a Run's appended
        events -- appears while the operator watches.
        """
        changed = bool(sweep_toasts(self.session, self.console_clock))
        if expire_prefix(self.session, self.console_clock) or changed:
            self.render_frame()
        seam, now = self.seam, self.console_clock.now()
        if seam is None or now - self._live_read_at < LIVE_REFRESH_SECONDS:
            return
        names = seam.live_on_screen()
        if names or self.route_key == ATTENTION_ROUTE:
            self._live_read_at = now
            self.run_worker(self._reload_live(names), group=LIVE_WORKERS, exclusive=True)

    async def _reload_live(self, names: tuple[str, ...]) -> None:
        """Read *names* again and repaint when any answer changed.

        On the Attention route the register is read again too: a stall the sweep raises
        or a Run's answer that clears it is a run-ledger line, which no patch carries.
        """
        seam = self.seam
        assert seam is not None, "only started with a seam"
        moved = False
        if self.route_key == ATTENTION_ROUTE:
            before = seam.projection_for(ATTENTION_ROUTE)
            try:
                after = await seam.load(ATTENTION_ROUTE)
            except Exception as exc:
                logger.warning(f"attention re-read failed cause={exc!r}")
            else:
                if before is None or after.digest != before.digest:
                    moved = True
                    self.deliver_attention()
        for name in names:
            before = seam.live(name)
            try:
                moved |= await seam.load_live(name) != before
            except Exception as exc:
                # the frame keeps what it holds; the next tick asks again
                logger.warning(f"live re-read failed name={name} cause={exc!r}")
        if moved and self.is_running:
            self.arrive()

    def _ctx(self, pressed_at: float | None = None) -> Ctx:
        """Return the context one keystroke or pointer activation acts in."""
        view = self.view()
        return Ctx(
            session=self.session,
            fixture=self.fixture,
            host=self,
            w=view.w,
            h=view.h,
            verbose=self.verbose,
            projection=view.projection,
            unheld=unheld(view),
            send=self.send,
            attention=self.seam.projection_for(ATTENTION_ROUTE) if self.seam else None,
            principal_refusal=view.principal_refusal,
            settings=view.settings,
            outstanding=len(self.seam.outstanding) if self.seam else 0,
            rows=view.rows,
            decisions=view.decisions,
            notices=view.notices,
            principal=view.principal,
            scope=self.seam.scope_name or self.seam.scope_id if self.seam is not None else "",
            gutter=view.gutter,
            clipboard=self.copy_text,
            pressed_at=pressed_at,
            tree_root=self._tree_root(),
            recover=self.recover if self.seam is not None else None,
            first_run=self.first_run,
            onboard=self.onboard if self.first_run is not None else None,
            live=view.live,
        )

    def _tree_root(self) -> Path | None:
        """Return the ``.ea`` directory of the tree the link reads, when it names one."""
        root = self.seam.repo_root if self.seam is not None else None
        return root / EA_DIRNAME if root is not None else None

    def copy_text(self, text: str) -> bool:
        """Put ``text`` on the operator's clipboard; the console's one clipboard seam.

        The text goes out as the terminal's OSC 52 copy sequence, which reaches the system
        clipboard through the terminal, tmux and SSH alike. On macOS it also goes to the
        system clipboard command, since macOS Terminal ignores that sequence. A headless
        console, such as a test's, writes to no clipboard of the machine it runs on.

        Returns:
            Whether the text was written; ``False`` before the console has a terminal.
        """
        if not self.is_running:
            return False
        self.copy_to_clipboard(text)
        command = shutil.which(PBCOPY) if sys.platform == "darwin" else None
        if command is not None and not self.is_headless:
            subprocess.run([command], input=text.encode(), timeout=PBCOPY_TIMEOUT, check=False)
        return True

    def _arrival(self, event: Key) -> float:
        """Return when ``event`` arrived, on the console clock.

        The toolkit stamps a key when its parser emits it. The parser holds a lone Escape
        for its escape delay, and emits the first of two Escapes closer than that delay
        only when the second arrives, so such a pair is stamped one delay apart whatever
        its real gap. A second Escape stamped within that delay of the first is therefore
        given the first one's arrival: the guard cannot tell it from one terminal burst.
        """
        at = self.console_clock.now() - max(0.0, get_time() - event.time)
        last = self._escape_at
        escape = event.key == "escape"
        self._escape_at = at if escape else None
        if escape and last is not None and at - last <= ESCAPE_DELAY + ESCAPE_SLACK:
            return last
        return at

    def press_key(self, key: str, *, shift: bool = False, at: float | None = None) -> None:
        """Dispatch one key by its dispatcher name and repaint.

        A key a handler claimed that left the frame as it was is answered with a toast
        naming why, and the frame is painted again to show it.

        Args:
            key: The key, named the way the dispatcher matches it.
            shift: Whether Shift was held.
            at: When the key arrived, on the console clock; ``None`` reads the clock.
        """
        before, head, toasts = self.frame_rows, self.session.log[:1], len(self.session.toasts)
        focus = focus_of(self.session)
        ctx = self._ctx(at)
        dispatch(ctx, key, shift)
        self._follow_route()
        self.render_frame()
        still = self.frame_rows == before and focus_of(self.session) == focus
        if say_why(ctx, key, head=head, toasts=toasts, still=still):
            self.render_frame()

    def select_row_at(self, y: int) -> None:
        """Walk the row cursor to frame row ``y`` one arrow key at a time, as a row click does.

        The walk presses the keys the keyboard would, so a click reaches exactly the rows the
        keys reach and no verb the keys do not. It stops when the cursor stops moving, and a
        press that carries the cursor past ``y`` -- a line that is no row, such as a heading
        or the selected row's second line -- is taken back, so no row is selected by guess.

        Args:
            y: The frame row the pointer is on.
        """

        def caret() -> int | None:
            rows = self.frame_rows
            return next((i for i, row in enumerate(rows) if row.lstrip().startswith(CARET)), None)

        for _ in range(len(self.frame_rows)):
            before = caret()
            if before is None or before == y:
                return
            down = y > before
            self.press_key("ArrowDown" if down else "ArrowUp")
            after = caret()
            if after is None or after == before:
                return
            if (after > y) if down else (after < y):
                self.press_key("ArrowUp" if down else "ArrowDown")
                return

    def activate_crumb(self, step: CrumbRun) -> None:
        """Walk the breadcrumb to ``step`` and repaint."""
        activate_crumb(self._ctx(), step)
        self._follow_route()
        self.render_frame()

    def send(self, request: VerbRequest | BulkRequest) -> bool:
        """Send a confirmed verb through the seam, off the key path.

        Args:
            request: The confirmed verb to send, or a card's targets sent as one bulk
                operation.

        Returns:
            Whether a daemon link took the verb: ``False`` for a console with no seam, or
            one not yet running, where nothing is sent.
        """
        seam = self.seam
        if seam is None or not self.is_running:
            return False
        work = (
            self._deliver_bulk(seam, request)
            if isinstance(request, BulkRequest)
            else self._deliver(seam, request)
        )
        self.run_worker(work, group=WRITE_WORKERS)
        return True

    def recover(self, door: str) -> bool:
        """Take one Recovery door through the seam, off the key path.

        Args:
            door: The door the operator chose.

        Returns:
            Whether a daemon link took the door: ``False`` for a console with no seam, or
            one not yet running.
        """
        seam = self.seam
        if seam is None or not self.is_running:
            return False
        self.run_worker(self._take_door(seam, door), group=WRITE_WORKERS)
        return True

    async def _take_door(self, seam: ProjectionSeam, door: str) -> None:
        """Wait for the door to be taken, then say where it left the console."""
        try:
            value = await seam.take_door(door)
        except (DaemonRpcError, OSError, ValueError) as error:
            self.raise_toast(
                f"{door} did not complete · {error}", title="recovery", sev=Severity.WARN
            )
        else:
            self.session.conn = conn_label(value)
            self.raise_toast(f"{door} · the console is {conn_label(value)}", title="recovery")
        if self.is_running:
            self.arrive()

    def onboard(self) -> bool:
        """Perform the confirmed first-run workspace step through the daemon, off the key path.

        Returns:
            Whether the step was started: ``False`` outside a first run or before the
            console runs.
        """
        first_run = self.first_run
        if first_run is None or not self.is_running:
            return False
        self.run_worker(self._file_workspace(first_run), group=WRITE_WORKERS)
        return True

    async def _file_workspace(self, first_run: FirstRun) -> None:
        """Wait for the daemon to register the workspace, then state the step as done."""
        try:
            done = await asyncio.to_thread(register_workspace, first_run)
        except (DaemonRpcError, OSError, RegistryReadError) as error:
            self.raise_toast(
                f"nothing was registered · {error}", title="not registered", sev=Severity.WARN
            )
        else:
            entry = next(state for state in self.fixture.proto.entry if state.id == ONBOARDING)
            chrome = with_entry_state(self.fixture.chrome, registered_state(entry, first_run.code))
            self.fixture = Fixture.from_chrome(chrome)
            self.raise_toast(done, title="registered")
            self.session.log_key(DAEMON_KEY, done)
        if self.is_running:
            self.arrive()

    async def _deliver(self, seam: ProjectionSeam, request: VerbRequest) -> None:
        """Wait for the daemon's answer to one verb, then say what became of it."""
        self.announce(await seam.request(request))

    async def _deliver_bulk(self, seam: ProjectionSeam, request: BulkRequest) -> None:
        """Wait for the daemon's answer to one bulk operation, then settle every row."""
        for result in await seam.bulk(request):
            self.announce(result)

    def announce(self, result: OperationResult) -> None:
        """Say what became of a sent verb, in the rack and the key log, and repaint.

        Args:
            result: What the daemon, or the seam on its behalf, answered.
        """
        outcome = result.disposition
        settle(self.session, result, self.console_clock.now())
        self.raise_toast(result.detail, title=outcome.value, sev=WRITE_TOAST_SEVERITY[outcome])
        self.session.log_key(DAEMON_KEY, f"{outcome.value} · {result.detail}")
        if self.is_running:
            self.arrive()

    def on_key(self, event: Key) -> None:
        """Take every key from the toolkit and dispatch it.

        Ctrl+C is handled here rather than through the route dispatcher: it is a
        global interrupt, not a route verb, so no overlay, drawer or entry-layer
        allowlist gets a chance to swallow it the way it used to.
        """
        event.stop()
        event.prevent_default()
        at = self._arrival(event)
        if event.key == "ctrl+c":
            self._ctrl_c_quit(at)
            return
        named = dispatcher_key(event.key, event.character)
        if named is not None:
            self.press_key(named[0], shift=named[1], at=at)
