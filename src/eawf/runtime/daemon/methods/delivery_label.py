"""``runtime.delivery.label_audit``: a principal's ground truth for one Batch audit subject.

The jury's calibration scores each verification-site verdict against what its
subject went on to do. The daemon observes most of that from the Batch ledger,
but a principal can know the truth of a subject better than the observation,
and this verb files that knowledge as an
:class:`~eawf.kernel.delivery.gold_label.AuditGoldLabel` line on the Batch
ledger. The label anchors on a subject some verification cycle actually held a
verdict for: a label about a criterion nobody judged calibrates nothing.

Labels are append-only. Relabelling a subject files a fresh line under the same
key and the newest line wins, so a correction supersedes a mistake without
rewriting it. The verb is sent against the Batch as the caller read it, and a
retry under the same idempotency key replays the first answer.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.delivery.batch_proof import BatchVerificationCycle
from eawf.kernel.delivery.gold_label import AuditGoldLabel, LabelNote, gold_label_line
from eawf.kernel.delivery.integration import IdempotencyKey
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import PrincipalKey, StrictPositiveInt
from eawf.kernel.state.epoch2.urns import BatchUrn
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.eval.native_cohort import (
    newest_by_subject,
    observe_verdict_outcomes,
    scoreable,
)
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import CYCLE_KEY_PREFIX, keyed_call
from eawf.runtime.daemon.methods.delivery_anchor import require_anchor
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.daemon.verdict_observations import jury_producers

logger = logging.getLogger(__name__)

#: The verb that files a principal's ground truth for one Batch audit subject.
DELIVERY_LABEL_AUDIT_METHOD: Final = "runtime.delivery.label_audit"


class LabelAuditParams(BaseModel):
    """Params of :data:`DELIVERY_LABEL_AUDIT_METHOD`.

    Attributes:
        urn: The Batch the subject belongs to.
        actor: The principal pinning the label.
        idempotency_key: The client's name for this request.
        expected_revision: The revision the caller read the Batch at, or ``None``
            for a caller that sends no anchor. A stale one is refused with
            ``revision_conflict``.
        criterion_id: The criterion the subject is.
        ground_truth: ``True`` when the subject was actually good as delivered.
        note: Why the label is pinned.
    """

    model_config = ConfigDict(extra="forbid")

    urn: BatchUrn
    actor: PrincipalKey
    idempotency_key: IdempotencyKey
    expected_revision: StrictPositiveInt | None = None
    criterion_id: GateIdentityStr
    ground_truth: bool
    note: LabelNote


class LabelAuditAnswer(BaseModel):
    """What one label filing answers with.

    Attributes:
        batch_ref: The Batch the subject belongs to.
        criterion_id: The criterion the subject is.
        ground_truth: The truth the label pins.
        labeled_at: When it was filed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: str
    criterion_id: str
    ground_truth: bool
    labeled_at: str


def label_audit(
    context: Epoch2RootContext, args: LabelAuditParams, *, now: datetime
) -> LabelAuditAnswer:
    """File one gold label for a subject some cycle of the Batch held a verdict for.

    Args:
        context: The native context of the tree the Batch lives in.
        args: The validated request.
        now: When the label is pinned.

    Returns:
        The subject and the truth filed for it.

    Raises:
        DaemonValidationError: No verification cycle of the Batch ever held a
            verdict for the criterion, or its newest verdict is one the jury's
            cohort never scores, so there is nothing for the label to score.
    """
    wanted = f"{CYCLE_KEY_PREFIX}{args.urn.entity_key}"
    with context.session([str(args.urn)]) as session:
        lines = tuple(
            BatchVerificationCycle.model_validate(item.payload)
            for item in read_ledger_records(session.ledger_path(Epoch2Collection.BATCH))
            if item.record_key == wanted
        )
        # The label judges the subject's newest verdict, so it is anchored by the
        # same rule the cohort scores by; settlement plays no part in that rule.
        newest = newest_by_subject(observe_verdict_outcomes(lines, merged_batches=frozenset())).get(
            (args.urn.entity_key, args.criterion_id)
        )
        if newest is None:
            raise DaemonValidationError(
                f"validation_failed: gold_label_unanchored: no verification cycle of batch "
                f"{args.urn.entity_key} held a verdict for criterion {args.criterion_id}, so a "
                "label on it scores nothing"
            )
        run_lines = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
        if not scoreable(newest, jury_producers(session.read_document(), run_lines)):
            raise DaemonValidationError(
                f"validation_failed: gold_label_unscored: the newest verdict on criterion "
                f"{args.criterion_id} of batch {args.urn.entity_key} is unverified or no "
                "reviewer identity answers for it, so the cohort never scores a label on it"
            )
        label = AuditGoldLabel(
            batch_ref=args.urn,
            criterion_id=args.criterion_id,
            ground_truth=args.ground_truth,
            labeled_by=args.actor,
            labeled_at=now,
            note=args.note,
        )
        commit_ledger_append(session, gold_label_line(label))
    logger.info(
        f"label_audit batch={args.urn.entity_key} criterion={args.criterion_id} "
        f"ground_truth={args.ground_truth}"
    )
    return LabelAuditAnswer(
        batch_ref=str(args.urn),
        criterion_id=args.criterion_id,
        ground_truth=args.ground_truth,
        labeled_at=now.isoformat(),
    )


@native_mutator(DELIVERY_LABEL_AUDIT_METHOD)
async def _label_audit(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """File a principal's ground truth for one Batch audit subject."""
    args = native_params(LabelAuditParams, params)
    context = ctx.native_root_context(authority.root)
    await asyncio.to_thread(require_anchor, context, args.urn, args.expected_revision)
    now = datetime.now(UTC)
    return await asyncio.to_thread(
        keyed_call,
        context,
        method=DELIVERY_LABEL_AUDIT_METHOD,
        key=args.idempotency_key,
        params=args.model_dump(mode="json"),
        call=partial(label_audit, context, args, now=now),
        at=now,
    )


__all__ = [
    "DELIVERY_LABEL_AUDIT_METHOD",
    "LabelAuditAnswer",
    "LabelAuditParams",
    "label_audit",
]
