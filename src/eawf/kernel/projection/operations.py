"""The operations read models: what the sandbox log, the queue and recovery draw.

Three routes answer what the machine is doing on the operator's behalf. ``sandbox.log``
renders the policies every authorisation decision was read against, ``unattended`` the
Runs the dispatch queue holds, and ``crash.recovery`` the Runs that kept going while the
console was away, at the cursor the console has to come back to.

The columns these routes want most are the ones no producer states yet, and they are
declared rather than dropped. The decision, its reason and the policy revision it cites
come from the sandbox-decision record; the queue state and the progress of a queued Run
come from the dispatch-queue projection. Neither producer has shipped, so every one of
those columns renders the unknown truth token naming the item it is waiting on -- which
is a different and more useful answer than a blank cell, and a very different answer from
a zero.
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

#: The item whose producer would state an authorisation decision.
SANDBOX_DECISION_PRODUCER: Final = "RUN-059 SandboxDecision"

#: The item whose producer would state the dispatch queue.
DISPATCH_QUEUE_PRODUCER: Final = "RUN-060 dispatch-queue projection"

#: What each operations route renders per row, in column order. The first field of every
#: route is the status the document states; every other column names the producer it is
#: waiting on, so the frame says which item would fill the cell.
OPERATIONS_FIELDS: Final[Mapping[str, tuple[RouteFieldSpec, ...]]] = MappingProxyType(
    {
        "sandbox.log": status_and(
            unstated("decision", missing_producer=SANDBOX_DECISION_PRODUCER),
            unstated("reason", missing_producer=SANDBOX_DECISION_PRODUCER),
            unstated("policy_revision", missing_producer=SANDBOX_DECISION_PRODUCER),
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
    "OPERATIONS_FIELDS",
    "OPERATIONS_ROUTES",
    "SANDBOX_DECISION_PRODUCER",
    "build_operations_view",
]
