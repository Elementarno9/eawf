"""``AutopilotModeScreen`` -- the ready-wave frontier + dispatch pane (mode digit 2).

The Autopilot mode (digit ``2``) renders the **dependency frontier** of the
active scope's wave graph -- the PENDING waves that are claim-ready right now
(every dep CLOSED + no lower-numbered ready sibling under the same iter) -- in
claim order, and offers **dispatch controls** that ask the daemon to claim +
spawn the selected ready wave.

Reusing the frontier compute (not a second predicate)
-----------------------------------------------------
The pane never re-derives the claimability rule. It projects the bound
read-only :class:`~eawf.kernel.state.models.State` into the slim
:class:`~eawf.kernel.spec.auq_bridge.WaveFrontierItem` view and calls the shared
:func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier`, which encodes the
exact claim-time gate (:func:`eawf.workflow.lifecycle.wave.claim_wave`) purely
off the view. So the frontier the operator sees is the same set the claim gate
would accept, single-sourced -- this pane is READ-only over state and adds no
predicate of its own. The resulting
:class:`~eawf.kernel.spec.auq_bridge.DrainableFrontier` carries the ready rows
in claim order, which the pane lists one per row.

Dispatch controls (the daemon-client seam)
------------------------------------------
The dispatch action (``d``) asks the daemon to live-spawn the selected ready
wave by calling ``agent.dispatch`` with ``spawn=True`` (the W01 live-spawn path)
through the same :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam the
rest of the TUI mutates through. The daemon owns claim + session registration +
the spawn behind the safety floor; the TUI only issues the request and surfaces
the typed result honestly. An unavailable socket or a rejecting / erroring spawn
is surfaced honestly rather than faked -- a spawn that did not happen is never
reported as one.

Multi-select fleet dispatch (the batch claim)
---------------------------------------------
``m`` opens an inline
:class:`~eawf.surfaces.tui.screens.overlays.multichoice_checklist.MultichoiceChecklist`
whose choices are EXACTLY the ready frontier ids; ``Space`` toggles ``[X]``
membership and ``Enter`` commits. On commit the staged ids dispatch as a fleet:
:meth:`AutopilotModeScreen._dispatch_claim_batch` calls ``agent.dispatch``
(``spawn=True``) ONCE per staged wave on a Textual worker (off the UI thread).
An unreachable daemon issues ZERO RPCs and surfaces the honest
:data:`BATCH_NO_DAEMON` line; a wave the daemon rejects (e.g. ``-32602``) reads
``rejected`` while the rest still dispatch -- one rejection never aborts the
fleet.

Intervention controls (the cockpit keys)
----------------------------------------
Beyond dispatch the pane offers the ratified cockpit intervention keys, all
routed through the same daemon-client seam so the TUI never mutates out of band
(per-method docstrings carry the detail):

* ``K`` (kill) / ``H`` (halt) stop the selected wave's spawned child through the
  real ``agent.kill`` RPC -- a SIGKILL-class signal vs a graceful SIGTERM, the
  daemon owning the ladder -- each gated behind a destructive-confirm
  :class:`~eawf.surfaces.tui.screens.overlays.confirm.ConfirmModal`.
  ``agent.kill`` is a placeholder returning ``killed=false``, reported honestly.
* ``space`` (pause / resume) is **live**: it reads
  :attr:`~eawf.kernel.state.models.State.dispatch_paused` and routes to
  ``agent.resume`` when paused, else ``agent.pause`` -- a deliberate stop the
  daemon persists and :func:`eawf.workflow.lifecycle.wave.claim_wave` reads.
* ``S`` (skip) advances the selection past the current ready wave -- a cheap
  local step rather than a doomed "skip this ready wave" daemon round-trip.
* ``a`` (arm / launch-flow) opens the FA1
  :class:`~eawf.surfaces.tui.screens.overlays.arm.ArmModal` launch form (scope /
  budget / concurrency / risk policy / convergence); ``Enter`` folds the typed
  spec into a ``fleet.drive`` RPC and flips the cockpit to ``DRAINING``, while a
  dry frontier surfaces the honest "nothing to drain" banner and refuses to arm.

Ready vs blocked split (the cosmetic-terminal reskin)
-----------------------------------------------------
The list renders the frontier as two bands. The **ready** band lists the
claim-ready waves, each leading with the multi-select checkbox affordance
(:func:`~eawf.surfaces.tui.widgets.sigils.chrome` ``check_on`` / ``check_off``)
and the dispatch chrome arrow. The **blocked** band lists the PENDING waves
held off the frontier, each naming the dep blocking it (e.g. ``<- P29-I04-W06``)
so the operator reads what must close or dispatch first. Both glyph columns
honour the App's resolved :attr:`~eawf.surfaces.tui.app.EaApp.render_mode`.

Honest-empty is the COMMON path: a scope whose wave graph has no claim-ready
wave renders the muted :data:`EMPTY_NOTICE` banner instead of an empty table
that reads as a quiet "ready to go", like the evidence / trust / research modes'
honest-empty surfaces; any blocked waves still render below it.

The fleet cockpit vitals header (the N-lane evolution)
------------------------------------------------------
Above the ready/blocked split the pane renders a single **vitals header** line
that reads the persisted :attr:`~eawf.kernel.state.models.State.fleet_run`
(:class:`~eawf.kernel.state.models.FleetRun` + its
:class:`~eawf.kernel.state.models.FleetCounters`) and surfaces, in one row, the
run-state sigil, the ``N/M lanes`` occupancy, the ``frontier K left`` queue
depth, the EU block-bar with its spend ratio, the ``$ used/cap`` spend, the
``wv/hr`` throughput, and the fork badge. Every figure is read STRAIGHT off the
persisted ``fleet_run`` -- the daemon loop is the only mutator and it persists
the counters, throughput, and terminal reason, so the cockpit never recomputes a
tally in the UI (the FA7 run-summary contract: surface what the daemon stored).
Single-wave dispatch is just the ``N=1``, concurrency-1 case of the same header.

Before the fleet is armed (``fleet_run`` is ``None`` or its run-state is
``IDLE``) the header renders the honest-empty cockpit hero -- the pinned
:data:`COCKPIT_IDLE` literal plus the ``a`` arm hint -- rather than a fabricated
zeroed vitals row that would read as a primed-but-stalled run. The vitals
refresh on the same daemon-push + mtime-poll backstop the rest of the TUI rides
(the App's reactive ``state`` flows a fresh ``fleet_run`` into :meth:`_rebuild`),
so the header never goes stale until a restart.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

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
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.ids import natural_key
from eawf.surfaces.tui.scopes import ScopeScreen
from eawf.surfaces.tui.widgets import sigils
from eawf.surfaces.tui.widgets.empty_state import (
    HONEST_EMPTY_CSS,
    render_empty_state,
    seal_empty_hero,
    seal_hero_css,
)
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, render_bar_markup
from eawf.surfaces.tui.widgets.footer import render_hint_label
from eawf.surfaces.tui.widgets.markup import escape_markup

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.screen import ModalScreen

    from eawf.kernel.state.models import FleetFork, FleetRun, State, Wave
    from eawf.surfaces.tui.screens.overlays.arm import ArmSpec
    from eawf.surfaces.tui.screens.overlays.multichoice_checklist import MultichoiceChecklist
    from eawf.surfaces.tui.widgets.eu_bar import RenderMode

logger = logging.getLogger(__name__)

#: Id of the frontier header banner (the ready-count line above the list).
FRONTIER_HEADER_ID: str = "autopilot-header"

#: Id of the scrollable ready-wave list container.
FRONTIER_LIST_ID: str = "autopilot-list"

#: Id of the ready-section sub-header (the "ready" band caption).
READY_SECTION_ID: str = "autopilot-ready-section"

#: Id of the blocked-section sub-header (the "blocked" band caption).
BLOCKED_SECTION_ID: str = "autopilot-blocked-section"

#: Id of the honest-empty notice shown when no wave is claim-ready.
FRONTIER_EMPTY_ID: str = "autopilot-empty"

#: Id of the fleet-cockpit vitals header row (above the frontier header). Carries
#: the run-state sigil + lanes / frontier / EU / $ / throughput / fork vitals
#: when a run is armed, or the honest-empty cockpit hero when it is not.
COCKPIT_VITALS_ID: str = "autopilot-cockpit"

#: Honest-empty cockpit hero shown before the fleet is armed (``fleet_run`` is
#: ``None`` or ``IDLE``): a calm pinned literal rather than a fabricated zeroed
#: vitals row that would read as a primed-but-stalled run. The ``a`` arm hint
#: trails it so the operator reads the next move.
COCKPIT_IDLE: str = "autopilot idle -- press a to arm the fleet drive"

#: The terminal run-state value that opens the FA7 run-summary card: a fleet run
#: that reached :attr:`~eawf.kernel.state.models.FleetRunState.DONE`.
_TERMINAL_RUN_STATE: str = "done"

#: Caption labels in the vitals header, in render order. Bare ASCII so the row
#: reads identically in both glyph columns.
COCKPIT_LANES_LABEL: str = "lanes"
COCKPIT_FRONTIER_LABEL: str = "frontier"
COCKPIT_FRONTIER_SUFFIX: str = "left"
COCKPIT_THROUGHPUT_LABEL: str = "wv/hr"

#: Run-state sigil map: a :class:`~eawf.kernel.state.models.FleetRunState` value
#: -> the lifecycle :class:`~eawf.surfaces.tui.widgets.sigils.Sigil` whose shape
#: reads the run-state honestly. DRAINING wears the RUNNING diamond (the loop is
#: actively claiming), PAUSED / HALTED the CLAIMED half-circle (held, not
#: progressing), DONE the CLOSED filled circle (terminal), IDLE the PENDING ring
#: (armed-but-not-yet-draining). Reusing the lifecycle alphabet keeps the cockpit
#: from inventing a glyph of its own. Keyed by the run-state string value so the
#: lookup stays decoupled from importing the enum at module load.
_RUN_STATE_SIGILS: dict[str, sigils.Sigil] = {
    "idle": sigils.Sigil.PENDING,
    "draining": sigils.Sigil.RUNNING,
    "paused": sigils.Sigil.CLAIMED,
    "halted": sigils.Sigil.CLAIMED,
    "done": sigils.Sigil.CLOSED,
}

#: The fork badge shape the vitals header trails when the run has recorded a
#: fork (a failed / re-planned lane): the FAILED cross so a forked run reads as
#: needing attention, distinct from a clean drain. The ``(unicode, ascii)`` pair
#: is resolved against the active glyph column.
_FORK_BADGE: tuple[str, str] = sigils._LIFECYCLE[sigils.Sigil.FAILED]

#: Id of the in-flight-lanes section sub-header (the "lanes" band caption).
LANES_SECTION_ID: str = "autopilot-lanes-section"

#: CSS class on each rendered in-flight lane cell row.
LANE_CELL_CLASS: str = "autopilot-lane-cell"

#: Caption above the in-flight-lanes band -- the lanes the loop is actively
#: draining, each showing its repair counter.
LANES_CAPTION: str = "lanes"

#: Repair-attempt budget the lane cell's ``repair n/<budget>`` counter reads
#: against -- the bounded grounded-repair loop's attempt ceiling (mirrors
#: :data:`eawf.workflow.dispatch.retry.DEFAULT_MAX_REPAIR_ATTEMPTS`). Pinned as a
#: local constant rather than imported so the TUI cold path never pulls the
#: runtime dispatch stack; the cell only DISPLAYS the budget, it never enforces
#: it (the daemon loop owns the ceiling).
REPAIR_BUDGET: int = 3

#: The label the lane cell leads its repair counter with -- ``repair n/<budget>``
#: so the operator reads how many grounded-repair attempts the lane has burned.
REPAIR_LABEL: str = "repair"

#: The label the lane cell escalates to when its wave has exhausted repair and
#: forked (a ``REPAIR_EXHAUSTED`` fork queued for the lane's wave): the cell
#: reads ``fork`` rather than disappearing, so a forked lane stays visible until
#: the operator resolves it via the FA5 inbox.
FORK_ESCALATION_LABEL: str = "fork"

#: The fork-escalation cell badge shape: the FAILED cross, so an exhausted lane
#: reads as needing attention. The ``(unicode, ascii)`` pair resolves against the
#: active glyph column.
_LANE_FORK_BADGE: tuple[str, str] = sigils._LIFECYCLE[sigils.Sigil.FAILED]

#: Id of the dispatch-result line (below the list); honest about whether the
#: request was issued, accepted, or could not reach the daemon.
DISPATCH_RESULT_ID: str = "autopilot-result"

#: CSS class on each rendered ready-wave row.
FRONTIER_ROW_CLASS: str = "autopilot-row"

#: CSS class on each rendered blocked-wave row (the not-yet-claimable band).
#: A blocked row is read-only -- never a dispatch target, so no selection look.
BLOCKED_ROW_CLASS: str = "autopilot-blocked-row"

#: CSS class flagging the selected ready-wave row (the dispatch target).
SELECTED_ROW_CLASS: str = "-selected"

#: Notice when the wave graph has no claim-ready wave -- phrased so the empty
#: surface is unmistakable, not a quiet "ready to go".
EMPTY_NOTICE: str = "no ready waves"

#: Caption above the ready band -- the claim-ready rows the dispatch affordance
#: can act on.
READY_CAPTION: str = "ready"

#: Caption above the blocked band -- the PENDING rows held off the frontier,
#: each naming the dep blocking it.
BLOCKED_CAPTION: str = "blocked"

#: Marker prefixing a blocked row's blocking dep (e.g. ``<- EUCAP-6``); ASCII
#: so it renders identically in both glyph columns.
BLOCKED_BY_MARKER: str = "<-"

#: Hover / flavour text on the dispatch affordance -- the cockpit's "speak it
#: into being" framing, flavour on the affordance, NOT a relabel of the verb.
DISPATCH_FLAVOUR: str = "speak it into being"

#: Result line before any dispatch has been issued (the idle dispatch surface).
DISPATCH_IDLE: str = "press d to dispatch the selected ready wave"

#: Result line when the dispatch request could not reach the daemon.
DISPATCH_NO_DAEMON: str = "dispatch: daemon unavailable -- request not issued"

#: Result line when there is no ready wave to dispatch.
DISPATCH_NO_TARGET: str = "dispatch: no ready wave to dispatch"

#: Daemon JSON-RPC method the kill / halt keys route through (a placeholder
#: seam); the signal param selects the SIGTERM-grace-SIGKILL ladder entry point.
_KILL_METHOD: str = "agent.kill"

#: SIGKILL-class signal the ``K`` (kill) key sends -- the hard stop.
_SIGNAL_KILL: str = "kill"

#: Graceful SIGTERM signal the ``H`` (halt) key sends -- the soft ladder entry.
_SIGNAL_TERM: str = "term"

#: Result line when a destructive intervention has nothing to act on.
KILL_NO_TARGET: str = "kill: no wave to kill"
HALT_NO_TARGET: str = "halt: no wave to halt"

#: Result line when an intervention request could not reach the daemon.
KILL_NO_DAEMON: str = "kill: daemon unavailable -- request not issued"
HALT_NO_DAEMON: str = "halt: daemon unavailable -- request not issued"

#: Daemon JSON-RPC methods the ``space`` (pause / resume) key routes through.
#: ``agent.pause`` persists ``dispatch_paused = True`` (a deliberate stop that
#: blocks the next claim); ``agent.resume`` clears it. The flag picks which.
#: These are the DEFAULT (no-fleet-run) target; when a fleet run is in flight the
#: ``space`` key instead drives the fleet-run pause / resume (W06).
_PAUSE_RPC: str = "agent.pause"
_RESUME_RPC: str = "agent.resume"

#: Daemon JSON-RPC methods the cockpit fleet controls drive while a fleet run is
#: DRAINING / PAUSED (W06): ``fleet.pause`` holds the running drive loop (it stops
#: claiming while in-flight lanes finish), ``fleet.resume`` continues the SAME
#: loop, and ``fleet.halt`` drains the in-flight lanes to the run-summary card.
#: None of the three aborts the run.
_FLEET_PAUSE_RPC: str = "fleet.pause"
_FLEET_RESUME_RPC: str = "fleet.resume"
_FLEET_HALT_RPC: str = "fleet.halt"

#: Fleet run-state values that mean a live run is in flight (so the cockpit
#: fleet controls route to the ``fleet.*`` RPCs rather than the per-wave / global
#: dispatch-pause fallbacks).
_ACTIVE_RUN_STATES: frozenset[str] = frozenset({"draining", "paused"})

#: Result line when a pause request could not reach the daemon.
PAUSE_NO_DAEMON: str = "pause: daemon unavailable -- request not issued"

#: Result line when a fleet halt request could not reach the daemon.
FLEET_HALT_NO_DAEMON: str = "halt: daemon unavailable -- request not issued"

#: Result line when ``S`` (skip) advances past the last ready wave -- nothing
#: further to step to, so the cursor stays put and the line says so honestly.
SKIP_NO_NEXT: str = "skip: no further ready wave to skip to"

#: Result line when ``S`` (skip) has no ready wave selected to step from.
SKIP_NO_TARGET: str = "skip: no ready wave to skip"

#: Id of the inline multi-select checklist (``m``) hosting the reused
#: ``MultichoiceChecklist`` whose choices are EXACTLY the ready frontier ids.
MULTI_SELECT_ID: str = "autopilot-multiselect"

#: The checklist header prefix -- a plain caption reading as a wave-claim batch.
MULTI_SELECT_PREFIX: str = "claim batch "

#: Result line when ``m`` opens on an empty frontier -- nothing to select.
MULTI_SELECT_NO_TARGET: str = "select: no ready wave to select"

#: Result lines after a committed batch (``Enter``): the staged line precedes a
#: dispatch; the second covers a commit with no wave checked.
MULTI_SELECT_COMMITTED: str = "select: staged"
MULTI_SELECT_EMPTY_COMMIT: str = "select: nothing staged (no wave checked)"

#: Worker group the committed claim batch dispatches under -- one in-flight
#: batch at a time, so a re-commit coalesces rather than stacking workers.
_BATCH_DISPATCH_GROUP: str = "autopilot-claim-batch"

#: Worker group every single-wave daemon round-trip (dispatch / pause / kill)
#: runs under, so the synchronous RPC body never blocks the UI thread (the
#: project's TUI-worker lesson: offload any sync git / subprocess / RPC call or
#: the cockpit hangs while the socket round-trips). ``exclusive`` within the
#: group coalesces a rapid re-press rather than stacking workers.
_INTERVENTION_GROUP: str = "autopilot-intervention"

#: Result line when a committed claim batch is dispatched with no reachable
#: daemon: the fleet issues ZERO RPCs and says so honestly (the exact phrasing
#: the cockpit contract pins).
BATCH_NO_DAEMON: str = "claim batch: daemon unavailable -- not issued"

#: Per-wave outcome verbs in the batch result line: ``spawned`` on accept,
#: ``rejected`` on a daemon rejection (the rest of the fleet still proceeds).
_BATCH_SPAWNED: str = "spawned"
_BATCH_REJECTED: str = "rejected"

#: Result line when ``f`` (fork inbox) is pressed with no queued fork -- the
#: pane has nothing to resolve, so it says so honestly rather than opening an
#: empty inbox card on top of the cockpit.
FORK_INBOX_NO_TARGET: str = "fork inbox: no blocking fork to resolve"

#: Footer hints for the Autopilot pane (full key names, arrows primary). The
#: intervention keys ride after dispatch for discoverability; the mode-switch
#: digits are not advertised here (the footer mode row already lists them).
#: Every label is produced through
#: :func:`~eawf.surfaces.tui.widgets.footer.render_hint_label` so the key tokens
#: stay pinned to the canonical vocabulary.
_AUTOPILOT_HINTS: tuple[str, ...] = (
    render_hint_label("↑↓", "select"),
    render_hint_label("d", "dispatch"),
    render_hint_label("H", "halt"),
    render_hint_label("S", "skip"),
    render_hint_label("K", "kill"),
    render_hint_label("space", "pause"),
    render_hint_label("a", "arm"),
    render_hint_label("w/r/u", "scope"),
    render_hint_label("/", "palette"),
    render_hint_label("?", "help"),
    render_hint_label("q", "quit"),
)


@dataclass(frozen=True)
class ReadyWaveRow:
    """One ready-frontier wave projected for the autopilot list.

    A display projection of a ready
    :class:`~eawf.kernel.spec.auq_bridge.WaveFrontierItem` enriched with the
    wave's title so the row is scannable. Produced only from the computed
    frontier, so every row is a genuinely claim-ready wave in claim order.

    Attributes:
        wave_id: The ready wave id (e.g. ``P29-I04-W12``).
        iter_id: The wave's parent iter id.
        title: The wave's bounded title, or the empty string when the wave
            row carries none (defensive -- every wave has a title in state).
    """

    wave_id: str
    iter_id: str
    title: str


@dataclass(frozen=True)
class BlockedWaveRow:
    """One PENDING wave held off the ready frontier, with its blocking dep.

    A display projection of a PENDING
    :class:`~eawf.kernel.spec.auq_bridge.WaveFrontierItem` that did NOT make
    the ready frontier, enriched with the wave's title and the id of the dep
    holding it off the frontier. The blocking dep is the wave's first
    not-yet-CLOSED dependency; when every dep is CLOSED but a lower-numbered
    ready sibling holds the wave, the blocker is that sibling (the monotonic
    claim-order gate).

    Attributes:
        wave_id: The blocked wave id (e.g. ``P29-I04-W12``).
        iter_id: The wave's parent iter id.
        title: The wave's bounded title, or the empty string when none.
        blocked_by: The id of the wave holding this one off the frontier --
            an open dep, or the lower-numbered ready sibling. Never empty for
            a genuinely blocked row.
    """

    wave_id: str
    iter_id: str
    title: str
    blocked_by: str


@dataclass(frozen=True)
class LaneCellRow:
    """One in-flight (or just-forked) fleet lane projected for the lanes band.

    A display projection of a :class:`~eawf.kernel.state.models.FleetLane` the
    loop is actively draining, enriched with its repair counter so the operator
    reads how many grounded-repair attempts the lane has burned. A lane whose
    wave has exhausted repair and forked carries :attr:`exhausted` so the cell
    escalates to a fork badge (FA5) rather than disappearing.

    Attributes:
        wave_id: The lane's wave id (e.g. ``P29-I04-W12``).
        attempt: The lane's 1-based dispatch attempt -- the ``n`` in the
            ``repair n/<budget>`` counter.
        exhausted: Whether the lane's wave has a queued ``REPAIR_EXHAUSTED``
            fork (its grounded repair budget was spent), so the cell escalates
            to the fork badge.
    """

    wave_id: str
    attempt: int
    exhausted: bool


def build_frontier_items(state: State | None) -> tuple[WaveFrontierItem, ...]:
    """Project the bound state's waves into the frontier-compute view.

    Maps each :class:`~eawf.kernel.state.models.Wave` onto the slim
    :class:`~eawf.kernel.spec.auq_bridge.WaveFrontierItem` the shared
    :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier` reduces (id,
    iter, status, deps). The full wave set is projected because the compute
    needs the CLOSED deps + sibling rows to decide readiness. An unbound or
    wave-less *state* yields an empty tuple (the honest-empty path) so the pane
    never crashes on a scope with no roadmap.

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
    wave title from *state* so the list row is scannable; a wave missing from
    *state* defaults to an empty title rather than raising.

    Args:
        frontier: The computed ready frontier from
            :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier`.
        state: The bound read-only state the titles are read from.

    Returns:
        The ready-wave display rows in claim order; empty when the frontier
        carries no ready wave.
    """
    waves = state.waves if state is not None else {}
    return tuple(
        ReadyWaveRow(
            wave_id=item.wave_id,
            iter_id=item.iter_id,
            title=_wave_title(waves.get(item.wave_id)),
        )
        for item in frontier.ready
    )


def blocked_rows(frontier: DrainableFrontier, state: State | None) -> tuple[BlockedWaveRow, ...]:
    """Project the PENDING waves held off the ready frontier into blocked rows.

    Walks :attr:`DrainableFrontier.by_id` for every PENDING wave NOT on
    :attr:`DrainableFrontier.ready` and names the wave holding it off the
    frontier (:func:`_blocker_of`) -- an open dep, or the lower-numbered ready
    sibling the monotonic claim-order gate prefers. Rows are returned in claim
    order so the blocked band reads top-to-bottom like the ready band; a wave
    with no resolvable blocker is skipped rather than shown empty.

    Args:
        frontier: The computed ready frontier from
            :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier`.
        state: The bound read-only state the titles are read from.

    Returns:
        The blocked-wave display rows in claim order; empty when no PENDING
        wave is blocked.
    """
    waves = state.waves if state is not None else {}
    ready_ids = set(frontier.ready_ids)
    blocked: list[BlockedWaveRow] = []
    for item in frontier.by_id.values():
        if item.status is not WaveStatus.PENDING or item.wave_id in ready_ids:
            continue
        blocker = _blocker_of(item, frontier, ready_ids)
        if blocker is None:
            continue
        blocked.append(
            BlockedWaveRow(
                wave_id=item.wave_id,
                iter_id=item.iter_id,
                title=_wave_title(waves.get(item.wave_id)),
                blocked_by=blocker,
            )
        )
    blocked.sort(key=lambda row: natural_key(row.wave_id))
    return tuple(blocked)


def _blocker_of(
    item: WaveFrontierItem,
    frontier: DrainableFrontier,
    ready_ids: set[str],
) -> str | None:
    """Return the id of the wave holding *item* off the ready frontier.

    The blocker is the wave's first not-yet-CLOSED dependency (the dep gate);
    when every dep is CLOSED the wave is held by the monotonic claim-order gate,
    so the blocker is the nearest lower-numbered sibling under the same iter
    (preferring a ready sibling). Returns ``None`` only when none resolves (a
    defensive path -- a PENDING wave off the frontier always has a blocker).

    Args:
        item: The blocked PENDING wave.
        frontier: The computed frontier carrying the full indexed view.
        ready_ids: The set of ready wave ids (claim-ready right now).

    Returns:
        The blocking wave id, or ``None`` when none resolves.
    """
    for dep_id in item.deps:
        dep = frontier.by_id.get(dep_id)
        if dep is None or dep.status is not WaveStatus.CLOSED:
            return dep_id
    my_key = natural_key(item.wave_id)
    siblings = sorted(
        (
            other
            for other in frontier.by_id.values()
            if other.iter_id == item.iter_id
            and other.wave_id != item.wave_id
            and natural_key(other.wave_id) < my_key
        ),
        key=lambda other: natural_key(other.wave_id),
        reverse=True,
    )
    ready_sibling = next((s.wave_id for s in siblings if s.wave_id in ready_ids), None)
    if ready_sibling is not None:
        return ready_sibling
    return siblings[0].wave_id if siblings else None


def _wave_title(wave: Wave | None) -> str:
    """Return the wave's title, or the empty string when the wave is absent."""
    return wave.title if wave is not None else ""


def render_frontier_header(
    rows: tuple[ReadyWaveRow, ...],
    blocked: tuple[BlockedWaveRow, ...] = (),
    *,
    mode: RenderMode = DEFAULT_RENDER_MODE,
) -> str:
    """Render the frontier header line above the ready/blocked split.

    Leads with the dispatch chrome arrow (:func:`sigils.chrome` ``"dispatch"``).
    When the frontier has ready waves the header reports the ready count (and
    the blocked count, when any wave is held); when nothing is ready it leads
    with the honest-empty banner rather than implying a primed dispatch queue.

    Args:
        rows: The ready-wave display rows (empty when nothing is ready).
        blocked: The blocked-wave display rows (empty when nothing is held).
        mode: The App's resolved render-mode label -- selects the glyph column.

    Returns:
        A content-markup header string.
    """
    arrow = escape_markup(sigils.chrome("dispatch", mode=mode))
    if not rows:
        held = f" [$warn]{len(blocked)} blocked[/]" if blocked else ""
        return f"[$warn]{EMPTY_NOTICE}[/]{held}\n[$muted]no claim-ready wave on the frontier[/]"
    blocked_suffix = f" [$muted]+[/] [$warn]{len(blocked)} blocked[/]" if blocked else ""
    return (
        f"[$accent]{arrow} ready frontier[/] [$ok]{len(rows)}[/] "
        f"[$muted]wave{'' if len(rows) == 1 else 's'} claimable[/]{blocked_suffix}"
    )


def _fork_badge(run: FleetRun, *, mode: RenderMode) -> str:
    """Render the fork badge for *run*, or the empty string when fork-free.

    Trails the vitals header with the FAILED-cross badge + the forked count when
    the run has recorded at least one fork (a failed / re-planned lane, read off
    :attr:`FleetCounters.forked`). A clean drain (zero forks) trails nothing, so
    the badge only appears when it carries signal.

    Args:
        run: The persisted fleet run whose counters back the badge.
        mode: The App's resolved render-mode label -- selects the glyph column.

    Returns:
        A content-markup badge fragment (leading space) when ``forked > 0``,
        else the empty string.
    """
    forked = run.counters.forked
    if forked <= 0:
        return ""
    glyph = escape_markup(_FORK_BADGE[1] if mode == sigils.ASCII_MODE else _FORK_BADGE[0])
    return f" [$err]{glyph} {forked} fork{'' if forked == 1 else 's'}[/]"


def render_cockpit_vitals(run: FleetRun | None, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render the fleet-cockpit vitals header off the persisted *run*.

    Reads the persisted :class:`~eawf.kernel.state.models.FleetRun` (its
    run-state, lanes, frontier queue, and :class:`FleetCounters`) into a single
    header row -- run-state sigil, ``N/M lanes`` occupancy, ``frontier K left``
    queue depth, the EU block-bar with its spend ratio, ``$ used/cap`` spend,
    ``wv/hr`` throughput, and the fork badge -- every figure read STRAIGHT off
    *run* so the cockpit never recomputes a tally in the UI (the FA7 contract:
    surface what the daemon stored). Before the fleet is armed (*run* is ``None``
    or its run-state is ``IDLE``) the honest-empty cockpit hero
    (:data:`COCKPIT_IDLE`) renders instead of a fabricated zeroed vitals row.

    Args:
        run: The persisted fleet run, or ``None`` when no run is armed.
        mode: The App's resolved render-mode label -- selects the glyph column
            for the run-state sigil, the EU bar, and the fork badge.

    Returns:
        A content-markup vitals header string, or the honest-empty cockpit hero
        when no run is armed.
    """
    if run is None or run.run_state.value == "idle":
        return f"[$muted]{COCKPIT_IDLE}[/]"
    sigil = escape_markup(sigils.glyph(_RUN_STATE_SIGILS[run.run_state.value], mode=mode))
    lanes = f"{len(run.lanes)}/{run.concurrency} {COCKPIT_LANES_LABEL}"
    frontier = f"{COCKPIT_FRONTIER_LABEL} {len(run.frontier)} {COCKPIT_FRONTIER_SUFFIX}"
    eu_bar = render_bar_markup(run.counters.spent_eu, run.eu_cap or 0.0, mode=mode)
    spend = f"$ {run.counters.spent_usd:.2f}/{run.usd_cap:.2f}" if run.usd_cap else "$ uncapped"
    runtime = escape_markup(sigils.chrome("runtime", mode=mode))
    throughput = (
        f"{run.throughput:.1f} {COCKPIT_THROUGHPUT_LABEL}"
        if run.throughput
        else f"-- {COCKPIT_THROUGHPUT_LABEL}"
    )
    return (
        f"[$accent]{sigil} {escape_markup(run.run_state.value)}[/] "
        f"[$ok]{lanes}[/]  [$muted]{frontier}[/]  "
        f"EU {eu_bar}  [$muted]{escape_markup(spend)}[/]  "
        f"[$muted]{runtime} {throughput}[/]{_fork_badge(run, mode=mode)}"
    )


def render_section_caption(caption: str, count: int) -> str:
    """Render a ready/blocked band caption naming the band and its row count.

    Args:
        caption: The band caption (:data:`READY_CAPTION` / :data:`BLOCKED_CAPTION`).
        count: The number of rows in the band.

    Returns:
        A content-markup caption string.
    """
    return f"[$muted]{escape_markup(caption)} ({count})[/]"


def render_ready_row(
    row: ReadyWaveRow,
    *,
    selected: bool = False,
    mode: RenderMode = DEFAULT_RENDER_MODE,
) -> str:
    """Render one ready-wave list row with its multi-select affordance look.

    Each ready row leads with a checkbox affordance (:func:`sigils.chrome`
    ``"check_on"`` when selected, ``"check_off"`` otherwise) so the band reads
    as a selectable set; the dispatch chrome arrow marks the row as a dispatch
    target. The ``m`` checklist drives the actual batch dispatch.

    Args:
        row: The ready-wave display row.
        selected: Whether this row is the current dispatch target (drives the
            checkbox affordance's filled / hollow look).
        mode: The App's resolved render-mode label -- selects the glyph column.

    Returns:
        A content-markup row string naming the wave id, its iter, and title.
    """
    box = escape_markup(sigils.chrome("check_on" if selected else "check_off", mode=mode))
    arrow = escape_markup(sigils.chrome("dispatch", mode=mode))
    title_suffix = f" [$muted]{escape_markup(row.title)}[/]" if row.title else ""
    marker = f"[$accent]{box} {arrow}[/]" if selected else f"[$muted]{box} {arrow}[/]"
    return (
        f"{marker} [$accent]{escape_markup(row.wave_id)}[/] "
        f"[$muted]{escape_markup(row.iter_id)}[/]{title_suffix}"
    )


def lane_cells(run: FleetRun | None) -> tuple[LaneCellRow, ...]:
    """Project a fleet run's in-flight + just-forked lanes into cell rows.

    Walks the persisted :attr:`~eawf.kernel.state.models.FleetRun.lanes` (the
    actively-draining slots) and the :attr:`~eawf.kernel.state.models.FleetRun.forks`
    queue (lanes the loop paused to a blocking fork), so a lane that exhausted
    its grounded-repair budget and forked stays visible -- it escalates to a
    fork-badge cell rather than disappearing the moment it leaves ``lanes``. Only
    a ``REPAIR_EXHAUSTED`` fork escalates a cell here; the other fork reasons
    (high-risk close, uncalibrated jury, needs-user split) ride the FA5 inbox
    alone and add no lane cell. Rows are returned in natural claim order. An
    unarmed run (``None``) yields no cells (the honest-empty lanes path).

    Args:
        run: The persisted fleet run, or ``None`` when no run is armed.

    Returns:
        The lane-cell display rows in claim order; empty when no lane is in
        flight or repair-forked.
    """
    if run is None:
        return ()
    from eawf.kernel.state.models import FleetForkReason

    cells: dict[str, LaneCellRow] = {}
    for lane in run.lanes.values():
        cells[lane.wave_id] = LaneCellRow(
            wave_id=lane.wave_id, attempt=lane.attempt, exhausted=False
        )
    for fork in run.forks:
        if fork.reason is not FleetForkReason.REPAIR_EXHAUSTED:
            continue
        cells[fork.wave_id] = LaneCellRow(
            wave_id=fork.wave_id, attempt=fork.attempt, exhausted=True
        )
    ordered = sorted(cells.values(), key=lambda cell: natural_key(cell.wave_id))
    logger.debug(f"lane_cells lanes={len(run.lanes)} cells={len(ordered)}")
    return tuple(ordered)


def render_lane_cell(row: LaneCellRow, *, mode: RenderMode = DEFAULT_RENDER_MODE) -> str:
    """Render one in-flight (or just-forked) lane cell with its repair counter.

    A draining lane reads ``<wave> repair n/<budget>`` so the operator sees how
    many grounded-repair attempts it has burned of the
    :data:`REPAIR_BUDGET` ceiling. A lane whose wave exhausted repair and forked
    escalates to ``<badge> <wave> fork`` (:data:`FORK_ESCALATION_LABEL`) -- it
    reads as needing attention and stays visible until the operator resolves it
    via the FA5 inbox, rather than vanishing the moment it left the lane slot.

    Args:
        row: The lane-cell display row.
        mode: The App's resolved render-mode label -- selects the glyph column
            for the fork-escalation badge.

    Returns:
        A content-markup cell string naming the wave + its repair counter / fork
        escalation.
    """
    wave = escape_markup(row.wave_id)
    if row.exhausted:
        badge = _LANE_FORK_BADGE[1] if mode == sigils.ASCII_MODE else _LANE_FORK_BADGE[0]
        glyph = escape_markup(badge)
        return f"[$err]{glyph} {wave} {FORK_ESCALATION_LABEL}[/]"
    return f"[$accent]{wave}[/] [$muted]{REPAIR_LABEL} {row.attempt}/{REPAIR_BUDGET}[/]"


def render_blocked_row(row: BlockedWaveRow) -> str:
    """Render one blocked-wave list row naming the dep holding it off the frontier.

    A blocked row carries no selection affordance (it is never a dispatch
    target) and instead trails with the ``<- <dep>`` marker
    (:data:`BLOCKED_BY_MARKER`) so the operator reads which wave must close /
    dispatch before this one becomes claim-ready.

    Args:
        row: The blocked-wave display row.

    Returns:
        A content-markup row string naming the wave id, iter, title, and dep.
    """
    title_suffix = f" [$muted]{escape_markup(row.title)}[/]" if row.title else ""
    blocker = f" [$warn]{BLOCKED_BY_MARKER} {escape_markup(row.blocked_by)}[/]"
    return (
        f"[$muted]{escape_markup(row.wave_id)}[/] "
        f"[$muted]{escape_markup(row.iter_id)}[/]{title_suffix}{blocker}"
    )


def _dispatch_idle_line() -> str:
    """Render the idle dispatch surface with the cockpit's flavour hover text.

    The result line before any dispatch has been issued; it pairs the literal
    dispatch instruction with the "speak it into being" flavour
    (:data:`DISPATCH_FLAVOUR`) as muted hover text -- flavour, not a relabel.

    Returns:
        A content-markup idle result line.
    """
    return f"[$muted]{DISPATCH_IDLE}[/] [$muted]({DISPATCH_FLAVOUR})[/]"


def _dispatch_one_wave(wave_id: str) -> str:
    """Issue one ``agent.dispatch`` (``spawn=True``) RPC and return its verb.

    The synchronous per-wave call the batch worker offloads onto a thread:
    opens a :class:`~eawf.surfaces.cli._daemon_client.DaemonClient`, calls
    ``agent.dispatch`` with the wave id + ``spawn=True``, and returns
    :data:`_BATCH_SPAWNED` on success. A daemon that rejects this wave (e.g.
    ``-32602 invalid_params``) or that is unreachable / times out mid-batch
    returns :data:`_BATCH_REJECTED` -- the caller renders that and moves on, so
    one wave's rejection never aborts the rest of the fleet.

    Args:
        wave_id: The claim-ready wave id to dispatch.

    Returns:
        :data:`_BATCH_SPAWNED` when the daemon accepted the spawn, else
        :data:`_BATCH_REJECTED`.
    """
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient(call_timeout_seconds=30.0) as client:
            client.call("agent.dispatch", {"wave_id": wave_id, "spawn": True})
    except DaemonRpcError as exc:
        logger.debug(f"_dispatch_one_wave rejected wave={wave_id} message={exc.message!r}")
        return _BATCH_REJECTED
    except (OSError, RuntimeError, TimeoutError) as exc:
        logger.debug(f"_dispatch_one_wave fallback wave={wave_id} cause={exc!r}")
        return _BATCH_REJECTED
    return _BATCH_SPAWNED


class AutopilotModeScreen(ScopeScreen):
    """Autopilot pane: the ready-wave frontier + dispatch controls.

    Projects the bound read-only state's wave graph into the shared
    :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier` view and lists
    the ready (claim-ready) waves in claim order. Arrows move the selection
    through the list; the ``d`` key asks the daemon to live-spawn the selected
    wave via the ``agent.dispatch`` RPC (``spawn=True``) through the daemon-
    client seam and surfaces the typed result honestly. When no wave is
    claim-ready the pane renders the honest-empty :data:`EMPTY_NOTICE` banner.

    ``m`` opens a multi-select checklist over the frontier; committing the batch
    dispatches each staged wave through ``agent.dispatch`` (``spawn=True``) once
    per wave on a worker, with an unreachable daemon issuing ZERO RPCs and a
    mid-batch rejected wave reading ``rejected`` while the rest proceed. The
    cockpit intervention keys (``K``/``H`` kill / halt, ``space`` pause / resume,
    ``S`` skip, ``a`` arm) route through the same daemon-client seam (or do a
    cheap local thing for ``S``), each surfacing its typed outcome honestly and
    never faking an action that did not happen -- see the module docstring.

    The screen self-binds to the host
    :class:`~eawf.surfaces.tui.app.EaApp` reactive ``state``: it seeds from
    ``app.state`` on mount and rebuilds when a daemon-pushed revision lands, so a
    wave closed after launch surfaces on the frontier without a relaunch.
    """

    DEFAULT_CSS: ClassVar[str] = """
    AutopilotModeScreen #autopilot-body {
        height: 1fr;
        padding: 1 2;
    }
    AutopilotModeScreen #autopilot-cockpit {
        height: auto;
        margin-bottom: 1;
    }
    AutopilotModeScreen #autopilot-header {
        height: auto;
        margin-bottom: 1;
    }
    AutopilotModeScreen #autopilot-list {
        height: 1fr;
        border: round $accent;
    }
    AutopilotModeScreen .autopilot-row {
        height: auto;
        padding: 0 1;
    }
    AutopilotModeScreen .autopilot-row.-selected {
        background: $accent 20%;
    }
    AutopilotModeScreen .autopilot-blocked-row {
        height: auto;
        padding: 0 1;
        color: $muted;
    }
    AutopilotModeScreen .autopilot-lane-cell {
        height: auto;
        padding: 0 1;
    }
    AutopilotModeScreen .autopilot-section {
        height: auto;
        padding: 0 1;
        margin-top: 1;
    }
    AutopilotModeScreen #autopilot-result {
        height: auto;
        margin-top: 1;
        color: $muted;
    }
    AutopilotModeScreen .autopilot-empty {
        HONEST_EMPTY_CSS
    }
    SEAL_HERO_CSS
    """.replace("HONEST_EMPTY_CSS", HONEST_EMPTY_CSS).replace(
        "SEAL_HERO_CSS", seal_hero_css("AutopilotModeScreen")
    )

    #: ``up`` / ``down`` move the selection; ``d`` dispatches; ``m`` opens the
    #: multi-select batch. The intervention keys (``H`` halt, ``S`` skip, ``K``
    #: kill, ``space`` pause/resume, ``a`` arm) ride on top: the uppercase
    #: letters are the brief's canonical intervention keys (distinct from the
    #: app-wide lowercase vim cursor aliases, so no collision). The chrome
    #: bindings come from the shared chassis + app-wide bindings; arrows stay
    #: primary, so the pane offers no j/k aliases here.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "select_prev", "up", show=False),
        Binding("down", "select_next", "down", show=False),
        Binding("d", "dispatch_selected", "dispatch", show=False),
        Binding("H", "halt_selected", "halt", show=False),
        Binding("S", "skip_selected", "skip", show=False),
        Binding("K", "kill_selected", "kill", show=False),
        Binding("space", "toggle_pause", "pause", show=False),
        Binding("a", "arm_flow", "arm", show=False),
        Binding("f", "open_fork_inbox", "forks", show=False),
        Binding("m", "open_multi_select", "select", show=False),
    ]

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _AUTOPILOT_HINTS

    #: Bound state, watched so a fresh revision recomputes the frontier (a
    #: wave closed after launch unblocks its dependents onto the frontier).
    state: reactive[State | None] = reactive(None)

    #: Index of the selected ready-wave row (the dispatch target); clamped to
    #: the ready list, ``0`` when the list is non-empty, ``-1`` when empty.
    selected: reactive[int] = reactive(0, init=False)

    def __init__(self) -> None:
        """Initialise the pane with empty ready / blocked lists until first compute."""
        super().__init__()
        self._rows: tuple[ReadyWaveRow, ...] = ()
        self._blocked: tuple[BlockedWaveRow, ...] = ()
        #: The in-flight (+ just-forked) fleet-lane cells the lanes band renders,
        #: each carrying its ``repair n/<budget>`` counter and fork-escalation
        #: flag. Empty until a fleet run is armed and a lane is in flight.
        self._lanes: tuple[LaneCellRow, ...] = ()
        #: The wave ids the operator last staged through the multi-select shell
        #: (``m`` -> Space-toggle -> Enter). Empty until a batch commits.
        self._claim_batch: tuple[str, ...] = ()
        #: The run-state string of the last fleet run the pane observed, so a
        #: transition INTO the terminal ``done`` state opens the FA7 run-summary
        #: card exactly once (a re-push of an already-DONE run does not reopen it).
        #: ``None`` until the first state seed.
        self._last_run_state: str | None = None
        #: The queued-fork count of the last fleet run the pane observed, so a
        #: RISE in the count (a lane newly forked) auto-raises the FA5 fork inbox
        #: exactly once -- a re-push at the same depth (a live poll re-delivering
        #: the same queue) does NOT re-raise it. ``0`` until the first state seed.
        self._last_fork_count: int = 0

    def compose_body(self) -> ComposeResult:
        """Yield the cockpit vitals, the frontier header, the list, and the result.

        The vitals header leads (the run-state sigil + lanes / frontier / EU / $
        / throughput / fork vitals when armed, or the honest-empty cockpit hero
        when not); the frontier header follows with the dispatch chrome arrow;
        the list container starts empty and :meth:`on_mount` populates it through
        :meth:`_render_rows` (single-sourced); the result line carries the idle
        dispatch surface until ``d`` is pressed.
        """
        self._rows = self._current_rows()
        self._blocked = self._current_blocked()
        self._lanes = self._current_lanes()
        mode = self._render_mode()
        with Vertical(id="autopilot-body"):
            yield Static(
                render_cockpit_vitals(self._current_fleet_run(), mode=mode),
                id=COCKPIT_VITALS_ID,
            )
            yield Static(
                render_frontier_header(self._rows, self._blocked, mode=mode),
                id=FRONTIER_HEADER_ID,
            )
            yield VerticalScroll(id=FRONTIER_LIST_ID)
            yield Static(_dispatch_idle_line(), id=DISPATCH_RESULT_ID)

    def on_mount(self) -> None:
        """Seed from app state, arm the rebuild seams, and render the frontier."""
        super().on_mount()
        app_state = getattr(self.app, "state", None)
        if app_state is not None and self.state is None:
            self.state = app_state
        if hasattr(self.app, "state"):
            self.watch(self.app, "state", self._on_app_state)
        if hasattr(self.app, "render_mode"):
            self.watch(self.app, "render_mode", self._on_render_mode)
        self._rebuild()

    def _on_app_state(self, new_state: State | None) -> None:
        """Mirror an app-level state change onto this screen's reactive."""
        self.state = new_state

    def _on_render_mode(self, _mode: RenderMode) -> None:
        """Repaint the reskinned glyphs when the App's render mode swaps."""
        if self.is_mounted:
            self._rebuild()

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
        """Ask the daemon to live-spawn the selected ready wave (off the UI thread).

        Issues ``agent.dispatch`` (``spawn=True``) for the selected ready wave
        through the daemon-client seam on a Textual worker so the synchronous RPC
        never blocks the event loop, then repaints the result line. With no ready
        wave there is nothing to dispatch; an unreachable daemon (checked up
        front, before any worker) says so rather than implying a spawn happened.
        """
        target = self._selected_row()
        if target is None:
            self._set_result(f"[$warn]{DISPATCH_NO_TARGET}[/]")
            return
        if not self._daemon_available():
            self._set_result(f"[$warn]{DISPATCH_NO_DAEMON}[/]")
            logger.info(f"action_dispatch_selected no_daemon wave={target.wave_id}")
            return
        self.run_worker(
            self._dispatch_worker(target),
            group=_INTERVENTION_GROUP,
            exclusive=True,
        )

    async def _dispatch_worker(self, target: ReadyWaveRow) -> None:
        """Worker body: issue the dispatch RPC off-thread, then repaint the result.

        Runs the synchronous :meth:`_issue_dispatch` through
        :func:`asyncio.to_thread` so the daemon round-trip never blocks the event
        loop, then sets the honest result line after the await (loop-safe).

        Args:
            target: The selected ready wave to dispatch.
        """
        result_line = await asyncio.to_thread(self._issue_dispatch, target)
        self._set_result(result_line)
        logger.info(f"action_dispatch_selected wave={target.wave_id} result={result_line!r}")

    def _issue_dispatch(self, target: ReadyWaveRow) -> str:
        """Issue the ``agent.dispatch`` RPC for *target* and return a result line.

        Calls ``agent.dispatch`` (``spawn=True``) through the
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam; the line
        reports the captured pid + runtime, or the honest unavailable / rejected
        line rather than a faked dispatch. The blocking call runs on a worker
        thread (:meth:`_dispatch_worker`), never the UI thread.

        Args:
            target: The selected ready wave to dispatch.

        Returns:
            A content-markup result line describing the dispatch outcome.
        """
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

    def action_kill_selected(self) -> None:
        """Kill the selected wave's spawned child (destructive -- confirm-gated).

        Confirms via a
        :class:`~eawf.surfaces.tui.screens.overlays.confirm.ConfirmModal`, then
        issues the real ``agent.kill`` RPC with a SIGKILL-class signal for the
        selected wave + attempt and surfaces the typed (today: placeholder
        ``killed=false``) outcome. With no selected wave there is nothing to
        kill, surfaced honestly without opening the modal.
        """
        target = self._selected_row()
        if target is None:
            self._set_result(f"[$warn]{KILL_NO_TARGET}[/]")
            return
        self._confirm_then_kill(
            target,
            prompt=f"Kill {target.wave_id}? SIGKILL the process group.",
            signal=_SIGNAL_KILL,
            verb="kill",
            no_daemon=KILL_NO_DAEMON,
        )

    def action_halt_selected(self) -> None:
        """Halt the fleet run, or the selected wave when no run is in flight (``H``).

        While a fleet run is in flight (DRAINING / PAUSED) ``H`` drives the FLEET
        halt (W06): ``fleet.halt`` blocks new claims, lets the in-flight lanes
        finish, then drains the run to the run-summary card -- it does NOT abort
        the run or reap live work. With no live fleet run it falls back to the
        per-wave graceful halt (``agent.kill`` SIGTERM on the selected wave). The
        fleet halt runs off the UI thread; an unreachable daemon (checked up
        front) surfaces the honest unavailable line.
        """
        if self._active_run_state() is not None:
            if not self._daemon_available():
                self._set_result(f"[$warn]{FLEET_HALT_NO_DAEMON}[/]")
                logger.info("action_halt_selected fleet no_daemon")
                return
            self.run_worker(
                self._fleet_halt_worker(),
                group=_INTERVENTION_GROUP,
                exclusive=True,
            )
            return
        target = self._selected_row()
        if target is None:
            self._set_result(f"[$warn]{HALT_NO_TARGET}[/]")
            return
        self._confirm_then_kill(
            target,
            prompt=f"Halt {target.wave_id}? graceful stop (SIGTERM ladder).",
            signal=_SIGNAL_TERM,
            verb="halt",
            no_daemon=HALT_NO_DAEMON,
        )

    async def _fleet_halt_worker(self) -> None:
        """Worker body: drive the fleet halt off-thread, then repaint the result.

        Runs the synchronous :meth:`_issue_fleet_halt` through
        :func:`asyncio.to_thread` so the daemon round-trip never blocks the event
        loop, then sets the honest result line after the await (loop-safe).
        """
        result_line = await asyncio.to_thread(self._issue_fleet_halt)
        self._set_result(result_line)
        logger.info(f"action_halt_selected fleet result={result_line!r}")

    def _issue_fleet_halt(self) -> str:
        """Drive the ``fleet.halt`` RPC and return a cockpit result line (W06).

        Calls ``fleet.halt`` through the
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam; the daemon
        blocks new claims, lets the in-flight lanes finish, and drains the run to
        the summary card. The line reports the halted verdict, or the honest
        unavailable / rejected line rather than a faked halt.

        Returns:
            A content-markup result line describing the fleet-halt outcome.
        """
        unavailable = f"[$warn]{FLEET_HALT_NO_DAEMON}[/]"
        if not self._daemon_available():
            return unavailable
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                client.call(_FLEET_HALT_RPC, {})
        except DaemonRpcError as exc:
            logger.debug(f"_issue_fleet_halt daemon_rejected message={exc.message!r}")
            return (
                f"[$warn]halt: daemon rejected request[/] [$muted]{escape_markup(exc.message)}[/]"
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_fleet_halt daemon_fallback cause={exc!r}")
            return unavailable
        return "[$ok]halt: fleet draining to summary[/]"

    def action_skip_selected(self) -> None:
        """Skip the selected ready wave by advancing the selection (local).

        A real, cheap, local frontier operation: it steps the selection past
        the currently-selected ready wave to the next one in claim order rather
        than firing a doomed daemon round-trip (no lightweight "skip this ready
        wave" transition exists). With no ready wave there is nothing to skip,
        and when the selection is already on the last ready wave there is
        nothing further to step to -- each surfaced honestly without moving the
        cursor.
        """
        if self._selected_row() is None:
            self._set_result(f"[$warn]{SKIP_NO_TARGET}[/]")
            return
        if self.selected >= len(self._rows) - 1:
            self._set_result(f"[$warn]{SKIP_NO_NEXT}[/]")
            return
        self.selected += 1
        skipped_to = self._rows[self.selected]
        result_line = f"[$ok]skip: now on[/] [$muted]{escape_markup(skipped_to.wave_id)}[/]"
        self._set_result(result_line)
        logger.info(f"action_skip_selected now={skipped_to.wave_id} selected={self.selected}")

    def action_toggle_pause(self) -> None:
        """Pause / resume dispatch through the real daemon RPC (off the UI thread).

        Reads the current
        :attr:`~eawf.kernel.state.models.State.dispatch_paused` flag and issues
        ``agent.resume`` when already paused, else ``agent.pause`` -- a
        deliberate operator stop the daemon persists and
        :func:`eawf.workflow.lifecycle.wave.claim_wave` reads to block the next
        claim. Pause is non-destructive. The blocking RPC runs on a Textual
        worker so the toggle never blocks the event loop; an unreachable daemon
        (checked up front) surfaces the honest unavailable line instead.
        """
        if not self._daemon_available():
            self._set_result(f"[$warn]{PAUSE_NO_DAEMON}[/]")
            logger.info("action_toggle_pause no_daemon")
            return
        self.run_worker(
            self._pause_worker(),
            group=_INTERVENTION_GROUP,
            exclusive=True,
        )

    async def _pause_worker(self) -> None:
        """Worker body: toggle dispatch pause off-thread, then repaint the result.

        Runs the synchronous :meth:`_issue_pause` through
        :func:`asyncio.to_thread` so the daemon round-trip never blocks the event
        loop, then sets the honest result line after the await (loop-safe).
        """
        result_line = await asyncio.to_thread(self._issue_pause)
        self._set_result(result_line)
        logger.info(f"action_toggle_pause result={result_line!r}")

    def _currently_paused(self) -> bool:
        """Return the bound state's ``dispatch_paused`` flag (``False`` if unbound).

        The pause toggle reads the current flag to pick which RPC to issue; an
        unbound state reads as not-paused so the first ``space`` issues
        ``agent.pause``.
        """
        state = self._current_state()
        return bool(state.dispatch_paused) if state is not None else False

    def _active_run_state(self) -> str | None:
        """Return the bound fleet run's state when a live run is in flight (W06).

        The ``space`` (pause / resume) + ``H`` (halt) keys route to the
        ``fleet.*`` control RPCs only while a fleet run is DRAINING / PAUSED; an
        idle / done / unarmed run keeps the per-wave / global dispatch-pause
        fallbacks. A bound run whose state is not active reads as ``None``.

        Returns:
            The active fleet run-state value (``"draining"`` / ``"paused"``), or
            ``None`` when no live run is in flight.
        """
        run = self._current_fleet_run()
        if run is None:
            return None
        value = run.run_state.value
        return value if value in _ACTIVE_RUN_STATES else None

    def _issue_pause(self) -> str:
        """Toggle pause via the real RPC and return a result line.

        While a fleet run is in flight (DRAINING / PAUSED) the ``space`` key drives
        the FLEET pause / resume (W06): ``fleet.pause`` holds the running drive
        loop (it stops claiming while the in-flight lanes finish) and
        ``fleet.resume`` continues the SAME loop -- neither aborts the run. With no
        live fleet run it falls back to the global ``agent.pause`` / ``agent.resume``
        dispatch-pause toggle. The call routes through the
        :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam when
        available; the line reports the persisted verdict, or the honest
        unavailable line rather than a faked toggle.

        Returns:
            A content-markup result line describing the pause / resume outcome.
        """
        unavailable = f"[$warn]{PAUSE_NO_DAEMON}[/]"
        if not self._daemon_available():
            return unavailable
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        run_state = self._active_run_state()
        if run_state is not None:
            # A live fleet run: drive the fleet pause / resume (W06). A PAUSED run
            # resumes; a DRAINING run pauses.
            method = _FLEET_RESUME_RPC if run_state == "paused" else _FLEET_PAUSE_RPC
            verb = "resumed" if run_state == "paused" else "paused"
        else:
            paused = self._currently_paused()
            method = _RESUME_RPC if paused else _PAUSE_RPC
            verb = None
        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                result = client.call(method, {})
        except DaemonRpcError as exc:
            logger.debug(f"_issue_pause daemon_rejected method={method!r} message={exc.message!r}")
            return (
                f"[$warn]pause: daemon rejected request[/] [$muted]{escape_markup(exc.message)}[/]"
            )
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_pause daemon_fallback method={method!r} cause={exc!r}")
            return unavailable
        if verb is None:
            # The global dispatch-pause path returns the persisted flag.
            now_paused = bool(result.get("paused", method == _PAUSE_RPC))
            verb = "paused" if now_paused else "resumed"
        return f"[$ok]pause: {verb}[/]"

    def action_arm_flow(self) -> None:
        """Open the FA1 arm / launch-flow overlay over the ready frontier.

        Mounts the
        :class:`~eawf.surfaces.tui.screens.overlays.arm.ArmModal` launch form
        (scope / budget / concurrency / risk policy / convergence). The overlay
        is told whether the frontier is dry (from the computed ready rows) so it
        can surface the honest "nothing to drain" banner over an empty frontier;
        :meth:`_on_arm_dismissed` handles its typed dismiss value.
        """
        from eawf.surfaces.tui.screens.overlays.arm import ArmModal

        frontier_empty = not self._rows
        self._push_overlay(ArmModal(frontier_empty=frontier_empty), self._on_arm_dismissed)
        logger.info(f"action_arm_flow opened frontier_empty={frontier_empty}")

    def _on_arm_dismissed(self, spec: ArmSpec | None) -> None:
        """Arm the fleet drive from the overlay's typed spec, or surface a cancel.

        A ``None`` spec (``Esc`` cancel or a dry-frontier refusal) arms nothing
        and says so honestly; a returned :class:`ArmSpec` folds the ready
        frontier + the spec into the ``fleet.drive`` RPC (via
        :func:`~eawf.surfaces.tui.screens.overlays.arm.issue_drive`), flipping
        the cockpit to ``DRAINING``.

        Args:
            spec: The overlay's typed launch config, or ``None`` on cancel.
        """
        from eawf.surfaces.tui.screens.overlays.arm import ARM_CANCELLED, issue_drive

        if spec is None:
            self._set_result(f"[$muted]{ARM_CANCELLED}[/]")
            logger.info("_on_arm_dismissed cancelled")
            return
        frontier = [row.wave_id for row in self._rows]
        result_line = issue_drive(spec, frontier, daemon_available=self._daemon_available())
        self._set_result(result_line)
        logger.info(f"_on_arm_dismissed armed result={result_line!r}")

    def action_open_multi_select(self) -> None:
        """Open the multi-select wave-claim shell over the ready frontier (``m``).

        Mounts the reused
        :class:`~eawf.surfaces.tui.screens.overlays.multichoice_checklist.MultichoiceChecklist`
        whose choices are EXACTLY the ready frontier wave ids (single-sourced
        from :attr:`_rows`), so a non-ready wave is never a selectable choice.
        Inside the checklist ``Space`` toggles ``[X]`` membership and ``Enter``
        commits the batch. With no ready wave there is nothing to select,
        surfaced honestly without mounting an empty checklist; a no-op when one
        is already open.
        """
        if self.query(f"#{MULTI_SELECT_ID}"):
            return
        choices = tuple(row.wave_id for row in self._rows)
        if not choices:
            self._set_result(f"[$warn]{MULTI_SELECT_NO_TARGET}[/]")
            return
        from eawf.surfaces.tui.screens.overlays.multichoice_checklist import MultichoiceChecklist

        checklist = MultichoiceChecklist(
            choices=choices,
            selected=list(self._claim_batch),
            prefix=MULTI_SELECT_PREFIX,
            id=MULTI_SELECT_ID,
        )
        listing = self.query(f"#{FRONTIER_LIST_ID}")
        if not listing:
            return
        listing.first(VerticalScroll).mount(checklist)
        logger.info(f"action_open_multi_select choices={len(choices)}")

    def on_multichoice_checklist_committed(self, message: MultichoiceChecklist.Committed) -> None:
        """Stage the committed wave-claim batch and dispatch it (``Enter``).

        Records the selected wave ids as the staged claim batch, tears the
        checklist down, and -- when at least one wave is staged -- kicks the
        fleet dispatch off the UI thread (:meth:`_dispatch_claim_batch`). The
        selected list is single-sourced from the ready frontier choices. A
        commit with nothing checked surfaces the honest empty-commit line and
        issues no dispatch.
        """
        message.stop()
        self._claim_batch = tuple(message.selected)
        self._teardown_multi_select()
        logger.info(f"multi_select_committed count={len(self._claim_batch)}")
        if not self._claim_batch:
            self._set_result(f"[$warn]{MULTI_SELECT_EMPTY_COMMIT}[/]")
            return
        self._dispatch_claim_batch(self._claim_batch)

    def _dispatch_claim_batch(self, wave_ids: tuple[str, ...]) -> None:
        """Dispatch each staged wave through ``agent.dispatch`` off the UI thread.

        Checks daemon reachability ONCE up front: an unreachable daemon issues
        ZERO RPCs and surfaces the honest :data:`BATCH_NO_DAEMON` line rather
        than faking a fleet spawn. With a reachable daemon it kicks the batch
        onto a Textual worker (:meth:`_batch_dispatch_worker`) so the per-wave
        calls never block the event loop; ``exclusive`` within
        :data:`_BATCH_DISPATCH_GROUP` coalesces a re-commit rather than stacking.

        Args:
            wave_ids: The staged claim-ready wave ids to dispatch, in order.
        """
        if not self._daemon_available():
            self._set_result(f"[$warn]{BATCH_NO_DAEMON}[/]")
            logger.info(f"_dispatch_claim_batch no_daemon waves={len(wave_ids)} issued=0")
            return
        self.run_worker(
            self._batch_dispatch_worker(wave_ids),
            group=_BATCH_DISPATCH_GROUP,
            exclusive=True,
        )

    async def _batch_dispatch_worker(self, wave_ids: tuple[str, ...]) -> None:
        """Worker body: dispatch each staged wave, then repaint the batch result.

        Issues ``agent.dispatch`` (``spawn=True``) once per wave in *wave_ids*,
        each call wrapped in :func:`asyncio.to_thread` so the synchronous RPC
        never blocks the event loop. A wave the daemon rejects (e.g.
        ``-32602 invalid_params``) reads ``rejected`` while the remaining waves
        still dispatch -- one rejection never aborts the fleet. The aggregated
        per-wave verdicts repaint the result line after the awaits (loop-safe).

        Args:
            wave_ids: The staged claim-ready wave ids to dispatch, in order.
        """
        outcomes: list[str] = []
        for wave_id in wave_ids:
            verb = await asyncio.to_thread(_dispatch_one_wave, wave_id)
            colour = "$ok" if verb == _BATCH_SPAWNED else "$warn"
            outcomes.append(f"[{colour}]{escape_markup(wave_id)} {verb}[/]")
        self._set_result(f"[$accent]claim batch[/] {' '.join(outcomes)}")
        logger.info(f"_batch_dispatch_worker issued={len(wave_ids)}")

    def on_multichoice_checklist_cancelled(self, message: MultichoiceChecklist.Cancelled) -> None:
        """Abort the multi-select shell without staging (``Esc`` in the checklist)."""
        message.stop()
        self._teardown_multi_select()
        logger.debug("multi_select_cancelled")

    def _teardown_multi_select(self) -> None:
        """Remove the mounted multi-select checklist, if present."""
        for widget in self.query(f"#{MULTI_SELECT_ID}"):
            widget.remove()

    def _confirm_then_kill(
        self,
        target: ReadyWaveRow,
        *,
        prompt: str,
        signal: str,
        verb: str,
        no_daemon: str,
    ) -> None:
        """Gate a destructive kill / halt behind a confirm, then issue it.

        Pushes a :class:`~eawf.surfaces.tui.screens.overlays.confirm.ConfirmModal`
        carrying *prompt*; the callback issues the real ``agent.kill`` RPC for
        *target* with *signal* only when the operator confirms (``True``), so a
        dismissed / cancelled modal issues no RPC. The confirm is always shown
        for the destructive keys (no ``ui.confirm_destructive`` knob exists yet).

        Args:
            target: The selected ready wave to act on.
            prompt: The confirm prompt naming the destructive action.
            signal: The ``agent.kill`` signal (SIGKILL-class for kill, SIGTERM
                for halt).
            verb: The action verb (``"kill"`` / ``"halt"``) for the result line.
            no_daemon: The honest line shown when the daemon is unreachable.
        """
        from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal

        def _on_confirm(confirmed: bool | None) -> None:
            if not confirmed:
                logger.debug(f"_confirm_then_kill cancelled verb={verb} wave={target.wave_id}")
                return
            if not self._daemon_available():
                self._set_result(f"[$warn]{no_daemon}[/]")
                logger.info(f"_confirm_then_kill no_daemon verb={verb} wave={target.wave_id}")
                return
            self.run_worker(
                self._kill_worker(target, signal=signal, verb=verb, no_daemon=no_daemon),
                group=_INTERVENTION_GROUP,
                exclusive=True,
            )

        self._push_overlay(ConfirmModal(prompt), _on_confirm)

    async def _kill_worker(
        self,
        target: ReadyWaveRow,
        *,
        signal: str,
        verb: str,
        no_daemon: str,
    ) -> None:
        """Worker body: issue the kill / halt RPC off-thread, then repaint the result.

        Runs the synchronous :meth:`_issue_kill` through
        :func:`asyncio.to_thread` so the daemon round-trip never blocks the event
        loop, then sets the honest result line after the await (loop-safe).

        Args:
            target: The selected ready wave to act on.
            signal: The ``agent.kill`` signal (SIGKILL-class for kill, SIGTERM
                for halt).
            verb: The action verb (``"kill"`` / ``"halt"``) for the result line.
            no_daemon: The honest line shown when the daemon is unreachable.
        """
        result_line = await asyncio.to_thread(
            self._issue_kill, target, signal=signal, verb=verb, no_daemon=no_daemon
        )
        self._set_result(result_line)
        logger.info(
            f"_confirm_then_kill verb={verb} wave={target.wave_id} "
            f"signal={signal!r} result={result_line!r}"
        )

    def _push_overlay(self, modal: ModalScreen[Any], callback: Callable[[Any], None]) -> None:
        """Push an overlay *modal* with *callback*, cap-aware when possible.

        The shared overlay-push seam for this pane (the destructive
        :class:`~eawf.surfaces.tui.screens.overlays.confirm.ConfirmModal` and the
        FA1 :class:`~eawf.surfaces.tui.screens.overlays.arm.ArmModal`): routes
        through the App's depth-capped ``push_modal`` when exposed, falling back
        to a plain ``push_screen`` under a bare harness so the push never raises.

        Args:
            modal: The overlay screen to push.
            callback: Invoked with the modal's dismiss value when it closes.
        """
        push_modal = getattr(self.app, "push_modal", None)
        if callable(push_modal):
            push_modal(modal, callback=callback)
            return
        self.app.push_screen(modal, callback)

    def _issue_kill(self, target: ReadyWaveRow, *, signal: str, verb: str, no_daemon: str) -> str:
        """Issue the ``agent.kill`` RPC for *target* and return a result line.

        Calls ``agent.kill`` with *target*'s wave + resolved attempt + *signal*
        through the :class:`~eawf.surfaces.cli._daemon_client.DaemonClient` seam
        when a daemon socket is available; the line reports the typed ``killed``
        verdict + delivered signal. An unreachable, rejecting, or timing-out
        daemon yields the honest unavailable / rejected line. ``agent.kill`` is
        still a placeholder returning ``killed=false``, so the not-killed verdict
        is reported honestly (it goes live once the kill wave lands).

        Args:
            target: The selected ready wave to act on.
            signal: The ``agent.kill`` signal to deliver.
            verb: The action verb (``"kill"`` / ``"halt"``) for the result line.
            no_daemon: The honest line shown when the daemon is unreachable.

        Returns:
            A content-markup result line describing the kill / halt outcome.
        """
        if not self._daemon_available():
            return f"[$warn]{no_daemon}[/]"
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        attempt = self._latest_attempt(target.wave_id)
        try:
            with DaemonClient(call_timeout_seconds=1.0) as client:
                result = client.call(
                    _KILL_METHOD,
                    {"wave_id": target.wave_id, "attempt": attempt, "signal": signal},
                )
        except DaemonRpcError as exc:
            logger.debug(f"_issue_kill daemon_rejected verb={verb} message={exc.message!r}")
            return f"[$warn]{verb}: daemon rejected request[/]"
        except (OSError, RuntimeError, TimeoutError) as exc:
            logger.debug(f"_issue_kill daemon_fallback verb={verb} cause={exc!r}")
            return f"[$warn]{no_daemon}[/]"
        killed = bool(result.get("killed"))
        delivered = str(result.get("signal", signal))
        if killed:
            return f"[$ok]{verb}: killed[/] [$muted]signal={escape_markup(delivered)}[/]"
        # ``agent.kill`` is still a placeholder returning killed=false; report
        # the daemon's verdict honestly.
        return (
            f"[$warn]{verb}: not killed[/] [$muted]signal={escape_markup(delivered)} "
            "(daemon kill not yet live)[/]"
        )

    def _latest_attempt(self, wave_id: str) -> int:
        """Return the highest recorded attempt for *wave_id*, defaulting to ``1``.

        The kill / halt path needs a wave attempt number for the ``agent.kill``
        params. A ready (PENDING) wave has no session table yet, so this
        defaults to ``1``; an already-dispatched wave resolves its highest
        recorded attempt.

        Args:
            wave_id: The wave whose attempt number is resolved.

        Returns:
            The highest recorded attempt, or ``1`` when none is recorded.
        """
        state = self._current_state()
        wave = state.waves.get(wave_id) if state is not None else None
        if wave is None or not wave.sessions:
            return 1
        return max(wave.sessions)

    def _rebuild(self) -> None:
        """Recompute the ready/blocked split from state and repaint the list.

        Recomputes the ready + blocked rows via
        :func:`~eawf.kernel.spec.auq_bridge.compute_ready_frontier`, clamps the
        selection into the new ready list, and remounts the split. An emptied
        frontier repaints the honest-empty notice; blocked rows still render
        below it so the operator reads what is waiting.
        """
        self._rows = self._current_rows()
        self._blocked = self._current_blocked()
        self._lanes = self._current_lanes()
        self._clamp_selection()
        self._render_rows()
        mode = self._render_mode()
        run = self._current_fleet_run()
        cockpit = self.query(f"#{COCKPIT_VITALS_ID}")
        if cockpit:
            cockpit.first(Static).update(render_cockpit_vitals(run, mode=mode))
        header = self.query(f"#{FRONTIER_HEADER_ID}")
        if header:
            header.first(Static).update(
                render_frontier_header(self._rows, self._blocked, mode=mode)
            )
        self._maybe_open_run_summary(run)
        self._maybe_open_fork_inbox(run)
        logger.info(
            f"autopilot_rebuild ready={len(self._rows)} blocked={len(self._blocked)} "
            f"lanes={len(self._lanes)} selected={self.selected}"
        )

    def _maybe_open_run_summary(self, run: FleetRun | None) -> None:
        """Open the FA7 run-summary card when the fleet run reaches a terminal stop.

        Watches the persisted :attr:`~eawf.kernel.state.models.FleetRun.run_state`
        across rebuilds: a transition INTO the terminal ``done`` state (the
        daemon stamped the run terminal) opens the
        :class:`~eawf.surfaces.tui.screens.overlays.run_summary.RunSummaryModal`
        over the cockpit exactly once. A re-push of an already-DONE run (the
        last-seen state was already terminal) does NOT reopen the card, so a live
        state poll that re-delivers the terminal run is idempotent. The card is
        passed the persisted run so every figure is read off the terminal record.

        Args:
            run: The persisted fleet run for this rebuild, or ``None`` when no
                run is armed.
        """
        previous = self._last_run_state
        current = run.run_state.value if run is not None else None
        self._last_run_state = current
        if current != _TERMINAL_RUN_STATE or previous == _TERMINAL_RUN_STATE:
            return
        if run is None:  # pragma: no cover - current==done implies run is not None
            return
        from eawf.surfaces.tui.screens.overlays.run_summary import RunSummaryModal

        reason = run.terminal_reason.value if run.terminal_reason else "unknown"
        logger.info(f"autopilot_run_summary opened terminal_reason={reason!r}")
        self._push_overlay(RunSummaryModal(run), self._on_run_summary_dismissed)

    def _on_run_summary_dismissed(self, _result: None) -> None:
        """Return to the cockpit after the run-summary card dismisses (no-op debrief)."""
        logger.debug("autopilot_run_summary dismissed")

    def _maybe_open_fork_inbox(self, run: FleetRun | None) -> None:
        """Auto-raise the FA5 fork inbox when a lane newly forks (DL-6).

        Watches the queued-fork depth (:attr:`~eawf.kernel.state.models.FleetRun.forks`)
        across rebuilds: a RISE in the count (the daemon paused a fresh lane to a
        blocking fork) auto-raises the
        :class:`~eawf.surfaces.tui.screens.overlays.fork_inbox.ForkInboxModal`
        over the cockpit so the operator-as-decider sees the interrupt. A re-push
        at the same depth (a live poll re-delivering the same queue) does NOT
        re-raise it, so the auto-raise fires once per new fork batch. The inbox is
        passed the persisted fork queue so every card reads off the daemon record.

        Args:
            run: The persisted fleet run for this rebuild, or ``None`` when no
                run is armed.
        """
        previous = self._last_fork_count
        forks = tuple(run.forks) if run is not None else ()
        self._last_fork_count = len(forks)
        if len(forks) <= previous or not forks:
            return
        logger.info(f"autopilot_fork_inbox auto-raised queued={len(forks)}")
        self._open_fork_inbox(forks)

    def action_open_fork_inbox(self) -> None:
        """Open the FA5 fork inbox over the cockpit (``f``).

        Reads the persisted fork queue
        (:attr:`~eawf.kernel.state.models.FleetRun.forks`) and opens the
        :class:`~eawf.surfaces.tui.screens.overlays.fork_inbox.ForkInboxModal`
        over it. With no queued fork there is nothing to resolve, surfaced
        honestly without opening an empty inbox card.
        """
        run = self._current_fleet_run()
        forks = tuple(run.forks) if run is not None else ()
        if not forks:
            self._set_result(f"[$warn]{FORK_INBOX_NO_TARGET}[/]")
            logger.info("action_open_fork_inbox no_target")
            return
        self._open_fork_inbox(forks)

    def _open_fork_inbox(self, forks: tuple[FleetFork, ...]) -> None:
        """Push the fork-inbox overlay over *forks* (the shared open seam).

        Shared by the ``f`` key and the auto-raise so both route through one
        push; the overlay reads the queued forks and routes each resolution to
        the ``fleet.resolve_fork`` RPC itself.

        Args:
            forks: The queued forks the inbox debriefs, in queue order.
        """
        from eawf.surfaces.tui.screens.overlays.fork_inbox import ForkInboxModal

        self._push_overlay(ForkInboxModal(forks), self._on_fork_inbox_dismissed)
        logger.info(f"autopilot_fork_inbox opened queued={len(forks)}")

    def _on_fork_inbox_dismissed(self, _result: None) -> None:
        """Return to the cockpit after the fork inbox dismisses (no-op debrief)."""
        logger.debug("autopilot_fork_inbox dismissed")

    def _render_rows(self) -> None:
        """Mount the ready/blocked split (or the honest-empty notice).

        Clears the list container, then mounts the ready band (caption + one
        selectable row per claim-ready wave, or the honest-empty notice) and the
        blocked band (caption + one row per held wave naming its blocking dep).
        Shared by the on-mount seed and every rebuild (single-sourced).
        """
        listing = self.query(f"#{FRONTIER_LIST_ID}")
        if not listing:
            return
        container = listing.first(VerticalScroll)
        container.remove_children()
        mode = self._render_mode()
        # The section captions (lanes / ready / blocked) and the honest-empty
        # notice carry NO fixed id on a (re)mount: Textual defers the
        # remove_children() above, so re-using a fixed id would race a DuplicateIds
        # when a rapid second rebuild (a live state push landing before the first
        # remove flushed) re-mounts a caption whose old copy has not yet been torn
        # down. The autopilot-section / autopilot-empty class is enough for
        # styling + the test probes; nothing queries these captions by id.
        if self._lanes:
            container.mount(
                Static(
                    render_section_caption(LANES_CAPTION, len(self._lanes)),
                    classes="autopilot-section",
                )
            )
            for lane_cell in self._lanes:
                container.mount(
                    Static(render_lane_cell(lane_cell, mode=mode), classes=LANE_CELL_CLASS)
                )
        if not self._rows:
            # Unicode path leads the dry-frontier hero with the centered
            # ASCII-art Seal (the research-board brand-mark pattern); the body
            # drops its glyph sigil so the art is the single brand mark. ASCII
            # path keeps the small brand glyph (the half-block art needs
            # block-glyph coverage).
            body = Static(
                self._frontier_empty_hero(mode=mode, with_sigil=mode != "unicode"),
                classes="autopilot-empty",
            )
            # hero_id=None: the list re-mounts on every rebuild and Textual
            # defers remove_children(), so a fixed id would collide with the
            # not-yet-torn-down prior hero (the same DuplicateIds race the
            # section captions above dodge by carrying no id). The hero class
            # carries the centering.
            container.mount(seal_empty_hero(body, hero_id=None) if mode == "unicode" else body)
        else:
            container.mount(
                Static(
                    render_section_caption(READY_CAPTION, len(self._rows)),
                    classes="autopilot-section",
                )
            )
            for index, row in enumerate(self._rows):
                selected = index == self.selected
                classes = (
                    f"{FRONTIER_ROW_CLASS} {SELECTED_ROW_CLASS}" if selected else FRONTIER_ROW_CLASS
                )
                container.mount(
                    Static(render_ready_row(row, selected=selected, mode=mode), classes=classes)
                )
        if self._blocked:
            container.mount(
                Static(
                    render_section_caption(BLOCKED_CAPTION, len(self._blocked)),
                    classes="autopilot-section",
                )
            )
            for blocked_row in self._blocked:
                container.mount(Static(render_blocked_row(blocked_row), classes=BLOCKED_ROW_CLASS))

    def _frontier_empty_hero(self, *, mode: RenderMode, with_sigil: bool = True) -> str:
        """Return the centered honest-empty hero for a dry ready frontier.

        Routes the :data:`EMPTY_NOTICE` "no ready waves" copy through the
        shared :func:`~eawf.surfaces.tui.widgets.empty_state.render_empty_state`
        hero so a frontier with nothing claimable reads as the calm centered
        hero (a muted brand sigil over the ``$warn`` headline + the framing
        subline) rather than a top-left one-liner. The ``[ a arm fleet ]``
        action chip mirrors the live ``a`` arm-flow binding -- the operator's
        next move from an idle frontier.

        Args:
            mode: The App's resolved render-mode label, threaded to the brand
                sigil's glyph column.
            with_sigil: When ``False`` the leading brand glyph is dropped -- the
                unicode path leads the hero with the ASCII-art Seal instead, so
                the glyph would be a redundant second brand mark.
        """
        return render_empty_state(
            EMPTY_NOTICE,
            "no claim-ready wave on the frontier",
            mode=mode,
            chips=(("a", "arm fleet"),),
            sigil=with_sigil,
        )

    def _repaint_selection(self) -> None:
        """Repaint the ready rows so the selection tint + checkbox look move.

        The checkbox affordance glyph lives in the row markup, so a selection
        change must re-render the ready rows (not merely retint): it rewrites
        each ready row with its current selected flag and toggles the
        ``-selected`` tint class to match.
        """
        mode = self._render_mode()
        for index, widget in enumerate(self.query(f".{FRONTIER_ROW_CLASS}").results(Static)):
            selected = index == self.selected
            if index < len(self._rows):
                widget.update(render_ready_row(self._rows[index], selected=selected, mode=mode))
            widget.set_class(selected, SELECTED_ROW_CLASS)

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
        frontier, and enriches each ready row with its wave title.

        Returns:
            The ready-wave display rows in claim order; empty when no wave is
            claim-ready.
        """
        state = self._current_state()
        frontier = compute_ready_frontier(build_frontier_items(state))
        return ready_rows(frontier, state)

    def _current_blocked(self) -> tuple[BlockedWaveRow, ...]:
        """Compute the blocked-wave display rows for the active scope.

        Derives the PENDING waves held off the same frontier compute the ready
        rows use -- each naming the dep blocking it (:func:`blocked_rows`) -- so
        the ready/blocked split stays consistent.

        Returns:
            The blocked-wave display rows in claim order; empty when no PENDING
            wave is held off the frontier.
        """
        state = self._current_state()
        frontier = compute_ready_frontier(build_frontier_items(state))
        return blocked_rows(frontier, state)

    def _current_lanes(self) -> tuple[LaneCellRow, ...]:
        """Compute the in-flight (+ just-forked) lane cells for the active run.

        Projects the bound state's persisted fleet run into the lane-cell rows
        (:func:`lane_cells`) -- each carrying its ``repair n/<budget>`` counter,
        a just-forked lane escalating to the fork badge. An unarmed run yields
        no cells (the honest-empty lanes path).

        Returns:
            The lane-cell display rows in claim order; empty when no run is
            armed or no lane is in flight / repair-forked.
        """
        return lane_cells(self._current_fleet_run())

    def _render_mode(self) -> RenderMode:
        """Return the App's active render mode, defaulting when unavailable.

        A bare harness without the reactive degrades to
        :data:`~eawf.surfaces.tui.widgets.eu_bar.DEFAULT_RENDER_MODE`.
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def _current_state(self) -> State | None:
        """Return the bound read-only state, if loaded."""
        from eawf.kernel.state.models import State

        state = self.state
        if state is not None:
            return state
        app_state = getattr(self.app, "state", None)
        return app_state if isinstance(app_state, State) else None

    def _current_fleet_run(self) -> FleetRun | None:
        """Return the bound state's persisted fleet run, or ``None`` when unarmed.

        Reads :attr:`~eawf.kernel.state.models.State.fleet_run` off the bound
        state -- the daemon-owned auto-drain loop persists it, so the cockpit
        vitals header reads it straight rather than recomputing any tally. An
        unbound state (fresh / user scope) or a state written before the field
        existed reads as no armed run.
        """
        state = self._current_state()
        return state.fleet_run if state is not None else None

    def _daemon_available(self) -> bool:
        """Return whether the App reports a reachable daemon socket.

        Delegates to the App's own daemon-socket probe so the dispatch path
        uses the same reachability verdict the rest of the TUI mutates through;
        a bare harness without the probe degrades to "unavailable".
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
    "BATCH_NO_DAEMON",
    "BLOCKED_BY_MARKER",
    "BLOCKED_CAPTION",
    "BLOCKED_ROW_CLASS",
    "BLOCKED_SECTION_ID",
    "COCKPIT_IDLE",
    "COCKPIT_VITALS_ID",
    "DISPATCH_FLAVOUR",
    "DISPATCH_IDLE",
    "DISPATCH_NO_DAEMON",
    "DISPATCH_NO_TARGET",
    "DISPATCH_RESULT_ID",
    "EMPTY_NOTICE",
    "FLEET_HALT_NO_DAEMON",
    "FORK_ESCALATION_LABEL",
    "FORK_INBOX_NO_TARGET",
    "FRONTIER_EMPTY_ID",
    "FRONTIER_HEADER_ID",
    "FRONTIER_LIST_ID",
    "FRONTIER_ROW_CLASS",
    "HALT_NO_DAEMON",
    "HALT_NO_TARGET",
    "KILL_NO_DAEMON",
    "KILL_NO_TARGET",
    "LANES_CAPTION",
    "LANES_SECTION_ID",
    "LANE_CELL_CLASS",
    "MULTI_SELECT_COMMITTED",
    "MULTI_SELECT_EMPTY_COMMIT",
    "MULTI_SELECT_ID",
    "MULTI_SELECT_NO_TARGET",
    "MULTI_SELECT_PREFIX",
    "PAUSE_NO_DAEMON",
    "READY_CAPTION",
    "READY_SECTION_ID",
    "REPAIR_BUDGET",
    "REPAIR_LABEL",
    "SELECTED_ROW_CLASS",
    "SKIP_NO_NEXT",
    "SKIP_NO_TARGET",
    "AutopilotModeScreen",
    "BlockedWaveRow",
    "LaneCellRow",
    "ReadyWaveRow",
    "blocked_rows",
    "build_frontier_items",
    "lane_cells",
    "ready_rows",
    "render_blocked_row",
    "render_cockpit_vitals",
    "render_frontier_header",
    "render_lane_cell",
    "render_ready_row",
    "render_section_caption",
]
