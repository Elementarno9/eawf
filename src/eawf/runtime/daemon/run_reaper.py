"""Failing the Runs no worker will ever end, and freeing the Tasks behind them.

A dispatched Run -- one with a dispatch attempt on the run ledger -- ends only
when the daemon's supervisor ends it, and the supervisor is process memory. A
Run a dead daemon dispatched is left ``QUEUED`` or ``RUNNING`` for good, and a
``run cancel`` does not help, because it records a request only a live worker
answers. So the daemon reaps on start: every open dispatched Run that was
created before the daemon started and that its supervisor does not await is
failed as ``worker-lost``, its workspace lease is withdrawn, and its Task is
handed back to ``PLANNED`` so a later dispatch claims it afresh.

A Run a host session started through ``run start`` has no attempt and is never
reaped: the host drives it and may do so across a daemon restart.
Only Runs older than the daemon are reaped, because a dispatch creates its Run
before it hands the turn to the supervisor, and the start time is what keeps
the reap from racing that window.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Final

from eawf.kernel.runtime.lease import LeaseStatus
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.native_dispatch import run_ledger, standing_attempt, stored_run
from eawf.runtime.daemon.run_supervisor import (
    _CAUSE_WORDS,
    RunEndCause,
    RunSupervisor,
    _fail,
    _move,
    release_lease,
)
from eawf.runtime.workspace.lease import root_leases

logger = logging.getLogger(__name__)

#: The Run statuses a worker still has to end.
_OPEN_STATUSES: Final = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})


def reap_lost_runs(
    context: Epoch2RootContext,
    supervisor: RunSupervisor,
    *,
    actor: PrincipalKey,
    started_at: datetime,
    now: datetime,
) -> tuple[Envelope, ...]:
    """Fail every dispatched open Run older than the daemon that no supervisor awaits.

    The document and the run ledger are read once without a lock to
    find the open dispatched Runs; each is
    then failed under its own transaction, which refuses a Run that moved in
    between. A refused Run is logged and left for the next start, so one Run
    never keeps the others open.

    Args:
        context: The root to reap.
        supervisor: The root's supervisor in this daemon process.
        actor: The principal the reap is attributed to.
        started_at: When this daemon started; a Run created since is left alone.
        now: The stamp every record of the reap shares.

    Returns:
        The envelopes the committed transitions produced.
    """
    document = document_path(context.require_selected_generation())
    rows = document_rows(read_document(document), Epoch2Collection.RUN)
    records = read_ledger_records(ledger_path(document, Epoch2Collection.RUN))
    envelopes: list[Envelope] = []
    for key in sorted(rows):
        run = Run.model_validate(rows[key])
        if run.status not in _OPEN_STATUSES or run.created_at >= started_at:
            continue
        if supervisor.task_of(str(run.urn)) is not None:
            continue
        if standing_attempt(records, run.urn) is None:
            continue
        try:
            envelopes.extend(_reap(context, run, actor=actor, now=now))
        except TransactionRefusedError as refusal:
            logger.warning(f"reap_lost_runs kept run={key!r} code={refusal.code.value}")
    return tuple(envelopes)


def _reap(
    context: Epoch2RootContext, run: Run, *, actor: PrincipalKey, now: datetime
) -> tuple[Envelope, ...]:
    """Fail one lost Run, withdraw its lease and free its Task.

    No edge leads from ``QUEUED`` to ``FAILED``, so a queued Run is taken up
    first, as a refused dispatch's Run is. Failing the Run hands its Task back.

    Raises:
        TransactionRefusedError: The transaction refused a Run edge.
    """
    label = f"{run.key}-reaped"
    envelopes: tuple[Envelope, ...] = ()
    if run.status is RunStatus.QUEUED:
        envelopes = _move(
            context,
            run,
            actor=actor,
            to_status=RunStatus.RUNNING,
            idempotency_key=f"reap-{run.key}-start",
            updates={"started_at": now.isoformat()},
            now=now,
        )
        with context.session([run.urn]) as session:
            run = stored_run(session, read_ledger_records(run_ledger(session)), run.urn)
    envelopes += _fail(
        context,
        run,
        actor=actor,
        attempt_ref=label,
        code=RunEndCause.WORKER_LOST.value,
        words=_CAUSE_WORDS[RunEndCause.WORKER_LOST],
        detail=None,
        now=now,
    )
    for lease in root_leases(context):
        if str(lease.run_ref) == str(run.urn) and lease.status is LeaseStatus.ACTIVE:
            release_lease(context, lease_id=lease.lease_id, now=now)
    logger.info(f"reap_lost_runs failed run={run.key!r}")
    return envelopes


__all__ = ["reap_lost_runs"]
