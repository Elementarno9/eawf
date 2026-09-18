"""The console's one link to the daemon projection, and the way back after a break.

Everything the console draws arrives through this seam, and the seam owns no
transport of its own: it holds a :class:`~eawf.surfaces.tui.state_binding.StateBinding`
and uses its socket push, its always-on poll backstop and its resume cursor. That is
the point of binding it this way rather than opening a second connection -- a second
socket would be a second thing to authorise, probe, throttle and reconnect, and the
console would then have two answers to "am I live" that could disagree.

Reconnect runs the seven steps the connection contract states. The seam persists the
scope, the route, the selected id, the filters, the projection revision and the last
acknowledged ordinal (1); it asks the daemon from that cursor and takes back a replay,
a refusal, or nothing to do (2); it refuses any patch outside the exact range the
daemon named (3); it applies the gap and lands on the daemon's cursor, or holds the
refusal until an operator accepts the repair and a whole projection is fetched (4);
and it restores the selection by stable id, reporting a selection that is gone rather
than sliding onto a neighbour (5). Step 7 -- a clean load and a replayed projection at
one cursor digest alike -- holds because the replay rebuilds through the daemon's own
projection builder rather than digesting rows here.

Step 6, reconciling outstanding action operations by operation id before retry, has no
producer yet: nothing in epoch 2 issues an operation id the console could hold. It is
a declared hole rather than a step this seam pretends to run.

A count is the other thing the seam answers, because only the seam knows what the link
can vouch for: outside a live and complete projection a count is labelled rather than
stated, and a count whose register could not be read at all renders as unavailable and
never as a zero.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eawf.kernel.projection.compute import KeyedPatch, RouteProjection
from eawf.kernel.projection.connection import (
    READ_METHOD_TEMPLATE,
    RECONNECT_METHOD_TEMPLATE,
    UNAVAILABLE_COUNT,
    ConnectionValue,
    ReconnectDisposition,
    ReconnectNegotiation,
    apply_patches,
    connection_for_disposition,
    connection_value,
    projection_now,
    staleness_target_seconds,
    vouches_for_counts,
)
from eawf.runtime.daemon.methods.state_subscribe import PROJECTION_SUBSCRIBE_METHOD
from eawf.surfaces.tui.state_binding import StateBinding, StateBindingCallbacks

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

#: The label a count carries when the link cannot vouch for completeness. The number
#: is still shown, because a register that was read states something true; what it
#: may not claim is that it holds every row.
KNOWN_COUNT_LABEL = "known"


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
    """

    negotiation: ReconnectNegotiation
    connection: ConnectionValue
    applied: int
    selection_missing: bool


class ProjectionSeam:
    """One console route's link to the daemon projection.

    The seam is constructed with the tree it reads and the callbacks the app wants
    for the epoch-1 legs it shares; it builds the one binding it uses and registers
    itself as that binding's patch sink. It opens no other transport.
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
        **binding_options: Any,
    ) -> None:
        """Build the seam and the one binding that carries it.

        Args:
            route: The console route key this seam reads.
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
            **binding_options: Passed through to the binding, for the poll and probe
                cadences and the client factory a test drives it with.
        """
        self._route = route
        self._scope_id = scope_id
        self._repo_root = repo_root
        self._clock = clock or projection_now
        self._app_state = on_state
        self._app_degraded = on_degraded
        self._projection: RouteProjection | None = None
        self._selected_id: str | None = None
        self._filters: dict[str, str] = {}
        self._connection = ConnectionValue.DISCONNECTED
        self._backstop_ticks = 0
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
        """Return the console route key the seam is bound to."""
        return self._route

    @property
    def connection(self) -> ConnectionValue:
        """Return the link's current value, one of the nine."""
        return self._connection

    @property
    def projection(self) -> RouteProjection | None:
        """Return the projection the console draws; ``None`` before the first load."""
        return self._projection

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

    async def load(self) -> RouteProjection:
        """Read the whole route from the daemon and hold it as the console's rows.

        Returns:
            The projection, at whatever cursor the tree stands at.
        """
        answer = await self._binding.call(
            READ_METHOD_TEMPLATE.format(route=self._route), self._params()
        )
        projection = self._adopt(RouteProjection.model_validate(answer))
        logger.debug(f"load route={self._route} cursor={projection.header.source_cursor}")
        return projection

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
            RECONNECT_METHOD_TEMPLATE.format(route=self._route),
            {**self._params(), "cursor": self.cursor},
        )
        negotiation = ReconnectNegotiation.model_validate(answer["negotiation"])
        patches = tuple(KeyedPatch.model_validate(row) for row in answer["patches"])
        self._connection = connection_for_disposition(negotiation.disposition)
        if negotiation.disposition is ReconnectDisposition.SNAPSHOT_REQUIRED:
            logger.info(
                f"reconnect refused route={self._route} "
                f"gap={negotiation.gap} first_missing={negotiation.first_missing}"
            )
            return self._outcome(negotiation, applied=0)
        if negotiation.disposition is ReconnectDisposition.CURRENT:
            self._adopt(held)
            return self._outcome(negotiation, applied=0)
        gap = negotiation.gap
        assert gap is not None, "a replay always states the range it closes"
        self._refuse_patches_outside(patches, negotiation=negotiation)
        self._adopt(
            apply_patches(
                held,
                patches,
                cursor=gap.last_sequence,
                scope_id=self._scope_id,
                generated_at=self._clock(),
            )
        )
        return self._outcome(negotiation, applied=len(patches))

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
        """Apply one pushed keyed patch to the held projection.

        A patch that does not reach this route is ignored: the feed is one stream
        for every route the console holds. A patch arriving before the first load
        is ignored too, because there are no rows for it to replace.
        """
        held = self._projection
        if held is None or self._route not in patch.routes:
            return
        self._adopt(
            apply_patches(
                held,
                (patch,),
                cursor=patch.canonical_sequence,
                scope_id=self._scope_id,
                generated_at=self._clock(),
            )
        )

    async def connect(self) -> None:
        """Start the one transport: push, probe and the always-on poll backstop."""
        await self._binding.connect()

    async def disconnect(self) -> None:
        """Stop the transport and mark the link as what it then is."""
        await self._binding.disconnect()
        self._connection = ConnectionValue.DISCONNECTED

    def _adopt(self, projection: RouteProjection) -> RouteProjection:
        """Hold *projection* and take the link's value from the header it carries.

        The value is derived rather than asserted: a projection states what its
        producer could vouch for, and a console that overrode that with a live value
        of its own would be claiming something no producer stated.
        """
        self._projection = projection
        self._connection = connection_value(
            connection_state=projection.header.connection_state,
            completeness=projection.header.completeness,
        )
        return projection

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

    def _outcome(self, negotiation: ReconnectNegotiation, *, applied: int) -> ReconnectOutcome:
        """Return the outcome, with the selection restored by stable id."""
        return ReconnectOutcome(
            negotiation=negotiation,
            connection=self._connection,
            applied=applied,
            selection_missing=self._selection_missing(),
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
    "KNOWN_COUNT_LABEL",
    "ProjectionSeam",
    "ReconnectOutcome",
    "SeamCursor",
]
