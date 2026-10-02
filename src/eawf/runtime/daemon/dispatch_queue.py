"""State the dispatch queue, and record the operator's control over it.

The queue is read off the selected generation's document and run ledger without a lock,
as every projection read is, and nothing is stored for it: the Runs are the document's
non-terminal Runs reduced by their confirmed control effects, the plan is the governor's
ceiling beside the admitted Runs still live and the dependency edges of the queued Runs'
Tasks, and the verification legs are the progress manifests their runners keep.

A control request is a fact appended to the run ledger under the project lock, beside the
admission receipts it governs, so the admission pass that reads the ledger to mint an
attempt reads the control too. A drain asked while a release is publishing is refused and
recorded refused, because draining then would strand the release's own verification.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from eawf.kernel.economics.governor import AdmissionDecision, InFlightGovernor
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES
from eawf.kernel.runtime.dispatch_queue import (
    DISPATCH_CONTROL_KEY_PREFIX,
    DispatchControlFact,
    DispatchOutcome,
    DispatchPlan,
    DispatchQueueView,
    DispatchVerb,
    ProgressMode,
    QueuedRun,
    QueueEdge,
    VerificationLeg,
    dispatch_control_facts,
    fold_dispatch_control,
)
from eawf.kernel.spec.release import ReleaseStatus
from eawf.kernel.state.epoch2.run import Run, RunStatus, TaskScope
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.control.reducer import reduce_run_control
from eawf.runtime.daemon.admission import in_flight_reservations, latest_receipts
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.native_dispatch import control_facts_of, run_binding_of
from eawf.runtime.verification.progress import (
    LegOutcome,
    ObligationDisposition,
    ProgressManifest,
)
from eawf.runtime.verification.progress import ProgressMode as LegMode

logger = logging.getLogger(__name__)

#: Why a drain is refused while a release publishes.
DRAIN_WHILE_PUBLISHING = (
    "a release is publishing, and draining now would hold the verification it waits on"
)


def _publishing(document: Mapping[str, Any]) -> bool:
    """Return whether any release of the tree is publishing."""
    rows = document_rows(dict(document), Epoch2Collection.RELEASE)
    return any(row.get("status") == ReleaseStatus.PUBLISHING.value for row in rows.values())


def request_dispatch_control(
    session: RootSession,
    records: Sequence[LedgerRecord],
    document: Mapping[str, Any],
    *,
    verb: DispatchVerb,
    actor: str,
    request_ref: str,
    now: datetime,
) -> DispatchControlFact:
    """Record one control request, or answer with the resend it repeats.

    A resend repeats the id and the verb of the request last recorded under that id,
    so it answers with that fact. A different verb under the same id is a new request
    and is recorded, so a pause and then a resume sent under one id both apply.

    Args:
        session: The locked session the fact is appended under.
        records: Every line the run ledger holds.
        document: The selected generation's document.
        verb: What the operator asked.
        actor: Who asked.
        request_ref: The id the request was sent under.
        now: The recording clock.

    Returns:
        The fact as recorded: confirmed, or rejected with its reason.
    """
    prior = [fact for fact in dispatch_control_facts(records) if fact.request_ref == request_ref]
    if prior and prior[-1].verb is verb:
        return prior[-1]
    rejected = verb is DispatchVerb.DRAIN and _publishing(document)
    fact = DispatchControlFact(
        request_ref=request_ref,
        verb=verb,
        actor=actor,
        requested_at=now,
        outcome=DispatchOutcome.REJECTED if rejected else DispatchOutcome.CONFIRMED,
        reason=DRAIN_WHILE_PUBLISHING if rejected else None,
    )
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"{DISPATCH_CONTROL_KEY_PREFIX}{request_ref}-{len(prior) + 1}",
            status=fact.outcome.value,
            recorded_at=now,
            payload=fact.model_dump(mode="json"),
        ),
    )
    logger.info(f"request_dispatch_control verb={verb.value} outcome={fact.outcome.value}")
    return fact


def _live_runs(
    document: Mapping[str, Any], records: tuple[LedgerRecord, ...]
) -> dict[str, tuple[Run, RunStatus]]:
    """Return every Run the document holds with its control-reduced status, by key."""
    held: dict[str, tuple[Run, RunStatus]] = {}
    for key, row in sorted(document_rows(dict(document), Epoch2Collection.RUN).items()):
        run = Run.model_validate(row)
        status = reduce_run_control(status=run.status, facts=control_facts_of(records, run.urn))
        held[key] = (run, status.status)
    return held


def _edges(document: Mapping[str, Any], queued_tasks: Sequence[str]) -> tuple[QueueEdge, ...]:
    """Return the edge of every queued Task waiting on a Task that has not completed."""
    tasks = document_rows(dict(document), Epoch2Collection.TASK)
    edges: list[QueueEdge] = []
    for key in dict.fromkeys(queued_tasks):
        for ref in tasks.get(key, {}).get("depends_on", ()):
            on = parse_qualified_urn(ref).entity_key
            if tasks.get(on, {}).get("status") != TaskStatus.COMPLETED.value:
                edges.append(QueueEdge(waits=key, on=on))
    return tuple(edges)


def _leg(manifest: ProgressManifest) -> VerificationLeg:
    """Return one running leg as its manifest states it."""
    tallies = dict.fromkeys(ObligationDisposition, 0)
    for record in manifest.obligations:
        tallies[record.disposition] += 1
    enumerated = manifest.progress_mode is LegMode.ENUMERATED
    return VerificationLeg(
        gate_id=manifest.leg.gate_id,
        criterion_id=manifest.leg.criterion_id,
        started_at=manifest.liveness.started_at,
        heartbeat_at=manifest.liveness.heartbeat_at,
        budget_seconds=manifest.liveness.resolved_timeout_seconds,
        progress_mode=ProgressMode.ENUMERATED if enumerated else ProgressMode.OPAQUE,
        completed=len(manifest.obligations) if enumerated else None,
        total=len(manifest.collected or ()) if enumerated else None,
        last_completed=manifest.obligations[-1].obligation_id if manifest.obligations else None,
        passed=tallies[ObligationDisposition.PASS],
        failed=tallies[ObligationDisposition.FAIL],
        unknown=tallies[ObligationDisposition.UNKNOWN],
    )


def dispatch_queue_view(
    document: Mapping[str, Any],
    records: Sequence[LedgerRecord],
    *,
    governor: InFlightGovernor | None,
    manifests: Sequence[ProgressManifest],
    now: datetime,
) -> DispatchQueueView:
    """Fold the document, the run ledger and the leg manifests into the dispatch queue.

    Args:
        document: The selected generation's document.
        records: Every line the run ledger holds.
        governor: The governor in force; ``None`` when the economics could not be read,
            which leaves the slots unstated.
        manifests: Every progress manifest the tree's gate runners keep.
        now: The instant the answer is taken at.

    Returns:
        The queue the ``unattended`` route renders.
    """
    ledger = tuple(records)
    runs = _live_runs(document, ledger)
    receipts = latest_receipts(ledger)
    queue: list[QueuedRun] = []
    for run, status in runs.values():
        if status in TERMINAL_RUN_STATUSES:
            continue
        receipt = receipts.get(str(run.urn))
        binding = run_binding_of(ledger, run.urn)
        budget = binding.capsule.budget if binding is not None and binding.capsule else None
        scope = run.scope
        queue.append(
            QueuedRun(
                run_key=run.key,
                task_key=scope.task_ref.entity_key if isinstance(scope, TaskScope) else None,
                state=status.value,
                suspension_reason=run.suspension_reason.value if run.suspension_reason else None,
                admitted_at=(
                    receipt.decided_at
                    if receipt is not None and receipt.decision is AdmissionDecision.ADMITTED
                    else None
                ),
                started_at=run.started_at,
                budget_seconds=budget.wall_seconds if budget is not None else None,
            )
        )

    def status_of(urn: RunUrn) -> RunStatus:
        held = runs.get(urn.entity_key)
        return held[1] if held is not None else RunStatus.CANCELLED

    live = in_flight_reservations(ledger, status_of=status_of, excluding=None)
    # an admitted Run already holds its slot, so only one still waiting can be held back
    waiting = [
        entry.task_key
        for entry in queue
        if entry.state == RunStatus.QUEUED.value
        and entry.admitted_at is None
        and entry.task_key is not None
    ]
    view = DispatchQueueView(
        runs=tuple(queue),
        legs=tuple(
            _leg(manifest) for manifest in manifests if manifest.outcome is LegOutcome.RUNNING
        ),
        plan=DispatchPlan(
            slots=governor.max_concurrent_runs if governor is not None else None,
            in_use=len(live),
            edges=_edges(document, waiting),
        ),
        control=fold_dispatch_control(dispatch_control_facts(ledger)),
        read_at=now,
    )
    logger.debug(
        f"dispatch_queue_view runs={len(view.runs)} legs={len(view.legs)} "
        f"in_use={view.plan.in_use} edges={len(view.plan.edges)}"
    )
    return view


__all__ = [
    "DRAIN_WHILE_PUBLISHING",
    "dispatch_queue_view",
    "request_dispatch_control",
]
