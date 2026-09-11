"""Durable per-gate execution receipts for the close scorer.

A close that runs gates and records only the ones that passed leaves a
verification history nobody can reconstruct: a wave reads as closed with
no answer to "which commands ran, with what argv, and what did they
exit". The durable :class:`~eawf.kernel.store.kinds.gate_receipt.GateReceipt`
covers only the branch where the close carries a complete freshness
context and a surviving proof log; every other executing branch -- the
advisory fleet lane, the daemonless close lane, a gate the runner
blocked before it could produce observations -- persisted nothing.

This module closes that hole with the row shape the rest of the verify
spine already reads: one :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord`
per executed gate, appended to ``evidence.jsonl``. The receipt carries
the gate id, the argv, the exit status and the execution timestamp on
``metrics`` so the store alone answers the question, and repeats the
gate + criterion ids on ``refs`` so the existing per-gate roll-up in
:func:`eawf.workflow.verify.readiness._gate_status_from_evidence` finds
it.

The append happens in the scoring process, from the ``CheckResult`` the
gate child handed back. Nothing here executes a check or claims a
freshness key, so recording a receipt cannot resurrect an in-process
gate run nor collide with the child's own claim.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import (
    EvidenceRecord,
    EvidenceStatus,
    mint_evidence_id,
)
from eawf.kernel.store.paths import store_path

logger = logging.getLogger(__name__)

#: ``metrics`` marker every gate-execution receipt carries, so a reader
#: can separate the per-run receipts from the criterion-level pass rows
#: that share the store.
RECEIPT_MARKER: str = "gate_execution"

#: Cap on the rendered argv stored on the receipt. The argv is bounded
#: in practice (a gate is one command), but ``metrics`` values ride
#: inside a 500-char-summary row family, so the render is truncated
#: rather than trusted to stay short.
ARGV_MAX_CHARS: int = 1000


def gate_execution_receipt(
    *,
    scope_id: str,
    criterion_id: str,
    gate_id: str,
    argv: Sequence[str] | None,
    exit_status: int | None,
    status: EvidenceStatus,
    executed_at: datetime | None = None,
) -> EvidenceRecord:
    """Mint the receipt row for one executed gate.

    Args:
        scope_id: Wave URN the gate scored.
        criterion_id: Criterion the gate was attached to.
        gate_id: Id of the gate that ran.
        argv: The argv the runner executed. ``None`` for a gate kind
            that runs no subprocess; the receipt then renders an empty
            argv rather than omitting the key, so every receipt has the
            same shape.
        exit_status: Process exit status, or ``None`` when the runner
            never reached a process (a blocked or crashed execution).
        status: The closed outcome word for the run.
        executed_at: When the run ended. Defaults to now, which is the
            correct stamp for a receipt minted straight off the result.

    Returns:
        A validated :class:`EvidenceRecord` ready for the store append.
    """
    rendered_argv = " ".join(argv or ())[:ARGV_MAX_CHARS]
    stamp = executed_at or datetime.now(UTC)
    exit_render = "none" if exit_status is None else str(exit_status)
    summary = (
        f"gate {gate_id} executed for criterion {criterion_id}: "
        f"status={status} exit_status={exit_render} argv={rendered_argv}"
    )[:500]
    return EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=scope_id,
        produced_by="tool",
        evidence_kind="deterministic",
        status=status,
        summary=summary,
        refs=[gate_id, criterion_id],
        metrics={
            "receipt": RECEIPT_MARKER,
            "criterion_id": criterion_id,
            "gate_id": gate_id,
            "argv": rendered_argv,
            "exit_status": exit_render,
            "executed_at": stamp.isoformat(),
        },
        created_at=stamp,
    )


def append_gate_execution_receipt(state_path: Path, receipt: EvidenceRecord) -> None:
    """Append one gate-execution receipt to the scope's evidence store.

    Args:
        state_path: Path to ``state.json``; anchors
            ``<state_dir>/store/evidence.jsonl``.
        receipt: The row minted by :func:`gate_execution_receipt`.
    """
    append_envelope(
        store_path(state_path, StoreKind.EVIDENCE),
        Envelope(
            id=receipt.id,
            kind=StoreKind.EVIDENCE,
            scope_id=receipt.scope_id,
            created_at=receipt.created_at,
            summary=receipt.summary,
            payload=receipt.model_dump(mode="json"),
        ),
    )
    logger.info(
        f"append_gate_execution_receipt scope_id={receipt.scope_id!r} "
        f"evidence_id={receipt.id!r} status={receipt.status!r}"
    )


__all__ = [
    "ARGV_MAX_CHARS",
    "RECEIPT_MARKER",
    "append_gate_execution_receipt",
    "gate_execution_receipt",
]
