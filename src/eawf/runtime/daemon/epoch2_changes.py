"""The change feed's two steps inside a native commit: journal its records, then file them.

A commit decides its change records from the document it read and the document it is
about to write, before the WAL record exists, and puts their lines in the envelope the
WAL journals. Each tier of the feed the records land in is asked of the commit policy at
the same point, so an undeclared feed refuses the commit having written nothing. Once
the document write is durable the records are appended, and a crash between the two is
finished by the replay from the journalled lines (:mod:`eawf.kernel.store.changes`).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from eawf.kernel.store.changes import (
    ChangeRecord,
    append_change_record,
    change_log_path,
    document_changes,
)
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.epoch2_recovery import CHANGE_LINES_KEY
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext


def journal_changes(
    context: Epoch2RootContext,
    *,
    document_path: Path,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    envelope: Envelope,
    event_name: str,
    sequence: int,
    actor_ref: str | None,
    now: datetime,
) -> tuple[Envelope, tuple[ChangeRecord, ...]]:
    """Return *envelope* carrying the change records of a commit, beside the records.

    Args:
        context: The native context of the tree the commit writes.
        document_path: The selected generation's document.
        before: The document the commit read.
        after: The document the commit writes.
        envelope: The commit's firehose row, whose id names the records.
        event_name: The name of the event the commit emits.
        sequence: The ordinal the commit drew.
        actor_ref: Who asked for the commit, if it names anyone.
        now: When the commit happened.

    Returns:
        The envelope with the records' lines journalled in it, unchanged when the commit
        changed no row, and the records.

    Raises:
        UndeclaredPathError: The commit policy declares no row for a tier of the feed.
        TypeError: A changed value is not JSON.
    """
    changes = document_changes(
        before,
        after,
        event_id=envelope.id,
        event_name=event_name,
        canonical_sequence=sequence,
        actor_ref=actor_ref,
        recorded_at=now,
    )
    if not changes:
        return envelope, changes
    for tier in sorted({record.tier for record in changes}):
        context.declared_path(change_log_path(document_path, tier))
    lines = [record.model_dump_json() for record in changes]
    payload = {**envelope.payload, CHANGE_LINES_KEY: lines}
    return envelope.model_copy(update={"payload": payload}), changes


def file_changes(document_path: Path, changes: tuple[ChangeRecord, ...]) -> None:
    """Append a commit's change records, once its document write is durable.

    Raises:
        StateConflict: A tier's append lock stayed held; the replay files the records.
    """
    for record in changes:
        append_change_record(document_path, record)


__all__ = ["file_changes", "journal_changes"]
