"""Raise a stall for every Run that went quiet, and answer which stalls still stand.

A Run that stops producing -- no tool call, no output, no state transition -- for its stall
interval has stalled, and nobody has to be looking for that to be true. So the daemon
sweeps every tree it serves and raises a typed stall fact on the run ledger for each
running Run silent past ``runtime.<name>.stall_interval_s``: one fact per quiet episode,
carrying the last activity, the elapsed silence and the control that resumes it.

The fact never moves the Run. A stall is the observation that makes *lost* a deliberate
call rather than a timeout guess: the Run stays stored as running, its outcome unknown,
and only a principal's control ends it. What the fact does open is a provider pause over
the Run citing it, which ends when the Run answers again or stops running.

``runtime.run.stalls.read`` answers with every stall still standing in the tree -- over a
Run still running that has produced nothing since -- so a surface can say which Runs are
lost without reading each Run's stream.

The same sweep measures each running Run against the effort constant of the mapping in
force, the newest revision the estimate ledger stores. A Run whose elapsed time since it
started reaches the notice policy's notify fraction of that estimate, and that has
produced nothing for the policy's grace period, opens one non-blocking budget notice.
The notice is upserted under the Run's key, so a later sweep or a restarted daemon finds
it on file and writes nothing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.identity import QualifiedUrn, parse_qualified_urn
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.stall import RunStallFact, standing_stall
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.pause import PauseStatus
from eawf.kernel.state.epoch2.run import Run, RunScope, RunStatus, TaskScope
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.budget.notices import (
    LOCAL_OPERATOR,
    BudgetCrossing,
    UpsertOutcome,
    notices_path,
    upsert_notice,
)
from eawf.runtime.control.reducer import reduce_run_control
from eawf.runtime.daemon.admission import load_economics
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.pause import (
    end_stall_pause,
    latest_pauses,
    open_stall_pause,
    stall_of,
)
from eawf.runtime.daemon.methods.run import RUN_CONTROL_REQUEST_METHOD, run_stall_interval
from eawf.runtime.daemon.native_dispatch import control_facts_of, stored_run
from eawf.runtime.daemon.native_guard import native_params, require_native_call
from eawf.runtime.daemon.run_events import (
    hello_facts_of,
    plan_stall,
    reduce_run_events,
    run_events_of,
    stall_facts_of,
)
from eawf.workflow.estimation.mapping_revisions import mapping_in_force

logger = logging.getLogger(__name__)

#: The read that lists every stall still standing in a tree.
RUN_STALLS_READ_METHOD: Final = "runtime.run.stalls.read"

#: The ledger-line key prefix a stall fact is filed under. The run collection holds
#: compacted Run records too, so the prefix keeps a stall from reading as a Run.
_STALL_KEY_PREFIX: Final = "STL-"

#: The status a stall line records.
_STALL_STATUS: Final = "stalled"

#: The notice ledger sits beside the tree's state file, which an epoch-2 tree keeps
#: under the same name.
_STATE_FILENAME: Final = "state.json"


class RunStallsRead(BaseModel):
    """A read of every standing stall in the addressed tree; it takes no parameter."""

    model_config = ConfigDict(extra="forbid")


class RunStallsAnswer(BaseModel):
    """Every stall still standing in a tree, at one instant.

    Attributes:
        stalls: One fact per running Run that has produced nothing since its stall was
            raised, in Run key order.
        read_at: When the daemon answered, which is what a reader ages the answer by.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    stalls: tuple[RunStallFact, ...]
    read_at: UtcDatetime


def stall_key(fact: RunStallFact) -> str:
    """Return the run-ledger key ``fact`` is filed under: its Run and its episode."""
    return f"{_STALL_KEY_PREFIX}{fact.run_ref.entity_key}-{fact.anchor_sequence}"


def _document_path(authority: RootAuthority) -> Path:
    """Return the selected generation's document of a fence-cleared tree."""
    target, generation_id = authority.target, authority.generation_id
    assert target is not None, "an epoch-2 answer always carries its target"
    assert generation_id is not None, "an epoch-2 answer always names a generation"
    return target.generation_path(generation_id) / GENERATION_DOCUMENT


def _running(document: Path) -> tuple[tuple[str, Any], ...]:
    """Return each Run the document stores as running, as its key and stored URN, in key order.

    The URN is returned as stored, so a row whose URN does not parse fails that Run's own
    sweep rather than the read every Run's sweep starts from.
    """
    rows = document_rows(read_document(document), Epoch2Collection.RUN)
    return tuple(
        (key, row.get("urn"))
        for key, row in sorted(rows.items())
        if row.get("status") == RunStatus.RUNNING.value
    )


def _still_running(records: tuple[LedgerRecord, ...], status: RunStatus, urn: QualifiedUrn) -> bool:
    """Return whether the Run's confirmed control effects leave it running."""
    reduced = reduce_run_control(status=status, facts=control_facts_of(records, urn)).status
    return reduced is RunStatus.RUNNING


def detect_stalls(context: Epoch2RootContext, *, now: datetime) -> tuple[str, ...]:
    """Raise a stall fact for every running Run in the tree silent past its interval.

    The document is read once without a lock to find the running Runs; each is then
    decided under its own lock, which reads the ledger again, so an event that landed
    in between keeps the Run live. A Run that cannot be read or decided is logged and
    passed over, so one bad Run never withholds the others' stalls.

    Args:
        context: The tree swept.
        now: The daemon's recording clock, which decides what has gone quiet.

    Returns:
        The keys of the Runs this sweep raised a stall for.
    """
    settle_stall_pauses(context, now=now)
    raised: list[str] = []
    for key, ref in _running(_document_path(context.require_selected_generation())):
        try:
            fact = _detect_stall(context, parse_qualified_urn(ref), now=now)
        except Exception:
            logger.exception(f"detect_stalls skipped run={key}")
            continue
        if fact is None:
            continue
        logger.info(
            f"detect_stalls raised run={key} elapsed_seconds={fact.elapsed_seconds:.0f} "
            f"interval_seconds={fact.interval_seconds}"
        )
        raised.append(key)
    return tuple(raised)


def _detect_stall(
    context: Epoch2RootContext, urn: QualifiedUrn, *, now: datetime
) -> RunStallFact | None:
    """Raise the Run's stall and open the pause over it when it went quiet, under its lock.

    Returns:
        The stall raised, or ``None`` when the Run is live, stopped or already stalled.
    """
    with context.session([urn]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        run = stored_run(session, records, urn)
        if not _still_running(records, run.status, urn):
            return None
        assert run.started_at is not None, "a running Run always carries its start stamp"
        fact = plan_stall(
            urn=run.urn,
            state=reduce_run_events(run_events_of(records, urn)),
            facts=stall_facts_of(records, urn),
            now=now,
            interval_seconds=run_stall_interval(context, hello_facts_of(records, urn), run),
            resume_method=RUN_CONTROL_REQUEST_METHOD,
            started_at=run.started_at,
        )
        if fact is None:
            return None
        key = stall_key(fact)
        commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.RUN,
                record_key=key,
                status=_STALL_STATUS,
                recorded_at=now,
                payload=fact.model_dump(mode="json"),
            ),
        )
        # a lost Run is a pause waiting on it to answer, opened with the stall it cites
        open_stall_pause(session, run_ref=run.urn, stall_key=key, now=now)
    return fact


def settle_stall_pauses(context: Epoch2RootContext, *, now: datetime) -> tuple[str, ...]:
    """End every open stall pause whose stall no longer stands over its Run.

    A pause over a quiet Run ends when the Run answers -- it is resolved -- or when the
    Run stops running -- cancelled with it when it was cancelled, else resolved. The run
    ledger is read once without a lock to find the open stall pauses; each Run is then
    decided under its own lock, which reads the ledger again. A Run that cannot be read
    is logged and passed over, so its pauses stand until it can be, and the others end.

    Args:
        context: The tree swept.
        now: The daemon's recording clock.

    Returns:
        The keys of the pauses this sweep ended.
    """
    document = _document_path(context.require_selected_generation())
    held = latest_pauses(read_ledger_records(ledger_path(document, Epoch2Collection.RUN)))
    runs = sorted(
        {
            pause.scope_ref
            for pause in held.values()
            if pause.status is PauseStatus.OPEN and stall_of(pause) is not None
        },
        key=str,
    )
    ended: list[str] = []
    for urn in runs:
        try:
            ended.extend(_settle_run(context, urn, now=now))
        except Exception:
            logger.exception(f"settle_stall_pauses skipped run={urn.entity_key}")
    return tuple(ended)


def _settle_run(context: Epoch2RootContext, urn: QualifiedUrn, *, now: datetime) -> list[str]:
    """End the Run's open stall pauses whose stall no longer stands, under its lock.

    Returns:
        The keys of the pauses ended.
    """
    ended: list[str] = []
    with context.session([urn]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        run = stored_run(session, records, urn)
        status = reduce_run_control(status=run.status, facts=control_facts_of(records, urn))
        state = reduce_run_events(run_events_of(records, urn))
        standing = (
            standing_stall(stall_facts_of(records, urn), state.last_activity_sequence)
            if status.status is RunStatus.RUNNING
            else None
        )
        stands = stall_key(standing) if standing else None
        for pause in latest_pauses(records).values():
            key = stall_of(pause)
            if pause.status is not PauseStatus.OPEN or pause.scope_ref != urn or key is None:
                continue
            if key != stands:
                cancelled = status.status is RunStatus.CANCELLED
                end_stall_pause(session, pause, cancelled=cancelled, now=now)
                ended.append(pause.key)
    return ended


def _estimate_audience(document: dict[str, Any], scope: RunScope) -> tuple[str, ...]:
    """Return who a Run's estimate notice is for: its Task's claim holder, else the operator."""
    if isinstance(scope, TaskScope):
        row = document_rows(document, Epoch2Collection.TASK).get(scope.task_ref.entity_key)
        if row is not None:
            holder = Task.model_validate(row).claimed_by
            if holder is not None:
                return (holder,)
    return (LOCAL_OPERATOR,)


def detect_estimate_crossings(context: Epoch2RootContext, *, now: datetime) -> tuple[str, ...]:
    """Open one budget notice for every running Run past its estimate with no progress.

    A Run is measured from ``started_at`` against the effort constant of the mapping in
    force. Short of the notify fraction, or with progress inside the grace period,
    nothing is written. A
    crossing upserts the notice keyed by the Run, so a Run already noticed -- by this
    daemon or an earlier one -- writes nothing again, and a notice the operator resolved
    is never reopened.

    Args:
        context: The tree swept.
        now: The daemon's recording clock.

    Returns:
        The keys of the Runs this sweep opened a notice for.
    """
    tree_root = context.identity.tree_root
    policy = load_economics(tree_root.parent).notice_policy.estimated_time
    path = _document_path(context.require_selected_generation())
    document = read_document(path)
    running = [
        Run.model_validate(row)
        for _, row in sorted(document_rows(document, Epoch2Collection.RUN).items())
        if row.get("status") == RunStatus.RUNNING.value
    ]
    if not running:
        return ()
    records = read_ledger_records(ledger_path(path, Epoch2Collection.RUN))
    estimates = read_ledger_records(ledger_path(path, Epoch2Collection.ESTIMATE))
    estimate_seconds = mapping_in_force(estimates).effort_minutes * 60
    opened: list[str] = []
    for run in running:
        if not _still_running(records, run.status, run.urn):
            continue
        assert run.started_at is not None, "a running Run always carries its start stamp"
        elapsed = max((now - run.started_at).total_seconds(), 0.0)
        if not policy.passed(elapsed_seconds=elapsed, estimate_seconds=estimate_seconds):
            continue
        activity = reduce_run_events(run_events_of(records, run.urn)).last_activity_at
        last_progress = run.started_at if activity is None else max(run.started_at, activity)
        if not policy.grace_met(last_progress_at=last_progress, now=now):
            continue
        upsert = upsert_notice(
            notices_path(tree_root / _STATE_FILENAME),
            BudgetCrossing(
                scope_id=run.key,
                axis="wall_seconds",
                basis="estimate",
                band="limit_reached",
                observed_value=int(elapsed),
                budget_value=int(estimate_seconds),
                observed_at=now,
                audience=_estimate_audience(document, run.scope),
            ),
        )
        if upsert.outcome is UpsertOutcome.CREATED:
            logger.info(
                f"detect_estimate_crossings opened run={run.key} "
                f"elapsed_seconds={elapsed:.0f} estimate_seconds={estimate_seconds:.0f}"
            )
            opened.append(run.key)
    return tuple(opened)


def standing_stall_facts(document: Path) -> tuple[RunStallFact, ...]:
    """Return every stall standing over a Run the document stores as running, in key order.

    A stall stands while its Run is running and has produced nothing since the activity
    the stall was measured from; a Run that answered afterwards has moved past it.

    Args:
        document: The selected generation's document; the run ledger beside it is read.
    """
    running = _running(document)
    records = read_ledger_records(ledger_path(document, Epoch2Collection.RUN)) if running else ()
    stalls: list[RunStallFact] = []
    for _key, ref in running:
        urn = parse_qualified_urn(ref)
        if not _still_running(records, RunStatus.RUNNING, urn):
            continue
        state = reduce_run_events(run_events_of(records, urn))
        fact = standing_stall(stall_facts_of(records, urn), state.last_activity_sequence)
        if fact is not None:
            stalls.append(fact)
    return tuple(stalls)


def standing_stalls(context: Epoch2RootContext, *, now: datetime) -> RunStallsAnswer:
    """Return every stall standing over a Run the tree still stores as running."""
    document = _document_path(context.require_selected_generation())
    return RunStallsAnswer(stalls=standing_stall_facts(document), read_at=now)


@register(RUN_STALLS_READ_METHOD)
async def _read_stalls(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Answer with every stall still standing in the addressed tree."""
    authority = require_native_call(ctx, params)
    native_params(RunStallsRead, params)
    context = ctx.native_root_context(authority.root)
    answer = await asyncio.to_thread(standing_stalls, context, now=datetime.now(UTC))
    return answer.model_dump(mode="json")


__all__ = [
    "RUN_STALLS_READ_METHOD",
    "RunStallsAnswer",
    "RunStallsRead",
    "detect_estimate_crossings",
    "detect_stalls",
    "settle_stall_pauses",
    "stall_key",
    "standing_stall_facts",
    "standing_stalls",
]
