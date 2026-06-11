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

The raw-output tail (FA4 zoom)
------------------------------
The zoom adds a raw-output tail (:class:`~eawf.surfaces.tui.widgets.output_tail.OutputTail`)
beneath the typed lifecycle stream. The lifecycle stream shows WHAT happened
to the session (``dispatch_cost`` / ``agent_end`` / ...); the tail shows what
the agent SAYS -- its raw stdout lines as they arrive, auto-scrolled so the
newest line stays in view. It is fed by the App's optional raw-output seam
(``live_output_buffer`` for the on-mount seed + the ``append_output`` fan-out
for live lines), keyed -- like the typed stream -- on the watched session's
wave id; a bare harness without that seam degrades to the pinned
``waiting for output...`` notice rather than a frozen blank pane.

Per-session keys (the FA4 zoom controls)
----------------------------------------
The zoom advertises four session keys: ``k`` confirm-gated kills this lane
(the ``agent.kill`` RPC, gated behind a
:class:`~eawf.surfaces.tui.screens.overlays.confirm.ConfirmModal` so a
destructive stop is never one keystroke), ``space`` pauses / resumes this
lane (the ``agent.pause`` / ``agent.resume`` RPC), ``l`` views the watched
session's log, and ``Esc`` leaves the zoom. Every advertised key resolves to
a live :class:`~textual.binding.Binding` (affordance parity).

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Grid, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Static

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    AgentSessionStatus,
    WaveStatus,
)
from eawf.kernel.state.ids import natural_key
from eawf.observability.eval.reputation import FleetVerdictRow, fleet_verdict_rollup
from eawf.surfaces.tui.modes.feed import FEED_ROW_CLASS, format_event_row
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.output_tail import OutputTail
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph, status_sigil, tint

if TYPE_CHECKING:
    from pathlib import Path

    from eawf.kernel.state.models import (
        AgentSession,
        FleetFork,
        FleetLane,
        State,
        Wave,
    )
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
#: the stream is not flowing rather than merely quiet. Voiced to match the
#: global degraded banner's calm "daemon unreachable, reconnecting" lead, then
#: states the pane-specific consequence.
WATCH_DEGRADED: str = "daemon unreachable, reconnecting -- session stream paused until it returns"

#: Result line before any cancel has been issued (the idle cancel surface).
CANCEL_IDLE: str = "press k to cancel the watched session"

#: Result line when the cancel request could not reach the daemon.
CANCEL_NO_DAEMON: str = "cancel: daemon unavailable -- request not issued"

#: Result line when there is no session to cancel.
CANCEL_NO_TARGET: str = "cancel: no session to cancel"

#: Id of the raw-output tail pane mounted beneath the typed lifecycle stream.
WATCH_OUTPUT_ID: str = "watch-output"

#: Daemon JSON-RPC method the ``k`` (kill-this-lane) key routes through; the
#: signal param selects the SIGTERM-grace-SIGKILL ladder entry point.
_KILL_METHOD: str = "agent.kill"

#: Graceful SIGTERM signal the ``k`` key sends -- the soft ladder entry.
_SIGNAL_TERM: str = "term"

#: Daemon JSON-RPC methods the ``space`` (pause / resume this lane) key routes
#: through. ``agent.pause`` persists ``dispatch_paused = True`` (a deliberate
#: stop that blocks the next claim); ``agent.resume`` clears it. The flag picks
#: which method fires.
_PAUSE_RPC: str = "agent.pause"
_RESUME_RPC: str = "agent.resume"

#: Result line when the pause / resume request could not reach the daemon.
PAUSE_NO_DAEMON: str = "pause: daemon unavailable -- request not issued"

#: Result line when there is no session to pause.
PAUSE_NO_TARGET: str = "pause: no session to pause"

#: Result line when the log-view key has no session to view a log for.
LOG_NO_TARGET: str = "log: no session to view a log for"

#: Result line when no session-log handle is recorded for the watched attempt.
LOG_NO_HANDLE: str = "log: no session log recorded yet"

#: Id of the multi-session watch-grid container -- the parallel tile pane the
#: grid surface mounts one tile per ACTIVE executor session into.
WATCH_GRID_ID: str = "watch-grid"

#: Id of the grid's honest-empty notice (zero dispatched sessions) and of its
#: honest-degraded notice (daemon unreachable). The grid shows exactly one of
#: these in place of any tiles when there is nothing to lay out.
WATCH_GRID_EMPTY_ID: str = "watch-grid-empty"

#: CSS class on each session tile in the grid.
WATCH_TILE_CLASS: str = "watch-tile"

#: CSS class on a tile's scrollable event-row column.
WATCH_TILE_LIST_CLASS: str = "watch-tile-list"

#: CSS class on each rendered event row inside a tile (shared with the Feed
#: row class so every live-stream surface styles its rows identically).
WATCH_TILE_ROW_CLASS: str = FEED_ROW_CLASS

#: Id of the fleet verdict-rollup pane -- the per-wave auditor-verdict summary
#: the Watch mode mounts above the watched-session stream / grid.
WATCH_ROLLUP_ID: str = "watch-rollup"

#: Id of the rollup's honest-empty notice (zero verdict rows). The pane shows
#: this line in place of any verdict rows when the report store has no verdict
#: to roll up -- never a fabricated pass / rollup.
WATCH_ROLLUP_EMPTY_ID: str = "watch-rollup-empty"

#: CSS class on each rendered verdict row in the rollup pane.
WATCH_ROLLUP_ROW_CLASS: str = "watch-rollup-row"

#: Notice when the report store carries zero per-wave verdict rows. Phrased so
#: the empty rollup is unmistakable rather than reading as a quiet / fabricated
#: rollup -- the honest-empty surface the success criterion pins.
ROLLUP_EMPTY_NOTICE: str = "no verdicts recorded"

#: Id of the FA3 parallel-session lane-grid container -- the one-row-per-lane
#: surface the lane grid mounts its selectable lane rows into.
LANE_GRID_ID: str = "watch-lane-grid"

#: Id of the lane grid's honest-empty notice (zero in-flight lanes). The grid
#: shows this literal in place of any lane row when no lane is in flight --
#: never a fabricated row (C2).
LANE_GRID_EMPTY_ID: str = "watch-lane-grid-empty"

#: CSS class on each rendered lane row in the FA3 grid.
LANE_GRID_ROW_CLASS: str = "watch-lane-row"

#: CSS class flagging the selected lane row (the Enter-zoom target).
LANE_SELECTED_CLASS: str = "-selected"

#: The honest-empty literal the lane grid renders when no lane is in flight --
#: pinned in a golden (C2) so the empty surface reads as unmistakably empty,
#: never as a quiet / fabricated lane row.
LANE_GRID_EMPTY: str = "no sessions in flight"

#: Risk-tier band badge per tier value -- the short band label the lane row
#: trails so the operator reads which auto-close band the lane sits in. Keyed by
#: the :class:`~eawf.kernel.state.enums.RiskTier` string value so the lookup
#: stays decoupled from importing the enum; a tier past the map reads its raw
#: value uppercased (the resolver stays total).
_LANE_TIER_BADGE: dict[str, str] = {
    "mech": "MECH",
    "med": "MED",
    "high": "HIGH",
    "ui": "UI",
}

#: The placeholder vendor label a lane wears when no runtime is resolvable yet
#: (the lane's spawn registered no session row, so its runtime is unknown). The
#: row stays honest rather than fabricating a vendor name.
_LANE_VENDOR_UNKNOWN: str = "?"

#: The placeholder tok/$ figure a lane wears when no runtime counters are
#: recorded yet (a freshly-dispatched lane). Reads as a dash rather than a
#: fabricated zero spend.
_LANE_SPEND_UNKNOWN: str = "--"

#: The watched session's lifecycle status -> the lifecycle :class:`Sigil` its
#: header mark draws from. An ACTIVE session wears the RUNNING diamond (the
#: stream may still be flowing); a CHECKPOINTED session the half-filled claimed
#: mark (paused mid-flight, resumable); a CLOSED session the closed dot; a
#: FAILED one the failed cross; a STALE session the inert pending dot (the
#: spawn dropped off the live stream) so the header never implies a flowing
#: stream that has gone quiet.
_SESSION_SIGIL: dict[AgentSessionStatus, Sigil] = {
    AgentSessionStatus.ACTIVE: Sigil.RUNNING,
    AgentSessionStatus.CHECKPOINTED: Sigil.CLAIMED,
    AgentSessionStatus.CLOSED: Sigil.CLOSED,
    AgentSessionStatus.STALE: Sigil.PENDING,
    AgentSessionStatus.FAILED: Sigil.FAILED,
}


def _sigil_markup(sigil: Sigil, *, mode: RenderMode) -> str:
    """Return *sigil*'s shape tinted by its lifecycle status.

    Composes the SHAPE (:func:`~eawf.surfaces.tui.widgets.sigils.glyph`) and
    the COLOUR (:func:`~eawf.surfaces.tui.widgets.sigils.tint`) from the sigils
    helper so a status renders as a tinted lifecycle mark rather than a raw
    status word; a sigil whose mapped status has no tint falls back to the
    muted span so the mark still renders.

    Args:
        sigil: The lifecycle mark to render.
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the tinted (or muted) lifecycle glyph.
    """
    mark = escape_markup(glyph(sigil, mode=mode))
    hue = tint(sigil)
    if hue is None:
        return f"[$muted]{mark}[/]"
    return f"[{hue}]{mark}[/]"


def cancel_mark(*, mode: RenderMode) -> str:
    """Return the cancel affordance's failed-look mark for *mode*.

    The cancel control wears the FAILED sigil (the multiplication-x in unicode,
    ``x`` in ASCII) so the kill verb reads as the destructive / failed look
    rather than a neutral key letter. Tinted the failed hue via :func:`tint`.

    Args:
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the tinted failed-x cancel mark.
    """
    return _sigil_markup(Sigil.FAILED, mode=mode)


def session_sigil_markup(status: AgentSessionStatus, *, mode: RenderMode) -> str:
    """Return the watched session's lifecycle sigil markup for *status*.

    Maps the session status onto its lifecycle sigil (:data:`_SESSION_SIGIL`)
    and renders the tinted shape via :func:`_sigil_markup`, so the watch header
    leads with a sigil (the RUNNING diamond for an ACTIVE stream) rather than
    relying on the raw status word alone.

    Args:
        status: The watched session's lifecycle status.
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the session status's tinted lifecycle sigil.
    """
    return _sigil_markup(_SESSION_SIGIL[status], mode=mode)


def verdict_sigil_markup(verdict: AgentReportVerdict, *, mode: RenderMode) -> str:
    """Return *verdict*'s outcome-tinted sigil markup for the rollup row.

    Resolves the verdict to its ratified glyph + tint + optional badge via the
    shared :func:`~eawf.surfaces.tui.widgets.sigils.status_sigil` resolver (the
    one home for the verdict -> outcome shape / colour mapping), so a ``pass``
    wears the CLOSED green circle, a ``fail`` the FAILED red cross, a
    ``pass-with-followups`` the closed circle plus its follow-up badge, and a
    ``blocked`` the warn-tinted withheld slash -- the pane invents no colour or
    glyph of its own. A resolved sigil with no tint falls back to the muted
    span so the mark still renders.

    Args:
        verdict: The recorded auditor verdict to render as a tinted outcome
            mark.
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the verdict's tinted (or muted) outcome sigil.
    """
    resolved = status_sigil(verdict)
    mark = escape_markup(resolved.render(mode=mode))
    hue = resolved.tint_hex
    if hue is None:
        return f"[$muted]{mark}[/]"
    return f"[{hue}]{mark}[/]"


def render_verdict_rollup_row(row: FleetVerdictRow, *, mode: RenderMode) -> str:
    """Render one fleet verdict-rollup row: a wave + its outcome-tinted verdict.

    Leads with the wave id, then the verdict's outcome-tinted sigil
    (:func:`verdict_sigil_markup`) followed by the verdict word in the same
    tint, then the producing runtime in the muted span, so a row reads as
    ``<wave> <tinted-mark> <tinted-verdict> <runtime>`` at a glance. The tint
    comes from the shared resolver, so two rows with different outcomes
    (a ``pass`` and a ``fail``) carry visibly different hues.

    Args:
        row: One wave's latest verdict row from the fleet rollup.
        mode: The App's resolved render-mode label -- selects the sigil's
            ASCII / unicode column.

    Returns:
        A content-markup row string.
    """
    sigil = verdict_sigil_markup(row.verdict, mode=mode)
    hue = status_sigil(row.verdict).tint_hex
    word = escape_markup(row.verdict.value)
    tinted_word = f"[{hue}]{word}[/]" if hue is not None else f"[$muted]{word}[/]"
    return (
        f"[$accent]{escape_markup(row.wave_id)}[/] {sigil} {tinted_word} "
        f"[$muted]{escape_markup(row.runtime)}[/]"
    )


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
        log_handle: The opaque session-log handle for the watched attempt
            (the per-runtime URN the adapter mints), or ``None`` when the
            live spawn has not yet recorded a session-attempt row for the
            wave. The ``l`` view-log key resolves the log surface from it.
    """

    session_id: str
    wave_id: str
    runtime: str
    status: AgentSessionStatus
    attempt: int
    log_handle: str | None = None

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
    attempt = _latest_attempt(state, wave_id=picked.scope_id)
    target = WatchTarget(
        session_id=picked.id,
        wave_id=picked.scope_id,
        runtime=picked.runtime,
        status=picked.status,
        attempt=attempt,
        log_handle=_log_handle(state, wave_id=picked.scope_id, attempt=attempt),
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


def _log_handle(state: State, *, wave_id: str, attempt: int) -> str | None:
    """Return the session-log handle for *wave_id* attempt *attempt*, or ``None``.

    The ``l`` view-log key resolves a log surface from the watched attempt's
    recorded session-log handle (the per-runtime URN the adapter mints at
    spawn). A wave with no session table yet -- or one whose *attempt* row has
    not been persisted -- has no handle to view, so this returns ``None`` and
    the action surfaces the honest "no session log recorded yet" line rather
    than opening a missing log.

    Args:
        state: The bound read-only state.
        wave_id: The wave whose session-log handle is resolved.
        attempt: The attempt row to read the handle off.

    Returns:
        The recorded session-log handle, or ``None`` when none exists.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        return None
    row = wave.sessions.get(attempt)
    return row.session_log_handle if row is not None else None


def _wave_executor_session(state: State, wave_id: str) -> AgentSession | None:
    """Return the most-recent executor session scoped to *wave_id*, or ``None``.

    The FA3 -> FA4 zoom resolves a lane's wave to its streaming session by the
    same wave-id key the live stream filters on (the executor session's
    ``scope_id``). When two attempts registered two sessions the most-recent (by
    ``started_at``) is preferred so the zoom streams the live attempt; a wave
    with no executor session yet yields ``None``.

    Args:
        state: The bound read-only state.
        wave_id: The wave whose executor session is resolved.

    Returns:
        The most-recent executor session for the wave, or ``None``.
    """
    scoped = [
        sess
        for sess in state.agent_sessions.values()
        if sess.role is AgentSessionRole.EXECUTOR and sess.scope_id == wave_id
    ]
    if not scoped:
        return None
    return max(scoped, key=lambda sess: sess.started_at)


def _wave_runtime(state: State, wave_id: str) -> str | None:
    """Return *wave_id*'s runtime, preferring its session then its wave attempt.

    Resolves the runtime the FA4 zoom header names: the wave's executor session
    runtime when one is registered, else the wave's latest session-attempt
    runtime. A wave with neither -- unknown to state -- yields ``None`` so the
    zoom is refused rather than streaming an unknowable session.

    Args:
        state: The bound read-only state.
        wave_id: The wave whose runtime is resolved.

    Returns:
        The wave's runtime adapter spelling, or ``None`` when unknown.
    """
    session = _wave_executor_session(state, wave_id)
    if session is not None:
        return session.runtime
    wave = state.waves.get(wave_id)
    if wave is not None and wave.sessions:
        return wave.sessions[max(wave.sessions)].runtime
    return None


def _wave_session_id(state: State, wave_id: str) -> str | None:
    """Return the executor session id scoped to *wave_id*, or ``None``."""
    session = _wave_executor_session(state, wave_id)
    return session.id if session is not None else None


def _wave_session_status(state: State, wave_id: str) -> AgentSessionStatus:
    """Return *wave_id*'s executor session status, defaulting to ACTIVE.

    The FA4 zoom header leads with the session's lifecycle sigil; a wave with a
    registered executor session reads its status, while a wave known only by its
    session-attempt rows (no live session yet) defaults to ACTIVE so the zoom
    reads as a live stream rather than a stale one.

    Args:
        state: The bound read-only state.
        wave_id: The wave whose session status is resolved.

    Returns:
        The executor session's status, or ACTIVE when no session is registered.
    """
    session = _wave_executor_session(state, wave_id)
    return session.status if session is not None else AgentSessionStatus.ACTIVE


def render_watch_header(
    target: WatchTarget | None, *, mode: RenderMode = DEFAULT_RENDER_MODE
) -> str:
    """Render the watched-session header line above the stream.

    When a session is being watched the header LEADS with the session's
    lifecycle sigil (the RUNNING diamond for an ACTIVE stream) then names the
    wave, the runtime, and the session status so the operator reads the target
    at a glance; when there is no target it leads with the honest-empty banner.

    Args:
        target: The watched session, or ``None`` when none is being watched.
        mode: The App's resolved render-mode label -- selects the session
            sigil's ASCII / unicode column.

    Returns:
        A content-markup header string.
    """
    if target is None:
        return f"[$warn]{EMPTY_NOTICE}[/]\n[$muted]no dispatched executor session to stream[/]"
    sigil = session_sigil_markup(target.status, mode=mode)
    return (
        f"{sigil} [$accent]watching[/] {escape_markup(target.wave_id)} "
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


def active_executor_sessions(state: State | None) -> list[AgentSession]:
    """Return the ACTIVE executor sessions to lay out as grid tiles.

    The multi-session grid shows one tile per ACTIVE executor
    :class:`~eawf.kernel.state.models.AgentSession` -- the live spawn engine
    registers one EXECUTOR session per dispatch, and only the ACTIVE ones are
    still streaming, so a tile per ACTIVE executor is the parallel watch
    surface. Ordered by :attr:`~eawf.kernel.state.models.AgentSession.id` so
    the tile layout is stable across re-renders (the dict insertion order is
    not load-bearing). An unbound or session-free state yields an empty list --
    the honest-empty grid path.

    Args:
        state: The bound read-only state, or ``None`` (fresh / user scope).

    Returns:
        The ACTIVE executor sessions, id-sorted; empty when none exist.
    """
    if state is None or not state.agent_sessions:
        return []
    return sorted(
        (
            sess
            for sess in state.agent_sessions.values()
            if sess.role is AgentSessionRole.EXECUTOR and sess.status is AgentSessionStatus.ACTIVE
        ),
        key=lambda sess: sess.id,
    )


def parity_session_ids(state: State | None) -> tuple[str, ...]:
    """Return the id-sorted ACTIVE-executor session ids -- the parity-set key.

    The parity lens lays out one tile per ACTIVE executor session
    (:func:`active_executor_sessions`); this is the stable id key over that
    set, used by the poll backstop to decide whether the dispatched fleet
    changed since the last render. When a poll tick reveals the SAME set of
    ACTIVE executors the body is left untouched (so the live-pushed event rows
    are not clobbered); when it reveals an added / removed / re-keyed session
    the body recomposes so the parity grid re-derives its tiles -- the
    always-on poll backstop the project's TUI-staleness lesson pins beside the
    push. Returns an empty tuple for the honest-empty / unbound path.

    Args:
        state: The bound read-only state, or ``None`` (fresh / user scope).

    Returns:
        The ACTIVE executor session ids, id-sorted; empty when none exist.
    """
    return tuple(sess.id for sess in active_executor_sessions(state))


def tile_dom_id(session_id: str) -> str:
    """Return the DOM id for *session_id*'s grid tile.

    Namespaces the session id under a ``watch-tile--`` prefix so two tiles for
    two sessions never collide, and so a pushed event routes to exactly one
    tile by id. The session id is the record key (an :data:`IdStr`), so the
    composed id is a stable, unique DOM selector.

    Args:
        session_id: The watched session's record id.

    Returns:
        The tile's DOM id (e.g. ``watch-tile--S-1``).
    """
    return f"watch-tile--{session_id}"


def session_routes_event(session: AgentSession, envelope: Envelope) -> bool:
    """Return whether *envelope* routes to *session*'s tile.

    A grid tile streams exactly one session, keyed -- like the single-session
    zoom (:func:`is_watched_event`) -- on the wave id: the executor session
    scopes to a wave (``scope_id``) and the C09 dispatch events stamp the same
    wave id as their own ``scope_id``. So an envelope routes to a session's
    tile exactly when its ``scope_id`` matches the session's ``scope_id``. With
    two ACTIVE executors on two different waves a pushed event lands in the one
    tile whose wave it names and not the other's.

    Args:
        session: The session owning a tile.
        envelope: A live event envelope from the App fan-out.

    Returns:
        ``True`` when the envelope belongs to this session's tile stream.
    """
    return envelope.scope_id is not None and envelope.scope_id == session.scope_id


class WatchTile(Vertical):
    """One session's tile in the parallel watch grid.

    Leads with a header line naming the session (its lifecycle sigil + wave +
    runtime) and a scrollable, newest-first column of the session's streamed
    event rows. A pushed event routes here (:meth:`route_event`) only when its
    ``scope_id`` matches the tile's session wave, so a tile shows exactly its
    own session's stream and never a sibling tile's.
    """

    def __init__(self, session: AgentSession, *, mode: RenderMode) -> None:
        """Build a tile for *session* in render *mode*.

        Args:
            session: The ACTIVE executor session this tile streams.
            mode: The App's resolved render-mode label -- selects the header
                sigil's ASCII / unicode column.
        """
        super().__init__(id=tile_dom_id(session.id), classes=WATCH_TILE_CLASS)
        self._session = session
        self._mode = mode

    @property
    def session_id(self) -> str:
        """Return the record id of the session this tile streams."""
        return self._session.id

    def compose(self) -> ComposeResult:
        """Yield the tile header line and the scrollable event column."""
        yield Static(self._header_markup(), classes="watch-tile-header")
        yield VerticalScroll(classes=WATCH_TILE_LIST_CLASS)

    def _header_markup(self) -> str:
        """Return the tile header markup: session sigil + wave + runtime."""
        sigil = session_sigil_markup(self._session.status, mode=self._mode)
        return (
            f"{sigil} [$accent]{escape_markup(self._session.scope_id)}[/] "
            f"[$muted]{escape_markup(self._session.runtime)}[/]"
        )

    def route_event(self, envelope: Envelope) -> bool:
        """Prepend *envelope* to this tile's column when it routes here.

        The per-tile fan-out entry point: a no-op when the tile has been
        unmounted between the grid scheduling the push and this running, or
        when the envelope does not route to this session's wave (the routing
        predicate :func:`session_routes_event` keys on ``scope_id``). Returns
        whether the event landed so the grid can assert per-tile routing.

        Args:
            envelope: The live event envelope from the grid fan-out.

        Returns:
            ``True`` when the envelope was rendered into this tile.
        """
        if not self.is_mounted or not session_routes_event(self._session, envelope):
            return False
        listing = self.query_one(f".{WATCH_TILE_LIST_CLASS}", VerticalScroll)
        row = Static(format_event_row(envelope), classes=WATCH_TILE_ROW_CLASS)
        listing.mount(row, before=0)
        logger.debug(
            f"route_event session={self.session_id!r} id={envelope.id!r} "
            f"kind={envelope.kind.value!r}"
        )
        return True


class WatchGrid(Widget):
    """The fleet parity grid: the side-by-side watch lens over many sessions.

    The parity lens of the Watch mode: it lays out one :class:`WatchTile` per
    ACTIVE executor session side-by-side in a
    :class:`~textual.containers.Grid`, so two or more dispatched sessions read
    in parallel rather than one zoomed session at a time. Each tile is fed by
    the App's single ``event.subscribe`` fan-out -- a pushed event routes to
    the one tile whose session wave it names and not the others
    (:meth:`append_event`) -- never a second daemon subscription. With zero
    dispatched sessions it shows the honest-empty :data:`EMPTY_NOTICE` rather
    than a fabricated grid; when the host App reports a daemon-unreachable
    degraded state it shows :data:`WATCH_DEGRADED` so the operator knows the
    tiles are not streaming rather than merely quiet.
    """

    DEFAULT_CSS: ClassVar[str] = """
    WatchGrid {
        height: 1fr;
    }
    WatchGrid #watch-grid {
        height: 1fr;
        grid-size: 2;
        grid-gutter: 1;
    }
    WatchGrid .watch-tile {
        height: 1fr;
        border: round $accent;
        padding: 0 1;
    }
    WatchGrid .watch-tile-header {
        height: auto;
        margin-bottom: 1;
    }
    WatchGrid .watch-tile-list {
        height: 1fr;
    }
    WatchGrid #watch-grid-empty {
        height: 1fr;
        color: $muted;
        padding: 1 2;
    }
    """

    def __init__(self, sessions: list[AgentSession], *, degraded: bool, mode: RenderMode) -> None:
        """Build the grid over *sessions* in render *mode*.

        Args:
            sessions: The ACTIVE executor sessions to lay out as tiles
                (:func:`active_executor_sessions`); empty drives the
                honest-empty / honest-degraded notice.
            degraded: Whether the host App reports a daemon-unreachable state,
                so the empty notice reads as degraded rather than honest-empty.
            mode: The App's resolved render-mode label, threaded into each
                tile's header sigil.
        """
        super().__init__()
        self._sessions = sessions
        self._degraded = degraded
        self._mode = mode

    def compose(self) -> ComposeResult:
        """Yield either the tile grid or the honest-empty / degraded notice."""
        if not self._sessions:
            yield Static(self._empty_notice(), id=WATCH_GRID_EMPTY_ID)
            return
        with Grid(id=WATCH_GRID_ID):
            for session in self._sessions:
                yield WatchTile(session, mode=self._mode)

    def _empty_notice(self) -> str:
        """Return the grid's empty-notice text for the current daemon state.

        Returns:
            :data:`WATCH_DEGRADED` when the host App reports degraded (daemon
            unreachable), else the honest-empty :data:`EMPTY_NOTICE`.
        """
        return WATCH_DEGRADED if self._degraded else EMPTY_NOTICE

    def append_event(self, envelope: Envelope) -> str | None:
        """Route one live *envelope* to the matching tile, if any.

        The grid fan-out entry point: a no-op when the grid has been unmounted,
        or when no tile's session wave matches the envelope. Routes the
        envelope to the single matching tile (:meth:`WatchTile.route_event`)
        and returns that tile's session id so a caller can assert the event
        landed in its OWN tile and not a sibling's.

        Args:
            envelope: The live event envelope from the App fan-out.

        Returns:
            The session id of the tile the event landed in, or ``None`` when
            no tile matched (the grid is empty or the wave is off-grid).
        """
        if not self.is_mounted:
            return None
        for tile in self.query(f".{WATCH_TILE_CLASS}").results(WatchTile):
            if tile.route_event(envelope):
                return tile.session_id
        return None


class LaneState(Enum):
    """The four lifecycle states a fleet lane reads as in the FA3 grid.

    A lane is :attr:`RUNNING` while it holds an in-flight dispatch slot, reads
    :attr:`CLOSED` once its wave closed clean, :attr:`FAILED` when its wave
    ended FAILED / ABANDONED, and :attr:`FORK` when the loop paused it to a
    blocking fork (it left the slot for the operator-resolved fork queue). Each
    state draws a distinct lifecycle :class:`~eawf.surfaces.tui.widgets.sigils.Sigil`
    (the SHAPE) and a short detail word so the grid reads at a glance.
    """

    RUNNING = "running"
    CLOSED = "closed"
    FAILED = "failed"
    FORK = "fork"


#: A :class:`LaneState` -> the lifecycle :class:`~eawf.surfaces.tui.widgets.sigils.Sigil`
#: its row mark draws from. RUNNING wears the diamond (the lane is draining),
#: CLOSED the filled circle (clean terminal), FAILED the cross (a genuine
#: failure), FORK the withheld circled-slash (paused for operator resolution) so
#: a forked lane never reads as a clean close or a hard fail.
_LANE_STATE_SIGIL: dict[LaneState, Sigil] = {
    LaneState.RUNNING: Sigil.RUNNING,
    LaneState.CLOSED: Sigil.CLOSED,
    LaneState.FAILED: Sigil.FAILED,
    LaneState.FORK: Sigil.ABANDONED,
}

#: A :class:`LaneState` -> the short state-detail word the row trails so the
#: lane's state reads in words beside its sigil, never the bare enum value.
_LANE_STATE_DETAIL: dict[LaneState, str] = {
    LaneState.RUNNING: "draining",
    LaneState.CLOSED: "closed clean",
    LaneState.FAILED: "failed",
    LaneState.FORK: "forked -- awaiting you",
}


@dataclass(frozen=True)
class LaneGridRow:
    """One in-flight (or just-terminal) fleet lane projected for the FA3 grid.

    A display projection of a :class:`~eawf.kernel.state.models.FleetLane` (or a
    lane the loop paused to a blocking fork), enriched with the lane's resolved
    vendor, elapsed window, tok/$ spend, risk-tier band, and lifecycle state so a
    row reads ``<sigil> <wave> <vendor> <elapsed> <tok/$> <tier> <detail>`` at a
    glance. Produced only from the persisted fleet run + the lane's wave row, so
    every figure is read straight off state rather than recomputed in the UI.

    Attributes:
        wave_id: The lane's wave id (the FA4 zoom target + the row's lead label).
        vendor: The runtime adapter the lane ran on (resolved from the lane's
            session, falling back to the wave's latest session-attempt runtime),
            or :data:`_LANE_VENDOR_UNKNOWN` when none is recorded yet.
        elapsed_label: A compact ``Nh Nm`` / ``Nm`` / ``Ns`` elapsed window from
            the lane's dispatch to now (running) or to its wave close (terminal).
        spend_label: The lane's ``<tok> tok $<usd>`` spend read off the wave's
            latest runtime counters, or :data:`_LANE_SPEND_UNKNOWN` when none is
            recorded yet.
        tier_badge: The lane's :class:`~eawf.kernel.state.enums.RiskTier` short
            band label (e.g. ``MECH`` / ``HIGH``).
        state: The lane's lifecycle :class:`LaneState` (running / closed /
            failed / fork) driving its sigil + detail word.
    """

    wave_id: str
    vendor: str
    elapsed_label: str
    spend_label: str
    tier_badge: str
    state: LaneState


def _elapsed_label(start: datetime, end: datetime) -> str:
    """Return a compact ``Nh Nm`` / ``Nm`` / ``Ns`` window from *start* to *end*.

    Renders the lane's dispatch-to-now (or dispatch-to-close) window in the
    coarsest single-or-double unit so the grid column stays narrow: hours+minutes
    past an hour, minutes alone under an hour, seconds alone under a minute. A
    negative window (a clock skew) clamps to ``0s`` rather than rendering a
    nonsense negative elapsed.

    Args:
        start: When the lane's dispatch was issued.
        end: The window end (now for a running lane, the wave close for a
            terminal one).

    Returns:
        The compact elapsed-window label.
    """
    total = int((end - start).total_seconds())
    if total <= 0:
        return "0s"
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def _lane_vendor(lane: FleetLane, wave: Wave | None, sessions: dict[str, AgentSession]) -> str:
    """Resolve the runtime adapter the *lane* ran on, or the unknown placeholder.

    Prefers the runtime of the executor session the lane registered (keyed by
    the lane's ``session_id``), falling back to the wave's latest recorded
    session-attempt runtime, and finally to :data:`_LANE_VENDOR_UNKNOWN` when no
    runtime is recorded yet (a freshly-dispatched lane). The vendor is the agent
    CLI spelling (``claude`` / ``codex`` / ``opencode``).

    Args:
        lane: The fleet lane whose vendor is resolved.
        wave: The lane's wave row, or ``None`` when absent from state.
        sessions: The bound state's agent sessions, keyed by session id.

    Returns:
        The lane's runtime adapter spelling, or the unknown placeholder.
    """
    if lane.session_id is not None:
        session = sessions.get(lane.session_id)
        if session is not None:
            return session.runtime
    if wave is not None and wave.sessions:
        latest = wave.sessions[max(wave.sessions)]
        return latest.runtime
    return _LANE_VENDOR_UNKNOWN


def _lane_spend_label(wave: Wave | None) -> str:
    """Return the lane's ``<tok> tok $<usd>`` spend, or the unknown placeholder.

    Reads the wave's latest cumulative runtime counters
    (:attr:`~eawf.kernel.state.models.Wave.runtime_latest`) -- the input +
    output token total and the USD cost the runtime sidecar reported -- so the
    grid surfaces what state stored rather than recomputing a tally. A wave with
    no recorded counters yet reads :data:`_LANE_SPEND_UNKNOWN` rather than a
    fabricated zero spend.

    Args:
        wave: The lane's wave row, or ``None`` when absent from state.

    Returns:
        The lane's spend label, or the unknown placeholder.
    """
    if wave is None or wave.runtime_latest is None:
        return _LANE_SPEND_UNKNOWN
    latest = wave.runtime_latest
    tokens = (latest.input_tokens or 0) + (latest.output_tokens or 0)
    cost = latest.cost_usd
    if not tokens and cost is None:
        return _LANE_SPEND_UNKNOWN
    cost_part = f"${cost:.2f}" if cost is not None else _LANE_SPEND_UNKNOWN
    return f"{tokens} tok {cost_part}"


def _lane_tier_badge(wave: Wave | None, forked_tier: str | None) -> str:
    """Return the lane's risk-tier band badge (e.g. ``MECH`` / ``HIGH``).

    A forked lane carries its resolved
    :attr:`~eawf.kernel.state.models.FleetFork.risk_tier` directly (*forked_tier*),
    so the badge reads off the recorded fork; a running lane classifies its
    wave's :class:`~eawf.kernel.state.enums.RiskTier` from the wave's gate kinds
    via the pure :func:`~eawf.workflow.verify.oracle.classify_risk_tier`
    (lazy-imported so the TUI cold path never pulls the runtime stack at module
    load). A wave absent from state defaults to the deterministic ``MECH`` band.

    Args:
        wave: The lane's wave row, or ``None`` when absent from state.
        forked_tier: The recorded risk-tier value of a forked lane, or ``None``
            for a running / terminal lane that classifies from its gates.

    Returns:
        The short risk-tier band label.
    """
    if forked_tier is not None:
        return _LANE_TIER_BADGE.get(forked_tier, forked_tier.upper())
    if wave is None:
        return _LANE_TIER_BADGE["mech"]
    from eawf.workflow.verify.oracle import classify_risk_tier

    tier = classify_risk_tier(wave.gates).value
    return _LANE_TIER_BADGE.get(tier, tier.upper())


def _lane_state(wave: Wave | None, *, forked: bool) -> LaneState:
    """Classify a lane's :class:`LaneState` from its wave status + fork flag.

    A lane the loop paused to a blocking fork reads :attr:`LaneState.FORK`
    (it left the in-flight slot for the operator-resolved fork queue), and
    overrides the wave status -- a forked lane is neither a clean close nor a
    hard fail. Otherwise the lane's state follows its wave's terminal: a CLOSED
    wave reads :attr:`LaneState.CLOSED`, a FAILED / ABANDONED wave reads
    :attr:`LaneState.FAILED`, and any still-live wave (or one absent from state)
    reads :attr:`LaneState.RUNNING` -- the lane is still draining.

    Args:
        wave: The lane's wave row, or ``None`` when absent from state.
        forked: Whether the lane was paused to a blocking fork.

    Returns:
        The lane's lifecycle state.
    """
    if forked:
        return LaneState.FORK
    if wave is None:
        return LaneState.RUNNING
    if wave.status is WaveStatus.CLOSED:
        return LaneState.CLOSED
    if wave.status in (WaveStatus.FAILED, WaveStatus.ABANDONED):
        return LaneState.FAILED
    return LaneState.RUNNING


def lane_grid_rows(state: State | None, *, now: datetime | None = None) -> tuple[LaneGridRow, ...]:
    """Project the bound state's fleet lanes into FA3 lane-grid rows.

    Walks the persisted :attr:`~eawf.kernel.state.models.FleetRun.lanes` (the
    in-flight draining slots) and the
    :attr:`~eawf.kernel.state.models.FleetRun.forks` queue (lanes the loop
    paused to a blocking fork), so a forked lane stays visible -- it escalates to
    a fork-state row rather than disappearing the moment it leaves the slot. Each
    row is enriched from the lane's wave row + the bound sessions: the vendor, the
    elapsed window, the tok/$ spend, the risk-tier band, and the lifecycle state.
    A fork row supersedes an in-flight row for the same wave (a lane that just
    forked may briefly appear in both), and rows are returned in natural
    claim order so the grid reads top-to-bottom. An unbound / unarmed state
    yields an empty tuple -- the honest-empty grid path (C2).

    Args:
        state: The bound read-only state, or ``None`` (fresh / user scope).
        now: The reference time the running-lane elapsed window measures to;
            defaults to :func:`datetime.now` in UTC so callers (tests) can pin
            it for a deterministic golden.

    Returns:
        The lane-grid display rows in claim order; empty when no lane is in
        flight or forked.
    """
    run = state.fleet_run if state is not None else None
    if run is None:
        return ()
    reference = now if now is not None else datetime.now(UTC)
    waves = state.waves if state is not None else {}
    sessions = state.agent_sessions if state is not None else {}
    forked_tiers = {fork.wave_id: fork.risk_tier.value for fork in run.forks}
    rows: dict[str, LaneGridRow] = {}
    for lane in run.lanes.values():
        if lane.wave_id in forked_tiers:
            continue
        rows[lane.wave_id] = _build_lane_row(
            lane, waves.get(lane.wave_id), sessions, reference, forked_tier=None
        )
    for fork in run.forks:
        prior_lane = run.lanes.get(fork.wave_id)
        rows[fork.wave_id] = _build_fork_row(fork, prior_lane, waves.get(fork.wave_id), sessions)
    ordered = sorted(rows.values(), key=lambda row: natural_key(row.wave_id))
    logger.debug(
        f"lane_grid_rows lanes={len(run.lanes)} forks={len(run.forks)} rows={len(ordered)}"
    )
    return tuple(ordered)


def lane_parity_key(state: State | None) -> tuple[tuple[str, str], ...]:
    """Return the lane grid's ``(wave_id, state)`` parity key over *state*.

    The poll-backstop key the host screen compares across state revisions to
    decide whether the lane grid changed: a lane added / removed / transitioned
    (running -> closed / failed / fork) flips the key, so the body recomposes and
    the grid re-derives its rows; an unchanged fleet leaves the key equal and the
    poll tick is a no-op. Keyed off the same :func:`lane_grid_rows` projection so
    the parity set and the rendered set stay in lockstep. Returns an empty tuple
    for the honest-empty / unbound path.

    Args:
        state: The bound read-only state, or ``None`` (fresh / user scope).

    Returns:
        The ``(wave_id, lane-state)`` pairs in claim order; empty when no lane is
        in flight.
    """
    return tuple((row.wave_id, row.state.value) for row in lane_grid_rows(state))


def _build_lane_row(
    lane: FleetLane,
    wave: Wave | None,
    sessions: dict[str, AgentSession],
    now: datetime,
    *,
    forked_tier: str | None,
) -> LaneGridRow:
    """Build one running / terminal :class:`LaneGridRow` from an in-flight *lane*."""
    end = wave.closed_at if (wave is not None and wave.closed_at is not None) else now
    return LaneGridRow(
        wave_id=lane.wave_id,
        vendor=_lane_vendor(lane, wave, sessions),
        elapsed_label=_elapsed_label(lane.dispatched_at, end),
        spend_label=_lane_spend_label(wave),
        tier_badge=_lane_tier_badge(wave, forked_tier),
        state=_lane_state(wave, forked=False),
    )


def _build_fork_row(
    fork: FleetFork,
    lane: FleetLane | None,
    wave: Wave | None,
    sessions: dict[str, AgentSession],
) -> LaneGridRow:
    """Build one forked :class:`LaneGridRow` from a queued fork (+ its prior lane).

    A forked lane reads its risk-tier band off the recorded fork (the loop
    captured it at fork time) and its elapsed window off the prior in-flight
    lane's dispatch when one is still recorded, falling back to the fork's own
    ``forked_at`` so the row never lacks an elapsed figure.
    """
    dispatched = lane.dispatched_at if lane is not None else fork.forked_at
    return LaneGridRow(
        wave_id=fork.wave_id,
        vendor=(
            _lane_vendor(lane, wave, sessions) if lane is not None else _lane_vendor_from_wave(wave)
        ),
        elapsed_label=_elapsed_label(dispatched, fork.forked_at),
        spend_label=_lane_spend_label(wave),
        tier_badge=_lane_tier_badge(wave, fork.risk_tier.value),
        state=LaneState.FORK,
    )


def _lane_vendor_from_wave(wave: Wave | None) -> str:
    """Resolve a forked lane's vendor off its wave alone (no in-flight lane left)."""
    if wave is not None and wave.sessions:
        return wave.sessions[max(wave.sessions)].runtime
    return _LANE_VENDOR_UNKNOWN


def lane_state_sigil_markup(state: LaneState, *, mode: RenderMode) -> str:
    """Return *state*'s lifecycle-tinted sigil markup for the lane row.

    Maps the lane state onto its lifecycle sigil (:data:`_LANE_STATE_SIGIL`) and
    renders the tinted shape via :func:`_sigil_markup`, so a running lane wears
    the RUNNING diamond, a closed lane the CLOSED circle, a failed lane the
    FAILED cross, and a forked lane the withheld circled-slash -- each a distinct
    shape so the four states read apart at a glance.

    Args:
        state: The lane's lifecycle state.
        mode: The App's resolved render-mode label -- selects the glyph column.

    Returns:
        A content-markup span: the lane state's tinted lifecycle sigil.
    """
    return _sigil_markup(_LANE_STATE_SIGIL[state], mode=mode)


def render_lane_row(row: LaneGridRow, *, selected: bool = False, mode: RenderMode) -> str:
    """Render one FA3 lane-grid row: ``<sigil> <wave> <vendor> <elapsed> ...``.

    Lays the row out as the lane's lifecycle sigil
    (:func:`lane_state_sigil_markup`), then the wave id, the vendor, the elapsed
    window, the tok/$ spend, the risk-tier band badge, and the state-detail word
    (:data:`_LANE_STATE_DETAIL`) -- so a row reads
    ``<sigil> <wave> <vendor> <elapsed> <tok/$> <tier> <detail>`` at a glance.
    The selected row leads with the accent hue so the Enter-zoom target reads as
    highlighted.

    Args:
        row: The lane-grid display row.
        selected: Whether this row is the current Enter-zoom target (drives the
            row's accent / muted lead).
        mode: The App's resolved render-mode label -- selects the sigil's glyph
            column.

    Returns:
        A content-markup lane-row string.
    """
    sigil = lane_state_sigil_markup(row.state, mode=mode)
    wave_hue = "$accent" if selected else "$muted"
    detail = escape_markup(_LANE_STATE_DETAIL[row.state])
    return (
        f"{sigil} [{wave_hue}]{escape_markup(row.wave_id)}[/] "
        f"[$muted]{escape_markup(row.vendor)}[/] "
        f"[$muted]{escape_markup(row.elapsed_label)}[/] "
        f"[$muted]{escape_markup(row.spend_label)}[/] "
        f"[$accent]{escape_markup(row.tier_badge)}[/] "
        f"[$muted]{detail}[/]"
    )


class LaneGrid(Widget):
    """The FA3 parallel-session lane grid: one selectable row per in-flight lane.

    Generalizes the I07-W08 watch grid into the fleet lane lens: it lists one
    :class:`LaneGridRow` per in-flight (or just-forked) fleet lane, each row
    reading ``<sigil> <wave> <vendor> <elapsed> <tok/$> <tier> <detail>`` with a
    distinct lifecycle sigil per running / closed / failed / fork state. Arrows
    move the selection through the rows; pressing Enter posts a :class:`Zoom`
    message carrying the selected lane's wave id so the host screen can zoom that
    lane into the FA4 single-session view. With zero in-flight lanes it shows the
    honest-empty :data:`LANE_GRID_EMPTY` literal rather than a fabricated row
    (C2).
    """

    DEFAULT_CSS: ClassVar[str] = """
    LaneGrid {
        height: 1fr;
    }
    LaneGrid #watch-lane-grid {
        height: 1fr;
        border: round $accent;
    }
    LaneGrid .watch-lane-row {
        height: auto;
        padding: 0 1;
    }
    LaneGrid .watch-lane-row.-selected {
        background: $accent 20%;
    }
    LaneGrid #watch-lane-grid-empty {
        height: 1fr;
        color: $muted;
        padding: 1 2;
    }
    """

    #: ``up`` / ``down`` move the lane selection; ``enter`` zooms the selected
    #: lane to the FA4 single-session view. Arrows stay primary (no vim aliases).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "select_prev", "up", show=False),
        Binding("down", "select_next", "down", show=False),
        Binding("enter", "zoom_lane", "zoom", show=False),
    ]

    #: Index of the selected lane row (the Enter-zoom target); clamped to the
    #: row list, ``0`` when non-empty, ``-1`` when empty.
    selected: reactive[int] = reactive(0, init=False)

    class Zoom(Message):
        """Posted when Enter zooms the selected lane to the FA4 session view.

        Attributes:
            wave_id: The selected lane's wave id -- the FA4 zoom target the host
                screen pins as its watched session.
        """

        def __init__(self, wave_id: str) -> None:
            super().__init__()
            self.wave_id = wave_id

    def __init__(self, rows: tuple[LaneGridRow, ...], *, mode: RenderMode) -> None:
        """Build the lane grid over *rows* in render *mode*.

        Args:
            rows: The lane-grid rows to lay out (:func:`lane_grid_rows`); empty
                drives the honest-empty :data:`LANE_GRID_EMPTY` literal (C2).
            mode: The App's resolved render-mode label, threaded into each row's
                lifecycle sigil.
        """
        super().__init__()
        self._rows = rows
        self._mode = mode

    def compose(self) -> ComposeResult:
        """Yield the selectable lane rows, or the honest-empty literal (C2)."""
        if not self._rows:
            yield Static(LANE_GRID_EMPTY, id=LANE_GRID_EMPTY_ID)
            return
        with VerticalScroll(id=LANE_GRID_ID):
            for index, row in enumerate(self._rows):
                selected = index == self.selected
                classes = (
                    f"{LANE_GRID_ROW_CLASS} {LANE_SELECTED_CLASS}"
                    if selected
                    else LANE_GRID_ROW_CLASS
                )
                yield Static(
                    render_lane_row(row, selected=selected, mode=self._mode), classes=classes
                )

    def on_mount(self) -> None:
        """Seed the selection at the first row (or ``-1`` for the empty grid)."""
        self.set_reactive(type(self).selected, 0 if self._rows else -1)

    def action_select_prev(self) -> None:
        """Move the selection to the previous lane row (clamped at the top)."""
        if self._rows:
            self.selected = max(0, self.selected - 1)

    def action_select_next(self) -> None:
        """Move the selection to the next lane row (clamped at the bottom)."""
        if self._rows:
            self.selected = min(len(self._rows) - 1, self.selected + 1)

    def action_zoom_lane(self) -> None:
        """Zoom the selected lane to the FA4 session view (Enter).

        Posts a :class:`Zoom` message carrying the selected lane's wave id so the
        host screen pins it as the FA4 watched session. A no-op when no lane is
        selected (the honest-empty grid has nothing to zoom).
        """
        row = self._selected_row()
        if row is None:
            return
        self.post_message(self.Zoom(row.wave_id))
        logger.info(f"lane_grid_zoom wave={row.wave_id}")

    def watch_selected(self) -> None:
        """Repaint the lane rows so the selection accent + tint move."""
        if not self.is_mounted:
            return
        for index, widget in enumerate(self.query(f".{LANE_GRID_ROW_CLASS}").results(Static)):
            if index >= len(self._rows):
                continue
            selected = index == self.selected
            widget.update(render_lane_row(self._rows[index], selected=selected, mode=self._mode))
            widget.set_class(selected, LANE_SELECTED_CLASS)

    def _selected_row(self) -> LaneGridRow | None:
        """Return the selected lane row, or ``None`` when none is selected."""
        if not self._rows or not 0 <= self.selected < len(self._rows):
            return None
        return self._rows[self.selected]


class VerdictRollupPane(Widget):
    """The fleet verdict-rollup pane: each wave's latest verdict, outcome-tinted.

    Renders one row per wave that has an auditor verdict
    (:func:`~eawf.observability.eval.reputation.fleet_verdict_rollup`), each
    leading with the wave id then the verdict's outcome-tinted sigil + word
    (:func:`render_verdict_rollup_row`), so a fleet's pass / fail mix reads at a
    glance with two visibly different hues. With zero verdict rows it shows the
    honest-empty :data:`ROLLUP_EMPTY_NOTICE` line rather than a fabricated
    pass / rollup -- the honesty contract the success criterion pins.
    """

    DEFAULT_CSS: ClassVar[str] = """
    VerdictRollupPane {
        height: auto;
        max-height: 8;
        border: round $accent;
        margin-bottom: 1;
        padding: 0 1;
    }
    VerdictRollupPane .watch-rollup-row {
        height: auto;
    }
    VerdictRollupPane #watch-rollup-empty {
        height: auto;
        color: $muted;
    }
    """

    def __init__(self, rows: list[FleetVerdictRow], *, mode: RenderMode) -> None:
        """Build the rollup over *rows* in render *mode*.

        Args:
            rows: Each wave's latest verdict from the fleet rollup
                (:func:`~eawf.observability.eval.reputation.fleet_verdict_rollup`);
                empty drives the honest-empty notice.
            mode: The App's resolved render-mode label, threaded into each row's
                outcome sigil.
        """
        super().__init__(id=WATCH_ROLLUP_ID)
        self._rows = rows
        self._mode = mode

    def compose(self) -> ComposeResult:
        """Yield either the per-wave verdict rows or the honest-empty notice."""
        if not self._rows:
            yield Static(ROLLUP_EMPTY_NOTICE, id=WATCH_ROLLUP_EMPTY_ID)
            return
        for row in self._rows:
            yield Static(
                render_verdict_rollup_row(row, mode=self._mode),
                classes=WATCH_ROLLUP_ROW_CLASS,
            )


class AgentWatchModeScreen(ScopeScreen):
    """Live agent-watch: the fleet parity lens over the dispatched sessions.

    With two or more ACTIVE executor sessions the body is the fleet parity
    :class:`WatchGrid` -- every session side-by-side, each streaming its own
    events; with one (or zero) it is the single-session zoom (or the
    honest-empty banner). Both surfaces are fed by the SAME live seams, never a
    second daemon subscription:

    * the **push** -- the screen registers as a
      :class:`~eawf.surfaces.tui.modes.feed.FeedListener`, so the App's single
      ``event.subscribe`` fan-out routes each envelope to the matching tile
      (grid) or the watched stream (zoom); and
    * the **poll backstop** -- the screen watches the App's reactive ``state``
      (which the binder refreshes from BOTH daemon-push and the always-on
      mtime-poll), so a tick that changes the ACTIVE-executor fleet
      (:func:`parity_session_ids`) recomposes the body to re-derive the parity
      grid. A tick that leaves the fleet unchanged is a no-op, so the
      live-pushed event rows are not clobbered.

    The default zoom target is the most-recent ACTIVE executor session
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
        border: round $accent;
    }
    AgentWatchModeScreen #watch-output {
        height: 1fr;
        margin-top: 1;
    }
    AgentWatchModeScreen #watch-result {
        height: auto;
        margin-top: 1;
        color: $muted;
    }
    """

    #: ``up`` / ``down`` scroll the stream; the FA4 session keys ``k`` (kill
    #: this lane, confirm-gated, with ``x`` as a cancel-verb alias), ``space``
    #: (pause / resume this lane), ``l`` (view this session's log), and ``Esc``
    #: (leave the zoom) act on the watched session. The chrome bindings (palette
    #: / help / quit / scope / mode digits) come from the shared chassis +
    #: app-wide bindings. ``k`` is the kill verb here (not a vim-up alias) --
    #: this pane keeps arrows primary for scrolling and does not offer the j/k
    #: vim scroll aliases, so ``k`` is free to mean "kill the watched session";
    #: ``x`` aliases it to the SAME confirm-gated kill so the cancel-x verb (the
    #: failed-look mark the result line already wears) is reachable directly.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "scroll_up", "up", show=False),
        Binding("down", "scroll_down", "down", show=False),
        Binding("pageup", "page_up", "page up", show=False),
        Binding("pagedown", "page_down", "page down", show=False),
        Binding("home", "scroll_home", "home", show=False),
        Binding("end", "scroll_end", "end", show=False),
        Binding("k", "cancel_session", "kill", show=False),
        Binding("x", "cancel_session", "kill", show=False),
        Binding("space", "pause_session", "pause", show=False),
        Binding("l", "view_log", "view log", show=False),
        Binding("escape", "leave_zoom", "back", show=False),
    ]

    #: Footer hints for the agent-watch zoom. The mode digits are surfaced by
    #: the always-visible mode row, not duplicated here. Every label is produced
    #: through :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the
    #: key tokens stay pinned to the canonical vocabulary. ``l`` (view log) is
    #: bound (affordance parity) but kept off the strip: it is not a member of
    #: the footer's frozen token vocabulary, so advertising it there would fail
    #: the authoring-time guard -- the Binding itself is the affordance.
    FOOTER_HINTS: ClassVar[tuple[str, ...]] = (
        render_hint_label("↑↓", "select"),
        render_hint_label("k", "kill"),
        render_hint_label("space", "pause"),
        render_hint_label("Esc", "back"),
        render_hint_label("w/r/u", "scope"),
        render_hint_label("/", "palette"),
        render_hint_label("?", "help"),
        render_hint_label("q", "quit"),
    )

    #: The session being watched, resolved on mount from the bound state. In
    #: the single-session path this is the zoom target; in the multi-session
    #: grid path it stays ``None`` (the grid streams every ACTIVE executor).
    target: reactive[WatchTarget | None] = reactive(None, init=False)

    #: Whether a cancel has been issued, so a render-mode flip repaints the
    #: still-idle cancel line but never clobbers an issued cancel's result.
    _cancel_issued: bool = False

    #: The fleet parity grid, mounted in place of the single-session zoom when
    #: two or more ACTIVE executor sessions are dispatched; ``None`` in the
    #: single-session / honest-empty / lane-grid path.
    _grid: WatchGrid | None = None

    #: The FA3 lane grid, mounted as the parallel-session surface when the
    #: bound fleet run has in-flight (or just-forked) lanes; ``None`` in the
    #: session-grid / single-session / honest-empty path. When mounted it
    #: supersedes the session surfaces -- the fleet lanes are the live truth.
    _lane_grid: LaneGrid | None = None

    #: The ACTIVE-executor session-id set the body was last composed for, so
    #: the poll backstop recomposes only when the dispatched fleet actually
    #: changes (an added / removed / re-keyed session) and leaves the live-
    #: pushed event rows untouched on a no-op tick. Seeded on every compose.
    _parity_ids: tuple[str, ...] = ()

    #: The FA3 lane-grid ``(wave_id, state)`` parity key the body was last
    #: composed for, so the poll backstop recomposes when a lane transitions
    #: (running -> closed / failed / fork) or is added / removed -- the always-on
    #: backstop the project's TUI-staleness lesson pins. Seeded on every compose.
    _lane_parity: tuple[tuple[str, str], ...] = ()

    def compose_body(self) -> ComposeResult:
        """Yield the fleet verdict rollup then the FA3 lane grid, parity grid, OR zoom.

        The fleet :class:`VerdictRollupPane` leads the body (each wave's latest
        auditor verdict, outcome-tinted, or the honest-empty rollup line) above
        the live surface. The live surface is, in precedence order:

        * the FA3 :class:`LaneGrid` -- when the bound fleet run has in-flight (or
          just-forked) lanes, one selectable row per lane reads
          ``<sigil> <wave> <vendor> <elapsed> <tok/$> <tier> <detail>`` with a
          distinct lifecycle sigil per running / closed / failed / fork state;
          Enter zooms the selected lane to the FA4 single-session view (C1);
        * the fleet parity :class:`WatchGrid` -- when two or more ACTIVE executor
          sessions are dispatched but no fleet run lane is in flight, every
          session side-by-side streaming its own events; or
        * the single-session zoom (FA4) -- a header leading with the watched
          target's lifecycle sigil (or the honest-empty banner), the typed
          lifecycle stream, the raw-output tail, and the cancel result line.

        The ACTIVE-executor fleet this body is composed for is recorded so the
        poll backstop (:meth:`_on_app_state`) recomposes only when that fleet
        changes.
        """
        state = self._current_state()
        sessions = active_executor_sessions(state)
        mode = self._render_mode()
        self._parity_ids = tuple(sess.id for sess in sessions)
        self._lane_parity = lane_parity_key(state)
        self._grid = None
        self._lane_grid = None
        yield VerdictRollupPane(self._fleet_verdict_rollup(), mode=mode)
        # A pending FA3 -> FA4 zoom forces the single-session zoom for the pinned
        # target, even while the fleet still reports lanes / multiple sessions
        # (the operator chose to drill ONE lane out of the parallel surface).
        if self._zoom_pending:
            self.target = self.target if self.target is not None else self._pick_target()
            yield from self._compose_single_session(mode=mode)
            return
        lane_rows = lane_grid_rows(state)
        if lane_rows:
            self._lane_grid = LaneGrid(lane_rows, mode=mode)
            yield self._lane_grid
            return
        if len(sessions) >= 2:
            self._grid = WatchGrid(sessions, degraded=self._degraded(), mode=mode)
            yield self._grid
            return
        self.target = self._pick_target()
        yield from self._compose_single_session(mode=mode)

    def _compose_single_session(self, *, mode: RenderMode) -> ComposeResult:
        """Yield the FA4 single-session zoom body for the pinned :attr:`target`."""
        with Vertical(id="watch-body"):
            yield Static(render_watch_header(self.target, mode=mode), id=WATCH_HEADER_ID)
            with VerticalScroll(id=WATCH_LIST_ID):
                yield Static(self._empty_notice(), id=WATCH_EMPTY_ID, classes="watch-empty")
            yield OutputTail(id=WATCH_OUTPUT_ID)
            yield Static(self._cancel_idle_line(), id=WATCH_RESULT_ID)

    def on_lane_grid_zoom(self, message: LaneGrid.Zoom) -> None:
        """Zoom the selected lane to the FA4 single-session view (Enter).

        The FA3 -> FA4 drill: a :class:`LaneGrid.Zoom` message names the lane's
        wave id, which this pins as the watched target (resolving the runtime +
        attempt + log handle from the bound state) and recomposes the body into
        the single-session zoom for that lane. A wave with no resolvable target
        (absent from state) is a no-op so the grid stays put rather than zooming
        into an empty session.

        Args:
            message: The lane-grid zoom message carrying the selected wave id.
        """
        message.stop()
        target = self._target_for_wave(message.wave_id)
        if target is None:
            logger.debug(f"on_lane_grid_zoom no_target wave={message.wave_id}")
            return
        self.target = target
        self._lane_grid = None
        self._zoom_pending = True
        logger.info(f"on_lane_grid_zoom wave={message.wave_id}")
        self.call_after_refresh(self._zoom_to_target)

    async def _zoom_to_target(self) -> None:
        """Recompose into the FA4 single-session zoom for the pinned target.

        Run on the event loop via :meth:`call_after_refresh` so the async
        :meth:`recompose` is awaited rather than left dangling; the pinned
        :attr:`target` + the :attr:`_zoom_pending` flag carry through the
        recompose so the body lands on the single-session zoom for the zoomed
        lane (even while the fleet still reports lanes), then re-seeds its stream.
        """
        await self.recompose()
        self._seed_from_buffer()
        self._seed_output_from_buffer()

    #: Set once an FA3 -> FA4 zoom drills into a lane, so every later compose /
    #: recompose lands on the single-session zoom for the pinned target rather
    #: than the parallel lane grid (the operator chose to watch ONE lane). Left
    #: set for the screen's lifetime: the operator leaves the zoom via ``Esc``
    #: (back to the broad feed), not back to the grid.
    _zoom_pending: bool = False

    def on_mount(self) -> None:
        """Register on the live-event seam and seed the watched session's stream.

        Calls the base chassis mount (footer hints) first, then registers with
        the App so subsequent pushes fan out to :meth:`append_event`, and seeds
        both the typed lifecycle scroll AND the raw-output tail from the App's
        live buffers filtered to the watched session (oldest-first buffer ->
        each prepended for the lifecycle stream, appended for the tail). A bare
        harness without the App fan-out hooks degrades to an empty live pane and
        the pinned ``waiting for output...`` tail notice.
        """
        super().on_mount()
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        register = getattr(self.app, "register_feed_listener", None)
        if callable(register):
            register(self)
        self._seed_from_buffer()
        self._seed_output_from_buffer()

    def _seed_from_buffer(self) -> None:
        """Seed the freshly-composed surface from the App's live event buffer.

        Replays the App's oldest-first live buffer into the current surface so
        a mode switch (or a poll-backstop recompose) shows the events that
        arrived before this body existed: into every parity tile that names the
        envelope's wave when the grid is mounted, else into the single-session
        zoom filtered to the watched session. Each envelope is prepended, so the
        most recent buffered event ends on top. A bare harness without the App
        buffer degrades to an empty surface.
        """
        if self._lane_grid is not None:
            # The FA3 lane grid is a static lane projection, not an event
            # stream -- there is no per-row event column to seed.
            return
        buffer = getattr(self.app, "live_event_buffer", ())
        if self._grid is not None:
            for envelope in buffer:
                self._grid.append_event(envelope)
            return
        for envelope in buffer:
            if is_watched_event(envelope, self.target):
                self._render_event(envelope)

    def _seed_output_from_buffer(self) -> None:
        """Seed the raw-output tail from the App's live raw-output buffer.

        Replays the App's oldest-first ``live_output_buffer`` -- a tuple of
        ``(wave_id, line)`` rows -- into the tail, filtered to the watched
        session's wave, so a mode switch (or a recompose) shows the agent stdout
        that arrived before the tail existed. Appended in order via
        :meth:`OutputTail.extend` so the newest line ends at the bottom and the
        pane is scrolled once. A no-op in the grid path (the parity grid has no
        per-tile output tail), or under a bare harness whose App exposes no
        output buffer -- the tail keeps its pinned waiting notice.
        """
        if self._grid is not None or self._lane_grid is not None or self.target is None:
            return
        buffer = getattr(self.app, "live_output_buffer", ())
        lines = [line for wave_id, line in buffer if wave_id == self.target.wave_id]
        if lines:
            self._output_tail().extend(lines)

    def _on_app_state(self, new_state: State | None) -> None:
        """Recompose the body when a poll tick changes the ACTIVE-executor fleet.

        The poll backstop the project's TUI-staleness lesson pins beside the
        push: the App's reactive ``state`` is refreshed from BOTH the daemon
        push and the always-on mtime-poll, so a session newly dispatched (or
        closed) becomes visible without an app restart even when the push
        dropped the envelope. When the new fleet of ACTIVE executor sessions
        (:func:`parity_session_ids`) differs from the set the body was last
        composed for, the body recomposes so the parity grid re-derives its
        tiles (a 1->2 fleet swaps the zoom for the side-by-side grid, a 2->0
        fleet swaps it for the honest-empty notice). An unchanged fleet is a
        no-op, so the live-pushed event rows already streamed into the tiles
        are not clobbered by a churn of the DOM on every quiet poll tick.

        Args:
            new_state: The fresh read-only state revision from the binder, or
                ``None`` when the binder clears it.
        """
        if not self.is_mounted:
            return
        from eawf.kernel.state.models import State

        resolved = new_state if isinstance(new_state, State) else None
        lane_changed = lane_parity_key(resolved) != self._lane_parity
        if parity_session_ids(resolved) == self._parity_ids and not lane_changed:
            return
        logger.info(
            f"agent_watch_parity_recompose was={list(self._parity_ids)} "
            f"now={list(parity_session_ids(resolved))} lane_changed={lane_changed}"
        )
        self.call_after_refresh(self._recompose_and_reseed)

    async def _recompose_and_reseed(self) -> None:
        """Recompose the body for the new fleet, then re-seed it from the buffer.

        Rebuilds the body so the parity grid (or zoom / honest-empty notice)
        re-derives for the new ACTIVE-executor fleet, then replays the App's
        live buffer into the freshly-composed surface so it shows the events
        that arrived before it existed. Run on the event loop via
        :meth:`~textual.message_pump.MessagePump.call_after_refresh` so the
        async :meth:`~textual.widget.Widget.recompose` is awaited rather than
        left dangling.
        """
        await self.recompose()
        self._seed_from_buffer()
        self._seed_output_from_buffer()

    def _on_render_mode(self, _mode: object) -> None:
        """Repaint the mode-sensitive chrome when the App's render mode flips.

        Swaps the header's session sigil and the idle cancel-look mark between
        their unicode and ASCII columns; the streamed event rows are not
        mode-sensitive (the Feed row formatter owns their glyphs), so only the
        header + the still-idle result line repaint. A no-op once a cancel has
        been issued (the result line is no longer the idle cancel-look line).
        """
        if not self.is_mounted:
            return
        if self._lane_grid is not None:
            # The FA3 lane grid bakes the mode into each row's sigil at build
            # time; a mode flip recomposes the body so the grid re-derives its
            # rows in the new glyph column.
            self.call_after_refresh(self._recompose_and_reseed)
            return
        mode = self._render_mode()
        header = self.query(f"#{WATCH_HEADER_ID}")
        if header:
            header.first(Static).update(render_watch_header(self.target, mode=mode))
        if not self._cancel_issued:
            result = self.query(f"#{WATCH_RESULT_ID}")
            if result:
                result.first(Static).update(self._cancel_idle_line())

    def on_unmount(self) -> None:
        """Unregister from the App fan-out so a torn-down pane gets no pushes."""
        unregister = getattr(self.app, "unregister_feed_listener", None)
        if callable(unregister):
            unregister(self)

    def append_event(self, envelope: Envelope) -> None:
        """Route one live *envelope* to its tile (grid) or stream (zoom).

        The App-fan-out entry point: a no-op when the pane has been unmounted
        between the App scheduling the push and this running. In the
        multi-session grid path the envelope routes to the one tile whose
        session wave it names (:meth:`WatchGrid.append_event`); in the
        single-session zoom path it prepends to the stream only when it belongs
        to the watched session (the zoom shows one session, not the whole feed).

        Args:
            envelope: The live event envelope from the App fan-out.
        """
        if not self.is_mounted or self._lane_grid is not None:
            return
        if self._grid is not None:
            self._grid.append_event(envelope)
            return
        if not is_watched_event(envelope, self.target):
            return
        self._render_event(envelope)

    def append_output(self, wave_id: str, line: str) -> None:
        """Route one live raw-output *line* to the tail when it names the wave.

        The App raw-output fan-out entry point: a no-op when the pane has been
        unmounted between the App scheduling the push and this running, when the
        body is the parity grid (no per-tile output tail in the multi-session
        path), or when *wave_id* does not match the watched session's wave (the
        tail shows one session's stdout, not the whole fleet's). A matching line
        appends to the tail and auto-scrolls so the newest output stays in view.

        Args:
            wave_id: The wave the output line belongs to (the dispatching
                session's ``scope_id``).
            line: The raw stdout line the spawned agent emitted.
        """
        if (
            not self.is_mounted
            or self._grid is not None
            or self._lane_grid is not None
            or self.target is None
        ):
            return
        if wave_id != self.target.wave_id:
            return
        self._output_tail().append_line(line)
        logger.debug(f"append_output wave={wave_id} len={len(line)}")

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
        """Confirm-gate, then ask the daemon to kill the watched session (``k``).

        Gates the destructive kill behind a
        :class:`~eawf.surfaces.tui.screens.overlays.confirm.ConfirmModal` so a
        SIGTERM-class stop of this lane is never one keystroke; only on a
        confirmed ``Yes`` does it issue the ``agent.kill`` request for the
        watched session's wave + attempt through the daemon-client seam and
        surface the typed outcome. With no target there is nothing to cancel
        (surfaced honestly without opening the modal); when the daemon is
        unreachable the result says so rather than implying a kill.
        ``agent.kill`` is still a daemon-side placeholder that returns
        ``killed=false``, so the surfaced result reports that honestly rather
        than faking a successful kill.
        """
        target = self.target
        if target is None:
            self._set_result(CANCEL_NO_TARGET)
            return
        from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal

        def _on_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                logger.debug(f"action_cancel_session cancelled wave={target.wave_id}")
                return
            result_line = self._issue_kill(target)
            self._set_result(result_line)
            logger.info(
                f"action_cancel_session wave={target.wave_id} attempt={target.attempt} "
                f"result={result_line!r}"
            )

        prompt = f"Kill {target.wave_id}? graceful stop (SIGTERM ladder)."
        self._push_overlay(ConfirmModal(prompt), _on_confirm)

    def action_pause_session(self) -> None:
        """Pause / resume the watched lane through the real daemon RPC (``space``).

        Reads the current
        :attr:`~eawf.kernel.state.models.State.dispatch_paused` flag and issues
        ``agent.resume`` when already paused, else ``agent.pause`` -- a
        deliberate operator stop the daemon persists and
        :func:`eawf.workflow.lifecycle.wave.claim_wave` reads to block the next
        claim. Pause is non-destructive (no confirm). With no target there is
        nothing to pause (surfaced honestly); the line carries the persisted
        verdict, or the honest unavailable line when the daemon is unreachable.
        """
        target = self.target
        if target is None:
            self._set_result(f"[$warn]{PAUSE_NO_TARGET}[/]")
            return
        result_line = self._issue_pause()
        self._set_result(result_line)
        logger.info(f"action_pause_session wave={target.wave_id} result={result_line!r}")

    def action_view_log(self) -> None:
        """View the watched session's log, surfacing its handle (``l``).

        Resolves the watched attempt's recorded session-log handle (the
        per-runtime URN the adapter mints) and surfaces it as the result line so
        the operator can locate the session log; a non-destructive read. With no
        target there is nothing to view, and a watched attempt with no recorded
        handle yet surfaces the honest "no session log recorded yet" line rather
        than pointing at a missing log.
        """
        target = self.target
        if target is None:
            self._set_result(f"[$warn]{LOG_NO_TARGET}[/]")
            return
        if target.log_handle is None:
            self._set_result(f"[$warn]{LOG_NO_HANDLE}[/]")
            logger.info(f"action_view_log no_handle wave={target.wave_id} attempt={target.attempt}")
            return
        result_line = f"[$ok]log:[/] [$muted]{escape_markup(target.log_handle)}[/]"
        self._set_result(result_line)
        logger.info(f"action_view_log wave={target.wave_id} handle={target.log_handle!r}")

    #: The mode ``Esc`` steps the FA4 zoom out to -- the broad live-stream Feed
    #: the zoom drills one session out of, mirroring the crash-frame's
    #: feed-return. Switching back to the whole-fleet feed is the natural "leave
    #: this one session" gesture.
    _LEAVE_MODE: ClassVar[str] = "feed"

    def action_leave_zoom(self) -> None:
        """Leave the session zoom for the broad Feed mode (``Esc``).

        Switches back to the whole-fleet :data:`_LEAVE_MODE` (the live-stream
        Feed the zoom drills one session out of) via the App's ``switch_mode``
        seam, the way the crash-frame returns to the feed; degrades to a quiet
        no-op under a bare harness whose App exposes no ``switch_mode`` (the
        binding stays live for affordance parity).
        """
        switch_mode = getattr(self.app, "switch_mode", None)
        if callable(switch_mode):
            switch_mode(self._LEAVE_MODE)
            logger.info(f"action_leave_zoom mode={self._LEAVE_MODE!r}")
            return
        logger.debug("action_leave_zoom no_switch_seam")

    def _push_overlay(
        self, modal: ModalScreen[bool], callback: Callable[[bool | None], None]
    ) -> None:
        """Push an overlay *modal* with *callback*, cap-aware when possible.

        The shared overlay-push seam for this pane's confirm-gated kill: routes
        through the App's depth-capped ``push_modal`` when exposed, falling back
        to a plain ``push_screen`` under a bare harness so the push never raises.

        Args:
            modal: The overlay screen to push (the kill ConfirmModal).
            callback: Invoked with the modal's dismiss value when it closes.
        """
        push_modal = getattr(self.app, "push_modal", None)
        if callable(push_modal):
            push_modal(modal, callback=callback)
            return
        self.app.push_screen(modal, callback)

    def _issue_pause(self) -> str:
        """Toggle dispatch pause via the real RPC and return a result line.

        Calls ``agent.resume`` (when paused) or ``agent.pause`` (when not)
        through the :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam
        when available; the line reports the persisted verdict (``paused`` /
        ``resumed``), or the honest unavailable line rather than a faked toggle.

        Returns:
            A content-markup result line describing the pause / resume outcome.
        """
        unavailable = f"[$warn]{PAUSE_NO_DAEMON}[/]"
        if not self._daemon_available():
            return unavailable
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        paused = self._currently_paused()
        method = _RESUME_RPC if paused else _PAUSE_RPC
        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                result = client.call(method, {})
        except DaemonRpcError as exc:
            logger.debug(f"_issue_pause daemon_rejected method={method!r} message={exc.message!r}")
            return "[$warn]pause: daemon rejected request[/]"
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_pause daemon_fallback method={method!r} cause={exc!r}")
            return unavailable
        now_paused = bool(result.get("paused", not paused))
        verb = "paused" if now_paused else "resumed"
        return f"[$ok]pause: {verb}[/]"

    def _currently_paused(self) -> bool:
        """Return the bound state's ``dispatch_paused`` flag (``False`` if unbound).

        The pause toggle reads the current flag to pick which RPC to issue; an
        unbound state reads as not-paused so the first ``space`` issues
        ``agent.pause``.

        Returns:
            The persisted ``dispatch_paused`` flag, or ``False`` when unbound.
        """
        state = self._current_state()
        return bool(state.dispatch_paused) if state is not None else False

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
                    _KILL_METHOD,
                    {
                        "wave_id": target.wave_id,
                        "attempt": target.attempt,
                        "signal": _SIGNAL_TERM,
                    },
                )
        except DaemonRpcError as exc:
            logger.debug(f"_issue_kill daemon_rejected message={exc.message!r}")
            return "[$warn]cancel: daemon rejected request[/]"
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_kill daemon_fallback cause={exc!r}")
            return f"[$warn]{CANCEL_NO_DAEMON}[/]"
        killed = bool(result.get("killed"))
        signal = str(result.get("signal", _SIGNAL_TERM))
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
        """Update the cancel-result line, if mounted, marking a cancel issued."""
        self._cancel_issued = True
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

    def _cancel_idle_line(self) -> str:
        """Return the idle cancel-result line wearing the failed-look mark.

        Leads the idle :data:`CANCEL_IDLE` copy with the failed-x cancel mark
        (:func:`cancel_mark`) so the destructive kill verb reads with the
        failed look the rest of the reskin uses, in the active render column.

        Returns:
            A content-markup result line: the cancel mark + the idle copy.
        """
        return f"{cancel_mark(mode=self._render_mode())} [$muted]{CANCEL_IDLE}[/]"

    def _render_mode(self) -> RenderMode:
        """Resolve the active render-mode label from the host app.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.render_mode` into the sigil
        helpers so an ``ascii`` flip swaps the header sigil + the cancel mark
        to their ASCII column; falls back to
        :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE` under a
        bare test harness whose host App carries no ``render_mode`` attribute.

        Returns:
            The render-mode label (``"ascii"`` or ``"unicode"``).
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def _output_tail(self) -> OutputTail:
        """Return the mounted raw-output tail for the single-session zoom.

        Only valid in the single-session zoom path (the grid path has no tail);
        callers gate on ``self._grid is None`` before reaching here.

        Returns:
            The mounted :class:`OutputTail` for the watched session.
        """
        return self.query_one(f"#{WATCH_OUTPUT_ID}", OutputTail)

    def _pick_target(self) -> WatchTarget | None:
        """Resolve the default watch target from the bound read-only state."""
        return pick_watch_target(self._current_state())

    def _target_for_wave(self, wave_id: str) -> WatchTarget | None:
        """Build the FA4 watch target for a specific *wave_id* (the FA3 zoom).

        Resolves the lane's wave to a :class:`WatchTarget` so the FA3 -> FA4
        drill streams that lane's session: it reads the wave's runtime from the
        most-recent executor session scoped to the wave (falling back to the
        wave's latest session-attempt runtime), the highest recorded attempt, and
        the attempt's session-log handle. A wave the bound state does not know --
        no session and no wave row -- yields ``None`` so the zoom is a no-op
        rather than streaming an empty session.

        Args:
            wave_id: The lane's wave id the operator chose to zoom.

        Returns:
            The :class:`WatchTarget` for *wave_id*, or ``None`` when the wave is
            unknown to the bound state.
        """
        state = self._current_state()
        if state is None:
            return None
        runtime = _wave_runtime(state, wave_id)
        if runtime is None:
            return None
        attempt = _latest_attempt(state, wave_id=wave_id)
        return WatchTarget(
            session_id=_wave_session_id(state, wave_id) or wave_id,
            wave_id=wave_id,
            runtime=runtime,
            status=_wave_session_status(state, wave_id),
            attempt=attempt,
            log_handle=_log_handle(state, wave_id=wave_id, attempt=attempt),
        )

    def _current_state(self) -> State | None:
        """Return the bound read-only state, if loaded."""
        from eawf.kernel.state.models import State

        app_state = getattr(self.app, "state", None)
        return app_state if isinstance(app_state, State) else None

    def _fleet_verdict_rollup(self) -> list[FleetVerdictRow]:
        """Read each wave's latest auditor verdict for the fleet rollup pane.

        Resolves the host App's read-only ``state.json`` path and rolls up the
        AUDITOR report store off disk (synchronous at build, like the rest of
        the watch panes source their data). A bare test harness whose host App
        carries no ``_state_path`` -- or one whose path is unset -- yields an
        empty rollup, so the pane shows its honest-empty line rather than
        reaching off a missing store.

        Returns:
            One :class:`~eawf.observability.eval.reputation.FleetVerdictRow`
            per wave with an auditor verdict, ordered by wave id; empty when no
            verdict row exists or no state path is bound.
        """
        state_path = self._state_path()
        if state_path is None:
            return []
        return fleet_verdict_rollup(state_path)

    def _state_path(self) -> Path | None:
        """Return the host App's read-only ``state.json`` path, if configured."""
        from pathlib import Path

        path = getattr(self.app, "_state_path", None)
        return path if isinstance(path, Path) else None

    def _degraded(self) -> bool:
        """Return whether the host App reports a daemon-unreachable state.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.degraded` into the grid so a
        zero-tile grid reads as honest-degraded rather than honest-empty when
        the daemon is unreachable; a bare harness without the flag reads as not
        degraded.
        """
        return bool(getattr(self.app, "degraded", False))

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
    "LANE_GRID_EMPTY",
    "LANE_GRID_EMPTY_ID",
    "LANE_GRID_ID",
    "LANE_GRID_ROW_CLASS",
    "LANE_SELECTED_CLASS",
    "LOG_NO_HANDLE",
    "LOG_NO_TARGET",
    "PAUSE_NO_DAEMON",
    "PAUSE_NO_TARGET",
    "ROLLUP_EMPTY_NOTICE",
    "WATCH_DEGRADED",
    "WATCH_EMPTY_ID",
    "WATCH_GRID_EMPTY_ID",
    "WATCH_GRID_ID",
    "WATCH_HEADER_ID",
    "WATCH_LIST_ID",
    "WATCH_OUTPUT_ID",
    "WATCH_RESULT_ID",
    "WATCH_ROLLUP_EMPTY_ID",
    "WATCH_ROLLUP_ID",
    "WATCH_ROLLUP_ROW_CLASS",
    "WATCH_ROW_CLASS",
    "WATCH_TILE_CLASS",
    "WATCH_TILE_LIST_CLASS",
    "WATCH_TILE_ROW_CLASS",
    "AgentWatchModeScreen",
    "LaneGrid",
    "LaneGridRow",
    "LaneState",
    "VerdictRollupPane",
    "WatchGrid",
    "WatchTarget",
    "WatchTile",
    "active_executor_sessions",
    "cancel_mark",
    "is_watched_event",
    "lane_grid_rows",
    "lane_parity_key",
    "lane_state_sigil_markup",
    "parity_session_ids",
    "pick_watch_target",
    "render_lane_row",
    "render_verdict_rollup_row",
    "render_watch_header",
    "session_routes_event",
    "session_sigil_markup",
    "tile_dom_id",
    "verdict_sigil_markup",
]
