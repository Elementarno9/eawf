"""``AgentWatchModeScreen`` -- the live agent-watch zoom pane (mode digit 8).

The Watch mode zooms in on a single dispatched session: it STREAMS that
session's live events (``dispatch_cost`` / ``agent_end`` / ``runtime_switched``
/ ``state_mutated`` for the wave) as they arrive, and offers a **cancel**
control that asks the daemon to stop the spawned child.

Reusing the live-event seam (not a second subscription)
------------------------------------------------------
The pane never opens its own daemon subscription. The App's read-only
:class:`~eawf.surfaces.tui.state_binding.StateBinding` already consumes the
``event.subscribe`` push stream on a worker thread and marshals each decoded
envelope back to the event loop via :meth:`EaApp._on_event`, which fans it
out to every registered :class:`~eawf.surfaces.tui.modes.feed.FeedListener`.
This pane registers as one such listener on mount (the same seam the full
Feed pane uses) and seeds from the App's live ring buffer; the difference is
that it FILTERS every envelope -- both the buffered seed and each live push
-- to the **one** watched session, keying on the wave id that the executor
:class:`~eawf.kernel.state.models.AgentSession` scopes to (its ``scope_id``)
and that the C09 dispatch events stamp as their own ``scope_id``. So the
zoom shows exactly that session's stream, where the full Feed shows all of
it.

Picking the watch target
-------------------------
The default watch target is the most-recent ACTIVE executor
:class:`~eawf.kernel.state.models.AgentSession` in the bound state (the live
spawn engine registers one EXECUTOR session per dispatch), falling back to
the most-recent executor session of any status when none is ACTIVE. When the
scope has no dispatched executor session at all the pane renders the
honest-empty :data:`EMPTY_NOTICE` banner rather than implying a stream is
flowing.

Cancel (the SP-6 path)
----------------------
The cancel action (the ``k`` key) asks the daemon to stop the watched
session's spawned child by calling the ``agent.kill`` JSON-RPC method
through the same daemon-client seam the rest of the TUI mutates through. The
daemon owns the SIGTERM-grace-SIGKILL process-group ladder
(:func:`eawf.runtime.runtimes.cancel.cancel_with_grace`); the TUI only issues
the request and surfaces the typed result honestly. ``agent.kill`` is still
a placeholder that returns ``killed=false`` until the daemon wires the live
ladder, so the action reports that placeholder result rather than faking a
successful kill; when the daemon socket is unavailable it reports that
honestly too.
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

from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus
from eawf.surfaces.tui.modes.feed import FEED_ROW_CLASS, format_event_row
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.markup import escape_markup

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.kernel.store.envelope import Envelope

logger = logging.getLogger(__name__)

#: Id of the watched-session header banner (target line above the stream).
WATCH_HEADER_ID: str = "watch-header"

#: Id of the scrollable stream list container.
WATCH_LIST_ID: str = "watch-list"

#: Id of the honest-empty notice shown when no session is being watched, and
#: of the live-waiting notice shown before the watched session's first event.
WATCH_EMPTY_ID: str = "watch-empty"

#: Id of the cancel-result line (below the stream); honest about whether the
#: kill request was issued, accepted, or could not reach the daemon.
WATCH_RESULT_ID: str = "watch-result"

#: CSS class on each rendered stream row (shared with the Feed pane's row
#: class so the two live-stream panes style their rows identically).
WATCH_ROW_CLASS: str = FEED_ROW_CLASS

#: Notice when no dispatched executor session exists to watch. Phrased so the
#: empty surface is unmistakable rather than reading as a quiet live stream.
EMPTY_NOTICE: str = "no active dispatched session"

#: Notice when the App is degraded (daemon unreachable) so the operator knows
#: the stream is not flowing rather than merely quiet.
WATCH_DEGRADED: str = "daemon unreachable -- session stream paused until it returns"

#: Result line before any cancel has been issued (the idle cancel surface).
CANCEL_IDLE: str = "press k to cancel the watched session"

#: Result line when the cancel request could not reach the daemon.
CANCEL_NO_DAEMON: str = "cancel: daemon unavailable -- request not issued"

#: Result line when there is no session to cancel.
CANCEL_NO_TARGET: str = "cancel: no session to cancel"


@dataclass(frozen=True)
class WatchTarget:
    """The single dispatched session the zoom streams + can cancel.

    Attributes:
        session_id: The watched :class:`~eawf.kernel.state.models.AgentSession`
            id (record key).
        wave_id: The wave the session scopes to (its ``scope_id``) -- the key
            the live event stream is filtered on, since the C09 dispatch
            events stamp the same wave id as their own ``scope_id``.
        runtime: The runtime adapter the session ran on (plugin spelling).
        status: The session lifecycle status at pick time.
        attempt: The wave attempt number to cancel -- the highest recorded
            attempt for the wave, defaulting to ``1`` when none is recorded.
    """

    session_id: str
    wave_id: str
    runtime: str
    status: AgentSessionStatus
    attempt: int

    @property
    def label(self) -> str:
        """Return a compact ``wave / runtime`` label for the header / notices."""
        return f"{self.wave_id} / {self.runtime}"


def pick_watch_target(state: State | None) -> WatchTarget | None:
    """Pick the default session to watch from *state*, or ``None``.

    Selects the most-recent ACTIVE executor
    :class:`~eawf.kernel.state.models.AgentSession` (the live spawn engine
    registers one EXECUTOR session per dispatch), preferring an ACTIVE
    session so the zoom defaults to a session that may still be streaming;
    when none is ACTIVE it falls back to the most-recent executor session of
    any status so a just-finished dispatch is still inspectable. "Most
    recent" is by :attr:`~eawf.kernel.state.models.AgentSession.started_at`.
    Returns ``None`` -- the honest-empty path -- when *state* is unbound or
    carries no executor session at all.

    Args:
        state: The bound read-only state, or ``None`` (fresh / user scope).

    Returns:
        The :class:`WatchTarget` for the picked session, or ``None`` when no
        executor session exists to watch.
    """
    if state is None or not state.agent_sessions:
        return None
    executors = [
        sess for sess in state.agent_sessions.values() if sess.role is AgentSessionRole.EXECUTOR
    ]
    if not executors:
        return None
    active = [sess for sess in executors if sess.status is AgentSessionStatus.ACTIVE]
    pool = active if active else executors
    picked = max(pool, key=lambda sess: sess.started_at)
    target = WatchTarget(
        session_id=picked.id,
        wave_id=picked.scope_id,
        runtime=picked.runtime,
        status=picked.status,
        attempt=_latest_attempt(state, wave_id=picked.scope_id),
    )
    logger.info(
        f"pick_watch_target session={target.session_id!r} wave={target.wave_id} "
        f"runtime={target.runtime!r} status={target.status.value} active={bool(active)}"
    )
    return target


def _latest_attempt(state: State, *, wave_id: str) -> int:
    """Return the highest recorded attempt for *wave_id*, defaulting to ``1``.

    The cancel path needs a wave attempt number for the ``agent.kill`` params;
    the live session table on the wave records one row per attempt, so the
    highest key is the most-recent attempt. A wave with no recorded session
    table (the live spawn may not have persisted an attempt row yet) defaults
    to attempt ``1`` so the request is still well-formed.

    Args:
        state: The bound read-only state.
        wave_id: The wave whose attempt count is resolved.

    Returns:
        The highest recorded attempt number, or ``1`` when none is recorded.
    """
    wave = state.waves.get(wave_id)
    if wave is None or not wave.sessions:
        return 1
    return max(wave.sessions)


def render_watch_header(target: WatchTarget | None) -> str:
    """Render the watched-session header line above the stream.

    When a session is being watched the header names the wave, the runtime,
    and the session status so the operator reads the target at a glance; when
    there is no target it leads with the honest-empty banner.

    Args:
        target: The watched session, or ``None`` when none is being watched.

    Returns:
        A content-markup header string.
    """
    if target is None:
        return f"[$warn]{EMPTY_NOTICE}[/]\n[$muted]no dispatched executor session to stream[/]"
    return (
        f"[$accent]watching[/] {escape_markup(target.wave_id)} "
        f"[$muted]{escape_markup(target.runtime)}[/] "
        f"[$accent]{escape_markup(target.status.value)}[/]"
    )


def is_watched_event(envelope: Envelope, target: WatchTarget | None) -> bool:
    """Return whether *envelope* belongs to the watched session's stream.

    The zoom filters the shared live stream to the one watched session by
    keying on the wave id: the executor session scopes to a wave
    (``scope_id``), and the C09 dispatch events (``dispatch_cost`` /
    ``agent_end`` / ``runtime_switched`` / ``state_mutated``) stamp the same
    wave id as their own ``scope_id``. So an envelope is part of the watched
    stream exactly when its ``scope_id`` matches the target's wave id. With no
    target (honest-empty) nothing is watched.

    Args:
        envelope: A live event envelope from the App fan-out.
        target: The watched session, or ``None``.

    Returns:
        ``True`` when the envelope belongs to the watched session's stream.
    """
    if target is None:
        return False
    return envelope.scope_id == target.wave_id


class AgentWatchModeScreen(ScopeScreen):
    """Live agent-watch zoom: stream one dispatched session, cancel it.

    Reuses the App's live-event seam (registering as a
    :class:`~eawf.surfaces.tui.modes.feed.FeedListener`) but filters the
    stream to the single watched session, keyed on the wave id the executor
    :class:`~eawf.kernel.state.models.AgentSession` scopes to. The default
    target is the most-recent ACTIVE executor session
    (:func:`pick_watch_target`); the cancel action asks the daemon to stop the
    spawned child via the ``agent.kill`` RPC and surfaces the typed result
    honestly.
    """

    DEFAULT_CSS: ClassVar[str] = """
    AgentWatchModeScreen #watch-body {
        height: 1fr;
        padding: 1 2;
    }
    AgentWatchModeScreen #watch-header {
        height: auto;
        margin-bottom: 1;
    }
    AgentWatchModeScreen #watch-list {
        height: 1fr;
        border: solid $accent;
    }
    AgentWatchModeScreen #watch-result {
        height: auto;
        margin-top: 1;
        color: $muted;
    }
    """

    #: ``up`` / ``down`` scroll the stream; ``k`` issues the cancel. The
    #: chrome bindings (palette / help / quit / scope / mode digits) come from
    #: the shared chassis + app-wide bindings. ``k`` is the cancel verb here
    #: (not a vim-up alias) -- this pane keeps arrows primary for scrolling and
    #: does not offer the j/k vim scroll aliases, so ``k`` is free to mean
    #: "kill the watched session".
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "scroll_up", "up", show=False),
        Binding("down", "scroll_down", "down", show=False),
        Binding("pageup", "page_up", "page up", show=False),
        Binding("pagedown", "page_down", "page down", show=False),
        Binding("home", "scroll_home", "home", show=False),
        Binding("end", "scroll_end", "end", show=False),
        Binding("k", "cancel_session", "cancel", show=False),
    ]

    #: Footer hints for the agent-watch zoom. The mode digits are surfaced by
    #: the always-visible mode row, not duplicated here. Every label is produced
    #: through :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the
    #: key tokens stay pinned to the canonical vocabulary.
    FOOTER_HINTS: ClassVar[tuple[str, ...]] = (
        render_hint_label("↑↓", "scroll"),
        render_hint_label("k", "cancel"),
        render_hint_label("w/r/u", "scope"),
        render_hint_label("/", "palette"),
        render_hint_label("?", "help"),
        render_hint_label("q", "quit"),
    )

    #: The session being watched, resolved on mount from the bound state.
    target: reactive[WatchTarget | None] = reactive(None, init=False)

    def compose_body(self) -> ComposeResult:
        """Yield the watched-session header, the stream column, and the result.

        The header names the watched target (or the honest-empty banner); the
        stream list starts with a single live-waiting / honest-empty notice
        that :meth:`on_mount` replaces with the seeded rows and
        :meth:`append_event` prepends live ones to; the result line carries
        the cancel surface (idle until ``k`` is pressed).
        """
        self.target = self._pick_target()
        with Vertical(id="watch-body"):
            yield Static(render_watch_header(self.target), id=WATCH_HEADER_ID)
            with VerticalScroll(id=WATCH_LIST_ID):
                yield Static(self._empty_notice(), id=WATCH_EMPTY_ID, classes="watch-empty")
            yield Static(CANCEL_IDLE, id=WATCH_RESULT_ID)

    def on_mount(self) -> None:
        """Register on the live-event seam and seed the watched session's stream.

        Calls the base chassis mount (footer hints) first, then registers with
        the App so subsequent pushes fan out to :meth:`append_event`, and seeds
        the scroll from the App's live buffer filtered to the watched session
        (oldest-first buffer -> each prepended, so the most recent buffered
        event ends on top). A bare harness without the App fan-out hooks
        degrades to an empty live pane.
        """
        super().on_mount()
        register = getattr(self.app, "register_feed_listener", None)
        if callable(register):
            register(self)
        buffer = getattr(self.app, "live_event_buffer", ())
        for envelope in buffer:
            if is_watched_event(envelope, self.target):
                self._render_event(envelope)

    def on_unmount(self) -> None:
        """Unregister from the App fan-out so a torn-down pane gets no pushes."""
        unregister = getattr(self.app, "unregister_feed_listener", None)
        if callable(unregister):
            unregister(self)

    def append_event(self, envelope: Envelope) -> None:
        """Prepend one live *envelope* to the top of the stream, if watched.

        The App-fan-out entry point: a no-op when the pane has been unmounted
        between the App scheduling the push and this running, or when the
        envelope does not belong to the watched session's stream (the zoom
        shows one session, not the whole feed).

        Args:
            envelope: The live event envelope from the App fan-out.
        """
        if not self.is_mounted:
            return
        if not is_watched_event(envelope, self.target):
            return
        self._render_event(envelope)

    def refresh_empty_notice(self) -> None:
        """Update the live-waiting notice text to track the degraded flag.

        Called by the App when it flips between live and degraded so a pane
        showing the waiting notice swaps between the live-waiting and
        daemon-unreachable wording. A no-op once events have arrived (the
        notice is gone) or before mount.
        """
        if not self.is_mounted:
            return
        notice = self.query(f"#{WATCH_EMPTY_ID}")
        if notice:
            notice.first(Static).update(self._empty_notice())

    def action_cancel_session(self) -> None:
        """Ask the daemon to cancel the watched session, surfacing the result.

        Issues the ``agent.kill`` request for the watched session's wave +
        attempt through the daemon-client seam and updates the result line with
        the typed outcome. With no target there is nothing to cancel; when the
        daemon is unreachable the result says so rather than implying a kill.
        ``agent.kill`` is still a daemon-side placeholder that returns
        ``killed=false``, so the surfaced result reports that honestly rather
        than faking a successful kill.
        """
        target = self.target
        if target is None:
            self._set_result(CANCEL_NO_TARGET)
            return
        result_line = self._issue_kill(target)
        self._set_result(result_line)
        logger.info(
            f"action_cancel_session wave={target.wave_id} attempt={target.attempt} "
            f"result={result_line!r}"
        )

    def _issue_kill(self, target: WatchTarget) -> str:
        """Issue the ``agent.kill`` RPC for *target* and return a result line.

        Calls the daemon ``agent.kill`` method through the same
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the rest
        of the TUI mutates through, when a daemon socket is available. The
        returned line reports the daemon's typed ``killed`` verdict +
        delivered signal; a daemon that is unreachable, rejecting, or timing
        out yields the honest "daemon unavailable" line rather than a faked
        kill.

        Args:
            target: The watched session to cancel.

        Returns:
            A content-markup result line describing the cancel outcome.
        """
        if not self._daemon_available():
            return f"[$warn]{CANCEL_NO_DAEMON}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                result = client.call(
                    "agent.kill",
                    {"wave_id": target.wave_id, "attempt": target.attempt, "signal": "term"},
                )
        except DaemonRpcError as exc:
            logger.debug(f"_issue_kill daemon_rejected message={exc.message!r}")
            return "[$warn]cancel: daemon rejected request[/]"
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_kill daemon_fallback cause={exc!r}")
            return f"[$warn]{CANCEL_NO_DAEMON}[/]"
        killed = bool(result.get("killed"))
        signal = str(result.get("signal", "term"))
        if killed:
            return f"[$ok]cancel: killed[/] [$muted]signal={escape_markup(signal)}[/]"
        # ``agent.kill`` is still a placeholder returning killed=false; report
        # the request was issued + the daemon's verdict honestly.
        return (
            f"[$warn]cancel: not killed[/] [$muted]signal={escape_markup(signal)} "
            "(daemon kill not yet live)[/]"
        )

    def _render_event(self, envelope: Envelope) -> None:
        """Mount one event row at the top of the stream (newest-first).

        Removes the live-waiting notice on the first row, then mounts the new
        row before the existing ones so the newest event stays on top. Shared
        by the on-mount seed loop and the live :meth:`append_event` fan-out so
        the render path is single-sourced.

        Args:
            envelope: The event envelope to render at the top.
        """
        listing = self.query_one(f"#{WATCH_LIST_ID}", VerticalScroll)
        empty = listing.query(f"#{WATCH_EMPTY_ID}")
        if empty:
            empty.first().remove()
        row = Static(format_event_row(envelope), classes=WATCH_ROW_CLASS)
        listing.mount(row, before=0)
        logger.debug(f"_render_event id={envelope.id!r} kind={envelope.kind.value!r}")

    def _set_result(self, line: str) -> None:
        """Update the cancel-result line, if mounted."""
        result = self.query(f"#{WATCH_RESULT_ID}")
        if result:
            result.first(Static).update(line)

    def _empty_notice(self) -> str:
        """Return the stream's empty-notice text for the current target + state.

        Returns:
            :data:`EMPTY_NOTICE` when nothing is watched; the degraded wording
            when the App reports degraded (daemon unreachable); else the
            live-waiting wording naming the watched target.
        """
        if self.target is None:
            return EMPTY_NOTICE
        if getattr(self.app, "degraded", False):
            return WATCH_DEGRADED
        return f"watching {self.target.label} -- waiting for session events..."

    def _pick_target(self) -> WatchTarget | None:
        """Resolve the default watch target from the bound read-only state."""
        return pick_watch_target(self._current_state())

    def _current_state(self) -> State | None:
        """Return the bound read-only state, if loaded."""
        from eawf.kernel.state.models import State

        app_state = getattr(self.app, "state", None)
        return app_state if isinstance(app_state, State) else None

    def _daemon_available(self) -> bool:
        """Return whether the App reports a reachable daemon socket.

        Delegates to the App's own daemon-socket probe so the cancel path uses
        the same reachability verdict the rest of the TUI mutates through; a
        bare harness without the probe degrades to "unavailable" so the cancel
        action never raises.
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
    "CANCEL_IDLE",
    "CANCEL_NO_DAEMON",
    "CANCEL_NO_TARGET",
    "EMPTY_NOTICE",
    "WATCH_DEGRADED",
    "WATCH_EMPTY_ID",
    "WATCH_HEADER_ID",
    "WATCH_LIST_ID",
    "WATCH_RESULT_ID",
    "WATCH_ROW_CLASS",
    "AgentWatchModeScreen",
    "WatchTarget",
    "is_watched_event",
    "pick_watch_target",
    "render_watch_header",
]
