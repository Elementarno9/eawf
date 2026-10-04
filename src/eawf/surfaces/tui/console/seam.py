"""The console's one link to the daemon projection, and the way back after a break.

Everything the console draws arrives through this seam, and the seam owns no transport of
its own: it holds a :class:`~eawf.surfaces.tui.chassis.state_binding.StateBinding` and uses
its socket push, its always-on poll backstop and its resume cursor. That is the point of
binding it this way rather than opening a second connection -- a second socket would be a
second thing to authorise, probe, throttle and reconnect, and the console would then have
two answers to "am I live" that could disagree.

Reconnect runs the seven steps the connection contract states. The seam persists the scope,
the route, the selected id, the filters, the projection revision and the last acknowledged
ordinal (1); it asks the daemon from that cursor and takes back a replay, a refusal, or
nothing to do (2); it refuses any patch outside the exact range the daemon named (3); it
applies the gap and lands on the daemon's cursor, or holds the refusal until an operator
accepts the repair and a whole projection is fetched (4); and it restores the selection by
stable id, reporting a selection that is gone rather than sliding onto a neighbour (5). It
reconciles every operation it sent and never heard back about (6): a Run control whose line
the replay carries is settled from that patch, because the daemon commits each control line
at an ordinal of its own and names the request it belongs to, so nothing is asked twice; any
other operation is sent again under its own operation id, which the daemon files the write
under, so the second send answers with what the first one did rather than writing twice, and
an operation whose answer is lost again stays outstanding in recovery, the operator's to
decide. Step 7 -- a clean load and a replayed projection at one cursor digest alike -- holds
because the replay rebuilds through the daemon's own projection builder rather than
digesting rows here.

The seam is also the one way a console verb reaches the daemon. A verb is addressed from the
projection rows the seam holds, so a write names the revision the operator was shown, and is
sent through the same binding every read uses; the write path and its ledger of outstanding
operations are :mod:`eawf.surfaces.tui.console.seam_writes`.

A count is the other thing the seam answers, because only the seam knows what the link can
vouch for: outside a live and complete projection a count is labelled rather than stated,
and a count whose register could not be read at all renders as unavailable and never as a
zero.

The seam holds more than the route on screen. It keeps a bounded cache of route projections
over its one binding: a route is read once, on the first navigation to it, and every held
route is kept current by the keyed patches the one feed pushes, so going back to a route
costs no read. Attention is pinned in the cache, because the header prints its count on
every route. The cache is bounded because a large tree makes each projection large; the
least recently shown unpinned route is evicted first, and is read again if the operator
returns to it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from eawf.kernel.projection.compute import (
    ROUTE_COLLECTIONS,
    KeyedPatch,
    ProjectionRow,
    RouteProjection,
)
from eawf.kernel.projection.connection import (
    UNAVAILABLE_COUNT,
    ConnectionValue,
    ReconnectDisposition,
    ReconnectNegotiation,
    ReplayNote,
    apply_patches,
    connection_for_disposition,
    connection_value,
    projection_now,
    read_method,
    reconnect_method,
    replay_note,
    staleness_target_seconds,
    vouches_for_counts,
)
from eawf.kernel.projection.registers import ATTENTION_ROUTE
from eawf.kernel.projection.settings import SETTINGS_ROUTE, SETTINGS_ROUTES, EffectiveSettingsView
from eawf.kernel.state.epoch2.evidence_rung import ClaimLadder
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.budget.notices import BudgetThresholdNotice
from eawf.runtime.daemon.methods.state_subscribe import PROJECTION_SUBSCRIBE_METHOD
from eawf.surfaces.tui.chassis.state_binding import StateBinding, StateBindingCallbacks
from eawf.surfaces.tui.console.bulk import BulkRequest
from eawf.surfaces.tui.console.decisions import ClaimRecord, DecisionRecords, QuestionRecord
from eawf.surfaces.tui.console.live_reads import LIVE_READS, counted_replay, held_records
from eawf.surfaces.tui.console.operations import (
    NOTICE_LIST_METHOD,
    ConsoleOperation,
    OperationResult,
    Operator,
    VerbRequest,
)
from eawf.surfaces.tui.console.registry import REGISTRY
from eawf.surfaces.tui.console.seam_writes import SeamWrites
from eawf.workflow.decision_question import QUESTION_DECISIONS_METHOD
from eawf.workflow.evidence.claim_ladder import EVIDENCE_LADDER_METHOD
from eawf.workflow.projection.acceptance import (
    MILESTONE_ACCEPTANCE_METHOD,
    MILESTONE_ROUTE,
    MilestoneAcceptanceRecord,
)

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

#: The label a count carries when the link cannot vouch for completeness. The number
#: is still shown, because a register that was read states something true; what it
#: may not claim is that it holds every row.
KNOWN_COUNT_LABEL = "known"

#: The routes the cache never evicts. The header prints the Attention count on every
#: route, so a console that dropped the register would print a stale or absent count.
PINNED_ROUTES: frozenset[str] = frozenset({ATTENTION_ROUTE})

#: How many route projections the seam holds at once, the pinned ones included. A
#: projection of a large tree is megabytes of rows, so the bound is what keeps a long
#: session's memory flat; eight covers a working set of back-and-forth navigation.
DEFAULT_ROUTE_CAPACITY = 8

#: How many routes read ahead of the operator opening them are kept. Each is one detail
#: frame an Enter is likely to open, so a handful covers the rows the caret just crossed.
PREFETCH_CAPACITY = 4

#: The collections whose moves can change what a Milestone was accepted at or by: the
#: Milestone itself, whose accepted revision moves, and the question an approval seals.
_ACCEPTANCE_COLLECTIONS: frozenset[Epoch2Collection] = frozenset(
    {Epoch2Collection.MILESTONE, Epoch2Collection.PENDING_ACTION}
)

#: The Evidence route and its rung card, which draw one claim's ladder; the route first.
EVIDENCE_ROUTES: tuple[str, ...] = ("evidence", "evidence.digest")

#: Called with the routes one pushed patch changed, so the app can repaint when the
#: route on screen is among them.
PatchListener = Callable[[tuple[str, ...]], None]

#: Called with the route projections one sync read, before it reads what hangs off them,
#: so the frame is drawn from its own rows while the slower per-record reads run.
ProjectedListener = Callable[[tuple[str, ...]], None]


#: The Recovery doors, the reconnect protocol's three paths back into a lost projection.
REATTACH_DOOR: Final = "reattach"
REPLAY_DOOR: Final = "replay"
READ_ONLY_DOOR: Final = "read-only"
RECOVERY_DOORS: Final = (REATTACH_DOOR, REPLAY_DOOR, READ_ONLY_DOOR)


@dataclass(frozen=True, slots=True, kw_only=True)
class SeamCursor:
    """Everything the console persists across a break in the link.

    Attributes:
        scope_id: The scope the projection is read for.
        route: The console route key the seam is bound to.
        selected_id: The stable id the selection is restored by; never a row offset,
            because an offset means a different row after any insert.
        filters: The filters in force, restored with the selection.
        projection_revision: The revision the held projection was read at.
        cursor: The last ordinal the console acknowledged; ``0`` before any.
    """

    scope_id: str
    route: str
    selected_id: str | None = None
    filters: Mapping[str, str] = field(default_factory=dict)
    projection_revision: int = 0
    cursor: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconnectOutcome:
    """What one run of the reconnect protocol did.

    Attributes:
        negotiation: The daemon's answer, gap range included.
        connection: The value the link reads as when the run returns.
        applied: How many keyed patches were applied; always ``0`` unless the
            disposition was a replay.
        selection_missing: Whether the restored selection names a row the projection
            no longer holds. The selection is kept rather than moved, so the console
            opens a resolution card instead of silently selecting a neighbour.
        reconciled: What each operation outstanding at the break turned out to be, in
            the order it was sent; one still unanswered is reported as outstanding.
    """

    negotiation: ReconnectNegotiation
    connection: ConnectionValue
    applied: int
    selection_missing: bool
    reconciled: tuple[OperationResult, ...] = ()


class ProjectionSeam:
    """The console's link to the daemon projection, holding the routes it has read.

    The seam is constructed with the tree it reads and the callbacks the app wants
    for the epoch-1 legs it shares; it builds the one binding it uses and registers
    itself as that binding's patch sink. It opens no other transport. One route is
    the visible one: it is what a reconnect restores and what the link's value is
    read from. The other held routes ride the same feed.
    """

    def __init__(
        self,
        *,
        route: str,
        scope_id: str,
        state_path: Path | None,
        repo_root: Path | None = None,
        on_state: Callable[[State], Awaitable[None]] | None = None,
        on_degraded: Callable[[bool], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
        capacity: int = DEFAULT_ROUTE_CAPACITY,
        operator: Operator | None = None,
        scope_name: str = "",
        **binding_options: Any,
    ) -> None:
        """Build the seam and the one binding that carries it.

        Args:
            route: The console route key the seam opens on.
            scope_id: The scope the projection is stated for.
            state_path: The tree's ``state.json``, which the poll backstop watches.
            repo_root: The repository the daemon should answer for; omitted when
                the daemon's own bound tree is the right one.
            on_state: The app's epoch-1 state sink, awaited on every backstop
                delivery. The seam counts the deliveries either way, so a silent
                push stream is still visible to it.
            on_degraded: The app's degraded sink. The seam also takes the flag as
                its own transport signal: a daemon that cannot be reached is a
                disconnected link, not a live one.
            clock: When a rebuilt projection is stamped; defaults to the wall clock.
            capacity: How many route projections are held at once, pinned ones
                included.
            operator: Who the console's writes are attributed to. A seam given none
                refuses every write with that reason and sends nothing.
            scope_name: The name an operator knows the scope by, which the header shows
                in place of ``scope_id``; empty shows the id.
            **binding_options: Passed through to the binding, for the poll and probe
                cadences and the client factory a test drives it with.

        Raises:
            ValueError: *capacity* leaves no room for a visible route beside the
                pinned ones.
        """
        if capacity <= len(PINNED_ROUTES):
            raise ValueError(
                f"capacity {capacity} holds no route beside the {len(PINNED_ROUTES)} pinned; "
                f"it must be at least {len(PINNED_ROUTES) + 1}"
            )
        self._route = route
        self._scope_id = scope_id
        self._repo_root = repo_root
        self._clock = clock or projection_now
        self._capacity = capacity
        self._app_state = on_state
        self._app_degraded = on_degraded
        self._held: OrderedDict[str, RouteProjection] = OrderedDict()
        # the record each held route was read for, since its closed rows are read per record
        self._held_about: dict[str, str | None] = {}
        # routes read ahead of the operator opening them, by route and the record read for
        self._prefetched: OrderedDict[tuple[str, str | None], RouteProjection] = OrderedDict()
        self._listeners: list[PatchListener] = []
        self._reading: set[str] = set()
        self._settings: EffectiveSettingsView | None = None
        self._acceptance: dict[str, MilestoneAcceptanceRecord] = {}
        # each live read's answer, beside the address it was read for
        self._live: dict[str, tuple[str, Any]] = {}
        self._notices: tuple[BudgetThresholdNotice, ...] | None = None
        self._decisions: tuple[PendingAction, ...] | None = None
        self._ladders: dict[str, ClaimLadder] = {}
        self._subject: str | None = None
        self._history_cursor: int | None = None
        self._selected_id: str | None = None
        self._filters: dict[str, str] = {}
        self._connection = ConnectionValue.DISCONNECTED
        self._replay: ReplayNote | None = None
        self._backstop_ticks = 0
        self._operator = operator
        self._scope_name = scope_name
        self._writes = SeamWrites(self)
        self._binding = StateBinding(
            state_path,
            StateBindingCallbacks(
                on_state=self._on_state,
                on_degraded=self._on_degraded,
                on_patch=self.apply_patch,
            ),
            subscribe_method=PROJECTION_SUBSCRIBE_METHOD,
            **binding_options,
        )

    @property
    def binding(self) -> StateBinding:
        """Return the one transport this seam runs on."""
        return self._binding

    @property
    def route(self) -> str:
        """Return the console route key on screen."""
        return self._route

    @property
    def scope_id(self) -> str:
        """Return the id of the scope the projection is stated for."""
        return self._scope_id

    @property
    def scope_name(self) -> str:
        """Return the name the header gives the scope; empty when it is named by its id."""
        return self._scope_name

    @property
    def repo_root(self) -> Path | None:
        """Return the repository the seam reads; ``None`` when the daemon's own tree is it."""
        return self._repo_root

    @property
    def operator(self) -> Operator | None:
        """Return who the console's writes are attributed to; ``None`` when nobody."""
        return self._operator

    @property
    def connection(self) -> ConnectionValue:
        """Return the link's current value, one of the nine."""
        return self._connection

    @property
    def replay_note(self) -> ReplayNote | None:
        """Return the last replay's start and head while the link is replaying, else ``None``.

        The note is the negotiation's own answer, so a replaying frame can say how far the
        head is without the console asserting a number of its own.
        """
        return self._replay if self._connection is ConnectionValue.REPLAYING else None

    @property
    def projection(self) -> RouteProjection | None:
        """Return the visible route's projection; ``None`` before its first load."""
        return self._projection

    @property
    def _projection(self) -> RouteProjection | None:
        """Return the visible route's held projection, if any."""
        return self._held.get(self._route)

    @_projection.setter
    def _projection(self, projection: RouteProjection | None) -> None:
        """Hold *projection* as the visible route's, or drop the route for ``None``."""
        if projection is None:
            self._held.pop(self._route, None)
        else:
            self._hold(self._route, projection)

    @property
    def held_routes(self) -> tuple[str, ...]:
        """Return the held routes, least recently shown first."""
        return tuple(self._held)

    def projection_for(self, route: str) -> RouteProjection | None:
        """Return the projection held for *route*; ``None`` when it is not held."""
        return self._held.get(route)

    def now(self) -> datetime:
        """Return the time a read is stamped with: the seam's clock."""
        return self._clock()

    def owed(self) -> tuple[str, ...]:
        """Return the routes the console needs held and does not yet hold.

        The visible route is owed when the daemon serves a read for it, and the
        pinned routes always are. The settings routes are owed their one view,
        read under the settings route, until it arrives. A route already being read
        is not owed again, so a quick run of navigations issues one read per route.
        """
        pinned = sorted(PINNED_ROUTES - {self._route})
        owed = [route for route in pinned if route in ROUTE_COLLECTIONS and route not in self._held]
        if self.route_owed():
            owed.insert(0, self._route)
        if self._route in SETTINGS_ROUTES and self._settings is None:
            owed.append(SETTINGS_ROUTE)
        if self._acceptance_owed():
            owed.append(MILESTONE_ACCEPTANCE_METHOD)
        # a live read is owed once it can be addressed, and again when its address moves
        for name, read in LIVE_READS.items():
            address = read.address(self) if self._route in read.routes else None
            if address is not None and self._live.get(name, ("", None))[0] != address:
                owed.append(name)
        # a budget notice is addressed to a principal, so a console acting as nobody has none
        if self._route == ATTENTION_ROUTE and self._operator is not None and self._notices is None:
            owed.append(NOTICE_LIST_METHOD)
        # a decision is answered from its Attention row, so its options are read there
        if self._route == ATTENTION_ROUTE and self._decisions is None:
            owed.append(QUESTION_DECISIONS_METHOD)
        # a claim's rung rows and its rung card are drawn from the one ladder read
        claim = self._claim_subject()
        if claim is not None and claim not in self._ladders:
            owed.append(EVIDENCE_LADDER_METHOD)
        return tuple(route for route in owed if route not in self._reading)

    def _acceptance_owed(self) -> bool:
        """Return whether the visible Milestone's acceptance is unread for its subject."""
        subject = self._subject
        return self._route == MILESTONE_ROUTE and bool(subject) and subject not in self._acceptance

    def frame_owed(self) -> bool:
        """Return whether the visible route's frame still waits on a read it states as fact.

        Its own rows, and a Milestone's acceptance, whose absence the frame would draw as
        "no bundle is sealed" rather than as not yet read.
        """
        return self.route_owed() or self._acceptance_owed()

    def route_owed(self) -> bool:
        """Return whether the visible route's own projection is unread or read for another record.

        A route opened on another record holds that record's closed rows, not this one's.
        """
        route = self._route
        if route not in ROUTE_COLLECTIONS:
            return False
        return route not in self._held or self._held_about.get(route) != self._about(route)

    def is_reading(self, name: str) -> bool:
        """Return whether owed read *name* is in flight."""
        return name in self._reading

    def retarget(self, route: str) -> None:
        """Make *route* the visible one.

        The selection and filters belong to the route they were made on, so a move
        clears them. The link's value is re-read from the route's projection when it
        is held; an unheld route leaves the value as the link last stated it.
        """
        if route == self._route:
            return
        self._route = route
        self._selected_id = None
        self._filters = {}
        held = self._held.get(route)
        if held is not None:
            self._held.move_to_end(route)
            self._connection = self._value_of(held)

    def about(self, subject: str | None) -> None:
        """Record the record the visible route is about, so its per-subject reads are owed.

        A route read ahead for that record is held at once, so opening it owes no read.
        """
        self._subject = subject
        if not self.route_owed():
            return
        about = self._about(self._route)
        ahead = self._prefetched.pop((self._route, about), None)
        if ahead is not None:
            self._hold(self._route, ahead)
            self._held_about[self._route] = about
            logger.debug(f"prefetch adopted route={self._route} about={about}")

    def page_history(self, cursor: int | None) -> None:
        """Record the feed cursor History reads its page from; ``None`` reads the newest."""
        self._history_cursor = cursor

    @property
    def history_cursor(self) -> int | None:
        """Return the feed cursor History reads its page from; ``None`` reads the newest."""
        return self._history_cursor

    def _about(self, route: str) -> str | None:
        """Return the record *route* is read for: the subject of a visible single-record route.

        A list route's subject is only the row under its caret, which moves on every key and
        changes nothing the route reads.
        """
        return self._read_for(route, self._subject) if route == self._route else None

    @staticmethod
    def _read_for(route: str, subject: str | None) -> str | None:
        """Return the record *route* is read for when it is opened onto *subject*."""
        spec = REGISTRY.by_key.get(route)
        return subject if spec is not None and spec.subject_required else None

    def acceptance_for(self, key: str | None) -> MilestoneAcceptanceRecord | None:
        """Return the acceptance read held for Milestone *key*; ``None`` before its read."""
        return None if key is None else self._acceptance.get(key)

    @property
    def subject(self) -> str | None:
        """Return the key of the record the visible route is about; ``None`` when none."""
        return self._subject

    def live(self, name: str, *, anywhere: bool = False) -> Any | None:
        """Return live read *name*'s answer for what the route is about now; ``None`` before it.

        With *anywhere*, the last answer is returned whatever route it was read on.
        """
        held = self._live.get(name)
        read = LIVE_READS[name]
        if held is None or self._route not in read.routes or held[0] != read.address(self):
            return held[1] if held is not None and anywhere else None
        return held[1]

    def live_on_screen(self) -> tuple[str, ...]:
        """Return the live reads the visible route holds an answer for, which it re-reads."""
        return tuple(name for name in LIVE_READS if self.live(name) is not None)

    def watch(self, listener: PatchListener) -> None:
        """Call *listener* with the routes every applied patch changed."""
        self._listeners.append(listener)

    async def sync(self, on_projected: ProjectedListener | None = None) -> tuple[str, ...]:
        """Read every owed route once, and hold what arrives.

        The route projections are read first and one at a time, and *on_projected* hears
        them before the reads that hang off them -- acceptance, live reads, notices --
        which run together, so a slow repository read never delays a frame's own rows.

        A read that fails leaves its route unheld, so the frame keeps saying it holds
        nothing rather than drawing a guess; the next navigation owes it again. The
        failure is not raised, because one unreachable register must not stop the
        other routes from loading.

        Args:
            on_projected: Called with the route projections each round read, when any.

        Returns:
            The routes this call read: the projections in the order they were read, then
            the rest in the order they were owed.
        """
        loaded: list[str] = []
        attempted: set[str] = set()
        # a read can make another owed -- a Run's lines once its route's rows arrive -- so
        # the owed set is taken again until it holds nothing this call has not tried
        while owed := tuple(route for route in self.owed() if route not in attempted):
            frames = [name for name in owed if name in ROUTE_COLLECTIONS or name == SETTINGS_ROUTE]
            if frames:
                attempted.update(frames)
                self._reading.update(frames)
                projected = [name for name in frames if await self._read_one(name)]
                loaded += projected
                if projected and on_projected is not None:
                    on_projected(tuple(projected))
                # what hangs off the rows is owed by what they hold, and by the subject the
                # listener read off them, so it is taken again once they are held
                continue
            attempted.update(owed)
            self._reading.update(owed)
            arrived = await asyncio.gather(*map(self._read_one, owed))
            loaded += [name for name, ok in zip(owed, arrived, strict=True) if ok]
        return tuple(loaded)

    async def _read_one(self, route: str) -> bool:
        """Read owed *route* once, holding what arrives; return whether it arrived."""
        try:
            if route == SETTINGS_ROUTE:
                await self.load_settings()
            elif route == MILESTONE_ACCEPTANCE_METHOD:
                await self.load_acceptance()
            elif route in LIVE_READS:
                await self.load_live(route)
            elif route == NOTICE_LIST_METHOD:
                await self.load_notices()
            elif route == QUESTION_DECISIONS_METHOD:
                await self.load_decisions()
            elif route == EVIDENCE_LADDER_METHOD:
                await self.load_ladder()
            else:
                await self.load(route)
        except Exception as exc:
            logger.warning(f"sync read failed route={route} cause={exc!r}")
            return False
        finally:
            self._reading.discard(route)
        return True

    async def prefetch(self, route: str, subject: str | None) -> None:
        """Read *route* as it would open onto *subject*, before the operator opens it.

        The answer is kept apart from the held routes until :meth:`about` opens it, so a
        row the caret only crossed never becomes the route on screen. A route already
        held or read ahead for that record is not read again, and a route the daemon
        serves no read for is not read at all. A Milestone's acceptance is read with it,
        because its frame waits for that too.

        Args:
            route: The route the row would open.
            subject: The record it would open onto.
        """
        about = self._read_for(route, subject)
        reads: list[Awaitable[object]] = []
        if route == MILESTONE_ROUTE and about and about not in self._acceptance:
            reads.append(self.load_acceptance(about))
        held = route in self._held and self._held_about.get(route) == about
        if route in ROUTE_COLLECTIONS and not held and (route, about) not in self._prefetched:
            reads.append(self._read_ahead(route, about))
        await asyncio.gather(*reads)

    async def _read_ahead(self, route: str, about: str | None) -> None:
        """Read *route* for record *about* into the read-ahead cache, dropping the oldest."""
        params = self._params() if about is None else {**self._params(), "key": about}
        answer = await self._binding.call(read_method(route), params)
        self._prefetched[(route, about)] = RouteProjection.model_validate(answer)
        while len(self._prefetched) > PREFETCH_CAPACITY:
            self._prefetched.popitem(last=False)
        logger.debug(f"prefetch route={route} about={about}")

    @property
    def settings(self) -> EffectiveSettingsView | None:
        """Return the effective-settings view; ``None`` before the first settings read."""
        return self._settings

    @property
    def cursor(self) -> int:
        """Return the last ordinal the console acknowledged; ``0`` before any.

        The transport tracks its own resume cursor from every patch it delivered,
        and the held projection tracks every patch that was applied. The two agree
        in the steady state; the later of them is what has actually been
        acknowledged, so it is the cursor a reconnect asks from.
        """
        held = self._held_cursor() if self._projection is not None else 0
        return max(held, self._binding.resume_cursor)

    @property
    def backstop_ticks(self) -> int:
        """Return how many times the poll backstop has delivered state to this seam."""
        return self._backstop_ticks

    @property
    def vouches_for_counts(self) -> bool:
        """Return whether a count taken now may be called complete."""
        return vouches_for_counts(self._connection)

    def persisted(self) -> SeamCursor:
        """Return everything the console keeps across a break in the link."""
        held = self._projection
        return SeamCursor(
            scope_id=self._scope_id,
            route=self._route,
            selected_id=self._selected_id,
            filters=dict(self._filters),
            projection_revision=held.header.projection_revision if held is not None else 0,
            cursor=self.cursor,
        )

    def select(self, selected_id: str | None, **filters: str) -> None:
        """Record the selection and filters a reconnect restores by stable id."""
        self._selected_id = selected_id
        self._filters = dict(filters)

    def staleness_target(self, *, terminal: bool = False) -> float:
        """Return the seconds this seam's view may age before it is stale.

        Args:
            terminal: Whether the subject drawn is a terminal entity, which ages
                informationally rather than pulsing like a live one.

        Returns:
            The target in seconds.
        """
        return staleness_target_seconds(self._route, terminal=terminal)

    def count(self, value: int | None) -> str:
        """Render one count as honestly as the link allows.

        Args:
            value: The count, or ``None`` when the register behind it could not be
                read at all.

        Returns:
            The number alone only under a live and complete link; the number under a
            ``known`` label otherwise; and the unavailable token when nothing was read.
        """
        if value is None:
            return UNAVAILABLE_COUNT
        if self.vouches_for_counts:
            return str(value)
        return f"{value} {KNOWN_COUNT_LABEL}"

    async def load(self, route: str | None = None) -> RouteProjection:
        """Read one whole route from the daemon and hold it.

        The visible route is read for the record it is opened on, so a record that has
        closed, and the closed records filed under it, are read back with it.

        Args:
            route: The route to read; the visible route when omitted.

        Returns:
            The projection, at whatever cursor the tree stands at.
        """
        route = route or self._route
        about = self._about(route)
        params = self._params() if about is None else {**self._params(), "key": about}
        answer = await self._binding.call(read_method(route), params)
        projection = self._hold(route, RouteProjection.model_validate(answer))
        self._held_about[route] = about
        logger.debug(f"load route={route} cursor={projection.header.source_cursor}")
        return projection

    async def load_settings(self) -> EffectiveSettingsView:
        """Read every configuration leaf and the layer behind it, and hold the answer.

        The settings view is not a row projection: config carries no ordinal, so there
        is nothing to patch it with and it is re-read instead. It comes through this
        seam anyway, so the console has one link and one answer to how old a read is.

        Returns:
            The view, with every leaf's effective value, winning layer and stack.
        """
        answer = await self._binding.call(read_method(SETTINGS_ROUTE), self._params())
        view = EffectiveSettingsView.model_validate(answer)
        self._settings = view
        logger.debug(f"load_settings leaves={len(view.leaves)}")
        return view

    async def load_acceptance(self, key: str | None = None) -> MilestoneAcceptanceRecord:
        """Read one Milestone's sealed bundle and bound approval, and hold the answer.

        The Milestone frame is about one record, so what it was accepted at is read per
        record; another Milestone's acceptance is never drawn under this one.

        Args:
            key: The Milestone to read; the visible route's subject when omitted.

        Returns:
            The record, with no bundle and no approval when none is held for it.

        Raises:
            ValueError: No Milestone was named and the visible route is about none.
        """
        key = key or self._subject
        if not key:
            raise ValueError("an acceptance read names the Milestone it is about")
        answer = await self._binding.call(
            MILESTONE_ACCEPTANCE_METHOD, {**self._params(), "milestone_key": key}
        )
        record = MilestoneAcceptanceRecord.model_validate(answer)
        self._acceptance[key] = record
        logger.debug(f"load_acceptance milestone={key} bundle={record.bundle is not None}")
        return record

    async def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Send one read to the daemon for this seam's tree and return its answer."""
        return await self._binding.call(method, {**self._params(), **params})

    async def load_live(self, name: str) -> Any:
        """Read live read *name* for what the visible route is about now, and hold it.

        Args:
            name: The read's name in :data:`LIVE_READS`.

        Returns:
            The answer the read's own fetch returned.

        Raises:
            ValueError: The read cannot be addressed: its route is not on screen, or the
                record it is about is not held yet.
        """
        read = LIVE_READS[name]
        address = read.address(self) if self._route in read.routes else None
        if address is None:
            raise ValueError(f"live read {name} is about nothing the console holds")
        value = await read.fetch(self, address)
        self._live[name] = (address, value)
        logger.debug(f"load_live name={name}")
        return value

    @property
    def notices(self) -> tuple[BudgetThresholdNotice, ...]:
        """Return the open budget notices in this principal's inbox; empty before their read."""
        return self._notices or ()

    async def load_notices(self) -> tuple[BudgetThresholdNotice, ...]:
        """Read the budget notices active in the operator's inbox, and hold them.

        The notice ledger is not a document collection, so no patch carries it; it is
        re-read after every disposition instead. A snoozed or acknowledged notice is not
        in the active inbox, so it is not held.

        Returns:
            The active notices, in ledger-key order.

        Raises:
            ValueError: The seam acts as nobody, so no inbox is addressed to it.
        """
        if self._operator is None:
            raise ValueError("a notice inbox is one principal's; the console acts as nobody")
        answer = await self._binding.call(
            NOTICE_LIST_METHOD, {**self._params(), "principal": self._operator.principal}
        )
        self._notices = tuple(
            BudgetThresholdNotice.model_validate(item) for item in answer.get("active", ())
        )
        logger.debug(f"load_notices active={len(self._notices)}")
        return self._notices

    @property
    def decisions(self) -> DecisionRecords | None:
        """Return the records the question and pause details are bound to; ``None`` unread.

        Each waiting operator decision is held as the question it asks, beside every
        question and pause the tree holds, each in the situation the daemon projected, so
        a detail opened from a row draws the record and never a prototype one.
        """
        held = held_records(self)
        if self._decisions is None and not self._ladders:
            return held
        operator, asked = self._operator, held.questions if held is not None else ()
        return (held or DecisionRecords()).model_copy(
            update={
                "principal": operator.principal if operator is not None else None,
                "questions": (*map(QuestionRecord.of_decision, self._decisions or ()), *asked),
                "claims": tuple(map(ClaimRecord.of_ladder, self._ladders.values())),
            }
        )

    def _claim_subject(self) -> str | None:
        """Return the claim the visible Evidence surface is about, or ``None`` off it.

        The subject names it; a route opened on no subject is about the first claim its
        projection lists, which is the claim the frame draws.
        """
        if self._route not in EVIDENCE_ROUTES:
            return None
        if self._subject:
            return self._subject
        held = self._held.get(EVIDENCE_ROUTES[0])
        claims = (
            [r.key for r in held.rows if r.collection is Epoch2Collection.CLAIM] if held else []
        )
        return claims[0] if claims else None

    async def load_ladder(self, key: str | None = None) -> ClaimLadder:
        """Read one claim with the latest record of each rung, and hold it.

        Args:
            key: The claim to read; the visible Evidence surface's claim when omitted.

        Returns:
            The claim and its ladder.

        Raises:
            ValueError: No claim was named and the visible surface is about none.
        """
        key = key or self._claim_subject()
        if not key:
            raise ValueError("a ladder read names the claim it is about")
        answer = await self._binding.call(
            EVIDENCE_LADDER_METHOD, {**self._params(), "claim_key": key}
        )
        ladder = ClaimLadder.model_validate(answer)
        self._ladders[key] = ladder
        logger.debug(f"load_ladder claim={key} rungs={len(ladder.rungs)}")
        return ladder

    async def load_decisions(self) -> tuple[PendingAction, ...]:
        """Read every waiting operator decision in full, and hold them.

        The Attention register carries a row's facts rather than its offered answers, so
        the options a decision's card draws are read beside it; a pushed patch to any
        pending action drops the held read, and the next sync reads it again.

        Returns:
            The waiting decisions, in key order.
        """
        answer = await self._binding.call(QUESTION_DECISIONS_METHOD, self._params())
        self._decisions = tuple(
            PendingAction.model_validate(item) for item in answer.get("decisions", ())
        )
        logger.debug(f"load_decisions waiting={len(self._decisions)}")
        return self._decisions

    async def reconnect(self) -> ReconnectOutcome:
        """Run the reconnect protocol from the cursor the console persisted.

        Returns:
            What the run did, with the daemon's negotiation and its exact gap range.

        Raises:
            ValueError: The seam holds no projection, so there is nothing to
                reconnect to and the console owes a cold load; or the daemon replayed
                a patch outside the range it negotiated, which would leave the
                console claiming a gap was closed that was not.
        """
        held = self._projection
        if held is None:
            raise ValueError(
                f"route {self._route!r} has no held projection to reconnect to; "
                "a console with no revision cold-loads rather than reconnecting"
            )
        answer = await self._binding.call(
            reconnect_method(self._route),
            {**self._params(), "cursor": self.cursor},
        )
        negotiation = ReconnectNegotiation.model_validate(answer["negotiation"])
        patches = tuple(KeyedPatch.model_validate(row) for row in answer["patches"])
        self._connection = connection_for_disposition(negotiation.disposition)
        self._replay = replay_note(negotiation)
        if negotiation.disposition is ReconnectDisposition.SNAPSHOT_REQUIRED:
            logger.info(
                f"reconnect refused route={self._route} "
                f"gap={negotiation.gap} first_missing={negotiation.first_missing}"
            )
            self._drop_hidden()
            return self._outcome(negotiation, applied=0, reconciled=await self._writes.reconcile())
        if negotiation.disposition is ReconnectDisposition.CURRENT:
            self._adopt(held)
            return self._outcome(negotiation, applied=0, reconciled=await self._writes.reconcile())
        gap = negotiation.gap
        assert gap is not None, "a replay always states the range it closes"
        self._refuse_patches_outside(patches, negotiation=negotiation)
        self._replay = await counted_replay(self, self._replay)
        for listener in self._listeners:
            listener((self._route,))
        read_back = self._writes.settle_from_patches(patches)
        self._adopt(
            apply_patches(
                held,
                patches,
                cursor=gap.last_sequence,
                scope_id=self._scope_id,
                generated_at=self._clock(),
            )
        )
        self._drop_hidden()
        reconciled = read_back + await self._writes.reconcile()
        return self._outcome(negotiation, applied=len(patches), reconciled=reconciled)

    async def take_door(self, door: str) -> ConnectionValue:
        """Take one Recovery door, the reconnect protocol's own path, and nothing else.

        Reattach reads the visible route at the daemon's head and leaves the link in
        ``GAP``, because the events between the held cursor and that head are not
        replayed. Replay runs :meth:`reconnect` from the held cursor. Read-only reads
        nothing and leaves the console on the snapshot it holds. No door discards a
        held row or picks another door on its own.

        Args:
            door: One of :data:`RECOVERY_DOORS`.

        Returns:
            The connection value the door left the link in.

        Raises:
            ValueError: ``door`` is not a Recovery door.
        """
        if door == REATTACH_DOOR:
            await self.load()
            self._drop_hidden()
            self._connection = ConnectionValue.GAP
        elif door == REPLAY_DOOR:
            await self.reconnect()
        elif door == READ_ONLY_DOOR:
            self._connection = ConnectionValue.OFFLINE_SNAPSHOT
        else:
            raise ValueError(f"{door!r} is not a Recovery door; the doors are {RECOVERY_DOORS}")
        logger.info(f"take_door door={door} route={self._route} value={self._connection}")
        return self._connection

    @property
    def outstanding(self) -> tuple[ConsoleOperation, ...]:
        """Return the operations sent and not yet answered, oldest first."""
        return self._writes.outstanding

    async def request(self, request: VerbRequest) -> OperationResult:
        """Send one console verb to the daemon, addressed from the rows the seam holds.

        See :meth:`~eawf.surfaces.tui.console.seam_writes.SeamWrites.request`.
        """
        return await self._writes.request(request)

    async def bulk(self, request: BulkRequest) -> tuple[OperationResult, ...]:
        """Send one confirmed card's targets as one daemon bulk operation.

        See :meth:`~eawf.surfaces.tui.console.seam_writes.SeamWrites.bulk`.
        """
        return await self._writes.bulk(request)

    def held_rows(self) -> tuple[ProjectionRow, ...]:
        """Return every row the held projections carry, one per key, the visible route's first.

        A consequence card is built from these, so a move is previewed at the revision and
        status the console was shown and at nothing it invented.
        """
        rows: dict[str, ProjectionRow] = {}
        for route in (self._route, *reversed(self._held)):
            held = self._held.get(route)
            for row in held.rows if held is not None else ():
                rows.setdefault(row.key, row)
        return tuple(rows.values())

    async def reload_holding(self, key: str) -> None:
        """Re-read every held route that holds ``key`` when a write says it must be.

        After a stale compare-and-swap the console reads the record as it now stands
        rather than leaving the old revision on screen for a second answer to be refused
        against; after a snooze, which moves no revision, it reads the row's new facts.
        """
        for route in [r for r, held in self._held.items() if any(x.key == key for x in held.rows)]:
            await self.load(route)

    def held_row(self, key: str) -> ProjectionRow | None:
        """Return the held row keyed *key*, the visible route's first."""
        for route in (self._route, *reversed(self._held)):
            held = self._held.get(route)
            found = next((row for row in held.rows if row.key == key), None) if held else None
            if found is not None:
                return found
        return None

    async def load_snapshot(self) -> RouteProjection:
        """Carry out the repair a ``snapshot_required`` refusal demands.

        The link reads as loading for the length of the transfer, and a transfer
        that fails restates the refusal rather than falling back to a live value --
        a failed snapshot leaves the console exactly as unrepaired as before it.

        Returns:
            The whole projection, at the daemon's cursor.

        Raises:
            Exception: Whatever the transport raised, after the refusal is restated.
        """
        self._connection = ConnectionValue.SNAPSHOT_LOADING
        try:
            return await self.load()
        except Exception:
            self._connection = ConnectionValue.SNAPSHOT_REQUIRED
            raise

    async def apply_patch(self, patch: KeyedPatch) -> None:
        """Apply one pushed keyed patch to every held route it reaches.

        The feed is one stream for every route the console holds, so a patch fans
        out to each held route it names and is ignored by the rest. A route not yet
        held is skipped too, because there are no rows for the patch to replace; its
        first read will already include it. The watchers hear which routes changed.
        """
        if any(entry.collection in _ACCEPTANCE_COLLECTIONS for entry in patch.entries):
            # a moved Milestone or sealed question may change what it was accepted at
            self._acceptance.clear()
        if any(entry.collection is Epoch2Collection.PENDING_ACTION for entry in patch.entries):
            # a filed or answered decision changes which ones wait
            self._decisions = None
        # a route read ahead is not kept current by the feed, so a patch to it drops it
        for ahead in [key for key in self._prefetched if key[0] in patch.routes]:
            del self._prefetched[ahead]
        patched = self._fan_out(patch)
        if not patched:
            return
        for listener in self._listeners:
            listener(patched)

    def _fan_out(self, patch: KeyedPatch) -> tuple[str, ...]:
        """Apply *patch* to each held route it reaches; return those routes.

        A route read at or past the patch's ordinal already states it, so the patch
        is not applied again; that happens when a read and the push race.
        """
        patched = tuple(
            route
            for route, held in self._held.items()
            if route in patch.routes and int(held.header.source_cursor) < patch.canonical_sequence
        )
        for route in patched:
            self._patch_route(route, [patch], cursor=patch.canonical_sequence)
        return patched

    def _patch_route(self, route: str, patches: list[KeyedPatch], *, cursor: int) -> None:
        """Advance the held *route* by *patches* to *cursor*; no patches is a no-op."""
        if not patches:
            return
        self._hold(
            route,
            apply_patches(
                self._held[route],
                patches,
                cursor=cursor,
                scope_id=self._scope_id,
                generated_at=self._clock(),
            ),
            shown=False,
        )

    async def connect(self) -> None:
        """Start the one transport: push, probe and the always-on poll backstop."""
        await self._binding.connect()

    async def disconnect(self) -> None:
        """Stop the transport and mark the link as what it then is."""
        await self._binding.disconnect()
        self._connection = ConnectionValue.DISCONNECTED

    def _adopt(self, projection: RouteProjection) -> RouteProjection:
        """Hold *projection* as the visible route's."""
        return self._hold(self._route, projection)

    def _hold(
        self, route: str, projection: RouteProjection, *, shown: bool = True
    ) -> RouteProjection:
        """Hold *projection* for *route*, evicting past the capacity.

        The visible route's projection also sets the link's value. The value is
        derived rather than asserted: a projection states what its producer could
        vouch for, and a console that overrode that with a live value of its own
        would be claiming something no producer stated.

        Args:
            route: The route the projection answers.
            projection: The projection to hold.
            shown: Whether this counts as the route being used, for the eviction
                order. A patch arriving in the background does not.
        """
        self._held[route] = projection
        if shown:
            self._held.move_to_end(route)
        if route == self._route:
            self._connection = self._value_of(projection)
        self._evict()
        return projection

    def _drop_hidden(self) -> None:
        """Drop every held route but the visible one, after a break in the link.

        A reconnect negotiates the visible route alone, and its replay carries only
        that route's patches, so the other held routes still stand before the gap.
        Holding them would draw a pre-break frame as current; dropping them makes
        the next sync read the pinned ones afresh, and any other on its next visit.
        """
        for route in [route for route in self._held if route != self._route]:
            del self._held[route]
        self._acceptance.clear()
        self._live.clear()
        self._prefetched.clear()

    def _evict(self) -> None:
        """Drop the least recently shown unpinned routes until the cache fits.

        The visible route is never dropped: the frame on screen is drawn from it.
        """
        spare = [
            route for route in self._held if route not in PINNED_ROUTES and route != self._route
        ]
        while len(self._held) > self._capacity and spare:
            route = spare.pop(0)
            del self._held[route]
            logger.debug(f"evict route={route} held={len(self._held)}")

    @staticmethod
    def _value_of(projection: RouteProjection) -> ConnectionValue:
        """Return the link value the projection's own header states."""
        return connection_value(
            connection_state=projection.header.connection_state,
            completeness=projection.header.completeness,
        )

    def _params(self) -> dict[str, Any]:
        """Return the request parameters that address this seam's tree."""
        if self._repo_root is None:
            return {}
        return {"repo_root": str(self._repo_root)}

    def _held_cursor(self) -> int:
        """Return the ordinal the held projection was read through."""
        held = self._projection
        assert held is not None, "only called with a projection held"
        return int(held.header.source_cursor)

    def _outcome(
        self,
        negotiation: ReconnectNegotiation,
        *,
        applied: int,
        reconciled: tuple[OperationResult, ...],
    ) -> ReconnectOutcome:
        """Return the outcome, with the selection restored by stable id."""
        return ReconnectOutcome(
            negotiation=negotiation,
            connection=self._connection,
            applied=applied,
            selection_missing=self._selection_missing(),
            reconciled=reconciled,
        )

    def _selection_missing(self) -> bool:
        """Return whether the persisted selection names a row the projection lost."""
        held = self._projection
        if self._selected_id is None or held is None:
            return False
        return all(row.key != self._selected_id for row in held.rows)

    def _refuse_patches_outside(
        self, patches: tuple[KeyedPatch, ...], *, negotiation: ReconnectNegotiation
    ) -> None:
        """Refuse a replay carrying an ordinal the negotiated range does not cover.

        Raises:
            ValueError: A patch falls outside the gap, or the patches arrive out of
                order. Either way the console cannot claim the range it was told.
        """
        gap = negotiation.gap
        assert gap is not None, "a replay always states the range it closes"
        sequences = [patch.canonical_sequence for patch in patches]
        outside = [
            sequence
            for sequence in sequences
            if not gap.first_sequence <= sequence <= gap.last_sequence
        ]
        if outside:
            raise ValueError(
                f"replay for route {self._route!r} carries {outside} outside the gap "
                f"{gap.first_sequence}-{gap.last_sequence} it negotiated"
            )
        if sequences != sorted(sequences):
            raise ValueError(f"replay for route {self._route!r} arrived out of order: {sequences}")

    async def _on_state(self, state: State) -> None:
        """Count one poll-backstop delivery and pass it to the app's own sink."""
        self._backstop_ticks += 1
        if self._app_state is not None:
            await self._app_state(state)

    async def _on_degraded(self, degraded: bool) -> None:
        """Take the transport flag as the link's own, and pass it on.

        A daemon that cannot be reached is a disconnected link. Coming back is not
        the same event: liveness is re-established by a reconnect that names a
        cursor, never by the probe alone.
        """
        if degraded:
            self._connection = ConnectionValue.DISCONNECTED
        if self._app_degraded is not None:
            await self._app_degraded(degraded)


__all__ = [
    "DEFAULT_ROUTE_CAPACITY",
    "KNOWN_COUNT_LABEL",
    "PINNED_ROUTES",
    "PREFETCH_CAPACITY",
    "READ_ONLY_DOOR",
    "REATTACH_DOOR",
    "RECOVERY_DOORS",
    "REPLAY_DOOR",
    "PatchListener",
    "ProjectedListener",
    "ProjectionSeam",
    "ReconnectOutcome",
    "SeamCursor",
]
