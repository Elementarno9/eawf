"""The register read models: what Activity, Attention, Notifications and Cost ceiling draw.

:mod:`~eawf.kernel.projection.spine` states the five spine routes' read models. This module
states the four register routes', and one rule separates the two. A spine route counts
every collection it binds, because a bound collection that holds nothing really does hold
nothing. A register route may bind a collection *no epoch-2 producer writes at all*, and
those two silences are not the same thing: a register that was read and held nothing counts
zero, while a register nothing writes states no count and comes back as an unknown
:class:`~eawf.kernel.projection.truth.TruthField` naming why. Counting the second as zero
would tell an operator that nothing needs them when in truth nothing has been asked yet.

:data:`UNWRITTEN_COLLECTIONS` is where that distinction is declared, and deleting a row from
it is the whole change the arrival of a producer needs here.

The budget reading is the other thing stated here. A Run that crossed its cap is recorded
as a :class:`~eawf.kernel.runtime.budget_notice.BudgetNotice` on that Run's ledger, so
Cost ceiling and Notifications read the notice's own contract -- which control a budget
termination opens, which status it leaves behind, and whether the crossing interrupts
anybody -- rather than restating those three as frame literals that could drift from it.
*Which* Runs a notice stopped is not a document row at all, so that part of the reading
comes back unknown naming why, and neither route draws a run as budget-stopped on the
strength of its status alone.

Nothing here reads a document or a lock. The input is one already-validated
:class:`~eawf.kernel.projection.compute.RouteProjection`, so a register view is a pure
function of the projection the daemon served.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.compute import (
    PROJECTION_PRODUCER,
    ROUTE_COLLECTIONS,
    ROUTE_READ_MODELS,
    ProjectionRow,
    RouteProjection,
)
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.projection.spine import SPINE_ROUTES
from eawf.kernel.projection.truth import (
    Completeness,
    Freshness,
    Precision,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.runtime.budget_notice import (
    BUDGET_TERMINATION_CONTROL,
    BUDGET_TERMINATION_STATUS,
    BudgetNotice,
)
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)


#: The console route whose register answers "how many things need me". The header prints
#: its count on every route, so it is named once here and derived nowhere else.
ATTENTION_ROUTE: Final = "attention"

#: The two routes that render the budget reading: one observes the ceiling, the other says
#: whether crossing it may interrupt. Both read the same notice contract.
COST_CEILING_ROUTE: Final = "cost.ceiling"
NOTIFICATIONS_ROUTE: Final = "notifications"

#: The console routes this module states a read model for. Every one is bound by
#: :data:`~eawf.kernel.projection.compute.ROUTE_COLLECTIONS`, so every one is served by
#: ``projection.<route>.read`` and by ``projection.<route>.reconnect``.
REGISTER_ROUTES: Final[tuple[str, ...]] = (
    "activity",
    ATTENTION_ROUTE,
    COST_CEILING_ROUTE,
    NOTIFICATIONS_ROUTE,
)

#: The collections nothing in epoch 2 writes yet. A route binding one of these reads a
#: register that is empty because it has no producer, which a console must not draw as a
#: register that was read and found quiet.
UNWRITTEN_COLLECTIONS: Final[frozenset[Epoch2Collection]] = frozenset(
    {Epoch2Collection.PENDING_ACTION}
)

#: Why a bound register states no count. The console prints the unknown truth token in the
#: count's place; this is what an operator reads when asking why it is not a number.
UNWRITTEN_REASON: Final = "no epoch-2 producer writes this register yet"

#: Why the budget reading cannot name the Runs a cap stopped. The notice lives on the
#: Run's ledger and a route projection carries the committed document, so the crossing is
#: outside what this view was built from.
BUDGET_UNSTATED_REASON: Final = "the budget notice is a run-ledger record, not a document row"

#: The notice field that decides whether a crossing interrupts the operator. Read by name
#: so renaming it breaks the console loudly rather than leaving it asserting the old answer.
_BLOCKING_FIELD: Final = "blocking"


def _check_declarations() -> None:
    """Refuse a register table the console could not render.

    Raises:
        ValueError: A register route binds no collection, so no projection serves it; or a
            route is claimed by both this module and the spine, which would leave two
            read models drawing one route. Raised at import, because a route that cannot
            be rendered is a startup failure rather than a frame that draws the wrong thing.
    """
    defects = [
        f"route {route!r} binds no collection, so no projection serves it"
        for route in REGISTER_ROUTES
        if route not in ROUTE_COLLECTIONS
    ]
    defects += [
        f"route {route!r} is both a register route and a spine route"
        for route in REGISTER_ROUTES
        if route in SPINE_ROUTES
    ]
    if defects:
        raise ValueError(f"register declarations are invalid: {'; '.join(defects)}")


_check_declarations()


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterView:
    """One register route's read model, as the console draws it.

    Attributes:
        route: The console route key the rows were gathered for.
        read_model: The declared read model the route renders.
        scope_id: The scope the projection was built for.
        source_cursor: The committed ``canonical_sequence`` the rows were read through,
            carried verbatim from the projection header.
        digest: The projection's digest, which one cursor yields once.
        complete: Whether the projection claimed every row of its scope, and so whether a
            count taken from it may be called complete.
        rows: The records, in the projection's own order.
        counts: The rows per *written* collection the route binds, keyed by collection
            name. A collection nothing writes has no entry, which is how a register with
            no producer is told apart from a register that was read and held nothing.
        withheld: The bound collections nothing writes, in binding order. The console
            prints the unknown token for each instead of a zero.
    """

    route: str
    read_model: ReadModelKind
    scope_id: str
    source_cursor: str
    digest: str
    complete: bool
    rows: tuple[ProjectionRow, ...]
    counts: Mapping[str, int]
    withheld: tuple[str, ...]

    def count(self, name: str) -> int | None:
        """Return the derived count of ``name``, or ``None`` when no register holds it."""
        return self.counts.get(name)

    def index_of(self, selected_id: str | None) -> int | None:
        """Return the row position ``selected_id`` names now, or ``None`` when it is gone.

        This is what restores a selection after a keyed patch reorders the rows: the
        console persists the stable id, never the offset, so an insert above the selection
        moves the cursor with the row rather than onto its neighbour.
        """
        if selected_id is None:
            return None
        for index, row in enumerate(self.rows):
            if row.key == selected_id:
                return index
        return None

    def status_counts(self) -> Mapping[str, int]:
        """Return the rows per stated status, in the order the statuses were first seen.

        A row whose status is unknown is counted under no status at all. A bucket count is
        a count of the rows that stated the bucket, and filing an unstated row under one
        would make the buckets claim more than the rows said.
        """
        tally: dict[str, int] = {}
        for row in self.rows:
            stated = _stated_status(row)
            if stated is not None:
                tally[stated] = tally.get(stated, 0) + 1
        return MappingProxyType(tally)

    def unstated_rows(self) -> int:
        """Return how many rows state no status, and so fall in no bucket."""
        return sum(1 for row in self.rows if _stated_status(row) is None)


@dataclass(frozen=True, slots=True, kw_only=True)
class BudgetReading:
    """What a Run crossing its cap states, as the two budget routes render it.

    Attributes:
        control: The control a budget termination opens on the over-cap Run.
        terminal_status: The Run status a confirmed budget termination leaves behind.
        interrupts: Whether the crossing interrupts the operator. Read off the notice
            rather than declared here, because the notice is what decides it.
        stopped: The Runs a notice stopped. Unknown while the notice is a ledger record,
            naming that as the reason, so neither route promotes a status into a claim
            about why a Run ended.
    """

    control: str
    terminal_status: str
    interrupts: bool
    stopped: TruthField[str]


def _stated_status(row: ProjectionRow) -> str | None:
    """Return the row's status when the document stated one, else ``None``."""
    status = row.status
    if status.state is not TruthState.KNOWN or not status.value:
        return None
    return status.value


def _unknown(*, reason: str, revision: int, refs: tuple[str, ...]) -> TruthField[str]:
    """Return the truth field a register or a reading with no producer comes back as."""
    return TruthField[str](
        value=None,
        state=TruthState.UNKNOWN,
        truth_kind=TruthKind.DERIVED,
        producer=PROJECTION_PRODUCER,
        producer_revision=revision,
        precision=Precision.UNAVAILABLE,
        measurement_quality=MeasurementQuality.UNAVAILABLE,
        freshness=Freshness.LIVE,
        provenance_refs=refs,
        missing_reason=reason,
    )


def _known(*, value: str, revision: int, refs: tuple[str, ...]) -> TruthField[str]:
    """Return the truth field a count derived from the projection's own rows comes back as."""
    return TruthField[str](
        value=value,
        state=TruthState.KNOWN,
        truth_kind=TruthKind.DERIVED,
        producer=PROJECTION_PRODUCER,
        producer_revision=revision,
        precision=Precision.EXACT,
        measurement_quality=MeasurementQuality.EXACT,
        freshness=Freshness.LIVE,
        provenance_refs=refs,
    )


def _revision_of(view: RegisterView) -> int:
    """Return the producer revision a derived field of ``view`` is stated at.

    The cursor is the revision: two views built at one cursor state one answer, and the
    offset keeps an empty workspace's first view at the lowest revision a field admits.
    """
    return int(view.source_cursor) + 1


def build_register_view(projection: RouteProjection) -> RegisterView:
    """Return the read model a register route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.

    Returns:
        The route's rows, the counts of every register it binds that something writes, and
        the names of the registers nothing writes yet.

    Raises:
        ValueError: The projection is for a route this module states no read model for. A
            register frame drawn from another route's rows would be showing one route's
            records under another's heading.
    """
    route = projection.route
    if route not in REGISTER_ROUTES:
        stated = ", ".join(REGISTER_ROUTES)
        raise ValueError(
            f"route {route!r} has no register read model, so its projection states no "
            f"register rows; register routes: {stated}"
        )
    bound = ROUTE_COLLECTIONS[route]
    written = tuple(c for c in bound if c not in UNWRITTEN_COLLECTIONS)
    rows = tuple(row for row in projection.rows if row.collection in written)
    counts = MappingProxyType(
        {
            collection.value: sum(1 for row in rows if row.collection is collection)
            for collection in written
        }
    )
    logger.debug(f"build_register_view route={route} cursor={projection.header.source_cursor}")
    return RegisterView(
        route=route,
        read_model=ROUTE_READ_MODELS[route],
        scope_id=projection.header.scope_id,
        source_cursor=projection.header.source_cursor,
        digest=projection.digest,
        complete=projection.header.completeness is Completeness.COMPLETE,
        rows=rows,
        counts=counts,
        withheld=tuple(c.value for c in bound if c in UNWRITTEN_COLLECTIONS),
    )


def attention_mine(view: RegisterView) -> TruthField[str]:
    """Return the count of actions addressed to this principal, as a truth field.

    The header's ``!N`` and the Attention route's own ``mine`` row both read this, so the
    two cannot state different numbers. A pending action is addressed to the operator of
    the scope it was raised in, so every row of the scope's register is one of this
    principal's; a per-principal producer would filter here rather than beside the header.

    Args:
        view: The Attention route's read model.

    Returns:
        The count when a producer writes the register, and the unknown state naming why
        when none does -- never a zero standing in for a register nobody writes.

    Raises:
        ValueError: ``view`` is another route's read model, whose rows are not actions.
    """
    if view.route != ATTENTION_ROUTE:
        raise ValueError(
            f"route {view.route!r} states no attention count; the count is the "
            f"{ATTENTION_ROUTE!r} route's register"
        )
    revision = _revision_of(view)
    if view.withheld:
        return _unknown(
            reason=UNWRITTEN_REASON,
            revision=revision,
            refs=tuple(f"{view.scope_id}:{name}" for name in view.withheld),
        )
    return _known(
        value=str(len(view.rows)),
        revision=revision,
        refs=tuple(row.urn for row in view.rows) or (view.scope_id,),
    )


def notice_interrupts() -> bool:
    """Return whether a budget notice interrupts the operator, read off the notice itself.

    Raises:
        KeyError: The notice no longer carries the field that decides it, so nothing here
            could answer the question honestly.
    """
    return bool(BudgetNotice.model_fields[_BLOCKING_FIELD].get_default())


def budget_reading(view: RegisterView) -> BudgetReading:
    """Return what a Run crossing its cap states, for the two routes that render it.

    Args:
        view: The Cost ceiling or Notifications read model.

    Returns:
        The control a budget termination opens, the status it leaves, whether the crossing
        interrupts anybody, and the unknown field standing where the stopped Runs would be.

    Raises:
        ValueError: ``view`` is a route that does not render the budget reading.
    """
    if view.route not in (COST_CEILING_ROUTE, NOTIFICATIONS_ROUTE):
        raise ValueError(
            f"route {view.route!r} renders no budget reading; the reading is "
            f"{COST_CEILING_ROUTE!r} and {NOTIFICATIONS_ROUTE!r}"
        )
    return BudgetReading(
        control=BUDGET_TERMINATION_CONTROL.value,
        terminal_status=BUDGET_TERMINATION_STATUS.value,
        interrupts=notice_interrupts(),
        stopped=_unknown(
            reason=BUDGET_UNSTATED_REASON,
            revision=_revision_of(view),
            refs=(view.scope_id,),
        ),
    )


__all__ = [
    "ATTENTION_ROUTE",
    "BUDGET_UNSTATED_REASON",
    "COST_CEILING_ROUTE",
    "NOTIFICATIONS_ROUTE",
    "REGISTER_ROUTES",
    "UNWRITTEN_COLLECTIONS",
    "UNWRITTEN_REASON",
    "BudgetReading",
    "RegisterView",
    "attention_mine",
    "budget_reading",
    "build_register_view",
    "notice_interrupts",
]
