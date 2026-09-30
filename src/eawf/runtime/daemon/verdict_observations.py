"""The verdict observations Trust lists: each audit verdict a Batch's current cycle holds.

A Batch's verification cycle is filed on the Batch ledger, one line per pass, and the
newest line is the cycle at the head the Batch delivers now. Every audit that line holds
is one independent verdict on one criterion of that Batch, reached by one reviewer Run.
Trust lists each as a verdict observation keyed by site, subject and producer, where the
producer is the reviewer's identity as ``(agent_role, runtime)``: the role its capsule
was sealed with and the harness its vendor session ran under, never a runner's name.

Nothing is stored here. The rows are read off the ledgers each time a projection is
built, so the Milestone a verdict is listed under holds no verdict of its own.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from eawf.kernel.delivery.batch_proof import BatchAudit, BatchVerificationCycle
from eawf.kernel.projection.compute import VERDICT_OBSERVATION_KIND
from eawf.kernel.runtime.control import RunBinding
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerRecord, effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods.delivery import CYCLE_KEY_PREFIX

logger = logging.getLogger(__name__)

#: The site a Batch audit verdict is reached at.
VERIFICATION_SITE: Final = "verification"


def _stored(
    document: dict[str, Any], records: tuple[LedgerRecord, ...], collection: Epoch2Collection
) -> dict[str, dict[str, Any]]:
    """Return a collection's records by key: the document's, else the ledger's newest."""
    rows = {
        item.record_key: item.payload
        for item in effective_records(records)
        if "payload_kind" not in item.payload and item.payload.get("key") == item.record_key
    }
    rows.update(
        (key, row)
        for key, row in document_rows(document, collection).items()
        if isinstance(row, dict)
    )
    return rows


def _roles(records: tuple[LedgerRecord, ...]) -> Mapping[str, str]:
    """Return the agent role each bound Run's capsule was sealed with, by Run key."""
    roles: dict[str, str] = {}
    for item in records:
        if item.payload.get("payload_kind") != "run_binding":
            continue
        binding = RunBinding.model_validate(item.payload)
        if binding.capsule is not None:
            roles[binding.run_ref.entity_key] = binding.capsule.agent_role.value
    return roles


def _current_cycles(records: tuple[LedgerRecord, ...]) -> tuple[BatchVerificationCycle, ...]:
    """Return each Batch's newest verification cycle, in the order the Batches were first cycled."""
    newest: dict[str, dict[str, Any]] = {}
    for item in records:
        if item.record_key.startswith(CYCLE_KEY_PREFIX):
            newest[item.record_key] = item.payload
    return tuple(BatchVerificationCycle.model_validate(payload) for payload in newest.values())


def _row(
    audit: BatchAudit, *, milestone_ref: str | None, role: str | None, runtime: str | None
) -> dict[str, Any]:
    """Return one audit verdict as the notice row Trust lists."""
    return {
        "payload_kind": VERDICT_OBSERVATION_KIND,
        "key": f"{audit.batch_ref.entity_key}-{audit.id}",
        "urn": str(audit.batch_ref),
        "revision": 1,
        "status": audit.verdict.value,
        "site": VERIFICATION_SITE,
        "subject": audit.criterion_id,
        "batch_ref": str(audit.batch_ref),
        "milestone_ref": milestone_ref,
        "verdict": audit.verdict.value,
        "agent_role": role,
        "runtime": runtime,
        "occurred_at": audit.recorded_at.isoformat(),
    }


def verdict_observation_rows(
    document_file: Path, document: dict[str, Any]
) -> tuple[dict[str, Any], ...]:
    """Return every audit verdict the Batches' current cycles hold, as notice rows.

    Args:
        document_file: The selected generation's document, which the ledgers sit beside.
        document: That document, as read.

    Returns:
        One row per audit of each Batch's newest cycle, with the Milestone its Batch is
        filed under and the reviewer's role and runtime; a part no record states is
        ``None``, which Trust renders unknown.

    Raises:
        pydantic.ValidationError: A cycle, binding or Run line does not validate, which
            means the ledger is corrupt.
    """
    batch_lines = read_ledger_records(ledger_path(document_file, Epoch2Collection.BATCH))
    run_lines = read_ledger_records(ledger_path(document_file, Epoch2Collection.RUN))
    batches = _stored(document, batch_lines, Epoch2Collection.BATCH)
    runs = _stored(document, run_lines, Epoch2Collection.RUN)
    roles = _roles(run_lines)
    rows: list[dict[str, Any]] = []
    for cycle in _current_cycles(batch_lines):
        milestone = batches.get(cycle.batch_ref.entity_key, {}).get("milestone_ref")
        for audit in cycle.audits:
            reviewer = audit.review.run_ref.entity_key
            stored = runs.get(reviewer)
            session = Run.model_validate(stored).vendor_session if stored is not None else None
            rows.append(
                _row(
                    audit,
                    milestone_ref=milestone if isinstance(milestone, str) else None,
                    role=roles.get(reviewer),
                    runtime=session.harness if session is not None else None,
                )
            )
    logger.debug(f"verdict_observation_rows verdicts={len(rows)}")
    return tuple(rows)


__all__ = ["VERIFICATION_SITE", "verdict_observation_rows"]
