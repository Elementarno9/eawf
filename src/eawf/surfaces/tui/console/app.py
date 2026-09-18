"""The console app: one screen of three row widgets, one key dispatcher, one clock.

:func:`compose_frame` is the frame renderer. An armed go prefix or an open drawer keeps the
route frame and replaces its tail; a full-frame overlay replaces the frame; otherwise the
route's own frame is shown. The app never caches the frame size: it reads its own size at
render time, so a terminal resize re-lays the frame on the next render. A held clock
registers no timer; a live clock sweeps the rack and the go prefix four times a second
through the app's one interval.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import TYPE_CHECKING

from rich.segment import Segment
from rich.style import Style
from textual.app import App, ComposeResult
from textual.events import Key, Resize
from textual.strip import Strip
from textual.widget import Widget

from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.delivery.integration import IntegrationConflict, IntegrationGeneration
from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.integration import INTEGRATION_ROUTES, build_integration_view
from eawf.kernel.projection.operations import OPERATIONS_ROUTES, build_operations_view
from eawf.kernel.projection.registers import (
    ATTENTION_ROUTE,
    REGISTER_ROUTES,
    RegisterView,
    build_register_view,
)
from eawf.kernel.projection.route_view import RouteReadModel
from eawf.kernel.projection.settings import SETTINGS_ROUTES, SettingsView
from eawf.kernel.projection.spine import NATIVE_ROUTES, SpineView, build_spine_view
from eawf.kernel.projection.transcript import TRANSCRIPT_ROUTE, build_transcript_view
from eawf.kernel.projection.verification import (
    VERIFICATION_ROUTES,
    RuntimeTupleVerdict,
    build_verification_view,
)
from eawf.kernel.runtime.events import RunEventRecord
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
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session, SessionSetup
from eawf.surfaces.tui.console.tokens import Severity
from eawf.surfaces.tui.console.width import cell_len, pad
from eawf.workflow.delivery.acceptance import AcceptanceApproval
from eawf.workflow.projection.acceptance import ACCEPTANCE_ROUTES, build_acceptance_view

if TYPE_CHECKING:
    # imported for the type alone: the seam pulls the daemon method registry in, and a
    # console that is drawing the prototype registers has no business registering verbs
    from eawf.surfaces.tui.console.seam import ProjectionSeam

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
        seam: The console's one link to the daemon projection. A console given one draws
            the bound spine, register, verification, operations, integration and
            transcript routes from the read model it holds; a console given none draws
            the prototype registers, which is the mode the tracked golden contract
            replays.
        health_verdicts: The conformance verdicts the health route draws. A console
            given none draws the unknown token for every tuple cell, which is the
            honest answer while nothing has read the conformance store for it.
        integration_generations: The Batch generations the Git surface draws, oldest
            first. Generations are ledger lines rather than document rows, so they
            arrive beside the projection under the same rule as the health verdicts.
        integration_conflicts: The conflict frames the conflict card draws. A console
            given none draws no hunk and says the Batch is not blocked.
        run_events: The Run event lines the transcript draws, in any order. A console
            given none draws no block rather than a block that says nothing.
        acceptance_bundle: The sealed bundle the Milestone frame draws. A bundle is a
            process record rather than a document row, so it arrives beside the
            projection under the same rule as the health verdicts.
        acceptance_approval: The approval given to a bundle digest. It is drawn only
            against the bundle whose digest it names, so a later revision never inherits
            an earlier consent.
        proof_receipts: The receipts a receipt card may open, in record order. A console
            given none opens no card and says the receipt is not held.
    """

    CSS = """
    Screen { background: $background; }
    """

    def __init__(
        self,
        fixture: Fixture,
        clock: Clock | None = None,
        *,
        verbose: bool = False,
        seam: ProjectionSeam | None = None,
        health_verdicts: Sequence[RuntimeTupleVerdict] = (),
        integration_generations: Sequence[IntegrationGeneration] = (),
        integration_conflicts: Sequence[IntegrationConflict] = (),
        run_events: Sequence[RunEventRecord] = (),
        acceptance_bundle: MilestoneAcceptanceBundle | None = None,
        acceptance_approval: AcceptanceApproval | None = None,
        proof_receipts: Sequence[ProofReceipt] = (),
    ) -> None:
        super().__init__()
        self.fixture = fixture
        self.console_clock: Clock = clock or Clock()
        self.verbose = verbose
        self.seam = seam
        self.health_verdicts = tuple(health_verdicts)
        self.integration_generations = tuple(integration_generations)
        self.integration_conflicts = tuple(integration_conflicts)
        self.run_events = tuple(run_events)
        self.acceptance_bundle = acceptance_bundle
        self.acceptance_approval = acceptance_approval
        self.proof_receipts = tuple(proof_receipts)
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

    @property
    def route_key(self) -> str:
        """Return the port key of the session's route.

        The registry addresses a row by the pack's id and the seam by the port's key,
        and the normalisation map renames one route between them; comparing the wrong
        one would leave that route drawing its prototype registers forever.
        """
        spec = REGISTRY.by_id.get(self.session.route)
        return spec.key if spec is not None else self.session.route

    def _held_projection(self) -> RouteProjection | None:
        """Return the projection the seam holds for the session's own route.

        The seam carries one route, so a projection for another route is not this route's
        answer and the frame falls back rather than drawing another route's rows. Each
        family states its own read model, and a route no family names falls back too.
        """
        seam = self.seam
        if seam is None or seam.route != self.route_key:
            return None
        return seam.projection

    def route_view(self) -> SpineView | RouteReadModel | None:
        """Return the read model the session's route draws from, if one is held."""
        projection = self._held_projection()
        if projection is None:
            return None
        route = projection.route
        if route in NATIVE_ROUTES:
            return build_spine_view(projection)
        if route in VERIFICATION_ROUTES:
            return build_verification_view(projection, verdicts=self.health_verdicts)
        if route in OPERATIONS_ROUTES:
            return build_operations_view(projection)
        if route in INTEGRATION_ROUTES:
            return build_integration_view(
                projection,
                generations=self.integration_generations,
                conflicts=self.integration_conflicts,
            )
        if route == TRANSCRIPT_ROUTE:
            return build_transcript_view(projection, events=self.run_events)
        if route in ACCEPTANCE_ROUTES:
            return build_acceptance_view(
                projection,
                bundle=self.acceptance_bundle,
                approval=self.acceptance_approval,
                receipts=self.proof_receipts,
            )
        return None

    def register_view(self) -> RegisterView | None:
        """Return the register read model the session's route draws from, if one is held."""
        projection = self._held_projection()
        if projection is None or projection.route not in REGISTER_ROUTES:
            return None
        return build_register_view(projection)

    def settings_view(self) -> SettingsView | None:
        """Return the effective-settings view the settings routes draw from.

        Config is not read per route, so this one answer serves the settings list and the
        stack card it opens; every other route draws nothing from it.
        """
        seam = self.seam
        if seam is None or self.route_key not in SETTINGS_ROUTES:
            return None
        return seam.settings

    def view(self) -> View:
        """Return the render view at the current frame size."""
        w, h = self.frame_size
        register = self.register_view()
        return View(
            session=self.session,
            fixture=self.fixture,
            w=w,
            h=h,
            verbose=self.verbose,
            held=self.held,
            projection=self.route_view(),
            register=register,
            # the header prints the attention count on every route, so it is held only
            # while the one seam is bound to the route that states it
            attention=register
            if register is not None and register.route == ATTENTION_ROUTE
            else None,
            settings=self.settings_view(),
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
            projection=view.projection,
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
