"""The ground truth a principal pins on one Batch audit subject.

A verification-site verdict is scored against what actually happened to the
subject it judged: one criterion of one Batch. Most of that truth is observed
from the ledgers -- the Batch merged and stayed clean, or a repair or a head move
refuted it -- but an operator can know better than the observation, and a
:class:`AuditGoldLabel` says so by hand. The label states whether the subject
was actually good as the Batch delivered it, and it overrides the observed
outcome of that subject's newest verdict.

Labels are append-only lines of the Batch ledger. A subject relabelled gets a
fresh line under the same key, and the newest line wins, so a correction
supersedes a mistake without rewriting it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Final, Literal

from pydantic import ConfigDict, StringConstraints

from eawf.kernel.delivery.receipts import canonical_digest
from eawf.kernel.state.epoch2.base import Epoch2Model, PrincipalKey
from eawf.kernel.state.epoch2.urns import BatchUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection

#: The payload kind a gold-label line states, which tells it apart from a Batch row.
GOLD_LABEL_KIND: Final = "audit_gold_label"

#: The Batch-ledger key prefix a subject's labels are filed under.
GOLD_LABEL_KEY_PREFIX: Final = "GLD-"

#: The status a gold-label line records.
GOLD_LABEL_STATUS: Final = "labeled"

#: Why a label was pinned: long enough to carry a real rationale.
LabelNote = Annotated[str, StringConstraints(strict=True, min_length=20, max_length=500)]

#: One Batch audit subject: the Batch key and the criterion id.
AuditSubject = tuple[str, str]


class AuditGoldLabel(Epoch2Model):
    """One principal's ground truth for one criterion of one Batch.

    Attributes:
        payload_kind: The line's kind, fixed.
        batch_ref: The Batch the subject belongs to.
        criterion_id: The criterion the subject is.
        ground_truth: ``True`` when the subject was actually good as delivered.
        labeled_by: The principal who pinned the label.
        labeled_at: When it was pinned.
        note: Why.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload_kind: Literal["audit_gold_label"] = GOLD_LABEL_KIND
    batch_ref: BatchUrn
    criterion_id: GateIdentityStr
    ground_truth: bool
    labeled_by: PrincipalKey
    labeled_at: UtcDatetime
    note: LabelNote

    @property
    def subject(self) -> AuditSubject:
        """Return the subject the label is about."""
        return (self.batch_ref.entity_key, self.criterion_id)


def gold_label_record_key(batch_ref: BatchUrn, criterion_id: str) -> str:
    """Return the Batch-ledger key every label of one subject is filed under.

    The criterion is digested rather than spelled so a long criterion id still
    fits the ledger's key bound.
    """
    digest = canonical_digest(criterion_id).removeprefix("sha256:")[:12]
    return f"{GOLD_LABEL_KEY_PREFIX}{batch_ref.entity_key}-{digest}"


def gold_label_line(label: AuditGoldLabel) -> LedgerRecord:
    """Return the Batch-ledger line one label is filed as."""
    return LedgerRecord(
        collection=Epoch2Collection.BATCH,
        record_key=gold_label_record_key(label.batch_ref, label.criterion_id),
        status=GOLD_LABEL_STATUS,
        recorded_at=label.labeled_at,
        payload=label.model_dump(mode="json"),
    )


def latest_gold_labels(records: Iterable[LedgerRecord]) -> Mapping[AuditSubject, AuditGoldLabel]:
    """Return the label in force for each subject: its newest line in ledger order.

    Args:
        records: The Batch ledger's lines, in the order they were appended.

    Returns:
        The label each labelled subject holds now.

    Raises:
        pydantic.ValidationError: A line claims to be a gold label and does not
            validate as one, which means the ledger is corrupt.
    """
    labels: dict[AuditSubject, AuditGoldLabel] = {}
    for item in records:
        if item.payload.get("payload_kind") != GOLD_LABEL_KIND:
            continue
        label = AuditGoldLabel.model_validate(item.payload)
        labels[label.subject] = label
    return labels


__all__ = [
    "GOLD_LABEL_KEY_PREFIX",
    "GOLD_LABEL_KIND",
    "GOLD_LABEL_STATUS",
    "AuditGoldLabel",
    "AuditSubject",
    "LabelNote",
    "gold_label_line",
    "gold_label_record_key",
    "latest_gold_labels",
]
