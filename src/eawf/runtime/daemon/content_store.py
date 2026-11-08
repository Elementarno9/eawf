"""File and read the bounded content transcript blocks unfold to.

Content is filed on the root's artifact ledger as a :class:`StoredContent` line, named by
a reference derived from the Run and the kept lines, so filing the same output twice
names the one line already standing rather than writing a second. The text is bounded
and scrubbed by :func:`~eawf.kernel.runtime.content.bound_content` before it is filed,
so the ledger never holds a withheld value.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Final

from eawf.kernel.identity import QualifiedUrn, parse_qualified_urn
from eawf.kernel.runtime.content import CONTENT_PAYLOAD_KIND, StoredContent, bound_content
from eawf.kernel.store.ledger import LedgerRecord, read_collection_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append

logger = logging.getLogger(__name__)

#: The reference prefix every stored content is named under.
CONTENT_REF_PREFIX: Final = "artifact://content/"


def stored_contents(session: RootSession, run_ref: str | QualifiedUrn) -> dict[str, StoredContent]:
    """Return one Run's stored contents by reference.

    Only lines named under the Run's own reference prefix are validated, so a line of
    another Run never refuses this Run's read or filing.

    Args:
        session: The open session the artifact ledger is read under.
        run_ref: The Run whose contents are asked for.

    Returns:
        The Run's stored contents, keyed by artifact reference.

    Raises:
        ValidationError: A line of this Run claims to be stored content and does not
            validate as one, which means the ledger is corrupt rather than merely
            unfamiliar.
    """
    prefix = _run_prefix(run_ref)
    held: dict[str, StoredContent] = {}
    for item in read_collection_records(session.document_path, Epoch2Collection.ARTIFACT):
        if item.payload.get("payload_kind") != CONTENT_PAYLOAD_KIND:
            continue
        if not item.record_key.startswith(prefix):
            continue
        content = StoredContent.model_validate(item.payload)
        held[content.artifact_ref] = content
    return held


def _run_prefix(run_ref: str | QualifiedUrn) -> str:
    """Return the reference prefix every content of *run_ref* is named under."""
    parsed = parse_qualified_urn(run_ref) if isinstance(run_ref, str) else run_ref
    return f"{CONTENT_REF_PREFIX}{parsed.entity_key.lower()}/"


def file_content(session: RootSession, *, run_ref: QualifiedUrn, text: str, now: datetime) -> str:
    """File *text* as one Run's bounded content and return its reference.

    Args:
        session: The open session of the Run, whose locks the append is decided under.
        run_ref: The Run that produced the text.
        text: The raw text; it is bounded and scrubbed here.
        now: The daemon's recording clock.

    Returns:
        The content's artifact reference. The same kept lines of the same Run name the
        same reference, and a second filing appends nothing.
    """
    bounded = bound_content(text)
    ref = f"{_run_prefix(run_ref)}{bounded.digest}"
    if ref in stored_contents(session, run_ref):
        return ref
    content = StoredContent(
        artifact_ref=ref,
        run_ref=run_ref,
        lines=bounded.lines,
        total_lines=bounded.total_lines,
        withheld_lines=bounded.withheld_lines,
        recorded_at=now,
    )
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.ARTIFACT,
            record_key=ref,
            status=CONTENT_PAYLOAD_KIND,
            recorded_at=now,
            payload=content.model_dump(mode="json"),
        ),
    )
    logger.info(
        f"file_content run={run_ref.entity_key} lines={len(bounded.lines)} "
        f"total={bounded.total_lines} withheld={bounded.withheld_lines}"
    )
    return ref


__all__ = ["CONTENT_REF_PREFIX", "file_content", "stored_contents"]
