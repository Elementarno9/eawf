"""The compare-and-swap anchor a delivery verb is sent under.

The candidate, integration, proof, approval and evidence verbs file facts
beside a lifecycle record rather than moving it, so none of them runs
through the transition transaction that checks ``expected_revision`` for a
lifecycle move. A caller still decided to send one of them against the
record as it read it, and a record that moved since -- a Batch that left
``MERGING``, a Task that was reopened -- is a different subject from the
one the decision was taken about. :func:`require_anchor` refuses that
request with the same ``revision_conflict`` a stale lifecycle move gets.

The anchor is read in its own locked session ahead of the verb's effect,
because each verb opens its own sessions (a proof runs its gates between
two of them) and there is no single session to check it inside.
"""

from __future__ import annotations

import logging

from pydantic import ValidationError

from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.tiers import ENTITY_COLLECTIONS, StorageTier, tier_for
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import (
    LIFECYCLE_ENTITIES,
    RECORD_CLASSES,
    TransactionRefusalCode,
    TransactionRefusedError,
)

logger = logging.getLogger(__name__)


def _standing_revision(session: RootSession, urn: QualifiedUrn) -> int | None:
    """Return the revision *urn*'s record stands at, or ``None`` when none is held.

    A terminal record is compacted out of the document into its ledger, so
    the ledger is read when the document holds no row: a Run whose report
    is bound after it finished is still the subject of that request.
    """
    entity = LIFECYCLE_ENTITIES[urn.kind]
    collection = ENTITY_COLLECTIONS[urn.kind]
    row = document_rows(session.read_document(), collection).get(urn.entity_key)
    if row is not None:
        return RECORD_CLASSES[entity].model_validate(row).revision
    if tier_for(collection) is not StorageTier.LEDGER:
        return None
    revision: int | None = None
    for line in effective_records(read_ledger_records(session.ledger_path(collection))):
        if line.record_key != urn.entity_key:
            continue
        try:
            revision = RECORD_CLASSES[entity].model_validate(line.payload).revision
        except ValidationError:
            continue
    return revision


def require_anchor(
    context: Epoch2RootContext, urn: QualifiedUrn, expected_revision: int | None
) -> None:
    """Refuse a request anchored at a revision its subject no longer stands at.

    Args:
        context: The native context of the addressed root.
        urn: The subject the request names.
        expected_revision: The revision the caller read the subject at, or
            ``None`` for a caller that sends no anchor.

    Raises:
        TransactionRefusedError: ``identity_kind_mismatch`` when the subject
            is not a lifecycle record, which has no revision to anchor on;
            ``identity_not_found`` when no record is held under it; or
            ``revision_conflict`` when it stands at another revision. The
            refusal carries the current revision, so the caller can re-read
            and retry without a second round trip.
    """
    if expected_revision is None:
        return
    if urn.kind not in LIFECYCLE_ENTITIES:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_KIND_MISMATCH,
            detail=f"a {urn.kind.value} carries no revision to anchor a request on",
            entity_ref=str(urn),
            remediation="Name a Track, Milestone, Batch, Task or Run as the subject.",
        )
    with context.session([urn]) as session:
        revision = _standing_revision(session, urn)
    if revision is None:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.IDENTITY_NOT_FOUND,
            detail=f"the tree holds no record keyed {urn.entity_key!r}",
            entity_ref=str(urn),
            remediation="Name a record the tree holds.",
        )
    if revision != expected_revision:
        logger.info(
            f"require_anchor refused urn={urn.entity_key} expected={expected_revision} "
            f"revision={revision}"
        )
        raise TransactionRefusedError(
            code=TransactionRefusalCode.REVISION_CONFLICT,
            detail=f"the record is at revision {revision} but the request expects "
            f"{expected_revision}",
            entity_ref=str(urn),
            remediation="Re-read the record and retry against its current revision.",
            revision=revision,
        )


__all__ = ["require_anchor"]
