"""How an imported lifecycle row reads after the cutover, and what may be appended.

An imported Milestone, Batch or Task is stored as a ``{status,
recorded_at, payload}`` wrapper keyed by its epoch-1 id. It does not
validate as a native record, so :func:`read_legacy_row` reads it through
the import's own model, together with the :class:`ContinuationEvent` rows
earlier releases appended beside the payload when they moved it.

An operator still files audits and artifacts against that imported work:
:func:`appended_record_line` builds the ledger line one becomes, and
:func:`ledger_ids` names every id a line answers to.

This module decides; it does not write. The daemon runner reads the
ledgers under its locks and commits the lines it is handed back.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.migration.epoch2.cutover import (
    ROW_PAYLOAD_FIELD,
    ROW_STATUS_FIELD,
)
from eawf.kernel.migration.epoch2.lifecycle import ImportedLifecycleRecord
from eawf.kernel.state.models import Artifact, Audit
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection

#: The row field the continuation events are appended under. It sits beside
#: the payload rather than inside it, because the payload is the source
#: record and stays byte-for-byte what the import wrote.
ROW_CONTINUATION_FIELD: Final = "continuation"

#: The ledger status a record appended after the cutover is filed under.
APPENDED_RECORD_STATUS: Final = "recorded"


class ContinuationRefusal(StrEnum):
    """The stable codes a continuation is refused with.

    Every value is also a code of the daemon's closed domain vocabulary, so
    the runner reports a refusal without translating it.
    """

    LEGACY_EDGE_REFUSED = "legacy_edge_refused"
    IDENTITY_NOT_FOUND = "identity_not_found"
    TRANSITION_GUARD_FAILED = "transition_guard_failed"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"


class LegacyContinuationRefusedError(ValueError):
    """One continuation move was refused before anything was written.

    Attributes:
        code: The stable code a client branches on.
        detail: The operator-facing explanation.
        guard: The gate or rule that did not hold, when one was reached.
        remediation: One sentence saying what to do about it.
    """

    def __init__(
        self,
        detail: str,
        *,
        code: ContinuationRefusal = ContinuationRefusal.LEGACY_EDGE_REFUSED,
        guard: str | None = None,
        remediation: str,
    ) -> None:
        """Build the refusal; the message leads with the code."""
        super().__init__(f"{code.value}: {detail}")
        self.code = code
        self.detail = detail
        self.guard = guard
        self.remediation = remediation


class GateReceiptBinding(BaseModel):
    """One decisive gate's receipt, as a continuation event binds it.

    Attributes:
        gate_id: The gate that ran.
        criterion_id: The criterion it scores.
        receipt_id: The receipt line's key in the generation's receipt ledger.
        receipt_digest: The digest of that line's payload, so the binding
            names exactly the observation it was taken against.
        status: The gate's outcome word.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: Annotated[str, Field(min_length=1, max_length=64)]
    criterion_id: Annotated[str, Field(min_length=1, max_length=64)]
    receipt_id: Annotated[str, Field(min_length=1, max_length=128)]
    receipt_digest: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    status: Annotated[str, Field(min_length=1, max_length=32)]


class ContinuationEvent(BaseModel):
    """One move an imported row made after the cutover.

    Attributes:
        from_status: Where the row was.
        to_status: Where the move left it.
        at: When the move committed.
        actor: Who asked for it.
        reason: Why, in the operator's words.
        evidence_refs: What the caller cited.
        gate_receipts: The receipts of the gates the move ran itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_status: Annotated[str, Field(min_length=1, max_length=32)]
    to_status: Annotated[str, Field(min_length=1, max_length=32)]
    at: UtcDatetime
    actor: Annotated[str, Field(min_length=1, max_length=32)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    evidence_refs: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = ()
    gate_receipts: tuple[GateReceiptBinding, ...] = ()


@dataclass(frozen=True, slots=True)
class LegacyRow:
    """One imported row as the document holds it, read and validated.

    Attributes:
        collection: The collection it is stored under.
        key: Its epoch-1 id.
        status: Its current status.
        raw: The row exactly as stored, which the move copies forward.
        record: The imported payload, validated.
        continuation: The moves it already made.
    """

    collection: Epoch2Collection
    key: str
    status: str
    raw: Mapping[str, Any]
    record: ImportedLifecycleRecord
    continuation: tuple[ContinuationEvent, ...]


def entity_ref(collection: Epoch2Collection, key: str) -> str:
    """Return the name a refusal and an event give one imported row."""
    return f"legacy:{collection.value}/{key}"


def read_legacy_row(collection: Epoch2Collection, key: str, row: Any) -> LegacyRow:
    """Return one stored row validated as an imported lifecycle record.

    Args:
        collection: The collection the row is stored under.
        key: The row's key.
        row: The stored row.

    Returns:
        The validated row.

    Raises:
        LegacyContinuationRefusedError: The row is not the wrapper the
            import writes, its payload does not validate, or it is not a
            legacy record of this collection -- a native record moves only
            through the native verbs.
    """
    if not isinstance(row, dict) or not isinstance(row.get(ROW_STATUS_FIELD), str):
        raise LegacyContinuationRefusedError(
            f"{entity_ref(collection, key)} is not an imported row with a readable status",
            code=ContinuationRefusal.SCHEMA_VALIDATION_FAILED,
            remediation="Address a row the epoch-2 cutover imported.",
        )
    try:
        record = ImportedLifecycleRecord.model_validate(row.get(ROW_PAYLOAD_FIELD))
        continuation = tuple(
            ContinuationEvent.model_validate(item) for item in row.get(ROW_CONTINUATION_FIELD, ())
        )
    except ValidationError as error:
        raise LegacyContinuationRefusedError(
            f"{entity_ref(collection, key)} does not validate as an imported lifecycle record",
            code=ContinuationRefusal.SCHEMA_VALIDATION_FAILED,
            remediation="Address a row the epoch-2 cutover imported; a native record moves "
            "through the native lifecycle verbs.",
        ) from error
    if record.origin.kind != "legacy" or record.target.value != collection.value:
        raise LegacyContinuationRefusedError(
            f"{entity_ref(collection, key)} is not a legacy {collection.value} record",
            remediation="Move a native record through the native lifecycle verbs.",
        )
    return LegacyRow(
        collection=collection,
        key=key,
        status=row[ROW_STATUS_FIELD],
        raw=row,
        record=record,
        continuation=continuation,
    )


class RecordKind(StrEnum):
    """The ledger records an operator may append after the cutover."""

    AUDIT = "audit"
    DECISION = "decision"
    ARTIFACT = "artifact"


#: The epoch-1 model each record kind validates through, and the ledger it
#: lands in. The epoch-1 models forbid unknown fields, so a record that
#: would not have been accepted before the cut is not accepted after it. A
#: decision has no epoch-1 model here: it is filed as a native decision,
#: through :mod:`eawf.kernel.migration.epoch2.decision_record`.
RECORD_MODELS: Final[Mapping[RecordKind, type[Audit | Artifact]]] = {
    RecordKind.AUDIT: Audit,
    RecordKind.ARTIFACT: Artifact,
}
RECORD_COLLECTIONS: Final[Mapping[RecordKind, Epoch2Collection]] = {
    RecordKind.AUDIT: Epoch2Collection.AUDIT,
    RecordKind.DECISION: Epoch2Collection.DECISION,
    RecordKind.ARTIFACT: Epoch2Collection.ARTIFACT,
}


def appended_record_line(
    kind: RecordKind, record: Mapping[str, Any], *, at: datetime
) -> LedgerRecord:
    """Return the ledger line one appended audit or artifact becomes.

    The payload keeps the record under ``payload``, the key the import used
    for the same kinds, so one reader serves imported and appended lines.

    Args:
        kind: Which record it is; an audit or an artifact, since a decision
            is filed through its own lifecycle.
        record: The record as the caller supplied it.
        at: When it was appended.

    Returns:
        The line, keyed by the record's own id.

    Raises:
        ValidationError: The record does not validate through its epoch-1
            model.
        KeyError: *kind* is a decision.
    """
    validated = RECORD_MODELS[kind].model_validate(record)
    return LedgerRecord(
        collection=RECORD_COLLECTIONS[kind],
        record_key=validated.id,
        status=APPENDED_RECORD_STATUS,
        recorded_at=at,
        payload={ROW_PAYLOAD_FIELD: validated.model_dump(mode="json")},
    )


def ledger_ids(record: LedgerRecord) -> frozenset[str]:
    """Return every id a ledger line answers to: its key and its record's own id.

    An imported audit is keyed ``legacy:audits/A001`` while an operator
    cites it as ``A001``; both spellings name the same line.
    """
    inner = record.payload.get(ROW_PAYLOAD_FIELD)
    inner_id = inner.get("id") if isinstance(inner, dict) else None
    return frozenset({record.record_key, *([inner_id] if isinstance(inner_id, str) else [])})


__all__ = [
    "RECORD_COLLECTIONS",
    "RECORD_MODELS",
    "ROW_CONTINUATION_FIELD",
    "ContinuationEvent",
    "ContinuationRefusal",
    "GateReceiptBinding",
    "LegacyContinuationRefusedError",
    "LegacyRow",
    "RecordKind",
    "appended_record_line",
    "entity_ref",
    "ledger_ids",
    "read_legacy_row",
]
