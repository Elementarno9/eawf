"""``ForkInboxModal`` -- the FA5 blocking-fork interrupt inbox overlay (DL-6).

When the daemon-owned fleet auto-drain loop pauses a lane to a blocking fork (a
high-risk close, an uncalibrated-jury advisory, or a needs-user split) it removes
that lane from :attr:`~eawf.kernel.state.models.FleetRun.lanes` and appends a
typed :class:`~eawf.kernel.state.models.FleetFork` to
:attr:`~eawf.kernel.state.models.FleetRun.forks` while the sibling lanes keep
draining. This overlay is the operator-as-decider surface: it queues those
forks and asks the operator to RESOLVE each one.

The card (the per-fork decision surface)
----------------------------------------
The inbox shows ONE fork at a time as a card that names the forked wave, its
:class:`~eawf.kernel.state.enums.RiskTier` band badge, WHY it forked (the
:class:`~eawf.kernel.state.models.FleetForkReason`), the EVIDENCE backing the
fork (the close verdict / jury ballot / needs-user question, read off
:attr:`FleetFork.evidence_ref`), and the four option keys. Each option key maps
to one of the four closed :class:`~eawf.kernel.state.models.FleetForkResolution`
paths and routes the operator's choice to the ``fleet.resolve_fork`` RPC:

* ``a`` -- approve-close (:attr:`FleetForkResolution.APPROVE_CLOSE`): accept the
  held close, the wave resolves CLOSED.
* ``r`` -- re-dispatch (:attr:`FleetForkResolution.RE_DISPATCH`): re-queue the
  wave onto the run frontier for a fresh lane / attempt.
* ``s`` -- skip (:attr:`FleetForkResolution.SKIP`): leave the wave PENDING and
  free the lane slot without re-queuing.
* ``x`` -- abort (:attr:`FleetForkResolution.ABORT_RUN`): halt the WHOLE run,
  abandoning every still-queued fork.

Cycle without losing one (the C2 queue contract)
------------------------------------------------
Multiple queued forks render a ``i/N`` count and ``Tab`` / ``shift+tab`` cycle
between them; resolving a fork advances to the next queued fork rather than
dismissing the inbox, so a multi-fork queue is drained one card at a time
without losing one. When the LAST queued fork resolves the inbox dismisses back
to the cockpit. With no forks the inbox renders the honest-empty
:data:`FORK_INBOX_EMPTY` literal rather than a blank card.

Routes-not-resolves (the daemon-client seam)
--------------------------------------------
The overlay holds NO resolution logic: it issues ``fleet.resolve_fork`` through
the same :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the rest of
the TUI mutates through, and surfaces the typed outcome honestly. An unreachable
or rejecting daemon is reported rather than faked -- a resolution that did not
land is never reported as one. The daemon owns the actual wave-state mutation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static

from eawf.kernel.state.enums import RiskTier
from eawf.kernel.state.models import FleetForkResolution
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import chrome

if TYPE_CHECKING:
    from eawf.kernel.state.models import FleetFork

logger = logging.getLogger(__name__)

#: Daemon JSON-RPC method the four resolution keys route through -- resolve a
#: single queued :class:`FleetFork` by ``(wave_id, attempt)`` + resolution.
_RESOLVE_RPC: str = "fleet.resolve_fork"

#: Render-mode label threaded into the sigil helper when the host App exposes no
#: ``render_mode`` (a bare standalone harness): the unicode column is the
#: default surface, ``"ascii"`` only when the App resolves it.
_DEFAULT_RENDER_MODE: str = "unicode"

#: Card title prefix -- the literal that leads the fork-inbox header before the
#: ``i/N`` queue position.
FORK_INBOX_TITLE: str = "blocking fork -- operator decision needed"

#: Honest-empty card body shown when the fork queue is empty: a calm literal
#: rather than a blank card that would read as a primed-but-stalled inbox.
FORK_INBOX_EMPTY: str = "no blocking forks -- the fleet is draining clean"

#: Id of the card box (the centred modal container).
FORK_INBOX_BOX_ID: str = "fork-inbox-box"

#: Id of the title / queue-position header row.
FORK_INBOX_HEADER_ID: str = "fork-inbox-header"

#: Id of the wave + risk-tier badge row.
FORK_INBOX_WAVE_ID: str = "fork-inbox-wave"

#: Id of the fork-reason row (WHY the lane forked).
FORK_INBOX_REASON_ID: str = "fork-inbox-reason"

#: Id of the evidence row (the close verdict / jury ballot / needs-user ref).
FORK_INBOX_EVIDENCE_ID: str = "fork-inbox-evidence"

#: Id of the four-option-keys row.
FORK_INBOX_OPTIONS_ID: str = "fork-inbox-options"

#: Id of the resolution-result line (below the options; honest about whether the
#: RPC was issued, accepted, or could not reach the daemon).
FORK_INBOX_RESULT_ID: str = "fork-inbox-result"

#: Id of the key-hint footer row.
FORK_INBOX_HINT_ID: str = "fork-inbox-hint"

#: The four resolution option keys, in card order: (key, label, resolution).
#: The keys are lowercase letters so they never collide with the ``Tab`` cycle.
_OPTIONS: tuple[tuple[str, str, FleetForkResolution], ...] = (
    ("a", "approve-close", FleetForkResolution.APPROVE_CLOSE),
    ("r", "re-dispatch", FleetForkResolution.RE_DISPATCH),
    ("s", "skip", FleetForkResolution.SKIP),
    ("x", "abort run", FleetForkResolution.ABORT_RUN),
)

#: Resolution lookup keyed by option key -- the RPC the pressed key routes to.
_RESOLUTION_BY_KEY: dict[str, FleetForkResolution] = {
    key: resolution for key, _label, resolution in _OPTIONS
}

#: Human-facing phrase per fork reason -- names WHY the lane forked (C1). Keyed
#: by the :class:`FleetForkReason` string value so the lookup stays decoupled
#: from importing the enum, and so a reason that drifted past the map still
#: renders an honest fallback rather than a blank reason row.
_REASON_HEADLINE: dict[str, str] = {
    "high_risk_close": "high-risk close -- a visual-band lane reported clean, held for review",
    "uncalibrated_jury": "uncalibrated jury -- the gating jury holds only advisory authority",
    "needs_user_split": "needs-user split -- the lane hit a clarification mid-run",
}

#: Risk-tier badge phrase per tier value -- the band badge the card renders on
#: the queued fork. Keyed by the :class:`RiskTier` string value.
_TIER_BADGE: dict[str, str] = {
    RiskTier.MECH.value: "MECH",
    RiskTier.MED.value: "MED",
    RiskTier.HIGH.value: "HIGH",
    RiskTier.UI.value: "UI",
}

#: Honest line when a queued fork carries no evidence ref (the loop captured
#: none): the row says so rather than rendering a blank evidence line.
EVIDENCE_NONE: str = "no evidence ref captured"

#: The key-hint footer vocab, mirroring the other reskin overlays: a calm
#: middle-dot-separated chord list under the card.
_KEY_HINT: str = "[ a approve · r re-dispatch · s skip · x abort · Tab next · Esc close ]"

#: Result line before any resolution has been issued (the idle decision surface).
RESOLVE_IDLE: str = "press a / r / s / x to resolve this fork"

#: Result line when the resolution request could not reach the daemon.
RESOLVE_NO_DAEMON: str = "resolve: daemon unavailable -- request not issued"


@dataclass(frozen=True)
class _ResolveResult:
    """Result line plus whether the daemon accepted the fork resolution."""

    line: str
    accepted: bool


def reason_headline(fork: FleetFork) -> str:
    """Return the human-facing phrase naming *fork*'s reason (C1).

    Resolves the :attr:`~eawf.kernel.state.models.FleetFork.reason` to its
    :data:`_REASON_HEADLINE` phrase so the card NAMES why the lane forked. A
    reason that drifted past the map reads an honest fallback naming the raw
    reason rather than a blank row.

    Args:
        fork: The queued fork the card debriefs.

    Returns:
        The fork-reason headline phrase.
    """
    reason = fork.reason.value
    return _REASON_HEADLINE.get(reason, f"forked -- {reason}")


def tier_badge(fork: FleetFork) -> str:
    """Return the risk-tier band badge for *fork* (C1).

    Resolves the :attr:`~eawf.kernel.state.models.FleetFork.risk_tier` to its
    :data:`_TIER_BADGE` short label so the card shows which band the lane sits
    in. A tier past the map reads the raw value uppercased.

    Args:
        fork: The queued fork the card debriefs.

    Returns:
        The risk-tier band badge label.
    """
    tier = fork.risk_tier.value
    return _TIER_BADGE.get(tier, tier.upper())


def evidence_line(fork: FleetFork) -> str:
    """Return the evidence ref backing *fork*, or the honest-empty marker (C1).

    Reads :attr:`~eawf.kernel.state.models.FleetFork.evidence_ref` -- the close
    verdict / jury ballot / needs-user question the operator reads before
    resolving. A fork the loop captured no ref for reads :data:`EVIDENCE_NONE`
    rather than a blank evidence row.

    Args:
        fork: The queued fork the card debriefs.

    Returns:
        The evidence ref, or :data:`EVIDENCE_NONE` when none was captured.
    """
    return fork.evidence_ref if fork.evidence_ref else EVIDENCE_NONE


def render_options_row() -> str:
    """Render the four-option-keys row (the resolution affordances).

    Lays the four closed-set resolution keys out in one row -- each a ``key
    label`` pair -- so the operator reads every available decision at a glance.

    Returns:
        A content-markup options row string.
    """
    cells = [
        f"[$accent]{key}[/] [$muted]{escape_markup(label)}[/]" for key, label, _res in _OPTIONS
    ]
    return "   ".join(cells)


def issue_resolve(
    fork: FleetFork,
    resolution: FleetForkResolution,
    *,
    daemon_available: bool,
) -> str:
    """Issue the ``fleet.resolve_fork`` RPC for *fork* and return a result line.

    Folds the queued *fork*'s ``(wave_id, attempt)`` key + the chosen
    *resolution* into the ``fleet.resolve_fork`` params and calls it through the
    :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam when the daemon
    is reachable. The line reports the run state the daemon returned, or the
    honest unavailable / rejected line rather than a faked resolution.

    Args:
        fork: The queued fork to resolve.
        resolution: The operator's chosen
            :class:`~eawf.kernel.state.models.FleetForkResolution`.
        daemon_available: Whether the host App reports a reachable daemon socket.

    Returns:
        A content-markup result line describing the resolution outcome.
    """
    return _issue_resolve(fork, resolution, daemon_available=daemon_available).line


def _issue_resolve(
    fork: FleetFork,
    resolution: FleetForkResolution,
    *,
    daemon_available: bool,
) -> _ResolveResult:
    """Issue the resolve RPC and record whether the daemon accepted it."""
    if not daemon_available:
        return _ResolveResult(f"[$warn]{RESOLVE_NO_DAEMON}[/]", accepted=False)
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient(call_timeout_seconds=30.0) as client:
            result = client.call(
                _RESOLVE_RPC,
                {
                    "wave_id": fork.wave_id,
                    "attempt": fork.attempt,
                    "resolution": resolution.value,
                },
            )
    except DaemonRpcError as exc:
        logger.debug(f"issue_resolve daemon_rejected message={exc.message!r}")
        return _ResolveResult(
            f"[$warn]resolve: daemon rejected request[/] [$muted]{escape_markup(exc.message)}[/]",
            accepted=False,
        )
    except (OSError, RuntimeError, TimeoutError) as exc:
        logger.debug(f"issue_resolve daemon_fallback cause={exc!r}")
        return _ResolveResult(f"[$warn]{RESOLVE_NO_DAEMON}[/]", accepted=False)
    run_state = str(result.get("run_state", ""))
    return _ResolveResult(
        (
            f"[$ok]resolve: {escape_markup(resolution.value)}[/] "
            f"[$muted]{escape_markup(fork.wave_id)} run={escape_markup(run_state)}[/]"
        ),
        accepted=True,
    )


class ForkInboxModal(ModalScreen[None]):
    """The FA5 blocking-fork interrupt inbox (dismisses ``None`` on close).

    A centred decision card opened over the cockpit when the fleet auto-drain
    loop pauses a lane to a blocking fork. The card NAMES the forked wave, its
    :class:`~eawf.kernel.state.enums.RiskTier` band badge, WHY it forked, the
    evidence backing it, and the four resolution option keys. Each option key
    (``a`` approve-close / ``r`` re-dispatch / ``s`` skip / ``x`` abort) routes
    the operator's choice to the ``fleet.resolve_fork`` RPC through the
    daemon-client seam. ``Tab`` / ``shift+tab`` cycle a multi-fork queue;
    resolving advances to the next queued fork, and the last resolution dismisses
    back to the cockpit. ``Esc`` closes the inbox without resolving. With no forks
    the card renders the honest-empty :data:`FORK_INBOX_EMPTY` literal.

    The overlay holds NO resolution logic -- it routes a typed choice to the
    daemon and surfaces the outcome honestly.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ForkInboxModal {
        align: center middle;
    }
    ForkInboxModal > #fork-inbox-box {
        width: 90%;
        min-width: 36;
        max-width: 96;
        height: auto;
        max-height: 90%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    ForkInboxModal .fork-inbox-title {
        height: auto;
        text-style: bold;
        margin-bottom: 1;
    }
    ForkInboxModal #fork-inbox-wave {
        height: auto;
    }
    ForkInboxModal #fork-inbox-reason {
        height: auto;
    }
    ForkInboxModal #fork-inbox-evidence {
        height: auto;
        margin-bottom: 1;
    }
    ForkInboxModal #fork-inbox-options {
        height: auto;
    }
    ForkInboxModal #fork-inbox-result {
        height: auto;
        color: $text-muted;
        margin-top: 1;
    }
    ForkInboxModal #fork-inbox-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: ``a`` / ``r`` / ``s`` / ``x`` resolve; ``Tab`` / ``shift+tab`` cycle the
    #: queue; ``Esc`` closes without resolving. The resolution letters are
    #: lowercase so they never collide with the cycle keys.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("a", "resolve('a')", "approve", show=False),
        Binding("r", "resolve('r')", "re-dispatch", show=False),
        Binding("s", "resolve('s')", "skip", show=False),
        Binding("x", "resolve('x')", "abort", show=False),
        Binding("tab", "cycle(1)", "next", show=False),
        Binding("shift+tab", "cycle(-1)", "prev", show=False),
        Binding("escape", "close_inbox", "close", show=False),
    ]

    #: Index of the displayed fork in the queue. ``Tab`` cycles it (wrapping);
    #: resolving advances it onto the next queued fork.
    index: reactive[int] = reactive(0)

    def __init__(self, forks: tuple[FleetFork, ...]) -> None:
        """Construct the inbox over the queued *forks*.

        Args:
            forks: The queued :class:`~eawf.kernel.state.models.FleetFork` rows
                read off :attr:`~eawf.kernel.state.models.FleetRun.forks` -- one
                card per fork, cycled by ``Tab``. An empty tuple renders the
                honest-empty card.
        """
        super().__init__()
        self._forks: tuple[FleetFork, ...] = forks

    def compose(self) -> ComposeResult:
        """Yield the header, the wave / reason / evidence rows, options, and hints.

        With no queued fork the body collapses to the honest-empty literal under
        the title; otherwise the rows render the current fork and the four option
        keys follow.
        """
        with Vertical(id=FORK_INBOX_BOX_ID):
            yield Static(self._header_line(), classes="fork-inbox-title", id=FORK_INBOX_HEADER_ID)
            if not self._forks:
                yield Static(f"[$muted]{FORK_INBOX_EMPTY}[/]", id=FORK_INBOX_REASON_ID)
                yield Static(_KEY_HINT, id=FORK_INBOX_HINT_ID)
                return
            yield Static(self._wave_line(), id=FORK_INBOX_WAVE_ID)
            yield Static(self._reason_line(), id=FORK_INBOX_REASON_ID)
            yield Static(self._evidence_line(), id=FORK_INBOX_EVIDENCE_ID)
            yield Static(render_options_row(), id=FORK_INBOX_OPTIONS_ID)
            yield Static(f"[$muted]{RESOLVE_IDLE}[/]", id=FORK_INBOX_RESULT_ID)
            yield Static(_KEY_HINT, id=FORK_INBOX_HINT_ID)

    def on_mount(self) -> None:
        """Watch for a render-mode flip so the header sigil repaints in its column."""
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)

    def _on_render_mode(self, _mode: object) -> None:
        """Repaint the rendered rows when the App's render mode flips."""
        self._repaint()

    def _render_mode(self) -> str:
        """Resolve the active render-mode label from the host app.

        Returns:
            The render-mode label (``"ascii"`` or a unicode label).
        """
        return getattr(self.app, "render_mode", _DEFAULT_RENDER_MODE)

    def watch_index(self) -> None:
        """Repaint the card rows when the queue cursor moves."""
        if self.is_mounted:
            self._repaint()

    def _current_fork(self) -> FleetFork | None:
        """Return the fork at the queue cursor, or ``None`` when the queue is empty."""
        if not self._forks or not 0 <= self.index < len(self._forks):
            return None
        return self._forks[self.index]

    def _header_line(self) -> str:
        """Render the card header: the gate sigil + title + ``i/N`` queue position.

        The header leads with the gate chrome sigil, names the fork-decision
        title, and -- when more than one fork is queued -- trails the ``i/N``
        position so the operator reads how many forks remain.
        """
        sigil = chrome("attention", mode=self._render_mode())
        total = len(self._forks)
        position = f"  [$muted]{self.index + 1}/{total}[/]" if total > 1 else ""
        return f"[$err]{sigil} {FORK_INBOX_TITLE}[/]{position}"

    def _wave_line(self) -> str:
        """Render the forked-wave + risk-tier band-badge row (C1)."""
        fork = self._current_fork()
        if fork is None:  # pragma: no cover - rows render only on a non-empty queue
            return ""
        badge = escape_markup(f"[{tier_badge(fork)}]")
        return f"[$accent]{escape_markup(fork.wave_id)}[/] [$warn]{badge}[/]"

    def _reason_line(self) -> str:
        """Render the WHY-it-forked reason row (C1)."""
        fork = self._current_fork()
        if fork is None:  # pragma: no cover - rows render only on a non-empty queue
            return ""
        return f"[$muted]why[/] [$warn]{escape_markup(reason_headline(fork))}[/]"

    def _evidence_line(self) -> str:
        """Render the evidence-ref row (the close verdict / jury ballot / question)."""
        fork = self._current_fork()
        if fork is None:  # pragma: no cover - rows render only on a non-empty queue
            return ""
        return f"[$muted]evidence[/] [$accent]{escape_markup(evidence_line(fork))}[/]"

    def _repaint(self) -> None:
        """Repaint the header + fork rows for the current queue cursor."""
        self.query_one(f"#{FORK_INBOX_HEADER_ID}", Static).update(self._header_line())
        if not self._forks:
            return
        self.query_one(f"#{FORK_INBOX_WAVE_ID}", Static).update(self._wave_line())
        self.query_one(f"#{FORK_INBOX_REASON_ID}", Static).update(self._reason_line())
        self.query_one(f"#{FORK_INBOX_EVIDENCE_ID}", Static).update(self._evidence_line())

    def action_cycle(self, delta: int) -> None:
        """Cycle the queue cursor by *delta* (wrapping), a no-op on an empty queue.

        Args:
            delta: ``1`` (next) or ``-1`` (previous).
        """
        if not self._forks:
            return
        self.index = (self.index + delta) % len(self._forks)

    def action_resolve(self, key: str) -> None:
        """Resolve the current fork via the option *key*, then advance the queue.

        Routes the option *key*'s :class:`FleetForkResolution` to the
        ``fleet.resolve_fork`` RPC for the displayed fork and surfaces the typed
        outcome on the result line. The fork is dropped from the local queue
        only after daemon acceptance; a no-daemon or rejected request keeps the
        card visible. With no fork to resolve (an empty queue) the key is a no-op.

        Args:
            key: The pressed option key (one of ``a`` / ``r`` / ``s`` / ``x``).
        """
        fork = self._current_fork()
        if fork is None:
            return
        resolution = _RESOLUTION_BY_KEY[key]
        result = _issue_resolve(fork, resolution, daemon_available=self._daemon_available())
        self._set_result(result.line)
        logger.info(
            f"fork_inbox resolved wave={fork.wave_id} attempt={fork.attempt} "
            f"resolution={resolution.value} accepted={result.accepted} result={result.line!r}"
        )
        if result.accepted:
            self._drop_resolved(fork)

    def _drop_resolved(self, fork: FleetFork) -> None:
        """Drop the just-resolved *fork* from the queue and advance, or dismiss.

        Removes *fork* from the local queue (so a re-press never re-resolves it),
        clamps the cursor into the shrunken queue, and repaints the next card. An
        emptied queue dismisses the inbox back to the cockpit (every fork
        resolved).

        Args:
            fork: The fork just resolved.
        """
        remaining = tuple(item for item in self._forks if item is not fork)
        self._forks = remaining
        if not remaining:
            logger.info("_drop_resolved drained=True remaining=0")
            self.dismiss(None)
            return
        self.index = min(self.index, len(remaining) - 1)
        self._repaint()
        self._set_result(f"[$muted]{RESOLVE_IDLE}[/]")

    def action_close_inbox(self) -> None:
        """Close the inbox without resolving (``Esc``)."""
        logger.debug(f"fork_inbox closed queued={len(self._forks)}")
        self.dismiss(None)

    def _set_result(self, line: str) -> None:
        """Update the resolution-result line, if mounted."""
        result = self.query(f"#{FORK_INBOX_RESULT_ID}")
        if result:
            result.first(Static).update(line)

    def _daemon_available(self) -> bool:
        """Return whether the host App reports a reachable daemon socket.

        Delegates to the App's own daemon-socket probe so the resolution path
        uses the same reachability verdict the rest of the TUI mutates through;
        a bare harness without the probe degrades to "unavailable".
        """
        probe = getattr(self.app, "_daemon_socket_available", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except OSError as exc:
            logger.debug(f"fork_inbox daemon_available probe_failed cause={exc!r}")
            return False


__all__ = [
    "EVIDENCE_NONE",
    "FORK_INBOX_BOX_ID",
    "FORK_INBOX_EMPTY",
    "FORK_INBOX_EVIDENCE_ID",
    "FORK_INBOX_HEADER_ID",
    "FORK_INBOX_HINT_ID",
    "FORK_INBOX_OPTIONS_ID",
    "FORK_INBOX_REASON_ID",
    "FORK_INBOX_RESULT_ID",
    "FORK_INBOX_TITLE",
    "FORK_INBOX_WAVE_ID",
    "RESOLVE_IDLE",
    "RESOLVE_NO_DAEMON",
    "ForkInboxModal",
    "evidence_line",
    "issue_resolve",
    "reason_headline",
    "render_options_row",
    "tier_badge",
]
