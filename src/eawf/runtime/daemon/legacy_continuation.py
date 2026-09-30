"""The daemon side of continuing imported records after the epoch-2 cutover.

:mod:`eawf.kernel.migration.epoch2.continuation` decides whether an
imported row may move; this module reads what that decision needs under
the root's locks, runs the gates a completing Task owes, and commits the
result through :mod:`eawf.runtime.daemon.epoch2_transaction`, which stays
the only writer of a document row or a ledger line.

A Task completion is taken in two locked passes with the gates run between
them. A gate is routinely a whole test suite, and holding the root's locks
for its length would stall every other writer on the tree. The second
pass re-reads the row and refuses when it moved in the meantime, so the
receipts bound to the completion are receipts taken against the row that
completes.

The gates run through the same out-of-process runner a wave close uses,
from the repository the tree belongs to, and their claims are kept under a
directory of this attempt's own below ``local/``. Every receipt is filed in
the generation's receipt ledger, passing or not, before the completion is
decided: a red gate leaves the Task where it was with the evidence of why
already on record. Nothing is written to the epoch-1 document or its
stores, which the cutover fenced.

Every Milestone, Batch and Task imported from epoch 1 belongs to the one
project the tree imported, so the locks and events of a continuation are
scoped to that project's URN rather than to a record URN an epoch-1 id
cannot spell.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.identity import EntityKind, format_qualified_urn
from eawf.kernel.migration.epoch2.continuation import (
    ContinuationEvent,
    ContinuationRefusal,
    DecisiveGate,
    GateReceiptBinding,
    LegacyContinuationRefusedError,
    LegacyRow,
    RecordKind,
    advanced_row,
    appended_record_line,
    child_refs,
    compaction_line,
    decisive_gates,
    edge_for,
    entity_ref,
    ledger_ids,
    read_legacy_row,
    receipt_digest,
    require_children_terminal,
    require_gates_passed,
    require_proof,
)
from eawf.kernel.migration.epoch2.decision_record import decision_lines
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.kernel.state.epoch2.decision import DecisionError
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import LedgerRecord, effective_records, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
    commit_ledger_append,
    commit_row_write,
)
from eawf.runtime.daemon.gate_execution import (
    GateChildCrashError,
    GateExecutionContext,
    gate_receipt_id,
    run_gate_out_of_process,
)
from eawf.workflow.verify.compile import compile_gate

logger = logging.getLogger(__name__)


#: The namespace of the event a continued row's move emits. A legacy move
#: is not a native lifecycle edge, so it stays out of the closed ``domain``
#: event vocabulary.
LEGACY_EVENT_NAMESPACE: Final = "legacy"

#: Where a continuation's gate claims live while its gates run, below the
#: tree's machine-local directory.
_GATE_CLAIMS_DIRNAME: Final = "legacy-gates"

#: The ledgers an evidence ref is resolved against.
_PROOF_COLLECTIONS: Final[tuple[Epoch2Collection, ...]] = (
    Epoch2Collection.AUDIT,
    Epoch2Collection.DECISION,
    Epoch2Collection.ARTIFACT,
)


class LegacyAdvanceRequest(BaseModel):
    """The strict parameters of one imported-row move.

    Attributes:
        key: The row's epoch-1 id.
        collection: The collection it is stored under.
        to: The status to move it to.
        reason: Why, in the operator's words.
        evidence_refs: Records the move cites; a Milestone close must cite
            an audit, a Milestone cancel a decision or artifact.
        actor: Who asked.
    """

    model_config = ConfigDict(extra="forbid")

    key: Annotated[str, Field(min_length=1, max_length=64)]
    collection: Literal["task", "batch", "milestone"]
    to: Annotated[str, Field(min_length=1, max_length=32)]
    reason: Annotated[str, Field(min_length=1, max_length=500)]
    evidence_refs: tuple[Annotated[str, Field(min_length=1, max_length=200)], ...] = ()
    actor: PrincipalKey


class LegacyAdvanceReceipt(BaseModel):
    """What one committed imported-row move left behind.

    Attributes:
        entity_ref: The row that moved.
        from_status: Where it was.
        to_status: Where it is.
        event_id: The firehose row the move appended.
        gate_receipts: The receipts of the gates the move ran itself.
        compacted: Whether the row left the document for its ledger.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_ref: str
    from_status: str
    to_status: str
    event_id: str
    gate_receipts: tuple[GateReceiptBinding, ...] = ()
    compacted: bool


class RecordAppendRequest(BaseModel):
    """The strict parameters of one audit, decision or artifact append.

    Attributes:
        kind: Which record it is.
        record: The record, validated when the line is built: an audit or
            artifact through its epoch-1 model, a decision as a native
            decision with its evidence and supersession chain.
    """

    model_config = ConfigDict(extra="forbid")

    kind: RecordKind
    record: dict[str, Any]


class RecordAppendReceipt(BaseModel):
    """What one committed record append left behind.

    Attributes:
        collection: The ledger the line landed in.
        record_key: The line's key, which is the record's own id.
        event_id: The firehose row the append wrote.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    collection: Epoch2Collection
    record_key: str
    event_id: str


@dataclass(frozen=True, slots=True)
class CommittedContinuation[ReceiptT]:
    """A committed answer beside the firehose rows to publish after the locks.

    Attributes:
        receipt: What the caller answers with.
        envelopes: Every firehose row the commit appended, in order.
    """

    receipt: ReceiptT
    envelopes: tuple[Envelope, ...]


def _refused(error: LegacyContinuationRefusedError, *, subject: str) -> TransactionRefusedError:
    """Return the wire refusal one continuation refusal stands for."""
    return TransactionRefusedError(
        code=TransactionRefusalCode(error.code.value),
        detail=error.detail,
        entity_ref=subject,
        guard=error.guard,
        remediation=error.remediation,
    )


def _project_urn(context: Epoch2RootContext) -> str:
    """Return the URN of the one project the tree imported.

    Raises:
        TransactionRefusedError: The selected document does not hold
            exactly one project row, so no scope can be named.
    """
    authority = context.require_selected_generation()
    assert authority.target is not None and authority.generation_id is not None
    document = read_document(
        authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    )
    projects = sorted(document_rows(document, Epoch2Collection.PROJECT))
    if len(projects) != 1:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=f"the selected document holds {len(projects)} project rows, not one",
            entity_ref=context.identity.tree_root.name,
            remediation="Continue imported records only on a tree that imported one project.",
        )
    code = projects[0]
    return format_qualified_urn(
        workspace_key=code,
        project_key=code,
        repository_key=None,
        kind=EntityKind.PROJECT,
        entity_key=code,
    )


def _standing(session: RootSession, collection: Epoch2Collection) -> tuple[LedgerRecord, ...]:
    """Return the lines of one ledger that still stand."""
    return effective_records(read_ledger_records(session.ledger_path(collection)))


def _status_of(
    session: RootSession, document: dict[str, Any], collection: Epoch2Collection, key: str
) -> str | None:
    """Return one record's status from the document, else from its ledger."""
    row = document_rows(document, collection).get(key)
    if isinstance(row, dict):
        status = row.get("status")
        return status if isinstance(status, str) else None
    for line in reversed(_standing(session, collection)):
        if line.record_key == key:
            return line.status
    return None


def _locate(
    session: RootSession, document: dict[str, Any], collection: Epoch2Collection, key: str
) -> LegacyRow:
    """Return the imported row *key* names, read from the locked document.

    Raises:
        LegacyContinuationRefusedError: The row has already left the
            document for its ledger, which only a terminal row does, or
            neither tier holds it.
    """
    row = document_rows(document, collection).get(key)
    if row is not None:
        return read_legacy_row(collection, key, row)
    status = _status_of(session, document, collection, key)
    if status is not None:
        raise LegacyContinuationRefusedError(
            f"{entity_ref(collection, key)} is {status} and already in its ledger, so it has "
            "no edge left",
            remediation="Nothing to do: the record reached its end.",
        )
    raise LegacyContinuationRefusedError(
        f"neither the document nor the ledger holds {entity_ref(collection, key)}",
        code=ContinuationRefusal.IDENTITY_NOT_FOUND,
        remediation="Name the epoch-1 id of an imported record.",
    )


def _resolved_proof(session: RootSession, refs: tuple[str, ...]) -> frozenset[Epoch2Collection]:
    """Return the ledgers at least one of *refs* names a standing record in."""
    if not refs:
        return frozenset()
    wanted = set(refs)
    return frozenset(
        collection
        for collection in _PROOF_COLLECTIONS
        if any(ledger_ids(line) & wanted for line in _standing(session, collection))
    )


def _receipt_for(
    gate: DecisiveGate,
    *,
    row: LegacyRow,
    context: Epoch2RootContext,
    gate_context: GateExecutionContext,
    now: datetime,
) -> tuple[GateReceiptBinding, LedgerRecord]:
    """Run one decisive gate and return its binding and its receipt line.

    Raises:
        LegacyContinuationRefusedError: The gate cannot be compiled for the
            deterministic runner, or its run produced no receipt to bind.
    """
    subject = entity_ref(row.collection, row.key)
    compiled = compile_gate(gate.gate, criterion=gate.criterion)
    if compiled is None:
        raise LegacyContinuationRefusedError(
            f"decisive gate {gate.gate.id} of {subject} is not one the deterministic gate "
            "runner can execute",
            code=ContinuationRefusal.TRANSITION_GUARD_FAILED,
            guard=gate.gate.id,
            remediation="Complete the Task only once every decisive gate is runnable.",
        )
    try:
        result = run_gate_out_of_process(
            compiled,
            cwd=context.identity.tree_root.parent,
            context=gate_context,
            criterion_id=gate.criterion.id,
            gate_id=gate.gate.id,
        )
    except (GateChildCrashError, ValueError) as error:
        logger.warning(
            f"_receipt_for crashed gate={gate.gate.id} subject={subject} detail={error!s}"
        )
        raise LegacyContinuationRefusedError(
            f"decisive gate {gate.gate.id} of {subject} crashed before it produced a receipt",
            code=ContinuationRefusal.TRANSITION_GUARD_FAILED,
            guard=gate.gate.id,
            remediation="Fix what stops the gate from running and complete the Task again.",
        ) from error
    if result.freshness_key is None:
        raise LegacyContinuationRefusedError(
            f"decisive gate {gate.gate.id} of {subject} ran without a freshness key, so it "
            "has no receipt to bind",
            code=ContinuationRefusal.TRANSITION_GUARD_FAILED,
            guard=gate.gate.id,
            remediation="Complete the Task again; the gate runner owes every run a key.",
        )
    status = result.status or ("pass" if result.passed else "fail")
    observed = result.model_dump(mode="json")
    payload = {
        "subject": subject,
        "gate_id": gate.gate.id,
        "criterion_id": gate.criterion.id,
        "freshness_key": result.freshness_key,
        "status": status,
        **{
            name: observed.get(name)
            for name in (
                "passed",
                "exit_status",
                "argv",
                "stdout_digest",
                "stderr_digest",
                "started_at",
                "ended_at",
                "duration_ms",
            )
        },
    }
    receipt_id = gate_receipt_id(result.freshness_key)
    line = LedgerRecord(
        collection=Epoch2Collection.RECEIPT,
        record_key=receipt_id,
        status=status,
        recorded_at=now,
        payload=payload,
    )
    binding = GateReceiptBinding(
        gate_id=gate.gate.id,
        criterion_id=gate.criterion.id,
        receipt_id=receipt_id,
        receipt_digest=receipt_digest(payload),
        status=status,
    )
    logger.info(f"_receipt_for subject={subject} gate={gate.gate.id} status={status}")
    return binding, line


def _run_gates(
    context: Epoch2RootContext, *, row: LegacyRow, gates: tuple[DecisiveGate, ...], now: datetime
) -> tuple[tuple[GateReceiptBinding, ...], tuple[LedgerRecord, ...]]:
    """Run every decisive gate of *row* from the tree's repository.

    Returns:
        The bindings and receipt lines, in gate order.

    Raises:
        LegacyContinuationRefusedError: A gate could not produce a receipt.
    """
    attempt_id = uuid.uuid4().hex
    claims = context.identity.tree_root / "local" / _GATE_CLAIMS_DIRNAME / attempt_id
    gate_context = GateExecutionContext(state_path=claims / "state.json", attempt_id=attempt_id)
    try:
        ran = [
            _receipt_for(gate, row=row, context=context, gate_context=gate_context, now=now)
            for gate in gates
        ]
    finally:
        shutil.rmtree(claims, ignore_errors=True)
    return tuple(binding for binding, _ in ran), tuple(line for _, line in ran)


def _commit_move(
    session: RootSession,
    *,
    row: LegacyRow,
    request: LegacyAdvanceRequest,
    bindings: tuple[GateReceiptBinding, ...],
    now: datetime,
) -> tuple[LegacyAdvanceReceipt, Envelope]:
    """Write the row's move and compact it when the move made it history."""
    event = ContinuationEvent(
        from_status=row.status,
        to_status=request.to,
        at=now,
        actor=request.actor,
        reason=request.reason,
        evidence_refs=request.evidence_refs,
        gate_receipts=bindings,
    )
    advanced = advanced_row(row, event)
    compaction = compaction_line(row, advanced, at=now)
    envelope = commit_row_write(
        session,
        collection=row.collection,
        record_key=row.key,
        row=advanced,
        event_name=f"{LEGACY_EVENT_NAMESPACE}.{row.collection.value}.advanced",
        event_fields={
            "entity_ref": entity_ref(row.collection, row.key),
            "from_status": row.status,
            "to_status": request.to,
            "actor_ref": request.actor,
            "evidence_refs": list(request.evidence_refs),
            "gate_receipts": [binding.model_dump(mode="json") for binding in bindings],
        },
        compaction=compaction,
        now=now,
    )
    receipt = LegacyAdvanceReceipt(
        entity_ref=entity_ref(row.collection, row.key),
        from_status=row.status,
        to_status=request.to,
        event_id=envelope.id,
        gate_receipts=bindings,
        compacted=compaction is not None,
    )
    return receipt, envelope


def advance_legacy(
    context: Epoch2RootContext, request: LegacyAdvanceRequest, *, now: datetime
) -> CommittedContinuation[LegacyAdvanceReceipt]:
    """Move one imported row along a declared legacy edge, or refuse.

    Args:
        context: The native context of the tree.
        request: The already-validated request.
        now: When the move happens.

    Returns:
        The receipt of the committed move beside every firehose row it
        wrote, receipts included.

    Raises:
        TransactionRefusedError: The edge is not declared, a child is still
            open, the cited evidence does not resolve, a decisive gate did
            not pass or could not run, or the row moved while its gates
            ran. A refusal after the gates ran leaves their receipts filed
            and the row where it was.
        NativeAuthorityRequiredError: The tree left epoch 2.
        LockTimeout: A lock stayed held past the lock timeout.
    """
    collection = Epoch2Collection(request.collection)
    subject = entity_ref(collection, request.key)
    lock = _project_urn(context)
    try:
        with context.session([lock]) as session:
            document = session.read_document()
            row = _locate(session, document, collection, request.key)
            edge = edge_for(collection, row.status, request.to)
            refs = child_refs(edge, row)
            if edge.children is not None:
                statuses = {ref: _status_of(session, document, edge.children, ref) for ref in refs}
                require_children_terminal(edge, row, statuses)
            if edge.proof:
                require_proof(edge, row, _resolved_proof(session, request.evidence_refs))
            if not edge.runs_gates:
                receipt, envelope = _commit_move(
                    session, row=row, request=request, bindings=(), now=now
                )
                return CommittedContinuation(receipt=receipt, envelopes=(envelope,))
            gates = decisive_gates(row)
        bindings, lines = _run_gates(context, row=row, gates=gates, now=now)
        with context.session([lock]) as session:
            current = _locate(session, session.read_document(), collection, request.key)
            if current.raw != row.raw:
                raise LegacyContinuationRefusedError(
                    f"{subject} moved to {current.status} while its gates ran",
                    remediation="Re-read the record and request the edge from where it is now.",
                )
            filed = tuple(commit_ledger_append(session, line) for line in lines)
            require_gates_passed(row, bindings)
            receipt, envelope = _commit_move(
                session, row=row, request=request, bindings=bindings, now=now
            )
            return CommittedContinuation(receipt=receipt, envelopes=(*filed, envelope))
    except LegacyContinuationRefusedError as error:
        logger.info(f"advance_legacy refused subject={subject} code={error.code.value}")
        raise _refused(error, subject=subject) from error


def _schema_refusal(error: ValidationError, *, kind: str, subject: str) -> TransactionRefusedError:
    """Return the refusal a record that does not validate is answered with."""
    fields = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
    return TransactionRefusedError(
        code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
        detail=f"the {kind} record does not validate; check {', '.join(fields) or 'the record'}",
        entity_ref=subject[:400],
        remediation=f"Correct the named fields of the {kind} and retry.",
    )


def _append_decision(
    context: Epoch2RootContext, request: RecordAppendRequest, *, now: datetime
) -> CommittedContinuation[RecordAppendReceipt]:
    """File one native decision, and the successor of any decision it supersedes.

    Raises:
        TransactionRefusedError: The decision does not validate, or its
            lifecycle move or supersession chain does not hold.
    """
    subject = f"{request.kind.value}/{request.record.get('key', '?')}"
    if "key" not in request.record and "id" in request.record:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=(
                f"decision {str(request.record['id'])[:40]} is in the epoch-1 shape "
                "(id, scope_id); a decision is appended as the native Decision document "
                "(key, scope_ref, alternatives, chosen_option_key, evidence_refs)"
            ),
            entity_ref=subject[:400],
            guard="decision_epoch1_shape",
            remediation="Resend the decision as the native Decision document.",
        )
    with context.session([_project_urn(context)]) as session:
        try:
            lines = decision_lines(
                request.record, _standing(session, Epoch2Collection.DECISION), at=now
            )
        except ValidationError as error:
            raise _schema_refusal(error, kind=request.kind.value, subject=subject) from error
        except DecisionError as error:
            illegal = error.code == "decision_transition_illegal"
            raise TransactionRefusedError(
                code=TransactionRefusalCode.ILLEGAL_TRANSITION
                if illegal
                else TransactionRefusalCode.TRANSITION_GUARD_FAILED,
                detail=str(error),
                entity_ref=subject[:400],
                guard=None if illegal else error.code,
                remediation="File a changed decision under a new key that supersedes it.",
            ) from error
        envelopes = tuple(commit_ledger_append(session, line) for line in lines)
    return CommittedContinuation(
        receipt=RecordAppendReceipt(
            collection=Epoch2Collection.DECISION,
            record_key=lines[-1].record_key,
            event_id=envelopes[-1].id,
        ),
        envelopes=envelopes,
    )


def append_record(
    context: Epoch2RootContext, request: RecordAppendRequest, *, now: datetime
) -> CommittedContinuation[RecordAppendReceipt]:
    """Append one audit, decision or artifact to the generation's ledger.

    Args:
        context: The native context of the tree.
        request: The already-validated request; its record is validated
            here, through the record kind's epoch-1 model.
        now: When the append happens.

    Returns:
        The receipt beside the one firehose row the append wrote.

    Raises:
        TransactionRefusedError: The record does not validate, or its
            ledger already holds a record under that id.
        NativeAuthorityRequiredError: The tree left epoch 2.
        LockTimeout: A lock stayed held past the lock timeout.
    """
    if request.kind is RecordKind.DECISION:
        return _append_decision(context, request, now=now)
    subject = f"{request.kind.value}/{request.record.get('id', '?')}"
    try:
        line = appended_record_line(request.kind, request.record, at=now)
    except ValidationError as error:
        raise _schema_refusal(error, kind=request.kind.value, subject=subject) from error
    with context.session([_project_urn(context)]) as session:
        if any(line.record_key in ledger_ids(held) for held in _standing(session, line.collection)):
            raise TransactionRefusedError(
                code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
                detail=f"the {line.collection.value} ledger already holds {line.record_key}",
                entity_ref=subject,
                remediation="Append the record under an id nothing holds yet.",
            )
        envelope = commit_ledger_append(session, line)
    return CommittedContinuation(
        receipt=RecordAppendReceipt(
            collection=line.collection, record_key=line.record_key, event_id=envelope.id
        ),
        envelopes=(envelope,),
    )


__all__ = [
    "LEGACY_EVENT_NAMESPACE",
    "CommittedContinuation",
    "LegacyAdvanceReceipt",
    "LegacyAdvanceRequest",
    "RecordAppendReceipt",
    "RecordAppendRequest",
    "advance_legacy",
    "append_record",
]
