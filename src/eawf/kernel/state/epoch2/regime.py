"""DeliveryRegime: one assurance-policy axis, and the debt a fast regime owes.

A regime is a property of a unit of delivery declared at plan time, not of
a session. It has exactly three values. ``steady`` is ordinary delivery
with every gate; ``fast`` is an Incident or hotfix path that may defer
ceremony but never safety; ``experimental`` is outcome-uncertain work tied
to a Hypothesis that never integrates or ships. There is no ``promoted``
value: promoting experimental work mints a successor binding in another
regime, and the experimental binding stays what it was.

What a regime may never do is weaken a gate its own column marks always
required. The only row a regime mints is :class:`VerificationDebt`, one
per deferred gate, and a debt cannot name an always-required gate, so no
path through this module skips security, authority, data integrity,
migration or rollback, exact-head integration, protected approval or
release truth. An open debt blocks stable Release approval and is
discharged only by running the gate it deferred, never by re-declaring the
regime.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Self

from pydantic import ConfigDict, StringConstraints, model_validator

from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    ShaStr,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.urns import BatchUrn, EvidenceUrn, MilestoneUrn
from eawf.kernel.state.models import HypothesisIdStr, IdStr
from eawf.kernel.state.types import UtcDatetime

#: A ``VDT-####`` verification-debt key. Local grammar: a debt is a row a
#: binding mints, not an addressable entity with a URN of its own.
DebtKey = Annotated[str, StringConstraints(strict=True, pattern=r"^VDT-\d{4,}$")]


class DeliveryRegime(StrEnum):
    """The three assurance policies a unit of delivery can be bound to."""

    STEADY = "steady"
    FAST = "fast"
    EXPERIMENTAL = "experimental"


class GateClass(StrEnum):
    """The gate families the regime table names, by what they protect."""

    DETERMINISTIC = "deterministic"
    REVIEW = "review"
    ACCEPTANCE = "acceptance"
    SECURITY = "security"
    AUTHORITY = "authority"
    DATA_INTEGRITY = "data_integrity"
    MIGRATION_ROLLBACK = "migration_rollback"
    EXACT_HEAD_INTEGRATION = "exact_head_integration"
    PROTECTED_APPROVAL = "protected_approval"
    RELEASE_TRUTH = "release_truth"
    SANDBOX = "sandbox"
    BUDGET = "budget"
    EVIDENCE_CAPTURE = "evidence_capture"
    CLEANUP_RETENTION = "cleanup_retention"
    NO_PUBLICATION = "no_publication"


#: The gates each regime may never defer. ``steady`` runs every applicable
#: gate, so its set is the whole vocabulary; ``fast`` narrows ceremony and
#: keeps every safety gate; ``experimental`` keeps the gates that hold an
#: isolated experiment isolated.
ALWAYS_REQUIRED: Final[Mapping[DeliveryRegime, frozenset[GateClass]]] = MappingProxyType(
    {
        DeliveryRegime.STEADY: frozenset(GateClass),
        DeliveryRegime.FAST: frozenset(
            {
                GateClass.SECURITY,
                GateClass.AUTHORITY,
                GateClass.DATA_INTEGRITY,
                GateClass.MIGRATION_ROLLBACK,
                GateClass.EXACT_HEAD_INTEGRATION,
                GateClass.PROTECTED_APPROVAL,
                GateClass.RELEASE_TRUTH,
            }
        ),
        DeliveryRegime.EXPERIMENTAL: frozenset(
            {
                GateClass.SANDBOX,
                GateClass.AUTHORITY,
                GateClass.BUDGET,
                GateClass.EVIDENCE_CAPTURE,
                GateClass.CLEANUP_RETENTION,
                GateClass.NO_PUBLICATION,
            }
        ),
    }
)

#: Each regime's legal successors. Loosening active work is never an
#: edge: a steady unit that needs a fast path gets new scoped work under
#: an Incident, and fast work that turns out uncertain gets a new
#: Hypothesis scope rather than an experimental re-binding.
_SUCCESSORS: Final[Mapping[DeliveryRegime, frozenset[DeliveryRegime]]] = MappingProxyType(
    {
        DeliveryRegime.EXPERIMENTAL: frozenset({DeliveryRegime.FAST, DeliveryRegime.STEADY}),
        DeliveryRegime.FAST: frozenset({DeliveryRegime.STEADY}),
        DeliveryRegime.STEADY: frozenset({DeliveryRegime.STEADY}),
    }
)


class RegimeError(ValueError):
    """A fast-binding admission or a debt move was refused.

    Attributes:
        code: The stable refusal code a caller branches on.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RegimeSuccession(Epoch2Model):
    """The binding a successor replaces, recorded on the successor itself.

    Attributes:
        prior_regime: The regime the work was bound under before.
        prior_policy_revision: The policy revision of that binding.
        promotion_audit_ref: The audit of the confirmed Hypothesis verdict that
            promoted experimental work; required exactly when the prior was
            experimental, since that verdict is the promotion's only authority.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    prior_regime: DeliveryRegime
    prior_policy_revision: StrictPositiveInt
    promotion_audit_ref: IdStr | None = None


class RegimeBinding(Epoch2Model):
    """The regime one Milestone or Batch is delivered under, at one policy revision.

    A binding is immutable: changing the regime is a successor binding at a
    later policy revision that names what it succeeds, and the regime table's
    edges are enforced on that record.

    Attributes:
        regime: The assurance policy.
        scope_ref: The Milestone or Batch the binding governs.
        incident_ref: The Incident a fast binding answers; required for fast only.
        hypothesis_ref: The Hypothesis experimental work tests; required for
            experimental only.
        policy_revision: The policy revision the binding was made under.
        effective_at: When the binding took effect.
        expires_at: When a fast binding lapses; required for fast only,
            because an unbounded hotfix path is steady delivery without its gates.
        verification_debt_refs: The debts this binding minted for the gates it
            deferred; only a fast binding defers a gate.
        succeeds: The binding this one replaces, when it is a successor.

    Raises:
        pydantic.ValidationError: An unknown regime value, a reference its
            regime does not admit, a fast window that ends before it starts, or
            a succession the regime table does not draw.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    regime: DeliveryRegime
    scope_ref: MilestoneUrn | BatchUrn
    incident_ref: IdStr | None = None
    hypothesis_ref: HypothesisIdStr | None = None
    policy_revision: StrictPositiveInt
    effective_at: UtcDatetime
    expires_at: UtcDatetime | None = None
    verification_debt_refs: tuple[DebtKey, ...] = ()
    succeeds: RegimeSuccession | None = None

    @model_validator(mode="after")
    def _references_match_regime(self) -> Self:
        """Require exactly the references this binding's regime admits.

        Raises:
            ValueError: A fast binding without its Incident or expiry, an
                experimental one without its Hypothesis, or a reference
                carried by a regime that does not admit it.
        """
        fast = self.regime is DeliveryRegime.FAST
        experimental = self.regime is DeliveryRegime.EXPERIMENTAL
        if fast != (self.incident_ref is not None):
            raise ValueError(f"incident_ref is required for fast only, not {self.regime.value}")
        if fast != (self.expires_at is not None):
            raise ValueError(f"expires_at is required for fast only, not {self.regime.value}")
        if experimental != (self.hypothesis_ref is not None):
            raise ValueError(
                f"hypothesis_ref is required for experimental only, not {self.regime.value}"
            )
        if self.verification_debt_refs and not fast:
            raise ValueError(f"a {self.regime.value} binding defers no gate and owes no debt")
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("a fast binding must expire after it takes effect")
        return self

    @model_validator(mode="after")
    def _succession_is_an_edge_of_the_table(self) -> Self:
        """Refuse a successor the regime table does not draw.

        Raises:
            ValueError: The successor loosens or sidesteps its prior's regime,
                is not at a later policy revision, or promotes experimental
                work without the audit of its verdict (or names one when the
                prior was not experimental).
        """
        prior = self.succeeds
        if prior is None:
            return self
        if self.regime not in _SUCCESSORS[prior.prior_regime]:
            raise ValueError(
                f"{prior.prior_regime.value} work cannot be re-bound as {self.regime.value}; "
                "loosening needs new scoped work under an Incident or a Hypothesis"
            )
        if self.policy_revision <= prior.prior_policy_revision:
            raise ValueError(
                f"a successor needs a policy revision after {prior.prior_policy_revision}, "
                f"got {self.policy_revision}"
            )
        promoted = prior.prior_regime is DeliveryRegime.EXPERIMENTAL
        if promoted != (prior.promotion_audit_ref is not None):
            raise ValueError(
                "promotion_audit_ref is required exactly when experimental work is promoted"
            )
        return self


def _fast_in_force(bindings: Iterable[RegimeBinding], *, at: UtcDatetime) -> int:
    """Return how many fast bindings are effective at *at* and not yet expired."""
    return sum(
        1
        for b in bindings
        if b.regime is DeliveryRegime.FAST
        and b.effective_at <= at
        and b.expires_at is not None
        and at < b.expires_at
    )


def admit_fast_binding(
    binding: RegimeBinding,
    *,
    in_force: Iterable[RegimeBinding],
    quota: int,
    at: UtcDatetime,
) -> None:
    """Refuse a fast *binding* that would exceed the quota or is already lapsed.

    Args:
        binding: The fast binding being admitted.
        in_force: Every binding already recorded in the workspace.
        quota: How many fast bindings may be in force at once.
        at: When the binding is being admitted.

    Raises:
        RegimeError: ``regime_not_fast`` when *binding* is not fast;
            ``fast_regime_expired`` when it has already lapsed at *at*;
            ``fast_regime_quota_exhausted`` when *quota* fast bindings are
            already in force.
    """
    if binding.regime is not DeliveryRegime.FAST or binding.expires_at is None:
        raise RegimeError("regime_not_fast", f"a {binding.regime.value} binding has no fast quota")
    if binding.expires_at <= at:
        raise RegimeError("fast_regime_expired", f"the fast window closed at {binding.expires_at}")
    used = _fast_in_force(in_force, at=at)
    if used >= quota:
        raise RegimeError(
            "fast_regime_quota_exhausted",
            f"{used} of {quota} fast bindings are already in force",
        )


class VerificationDebtStatus(StrEnum):
    """The lifecycle of one deferred gate."""

    OPEN = "OPEN"
    DISCHARGED = "DISCHARGED"
    CANCELLED = "CANCELLED"


class VerificationDebt(Epoch2Model):
    """The gate a fast binding deferred, owed until it runs and passes.

    Attributes:
        key: The debt's ``VDT-####`` key.
        scope_ref: The Milestone or Batch whose fast binding deferred the gate.
        incident_ref: The Incident the fast binding answers.
        deferred_gate: The gate that was deferred; never one fast keeps.
        status: Where the debt is in its lifecycle.
        opened_at: When the gate was deferred.
        discharged_head: The exact head the deferred gate passed at.
        discharge_evidence_ref: The evidence of that pass.
        cancelled_reason: Why the covered work was cancelled or superseded.
        closed_at: When the debt left ``OPEN``.

    Raises:
        pydantic.ValidationError: The deferred gate is one fast may never
            defer, or the closing fields disagree with the status.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: DebtKey
    scope_ref: MilestoneUrn | BatchUrn
    incident_ref: IdStr
    deferred_gate: GateClass
    status: VerificationDebtStatus = VerificationDebtStatus.OPEN
    opened_at: UtcDatetime
    discharged_head: ShaStr | None = None
    discharge_evidence_ref: EvidenceUrn | None = None
    cancelled_reason: NonEmptyStr | None = None
    closed_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _gate_is_deferrable_and_closure_matches(self) -> Self:
        """Refuse a debt for a safety gate, and keep the closing fields honest.

        Raises:
            ValueError: The gate is always required under fast, or the
                discharge, cancellation and closing fields disagree with
                the status.
        """
        if self.deferred_gate in ALWAYS_REQUIRED[DeliveryRegime.FAST]:
            raise ValueError(
                f"fast cannot defer the {self.deferred_gate.value} gate; it is always required"
            )
        discharged = self.status is VerificationDebtStatus.DISCHARGED
        cancelled = self.status is VerificationDebtStatus.CANCELLED
        if discharged != (
            self.discharged_head is not None and self.discharge_evidence_ref is not None
        ):
            raise ValueError("a discharged debt names the head and evidence of the passing gate")
        if not discharged and (
            self.discharged_head is not None or self.discharge_evidence_ref is not None
        ):
            raise ValueError(f"an {self.status.value} debt carries no discharge")
        if cancelled != (self.cancelled_reason is not None):
            raise ValueError("cancelled_reason is set exactly when the debt is cancelled")
        if (self.status is VerificationDebtStatus.OPEN) != (self.closed_at is None):
            raise ValueError("closed_at is set exactly when the debt has left OPEN")
        return self

    def discharge(self, *, head: str, evidence_ref: EvidenceUrn, at: UtcDatetime) -> Self:
        """Return this debt discharged by the deferred gate passing at *head*.

        Args:
            head: The exact head the gate passed at.
            evidence_ref: The evidence of the passing run.
            at: When it passed.

        Returns:
            The successor record at ``DISCHARGED``.

        Raises:
            RegimeError: ``verification_debt_closed`` when the debt is not open.
        """
        self._require_open()
        return self.model_validate(
            {
                **self.model_dump(),
                "status": VerificationDebtStatus.DISCHARGED,
                "discharged_head": head,
                "discharge_evidence_ref": evidence_ref,
                "closed_at": at,
            }
        )

    def cancel(self, *, reason: str, at: UtcDatetime) -> Self:
        """Return this debt cancelled because the work it covers was.

        Args:
            reason: Why the covered work was cancelled or superseded.
            at: When.

        Returns:
            The successor record at ``CANCELLED``.

        Raises:
            RegimeError: ``verification_debt_closed`` when the debt is not open.
        """
        self._require_open()
        return self.model_validate(
            {
                **self.model_dump(),
                "status": VerificationDebtStatus.CANCELLED,
                "cancelled_reason": reason,
                "closed_at": at,
            }
        )

    def _require_open(self) -> None:
        if self.status is not VerificationDebtStatus.OPEN:
            raise RegimeError(
                "verification_debt_closed", f"{self.key} is {self.status.value}, not OPEN"
            )


def stable_release_blockers(debts: Iterable[VerificationDebt]) -> tuple[str, ...]:
    """Return the keys of every open debt, each of which blocks stable Release approval.

    Args:
        debts: The debts recorded against the Release's membership.

    Returns:
        The open debts' keys in the order given; empty when approval may proceed.
    """
    return tuple(d.key for d in debts if d.status is VerificationDebtStatus.OPEN)


__all__ = [
    "ALWAYS_REQUIRED",
    "DebtKey",
    "DeliveryRegime",
    "GateClass",
    "RegimeBinding",
    "RegimeError",
    "RegimeSuccession",
    "VerificationDebt",
    "VerificationDebtStatus",
    "admit_fast_binding",
    "stable_release_blockers",
]
