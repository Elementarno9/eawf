"""The closed route an imported lifecycle row takes after the cutover.

An imported Milestone, Batch or Task is stored as a ``{status,
recorded_at, payload}`` wrapper keyed by its epoch-1 id. It does not
validate as a native record, and the native transition reducer refuses
every legacy origin, so without this module the work that was in flight
at the cut -- a claimed wave, the iter around it, the phase around that --
has no way to finish.

The route here is deliberately narrow. It admits only the edges an
in-flight epoch-1 record still needs to reach an end, each one declared in
:data:`LEGACY_EDGES`; everything else is refused as
``legacy_edge_refused``. The imported payload is never edited: a move
changes the row's status and appends one :class:`ContinuationEvent` beside
the payload, so the record still says exactly what the source said and the
continuation says, separately, what happened to it afterwards.

Completing a Task is where an epoch-1 wave close used to run its gates, so
the edge that completes one names the Task's decisive gates -- the
required, blocking gates its source wave declared -- and the caller has to
bind a passing receipt for every one of them. A Task that declares none
cannot be completed here at all: a receipt the caller merely presents
proves nothing about the tree, and there is no gate left to run.

This module decides; it does not write. The daemon runner reads the
document and the ledgers under its locks, asks these functions whether the
move stands, and commits the row it is handed back.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.migration.epoch2.criteria import (
    CRITERION_FIELD_ROUTES,
    FieldDisposition,
    ImportedCriterion,
)
from eawf.kernel.migration.epoch2.cutover import (
    COMPACTING_STATUSES,
    ROW_PAYLOAD_FIELD,
    ROW_RECORDED_AT_FIELD,
    ROW_STATUS_FIELD,
)
from eawf.kernel.migration.epoch2.lifecycle import ImportedLifecycleRecord
from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.epoch2.batch import BatchStatus
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.state.epoch2.transitions import TERMINAL_STATUSES, LifecycleEntity
from eawf.kernel.state.models import Artifact, Audit, Decision
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection

#: The row field the continuation events are appended under. It sits beside
#: the payload rather than inside it, because the payload is the source
#: record and stays byte-for-byte what the import wrote.
ROW_CONTINUATION_FIELD: Final = "continuation"

#: The payload key a compacted row's continuation travels under. A ledger
#: line has one free mapping, so the events join the payload there; every
#: key the import wrote is still present and unchanged.
LEDGER_CONTINUATION_KEY: Final = "continuation"

#: The ledger status a record appended after the cutover is filed under.
APPENDED_RECORD_STATUS: Final = "recorded"

#: The gate policy that makes a required gate decisive for a close.
_BLOCKING_POLICY: Final = "block"


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


@dataclass(frozen=True, slots=True)
class LegacyEdge:
    """One move an imported row may make, and what it needs to make it.

    Attributes:
        collection: The collection the row is stored under.
        from_status: The status the row must be in.
        to_status: The status the move lands it in.
        runs_gates: Whether the move needs a passing receipt for every
            decisive gate the row's source declared.
        children_field: The record field listing the children that must
            all be terminal first, or ``None``.
        children: The collection those children are stored under.
        proof: The ledgers one of the request's evidence refs must resolve
            in; empty when the move needs no evidence.
    """

    collection: Epoch2Collection
    from_status: str
    to_status: str
    runs_gates: bool = False
    children_field: str | None = None
    children: Epoch2Collection | None = None
    proof: frozenset[Epoch2Collection] = frozenset()


#: Every move an imported row may make. The table is closed: an edge that is
#: not here is refused, whatever the native machine would allow.
LEGACY_EDGES: Final[tuple[LegacyEdge, ...]] = (
    LegacyEdge(Epoch2Collection.TASK, TaskStatus.PLANNED.value, TaskStatus.CLAIMED.value),
    LegacyEdge(Epoch2Collection.TASK, TaskStatus.CLAIMED.value, TaskStatus.RUNNING.value),
    LegacyEdge(
        Epoch2Collection.TASK, TaskStatus.RUNNING.value, TaskStatus.COMPLETED.value, runs_gates=True
    ),
    LegacyEdge(
        Epoch2Collection.TASK, TaskStatus.CLAIMED.value, TaskStatus.COMPLETED.value, runs_gates=True
    ),
    LegacyEdge(Epoch2Collection.TASK, TaskStatus.DRAFT.value, TaskStatus.DROPPED.value),
    LegacyEdge(
        Epoch2Collection.BATCH,
        BatchStatus.ACTIVE.value,
        BatchStatus.COMPLETED.value,
        children_field="task_refs",
        children=Epoch2Collection.TASK,
    ),
    LegacyEdge(
        Epoch2Collection.MILESTONE,
        MilestoneStatus.ACTIVE.value,
        MilestoneStatus.COMPLETED.value,
        children_field="required_batch_refs",
        children=Epoch2Collection.BATCH,
        proof=frozenset({Epoch2Collection.AUDIT}),
    ),
    LegacyEdge(
        Epoch2Collection.MILESTONE,
        MilestoneStatus.PLANNED.value,
        MilestoneStatus.CANCELLED.value,
        proof=frozenset({Epoch2Collection.DECISION, Epoch2Collection.ARTIFACT}),
    ),
)

#: Which lifecycle machine's terminal set judges each collection's children.
_CHILD_ENTITIES: Final[Mapping[Epoch2Collection, LifecycleEntity]] = {
    Epoch2Collection.TASK: LifecycleEntity.TASK,
    Epoch2Collection.BATCH: LifecycleEntity.DELIVERY_BATCH,
}


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


@dataclass(frozen=True, slots=True)
class DecisiveGate:
    """One gate the Task's source wave declared required and blocking.

    Attributes:
        gate: The epoch-1 gate, as the wave recorded it.
        criterion: The criterion it scores, rebuilt from the import.
    """

    gate: GateSpec
    criterion: CriterionSpec


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


def edge_for(collection: Epoch2Collection, from_status: str, to_status: str) -> LegacyEdge:
    """Return the declared edge from *from_status* to *to_status*.

    Raises:
        LegacyContinuationRefusedError: The table declares no such edge.
    """
    for edge in LEGACY_EDGES:
        if (edge.collection, edge.from_status, edge.to_status) == (
            collection,
            from_status,
            to_status,
        ):
            return edge
    admitted = ", ".join(
        f"{edge.from_status}->{edge.to_status}"
        for edge in LEGACY_EDGES
        if edge.collection is collection
    )
    raise LegacyContinuationRefusedError(
        f"an imported {collection.value} does not move {from_status} -> {to_status}; "
        f"admitted: {admitted or 'none'}",
        remediation="Request one of the admitted legacy edges.",
    )


def child_refs(edge: LegacyEdge, row: LegacyRow) -> tuple[str, ...]:
    """Return the children the edge requires terminal, as the source listed them.

    Raises:
        LegacyContinuationRefusedError: The record lists them as something
            other than a list of ids.
    """
    if edge.children_field is None:
        return ()
    refs = row.record.record.get(edge.children_field, [])
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        raise LegacyContinuationRefusedError(
            f"{entity_ref(row.collection, row.key)} lists {edge.children_field} as something "
            "other than ids",
            code=ContinuationRefusal.SCHEMA_VALIDATION_FAILED,
            remediation="Repair the imported record before closing it.",
        )
    return tuple(refs)


def require_children_terminal(
    edge: LegacyEdge, row: LegacyRow, statuses: Mapping[str, str | None]
) -> None:
    """Refuse the move while any child is still open.

    Args:
        edge: The edge being taken.
        row: The row being moved.
        statuses: Each child's current status, ``None`` when neither the
            document nor the ledger holds it.

    Raises:
        LegacyContinuationRefusedError: A child is absent or not terminal.
    """
    if edge.children is None:
        return
    terminal = TERMINAL_STATUSES[_CHILD_ENTITIES[edge.children]]
    open_children = sorted(
        f"{ref}={statuses.get(ref) or 'absent'}"
        for ref in statuses
        if statuses.get(ref) not in terminal
    )
    if open_children:
        raise LegacyContinuationRefusedError(
            f"{entity_ref(row.collection, row.key)} cannot reach {edge.to_status} while "
            f"{edge.children.value} children are open: {', '.join(open_children)}",
            code=ContinuationRefusal.TRANSITION_GUARD_FAILED,
            guard=f"{edge.children.value}_terminal",
            remediation=f"Finish or drop every {edge.children.value} first.",
        )


def require_proof(edge: LegacyEdge, row: LegacyRow, resolved: frozenset[Epoch2Collection]) -> None:
    """Refuse the move unless an evidence ref resolves in a ledger the edge names.

    Args:
        edge: The edge being taken.
        row: The row being moved.
        resolved: The ledgers at least one of the request's evidence refs
            resolved in.

    Raises:
        LegacyContinuationRefusedError: The edge needs evidence and none of
            the cited refs is a record of the kind it needs.
    """
    if not edge.proof or edge.proof & resolved:
        return
    wanted = " or ".join(sorted(collection.value for collection in edge.proof))
    raise LegacyContinuationRefusedError(
        f"{entity_ref(row.collection, row.key)} reaches {edge.to_status} only against a "
        f"recorded {wanted}, and no evidence ref resolves to one",
        code=ContinuationRefusal.TRANSITION_GUARD_FAILED,
        guard=f"{wanted.replace(' or ', '_or_')}_ref",
        remediation=f"Append the {wanted} record, then cite its id as an evidence ref.",
    )


def _criterion_spec(criterion: ImportedCriterion) -> CriterionSpec:
    """Rebuild the epoch-1 criterion the import converted, field for field.

    The criteria disposition routes every epoch-1 field either to a native
    field or to ``legacy_refs``, and the routes are checked total, so the
    inverse is exact.

    Raises:
        ValidationError: The rebuilt row does not validate.
    """
    fields: dict[str, Any] = {
        "id": criterion.id,
        "text": criterion.text,
        "gate_ids": list(criterion.gate_ids),
        "waiver_reason": criterion.waiver_reason,
    }
    for name, route in CRITERION_FIELD_ROUTES.items():
        if route.disposition is FieldDisposition.LEGACY_REF:
            fields[name] = criterion.legacy_refs.get(route.target_key)
    return CriterionSpec.model_validate(fields)


def decisive_gates(row: LegacyRow) -> tuple[DecisiveGate, ...]:
    """Return the gates a Task's source wave declared required and blocking.

    Args:
        row: The Task being completed.

    Returns:
        One entry per decisive gate, in the order the wave declared them.

    Raises:
        LegacyContinuationRefusedError: A gate row, or the criterion a
            decisive gate scores, does not validate or is missing -- a
            decisive gate nobody can run is a close nobody can prove.
    """
    raw_gates = row.record.legacy_refs.get("gates") or []
    criteria = {criterion.id: criterion for criterion in row.record.criteria}
    gates: list[DecisiveGate] = []
    for raw in raw_gates:
        try:
            gate = GateSpec.model_validate(raw)
        except ValidationError as error:
            raise LegacyContinuationRefusedError(
                f"{entity_ref(row.collection, row.key)} carries a gate row that does not validate",
                code=ContinuationRefusal.TRANSITION_GUARD_FAILED,
                guard="decisive_gate_unreadable",
                remediation="Repair the imported gate before completing the Task.",
            ) from error
        if not gate.required or gate.policy != _BLOCKING_POLICY:
            continue
        criterion = criteria.get(gate.criterion_id)
        try:
            spec = None if criterion is None else _criterion_spec(criterion)
        except ValidationError:
            spec = None
        if spec is None:
            raise LegacyContinuationRefusedError(
                f"decisive gate {gate.id} scores criterion {gate.criterion_id}, which "
                f"{entity_ref(row.collection, row.key)} does not carry in a readable form",
                code=ContinuationRefusal.TRANSITION_GUARD_FAILED,
                guard=gate.id,
                remediation="Repair the imported criterion before completing the Task.",
            )
        gates.append(DecisiveGate(gate=gate, criterion=spec))
    if not gates:
        raise LegacyContinuationRefusedError(
            f"{entity_ref(row.collection, row.key)} declares no decisive gate, so completing "
            "it would rest on caller-supplied receipts alone",
            code=ContinuationRefusal.TRANSITION_GUARD_FAILED,
            guard="decisive_gates_absent",
            remediation="Complete the Task through a scope that declares a gate the daemon "
            "can run; a presented receipt is never accepted in place of one.",
        )
    return tuple(gates)


def require_gates_passed(row: LegacyRow, bindings: tuple[GateReceiptBinding, ...]) -> None:
    """Refuse the completion unless every decisive gate's receipt passed.

    Raises:
        LegacyContinuationRefusedError: A gate did not pass. The first one
            is named as the guard and every one is listed in the detail.
    """
    failed = [binding for binding in bindings if binding.status != "pass"]
    if not failed:
        return
    listed = ", ".join(f"{item.gate_id}={item.status} ({item.receipt_id})" for item in failed)
    raise LegacyContinuationRefusedError(
        f"{entity_ref(row.collection, row.key)} stays {row.status}: decisive gate "
        f"{failed[0].gate_id} did not pass; {listed}",
        code=ContinuationRefusal.TRANSITION_GUARD_FAILED,
        guard=failed[0].gate_id,
        remediation="Fix what the gate reports and complete the Task again.",
    )


def receipt_digest(payload: Mapping[str, Any]) -> str:
    """Return the digest a binding names one receipt line's payload by."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def advanced_row(row: LegacyRow, event: ContinuationEvent) -> dict[str, Any]:
    """Return the stored row after one move: new status, same payload, one more event."""
    return {
        **row.raw,
        ROW_STATUS_FIELD: event.to_status,
        ROW_RECORDED_AT_FIELD: event.at.isoformat(),
        ROW_CONTINUATION_FIELD: [
            *(item.model_dump(mode="json") for item in row.continuation),
            event.model_dump(mode="json"),
        ],
    }


def compaction_line(
    row: LegacyRow, advanced: Mapping[str, Any], *, at: datetime
) -> LedgerRecord | None:
    """Return the ledger line a row moved to a compacting status becomes, else ``None``.

    Args:
        row: The row before the move.
        advanced: The row after it.
        at: When the move committed.

    Returns:
        The line, whose payload is the imported payload plus the row's
        continuation, or ``None`` when the new status stays in the
        document -- a dropped Task never entered delivery, so the tier
        rules keep it there.
    """
    status = advanced[ROW_STATUS_FIELD]
    if status not in COMPACTING_STATUSES.get(row.collection, frozenset()):
        return None
    return LedgerRecord(
        collection=row.collection,
        record_key=row.key,
        status=status,
        recorded_at=at,
        payload={
            **advanced[ROW_PAYLOAD_FIELD],
            LEDGER_CONTINUATION_KEY: advanced[ROW_CONTINUATION_FIELD],
        },
    )


class RecordKind(StrEnum):
    """The ledger records an operator may append after the cutover."""

    AUDIT = "audit"
    DECISION = "decision"
    ARTIFACT = "artifact"


#: The epoch-1 model each record kind validates through, and the ledger it
#: lands in. The epoch-1 models forbid unknown fields, so a record that
#: would not have been accepted before the cut is not accepted after it.
RECORD_MODELS: Final[Mapping[RecordKind, type[Audit | Decision | Artifact]]] = {
    RecordKind.AUDIT: Audit,
    RecordKind.DECISION: Decision,
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
    """Return the ledger line one appended audit, decision or artifact becomes.

    The payload keeps the record under ``payload``, the key the import used
    for the same kinds, so one reader serves imported and appended lines.

    Args:
        kind: Which record it is.
        record: The record as the caller supplied it.
        at: When it was appended.

    Returns:
        The line, keyed by the record's own id.

    Raises:
        ValidationError: The record does not validate through its epoch-1
            model.
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
    "LEDGER_CONTINUATION_KEY",
    "LEGACY_EDGES",
    "RECORD_COLLECTIONS",
    "RECORD_MODELS",
    "ROW_CONTINUATION_FIELD",
    "ContinuationEvent",
    "ContinuationRefusal",
    "DecisiveGate",
    "GateReceiptBinding",
    "LegacyContinuationRefusedError",
    "LegacyEdge",
    "LegacyRow",
    "RecordKind",
    "advanced_row",
    "appended_record_line",
    "child_refs",
    "compaction_line",
    "decisive_gates",
    "edge_for",
    "entity_ref",
    "ledger_ids",
    "read_legacy_row",
    "receipt_digest",
    "require_children_terminal",
    "require_gates_passed",
    "require_proof",
]
