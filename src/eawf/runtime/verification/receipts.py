"""Receipt freshness and reuse for compiled execution contracts.

A receipt proves something only about the exact inputs it ran against.
This module turns a compiled :class:`~eawf.workflow.delivery.criteria.ExecutionContract`
plus the runtime facts of a leg into the expected
:class:`~eawf.kernel.delivery.receipts.ProofFreshnessKey`, refuses a
receipt whose key differs, and decides for every required leg whether a
fresh deterministic receipt can be reused. Every leg that is not reused is
returned with its reason, so an audit names each rerun instead of silently
re-running or silently trusting.

Which revision a leg is required at is the caller's decision, not this
module's: :func:`build_verification_leg` takes the binding it is handed.
That is what lets one caller require a proof at the head everyone is
delivering while requiring another at the older revision it was proved on.

Nothing here runs a gate or touches storage: callers pass the receipts
they hold and act on the returned plan.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Self

from pydantic import ConfigDict, model_validator

from eawf.kernel.delivery.receipts import (
    REASON_DISPOSITION,
    ComponentComparison,
    ExternalInputDigest,
    FreshnessComponent,
    ProofFreshnessKey,
    ProofReceipt,
    ReceiptReuseDecision,
    ReceiptReusePlan,
    ReuseReason,
    RevisionBinding,
)
from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.epoch2.base import Epoch2Model, Sha256DigestStr
from eawf.workflow.delivery.criteria import ExecutionContract

logger = logging.getLogger(__name__)


class StaleReceiptError(ValueError):
    """A receipt was offered for inputs other than the ones it ran against.

    Attributes:
        receipt_id: The refused receipt.
        changed: The freshness components whose digests differ, in
            :class:`~eawf.kernel.delivery.receipts.FreshnessComponent` order.
    """

    def __init__(self, receipt_id: str, changed: Sequence[FreshnessComponent]) -> None:
        """Keep the refused receipt and the inputs that moved.

        Args:
            receipt_id: The refused receipt's key.
            changed: The components whose digests differ; non-empty.
        """
        self.receipt_id = receipt_id
        self.changed = tuple(changed)
        names = ", ".join(component.value for component in self.changed)
        super().__init__(f"receipt {receipt_id!r} is stale: changed {names}")


def compute_proof_freshness_key(
    contract: ExecutionContract,
    *,
    revision_binding: RevisionBinding,
    selector_digest: str,
    policy_digest: str,
    runner_digest: str,
    environment_digest: str,
    external_inputs: Sequence[ExternalInputDigest] = (),
) -> ProofFreshnessKey:
    """Assemble the key a receipt for *contract* must carry to count.

    The criterion and gate components come from the contract's own
    recomputed digests, so editing a covered criterion or the gate moves
    the key even when every runtime fact is unchanged.

    Args:
        contract: The compiled contract the leg executes.
        revision_binding: The exact code and frozen plan inputs of the leg.
        selector_digest: ``sha256:`` digest of the selector result the
            runner receives.
        policy_digest: ``sha256:`` digest of the effective verification
            policy.
        runner_digest: ``sha256:`` digest of the runner and toolchain.
        environment_digest: ``sha256:`` digest of the execution
            environment; must equal the binding's when the binding has one.
        external_inputs: Every declared external input the proof reads.

    Returns:
        The expected freshness key.

    Raises:
        pydantic.ValidationError: A digest is malformed, an external input
            repeats, or the environment disagrees with the binding.
    """
    return ProofFreshnessKey(
        revision_binding=revision_binding,
        criterion_digest=contract.criteria_digest(),
        gate_digest=contract.gate_digest(),
        selector_digest=selector_digest,
        policy_digest=policy_digest,
        runner_digest=runner_digest,
        environment_digest=environment_digest,
        external_inputs=tuple(external_inputs),
    )


def compare_freshness(
    recorded: ProofFreshnessKey,
    expected: ProofFreshnessKey,
) -> tuple[ComponentComparison, ...]:
    """Compare two keys component by component.

    Args:
        recorded: The key a receipt was taken with.
        expected: The key the current leg requires.

    Returns:
        One comparison per component, in component order.
    """
    recorded_digests = recorded.component_digests()
    expected_digests = expected.component_digests()
    return tuple(
        ComponentComparison(
            component=component,
            expected_digest=expected_digests[component],
            recorded_digest=recorded_digests[component],
        )
        for component in FreshnessComponent
    )


def require_fresh_receipt(receipt: ProofReceipt, expected: ProofFreshnessKey) -> ProofReceipt:
    """Return *receipt* only when it was taken against exactly *expected*.

    A receipt bound to an older exact head, or to any other changed input,
    is refused with the components that moved.

    Args:
        receipt: The receipt offered as proof.
        expected: The key the current leg requires.

    Returns:
        *receipt* unchanged.

    Raises:
        StaleReceiptError: The receipt's key differs from *expected*.
    """
    if receipt.freshness_key == expected.digest():
        return receipt
    changed = [
        item.component
        for item in compare_freshness(receipt.freshness, expected)
        if not item.matches
    ]
    logger.info(
        f"require_fresh_receipt status=stale receipt={receipt.id!r} "
        f"changed={[component.value for component in changed]}"
    )
    raise StaleReceiptError(receipt.id, changed)


class VerificationLeg(Epoch2Model):
    """One required proof: a compiled contract and the key it must be proved at."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: ExecutionContract
    expected: ProofFreshnessKey

    @model_validator(mode="after")
    def _key_belongs_to_contract(self) -> Self:
        """Require the expected key to carry this contract's digests.

        Raises:
            ValueError: The key's criterion or gate digest was computed from
                another contract.
        """
        if (
            self.expected.criterion_digest != self.contract.criteria_digest()
            or self.expected.gate_digest != self.contract.gate_digest()
        ):
            raise ValueError(
                f"leg {self.contract.gate_id!r} expects a key built for another contract"
            )
        return self


class ProofRuntimeFacts(Epoch2Model):
    """The half of a freshness key that no stored record supplies.

    A deterministic proof is reusable only while the runner, the
    execution environment, the selector result and the effective policy
    are still what they were, and none of those four is derivable from
    the plan or from the tree: they are observations of the machine that
    ran the proof. They travel together so a leg cannot be assembled from
    three of them plus a guess at the fourth.

    Attributes:
        selector_digest: Digest of the selector result the runner receives.
        policy_digest: Digest of the effective verification policy.
        runner_digest: Digest of the runner and its toolchain.
        environment_digest: Digest of the execution environment.
        external_inputs: Every declared external input the proof reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector_digest: Sha256DigestStr
    policy_digest: Sha256DigestStr
    runner_digest: Sha256DigestStr
    environment_digest: Sha256DigestStr
    external_inputs: tuple[ExternalInputDigest, ...] = ()


def build_verification_leg(
    contract: ExecutionContract,
    *,
    revision_binding: RevisionBinding,
    facts: ProofRuntimeFacts,
) -> VerificationLeg:
    """Return the leg *contract* must be proved at on *revision_binding*.

    Args:
        contract: The compiled contract the leg executes.
        revision_binding: The exact code the proof is required against.
        facts: The runtime half of the key.

    Returns:
        The required leg, carrying the key a receipt must match.

    Raises:
        pydantic.ValidationError: A digest is malformed, an external input
            repeats, or the binding and the runner disagree about the
            execution environment.
    """
    return VerificationLeg(
        contract=contract,
        expected=compute_proof_freshness_key(
            contract,
            revision_binding=revision_binding,
            selector_digest=facts.selector_digest,
            policy_digest=facts.policy_digest,
            runner_digest=facts.runner_digest,
            environment_digest=facts.environment_digest,
            external_inputs=facts.external_inputs,
        ),
    )


def _is_expired(receipt: ProofReceipt, *, now: datetime | None, max_age: timedelta | None) -> bool:
    """Return whether *receipt* is older than the reuse age limit, when one is set."""
    if now is None or max_age is None:
        return False
    return now - receipt.ended_at > max_age


def _pick_candidate(
    leg: VerificationLeg,
    receipts: Sequence[ProofReceipt],
    superseded: set[str],
) -> ProofReceipt | None:
    """Return the receipt that best answers *leg*, preferring an exact key.

    Among live (unsuperseded) receipts for the leg, the latest one at the
    expected key wins; failing that, the latest one at any key, so a stale
    verdict can name what moved.
    """
    live = [
        receipt
        for receipt in receipts
        if receipt.scope_id == leg.contract.scope_id
        and receipt.gate_id == leg.contract.gate_id
        and receipt.id not in superseded
    ]
    if not live:
        return None
    expected_key = leg.expected.digest()
    exact = [receipt for receipt in live if receipt.freshness_key == expected_key]
    pool = exact or live
    return max(pool, key=lambda receipt: (receipt.ended_at, receipt.id))


def _decision(
    leg: VerificationLeg,
    reason: ReuseReason,
    *,
    receipt: ProofReceipt | None,
) -> ReceiptReuseDecision:
    """Build the decision for *leg*, comparing every component when a receipt exists."""
    comparisons = () if receipt is None else compare_freshness(receipt.freshness, leg.expected)
    return ReceiptReuseDecision(
        scope_id=leg.contract.scope_id,
        gate_id=leg.contract.gate_id,
        criterion_ids=leg.contract.criterion_ids,
        receipt_id=None if receipt is None else receipt.id,
        expected_freshness_key=leg.expected.digest(),
        comparisons=comparisons,
        disposition=REASON_DISPOSITION[reason],
        reason=reason,
    )


def decide_leg_reuse(
    leg: VerificationLeg,
    receipts: Sequence[ProofReceipt],
    *,
    now: datetime | None = None,
    max_age: timedelta | None = None,
) -> ReceiptReuseDecision:
    """Decide whether one required leg can reuse a receipt, and why.

    A judgment leg is unavailable to this path. A deterministic leg reuses
    a receipt only when it is unsuperseded, at the exact expected key,
    passed, and within the age limit; otherwise the leg reruns with the
    first reason that applies: missing, stale, not passed, expired.

    Args:
        leg: The required proof.
        receipts: The receipts held for the scope, in any order.
        now: The UTC decision time; required together with *max_age*.
        max_age: The oldest receipt age that may be reused; ``None`` means
            no age limit.

    Returns:
        The leg's decision.

    Raises:
        ValueError: Exactly one of *now* and *max_age* is given, or *now*
            is naive.
    """
    if (now is None) != (max_age is None):
        raise ValueError("now and max_age must be given together")
    if now is not None and now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if leg.contract.evidence_kind != "deterministic":
        return _decision(leg, ReuseReason.NOT_DETERMINISTIC, receipt=None)
    superseded = {receipt.supersedes_id for receipt in receipts if receipt.supersedes_id}
    candidate = _pick_candidate(leg, receipts, superseded)
    if candidate is None:
        reason = ReuseReason.MISSING
    elif candidate.freshness_key != leg.expected.digest():
        reason = ReuseReason.STALE
    elif candidate.result is not GateReceiptResult.PASS:
        reason = ReuseReason.NOT_PASSED
    elif _is_expired(candidate, now=now, max_age=max_age):
        reason = ReuseReason.EXPIRED
    else:
        reason = ReuseReason.FRESH
    return _decision(leg, reason, receipt=candidate)


def decide_receipt_reuse(
    legs: Sequence[VerificationLeg],
    receipts: Sequence[ProofReceipt],
    *,
    now: datetime | None = None,
    max_age: timedelta | None = None,
) -> ReceiptReusePlan:
    """Decide reuse for every required leg of an audit.

    Args:
        legs: The required proofs; one per ``(scope_id, gate_id)``.
        receipts: Every receipt held for those scopes.
        now: The UTC decision time; required together with *max_age*.
        max_age: The oldest receipt age that may be reused; ``None`` means
            no age limit.

    Returns:
        The plan: :attr:`~eawf.kernel.delivery.receipts.ReceiptReusePlan.reused`
        names each reused leg and
        :attr:`~eawf.kernel.delivery.receipts.ReceiptReusePlan.reruns` names
        each leg to rerun with its reason.

    Raises:
        ValueError: Exactly one of *now* and *max_age* is given, or a leg
            repeats.
    """
    decisions = tuple(decide_leg_reuse(leg, receipts, now=now, max_age=max_age) for leg in legs)
    plan = ReceiptReusePlan(decisions=decisions)
    logger.info(
        f"decide_receipt_reuse legs={len(decisions)} reused={len(plan.reused)} "
        f"reruns={len(plan.reruns)} unavailable={len(plan.unavailable)}"
    )
    return plan


__all__ = [
    "ProofRuntimeFacts",
    "StaleReceiptError",
    "VerificationLeg",
    "build_verification_leg",
    "compare_freshness",
    "compute_proof_freshness_key",
    "decide_leg_reuse",
    "decide_receipt_reuse",
    "require_fresh_receipt",
]
