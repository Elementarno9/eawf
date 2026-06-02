"""``AutopilotModeScreen`` -- the ready-wave frontier + dispatch pane (mode digit 9).

The Autopilot mode (digit ``9``) renders the **dependency frontier** of the
active scope's wave graph -- the PENDING waves that are claim-ready right now
(every dep CLOSED + no lower-numbered ready sibling under the same iter) -- in
claim order, and offers **dispatch controls** that ask the daemon to claim +
spawn the selected ready wave.

Reusing the frontier compute (not a second predicate)
-----------------------------------------------------
The pane never re-derives the claimability rule. It projects the bound
read-only :class:`~eawf.kernel.state.models.State` into the slim injected
view (:class:`~eawf.kernel.spec.auq_bridge.WaveFrontierItem` rows -- id, iter,
status, deps) and calls the shared
:func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier`, which encodes the
exact claim-time gate (:func:`eawf.workflow.lifecycle.wave.claim_wave`) purely
off the view. So the frontier the operator sees is the same set the claim gate
would accept, single-sourced -- this pane is READ-only over state and adds no
predicate of its own. The resulting
:class:`~eawf.kernel.spec.auq_bridge.DrainableFrontier` carries the ready rows
in claim order, which the pane lists one per row.

Dispatch controls (the daemon-client seam)
------------------------------------------
The dispatch action (the ``d`` key) asks the daemon to live-spawn the selected
ready wave by calling the ``agent.dispatch`` JSON-RPC method with ``spawn=True``
(the W01 live-spawn path) through the same
:class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the rest of the TUI
mutates through. The daemon owns claim + session registration + the spawn behind
the safety floor (jailed argv + scrubbed env); the TUI only issues the request
and surfaces the typed result honestly. When the daemon socket is unavailable,
or the spawn path rejects / errors, the action surfaces that honestly rather
than faking a dispatch -- a live spawn that did not happen is never reported as
one.

Honest-empty is the COMMON path: a scope whose wave graph has no claim-ready
wave (every wave CLOSED, or the next waves still blocked on open deps) renders
the muted :data:`EMPTY_NOTICE` banner instead of an empty table that reads as a
quiet "ready to go", exactly like the evidence / trust / research modes'
honest-empty surfaces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Static

from eawf.kernel.spec.auq_bridge import (
    DrainableFrontier,
    WaveFrontierItem,
    compute_ready_frontier,
)
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.markup import escape_markup

if TYPE_CHECKING:
    from eawf.kernel.state.models import State, Wave

logger = logging.getLogger(__name__)

#: Id of the frontier header banner (the ready-count line above the list).
FRONTIER_HEADER_ID: str = "autopilot-header"

#: Id of the scrollable ready-wave list container.
FRONTIER_LIST_ID: str = "autopilot-list"

#: Id of the honest-empty notice shown when no wave is claim-ready.
FRONTIER_EMPTY_ID: str = "autopilot-empty"

#: Id of the dispatch-result line (below the list); honest about whether the
#: dispatch request was issued, accepted, or could not reach the daemon.
DISPATCH_RESULT_ID: str = "autopilot-result"

#: CSS class on each rendered ready-wave row.
FRONTIER_ROW_CLASS: str = "autopilot-row"

#: CSS class flagging the selected ready-wave row (the dispatch target).
SELECTED_ROW_CLASS: str = "-selected"

#: Notice when the wave graph has no claim-ready wave. Phrased so the empty
#: surface is unmistakable rather than reading as a quiet "ready to go".
EMPTY_NOTICE: str = "no ready waves"

#: Result line before any dispatch has been issued (the idle dispatch surface).
DISPATCH_IDLE: str = "press d to dispatch the selected ready wave"

#: Result line when the dispatch request could not reach the daemon.
DISPATCH_NO_DAEMON: str = "dispatch: daemon unavailable -- request not issued"

#: Result line when there is no ready wave to dispatch.
DISPATCH_NO_TARGET: str = "dispatch: no ready wave to dispatch"

#: Footer hints for the Autopilot pane (full key names, arrows primary).
_AUTOPILOT_HINTS: tuple[str, ...] = (
    "up/down select",
    "d dispatch",
    "1-9 mode",
    "w/r/u scope",
    "/ palette",
    "? help",
    "q quit",
)


@dataclass(frozen=True)
class ReadyWaveRow:
    """One ready-frontier wave projected for the autopilot list.

    A display projection of a ready
    :class:`~eawf.kernel.spec.auq_bridge.WaveFrontierItem` enriched with the
    wave's title (read off the bound state) so the row is scannable without a
    second lookup. Produced only from the computed frontier, so every row is a
    genuinely claim-ready wave in claim order.

    Attributes:
        wave_id: The ready wave id (e.g. ``P29-I04-W12``).
        iter_id: The wave's parent iter id.
        title: The wave's bounded title, or the empty string when the wave
            row carries none (defensive -- every wave has a title in state).
    """

    wave_id: str
    iter_id: str
    title: str


def build_frontier_items(state: State | None) -> tuple[WaveFrontierItem, ...]:
    """Project the bound state's waves into the frontier-compute view.

    Maps each :class:`~eawf.kernel.state.models.Wave` in *state* onto the slim
    :class:`~eawf.kernel.spec.auq_bridge.WaveFrontierItem` the shared
    :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier` reduces -- the
    four fields the claimability predicate needs (id, iter, status, deps). The
    full wave set is projected (not just PENDING ones) because the compute
    needs the CLOSED deps + the sibling rows to decide readiness. Returns an
    empty tuple -- the honest-empty path -- when *state* is unbound or carries
    no wave, so the pane renders honest-empty rather than crashing on a scope
    with no roadmap.

    Args:
        state: The bound read-only state, or ``None`` (fresh / user scope).

    Returns:
        The wave-graph view rows, one per wave in *state*; empty when no wave
        exists.
    """
    if state is None or not state.waves:
        return ()
    items = tuple(
        WaveFrontierItem(
            wave_id=wave.id,
            iter_id=wave.iter_id,
            status=wave.status,
            deps=tuple(wave.deps),
        )
        for wave in state.waves.values()
    )
    logger.debug(f"build_frontier_items waves={len(items)}")
    return items


def ready_rows(frontier: DrainableFrontier, state: State | None) -> tuple[ReadyWaveRow, ...]:
    """Enrich the computed frontier's ready rows with each wave's title.

    Walks :attr:`DrainableFrontier.ready` (already in claim order) and pairs
    each ready :class:`~eawf.kernel.spec.auq_bridge.WaveFrontierItem` with its
    wave title from *state* so the list row is scannable. A wave that is on the
    frontier but missing from *state* (it cannot be, since the frontier was
    computed from *state*) defaults to an empty title rather than raising.

    Args:
        frontier: The computed ready frontier from
            :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier`.
        state: The bound read-only state the titles are read from.

    Returns:
        The ready-wave display rows in claim order; empty when the frontier
        carries no ready wave.
    """
    waves = state.waves if state is not None else {}
    rows = tuple(
        ReadyWaveRow(
            wave_id=item.wave_id,
            iter_id=item.iter_id,
            title=_wave_title(waves.get(item.wave_id)),
        )
        for item in frontier.ready
    )
    return rows


def _wave_title(wave: Wave | None) -> str:
    """Return the wave's title, or the empty string when the wave is absent."""
    return wave.title if wave is not None else ""


def render_frontier_header(rows: tuple[ReadyWaveRow, ...]) -> str:
    """Render the frontier header line above the ready-wave list.

    When the frontier has ready waves the header reports the ready count so
    the operator reads the frontier size at a glance; when it is empty it leads
    with the honest-empty banner rather than implying a primed dispatch queue.

    Args:
        rows: The ready-wave display rows (empty when nothing is ready).

    Returns:
        A content-markup header string.
    """
    if not rows:
        return f"[$warn]{EMPTY_NOTICE}[/]\n[$muted]no claim-ready wave on the frontier[/]"
    return (
        f"[$accent]ready frontier[/] [$ok]{len(rows)}[/] "
        f"[$muted]wave{'' if len(rows) == 1 else 's'} claimable[/]"
    )


def render_ready_row(row: ReadyWaveRow) -> str:
    """Render one ready-wave list row (the wave id + iter + title).

    Args:
        row: The ready-wave display row.

    Returns:
        A content-markup row string naming the wave id, its iter, and title.
    """
    title_suffix = f" [$muted]{escape_markup(row.title)}[/]" if row.title else ""
    return (
        f"[$accent]{escape_markup(row.wave_id)}[/] "
        f"[$muted]{escape_markup(row.iter_id)}[/]{title_suffix}"
    )


class AutopilotModeScreen(ScopeScreen):
    """Autopilot pane: the ready-wave frontier + dispatch controls.

    Projects the bound read-only state's wave graph into the shared
    :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier` view and lists
    the ready (claim-ready) waves in claim order. Arrows move the selection
    through the list; the ``d`` key asks the daemon to live-spawn the selected
    wave via the ``agent.dispatch`` RPC (``spawn=True``) through the daemon-
    client seam and surfaces the typed result honestly. When no wave is
    claim-ready the pane renders the honest-empty :data:`EMPTY_NOTICE` banner.

    The screen self-binds to the host
    :class:`~eawf.surfaces.tui.app.EaApp` reactive ``state``: it seeds from
    ``app.state`` on mount and rebuilds when a daemon-pushed revision lands, so
    a wave closed (unblocking its dependents) after launch surfaces on the
    frontier without a relaunch.
    """

    DEFAULT_CSS: ClassVar[str] = """
    AutopilotModeScreen #autopilot-body {
        height: 1fr;
        padding: 1 2;
    }
    AutopilotModeScreen #autopilot-header {
        height: auto;
        margin-bottom: 1;
    }
    AutopilotModeScreen #autopilot-list {
        height: 1fr;
        border: solid $accent;
    }
    AutopilotModeScreen .autopilot-row {
        height: auto;
        padding: 0 1;
    }
    AutopilotModeScreen .autopilot-row.-selected {
        background: $accent 20%;
    }
    AutopilotModeScreen #autopilot-result {
        height: auto;
        margin-top: 1;
        color: $muted;
    }
    """

    #: ``up`` / ``down`` move the selection through the ready frontier; ``d``
    #: issues the dispatch. The chrome bindings (palette / help / quit / scope
    #: / mode digits) come from the shared chassis + app-wide bindings. ``d``
    #: is the dispatch verb here -- arrows stay primary for selection, so the
    #: pane offers no j/k vim aliases and ``d`` is free to mean "dispatch".
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "select_prev", "up", show=False),
        Binding("down", "select_next", "down", show=False),
        Binding("d", "dispatch_selected", "dispatch", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _AUTOPILOT_HINTS

    #: Bound state, watched so a fresh revision recomputes the frontier (a
    #: wave closed after launch unblocks its dependents onto the frontier).
    state: reactive[State | None] = reactive(None)

    #: Index of the selected ready-wave row (the dispatch target); clamped to
    #: the ready list, ``0`` when the list is non-empty, ``-1`` when empty.
    selected: reactive[int] = reactive(0, init=False)

    def __init__(self) -> None:
        """Initialise the pane with an empty ready list until first compute."""
        super().__init__()
        self._rows: tuple[ReadyWaveRow, ...] = ()

    def compose_body(self) -> ComposeResult:
        """Yield the frontier header, the (empty) ready-wave list, and the result.

        The header reports the ready count (or the honest-empty banner); the
        list container starts empty and :meth:`on_mount` populates it (with the
        ready rows, or the honest-empty notice) so the row mount path is
        single-sourced through :meth:`_render_rows`; the result line carries the
        dispatch surface (idle until ``d`` is pressed).
        """
        self._rows = self._current_rows()
        with Vertical(id="autopilot-body"):
            yield Static(render_frontier_header(self._rows), id=FRONTIER_HEADER_ID)
            yield VerticalScroll(id=FRONTIER_LIST_ID)
            yield Static(DISPATCH_IDLE, id=DISPATCH_RESULT_ID)

    def on_mount(self) -> None:
        """Seed from app state, arm the rebuild seam, and render the frontier."""
        super().on_mount()
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        self._rebuild()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this screen's reactive."""
        self.state = new_state

    def watch_state(self) -> None:
        """Recompute the frontier when the bound state changes."""
        if self.is_mounted:
            self._rebuild()

    def watch_selected(self) -> None:
        """Repaint the selection highlight when the selected index changes."""
        if self.is_mounted:
            self._repaint_selection()

    def action_select_prev(self) -> None:
        """Move the selection to the previous ready wave (clamped at the top)."""
        if self._rows:
            self.selected = max(0, self.selected - 1)

    def action_select_next(self) -> None:
        """Move the selection to the next ready wave (clamped at the bottom)."""
        if self._rows:
            self.selected = min(len(self._rows) - 1, self.selected + 1)

    def action_dispatch_selected(self) -> None:
        """Ask the daemon to live-spawn the selected ready wave.

        Issues the ``agent.dispatch`` request (``spawn=True``) for the selected
        ready wave through the daemon-client seam and updates the result line
        with the typed outcome. With no ready wave there is nothing to
        dispatch; when the daemon is unreachable the result says so rather than
        implying a spawn happened.
        """
        target = self._selected_row()
        if target is None:
            self._set_result(f"[$warn]{DISPATCH_NO_TARGET}[/]")
            return
        result_line = self._issue_dispatch(target)
        self._set_result(result_line)
        logger.info(f"action_dispatch_selected wave={target.wave_id} result={result_line!r}")

    def _issue_dispatch(self, target: ReadyWaveRow) -> str:
        """Issue the ``agent.dispatch`` RPC for *target* and return a result line.

        Calls the daemon ``agent.dispatch`` method with ``spawn=True`` through
        the same :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam
        the rest of the TUI mutates through, when a daemon socket is available.
        The returned line reports the daemon's captured pid + serving runtime;
        a daemon that is unreachable, rejecting, or timing out yields the
        honest "daemon unavailable" / "rejected" line rather than a faked
        dispatch.

        Args:
            target: The selected ready wave to dispatch.

        Returns:
            A content-markup result line describing the dispatch outcome.
        """
        if not self._daemon_available():
            return f"[$warn]{DISPATCH_NO_DAEMON}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=30.0) as client:
                result = client.call(
                    "agent.dispatch",
                    {"wave_id": target.wave_id, "spawn": True},
                )
        except DaemonRpcError as exc:
            logger.debug(f"_issue_dispatch daemon_rejected message={exc.message!r}")
            return (
                "[$warn]dispatch: daemon rejected request[/] "
                f"[$muted]{escape_markup(exc.message)}[/]"
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_dispatch daemon_fallback cause={exc!r}")
            return f"[$warn]{DISPATCH_NO_DAEMON}[/]"
        pid = result.get("pid")
        runtime = str(result.get("runtime", ""))
        return (
            f"[$ok]dispatch: spawned[/] [$muted]{escape_markup(target.wave_id)} "
            f"runtime={escape_markup(runtime)} pid={pid}[/]"
        )

    def _rebuild(self) -> None:
        """Recompute the ready frontier from state and repaint the list.

        Recomputes the rows via :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier`
        over the projected view, clamps the selection into the new list, and
        remounts the rows under the list container. A frontier that became
        empty (every ready wave dispatched / closed) repaints the honest-empty
        notice.
        """
        self._rows = self._current_rows()
        self._clamp_selection()
        self._render_rows()
        header = self.query(f"#{FRONTIER_HEADER_ID}")
        if header:
            header.first(Static).update(render_frontier_header(self._rows))
        logger.info(f"autopilot_rebuild ready={len(self._rows)} selected={self.selected}")

    def _render_rows(self) -> None:
        """Mount one Static per ready wave (or the honest-empty notice).

        Clears the list container, then mounts either the honest-empty notice
        (no ready wave) or one row per ready wave in claim order. Shared by the
        on-mount seed and every rebuild so the render path is single-sourced.
        """
        listing = self.query(f"#{FRONTIER_LIST_ID}")
        if not listing:
            return
        container = listing.first(VerticalScroll)
        container.remove_children()
        if not self._rows:
            container.mount(Static(EMPTY_NOTICE, id=FRONTIER_EMPTY_ID, classes="autopilot-empty"))
            return
        for index, row in enumerate(self._rows):
            classes = FRONTIER_ROW_CLASS
            if index == self.selected:
                classes = f"{FRONTIER_ROW_CLASS} {SELECTED_ROW_CLASS}"
            container.mount(Static(render_ready_row(row), classes=classes))

    def _repaint_selection(self) -> None:
        """Toggle the ``-selected`` class onto the selected row only."""
        rows = self.query(f".{FRONTIER_ROW_CLASS}")
        for index, widget in enumerate(rows):
            widget.set_class(index == self.selected, SELECTED_ROW_CLASS)

    def _clamp_selection(self) -> None:
        """Clamp the selection into the current ready list.

        A non-empty list clamps the index into ``0..len-1``; an empty list
        parks the selection at ``-1`` so :meth:`_selected_row` returns ``None``.
        """
        if not self._rows:
            self.set_reactive(type(self).selected, -1)
            return
        self.set_reactive(type(self).selected, min(max(0, self.selected), len(self._rows) - 1))

    def _selected_row(self) -> ReadyWaveRow | None:
        """Return the selected ready-wave row, or ``None`` when none is ready."""
        if not self._rows or not 0 <= self.selected < len(self._rows):
            return None
        return self._rows[self.selected]

    def _set_result(self, line: str) -> None:
        """Update the dispatch-result line, if mounted."""
        result = self.query(f"#{DISPATCH_RESULT_ID}")
        if result:
            result.first(Static).update(line)

    def _current_rows(self) -> tuple[ReadyWaveRow, ...]:
        """Compute the ready-wave display rows for the active scope.

        Projects the bound state into the frontier view, computes the ready
        frontier, and enriches each ready row with its wave title. When the
        view has a duplicate wave id (it cannot, since state keys waves by id)
        the compute would raise; that is left to fail loudly rather than masked.

        Returns:
            The ready-wave display rows in claim order; empty when no wave is
            claim-ready.
        """
        state = self._current_state()
        frontier = compute_ready_frontier(build_frontier_items(state))
        return ready_rows(frontier, state)

    def _current_state(self) -> State | None:
        """Return the bound read-only state, if loaded."""
        from eawf.kernel.state.models import State

        state = self.state
        if state is not None:
            return state
        app_state = getattr(self.app, "state", None)
        return app_state if isinstance(app_state, State) else None

    def _daemon_available(self) -> bool:
        """Return whether the App reports a reachable daemon socket.

        Delegates to the App's own daemon-socket probe so the dispatch path
        uses the same reachability verdict the rest of the TUI mutates through;
        a bare harness without the probe degrades to "unavailable" so the
        dispatch action never raises.
        """
        probe = getattr(self.app, "_daemon_socket_available", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except OSError as exc:
            logger.debug(f"_daemon_available probe_failed cause={exc!r}")
            return False


__all__ = [
    "DISPATCH_IDLE",
    "DISPATCH_NO_DAEMON",
    "DISPATCH_NO_TARGET",
    "DISPATCH_RESULT_ID",
    "EMPTY_NOTICE",
    "FRONTIER_EMPTY_ID",
    "FRONTIER_HEADER_ID",
    "FRONTIER_LIST_ID",
    "FRONTIER_ROW_CLASS",
    "SELECTED_ROW_CLASS",
    "AutopilotModeScreen",
    "ReadyWaveRow",
    "build_frontier_items",
    "ready_rows",
    "render_frontier_header",
    "render_ready_row",
]
