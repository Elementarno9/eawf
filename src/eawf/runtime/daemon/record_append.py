"""The daemon side of filing an audit, decision or artifact in an epoch-2 tree.

:mod:`eawf.kernel.migration.epoch2.continuation` builds the ledger line an
audit or artifact becomes, and :mod:`eawf.kernel.migration.epoch2.decision_record`
the lines a native decision and its supersession chain become; this module
reads what those need under the root's locks and commits the lines through
:mod:`eawf.runtime.daemon.epoch2_transaction`, which stays the only writer of
a ledger line.

Every record is filed under the one project the tree imported, so the lock
of an append is scoped to that project's URN.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.identity import EntityKind, format_qualified_urn
from eawf.kernel.migration.epoch2.continuation import (
    RecordKind,
    appended_record_line,
    ledger_ids,
)
from eawf.kernel.migration.epoch2.decision_record import decision_lines
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.urn_rekey import spells_single_key
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
)


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
class CommittedAppend:
    """A committed append beside the firehose rows to publish after the locks.

    Attributes:
        receipt: What the caller answers with.
        envelopes: Every firehose row the commit appended, in order.
    """

    receipt: RecordAppendReceipt
    envelopes: tuple[Envelope, ...]


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
            remediation="Append records only on a tree that imported one project.",
        )
    code = projects[0]
    return format_qualified_urn(
        workspace_key=code,
        project_key=code,
        repository_key=None,
        kind=EntityKind.PROJECT,
        entity_key=code,
        single_key=spells_single_key(document),
    )


def _standing(session: RootSession, collection: Epoch2Collection) -> tuple[LedgerRecord, ...]:
    """Return the lines of one ledger that still stand."""
    return effective_records(read_ledger_records(session.ledger_path(collection)))


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
) -> CommittedAppend:
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
    return CommittedAppend(
        receipt=RecordAppendReceipt(
            collection=Epoch2Collection.DECISION,
            record_key=lines[-1].record_key,
            event_id=envelopes[-1].id,
        ),
        envelopes=envelopes,
    )


def append_record(
    context: Epoch2RootContext, request: RecordAppendRequest, *, now: datetime
) -> CommittedAppend:
    """Append one audit, decision or artifact to the generation's ledger.

    Args:
        context: The native context of the tree.
        request: The already-validated request; its record is validated
            here, through the record kind's epoch-1 model.
        now: When the append happens.

    Returns:
        The receipt beside every firehose row the append wrote.

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
    return CommittedAppend(
        receipt=RecordAppendReceipt(
            collection=line.collection, record_key=line.record_key, event_id=envelope.id
        ),
        envelopes=(envelope,),
    )


__all__ = [
    "CommittedAppend",
    "RecordAppendReceipt",
    "RecordAppendRequest",
    "append_record",
]
