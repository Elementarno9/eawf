"""The durable collection of Release records, one row per revision.

:mod:`eawf.workflow.release.ledger` remembers what a *publication* call
produced. Nothing remembered the checkpoint record itself, so a record
opened by ``release.create`` and carried to APPROVED by
``release.approve`` lived only in the reply of whichever RPC produced it
-- an approval that no reader could find again is indistinguishable from
one that never happened.

This module is that reader's store. It lives in its own canonical JSONL
file under :attr:`~eawf.kernel.state.enums.StoreKind.RELEASE_RECORD`
rather than beside the publication ledger, because the two families key
their rows differently: a ledger row is addressed by the operator's
idempotency key, a record row by ``<release key>@<revision>``. Sharing
one id namespace would let an unlucky idempotency key collide with a
release revision and silently drop one of them at compaction.

One row per revision rather than one row per release: an append is then
idempotent for a replayed transition (same key, same revision, same
envelope id) while the lineage from DRAFT to APPROVED stays readable.
The collection reads back the highest revision per release key, which is
the record's current state.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from eawf.kernel.spec.release import Release
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.workflow.release.boundaries import PublicationBoundary, durable_boundary

logger = logging.getLogger(__name__)


def release_records_path(state_path: Path) -> Path:
    """Return the JSONL path of the release-record collection.

    Args:
        state_path: Path to ``state.json``.

    Returns:
        ``<state_dir>/store/release_record.jsonl``.
    """
    return store_path(state_path, StoreKind.RELEASE_RECORD)


def record_envelope_id(release: Release) -> str:
    """Return the envelope id identifying one revision of *release*.

    Args:
        release: The record being written.

    Returns:
        ``<key>@<revision>``, which is stable for a replayed transition
        and distinct for every real one.
    """
    return f"{release.key}@{release.revision}"


@durable_boundary(PublicationBoundary.TRANSITION_APPLY)
def record_release(
    state_path: Path,
    release: Release,
    *,
    recorded_at: datetime,
    summary: str,
) -> Release:
    """Append *release* to the collection and return it unchanged.

    Args:
        state_path: Path to ``state.json``.
        release: The record to persist.
        recorded_at: Timezone-aware UTC instant of the append.
        summary: One-line operator-facing description.

    Returns:
        *release*, so a caller can persist and return in one expression.

    Raises:
        TypeError: When *release* is not a
            :class:`~eawf.kernel.spec.release.Release`.
        ValueError: When *recorded_at* is naive. A row whose instant
            carries no zone cannot be ordered against the others.
        StateConflict: When the append lock cannot be acquired.
    """
    if not isinstance(release, Release):
        raise TypeError(f"release must be Release; got {type(release).__name__}")
    if recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be timezone-aware")
    append_envelope(
        release_records_path(state_path),
        Envelope(
            id=record_envelope_id(release),
            kind=StoreKind.RELEASE_RECORD,
            scope_id=release.key,
            created_at=recorded_at,
            summary=summary[:500],
            payload=release.model_dump(mode="json"),
        ),
    )
    logger.info(
        f"record_release key={release.key!r} status={release.status.value!r} "
        f"revision={release.revision}"
    )
    return release


def read_release_records(state_path: Path) -> dict[str, Release]:
    """Return the current record of every release the collection carries.

    Args:
        state_path: Path to ``state.json``.

    Returns:
        A mapping of ``REL-<version>`` key to the highest-revision
        record written under it. Empty when nothing has been recorded.

    Raises:
        ValueError: When a line is not a valid envelope, is filed under
            another store kind, or does not carry a release record. A
            corrupt collection is a refusal rather than a skipped line:
            skipping is how an approval quietly disappears.
    """
    path = release_records_path(state_path)
    if not path.exists():
        return {}
    latest: dict[str, Release] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = _record(line, path, number)
        current = latest.get(record.key)
        if current is None or record.revision >= current.revision:
            latest[record.key] = record
    return latest


def read_release_record(state_path: Path, key: str) -> Release | None:
    """Return the current record filed under *key*, or ``None``.

    Args:
        state_path: Path to ``state.json``.
        key: ``REL-<version>`` key to look up.

    Returns:
        The highest-revision record, or ``None`` when the collection
        carries no row for *key*.

    Raises:
        ValueError: When the collection is corrupt.
    """
    return read_release_records(state_path).get(key)


def _record(line: str, path: Path, number: int) -> Release:
    """Return the release record carried by the envelope on *line*.

    Args:
        line: One raw JSONL line.
        path: Collection path, for the error message.
        number: 1-based line number, for the error message.

    Returns:
        The validated record.

    Raises:
        ValueError: When the line is not an envelope of the right kind,
            or its payload is not a release record.
    """
    try:
        envelope = Envelope.model_validate_json(line)
    except ValidationError as exc:
        raise ValueError(
            f"release record collection {path} line {number} is not an envelope: {exc}"
        ) from exc
    if envelope.kind is not StoreKind.RELEASE_RECORD:
        raise ValueError(
            f"release record collection {path} line {number} is filed under "
            f"{envelope.kind.value!r}, not {StoreKind.RELEASE_RECORD.value!r}"
        )
    try:
        return Release.model_validate(envelope.payload)
    except ValidationError as exc:
        raise ValueError(
            f"release record collection {path} line {number} payload is not a release: {exc}"
        ) from exc


__all__ = [
    "read_release_record",
    "read_release_records",
    "record_envelope_id",
    "record_release",
    "release_records_path",
]
