"""The commit path of a whole-document rewrite its caller decided.

A migration that re-addresses every row of a tree at once has no single
row for :func:`~eawf.runtime.daemon.epoch2_transaction.commit_row_write`
and no guarded edge for a transition, but it still owes the steps every
native commit walks, in the same order, so a crash mid-rewrite is finished
or abandoned by the replay like any other commit.
"""

from __future__ import annotations

import copy
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.canonical_sequence import CanonicalSequenceAllocator
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.io import state_version
from eawf.kernel.store.append import append_json_line
from eawf.kernel.store.envelope import Envelope
from eawf.observability.logging.state_leak import state_leak_refusal
from eawf.runtime.daemon.epoch2_changes import file_changes, journal_changes
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    CANONICAL_SEQUENCE_KEY,
    TRANSITION_EVENT_SCHEMA_VERSION,
    TransactionRefusalCode,
    TransactionRefusedError,
    _firehose_path,
    _high_water_mark,
)
from eawf.runtime.daemon.wal import WalRecord, mark_applied, mark_fsynced, write_pending

logger = logging.getLogger(__name__)


def commit_document_rewrite(
    session: RootSession,
    *,
    new_document: dict[str, Any],
    event_name: str,
    event_fields: Mapping[str, Any],
    now: datetime,
) -> Envelope:
    """Commit one whole-document rewrite its caller decided, inside *session*.

    The steps are those of a row write: the leak scrub, the sequence drawn
    inside the commit, one WAL intent around one document write, the change
    feed's record of every row it changed, and one firehose row.

    Args:
        session: The open session the rewrite was decided under.
        new_document: The whole document to leave behind. Its sequence
            high-water mark is replaced by the one this commit draws.
        event_name: The dotted name the firehose row carries.
        event_fields: What the event says about the rewrite, beside the
            fields every event carries.
        now: When the rewrite happened.

    Returns:
        The firehose row the commit appended, for a caller that publishes
        it once the session is released.

    Raises:
        TransactionRefusedError: The rewrite adds text carrying a leak
            shape. Nothing was written.
        ValueError: The document holds a non-integer sequence.
        RuntimeError: The session is closed.
        UndeclaredPathError: The commit policy declares no row for the
            firehose or the change feed.
    """
    context = session.context
    subject = parse_qualified_urn(session.locked_urns[0])
    document = session.read_document()
    rewritten = copy.deepcopy(new_document)
    refusal = state_leak_refusal(document, rewritten)
    if refusal is not None:
        logger.warning(f"epoch2 document rewrite refused for leaks event={event_name!r}")
        raise TransactionRefusedError(
            code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
            detail=refusal,
            entity_ref=str(subject),
            remediation="Remove the flagged text from the rewrite and retry.",
        )
    allocator = CanonicalSequenceAllocator.recover(
        workspace_key=subject.workspace_key,
        high_water_mark=_high_water_mark(document),
    )
    with allocator.transaction() as sequences:
        sequence = sequences.allocate()
        rewritten[CANONICAL_SEQUENCE_KEY] = sequence
        event_id = f"evt-{uuid.uuid4().hex}"
        envelope = Envelope(
            id=event_id,
            kind=StoreKind.EVENT,
            scope_id=session.locked_urns[0],
            created_at=now,
            summary=event_name,
            payload={
                "schema_version": TRANSITION_EVENT_SCHEMA_VERSION,
                "name": event_name,
                "event_id": event_id,
                "occurred_at": now.isoformat(),
                "workspace_ref": subject.workspace_key,
                "project_ref": subject.project_key,
                "canonical_sequence": sequence,
                **event_fields,
            },
        )
        envelope, changes = journal_changes(
            context,
            document_path=session.document_path,
            before=document,
            after=rewritten,
            envelope=envelope,
            event_name=event_name,
            sequence=sequence,
            actor_ref=None,
            now=now,
        )
        wal_record = WalRecord(
            record_id=uuid.uuid4().hex,
            envelope=envelope,
            written_at=now,
            before_state_version=state_version(document),
            after_state_version=state_version(rewritten),
            state_path=str(session.document_path),
        )
        write_pending(context.wal_dir, wal_record)
        session.write_document(rewritten)
        mark_applied(context.wal_dir, wal_record.record_id)
        file_changes(session.document_path, changes)
        append_json_line(_firehose_path(context), envelope.model_dump_json())
        mark_fsynced(context.wal_dir, wal_record.record_id)
    logger.info(
        f"epoch2 document rewrite committed root={context.identity.root_id} "
        f"event={event_name!r} rows_changed={len(changes)} sequence={sequence}"
    )
    return envelope


__all__ = ["commit_document_rewrite"]
