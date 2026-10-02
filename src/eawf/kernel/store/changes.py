"""The per-entity change feed: which fields one commit changed, before and after.

The document keeps only the latest value of every field, and a transition row on the
firehose carries a status, a revision and an actor, so nothing could say what a field
held before. Every commit that rewrites document rows therefore files one
:class:`ChangeRecord` per row it changed, listing each changed field with the value it
held before and the value it holds after.

A record is filed in the same transaction as the commit that caused it: the lines are
journalled inside the commit's WAL envelope, appended once the document write is durable,
and appended again by the replay only when missing, so a commit that never lands files no
change and one that lands is never left without its change.

The feed follows the status split of the document (:mod:`eawf.kernel.store.compaction`).
A change to a Run, or to a Task entering or moving within an in-flight status, is
machine-local, because every claim, start and completion would otherwise rewrite a
tracked file; it lands under ``local/history/``. Every other change is a committed
definition and lands under ``history/`` beside the generation's ledgers.

Values are bounded. A value whose canonical JSON is at most :data:`VALUE_BYTE_CAP` bytes
is kept verbatim; a larger one keeps the first :data:`PREVIEW_CHARS` characters of that
JSON, its byte size and its sha256 digest, which is enough to show that it changed and to
match it against a copy, never enough to restore it. ``revision`` and ``updated_at`` move
on every commit and are carried by the record itself, so they are never listed as fields.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.append import append_json_line
from eawf.kernel.store.compaction import IN_FLIGHT_TASK_STATUSES
from eawf.kernel.store.paths import LOCAL_DIRNAME
from eawf.kernel.store.tiers import STATUS_PROJECTION_COLLECTIONS, Epoch2Collection

logger = logging.getLogger(__name__)

#: The day the producer shipped; an entity with no change on file has none since then.
CHANGE_FEED_SINCE: Final = date(2026, 10, 2)

#: The directory a generation's change feed lives in, beside ``ledger/``.
CHANGE_DIRNAME: Final = "history"

#: The file one tier of the change feed is appended to.
CHANGE_FILENAME: Final = "change.jsonl"

#: The largest canonical JSON, in bytes, a field value is kept verbatim at.
VALUE_BYTE_CAP: Final = 256

#: How many characters of a larger value's canonical JSON are kept as its preview.
PREVIEW_CHARS: Final = 64

#: The fields every commit moves, which the record carries rather than lists.
UNLISTED_FIELDS: Final = frozenset({"revision", "updated_at"})

#: The most records one read of the feed returns.
MAX_PAGE: Final = 200

_IN_FLIGHT_VALUES: Final = frozenset(status.value for status in IN_FLIGHT_TASK_STATUSES)
_ABSENT: Final = object()


class ChangeTier(StrEnum):
    """Whether a change is committed with its definition or kept on this machine."""

    COMMITTED = "committed"
    LOCAL = "local"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StoredValue(_Closed):
    """One field value as the feed keeps it.

    Attributes:
        value: The value itself, or the preview of its canonical JSON when it was cut.
        size: The byte size of the value's canonical JSON.
        digest: The sha256 hex digest of that JSON when the value was cut, else ``None``.
    """

    value: JsonValue
    size: Annotated[int, Field(ge=0)]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @classmethod
    def of(cls, value: Any) -> StoredValue:
        """Return *value* kept verbatim, or cut to its preview when it is over the cap."""
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        raw = text.encode("utf-8")
        if len(raw) <= VALUE_BYTE_CAP:
            return cls(value=value, size=len(raw))
        return cls(
            value=text[:PREVIEW_CHARS], size=len(raw), digest=hashlib.sha256(raw).hexdigest()
        )

    @property
    def cut(self) -> bool:
        """Return whether only a preview of the value was kept."""
        return self.digest is not None


class FieldChange(_Closed):
    """One field one commit changed.

    Attributes:
        field: The field's name in the stored row.
        before: What it held before, or ``None`` when the row did not carry it.
        after: What it holds after, or ``None`` when the row no longer carries it.
    """

    field: Annotated[str, Field(min_length=1, max_length=128)]
    before: StoredValue | None
    after: StoredValue | None


class ChangeRecord(_Closed):
    """The fields one commit changed on one row.

    Attributes:
        change_id: ``<event id>:<collection>:<record key>``, unique per row per commit,
            which is what lets the replay append a journalled record only once.
        tier: Where the record is filed.
        collection: The collection the row is stored under.
        record_key: The row's key.
        event_name: The name of the event the commit emitted.
        revision_before: The row's revision before, or ``None`` when it had none.
        revision_after: The row's revision after, or ``None`` when it has none.
        canonical_sequence: The workspace-global ordinal of the commit; the feed's cursor.
        actor_ref: Who asked for the commit, or ``None`` when the commit names nobody.
        recorded_at: When the commit happened.
        changes: The changed fields, in name order.
    """

    change_id: Annotated[str, Field(min_length=1, max_length=320)]
    tier: ChangeTier
    collection: Epoch2Collection
    record_key: Annotated[str, Field(min_length=1, max_length=128)]
    event_name: Annotated[str, Field(min_length=1, max_length=120)]
    revision_before: int | None
    revision_after: int | None
    canonical_sequence: Annotated[int, Field(ge=1)]
    actor_ref: Annotated[str, Field(max_length=128)] | None
    recorded_at: UtcDatetime
    changes: Annotated[tuple[FieldChange, ...], Field(min_length=1)]


class ChangePage(_Closed):
    """One page of the feed, newest first.

    Attributes:
        changes: The records, by descending ``canonical_sequence``.
        next_cursor: The cursor of the next older page, or ``None`` on the last page.
        since: The day the producer shipped, before which nothing was recorded.
    """

    changes: tuple[ChangeRecord, ...] = ()
    next_cursor: Annotated[int, Field(ge=1)] | None = None
    since: date = CHANGE_FEED_SINCE


def change_log_path(state_path: Path, tier: ChangeTier) -> Path:
    """Return where one tier of the feed of the document at *state_path* is appended.

    Args:
        state_path: Path to the generation's ``state.json``.
        tier: The tier to locate.

    Returns:
        ``<dir>/history/change.jsonl`` for committed changes and
        ``<dir>/local/history/change.jsonl`` for machine-local ones.
    """
    base = state_path.parent if tier is ChangeTier.COMMITTED else state_path.parent / LOCAL_DIRNAME
    return base / CHANGE_DIRNAME / CHANGE_FILENAME


def change_tier(collection: Epoch2Collection, after: Mapping[str, Any]) -> ChangeTier:
    """Return the tier a change to a row of *collection* that left it as *after* belongs to."""
    if collection in STATUS_PROJECTION_COLLECTIONS:
        return ChangeTier.LOCAL
    if collection is Epoch2Collection.TASK and after.get("status") in _IN_FLIGHT_VALUES:
        return ChangeTier.LOCAL
    return ChangeTier.COMMITTED


def field_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[FieldChange, ...]:
    """Return every listed field whose value differs between two versions of a row.

    Args:
        before: The row as it was; empty for a row the commit created.
        after: The row as the commit left it.

    Returns:
        One change per differing field, in name order.
    """
    changes: list[FieldChange] = []
    for name in sorted((before.keys() | after.keys()) - UNLISTED_FIELDS):
        old, new = before.get(name, _ABSENT), after.get(name, _ABSENT)
        if old == new:
            continue
        changes.append(
            FieldChange(
                field=name,
                before=None if old is _ABSENT else StoredValue.of(old),
                after=None if new is _ABSENT else StoredValue.of(new),
            )
        )
    return tuple(changes)


def document_changes(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    event_id: str,
    event_name: str,
    canonical_sequence: int,
    actor_ref: str | None,
    recorded_at: UtcDatetime,
) -> tuple[ChangeRecord, ...]:
    """Return one record per row a commit changed between two versions of the document.

    Args:
        before: The document the commit read.
        after: The document the commit writes.
        event_id: The id of the event the commit emitted.
        event_name: Its name.
        canonical_sequence: The ordinal the commit drew.
        actor_ref: Who asked for the commit, if anyone is named.
        recorded_at: When the commit happened.

    Returns:
        The records, by collection then key.
    """
    records: list[ChangeRecord] = []
    for collection in Epoch2Collection:
        old_rows, new_rows = before.get(collection.value), after.get(collection.value)
        old_rows = old_rows if isinstance(old_rows, dict) else {}
        new_rows = new_rows if isinstance(new_rows, dict) else {}
        for key in sorted(old_rows.keys() | new_rows.keys()):
            old, new = old_rows.get(key) or {}, new_rows.get(key) or {}
            if old == new:
                continue
            changes = field_changes(old, new)
            if not changes:
                continue
            records.append(
                ChangeRecord(
                    change_id=f"{event_id}:{collection.value}:{key}",
                    tier=change_tier(collection, new),
                    collection=collection,
                    record_key=key,
                    event_name=event_name,
                    revision_before=_revision(old),
                    revision_after=_revision(new),
                    canonical_sequence=canonical_sequence,
                    actor_ref=actor_ref,
                    recorded_at=recorded_at,
                    changes=changes,
                )
            )
    return tuple(records)


def _revision(row: Mapping[str, Any]) -> int | None:
    """Return the integer revision a row carries, or ``None`` when it carries none."""
    revision = row.get("revision")
    return revision if isinstance(revision, int) and not isinstance(revision, bool) else None


def append_change_record(state_path: Path, record: ChangeRecord) -> Path:
    """Append *record* to its tier of the feed of the document at *state_path*.

    Returns:
        The file the record was appended to.

    Raises:
        StateConflict: The file's append lock stayed held.
    """
    path = change_log_path(state_path, record.tier)
    append_json_line(path, record.model_dump_json())
    return path


def append_change_record_once(state_path: Path, record: ChangeRecord) -> bool:
    """Append *record* unless its tier of the feed already holds its ``change_id``.

    Returns:
        Whether the record was appended.

    Raises:
        StateConflict: The file's append lock stayed held.
    """
    held = {item.change_id for item in _read_tier(change_log_path(state_path, record.tier))}
    if record.change_id in held:
        return False
    append_change_record(state_path, record)
    return True


def _read_tier(path: Path) -> list[ChangeRecord]:
    """Return every record one tier holds, skipping a line a crash tore.

    A torn line is the tail of an append the crash interrupted; the replay appends that
    record again from its journal, so skipping the torn bytes loses nothing.
    """
    if not path.is_file():
        return []
    records: list[ChangeRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(ChangeRecord.model_validate_json(line))
        except ValidationError:
            logger.warning(f"change feed line unreadable path={path.name}")
    return records


def read_change_page(
    state_path: Path,
    *,
    record_key: str | None,
    cursor: int | None,
    limit: int,
) -> ChangePage:
    """Return one page of the feed, newest first, across both tiers.

    Args:
        state_path: Path to the generation's ``state.json``.
        record_key: The one row whose changes are read, or ``None`` for every row.
        cursor: Only records whose ``canonical_sequence`` is below it are read; ``None``
            reads from the newest.
        limit: The most records the page holds.

    Returns:
        The page, and the cursor of the next one when older records remain. A page never
        splits one commit: the records of the commit it ends on all ride on it, so the
        next page, which reads below that commit's ordinal, misses none of them.

    Raises:
        ValueError: *limit* is below one or above :data:`MAX_PAGE`.
    """
    if not 1 <= limit <= MAX_PAGE:
        raise ValueError(f"a change page holds 1 to {MAX_PAGE} records, not {limit}")
    held = [
        record
        for tier in ChangeTier
        for record in _read_tier(change_log_path(state_path, tier))
        if (record_key is None or record.record_key == record_key)
        and (cursor is None or record.canonical_sequence < cursor)
    ]
    held.sort(key=lambda record: (record.canonical_sequence, record.change_id), reverse=True)
    if len(held) <= limit:
        return ChangePage(changes=tuple(held))
    last = held[limit - 1].canonical_sequence
    page = [record for record in held if record.canonical_sequence >= last]
    more = len(page) < len(held)
    return ChangePage(changes=tuple(page), next_cursor=last if more else None)


__all__ = [
    "CHANGE_DIRNAME",
    "CHANGE_FEED_SINCE",
    "CHANGE_FILENAME",
    "MAX_PAGE",
    "PREVIEW_CHARS",
    "UNLISTED_FIELDS",
    "VALUE_BYTE_CAP",
    "ChangePage",
    "ChangeRecord",
    "ChangeTier",
    "FieldChange",
    "StoredValue",
    "append_change_record",
    "append_change_record_once",
    "change_log_path",
    "change_tier",
    "document_changes",
    "field_changes",
    "read_change_page",
]
