"""Admitting a Run against the prompt budget and the in-flight governor.

Admission runs inside the locked pass that mints a dispatch attempt, so
it is decided before a lease is issued or a provider started, and two
dispatches racing for the last slot are serialized by the root's document
lock rather than both admitted.

What is in flight is derived, never stored as a counter. A Run holds its
reservation while its latest admission receipt says admitted and its
control-reduced status is not terminal, so a Run that completes, fails,
is cancelled or is reconciled after its provider was lost releases its
reservation by reaching that status -- there is no release step that a
crash or a missed reconciliation could skip and leak.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from eawf.kernel.config.layered import merge_config
from eawf.kernel.economics.governor import (
    AdmissionDecision,
    AdmissionReceipt,
    EconomicsPolicy,
    RunReservation,
    decide_admission,
    economics_policy_from,
)
from eawf.kernel.economics.prompt_budget import (
    BudgetClassId,
    RenderedSize,
    evaluate_prompt_budget,
)
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.control import TERMINAL_RUN_STATUSES
from eawf.kernel.runtime.usage import UsagePayload, aggregate_usage
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.run_events import run_events_of

logger = logging.getLogger(__name__)

#: The ledger-line key an admission receipt is filed under. One Run may
#: carry several -- queued, then later admitted -- and the last one stands.
_RECEIPT_KEY_PREFIX: Final = "ADM-"


class EconomicsPolicyError(ValueError):
    """The root's ``economics`` configuration does not validate."""


def load_economics(repo_root: Path) -> EconomicsPolicy:
    """Return the ``economics`` policy of the repository at *repo_root*.

    Args:
        repo_root: The repository whose layered configuration is merged.

    Returns:
        The validated policy, defaulted where the configuration is silent.

    Raises:
        EconomicsPolicyError: The table breaks a policy rule.
    """
    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    try:
        return economics_policy_from(merged)
    except ValidationError as error:
        raise EconomicsPolicyError(f"economics configuration is invalid: {error}") from error


def rendered_sizes(*, capsule: AuthorityCapsule, prompt: str) -> tuple[RenderedSize, ...]:
    """Measure what the provider is started with, class by class.

    The capsule and the prompt are measured in the bytes that are handed
    over. No token count is measured here, because nothing on this path
    tokenizes, so a token ceiling on either class stays unavailable rather
    than being guessed.
    """
    return (
        RenderedSize(
            class_id=BudgetClassId.AUTHORITY_CAPSULE,
            bytes=len(capsule.model_dump_json().encode("utf-8")),
        ),
        RenderedSize(class_id=BudgetClassId.TASK_PACKET, bytes=len(prompt.encode("utf-8"))),
    )


def latest_receipts(records: Sequence[LedgerRecord]) -> dict[str, AdmissionReceipt]:
    """Return each Run's standing admission receipt, keyed by Run URN."""
    latest: dict[str, AdmissionReceipt] = {}
    for item in records:
        if item.payload.get("payload_kind") == "admission_receipt":
            receipt = AdmissionReceipt.model_validate(item.payload)
            latest[str(receipt.run_ref)] = receipt
    return latest


def in_flight_reservations(
    records: Sequence[LedgerRecord],
    *,
    status_of: Callable[[RunUrn], RunStatus],
    excluding: RunUrn,
) -> tuple[RunReservation, ...]:
    """Return what every other live, admitted Run holds, accrual included.

    Args:
        records: Every line the run ledger holds.
        status_of: The control-reduced status of a Run.
        excluding: The Run being admitted, which holds nothing yet.

    Returns:
        One reservation per live admitted Run, carrying what its usage
        events say it has accrued so far.
    """
    held: list[RunReservation] = []
    for receipt in latest_receipts(records).values():
        if receipt.decision is not AdmissionDecision.ADMITTED or receipt.run_ref == excluding:
            continue
        if status_of(receipt.run_ref) in TERMINAL_RUN_STATUSES:
            continue
        accrued = aggregate_usage(
            event.payload
            for event in run_events_of(records, receipt.run_ref)
            if event.quarantine is None and isinstance(event.payload, UsagePayload)
        )
        held.append(
            receipt.reservation.model_copy(
                update={
                    "accrued_tokens": accrued.tokens,
                    "accrued_cost_microusd": accrued.cost_microusd,
                }
            )
        )
    return tuple(held)


def admit_run(
    session: RootSession,
    records: Sequence[LedgerRecord],
    *,
    policy: EconomicsPolicy,
    capsule: AuthorityCapsule,
    prompt: str,
    status_of: Callable[[RunUrn], RunStatus],
    now: datetime,
) -> AdmissionReceipt:
    """Decide and record whether the capsule's Run may start now.

    The receipt is appended whatever it decides, so a queued or denied
    Run carries the reason on its ledger. Nothing else is written: the
    Run's lifecycle is not the governor's to move.

    Args:
        session: The locked session the attempt is minted under.
        records: Every line the run ledger holds.
        policy: The prompt budget and governor in force.
        capsule: The sealed capsule, whose caps the Run reserves.
        prompt: The rendered task packet the provider would be started with.
        status_of: The control-reduced status of a Run.
        now: The decision stamp.

    Returns:
        The appended receipt.
    """
    run_ref = capsule.run_ref
    receipt = decide_admission(
        policy.governor,
        request=RunReservation(
            run_ref=run_ref,
            tokens=capsule.budget.tokens,
            cost_microusd=capsule.budget.cost_microusd,
        ),
        in_flight=in_flight_reservations(records, status_of=status_of, excluding=run_ref),
        prompt_budget=evaluate_prompt_budget(
            policy.prompt_budget, rendered_sizes(capsule=capsule, prompt=prompt)
        ),
        decided_at=now,
    )
    commit_ledger_append(
        session,
        LedgerRecord(
            collection=Epoch2Collection.RUN,
            record_key=f"{_RECEIPT_KEY_PREFIX}{run_ref.entity_key}",
            status=receipt.decision.value,
            recorded_at=now,
            payload=receipt.model_dump(mode="json"),
        ),
    )
    logger.info(f"admit_run run={run_ref.entity_key!r} decision={receipt.decision.value}")
    return receipt


def admission_drift(receipt: AdmissionReceipt | None, policy: EconomicsPolicy) -> str | None:
    """Name the policy that changed since *receipt* was decided, or ``None``.

    A Run admitted under one prompt budget or governor and resumed under
    another would run under a policy nobody admitted it with, so the
    change invalidates the attempt rather than applying silently.
    """
    if receipt is None:
        return None
    if receipt.prompt_budget.policy_digest != policy.prompt_budget.policy_digest:
        return "prompt budget"
    if receipt.governor_digest != policy.governor.governor_digest:
        return "governor"
    return None


__all__ = [
    "EconomicsPolicyError",
    "admission_drift",
    "admit_run",
    "in_flight_reservations",
    "latest_receipts",
    "load_economics",
    "rendered_sizes",
]
