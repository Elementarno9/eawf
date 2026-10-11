"""Awaiting a dispatched Run's turn in the background, and ending the Run with it.

Dispatch answers as soon as the worker is announced; the turn the worker was started
on runs for minutes after that. The daemon keeps one supervisor per dispatched Run for
exactly that stretch. It awaits the turn the launcher handed back, then ends the Run
the way the turn ended: ``COMPLETED`` when the worker bound its report through
``submit_report``, ``FAILED`` otherwise, with an ``error_observed`` line naming why.
A completing Run's workspace changes are first committed on its lease branch. Only
then is the workspace lease withdrawn, and the reconcile that follows removes the
worktree only when its residue is proven worthless, so a Run's directory is never taken
away while the Run is still running.

The registry is process memory, deliberately. A supervisor is an awaiting coroutine in
this daemon's event loop, so a restarted daemon has none, and a Run it finds running
with a spawned worker is a Run nobody is watching: the worker was this daemon's child
and went with it. :func:`sweep_after_boot` states that as ``provider_lost`` and fails
the Run, once per root per daemon process, which is what lets ``runtime.run.retry``
link a successor instead of waiting on a turn that will never end.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import weakref
from collections.abc import Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.candidate import DELIVERABLE_VERDICTS
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES
from eawf.kernel.runtime.events import ErrorPayload, ProviderLossPayload, RunEventKind
from eawf.kernel.runtime.lease import LeaseStatus
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.state.epoch2.run import Run, RunRuntimeTuple, RunStatus, TaskScope
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.task_status import TaskStatus
from eawf.kernel.state.epoch2.transitions import ObservedFact
from eawf.kernel.state.epoch2.values import TransitionReason
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.budget.service import TerminationResult
from eawf.runtime.candidate.seal import accepted_report_of
from eawf.runtime.control.reducer import reduce_run_control
from eawf.runtime.daemon.batch_close import close_after_ready
from eawf.runtime.daemon.content_store import file_content
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusedError,
    TransitionRequest,
    run_transaction,
)
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import (
    DispatchAttempt,
    DispatchRefusal,
    DispatchStage,
    Publish,
    control_facts_of,
    refused,
    run_ledger,
    stage_reached,
    standing_attempt,
    stored_run,
)
from eawf.runtime.daemon.run_capture_updates import bind_run_capture
from eawf.runtime.daemon.run_events import RunEventAppend
from eawf.runtime.hooks.event import HOST_HARNESSES
from eawf.runtime.integration.git_workspace import COMMIT_ARTIFACT_PREFIX
from eawf.runtime.runtimes.adapter import TurnOutcome
from eawf.runtime.runtimes.cancel import cancel_with_grace
from eawf.runtime.runtimes.host_transcript import scrubbed_words
from eawf.runtime.workspace.lease import (
    MCP_ARTIFACT_DIRNAME,
    LeaseRefusedError,
    reconcile_root,
    revoke_lease,
    root_leases,
)
from eawf.runtime.worktree.tree_change import TreeSnapshot, commit_worktree, snapshot_tree
from eawf.surfaces.cli import errors as cli_errors

logger = logging.getLogger(__name__)


class RunEndCause(StrEnum):
    """Why a supervised Run failed, as its failure reason and its error line name it."""

    TURN_FAILED = "turn-failed"
    TURN_INTERRUPTED = "turn-interrupted"
    BUDGET_TERMINATED = "budget-terminated"
    BUDGET_EXHAUSTED = "budget-exhausted"
    REPORT_UNBOUND = "report-unbound"
    PROVIDER_LOST = "provider-lost"
    WORKER_LOST = "worker-lost"
    DAEMON_STOPPED = "daemon-stopped"


#: What an operator reads when the turn itself said nothing about how it ended.
_CAUSE_WORDS: Final[dict[RunEndCause, str]] = {
    RunEndCause.TURN_FAILED: "the worker's turn failed",
    RunEndCause.TURN_INTERRUPTED: "the worker's turn was interrupted",
    RunEndCause.BUDGET_TERMINATED: "the worker's turn was stopped at the Run's token cap",
    RunEndCause.BUDGET_EXHAUSTED: (
        "the Run reached its wall-clock ceiling, so the daemon ended it and stopped its worker"
    ),
    RunEndCause.REPORT_UNBOUND: "the worker's turn ended without binding a report",
    RunEndCause.PROVIDER_LOST: (
        "the daemon restarted while the worker's turn ran, so nothing awaits it any more"
    ),
    RunEndCause.WORKER_LOST: (
        "the daemon that dispatched the Run stopped before its worker's turn ended, so "
        "nothing is left to end it"
    ),
    RunEndCause.DAEMON_STOPPED: (
        "the daemon was stopped before the worker bound a report, so it stopped the worker"
    ),
}

#: How each ending of a turn other than a clean completion is named.
_EXIT_CAUSES: Final[dict[str, RunEndCause]] = {
    "failed": RunEndCause.TURN_FAILED,
    "interrupted": RunEndCause.TURN_INTERRUPTED,
    "budget_terminated": RunEndCause.BUDGET_TERMINATED,
}


def end_cause(turn: TurnOutcome, *, reported: bool, stopped: bool = False) -> RunEndCause | None:
    """Return why the Run fails, or ``None`` when the turn completed with its report.

    Args:
        turn: How the worker's turn ended.
        reported: Whether the Run holds an accepted report.
        stopped: Whether the daemon stopped the worker before its turn ended; the
            report alone then decides, since the stop is what ended the turn.

    Returns:
        The failure cause, or ``None`` for a Run that completes.
    """
    if stopped:
        return None if reported else RunEndCause.DAEMON_STOPPED
    cause = _EXIT_CAUSES.get(turn.exit_kind)
    if cause is not None:
        return cause
    return None if reported else RunEndCause.REPORT_UNBOUND


class RunSupervisor:
    """The turns one daemon process is awaiting for the Runs of one root."""

    def __init__(self) -> None:
        """Start awaiting nothing, with the root not yet swept."""
        self._turns: dict[str, asyncio.Task[None]] = {}
        self._launching: set[str] = set()
        self._workers: dict[str, int] = {}
        self._swept = False
        self._draining = False
        self._stopping = False
        self._stop_waiters: set[asyncio.Future[None]] = set()

    def task_of(self, run_ref: str) -> asyncio.Task[None] | None:
        """Return the task awaiting *run_ref*'s turn, or ``None`` when none is."""
        task = self._turns.get(run_ref)
        return None if task is None or task.done() else task

    def busy(self, run_ref: str) -> bool:
        """Return whether a dispatch is launching *run_ref*'s worker or awaits its turn."""
        return run_ref in self._launching or self.task_of(run_ref) is not None

    def awaited(self) -> tuple[asyncio.Task[None], ...]:
        """Return every task still awaiting a Run's turn."""
        return tuple(task for task in self._turns.values() if not task.done())

    def drain(self) -> None:
        """Refuse every dispatch from now on, leaving the awaited turns to end."""
        self._draining = True

    def stop(self) -> None:
        """Refuse every dispatch and end every awaited turn at once.

        Each supervisor awaiting a turn stops its worker and ends the Run on the
        report it holds, rather than leaving it for the next daemon to reap.
        """
        self._draining = self._stopping = True
        for waiter in self._stop_waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def until_stopped(self) -> None:
        """Return once :meth:`stop` was called."""
        if self._stopping:
            return
        waiter = asyncio.get_running_loop().create_future()
        self._stop_waiters.add(waiter)
        try:
            await waiter
        finally:
            self._stop_waiters.discard(waiter)

    @contextmanager
    def launching(self, run_ref: str) -> Iterator[None]:
        """Hold *run_ref* as mid-launch until the dispatch driving it ends.

        The turn is registered only once the launcher returns, which can take
        as long as the provider takes to start; the hold covers that window, so
        a second dispatch of the Run is refused rather than starting a second
        worker under it.

        Raises:
            DaemonValidationError: The Run is already launching or supervised,
                refused as ``dispatch_attempt_in_flight``.
        """
        if self._draining:
            raise refused(
                DispatchRefusal.DAEMON_STOPPING,
                f"run {run_ref} was not dispatched because the daemon is stopping; dispatch "
                "it again once a daemon is running",
            )
        if self.busy(run_ref):
            raise refused(
                DispatchRefusal.ATTEMPT_IN_FLIGHT,
                f"run {run_ref} has a worker this daemon is still launching or awaiting, so a "
                "second dispatch would start a second worker under one Run",
            )
        self._launching.add(run_ref)
        try:
            yield
        finally:
            self._launching.discard(run_ref)

    def supervise(self, run_ref: str, work: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Register *work* as the one task awaiting *run_ref*'s turn.

        Raises:
            RuntimeError: Another task already awaits the Run, which would let
                two supervisors end one Run twice.
        """
        if self.task_of(run_ref) is not None:
            work.close()
            raise RuntimeError(f"run {run_ref} is already supervised")
        task = asyncio.get_running_loop().create_task(work, name=f"supervise:{run_ref}")
        self._turns[run_ref] = task
        task.add_done_callback(lambda done: self._forget(run_ref, done))
        return task

    def _forget(self, run_ref: str, done: asyncio.Task[None]) -> None:
        """Drop a finished task, unless a later one already took its place."""
        if self._turns.get(run_ref) is done:
            del self._turns[run_ref]

    @contextmanager
    def worker(self, run_ref: str, pgid: int) -> Iterator[None]:
        """Hold *pgid* as the process group working *run_ref* while its turn is awaited.

        The hold is what lets the daemon stop a worker it decided to end: an ended
        Run's worker keeps calling tools and spending provider tokens until its
        group is signalled.
        """
        self._workers[run_ref] = pgid
        try:
            yield
        finally:
            if self._workers.get(run_ref) == pgid:
                del self._workers[run_ref]

    def stop_worker(self, run_ref: str) -> TerminationResult | None:
        """Signal *run_ref*'s worker group through the grace-then-kill ladder.

        Returns:
            What the ladder did, or ``None`` when this daemon holds no worker for
            the Run.
        """
        pgid = self._workers.get(run_ref)
        if pgid is None:
            return None
        logger.info(f"stop_worker run={run_ref} pgid={pgid}")
        return cancel_with_grace(pgid)

    def claim_sweep(self) -> bool:
        """Return ``True`` exactly once: the first caller sweeps the root."""
        if self._swept:
            return False
        self._swept = True
        return True


#: One supervisor per attached root. Keyed weakly on the root's context, which the
#: daemon holds for its whole life, so a restarted daemon starts with none.
_SUPERVISORS: Final[weakref.WeakKeyDictionary[Epoch2RootContext, RunSupervisor]] = (
    weakref.WeakKeyDictionary()
)


def supervisor_of(context: Epoch2RootContext) -> RunSupervisor:
    """Return the supervisor of the root *context* belongs to, creating it once."""
    supervisor = _SUPERVISORS.get(context)
    if supervisor is None:
        supervisor = _SUPERVISORS[context] = RunSupervisor()
    return supervisor


async def drain_supervised_runs(*, grace_seconds: float, conclude_seconds: float) -> bool:
    """Let every awaited turn end within the grace, then end the rest deterministically.

    Every supervisor refuses new dispatches first. A turn that ends within
    *grace_seconds* ends its Run as it always does. Past the grace, every
    supervisor is stopped: each stops its worker and ends its Run ``COMPLETED``
    when the worker already bound a report, ``FAILED`` as ``daemon-stopped``
    otherwise, so no Run is left for the next daemon to reap as lost.

    Args:
        grace_seconds: How long the turns may take to end on their own.
        conclude_seconds: How long the stopped Runs may take to be ended.

    Returns:
        Whether every awaited Run was ended.
    """
    supervisors = tuple(_SUPERVISORS.values())
    for supervisor in supervisors:
        supervisor.drain()
    awaited = [task for supervisor in supervisors for task in supervisor.awaited()]
    if awaited:
        _done, pending = await asyncio.wait(awaited, timeout=grace_seconds)
        if not pending:
            return True
    for supervisor in supervisors:
        supervisor.stop()
    # a dispatch still launching at the grace's end registers its turn late
    awaited = [task for supervisor in supervisors for task in supervisor.awaited()]
    if not awaited:
        return True
    _done, pending = await asyncio.wait(awaited, timeout=conclude_seconds)
    logger.info(f"drain_supervised_runs stopped={len(awaited)} unended={len(pending)}")
    return not pending


@dataclass(frozen=True, slots=True)
class SupervisedTurn:
    """What ending one dispatched Run needs once its turn is over.

    Attributes:
        context: The root the Run lives in.
        urn: The dispatched Run.
        actor: The principal the dispatch was asked by, whom the ending is
            attributed to.
        attempt_ref: The dispatch attempt the worker was started under, which
            names every line and transition the ending writes.
        lease_id: The workspace lease the Run holds.
        turn: The launcher's turn, or ``None`` for a launcher that ran the whole
            turn before it announced the worker.
        pgid: The worker's process group, which a Run the daemon ends is stopped by.
        workspace: The leased directory the worker ran in.
        before: The workspace's tree as the launch found it, or ``None`` when git
            could not read it, which costs the Run its edit record and nothing else.
        publish: Where each committed transition is handed for the fan-out.
    """

    context: Epoch2RootContext
    urn: QualifiedUrn
    actor: PrincipalKey
    attempt_ref: str
    lease_id: str
    turn: asyncio.Task[TurnOutcome] | None
    pgid: int
    workspace: Path
    before: TreeSnapshot | None
    publish: Publish | None


async def worker_tree(workspace: Path) -> TreeSnapshot | None:
    """Return the tree of the worker's workspace, or ``None`` when git cannot read it.

    An unreadable tree costs the Run its edit record and nothing else, so the
    dispatch goes on without one.
    """
    try:
        return await asyncio.to_thread(snapshot_tree, workspace)
    except cli_errors.CliError as error:
        logger.warning(f"worker_tree unreadable workspace={workspace.name} error={error}")
        return None


#: The paths a worker's edits are read over: the whole workspace except the
#: isolation directory eawf writes the Run's capsule, MCP binding and temporary files
#: to, which is never the worker's edit.
_WORKER_EDIT_PATHS: Final[tuple[str, ...]] = (".", f":(exclude){MCP_ARTIFACT_DIRNAME}")


async def record_worker_edits(job: SupervisedTurn) -> None:
    """Record what the worker's turn changed in its workspace on the Run's stream.

    The event is named from the attempt, so a turn recorded twice repeats the line
    rather than adding one.
    """
    # The recorder appends through the run verbs, which import this module.
    from eawf.runtime.daemon.file_changes import FileEdit, record_file_edit

    after = None if job.before is None else await worker_tree(job.workspace)
    if job.before is None or after is None:
        return
    digest = hashlib.sha256(f"{job.attempt_ref}:edits".encode()).hexdigest()[:32]
    try:
        await asyncio.to_thread(
            record_file_edit,
            job.context,
            FileEdit(
                workspace=job.workspace, before=job.before, after=after, paths=_WORKER_EDIT_PATHS
            ),
            run_ref=job.urn,
            event_ref=f"EVT-{digest}",
            actor=job.actor,
            now=datetime.now(UTC),
        )
    except (cli_errors.CliError, DaemonValidationError) as error:
        logger.warning(f"record_worker_edits skipped attempt={job.attempt_ref} error={error}")


def _event_ref(attempt_ref: str, label: str) -> str:
    """Return the id of one line the supervisor states, the same on every replay."""
    body = f"{attempt_ref}:supervisor:{label}"
    return f"EVT-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:32]}"


async def _settle(turn: asyncio.Task[TurnOutcome] | None) -> TurnOutcome:
    """Return how the turn ended, reading a turn that raised as a failed one.

    Cancellation is not caught: a supervisor cancelled with its daemon leaves the
    Run running, and the next daemon's sweep states the loss.
    """
    if turn is None:
        return TurnOutcome(exit_kind="completed")
    try:
        return await turn
    # A launcher maps the provider's failures onto the outcome, but one escaping
    # it unmapped would otherwise leave the Run running with nothing awaiting it.
    except Exception as error:
        logger.warning(f"supervised turn raised error={type(error).__name__}")
        return TurnOutcome(exit_kind="failed", error_text=str(error) or type(error).__name__)


async def _turn_or_stop(job: SupervisedTurn, supervisor: RunSupervisor) -> TurnOutcome | None:
    """Return how the turn ended, or ``None`` when the daemon stopped it first.

    A stopped turn's worker is signalled through the grace-then-kill ladder and
    the turn is cancelled, so nothing the worker does after the stop reaches the Run.
    """
    settle = asyncio.ensure_future(_settle(job.turn))
    stop = asyncio.ensure_future(supervisor.until_stopped())
    try:
        await asyncio.wait({settle, stop}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        settle.cancel()
        raise
    finally:
        stop.cancel()
    if settle.done():
        return settle.result()
    await asyncio.to_thread(supervisor.stop_worker, str(job.urn))
    settle.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await settle
    logger.info(f"supervise_turn stopped attempt={job.attempt_ref}")
    return None


async def supervise_turn(job: SupervisedTurn) -> None:
    """Await the Run's turn, then end the Run and withdraw its lease.

    Nothing that raises while the ending is written leaves the Run running: an
    edit record that raises costs the Run its record, and an ending that raises
    is written again as a failure naming what raised. A daemon that stops before
    the turn ends stops the worker and ends the Run on the report it holds.

    Args:
        job: The Run and the turn to await.
    """
    supervisor = supervisor_of(job.context)
    with supervisor.worker(str(job.urn), job.pgid):
        settled = await _turn_or_stop(job, supervisor)
    stopped = settled is None
    turn = TurnOutcome(exit_kind="interrupted") if settled is None else settled
    try:
        await record_worker_edits(job)
    except Exception as error:
        logger.warning(
            f"supervise_turn unrecorded edits attempt={job.attempt_ref} "
            f"error={type(error).__name__}"
        )
    try:
        envelopes = await asyncio.to_thread(
            _end_supervised_run, job, turn, now=datetime.now(UTC), stopped=stopped
        )
    except Exception as error:
        logger.warning(
            f"supervise_turn ending raised attempt={job.attempt_ref} error={type(error).__name__}"
        )
        failed = TurnOutcome(
            exit_kind="failed",
            error_text=(
                f"the daemon could not record how the worker's turn ended: "
                f"{type(error).__name__}: {error}"
            ),
        )
        envelopes = await asyncio.to_thread(_end_supervised_run, job, failed, now=datetime.now(UTC))
    if job.publish is not None:
        for envelope in envelopes:
            job.publish(envelope)


def _end_supervised_run(
    job: SupervisedTurn, turn: TurnOutcome, *, now: datetime, stopped: bool = False
) -> tuple[Envelope, ...]:
    """Take up a Run its dispatch never started, then end it the way its turn ended.

    The worker is handed to the supervisor before the dispatch moves the Run to
    ``RUNNING``, so a dispatch whose start was refused, or that raised after the
    hand-over, leaves a supervised Run ``QUEUED``. :func:`conclude_run` ends only a
    running Run, which would leave that one queued with its lease held for good.

    Args:
        job: The supervised Run.
        turn: How the worker's turn ended.
        now: The stamp every record of the ending shares.
        stopped: Whether the daemon stopped the worker before its turn ended.

    Returns:
        The envelopes the committed transitions produced.

    Raises:
        TransactionRefusedError: The transaction refused the start.
    """
    context, urn = job.context, job.urn
    with context.session([urn]) as session:
        records = read_ledger_records(run_ledger(session))
        run = stored_run(session, records, urn)
        status = reduce_run_control(status=run.status, facts=control_facts_of(records, urn)).status
    started: tuple[Envelope, ...] = ()
    if status is RunStatus.QUEUED:
        logger.info(f"_end_supervised_run starting unstarted run={urn.entity_key!r}")
        started = _move(
            context,
            run,
            actor=job.actor,
            to_status=RunStatus.RUNNING,
            idempotency_key=f"supervise-{job.attempt_ref}-launch-start",
            updates={"started_at": now.isoformat()},
            now=now,
        )
    return started + conclude_run(job, turn, now=now, stopped=stopped)


def conclude_run(
    job: SupervisedTurn, turn: TurnOutcome, *, now: datetime, stopped: bool = False
) -> tuple[Envelope, ...]:
    """End the Run the way its turn ended, then withdraw its workspace lease.

    A Run something else already ended -- a cancel, a crossing of its token cap --
    keeps that ending. A Run whose end could not be committed keeps its lease, so
    its workspace is not reclaimed under a Run still recorded as running. Whatever
    ended the Run, a Task its report did not move to READY_TO_INTEGRATE is handed
    back to PLANNED rather than left RUNNING behind it.

    Args:
        job: The supervised Run.
        turn: How the worker's turn ended.
        now: The stamp every record of the ending shares.
        stopped: Whether the daemon stopped the worker before its turn ended.

    Returns:
        The envelopes the committed transition produced.
    """
    context, urn = job.context, job.urn
    with context.session([urn]) as session:
        records = read_ledger_records(run_ledger(session))
        run = stored_run(session, records, urn)
        status = reduce_run_control(status=run.status, facts=control_facts_of(records, urn)).status
        accepted = accepted_report_of(records, str(urn))
    cause = end_cause(turn, reported=accepted is not None, stopped=stopped)
    logger.info(
        f"conclude_run run={urn.entity_key!r} status={status.value} exit={turn.exit_kind} "
        f"cause={None if cause is None else cause.value}"
    )
    envelopes: tuple[Envelope, ...] = ()
    if status is RunStatus.RUNNING:
        try:
            if cause is None:
                commit = commit_worker_edits(job, run)
                envelopes = _complete(
                    context,
                    run,
                    actor=job.actor,
                    attempt_ref=job.attempt_ref,
                    commit=commit,
                    now=now,
                )
                if accepted is not None and accepted.verdict in DELIVERABLE_VERDICTS:
                    envelopes += _ready_task(
                        context,
                        run,
                        actor=job.actor,
                        attempt_ref=job.attempt_ref,
                        commit=commit,
                        now=now,
                    )
                    envelopes += close_after_ready(context, run, actor=job.actor, now=now)
                # a report that does not deliver, or a ready edge refused, leaves
                # the Task RUNNING behind a finished Run unless it is handed back
                assert accepted is not None, "a Run completes only on its bound report"
                envelopes += _requeue_task(
                    context,
                    run,
                    actor=job.actor,
                    attempt_ref=job.attempt_ref,
                    code=f"report-{accepted.verdict}",
                    now=now,
                )
            else:
                envelopes = _fail(
                    context,
                    run,
                    actor=job.actor,
                    attempt_ref=job.attempt_ref,
                    code=cause.value,
                    words=_CAUSE_WORDS[cause],
                    detail=turn.error_text,
                    now=now,
                )
        except TransactionRefusedError as refusal:
            logger.warning(
                f"conclude_run kept the lease run={urn.entity_key!r} code={refusal.code.value}"
            )
            return ()
    elif status not in TERMINAL_RUN_STATUSES:
        # A Run waiting on a suspension has its own way back; the lease stays
        # with it rather than being reclaimed under a Run that may resume.
        logger.warning(f"conclude_run left run={urn.entity_key!r} status={status.value}")
        return ()
    elif status is not RunStatus.COMPLETED:
        # a cancel or a token-cap stop ended the Run before its turn did
        envelopes = _requeue_task(
            context,
            run,
            actor=job.actor,
            attempt_ref=job.attempt_ref,
            code=f"run-{status.value.lower()}",
            now=now,
        )
    release_lease(context, lease_id=job.lease_id, now=now)
    return envelopes


def commit_worker_edits(job: SupervisedTurn, run: Run) -> str | None:
    """Commit what an accepted Run changed on its lease branch, and return the commit.

    Only a Run whose report was accepted reaches here, so the branch holds work its
    worker stood behind. The commit is never pushed or merged: it is what a later
    candidate submission or an operator picks up, and the reconcile keeps the
    worktree because its HEAD has moved past the base. A commit git refuses costs
    the Run its commit and nothing else -- the edits stay in the worktree, which
    the reconcile keeps for the same reason.

    Args:
        job: The supervised Run, naming its workspace.
        run: The stored Run, whose Task the commit's trailer names.

    Returns:
        The commit's SHA, or ``None`` when the worker changed nothing or git
        refused the commit.
    """
    assert isinstance(run.scope, TaskScope), "only a task-scoped Run holds a workspace lease"
    task_key = run.scope.task_ref.entity_key
    message = f"Run {run.key} changes for {task_key}\n\nTask: {task_key}\n"
    try:
        commit = commit_worktree(job.workspace, message=message, exclude=(MCP_ARTIFACT_DIRNAME,))
    except cli_errors.CliError as error:
        logger.warning(f"commit_worker_edits skipped run={run.key!r} error={error}")
        return None
    logger.info(f"commit_worker_edits run={run.key!r} committed={commit is not None}")
    return commit


def _complete(
    context: Epoch2RootContext,
    run: Run,
    *,
    actor: PrincipalKey,
    attempt_ref: str,
    commit: str | None,
    now: datetime,
) -> tuple[Envelope, ...]:
    """Move the Run to ``COMPLETED`` on the report its worker bound.

    The workspace commit, when one was made, is named among the proofs the
    completion was taken against, so the Run's transition records which commit
    holds its work.
    """
    return _move(
        context,
        run,
        actor=actor,
        to_status=RunStatus.COMPLETED,
        idempotency_key=f"supervise-{attempt_ref}-complete",
        updates={"ended_at": now.isoformat()},
        binding_refs=() if commit is None else (f"{COMMIT_ARTIFACT_PREFIX}{commit}",),
        now=now,
    )


def _ready_task(
    context: Epoch2RootContext,
    run: Run,
    *,
    actor: PrincipalKey,
    attempt_ref: str,
    commit: str | None,
    now: datetime,
) -> tuple[Envelope, ...]:
    """Move the completed Run's Task to ``READY_TO_INTEGRATE`` on its delivering report.

    The Task is moved only while it is RUNNING under this very Run: a Task a
    retry rebound to a later Run waits on that Run's report instead. The move
    is taken against the lease commit holding the work and the Run whose
    report stands behind it, which is the evidence the edge asks for.

    The Run is already COMPLETED by then, so a refused Task edge is logged and
    leaves the Task RUNNING for the operator rather than undoing the ending.

    Returns:
        The envelopes the committed transition produced, or none when the
        Task is not RUNNING under this Run or the edge was refused.
    """
    assert isinstance(run.scope, TaskScope), "only a task-scoped Run is dispatched natively"
    task_urn = run.scope.task_ref
    with context.session([task_urn]) as session:
        row = document_rows(session.read_document(), Epoch2Collection.TASK).get(task_urn.entity_key)
    if row is None:
        return ()
    task = Task.model_validate(row)
    if task.status is not TaskStatus.RUNNING or str(task.active_run_ref) != str(run.urn):
        logger.info(
            f"_ready_task left task={task.key!r} status={task.status.value} run={run.key!r}"
        )
        return ()
    proofs = (
        (str(run.urn),) if commit is None else (f"{COMMIT_ARTIFACT_PREFIX}{commit}", str(run.urn))
    )
    try:
        committed = run_transaction(
            context=context,
            request=TransitionRequest(
                urn=task_urn,
                to_status=TaskStatus.READY_TO_INTEGRATE.value,
                expected_revision=task.revision,
                idempotency_key=f"supervise-{attempt_ref}-task-ready",
                actor=actor,
                observations=(ObservedFact.RUN_REPORT_BOUND,),
                binding_refs=proofs,
            ),
            now=now,
        )
    except TransactionRefusedError as refusal:
        logger.warning(
            f"_ready_task refused task={task.key!r} run={run.key!r} code={refusal.code.value}"
        )
        return ()
    return () if committed.envelope is None else (committed.envelope,)


def _fail(
    context: Epoch2RootContext,
    run: Run,
    *,
    actor: PrincipalKey,
    attempt_ref: str,
    code: str,
    words: str,
    detail: str | None,
    now: datetime,
) -> tuple[Envelope, ...]:
    """State why the Run failed on its stream, move it to ``FAILED`` and requeue its Task.

    The error line is the Run's account of how it ended, so the failing edge is
    taken against it: the transition names the line as the bound report.

    Args:
        context: The root the Run lives in.
        run: The running Run.
        actor: The principal the ending is attributed to.
        attempt_ref: What names the ending's line and transition.
        code: The failure code, a slug.
        words: What an operator reads when *detail* says nothing.
        detail: What the failing side said, filed whole as the trace.
        now: The stamp of the ending.

    Raises:
        TransactionRefusedError: The transaction refused the edge.
    """
    said = None if detail is None else scrubbed_words(detail)
    message = said if said is not None else words
    event_ref = _event_ref(attempt_ref, f"ended:{code}")
    trace = None
    if detail:
        with context.session([run.urn]) as session:
            trace = file_content(session, run_ref=run.urn, text=detail, now=now)
    _state(
        context,
        run.urn,
        actor=actor,
        event_ref=event_ref,
        kind=RunEventKind.ERROR_OBSERVED,
        payload=ErrorPayload(
            code=code, retry_class="new_linked_run", message=message, diagnostic_ref=trace
        ),
        now=now,
    )
    failed = _move(
        context,
        run,
        actor=actor,
        to_status=RunStatus.FAILED,
        idempotency_key=f"supervise-{attempt_ref}-fail",
        updates={
            "ended_at": now.isoformat(),
            "failure": TransitionReason(code=code, message=message).model_dump(mode="json"),
        },
        reason_code=code,
        binding_refs=(event_ref,),
        now=now,
    )
    return failed + _requeue_task(
        context, run, actor=actor, attempt_ref=attempt_ref, code=code, now=now
    )


def _requeue_task(
    context: Epoch2RootContext,
    run: Run,
    *,
    actor: PrincipalKey,
    attempt_ref: str,
    code: str,
    now: datetime,
) -> tuple[Envelope, ...]:
    """Hand an ended Run's Task back to ``PLANNED`` when it is RUNNING under that Run.

    A Task left RUNNING under a Run that ended shows work in flight that nothing
    does, so the Task is replanned and a later dispatch claims it afresh. A Task a
    retry already rebound to a later Run is that Run's to end. The Run has ended by
    then, so a refused Task edge is logged and leaves the Task for the operator
    rather than undoing the Run's ending.

    Args:
        context: The root the Run lives in.
        run: The ended Run.
        actor: The principal the ending is attributed to.
        attempt_ref: What names the requeue's transition.
        code: Why the Run ended, a slug, recorded as the Task's reason.
        now: The stamp of the requeue.

    Returns:
        The envelopes the committed transition produced; none for a Run with no
        Task, a Task not RUNNING under it, or a refused edge.
    """
    if not isinstance(run.scope, TaskScope):
        return ()
    task_urn = run.scope.task_ref
    with context.session([task_urn]) as session:
        row = document_rows(session.read_document(), Epoch2Collection.TASK).get(task_urn.entity_key)
    if row is None:
        return ()
    task = Task.model_validate(row)
    if task.status is not TaskStatus.RUNNING or str(task.active_run_ref) != str(run.urn):
        return ()
    try:
        committed = run_transaction(
            context=context,
            request=TransitionRequest(
                urn=task_urn,
                to_status=TaskStatus.PLANNED.value,
                expected_revision=task.revision,
                idempotency_key=f"supervise-{attempt_ref}-task-requeue",
                actor=actor,
                reason_code=code,
                binding_refs=(str(run.urn),),
            ),
            now=now,
        )
    except TransactionRefusedError as refusal:
        logger.warning(
            f"_requeue_task refused task={task.key!r} run={run.key!r} code={refusal.code.value}"
        )
        return ()
    logger.info(f"_requeue_task replanned task={task.key!r} run={run.key!r} code={code}")
    return () if committed.envelope is None else (committed.envelope,)


def _state(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    actor: PrincipalKey,
    event_ref: str,
    kind: RunEventKind,
    payload: ErrorPayload | ProviderLossPayload,
    now: datetime,
) -> None:
    """State one line the daemon observed about the Run's turn on the Run's stream."""
    # the run verbs import the dispatch driver, which this module imports
    from eawf.runtime.daemon.methods.run import append_run_event

    append_run_event(
        context,
        RunEventAppend(
            urn=urn,
            event_ref=event_ref,
            run_sequence=1,
            event_kind=kind,
            provenance="daemon_observed",
            payload=payload,
            actor=actor,
        ),
        now=now,
        at_tail=True,
    )


def _move(
    context: Epoch2RootContext,
    run: Run,
    *,
    actor: PrincipalKey,
    to_status: RunStatus,
    idempotency_key: str,
    updates: dict[str, object],
    now: datetime,
    reason_code: str | None = None,
    binding_refs: tuple[str, ...] = (),
) -> tuple[Envelope, ...]:
    """Take one Run edge through the canonical transaction, with its counter readings.

    Raises:
        TransactionRefusedError: The transaction refused the edge.
    """
    observations = (ObservedFact.RUN_REPORT_BOUND,) if to_status in TERMINAL_RUN_STATUSES else ()
    captured = bind_run_capture(
        context, urn=run.urn, to_status=to_status, updates=updates, actor=actor
    )
    committed = run_transaction(
        context=context,
        request=TransitionRequest(
            urn=run.urn,
            to_status=to_status.value,
            expected_revision=run.revision,
            idempotency_key=idempotency_key,
            actor=actor,
            observations=observations,
            updates=captured,
            reason_code=reason_code,
            binding_refs=binding_refs,
        ),
        now=now,
    )
    return () if committed.envelope is None else (committed.envelope,)


def start_run(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    actor: PrincipalKey,
    attempt: DispatchAttempt,
    runtime: RunRuntimeTuple | None,
    now: datetime,
) -> tuple[Envelope, ...]:
    """Move a Run whose worker was spawned from ``QUEUED`` to ``RUNNING``.

    The vendor session is the provider's own session handle, hashed, so the Run's
    usage is counted against the session its worker actually runs in. A Run that
    already left the queue is left where it is.

    Args:
        context: The root the Run lives in.
        urn: The dispatched Run.
        actor: The principal the dispatch was asked by.
        attempt: The attempt at its spawned stage.
        runtime: The runtime the launcher observed by the announcement; a
            member it did not observe stays absent on the Run.
        now: The dispatch stamp, which is the Run's start.

    Returns:
        The envelopes the committed transition produced.

    Raises:
        TransactionRefusedError: The transaction refused the start.
    """
    assert attempt.provider_session_ref is not None, "a spawned attempt names its session"
    with context.session([urn]) as session:
        run = stored_run(session, read_ledger_records(run_ledger(session)), urn)
    if run.status is not RunStatus.QUEUED:
        return ()
    session_ref = VendorSessionRef(
        harness=HOST_HARNESSES[attempt.provider_kind],
        session_digest=attempt.provider_session_ref,
    )
    updates: dict[str, object] = {
        "started_at": now.isoformat(),
        "vendor_session": session_ref.model_dump(mode="json"),
    }
    if runtime is not None:
        updates["runtime_tuple"] = runtime.model_dump(mode="json", exclude_none=True)
    return _move(
        context,
        run,
        actor=actor,
        to_status=RunStatus.RUNNING,
        idempotency_key=f"supervise-{attempt.attempt_ref}-start",
        updates=updates,
        now=now,
    )


def release_lease(context: Epoch2RootContext, *, lease_id: str, now: datetime) -> None:
    """Withdraw a terminal Run's lease and let the reconcile judge its residue.

    The reconcile releases the worktree when git shows it holds nothing beyond its
    base, and quarantines it otherwise, so a Run's uncommitted or committed work is
    kept rather than removed.

    Args:
        context: The root the lease belongs to.
        lease_id: The lease to withdraw.
        now: When the withdrawal was decided.
    """
    try:
        revoke_lease(context, lease_id=lease_id, now=now, hard=True)
    except LeaseRefusedError as error:
        # Already withdrawn or expired: the reconcile below still judges it.
        logger.info(f"release_lease lease={lease_id} code={error.code.value}")
    reconcile_root(context, now=now)


def fail_unlaunched_run(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    actor: PrincipalKey,
    code: str,
    detail: str,
    now: datetime,
) -> tuple[Envelope, ...]:
    """End a Run whose dispatch was refused after it was created, and withdraw its lease.

    Left alone, such a Run stays ``QUEUED`` -- with a workspace lease when the refusal
    came after the lease -- and keeps its Task from ever being dispatched again. It is
    failed with the refusal's code instead. No edge leads from ``QUEUED`` to
    ``FAILED``, so the Run is taken up first: the dispatch did try to start it. A Run
    another dispatch is still launching or a supervisor awaits, or one still running or
    suspended, has its own ending and is left to it; a Run already stopped only has its
    lease withdrawn.

    Args:
        context: The root the Run lives in.
        urn: The Run the refused dispatch created.
        actor: The principal the dispatch was asked by.
        code: The refusal's code, a slug.
        detail: What the refusal said, filed whole as the trace.
        now: The stamp every record of the ending shares.

    Returns:
        The envelopes the committed transitions produced.
    """
    if supervisor_of(context).busy(str(urn)):
        return ()
    with context.session([urn]) as session:
        records = read_ledger_records(run_ledger(session))
        run = stored_run(session, records, urn)
        status = reduce_run_control(status=run.status, facts=control_facts_of(records, urn)).status
        attempt = standing_attempt(records, urn)
    label = urn.entity_key if attempt is None else attempt.attempt_ref
    envelopes: tuple[Envelope, ...] = ()
    if status is RunStatus.QUEUED:
        try:
            envelopes = _move(
                context,
                run,
                actor=actor,
                to_status=RunStatus.RUNNING,
                idempotency_key=f"supervise-{label}-launch-start",
                updates={"started_at": now.isoformat()},
                now=now,
            )
            with context.session([urn]) as session:
                run = stored_run(session, read_ledger_records(run_ledger(session)), urn)
            envelopes += _fail(
                context,
                run,
                actor=actor,
                attempt_ref=label,
                code=code,
                words="the dispatch was refused before its worker was announced",
                detail=detail,
                now=now,
            )
        except TransactionRefusedError as refusal:
            logger.warning(
                f"fail_unlaunched_run kept the lease run={urn.entity_key!r} "
                f"code={refusal.code.value}"
            )
            return envelopes
    elif status not in TERMINAL_RUN_STATUSES:
        return ()
    for lease in root_leases(context):
        if str(lease.run_ref) == str(urn) and lease.status is LeaseStatus.ACTIVE:
            release_lease(context, lease_id=lease.lease_id, now=now)
    logger.info(f"fail_unlaunched_run run={urn.entity_key!r} status={status.value} code={code}")
    return envelopes


def end_exhausted_run(
    context: Epoch2RootContext,
    urn: QualifiedUrn,
    *,
    actor: PrincipalKey,
    detail: str,
    now: datetime,
) -> tuple[Envelope, ...]:
    """Fail a running Run that reached its hard ceiling, then stop its worker.

    The gateway refuses every call a Run past its ceiling makes, but refusing calls
    does not stop the worker making them, and it spends provider tokens on each. So
    the Run is failed first, under its own lock, and its worker is signalled after:
    whatever ends the turn then -- the supervisor or a campaign round -- finds the
    Run ended and keeps that ending. A Run something else already stopped is left
    as it is and its worker is not signalled.

    Args:
        context: The root the Run lives in.
        urn: The Run that reached its ceiling.
        actor: The principal the ending is attributed to.
        detail: What was measured against which ceiling, filed as the trace.
        now: The stamp every record of the ending shares.

    Returns:
        The envelopes the committed transition produced; empty for a Run that was
        no longer running.

    Raises:
        TransactionRefusedError: The transaction refused the failing edge.
    """
    with context.session([urn]) as session:
        records = read_ledger_records(run_ledger(session))
        run = stored_run(session, records, urn)
        status = reduce_run_control(status=run.status, facts=control_facts_of(records, urn)).status
    if status is not RunStatus.RUNNING:
        return ()
    cause = RunEndCause.BUDGET_EXHAUSTED
    envelopes = _fail(
        context,
        run,
        actor=actor,
        attempt_ref=f"{run.key}-ceiling",
        code=cause.value,
        words=_CAUSE_WORDS[cause],
        detail=detail,
        now=now,
    )
    stopped = supervisor_of(context).stop_worker(str(urn))
    logger.info(
        f"end_exhausted_run run={run.key!r} stopped={stopped is not None} "
        f"sigkill={stopped is not None and stopped.sigkill_sent}"
    )
    return envelopes


def sweep_lost_runs(
    context: Epoch2RootContext,
    supervisor: RunSupervisor,
    *,
    actor: PrincipalKey,
    now: datetime,
) -> tuple[Envelope, ...]:
    """Fail every running Run whose spawned worker no supervisor in this daemon awaits.

    Only a dispatched Run holds a workspace lease, so the active leases are what is
    walked. Each Run is decided under its own session.

    Args:
        context: The root to sweep.
        supervisor: The root's supervisor in this daemon process.
        actor: The principal the sweep is attributed to.
        now: The stamp every record of the sweep shares.

    Returns:
        The envelopes the committed transitions produced.
    """
    envelopes: list[Envelope] = []
    for lease in root_leases(context):
        if lease.status is not LeaseStatus.ACTIVE:
            continue
        if supervisor.task_of(str(lease.run_ref)) is not None:
            continue
        lost = _lost_attempt(context, lease.run_ref)
        if lost is None:
            continue
        run, attempt = lost
        _state(
            context,
            run.urn,
            actor=actor,
            event_ref=_event_ref(attempt.attempt_ref, "provider_lost"),
            kind=RunEventKind.PROVIDER_LOST,
            payload=ProviderLossPayload(message=_CAUSE_WORDS[RunEndCause.PROVIDER_LOST]),
            now=now,
        )
        try:
            envelopes.extend(
                _fail(
                    context,
                    run,
                    actor=actor,
                    attempt_ref=attempt.attempt_ref,
                    code=RunEndCause.PROVIDER_LOST.value,
                    words=_CAUSE_WORDS[RunEndCause.PROVIDER_LOST],
                    detail=None,
                    now=now,
                )
            )
        except TransactionRefusedError as refusal:
            logger.warning(f"sweep_lost_runs kept run={run.key!r} code={refusal.code.value}")
            continue
        release_lease(context, lease_id=lease.lease_id, now=now)
        logger.info(f"sweep_lost_runs failed run={run.key!r} attempt={attempt.attempt_ref}")
    return tuple(envelopes)


def _lost_attempt(
    context: Epoch2RootContext, urn: QualifiedUrn
) -> tuple[Run, DispatchAttempt] | None:
    """Return the Run and its attempt when a running Run's spawned worker is lost."""
    with context.session([urn]) as session:
        records = read_ledger_records(run_ledger(session))
        run = stored_run(session, records, urn)
        status = reduce_run_control(status=run.status, facts=control_facts_of(records, urn)).status
        attempt = standing_attempt(records, urn)
    if status is not RunStatus.RUNNING or attempt is None:
        return None
    if not stage_reached(attempt.stage, DispatchStage.SPAWNED):
        return None
    return run, attempt


async def sweep_after_boot(
    context: Epoch2RootContext, *, actor: PrincipalKey, publish: Publish | None
) -> None:
    """Sweep the root for lost Runs the first time this daemon process reaches it.

    Args:
        context: The root a Run verb addressed.
        actor: The principal the sweep is attributed to.
        publish: Where each committed transition is handed for the fan-out.
    """
    supervisor = supervisor_of(context)
    if not supervisor.claim_sweep():
        return
    envelopes = await asyncio.to_thread(
        sweep_lost_runs, context, supervisor, actor=actor, now=datetime.now(UTC)
    )
    if publish is not None:
        for envelope in envelopes:
            publish(envelope)


__all__ = [
    "Publish",
    "RunEndCause",
    "RunSupervisor",
    "SupervisedTurn",
    "commit_worker_edits",
    "conclude_run",
    "drain_supervised_runs",
    "end_cause",
    "end_exhausted_run",
    "fail_unlaunched_run",
    "record_worker_edits",
    "release_lease",
    "start_run",
    "supervise_turn",
    "supervisor_of",
    "sweep_after_boot",
    "sweep_lost_runs",
    "worker_tree",
]
