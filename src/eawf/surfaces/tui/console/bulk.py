"""One confirmed card over many records, sent as one daemon bulk operation.

A card previewing a lifecycle verb over a marked selection names every target, and
confirming it is one operator decision. For a verb the daemon runs as a bulk operation
the console sends it as one: ``runtime.bulk.preview`` first, whose confirmation digest
must be the one the card's own targets and verb confirm to, then ``runtime.bulk.control``
under the card's operation id, anchored at the revisions the card showed. The answer
carries one result per target, and each becomes that target's row; a target the daemon
could not observe reads ``unknown`` and is asked again only through
``runtime.bulk.reconcile`` under the same id, which re-asks exactly the unsettled ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from eawf.kernel.delivery.bulk import BulkItemState, BulkVerb
from eawf.kernel.runtime.control import ControlDisposition
from eawf.surfaces.tui.console.operations import OperationResult, OperationStatus

#: The daemon's bulk verbs, spelled here because importing the daemon's method module
#: registers its handlers in the console's process; a contract test pins the spellings.
BULK_PREVIEW_METHOD: Final = "runtime.bulk.preview"
BULK_CONTROL_METHOD: Final = "runtime.bulk.control"
BULK_RECONCILE_METHOD: Final = "runtime.bulk.reconcile"

#: The lifecycle verbs a marked selection is sent as one bulk operation for, and the bulk
#: verb each is. Every other lifecycle verb is sent target by target.
BULK_METHODS: Final[Mapping[str, BulkVerb]] = MappingProxyType(
    {"domain.task.release": BulkVerb.RELEASE}
)

#: The console outcome each item state reads as. The two vocabularies agree state for
#: state, so an unknown item is never counted as a success or a failure.
ITEM_DISPOSITIONS: Final[Mapping[BulkItemState, ControlDisposition]] = MappingProxyType(
    {
        BulkItemState.REQUESTED: ControlDisposition.REQUESTING,
        BulkItemState.ACCEPTED: ControlDisposition.ACCEPTED,
        BulkItemState.REJECTED: ControlDisposition.REJECTED,
        BulkItemState.CONFIRMED: ControlDisposition.CONFIRMED,
        BulkItemState.UNKNOWN: ControlDisposition.UNKNOWN,
        BulkItemState.INVALIDATED: ControlDisposition.INVALIDATED,
    }
)

#: Where each item disposition leaves the console's ledger.
_STATUSES: Final[Mapping[ControlDisposition, OperationStatus]] = MappingProxyType(
    {
        ControlDisposition.REQUESTING: OperationStatus.OUTSTANDING,
        ControlDisposition.ACCEPTED: OperationStatus.OUTSTANDING,
        ControlDisposition.UNKNOWN: OperationStatus.OUTSTANDING,
        ControlDisposition.REJECTED: OperationStatus.REFUSED,
        ControlDisposition.INVALIDATED: OperationStatus.REFUSED,
        ControlDisposition.CONFIRMED: OperationStatus.APPLIED,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class BulkRequest:
    """A confirmed card's targets, sent as one bulk operation.

    Attributes:
        verb: The bulk verb every target receives.
        targets: The targets' public keys, in the card's order.
        revisions: The revision each target was previewed at, keyed by public key.
        operation_id: The card's id, which the daemon files the operation under.
        reconcile: Ask again for every unsettled target of the operation already opened
            under the id, rather than open it.
    """

    verb: BulkVerb
    targets: tuple[str, ...]
    revisions: Mapping[str, int]
    operation_id: str
    reconcile: bool = False

    def __post_init__(self) -> None:
        """Refuse a request that names no target or leaves one unanchored.

        Raises:
            ValueError: ``targets`` is empty, or a target has no revision.
        """
        if not self.targets:
            raise ValueError("a bulk request names at least one target")
        if set(self.revisions) != set(self.targets):
            raise ValueError("every target of a bulk request carries the revision it was shown at")


def bulk_params(
    request: BulkRequest, urns: Mapping[str, str], *, actor: str, digest: str
) -> dict[str, Any]:
    """Return the ``runtime.bulk.control`` parameters ``request`` opens under.

    Args:
        request: The confirmed targets.
        urns: Each target's canonical address, by public key.
        actor: The principal the operation is attributed to.
        digest: The confirmation digest the preview answered.

    Returns:
        The parameters, anchored at the revisions the card showed.
    """
    return {
        "verb": request.verb.value,
        "item_refs": [urns[key] for key in request.targets],
        "expected_revisions": {urns[key]: request.revisions[key] for key in request.targets},
        "actor": actor,
        "idempotency_key": request.operation_id,
        "confirmation_digest": digest,
    }


def bulk_results(
    request: BulkRequest, urns: Mapping[str, str], answer: Mapping[str, Any]
) -> tuple[OperationResult, ...]:
    """Return one result per target from the operation the daemon answered with.

    Args:
        request: The confirmed targets.
        urns: Each target's canonical address, by public key.
        answer: The operation, as the daemon answered it.

    Returns:
        The results, in the card's order, each carrying its item's own outcome.
    """
    rows = answer["item_results"]
    results: list[OperationResult] = []
    for key in request.targets:
        row = rows[urns[key]]
        disposition = ITEM_DISPOSITIONS[BulkItemState(row["state"])]
        said = " · ".join(part for part in (row.get("code"), row.get("detail")) if part)
        results.append(
            OperationResult(
                operation_id=request.operation_id,
                target=key,
                status=_STATUSES[disposition],
                detail=f"{key} {request.verb.value} {disposition.value}"
                + (f" · {said}" if said else ""),
                disposition=disposition,
            )
        )
    return tuple(results)


def unanswered_bulk(request: BulkRequest, why: str) -> tuple[OperationResult, ...]:
    """Return an ``unknown`` result per target of an operation whose answer never arrived.

    Args:
        request: The confirmed targets.
        why: What the console knows about the missing answer.

    Returns:
        The results; reconcile asks again under the same id.
    """
    return tuple(
        OperationResult(
            operation_id=request.operation_id,
            target=key,
            status=OperationStatus.OUTSTANDING,
            detail=f"{key} {request.verb.value} outcome unknown · {why}",
            disposition=ControlDisposition.UNKNOWN,
        )
        for key in request.targets
    )


def refused_bulk(request: BulkRequest, why: str) -> tuple[OperationResult, ...]:
    """Return a ``rejected`` result per target of an operation the daemon refused whole.

    Args:
        request: The confirmed targets.
        why: The daemon's refusal.

    Returns:
        The results; nothing was opened, so every target stands where it did.
    """
    return tuple(
        OperationResult(
            operation_id=request.operation_id,
            target=key,
            status=OperationStatus.REFUSED,
            detail=f"{key} {request.verb.value} rejected · {why}",
            disposition=ControlDisposition.REJECTED,
        )
        for key in request.targets
    )


__all__ = [
    "BULK_CONTROL_METHOD",
    "BULK_METHODS",
    "BULK_PREVIEW_METHOD",
    "BULK_RECONCILE_METHOD",
    "ITEM_DISPOSITIONS",
    "BulkRequest",
    "bulk_params",
    "bulk_results",
    "refused_bulk",
    "unanswered_bulk",
]
