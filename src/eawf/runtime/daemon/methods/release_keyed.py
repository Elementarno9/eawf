"""The bookkeeping every keyed ``release.*`` verb shares.

``release.publish``, ``release.retry_target``, ``release.reconcile``,
``release.observe_target`` and ``release.burn`` are keyed twice: by an
``idempotency_key`` that makes a repeated call a replay, and by an
``expected_revision`` that makes a stale caller a refusal. What those
five verbs then do with the ledger and the record collection is the
same each time, and it lives here once so no verb can drift from the
others:

* the request identity a replay is recognised by;
* the replay lookup, and the record a replay answers with;
* the open-operation lookup;
* the reply shape;
* persisting every record revision a call walked through.

The ledger append stays in each handler, ahead of the record rows it
precedes. The ledger is the durable boundary a crash is judged against;
the record rows are the projection ``release.show`` reads, so they land
after it.

Like :mod:`eawf.runtime.daemon.methods.release_context`, this module
imports no sibling handler module.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from eawf.kernel.spec.publication import PublicationOperation
from eawf.kernel.spec.release import Release
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.workflow.release.ledger import (
    IdempotencyConflictError,
    current_operation,
    replayed_receipt,
    request_fingerprint,
)
from eawf.workflow.release.publication import operation_reference
from eawf.workflow.release.records import read_release_record, record_release

logger = logging.getLogger(__name__)

#: Params that describe which revision the caller holds rather than
#: what the caller asked for. The record payload enters the identity
#: only as its key.
_REVISION_PARAMS: Final[frozenset[str]] = frozenset(
    {"idempotency_key", "expected_revision", "release"}
)


def request_identity(method: str, params: dict[str, Any], *, release_key: str) -> str:
    """Return the digest a replay of this request is recognised by.

    The record payload and ``expected_revision`` are left out, so a
    caller that re-reads the current record before repeating a call
    presents the same request. A repeat is exactly the call whose first
    attempt moved the record on; if the moved revision changed the
    digest, the repeat would be refused ``idempotency_conflict`` instead
    of answered with the receipt it already earned. The key stays in, so
    one idempotency key cannot replay against another checkpoint.

    Args:
        method: JSON-RPC method name.
        params: Raw request params.
        release_key: Key of the record the request acts on.

    Returns:
        The digest the idempotency index compares against.
    """
    asked = {key: value for key, value in params.items() if key not in _REVISION_PARAMS}
    return request_fingerprint(method, {**asked, "release_key": release_key})


def replayed_operation(
    state_path: Path,
    *,
    idempotency_key: str,
    fingerprint: str,
) -> PublicationOperation | None:
    """Return the snapshot an earlier identical call produced, if any.

    Args:
        state_path: Path to ``state.json``.
        idempotency_key: The key the caller presented.
        fingerprint: Digest of the request the caller presented.

    Returns:
        The recorded snapshot, or ``None`` when the key is new.

    Raises:
        DaemonValidationError: When the key was reused with a different
            request (``idempotency_conflict``), or the ledger is corrupt.
    """
    try:
        return replayed_receipt(
            state_path, idempotency_key=idempotency_key, fingerprint=fingerprint
        )
    except IdempotencyConflictError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc


def replay_record(state_path: Path, presented: Release) -> Release:
    """Return the record a replayed call answers with.

    The payload a repeating caller presents is the record from before
    its first attempt, which that attempt has since moved on. Echoing it
    back would report a status the checkpoint has left, so the recorded
    record answers instead. The presented one is the fallback only where
    the collection holds nothing for the key: a ledger row that landed
    without its record rows.

    Args:
        state_path: Path to ``state.json``.
        presented: The record the caller presented.

    Returns:
        The recorded record, or *presented*.

    Raises:
        DaemonValidationError: When the record collection is corrupt.
    """
    try:
        recorded = read_release_record(state_path, presented.key)
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    return presented if recorded is None else recorded


def open_operation(state_path: Path, release: Release) -> PublicationOperation:
    """Return the operation already open for *release*, or refuse.

    Args:
        state_path: Path to ``state.json``.
        release: The record whose episode is looked up.

    Returns:
        The highest-revision snapshot recorded for that release.

    Raises:
        DaemonValidationError: When no episode has been opened, or the
            ledger is corrupt.
    """
    try:
        operation = current_operation(state_path, release.key)
    except ValueError as exc:
        raise DaemonValidationError(f"validation_failed: {exc}") from exc
    if operation is None:
        raise DaemonValidationError(
            f"validation_failed: no publication operation is open for {release.key!r}"
        )
    return operation


def keyed_reply(
    operation: PublicationOperation,
    release: Release,
    *,
    replayed: bool,
) -> dict[str, Any]:
    """Return the wire shape every keyed verb answers with.

    Args:
        operation: The operation snapshot the call produced.
        release: The record the call produced.
        replayed: Whether this is the original receipt of an earlier
            identical call rather than fresh work.

    Returns:
        The operation reference, both records and the replay flag.
    """
    return {
        "operation_ref": operation_reference(operation),
        "operation": operation.model_dump(mode="json"),
        "release": release.model_dump(mode="json"),
        "replayed": replayed,
    }


def persist_walk(
    state_path: Path,
    walked: Sequence[Release],
    *,
    recorded_at: datetime,
    summary: str,
) -> Release:
    """Record every revision a call walked through and return the last.

    One row per revision rather than only the final one: a reconcile that
    also opens verification made two transitions, and a lineage missing
    the first would show a revision nothing produced.

    Args:
        state_path: Path to ``state.json``.
        walked: The successive records, oldest first. Never empty.
        recorded_at: Timezone-aware UTC instant of the appends.
        summary: What the call did; each row adds the status it reached.

    Returns:
        The last record of *walked*, which is the one the call answers
        with and the one ``release.show`` now reports.

    Raises:
        ValueError: When *walked* is empty, or *recorded_at* is naive.
        StateConflict: When the append lock cannot be acquired.
    """
    if not walked:
        raise ValueError("persist_walk needs at least one record to persist")
    for record in walked:
        record_release(
            state_path,
            record,
            recorded_at=recorded_at,
            summary=f"{summary}: {record.status.value}",
        )
    logger.info(
        f"persist_walk key={walked[-1].key!r} revisions="
        f"{[record.revision for record in walked]} status={walked[-1].status.value!r}"
    )
    return walked[-1]


__all__ = [
    "keyed_reply",
    "open_operation",
    "persist_walk",
    "replay_record",
    "replayed_operation",
    "request_identity",
]
