"""Bringing an accepted Milestone back from its ledger so it can be reopened.

An accepted Milestone leaves the document on the commit that completes it:
its row becomes a line of the append-only Milestone ledger. Reopening it is
an ordinary guarded edge, ``COMPLETED -> ACTIVE``, but the transaction moves
a record the document holds, so the record has to be in the document first.

Re-admission is two writes inside one locked session, in this order. The
standing ledger row is written back into the document, and then a
correction line is appended that supersedes the acceptance line. The
acceptance line is never rewritten: it stays readable, and the correction
names its digest. Until the correction lands the ledger still holds the
record, so a crash between the two leaves the record in both places and
start-up recovery drops the document row, which is the state before the
reopen began. After it lands the ledger line is history, and the document
row is the record's one home again, so the reopen edge can take it.

The correction line is filed under its own key rather than the Milestone's,
so it is not a second standing row of the Milestone: once the Milestone is
accepted again it compacts into a fresh line under its own key.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Final

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import (
    LedgerRecord,
    effective_records,
    line_digest,
    read_ledger_records,
    render_ledger_line,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append, commit_row_write

logger = logging.getLogger(__name__)

#: The ledger-line key prefix a re-admission's correction line is filed under.
READMISSION_KEY_PREFIX: Final = "MRA-"

#: The status a re-admission's correction line records.
READMITTED_STATUS: Final = "readmitted"

#: The firehose name of the row write that puts the Milestone back.
MILESTONE_READMITTED_EVENT: Final = "domain.milestone.readmitted"


def standing_milestone(document_path: Path, key: str) -> LedgerRecord | None:
    """Return the ledger line that holds Milestone *key*, if one stands.

    Args:
        document_path: The selected generation's document, which the
            Milestone ledger resolves against.
        key: The Milestone's ``MLS-NNNN`` key.

    Returns:
        The last line filed under the key whose payload is the record, or
        ``None`` when the ledger holds no standing line for it.

    Raises:
        LedgerTornTailError: The ledger ends mid-line.
    """
    path = ledger_path(document_path, Epoch2Collection.MILESTONE)
    held = [
        item
        for item in effective_records(read_ledger_records(path))
        if item.record_key == key and item.payload.get("key") == key
    ]
    return held[-1] if held else None


def readmit_milestone(
    context: Epoch2RootContext, *, urn: QualifiedUrn, actor: str, now: datetime
) -> tuple[Envelope, ...]:
    """Put the accepted Milestone *urn* back in the document, superseding its ledger line.

    Args:
        context: The native context of the tree the Milestone lives in.
        urn: The Milestone to re-admit.
        actor: The principal reopening it.
        now: When the re-admission happened.

    Returns:
        The firehose rows the two writes appended, for the caller to
        publish; empty when the document already holds the Milestone,
        which is what a retry after a finished re-admission finds.

    Raises:
        TransactionRefusedError: A write carries a leak shape. Nothing
            was written by that write.
    """
    key = urn.entity_key
    with context.session([urn]) as session:
        if key in document_rows(session.read_document(), Epoch2Collection.MILESTONE):
            return ()
        line = standing_milestone(session.document_path, key)
        assert line is not None, "the reopen preflight read this Milestone from its ledger"
        revision = line.payload["revision"]
        restored = commit_row_write(
            session,
            collection=Epoch2Collection.MILESTONE,
            record_key=key,
            row=dict(line.payload),
            event_name=MILESTONE_READMITTED_EVENT,
            event_fields={
                "entity_ref": str(urn),
                "to_status": line.status,
                "revision_after": revision,
                "actor": actor,
            },
            compaction=None,
            now=now,
        )
        corrected = commit_ledger_append(
            session,
            LedgerRecord(
                collection=Epoch2Collection.MILESTONE,
                record_key=f"{READMISSION_KEY_PREFIX}{revision:04d}-{key}",
                status=READMITTED_STATUS,
                recorded_at=now,
                supersedes=line_digest(render_ledger_line(line)),
                payload={"milestone_ref": str(urn), "revision": revision, "actor": actor},
            ),
        )
    logger.info(f"readmit_milestone milestone={key} revision={revision}")
    return restored, corrected


__all__ = [
    "MILESTONE_READMITTED_EVENT",
    "READMISSION_KEY_PREFIX",
    "READMITTED_STATUS",
    "readmit_milestone",
    "standing_milestone",
]
