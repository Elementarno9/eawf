"""The operations read models: what the sandbox log, the queue and recovery draw.

Three routes answer what the machine is doing on the operator's behalf. ``sandbox.log``
renders every authorisation decision the gateway made, allowed and denied, beside the
policies the document holds, ``unattended`` the
Runs the dispatch queue holds, and ``crash.recovery`` the Runs that kept going while the
console was away, at the cursor the console has to come back to.

A decision's columns are the facts its sandbox-decision record states: the outcome, the
Run, the reason, the rule that decided with its value in force, and the policy revision it
cites. A fact a row does not state -- a decision whose revision cannot be read, or any
decision column on a policy row -- renders the unknown truth token saying so, never a
guess. The queue state and the progress of a Run are not columns of these rows: the route
reads them from the daemon's dispatch-queue read beside the projection, so these columns
render the unknown token naming it until that read arrives -- a different and more useful
answer than a blank cell, and a very different answer from a zero.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.route_view import (
    RouteFieldSpec,
    RouteReadModel,
    build_route_read_model,
    check_field_tables,
    stated,
    status_and,
    unstated,
)

logger = logging.getLogger(__name__)

#: The family name a refusal from this module names.
FAMILY: Final = "operations"

#: The console routes this module states a read model for. Every one is bound by
#: :data:`~eawf.kernel.projection.compute.ROUTE_COLLECTIONS`, so every one is served by
#: ``projection.<route>.read`` and by ``projection.<route>.reconnect``.
OPERATIONS_ROUTES: Final[tuple[str, ...]] = ("sandbox.log", "unattended", "crash.recovery")

#: The record every authorisation decision is read from.
SANDBOX_DECISION_PRODUCER: Final = "the sandbox-decision record"

#: Why a decision's policy revision reads unknown: the row states none that can be read.
UNREADABLE_REVISION: Final = "the policy revision this decision cites cannot be read"

#: Why a decision column reads unknown on a row that is not a decision.
NOT_A_DECISION: Final = "this row is a policy, not a decision"

#: What states the dispatch queue's progress, plan and control beside the projection.
DISPATCH_QUEUE_PRODUCER: Final = "the daemon's dispatch-queue read"

#: What each operations route renders per row, in column order. The first field of every
#: route is the status the document states; every other column names the producer it is
#: waiting on, so the frame says which item would fill the cell.
OPERATIONS_FIELDS: Final[Mapping[str, tuple[RouteFieldSpec, ...]]] = MappingProxyType(
    {
        "sandbox.log": status_and(
            stated("decision", absent=NOT_A_DECISION),
            stated("run", absent=NOT_A_DECISION),
            stated("reason", absent=NOT_A_DECISION),
            stated("rule", absent=NOT_A_DECISION),
            stated("rule_value", absent=NOT_A_DECISION),
            stated("policy_revision", absent=UNREADABLE_REVISION),
            stated("decided_at", absent=NOT_A_DECISION),
        ),
        "unattended": status_and(
            unstated("queue_state", missing_producer=DISPATCH_QUEUE_PRODUCER),
            unstated("progress", missing_producer=DISPATCH_QUEUE_PRODUCER),
        ),
        "crash.recovery": status_and(unstated("door"), unstated("cost")),
    }
)


check_field_tables(family=FAMILY, routes=OPERATIONS_ROUTES, fields=OPERATIONS_FIELDS)


def build_operations_view(projection: RouteProjection) -> RouteReadModel:
    """Return the read model one operations route draws from one served projection.

    Args:
        projection: The route projection the daemon answered, already validated.

    Returns:
        The route's rows with every declared field stated, and the counts derived from
        those rows.

    Raises:
        ValueError: The projection is for a route this module states no read model for.
    """
    model = build_route_read_model(projection, family=FAMILY, fields=OPERATIONS_FIELDS)
    logger.debug(f"build_operations_view route={model.route} rows={len(model.rows)}")
    return model


__all__ = [
    "DISPATCH_QUEUE_PRODUCER",
    "FAMILY",
    "NOT_A_DECISION",
    "OPERATIONS_FIELDS",
    "OPERATIONS_ROUTES",
    "SANDBOX_DECISION_PRODUCER",
    "UNREADABLE_REVISION",
    "build_operations_view",
]
