"""The records and ledger lines the delivery-verb suites are driven on.

A completion is proved by two ledger facts and a set of receipts, and a
Batch merge by a filed read-back. Both suites need the same ones, so they
are built here once from the shared completion world and the transition
registry's seed records rather than written twice.
"""

from __future__ import annotations

from typing import Any, Final

from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods.delivery_completion import PROOF_KEY_PREFIX, FiledProof
from eawf.workflow.integration.reconcile import (
    HostMergeObservation,
    reconcile_merge,
    reconciliation_record_key,
)
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import AT
from tests.integration.workflow.delivery import _completion_fixtures as world

RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
BRANCH: Final = "feature/eawf-v0.7"
EVIDENCE_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"
ENDED_AT: Final = "2026-09-08T02:00:00Z"
FAILURE: Final = {"code": "gate-red", "message": "the decisive gate exited non-zero"}


def landed_line(batch_row: dict[str, Any], *, head_sha: str | None = None) -> LedgerRecord:
    """Return the landed read-back a reconciliation files for a merging Batch.

    Args:
        batch_row: The Batch as it stood while merging; the line is decided
            against its pinned head.
        head_sha: The head the target branch was read at; defaults to the
            Batch's own pinned head, which is what makes the read-back land.

    Returns:
        The Batch-ledger line ``runtime.delivery.reconcile_merge`` files.
    """
    merging = DeliveryBatch.model_validate({**batch_row, "status": "MERGING"})
    assert merging.current_head_binding is not None
    pinned = merging.current_head_binding.head_sha
    head = pinned if head_sha is None else head_sha
    observation = HostMergeObservation(
        batch_ref=merging.urn,
        target_branch=BRANCH,
        observed_at=AT,
        target_head_sha=head,
        contained_shas=(head,) if head == pinned else (head, pinned),
    )
    decision = reconcile_merge(merging, observation)
    return LedgerRecord(
        collection=Epoch2Collection.BATCH,
        record_key=reconciliation_record_key(decision),
        status=decision.outcome.value,
        recorded_at=AT,
        payload=decision.model_dump(mode="json"),
    )


def _receipts() -> tuple[ProofReceipt, ...]:
    """Return the passing receipts that prove the shared Task at its delivery."""
    contracts = world.compiled("CR-01", "CR-02")
    return world.receipts_at(
        contracts, revision_binding=world.delivering_generation().integrated_revision
    )


def proof_line(receipt: ProofReceipt) -> LedgerRecord:
    """Return the receipt-ledger line a proof run files for *receipt*."""
    gate = world.gate(receipt.criterion_ids[0])
    proof = FiledProof(task_ref=world.TASK, gate=gate, receipt=receipt)
    return LedgerRecord(
        collection=Epoch2Collection.RECEIPT,
        record_key=f"{PROOF_KEY_PREFIX}{receipt.freshness_key[:16]}-EAWF-0042",
        status=receipt.result.value,
        recorded_at=AT,
        payload=proof.model_dump(mode="json"),
    )


def completion_params(**assessment: Any) -> dict[str, Any]:
    """Return the request half of a completion the shared world proves.

    Args:
        assessment: Assessment fields to override, such as ``receipts``.

    Returns:
        The ``integrated_commit`` and ``assessment`` wire fields.
    """
    receipts = _receipts()
    fields: dict[str, Any] = {
        "base": world.BASE.model_dump(mode="json"),
        "report_verdict": "pass",
        "gates": [world.gate(item).model_dump(mode="json") for item in ("CR-01", "CR-02")],
        "receipts": [item.model_dump(mode="json") for item in receipts],
        "proof_facts": world.facts().model_dump(mode="json"),
    }
    fields.update(assessment)
    return {"integrated_commit": world.SECOND_HEAD, "assessment": fields}


def completion_lines(*, filed: bool = True) -> tuple[LedgerRecord, ...]:
    """Return the seal, the generation and the filed proofs that deliver the shared Task.

    Args:
        filed: Whether the proof run filed its receipts; off, the receipts
            exist only as the request presents them.
    """
    proofs = tuple(proof_line(item) for item in _receipts()) if filed else ()
    return (
        world.bundle_line(world.bundle_row()),
        world.generation_line(world.delivering_generation()),
        *proofs,
    )


__all__ = [
    "BRANCH",
    "ENDED_AT",
    "EVIDENCE_URN",
    "FAILURE",
    "RUN_URN",
    "completion_lines",
    "completion_params",
    "landed_line",
    "proof_line",
]
