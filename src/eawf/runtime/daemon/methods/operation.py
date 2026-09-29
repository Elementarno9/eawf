"""``operation.*`` JSON-RPC methods: submit long work, and follow it.

A verb that runs gates or git work can take minutes, and holding the caller's
connection open for all of them makes the answer hostage to the wire.
``operation.submit`` instead records the operation as queued, schedules the
work on the daemon's loop and answers with the operation reference at once;
the work then records ``running`` and, when it answers, ``succeeded`` or
``failed`` carrying exactly the result or error a direct call would have
returned. ``wait`` blocks the same submission until that terminal row, so a
waited call and a followed one run the same work under the same record.

``operation.follow`` reads one operation's trail from a cursor. It never
writes: an operation whose daemon is gone is reported ``unknown`` by
projection, not by appending a row.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final
from uuid import UUID

import orjson
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from eawf.kernel.store.kinds.operation import TERMINAL_STATES, OperationRecord, OperationState
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.delivery import DELIVERY_INTEGRATE_METHOD
from eawf.runtime.daemon.methods.delivery_proof import DELIVERY_PROVE_METHOD
from eawf.runtime.daemon.methods.release_context import require_state_path
from eawf.runtime.daemon.native_guard import REPO_ROOT_PARAM, native_params, native_root
from eawf.runtime.daemon.operations import (
    advance,
    append_record,
    observed,
    operation_id_for,
    operation_reference,
    parse_reference,
    publication_trail,
    read_trail,
    settle,
)
from eawf.runtime.daemon.server import process_frame_bytes
from eawf.workflow.release.ledger import read_ledger, request_fingerprint

logger = logging.getLogger(__name__)

#: Records a long verb as an operation and runs it in the background.
OPERATION_SUBMIT_METHOD: Final = "operation.submit"

#: Reads one operation's trail from a cursor.
OPERATION_FOLLOW_METHOD: Final = "operation.follow"

#: The verbs a caller submits as operations rather than awaits: each runs
#: gates or git work inside the request. ``release.publish`` is not here
#: because it already answers with its publication's reference.
LONG_RUNNING_METHODS: Final[frozenset[str]] = frozenset(
    {DELIVERY_PROVE_METHOD, DELIVERY_INTEGRATE_METHOD}
)

#: The work each daemon-run operation is doing, by operation id. A task is
#: kept referenced here until it finishes, or the loop may collect it.
_OPERATION_TASKS: dict[UUID, asyncio.Task[None]] = {}


class OperationSubmitParams(BaseModel):
    """Params of :data:`OPERATION_SUBMIT_METHOD`.

    Attributes:
        method: The long verb to run, one of :data:`LONG_RUNNING_METHODS`.
        params: That verb's own params, less ``repo_root``.
        idempotency_key: The caller's key; resubmitting it names the same
            operation.
        wait: Whether to answer only once the operation is terminal.
    """

    model_config = ConfigDict(extra="forbid")

    method: str
    params: dict[str, Any]
    idempotency_key: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]
    wait: bool = False


class OperationFollowParams(BaseModel):
    """Params of :data:`OPERATION_FOLLOW_METHOD`.

    Attributes:
        operation_ref: The reference a submission answered with.
        cursor: How many revisions the caller has already read; the answer
            carries the ones from there on.
    """

    model_config = ConfigDict(extra="forbid")

    operation_ref: str
    cursor: Annotated[int, Field(ge=0)] = 0


def _instance(ctx: MethodContext) -> str:
    """Return the identity of this daemon process, as operation rows name it."""
    return f"{ctx.pid}@{ctx.started_at}"


def _live(operation_id: UUID) -> bool:
    """Return whether this process is still running *operation_id*'s work."""
    task = _OPERATION_TASKS.get(operation_id)
    return task is not None and not task.done()


def _tree_state_path(ctx: MethodContext, params: dict[str, Any]) -> Path:
    """Return the ``state.json`` of the tree one request addresses.

    Raises:
        DaemonValidationError: The request names no tree and the daemon is
            bound to none, so there is nowhere to keep the operation.
    """
    root = native_root(ctx, params)
    if root is None:
        raise DaemonValidationError(
            f"validation_failed: operation_tree_unresolved: the request names no usable "
            f"{REPO_ROOT_PARAM} and the daemon is bound to no tree"
        )
    return root / "state.json"


async def _run(
    ctx: MethodContext, state_path: Path, record: OperationRecord, params: dict[str, Any]
) -> None:
    """Run one queued operation's work and record each state it reaches.

    The work is dispatched as the same frame a direct call sends, so the
    terminal row carries the very result or error that call would have
    answered with. Counting it in flight keeps an idle daemon from exiting
    under it and lets a draining shutdown wait for it.
    """
    running = advance(record, OperationState.RUNNING, at=datetime.now(UTC))
    append_record(state_path, running)
    frame = orjson.dumps(
        {"jsonrpc": "2.0", "id": str(record.operation_id), "method": record.verb, "params": params}
    )
    ctx.in_flight_mutations += 1
    try:
        response = orjson.loads(await process_frame_bytes(frame, ctx))
    except asyncio.CancelledError:
        append_record(state_path, advance(running, OperationState.UNKNOWN, at=datetime.now(UTC)))
        raise
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)
    append_record(state_path, settle(running, response, at=datetime.now(UTC)))


def _reply(record: OperationRecord, *, replayed: bool) -> dict[str, Any]:
    """Return the wire shape a submission answers with."""
    return {
        "operation_ref": operation_reference(record),
        "operation": record.model_dump(mode="json"),
        "replayed": replayed,
    }


@register(OPERATION_SUBMIT_METHOD)
async def submit(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Record one long verb as an operation, start it, and answer its reference.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`OperationSubmitParams`, plus the
            ``repo_root`` of the tree the work addresses.

    Returns:
        The operation reference, the operation's latest revision (queued, or
        terminal under ``wait`` or on a replay of a finished operation) and
        whether the key named an operation already submitted.

    Raises:
        DaemonValidationError: The params do not parse, the verb is not a
            long one, the tree is unresolved, or the key already names a
            different request (``idempotency_conflict``).
    """
    args = native_params(OperationSubmitParams, params)
    if args.method not in LONG_RUNNING_METHODS:
        raise DaemonValidationError(
            f"validation_failed: operation_not_long_running: {args.method!r} answers "
            f"directly; submit one of {sorted(LONG_RUNNING_METHODS)}"
        )
    subject = args.params.get("urn")
    if not isinstance(subject, str) or not subject:
        raise DaemonValidationError("validation_failed: schema_validation_failed: check params.urn")
    state_path = _tree_state_path(ctx, params)
    fingerprint = request_fingerprint(args.method, args.params)
    operation_id = operation_id_for(args.method, args.idempotency_key)
    trail = read_trail(state_path, operation_id)
    replayed = bool(trail)
    if replayed and trail[-1].request_fingerprint != fingerprint:
        raise DaemonValidationError(
            f"validation_failed: idempotency_conflict: key {args.idempotency_key!r} already "
            f"names operation {operation_reference(trail[-1])} for a different request"
        )
    if not replayed:
        now = datetime.now(UTC)
        queued = OperationRecord(
            operation_id=operation_id,
            verb=args.method,
            subject=subject,
            idempotency_key=args.idempotency_key,
            request_fingerprint=fingerprint,
            state=OperationState.QUEUED,
            revision=0,
            submitted_at=now,
            updated_at=now,
            daemon_instance=_instance(ctx),
        )
        append_record(state_path, queued)
        work = {**args.params}
        if REPO_ROOT_PARAM in params:
            work[REPO_ROOT_PARAM] = params[REPO_ROOT_PARAM]
        _OPERATION_TASKS[operation_id] = asyncio.create_task(
            _run(ctx, state_path, queued, work), name=f"operation-{operation_id}"
        )
        logger.info(f"submit operation_id={operation_id} verb={args.method!r}")
    if args.wait and _live(operation_id):
        await asyncio.shield(_OPERATION_TASKS[operation_id])
    latest = read_trail(state_path, operation_id)[-1]
    return _reply(
        observed(latest, instance=_instance(ctx), live=_live(operation_id)), replayed=replayed
    )


@register(OPERATION_FOLLOW_METHOD)
async def follow(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Answer one operation's revisions from the caller's cursor on.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`OperationFollowParams`, plus the
            ``repo_root`` of the tree a daemon-run operation lives in.

    Returns:
        The reference, the revisions at or past the cursor, the operation as
        it stands now, whether that is terminal, and the cursor to send next.

    Raises:
        DaemonValidationError: The params or reference do not parse, the
            tree is unresolved, or no operation has that reference
            (``operation_not_found``).
    """
    args = native_params(OperationFollowParams, params)
    try:
        release_ref, operation_id = parse_reference(args.operation_ref)
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    if release_ref is None:
        trail = read_trail(_tree_state_path(ctx, params), operation_id)
    else:
        trail = publication_trail(read_ledger(require_state_path(ctx)).values(), operation_id)
    if not trail:
        raise DaemonValidationError(
            f"validation_failed: operation_not_found: no operation {args.operation_ref!r}"
        )
    latest = observed(trail[-1], instance=_instance(ctx), live=_live(operation_id))
    if latest is not trail[-1]:
        trail = (*trail, latest)
    return {
        "operation_ref": args.operation_ref,
        "records": [
            record.model_dump(mode="json") for record in trail if record.revision >= args.cursor
        ],
        "operation": latest.model_dump(mode="json"),
        "terminal": latest.state in TERMINAL_STATES,
        "cursor": latest.revision + 1,
    }


__all__ = [
    "LONG_RUNNING_METHODS",
    "OPERATION_FOLLOW_METHOD",
    "OPERATION_SUBMIT_METHOD",
    "follow",
    "submit",
]
