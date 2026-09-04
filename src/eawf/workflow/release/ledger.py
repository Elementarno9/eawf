"""The durable publication ledger and its idempotency index.

Every publication RPC is an *external-effect* verb, so retrying one must
be safe by construction rather than by operator care. That safety is one
rule: a call is identified by its idempotency key, and the ledger
remembers what that key already produced.

* A key the ledger has never seen opens a new record.
* A key it has seen, presented with the same request, returns the
  **original** receipt -- not a fresh publication that happens to look
  the same.
* A key it has seen, presented with a *different* request, is refused
  (:class:`IdempotencyConflictError`). Silently honouring it would let a
  changed payload inherit an earlier call's identity, which is exactly
  how a double-publication hides.

The ledger lives in the canonical JSONL store at
``<state_dir>/store/release.jsonl`` under :attr:`StoreKind.RELEASE`.
Each envelope's id **is** the idempotency key and its payload is the
snapshot of the operation that call produced, so replay is a lookup
rather than a re-derivation, and the highest-revision snapshot for a
release is the operation's current state.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eawf.kernel.spec.publication import PublicationOperation
from eawf.kernel.spec.release import Release
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path

logger = logging.getLogger(__name__)


class StaleReleaseRevisionError(ValueError):
    """The caller's ``expected_revision`` is not the record's revision.

    Attributes:
        expected: The revision the caller believed it was mutating.
        actual: The revision the record actually carries.
    """

    def __init__(self, expected: int, actual: int) -> None:
        """Store both revisions alongside the operator-facing message."""
        super().__init__(
            f"stale_release_revision: expected revision {expected}, record is at {actual}"
        )
        self.expected = expected
        self.actual = actual


class IdempotencyConflictError(ValueError):
    """An idempotency key was reused with a different request.

    Attributes:
        idempotency_key: The reused key.
        recorded_fingerprint: The request digest the key already carries.
        offered_fingerprint: The digest of the request now presented.
    """

    def __init__(
        self,
        idempotency_key: str,
        recorded_fingerprint: str | None,
        offered_fingerprint: str,
    ) -> None:
        """Store both fingerprints alongside the operator-facing message."""
        super().__init__(
            f"idempotency_conflict: key {idempotency_key!r} already carries request "
            f"{recorded_fingerprint!r}, not {offered_fingerprint!r}"
        )
        self.idempotency_key = idempotency_key
        self.recorded_fingerprint = recorded_fingerprint
        self.offered_fingerprint = offered_fingerprint


def request_fingerprint(method: str, params: Mapping[str, Any]) -> str:
    """Return the digest identifying one operator request.

    The method name is part of the digest so the same key cannot be
    replayed across two different verbs.

    Args:
        method: JSON-RPC method name, e.g. ``release.publish``.
        params: The request params, minus the idempotency key itself.

    Returns:
        A ``sha256:``-prefixed digest.
    """
    body = json.dumps(
        {"method": method, "params": params}, sort_keys=True, separators=(",", ":"), default=str
    )
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def assert_fresh_revision(release: Release, expected_revision: int) -> None:
    """Raise unless *release* is at *expected_revision*.

    This is the compare-and-swap that makes a publication verb safe to
    issue from two places at once: the second caller, holding the older
    revision, is refused rather than overwriting the first.

    Args:
        release: The record being mutated.
        expected_revision: The revision the caller believes it holds.

    Returns:
        ``None`` when the revisions agree.

    Raises:
        StaleReleaseRevisionError: When they do not.
    """
    if release.revision != expected_revision:
        raise StaleReleaseRevisionError(expected_revision, release.revision)


def ledger_path(state_path: Path) -> Path:
    """Return the JSONL path of the publication ledger.

    Args:
        state_path: Path to ``state.json``.

    Returns:
        ``<state_dir>/store/release.jsonl``.
    """
    return store_path(state_path, StoreKind.RELEASE)


def read_ledger(state_path: Path) -> dict[str, PublicationOperation]:
    """Return the latest snapshot recorded under each idempotency key.

    Args:
        state_path: Path to ``state.json``.

    Returns:
        A mapping of idempotency key to the operation snapshot that key
        produced. Empty when the ledger file does not exist yet.

    Raises:
        ValueError: When a line is not a valid envelope, or its payload
            is not a publication operation. A corrupt ledger is a
            refusal, never a silently skipped line: skipping is how a
            replay turns into a second publication.
    """
    path = ledger_path(state_path)
    if not path.exists():
        return {}
    snapshots: dict[str, PublicationOperation] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        snapshots[_envelope_id(line, path, number)] = _payload(line, path, number)
    return snapshots


def _envelope_id(line: str, path: Path, number: int) -> str:
    """Return the envelope id on *line*.

    Args:
        line: One raw JSONL line.
        path: Ledger path, for the error message.
        number: 1-based line number, for the error message.

    Returns:
        The envelope id, which is the call's idempotency key.

    Raises:
        ValueError: When the line is not a valid envelope.
    """
    try:
        return Envelope.model_validate_json(line).id
    except ValidationError as exc:
        raise ValueError(
            f"publication ledger {path} line {number} is not an envelope: {exc}"
        ) from exc


def _payload(line: str, path: Path, number: int) -> PublicationOperation:
    """Return the operation carried by the envelope on *line*.

    Args:
        line: One raw JSONL line.
        path: Ledger path, for the error message.
        number: 1-based line number, for the error message.

    Returns:
        The validated operation snapshot.

    Raises:
        ValueError: When the payload is not a publication operation.
    """
    try:
        return PublicationOperation.model_validate(Envelope.model_validate_json(line).payload)
    except ValidationError as exc:
        raise ValueError(
            f"publication ledger {path} line {number} payload is not an operation: {exc}"
        ) from exc


def replayed_receipt(
    state_path: Path,
    *,
    idempotency_key: str,
    fingerprint: str,
) -> PublicationOperation | None:
    """Return the receipt *idempotency_key* already produced, if any.

    Args:
        state_path: Path to ``state.json``.
        idempotency_key: The key the caller presented.
        fingerprint: Digest of the request the caller presented.

    Returns:
        The original snapshot when the key is known and the request
        matches, or ``None`` when the key is new.

    Raises:
        IdempotencyConflictError: When the key is known but carries a
            different request.
        ValueError: When the ledger is corrupt.
    """
    recorded = read_ledger(state_path).get(idempotency_key)
    if recorded is None:
        return None
    if recorded.request_fingerprint != fingerprint:
        raise IdempotencyConflictError(idempotency_key, recorded.request_fingerprint, fingerprint)
    logger.info(
        f"replayed_receipt idempotency_key={idempotency_key!r} "
        f"operation_id={recorded.operation_id} revision={recorded.revision}"
    )
    return recorded


def current_operation(state_path: Path, release_ref: str) -> PublicationOperation | None:
    """Return the highest-revision snapshot recorded for *release_ref*.

    Args:
        state_path: Path to ``state.json``.
        release_ref: ``REL-<version>`` key to look up.

    Returns:
        The operation's current state, or ``None`` when no episode has
        been opened for that release.

    Raises:
        ValueError: When the ledger is corrupt.
    """
    candidates = [
        snapshot
        for snapshot in read_ledger(state_path).values()
        if snapshot.release_ref == release_ref
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda snapshot: snapshot.revision)


def record_operation(
    state_path: Path,
    operation: PublicationOperation,
    *,
    idempotency_key: str,
    fingerprint: str,
    recorded_at: datetime,
    summary: str,
) -> PublicationOperation:
    """Append the snapshot *idempotency_key* produced and return it.

    Args:
        state_path: Path to ``state.json``.
        operation: The operation snapshot to record.
        idempotency_key: The call's key; becomes the envelope id, which
            is what makes the append idempotent on replay.
        fingerprint: Digest of the request that produced the snapshot.
        recorded_at: Timezone-aware UTC instant of the append.
        summary: One-line operator-facing description.

    Returns:
        The snapshot as recorded, carrying the fingerprint.

    Raises:
        ValueError: When *recorded_at* is naive.
        StateConflict: When the append lock cannot be acquired.
    """
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    stamped = operation.model_copy(update={"request_fingerprint": fingerprint})
    append_envelope(
        ledger_path(state_path),
        Envelope(
            id=idempotency_key,
            kind=StoreKind.RELEASE,
            scope_id=operation.release_ref,
            created_at=recorded_at,
            summary=summary[:500],
            payload=stamped.model_dump(mode="json"),
        ),
    )
    logger.info(
        f"record_operation release_ref={operation.release_ref!r} "
        f"operation_id={operation.operation_id} revision={operation.revision} "
        f"idempotency_key={idempotency_key!r}"
    )
    return stamped


__all__ = [
    "IdempotencyConflictError",
    "StaleReleaseRevisionError",
    "assert_fresh_revision",
    "current_operation",
    "ledger_path",
    "read_ledger",
    "record_operation",
    "replayed_receipt",
    "request_fingerprint",
]
