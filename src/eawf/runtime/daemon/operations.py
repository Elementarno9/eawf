"""The daemon's operation store: one trail of rows per submitted operation.

A long-running verb answers with an operation reference instead of its
result. The reference names a trail of :class:`OperationRecord` rows -- one
per state the operation reached -- which a follower reads from a cursor, so a
dropped connection costs nothing but a reconnect.

Two trails answer to the same reference scheme. An operation the daemon runs
itself lives in the machine-local ``operation`` collection, appended through
the same envelope store every other collection uses. A release publication
is already recorded, revision by revision, in the publication ledger; its
reference (``operation://REL-<version>/<id>``) is read back from there and
stated in the same vocabulary, so a follower never needs to know which verb
opened the operation it holds.

A row whose daemon is gone is not left reading as running forever: the
trail a follower gets ends in ``unknown`` whenever the daemon named on a
non-terminal row is not the one answering, or is but holds no live task for
it. That is a projection, not a write -- reading an operation never appends
to it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from eawf.kernel.spec.publication import PublicationOperation, PublicationOperationKind
from eawf.kernel.spec.publication import PublicationOperationStatus as PublicationStatus
from eawf.kernel.spec.release import ReleaseTargetStatus
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.operation import (
    TERMINAL_STATES,
    OperationError,
    OperationRecord,
    OperationState,
)
from eawf.kernel.store.paths import local_store_path

logger = logging.getLogger(__name__)

#: The scheme every operation reference carries.
OPERATION_SCHEME: Final = "operation://"

#: Names an operation id after the verb and key that opened it, so a
#: resubmission under the same key names the same operation.
_OPERATION_NAMESPACE: Final = uuid.UUID("5b0c7d1e-2f64-4c1a-9a57-0e6f3b8d2c41")

#: The verb each publication episode kind was opened by.
_PUBLICATION_VERBS: Final[dict[PublicationOperationKind, str]] = {
    PublicationOperationKind.PUBLISH: "release.publish",
    PublicationOperationKind.RETRY_TARGET: "release.retry_target",
    PublicationOperationKind.RECONCILE: "release.reconcile",
}

#: The leg results that count as a publication that worked.
_SUCCEEDED_TARGETS: Final = frozenset(
    {ReleaseTargetStatus.REPORTED_SUCCESS, ReleaseTargetStatus.OBSERVED_SUCCESS}
)


def operation_id_for(method: str, idempotency_key: str) -> uuid.UUID:
    """Return the id of the operation *method* opens under *idempotency_key*.

    Args:
        method: The dotted JSON-RPC name of the work.
        idempotency_key: The caller's key for the request.

    Returns:
        A stable UUID: the same verb and key always name the same operation.
    """
    return uuid.uuid5(_OPERATION_NAMESPACE, f"{method}:{idempotency_key}")


def operation_reference(record: OperationRecord) -> str:
    """Return the reference a caller follows *record*'s operation by.

    Args:
        record: Any revision of a daemon-run operation.

    Returns:
        ``operation://<operation_id>``.
    """
    return f"{OPERATION_SCHEME}{record.operation_id}"


def parse_reference(reference: str) -> tuple[str | None, uuid.UUID]:
    """Split an operation reference into its release key and operation id.

    Args:
        reference: ``operation://<id>`` for a daemon-run operation, or
            ``operation://REL-<version>/<id>`` for a release publication.

    Returns:
        The release key (``None`` for a daemon-run operation) and the id.

    Raises:
        ValueError: When the reference does not have either shape.
    """
    body = reference.removeprefix(OPERATION_SCHEME)
    release_ref, _, raw_id = body.rpartition("/")
    if body == reference or (release_ref and not release_ref.startswith("REL-")):
        raise ValueError(
            f"operation_ref_invalid: {reference!r} is neither operation://<id> nor "
            "operation://REL-<version>/<id>"
        )
    try:
        operation_id = uuid.UUID(raw_id)
    except ValueError as exc:
        raise ValueError(f"operation_ref_invalid: {raw_id!r} is not an operation id") from exc
    return release_ref or None, operation_id


def operation_store_path(state_path: Path) -> Path:
    """Return the machine-local JSONL file daemon-run operations are kept in.

    Args:
        state_path: Path to the tree's ``state.json``.

    Returns:
        ``<state_dir>/local/operation.jsonl``.
    """
    return local_store_path(state_path, StoreKind.OPERATION)


def append_record(state_path: Path, record: OperationRecord) -> None:
    """Append one revision of an operation to its tree's store.

    Args:
        state_path: Path to the tree's ``state.json``.
        record: The revision to append.

    Raises:
        StateConflict: When the append lock cannot be acquired.
    """
    append_envelope(
        operation_store_path(state_path),
        Envelope(
            id=f"{record.operation_id}:{record.revision}",
            kind=StoreKind.OPERATION,
            scope_id=record.subject,
            created_at=record.updated_at,
            summary=f"{record.verb} {record.state.value}",
            payload=record.model_dump(mode="json"),
        ),
    )
    logger.info(
        f"append_record operation_id={record.operation_id} verb={record.verb!r} "
        f"state={record.state.value} revision={record.revision}"
    )


def read_trail(state_path: Path, operation_id: uuid.UUID) -> tuple[OperationRecord, ...]:
    """Return every recorded revision of one operation, oldest first.

    Args:
        state_path: Path to the tree's ``state.json``.
        operation_id: The operation to read.

    Returns:
        The revisions in revision order; empty when none was recorded.

    Raises:
        ValueError: When a line is not an operation row. A corrupt store
            refuses rather than skips, since a skipped terminal row would
            leave a finished operation reading as running.
    """
    path = operation_store_path(state_path)
    if not path.is_file():
        return ()
    trail: list[OperationRecord] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = OperationRecord.model_validate(Envelope.model_validate_json(line).payload)
        except ValidationError as exc:
            raise ValueError(
                f"operation store {path} line {number} is not an operation: {exc}"
            ) from exc
        if record.operation_id == operation_id:
            trail.append(record)
    return tuple(sorted(trail, key=lambda record: record.revision))


def advance(
    record: OperationRecord,
    state: OperationState,
    *,
    at: datetime,
    result: dict[str, Any] | None = None,
    error: OperationError | None = None,
) -> OperationRecord:
    """Return the next revision of *record*, standing at *state*.

    Args:
        record: The operation's latest revision.
        state: Where the operation now stands.
        at: When the move happened, timezone-aware.
        result: The answer, for a success.
        error: The error, for a failure.

    Returns:
        The validated next revision.

    Raises:
        ValueError: When *record* is already terminal, or the outcome does
            not match *state*.
    """
    if record.state in TERMINAL_STATES:
        raise ValueError(
            f"operation {record.operation_id} is {record.state.value}; no state follows it"
        )
    return OperationRecord.model_validate(
        {
            **record.model_dump(),
            "state": state,
            "revision": record.revision + 1,
            "updated_at": at,
            "result": result,
            "error": error,
        }
    )


def settle(record: OperationRecord, response: dict[str, Any], *, at: datetime) -> OperationRecord:
    """Return the terminal revision one JSON-RPC response settles *record* at.

    Args:
        record: The operation's running revision.
        response: The response frame the work answered with, as a direct
            call would have received it.
        at: When the work answered.

    Returns:
        ``succeeded`` carrying the result, or ``failed`` carrying the error.
    """
    if "error" in response:
        return advance(
            record, OperationState.FAILED, at=at, error=OperationError(**response["error"])
        )
    return advance(record, OperationState.SUCCEEDED, at=at, result=response["result"])


def observed(record: OperationRecord, *, instance: str, live: bool) -> OperationRecord:
    """Return *record* as a follower should read it now.

    Args:
        record: The operation's latest recorded revision.
        instance: The identity of the daemon answering.
        live: Whether that daemon holds a live task for the operation.

    Returns:
        *record* unchanged, or an ``unknown`` revision after it when the row
        is not terminal and nothing is running it any more.
    """
    if record.state in TERMINAL_STATES or record.daemon_instance is None:
        return record
    if record.daemon_instance == instance and live:
        return record
    return advance(record, OperationState.UNKNOWN, at=record.updated_at)


def publication_trail(
    snapshots: Iterable[PublicationOperation], operation_id: uuid.UUID
) -> tuple[OperationRecord, ...]:
    """State one publication episode's ledger snapshots as an operation trail.

    Args:
        snapshots: Publication ledger snapshots, in any order.
        operation_id: The episode to read.

    Returns:
        One record per snapshot of that episode, oldest first.
    """
    episode = sorted(
        (snapshot for snapshot in snapshots if snapshot.operation_id == operation_id),
        key=lambda snapshot: snapshot.revision,
    )
    return tuple(_publication_record(snapshot) for snapshot in episode)


def _publication_record(snapshot: PublicationOperation) -> OperationRecord:
    """Return one publication snapshot in the operation vocabulary."""
    moments = [snapshot.opened_at]
    for row in snapshot.publication_receipts:
        moments.append(row.settled_at or row.started_at)
    return OperationRecord(
        operation_id=snapshot.operation_id,
        verb=_PUBLICATION_VERBS[snapshot.kind],
        subject=snapshot.release_ref,
        idempotency_key=snapshot.idempotency_key,
        request_fingerprint=snapshot.request_fingerprint,
        state=_publication_state(snapshot),
        revision=snapshot.revision,
        submitted_at=snapshot.opened_at,
        updated_at=max(moments),
        result=snapshot.model_dump(mode="json"),
    )


def _publication_state(snapshot: PublicationOperation) -> OperationState:
    """Return where one publication episode stands.

    Open legs are running work. A settled episode succeeded only when the
    latest attempt of every target worked; a target whose outcome is
    unknown makes the episode unknown, and any other settled leg failed it.
    An abandoned episode was given up on, which is a failure.
    """
    if snapshot.status is PublicationStatus.OPEN:
        return OperationState.RUNNING
    if snapshot.status is PublicationStatus.ABANDONED:
        return OperationState.FAILED
    latest = {row.target_id: row.status for row in snapshot.publication_receipts}
    if ReleaseTargetStatus.UNKNOWN in latest.values():
        return OperationState.UNKNOWN
    if set(latest.values()) <= _SUCCEEDED_TARGETS:
        return OperationState.SUCCEEDED
    return OperationState.FAILED


__all__ = [
    "OPERATION_SCHEME",
    "advance",
    "append_record",
    "observed",
    "operation_id_for",
    "operation_reference",
    "operation_store_path",
    "parse_reference",
    "publication_trail",
    "read_trail",
    "settle",
]
