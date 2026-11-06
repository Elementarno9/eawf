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

A host may also simply walk away from its Run, and a dispatched Run can outlive
every daemon that would reap it. So any open Run that recorded no run event and
took no transition for :data:`ABANDON_AFTER`, and that no supervisor awaits, is
failed as ``stalled-abandoned`` the same way, Task and lease included. That
sweep runs on start and on the stall loop's cadence; it decides every Run from
one unlocked read, so a pass that ends nothing takes no lock at all. A Run it
ends is read again under its lock before each write, so one that recorded an
event or moved since the read is left open with nothing written on it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from pydantic import ValidationError

from eawf.kernel.runtime.events import ErrorPayload, RunEventKind, RunEventRecord
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.run_index import indexed_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.control.reducer import reduce_run_control
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.native_dispatch import (
    control_facts_of,
    run_ledger,
    standing_attempt,
    stored_run,
)
from eawf.runtime.daemon.run_events import run_events_by_run, run_events_of
from eawf.runtime.daemon.run_supervisor import (
    _CAUSE_WORDS,
    RunEndCause,
    RunSupervisor,
    _event_ref,
    _fail_edge,
    _move,
    _state_in_session,
    release_run_leases,
)

logger = logging.getLogger(__name__)

#: The Run statuses a worker still has to end.
_OPEN_STATUSES: Final = frozenset({RunStatus.QUEUED, RunStatus.RUNNING})

#: How long an open Run may record nothing before it counts as abandoned. A live
#: worker or host session records an event far more often than this, and a day
#: outlasts any one sitting an operator would come back to.
ABANDON_AFTER: Final = timedelta(hours=24)


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
    records = indexed_ledger_records(ledger_path(document, Epoch2Collection.RUN))
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
            envelopes.extend(
                _reap(
                    context,
                    run,
                    cause=RunEndCause.WORKER_LOST,
                    start_key=f"reap-{run.key}-start",
                    attempt_ref=f"{run.key}-reaped",
                    actor=actor,
                    now=now,
                )
            )
        except TransactionRefusedError as refusal:
            logger.warning(f"reap_lost_runs kept run={key!r} code={refusal.code.value}")
    return tuple(envelopes)


def end_abandoned_runs(
    context: Epoch2RootContext,
    supervisor: RunSupervisor,
    *,
    actor: PrincipalKey,
    now: datetime,
) -> tuple[Envelope, ...]:
    """Fail every open Run silent for :data:`ABANDON_AFTER` that no supervisor awaits.

    A Run's silence runs from its last run event or its last transition,
    whichever is later; a Run with neither is measured from its creation,
    which its first stored revision stamps. The document and the run ledger
    are read once without a lock and every Run is decided from that read, so
    only a Run that is ending takes the lock, through its own transitions,
    and a pass with nothing to end takes none. Each ending is refused by the
    transaction if the Run moved since the read; a refused Run, like a row
    that does not read as a Run, is logged and left for the next pass, so one
    Run never keeps the others open.

    Args:
        context: The root to sweep.
        supervisor: The root's supervisor in this daemon process; a Run it is
            launching or awaiting is never ended here.
        actor: The principal the endings are attributed to.
        now: The recording clock, which decides what has gone quiet, and the
            stamp every record of the sweep shares.

    Returns:
        The envelopes the committed transitions produced; empty when no Run
        was abandoned.
    """
    document = document_path(context.require_selected_generation())
    rows = document_rows(read_document(document), Epoch2Collection.RUN)
    open_rows = {
        key: row
        for key, row in rows.items()
        if row.get("status") in {status.value for status in _OPEN_STATUSES}
    }
    if not open_rows:
        return ()
    records = indexed_ledger_records(ledger_path(document, Epoch2Collection.RUN))
    events = run_events_by_run(records, run_keys=open_rows.keys())
    envelopes: list[Envelope] = []
    for key in sorted(open_rows):
        try:
            run = Run.model_validate(open_rows[key])
        except ValidationError as error:
            logger.warning(f"end_abandoned_runs skipped run={key!r} errors={error.error_count()}")
            continue
        run_events = events.get(run.urn, ())
        if not _abandoned(run, records, run_events, supervisor, now=now):
            continue
        try:
            envelopes.extend(
                _reap(
                    context,
                    run,
                    cause=RunEndCause.STALLED_ABANDONED,
                    start_key=f"abandon-{run.key}-start",
                    attempt_ref=f"{run.key}-abandoned",
                    actor=actor,
                    now=now,
                    silence=_Silence(since=_last_activity(run, run_events), supervisor=supervisor),
                )
            )
        except TransactionRefusedError as refusal:
            logger.warning(f"end_abandoned_runs kept run={key!r} code={refusal.code.value}")
    return tuple(envelopes)


def _abandoned(
    run: Run,
    records: tuple[LedgerRecord, ...],
    events: tuple[RunEventRecord, ...],
    supervisor: RunSupervisor,
    *,
    now: datetime,
) -> bool:
    """Return whether an open Run has been silent past the threshold with nothing awaiting it."""
    if now - _last_activity(run, events) <= ABANDON_AFTER or supervisor.busy(str(run.urn)):
        return False
    # a confirmed cancel or suspend is that control's ending to finish, not this sweep's
    reduced = reduce_run_control(status=run.status, facts=control_facts_of(records, run.urn))
    return reduced.status in _OPEN_STATUSES


def _last_activity(run: Run, events: tuple[RunEventRecord, ...]) -> datetime:
    """Return when the Run last recorded an event or took a transition, whichever is later."""
    return max((run.updated_at, *(event.recorded_at for event in events)))


@dataclass(frozen=True, slots=True)
class _Silence:
    """What an abandonment was decided on, checked again under the Run's lock before it ends.

    Attributes:
        since: The Run's last activity as the unlocked read saw it.
        supervisor: The root's supervisor, which may have taken the Run up since.
    """

    since: datetime
    supervisor: RunSupervisor

    def holds(self, run: Run, records: tuple[LedgerRecord, ...], *, revision: int) -> bool:
        """Return whether the locked *run* is still the silent Run the sweep decided on.

        A run event leaves the revision where it was, so the stream is read for a
        line recorded since; any transition moves the revision.

        Args:
            run: The Run as the locked session reads it.
            records: The run ledger as the locked session reads it.
            revision: The revision the Run stands at if nothing but this
                ending touched it.
        """
        if run.revision != revision or self.supervisor.busy(str(run.urn)):
            return False
        if any(event.recorded_at > self.since for event in run_events_of(records, run.urn)):
            return False
        reduced = reduce_run_control(status=run.status, facts=control_facts_of(records, run.urn))
        return reduced.status in _OPEN_STATUSES


def _reap(
    context: Epoch2RootContext,
    run: Run,
    *,
    cause: RunEndCause,
    start_key: str,
    attempt_ref: str,
    actor: PrincipalKey,
    now: datetime,
    silence: _Silence | None = None,
) -> tuple[Envelope, ...]:
    """Fail one Run nothing will end, withdraw its lease and free its Task.

    No edge leads from ``QUEUED`` to ``FAILED``, so a queued Run is taken up
    first, as a refused dispatch's Run is. Failing the Run hands its Task back.
    The failure line is stated in the same locked session that reads the Run
    the failing edge is taken against, and only while the Run stands at the
    revision it was read at, so the edge is refused only if another writer
    moved the Run between that session and the transaction's.

    Args:
        context: The root the Run lives in.
        run: The open Run, as last read.
        cause: Why the Run ends, its failure code.
        start_key: The idempotency key of a queued Run's start.
        attempt_ref: What names the ending's line and transition.
        actor: The principal the ending is attributed to.
        now: The stamp every record of the ending shares.
        silence: What an abandonment was decided on; when given, a Run that
            recorded an event or moved since is left open, checked under its
            lock before each write.

    Returns:
        The envelopes the committed transitions produced; without the failing
        edge's when the Run moved or woke before its failure line was written.

    Raises:
        TransactionRefusedError: The transaction refused a Run edge.
    """
    envelopes: tuple[Envelope, ...] = ()
    revision = run.revision
    if run.status is RunStatus.QUEUED:
        if silence is not None:
            with context.session([run.urn]) as session:
                records = read_ledger_records(run_ledger(session))
                held = silence.holds(
                    stored_run(session, records, run.urn), records, revision=revision
                )
            if not held:
                logger.info(f"_reap kept run={run.key!r}: it woke since the sweep read it")
                return ()
        envelopes = _move(
            context,
            run,
            actor=actor,
            to_status=RunStatus.RUNNING,
            idempotency_key=start_key,
            updates={"started_at": now.isoformat()},
            now=now,
        )
        revision += 1
    message = _CAUSE_WORDS[cause]
    event_ref = _event_ref(attempt_ref, f"ended:{cause.value}")
    with context.session([run.urn]) as session:
        records = read_ledger_records(run_ledger(session))
        run = stored_run(session, records, run.urn)
        moved = run.revision != revision
        if moved or (silence is not None and not silence.holds(run, records, revision=revision)):
            logger.info(f"_reap kept run={run.key!r}: it moved or woke since it was read")
            return envelopes
        _state_in_session(
            session,
            run.urn,
            actor=actor,
            event_ref=event_ref,
            kind=RunEventKind.ERROR_OBSERVED,
            payload=ErrorPayload(
                code=cause.value, retry_class="new_linked_run", message=message, diagnostic_ref=None
            ),
            now=now,
        )
    envelopes += _fail_edge(
        context,
        run,
        actor=actor,
        attempt_ref=attempt_ref,
        code=cause.value,
        message=message,
        event_ref=event_ref,
        now=now,
    )
    release_run_leases(context, run.urn, now=now)
    logger.info(f"_reap failed run={run.key!r} code={cause.value}")
    return envelopes


__all__ = ["ABANDON_AFTER", "end_abandoned_runs", "reap_lost_runs"]
