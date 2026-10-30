"""Back-fill the transcript index for a root's Runs that no hook or launcher indexed.

A Run started before the index existed names its vendor session only as a digest. The
daemon reads every Run of the root once, without a lock, and hands each Run's session
to :func:`~eawf.runtime.session.transcript_index.backfill_transcript_index`, which
finds the matching file in the runtime's own transcript directories.
"""

from __future__ import annotations

import logging
from datetime import datetime

from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.session.transcript_index import backfill_transcript_index

logger = logging.getLogger(__name__)


def backfill_root_transcripts(context: Epoch2RootContext, *, now: datetime) -> int:
    """Index the transcript of every Run of *context* that recorded a vendor session.

    Only the session and the reference of each Run are read, not the whole record,
    so a root with many Runs is scanned without validating each one.

    Args:
        context: The root whose Runs are back-filled.
        now: When the rows are written.

    Returns:
        How many rows were appended.

    Raises:
        NativeAuthorityRequiredError: The tree is not native.
        LedgerTornTailError: The run ledger ends mid-line.
        OSError: A transcript directory could not be listed.
        pydantic.ValidationError: A recorded vendor session does not validate.
        StateConflict: The index's append lock could not be acquired.
    """
    document = document_path(context.require_selected_generation())
    rows = document_rows(read_document(document), Epoch2Collection.RUN)
    records = read_ledger_records(ledger_path(document, Epoch2Collection.RUN))
    # A terminal Run is compacted out of the document into the run ledger, where the
    # last standing line of its key is its record.
    payloads = {
        item.record_key: item.payload
        for item in effective_records(records)
        if "payload_kind" not in item.payload and item.payload.get("key") == item.record_key
    } | rows
    sessions = [
        (str(payload["urn"]), VendorSessionRef.model_validate(payload["vendor_session"]))
        for payload in payloads.values()
        if payload.get("vendor_session") is not None
    ]
    recorded = backfill_transcript_index(context.identity.tree_root, sessions, now=now)
    logger.info(f"backfill_root_transcripts runs={len(sessions)} recorded={recorded}")
    return recorded


__all__ = ["backfill_root_transcripts"]
