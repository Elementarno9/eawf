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
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import LedgerRecord, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.control.reducer import reduce_run_control
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

logger = logging.getLogger(__name__)

#: The read that lists every stall still standing in a tree.
RUN_STALLS_READ_METHOD: Final = "runtime.run.stalls.read"

#: The ledger-line key prefix a stall fact is filed under. The run collection holds
#: compacted Run records too, so the prefix keeps a stall from reading as a Run.
_STALL_KEY_PREFIX: Final = "STL-"

#: The status a stall line records.
_STALL_STATUS: Final = "stalled"


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


def _document_path(authority: RootAuthority) -> Path:
    """Return the selected generation's document of a fence-cleared tree."""
    target, generation_id = authority.target, authority.generation_id
    assert target is not None, "an epoch-2 answer always carries its target"
    assert generation_id is not None, "an epoch-2 answer always names a generation"
    return target.generation_path(generation_id) / GENERATION_DOCUMENT


def _running(document: Path) -> tuple[QualifiedUrn, ...]:
    """Return the Runs the document stores as running, in key order."""
    rows = document_rows(read_document(document), Epoch2Collection.RUN)
    return tuple(
        parse_qualified_urn(row["urn"])
        for _, row in sorted(rows.items())
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
    in between keeps the Run live.

    Args:
        context: The tree swept.
        now: The daemon's recording clock, which decides what has gone quiet.

    Returns:
        The keys of the Runs this sweep raised a stall for.
    """
    settle_stall_pauses(context, now=now)
    raised: list[str] = []
    for urn in _running(_document_path(context.require_selected_generation())):
        with context.session([urn]) as session:
            records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
            run = stored_run(session, records, urn)
            if not _still_running(records, run.status, urn):
                continue
            fact = plan_stall(
                urn=run.urn,
                state=reduce_run_events(run_events_of(records, urn)),
                facts=stall_facts_of(records, urn),
                now=now,
                interval_seconds=run_stall_interval(context, hello_facts_of(records, urn)),
                resume_method=RUN_CONTROL_REQUEST_METHOD,
            )
            if fact is None:
                continue
            key = f"{_STALL_KEY_PREFIX}{run.key}-{fact.anchor_sequence}"
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
        logger.info(
            f"detect_stalls raised run={run.key} elapsed_seconds={fact.elapsed_seconds:.0f} "
            f"interval_seconds={fact.interval_seconds}"
        )
        raised.append(run.key)
    return tuple(raised)


def settle_stall_pauses(context: Epoch2RootContext, *, now: datetime) -> tuple[str, ...]:
    """End every open stall pause whose stall no longer stands over its Run.

    A pause over a quiet Run ends when the Run answers -- it is resolved -- or when the
    Run stops running -- cancelled with it when it was cancelled, else resolved. The run
    ledger is read once without a lock to find the open stall pauses; each Run is then
    decided under its own lock, which reads the ledger again.

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
            str(pause.scope_ref)
            for pause in held.values()
            if pause.status is PauseStatus.OPEN and stall_of(pause) is not None
        }
    )
    ended: list[str] = []
    for ref in runs:
        urn = parse_qualified_urn(ref)
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
            stands = (
                f"{_STALL_KEY_PREFIX}{run.key}-{standing.anchor_sequence}" if standing else None
            )
            for pause in latest_pauses(records).values():
                key = stall_of(pause)
                if pause.status is not PauseStatus.OPEN or pause.scope_ref != urn or key is None:
                    continue
                if key != stands:
                    cancelled = status.status is RunStatus.CANCELLED
                    end_stall_pause(session, pause, cancelled=cancelled, now=now)
                    ended.append(pause.key)
    return tuple(ended)


def standing_stalls(context: Epoch2RootContext, *, now: datetime) -> RunStallsAnswer:
    """Return every stall standing over a Run the tree still stores as running.

    A stall stands while its Run is running and has produced nothing since the activity
    the stall was measured from; a Run that answered afterwards has moved past it.
    """
    authority = context.require_selected_generation()
    document = _document_path(authority)
    running = _running(document)
    records = read_ledger_records(ledger_path(document, Epoch2Collection.RUN)) if running else ()
    stalls: list[RunStallFact] = []
    for urn in running:
        if not _still_running(records, RunStatus.RUNNING, urn):
            continue
        state = reduce_run_events(run_events_of(records, urn))
        fact = standing_stall(stall_facts_of(records, urn), state.last_activity_sequence)
        if fact is not None:
            stalls.append(fact)
    return RunStallsAnswer(stalls=tuple(stalls), read_at=now)


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
    "detect_stalls",
    "settle_stall_pauses",
    "standing_stalls",
]
