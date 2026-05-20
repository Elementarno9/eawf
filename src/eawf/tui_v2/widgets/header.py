"""``Header`` — shared chassis header (widget catalog).

A single :class:`~textual.widgets.Static` composite reused by every
per-scope screen (``RepoScreen`` / ``WorkspaceScreen`` / ``UserScreen``)
with **no per-scope duplication**. It renders, left to right:

* the literal ``Eä`` brand (capital E + a-umlaut), bold-accent styled,
  positioned **outside-left** of the breadcrumb per the operator
  branding convention;
* a ``scope > code > phase`` breadcrumb derived from the bound
  :class:`~eawf.state.models.State` (rendered with the angle-ornament
  separator :data:`CRUMB_SEP`);
* a runtime cell — ``runtime: idle`` muted when nothing is
  dispatched, flipping to the active runtime when a wave is running;
* a UTC clock (``HH:MM UTC``).

The header is driven by the host :class:`~eawf.tui_v2.app.EaApp`
reactive ``state``: on mount it seeds from ``app.state`` and registers a
watcher so daemon-pushed (or mtime-poll) revisions repaint the
breadcrumb + runtime cell in place. Standalone tests assign
:attr:`state` directly and the same repaint fires.

The brand string, the project-code fallback, and the breadcrumb builder
live here as the single source of truth; :mod:`eawf.tui_v2.app`
re-exports them so the scope-dispatch shell and the breadcrumb tests
share one definition (DRY) without a circular import. Colours resolve
against the ``theme.tcss`` palette vars (``$accent`` for the brand, the
``runtime`` cell band) — never hardcoded hex — so the runtime theme swap
stays a CSS var rebind.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar

from textual.reactive import reactive
from textual.widgets import Static

if TYPE_CHECKING:
    from eawf.state.models import State

#: The brand string rendered outside-left of the scope breadcrumb.
#: Literal ``Eä`` (capital E + a-umlaut) per the operator branding
#: convention; bold-accent styling is applied via ``theme.tcss``.
BRAND: str = "Eä"

#: Fallback project code shown in the breadcrumb when no state is loaded
#: yet (fresh workspace / daemon cold-spawn placeholder).
DEFAULT_PROJECT_CODE: str = "EAWF"

#: Breadcrumb segment separator — the angle ornament in the Header row.
#: The glyph is intentional UI chrome (not a ``>`` typo), so the
#: ambiguous-unicode lint is suppressed on the literal.
CRUMB_SEP: str = " ❯ "  # noqa: RUF001

#: Idle runtime-cell text shown when no wave is dispatched — the cell
#: stays visible and muted so the operator sees the field exists.
RUNTIME_IDLE: str = "idle"


def build_breadcrumb(state: State | None) -> str:
    """Build the ``scope > code > phase`` breadcrumb from typed state.

    Falls back to :data:`DEFAULT_PROJECT_CODE` when no state is loaded so
    the header stays informative during the daemon cold-spawn window.

    Args:
        state: The currently bound state, or ``None`` before first load.

    Returns:
        The breadcrumb string (without the brand prefix).
    """
    if state is None:
        return DEFAULT_PROJECT_CODE
    code = state.project.code if state.project is not None else DEFAULT_PROJECT_CODE
    parts: list[str] = [state.scope_kind.value, code]
    if state.current.phase_id is not None:
        parts.append(state.current.phase_id)
    return CRUMB_SEP.join(parts)


def runtime_cell_text(state: State | None) -> str:
    """Return the runtime-cell label for *state*.

    The runtime cell shows ``runtime: idle`` (muted) when no wave is
    dispatched; once a wave is active it surfaces ``runtime: active``.
    The richer per-runtime id + switchover colour banding lands with
    the event-stream wiring in a later wave; this is the idle/active
    seam those revisions extend.

    Args:
        state: The currently bound state, or ``None``.

    Returns:
        The runtime-cell text, e.g. ``runtime: idle``.
    """
    if state is None or not state.current.active_wave_ids:
        return f"runtime: {RUNTIME_IDLE}"
    return "runtime: active"


def _clock_text() -> str:
    """Return the current wall-clock as ``HH:MM UTC``."""
    return f"{datetime.now(UTC):%H:%M} UTC"


def render_header(state: State | None) -> str:
    """Render the full header content-markup line from *state*.

    Pure render source — unit-testable without mounting the widget. The
    brand is wrapped in a ``[$accent][b]…[/b][/]`` span so it carries the
    palette accent colour + bold; the runtime cell is muted via
    ``[$muted]…[/]``.

    Args:
        state: The currently bound state, or ``None``.

    Returns:
        A Textual content-markup string for the header line.
    """
    crumb = build_breadcrumb(state)
    runtime = runtime_cell_text(state)
    return (
        f"[$accent][b]{BRAND}[/b][/]  {crumb}    [$muted]{runtime}[/]    [$muted]{_clock_text()}[/]"
    )


class Header(Static):
    """Shared chassis header: ``Eä`` brand + breadcrumb + runtime + clock.

    Reused verbatim by every per-scope screen (shared chassis). Reads
    the host app's reactive ``state`` (seeded on mount) and repaints on
    every revision. Standalone-testable by assigning :attr:`state`
    directly.
    """

    DEFAULT_CSS: ClassVar[str] = """
    Header {
        height: 1;
        dock: top;
        background: $panel;
        color: $text;
        padding: 0 1;
    }
    """

    #: Bound state, watched so a fresh revision repaints the breadcrumb +
    #: runtime cell. ``None`` until the first read-only load completes.
    state: reactive[State | None] = reactive(None)

    def on_mount(self) -> None:
        """Seed from the app's reactive state and watch for revisions.

        Standalone tests that assign :attr:`state` directly do not need
        the app watcher; the guard skips it when the host has no ``state``
        attribute (e.g. mounted under a bare harness).
        """
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        self._repaint()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def watch_state(self) -> None:
        """Repaint when the bound state changes."""
        self._repaint()

    def _repaint(self) -> None:
        """Re-render the header line from the current state."""
        self.update(render_header(self.state))


__all__ = [
    "BRAND",
    "CRUMB_SEP",
    "DEFAULT_PROJECT_CODE",
    "RUNTIME_IDLE",
    "Header",
    "build_breadcrumb",
    "render_header",
    "runtime_cell_text",
]
