"""``Header`` — shared chassis header (widget catalog).

A single :class:`~textual.widgets.Static` composite reused by every
per-scope screen (``RepoScreen`` / ``WorkspaceScreen`` / ``UserScreen``)
with **no per-scope duplication**. It renders, left to right:

* the literal ``Eä`` brand (capital E + a-umlaut), bold-accent styled,
  positioned **outside-left** of the breadcrumb per the operator
  branding convention;
* a full-location ``scope > code > phase > iter > mode`` breadcrumb
  derived from the bound :class:`~eawf.kernel.state.models.State`
  (rendered with the angle-ornament separator :data:`CRUMB_SEP`), with an
  optional trailing ``> <entity>`` segment when a peek/detail is open.
  Only the ``phase`` + ``iter`` segments are Textual ``[@click=...]`` nav
  links (to their reference cards); the ``scope``, ``code``, ``mode``, and
  ``entity`` segments render as plain (non-clickable) text — see
  :func:`build_breadcrumb` for the per-segment wiring;
* a runtime cell — ``runtime: idle`` muted when nothing is dispatched,
  flipping to ``runtime: <runtime> - <n> running`` (the active runtime id
  + the running-wave count) when one or more waves are active;
* a UTC clock (``HH:MM UTC``).

The header is driven by the host :class:`~eawf.surfaces.tui.app.EaApp`
reactive ``state``: on mount it seeds from ``app.state`` and registers a
watcher so daemon-pushed (or mtime-poll) revisions repaint the
breadcrumb + runtime cell in place. Standalone tests assign
:attr:`state` directly and the same repaint fires.

The brand string, the project-code fallback, and the breadcrumb builder
live here as the single source of truth; :mod:`eawf.surfaces.tui.app`
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

from eawf.kernel.state.enums import AgentSessionStatus
from eawf.surfaces.tui.widgets.markup import escape_markup

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

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


def _link(label: str, action: str | None) -> str:
    """Wrap *label* in a Textual ``[@click=...]`` span, or escape it plain.

    *label* is always markup-escaped (a project code or entity title can
    carry a ``[`` that the content-markup parser would otherwise eat). When
    *action* is given the escaped label is wrapped in a ``[@click=action]``
    link; when it is ``None`` the segment renders as plain (non-clickable)
    escaped text — the honest fallback for a segment with no existing nav
    action to wire to.

    Args:
        label: The segment display text.
        action: The Textual action string to fire on click (e.g.
            ``"app.switch_scope('repo')"`` -- ``app.``-namespaced so it
            resolves against the host App, not the Static owning the link),
            or ``None`` for a plain segment.

    Returns:
        The content-markup for the segment.
    """
    safe = escape_markup(label)
    if action is None:
        return safe
    return f"[@click={action}]{safe}[/]"


def build_breadcrumb(
    state: State | None,
    scope: str | None = None,
    mode: str | None = None,
    *,
    mode_name: str | None = None,
    entity: str | None = None,
    clickable: bool = False,
) -> str:
    """Build the ``scope > code > phase > iter > mode`` breadcrumb from state.

    The breadcrumb reads as a full-location trail, left (broad) to right
    (specific): the *scope* segment (the active **screen** scope:
    ``repo`` / ``workspace`` / ``user``) comes from *scope* when supplied,
    falling back to ``state.scope_kind`` otherwise. The override matters
    for the user scope, whose synthesized portfolio state carries
    ``scope_kind=workspace`` — without it the user screen's breadcrumb
    would lead with ``workspace`` instead of ``user``. The *code* segment
    is the project code; *phase* and *iter* come from
    ``state.current.phase_id`` / ``state.current.iter_id`` and appear only
    while each is active. The trailing *mode* segment is the active content
    mode (Home / Trust / Doctor / ...); mode and scope are orthogonal axes,
    so the mode trails the in-mode location. An optional *entity* segment
    trails everything when a peek/detail is open.

    When *clickable* is set, ONLY the *phase* + *iter* segments become
    Textual ``[@click=...]`` links. The actions are ``app.``-namespaced so
    Textual resolves them against the host
    :class:`~eawf.surfaces.tui.app.EaApp` (which defines them) rather than
    against the :class:`~textual.widgets.Static` that owns the markup link
    (a bare action would resolve against the Static, find nothing, and
    silently no-op the click):

    * phase -> ``app.open_phase_ref('<phase>')`` (the phase reference card);
    * iter  -> ``app.open_iter_ref('<iter>')`` (the iter reference card).

    The *scope*, *code*, *mode*, and *entity* segments render as plain
    (non-clickable) escaped text even on the clickable path: a click on a
    plain segment carries no ``[@click=...]`` action, so it fires no
    navigation (a genuine de-link, not a styled-but-live near-miss). The
    *scope* segment had a ``app.switch_scope`` link and the *code* segment a
    return-to-Home ``app.switch_mode('home')`` link before the de-link; both
    are gone — the App still owns those actions (reachable by keybinding),
    the breadcrumb just no longer wires a click to them. When *clickable* is
    unset every segment is plain escaped text — the contract the non-TTY
    status frame relies on.

    Falls back to :data:`DEFAULT_PROJECT_CODE` when no state is loaded so
    the header stays informative during the daemon cold-spawn window; the
    *mode* segment, when given, still trails the fallback so the active
    mode is visible before first state load.

    Args:
        state: The currently bound state, or ``None`` before first load.
        scope: The active screen scope name to use for the scope segment,
            or ``None`` to read it from ``state.scope_kind``.
        mode: The active mode **title** to trail the breadcrumb with, or
            ``None`` to omit the mode segment.
        mode_name: The active mode **name** (registry key) the mode
            segment links to via ``app.switch_mode``; ``None`` leaves the
            mode segment plain text even when *clickable* is set.
        entity: An optional trailing entity label (an open peek/detail),
            or ``None`` to omit it.
        clickable: Emit ``[@click=...]`` markup for the wired segments when
            ``True``; render every segment as plain escaped text when
            ``False`` (the default — the non-markup / ASCII consumers).

    Returns:
        The breadcrumb string (without the brand prefix).
    """
    parts: list[str] = []
    if state is None:
        parts.append(_link(DEFAULT_PROJECT_CODE, None))
    else:
        code = state.project.code if state.project is not None else DEFAULT_PROJECT_CODE
        scope_label = scope if scope is not None else state.scope_kind.value
        # scope + code are de-linked plain text on every path (the operator
        # decision: the scope screen-switch and the code return-to-Home
        # shortcuts move off the breadcrumb). The App still owns the
        # underlying actions; the breadcrumb just no longer wires a click.
        parts.append(_link(scope_label, None))
        parts.append(_link(code, None))
        if state.current.phase_id is not None:
            phase = state.current.phase_id
            parts.append(_link(phase, f"app.open_phase_ref({phase!r})" if clickable else None))
        if state.current.iter_id is not None:
            iter_id = state.current.iter_id
            parts.append(_link(iter_id, f"app.open_iter_ref({iter_id!r})" if clickable else None))
    if mode is not None:
        # The trailing mode (leaf) segment is de-linked plain text -- EXCEPT
        # under the POC_DEFECTS_ENV build flag, which leaves its
        # app.switch_mode('<mode_name>') link live: the planted W10 near-miss
        # where the leaf still navigates despite the de-link decision (the
        # subtle regression a plain-rendered breadcrumb hides from a frame).
        # With the flag unset (the default) the leaf is genuinely de-linked.
        from eawf.surfaces.tui.poc_defects import poc_defects_enabled

        link_mode = clickable and mode_name is not None and poc_defects_enabled()
        mode_action = f"app.switch_mode({mode_name!r})" if link_mode else None
        parts.append(_link(mode, mode_action))
    if entity is not None:
        parts.append(_link(entity, None))
    return CRUMB_SEP.join(parts)


def active_runtime_id(state: State | None) -> str | None:
    """Return the runtime id currently driving an active wave, or ``None``.

    Reads the active runtime off the bound state's agent sessions: an
    ACTIVE :class:`~eawf.kernel.state.models.AgentSession` is a live
    runtime-backed subagent, and its ``runtime`` field is the adapter id
    (``"claude"`` / ``"codex"`` / ``"opencode"``). When several are ACTIVE
    the most-recently-started one wins (the runtime the operator most
    likely just dispatched), mirroring the agent-watch zoom's pick. Returns
    ``None`` when no ACTIVE session carries a runtime — the honest case
    where the header knows a wave is running but not yet which runtime.

    Args:
        state: The currently bound state, or ``None``.

    Returns:
        The active runtime adapter id, or ``None`` when none is resolvable.
    """
    if state is None or not state.agent_sessions:
        return None
    active = [
        sess for sess in state.agent_sessions.values() if sess.status is AgentSessionStatus.ACTIVE
    ]
    if not active:
        return None
    return max(active, key=lambda sess: sess.started_at).runtime


def runtime_cell_text(state: State | None) -> str:
    """Return the runtime-cell label for *state*.

    The runtime cell shows ``runtime: idle`` (muted) when no wave is
    dispatched. Once one or more waves are active it surfaces the live
    runtime id + the running-wave count, e.g. ``runtime: claude - 2
    running``. When waves are active but no ACTIVE agent session resolves a
    runtime id (the header knows N waves run but not yet which runtime), it
    falls back to ``runtime: <n> running`` — honest about the count without
    inventing a runtime name.

    Args:
        state: The currently bound state, or ``None``.

    Returns:
        The runtime-cell text, e.g. ``runtime: idle`` or ``runtime: claude
        - 2 running``.
    """
    if state is None or not state.current.active_wave_ids:
        return f"runtime: {RUNTIME_IDLE}"
    running = len(state.current.active_wave_ids)
    runtime = active_runtime_id(state)
    if runtime is None:
        return f"runtime: {running} running"
    return f"runtime: {runtime} - {running} running"


def _clock_text() -> str:
    """Return the current wall-clock as ``HH:MM UTC``."""
    return f"{datetime.now(UTC):%H:%M} UTC"


def render_header(
    state: State | None,
    scope: str | None = None,
    mode: str | None = None,
    *,
    mode_name: str | None = None,
    entity: str | None = None,
) -> str:
    """Render the full header content-markup line from *state*.

    Pure render source — unit-testable without mounting the widget. The
    brand is wrapped in a ``[$accent][b]…[/b][/]`` span so it carries the
    palette accent colour + bold; the breadcrumb segments carry their
    ``[@click=...]`` nav links (this is the clickable render path), and the
    runtime cell is muted via ``[$muted]…[/]``.

    Args:
        state: The currently bound state, or ``None``.
        scope: The active screen scope name driving the breadcrumb's
            scope segment, or ``None`` to read it from ``state.scope_kind``.
        mode: The active mode title trailing the breadcrumb, or ``None`` to
            omit the mode segment.
        mode_name: The active mode name the mode segment links to via
            ``switch_mode``, or ``None`` to leave the mode segment plain.
        entity: An optional trailing entity label (an open peek/detail).

    Returns:
        A Textual content-markup string for the header line.
    """
    crumb = build_breadcrumb(state, scope, mode, mode_name=mode_name, entity=entity, clickable=True)
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
        attribute (e.g. mounted under a bare harness). Also subscribes to
        the app's mode-change signal so the breadcrumb's mode segment
        repaints when ``switch_mode`` flips the active mode (the signal
        fires after ``current_mode`` is updated, so a first-switch mount
        that read the prior mode is corrected on the same flip).
        """
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        mode_signal = getattr(self.app, "mode_change_signal", None)
        if mode_signal is not None:
            mode_signal.subscribe(self, self._on_mode_change)
        self._repaint()

    def _on_mode_change(self, _mode: str) -> None:
        """Repaint the header line when the active mode changes."""
        self._repaint()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this widget's reactive."""
        self.state = new_state

    def watch_state(self) -> None:
        """Repaint when the bound state changes."""
        self._repaint()

    def _repaint(self) -> None:
        """Re-render the header line from the current state + bound nav position.

        Reads the host app's bound nav position (``EaApp.nav_position``) so
        the breadcrumb's scope + mode segments track the validated
        ``(scope, mode)`` the operator is on -- the single source of truth
        the nav state machine owns -- rather than the bound state's
        ``scope_kind`` (which reads ``workspace`` for the user scope's
        synthesized portfolio). The mode segment is passed both its title
        (for display) and its name (for the ``app.switch_mode`` click
        target).
        A bare harness without ``nav_position`` falls back to the separate
        ``_scope`` / ``current_mode`` fields, and one without those falls
        back gracefully (no mode segment, ``state.scope_kind`` for the
        scope).
        """
        from eawf.surfaces.tui.modes import mode_title

        position = getattr(self.app, "nav_position", None)
        if position is not None:
            scope: str | None = position.scope
            mode_name: str | None = position.mode
            mode: str | None = mode_title(position.mode)
        else:
            scope = getattr(self.app, "_scope", None)
            raw_mode = getattr(self.app, "current_mode", None)
            mode_name = raw_mode if isinstance(raw_mode, str) else None
            mode = mode_title(raw_mode) if isinstance(raw_mode, str) else None
        self.update(render_header(self.state, scope, mode, mode_name=mode_name))


__all__ = [
    "BRAND",
    "CRUMB_SEP",
    "DEFAULT_PROJECT_CODE",
    "RUNTIME_IDLE",
    "Header",
    "active_runtime_id",
    "build_breadcrumb",
    "render_header",
    "runtime_cell_text",
]
