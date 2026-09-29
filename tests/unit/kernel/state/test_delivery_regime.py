"""PLAN-019 and PLAN-006: the three delivery regimes and the debt fast work owes.

PLAN-019: exactly three values, no promoted value, experimental promotion
mints a successor, and fast can never defer an always-required gate.
PLAN-006: a fast binding is bounded by quota and expiry, its deferred gates
become debt, and open debt blocks stable Release approval until the gate
runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.epoch2.regime import (
    ALWAYS_REQUIRED,
    DeliveryRegime,
    GateClass,
    RegimeBinding,
    RegimeError,
    VerificationDebt,
    VerificationDebtStatus,
    admit_fast_binding,
    stable_release_blockers,
)

pytestmark = pytest.mark.unit

SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
MILESTONE = f"{SLOT}/milestone/MLS-0030"
OTHER_MILESTONE = f"{SLOT}/milestone/MLS-0031"
EVIDENCE = f"{SLOT}/evidence/EVD-0001"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _binding(regime: str, **overrides: Any) -> RegimeBinding:
    fields: dict[str, Any] = {
        "regime": regime,
        "scope_ref": MILESTONE,
        "policy_revision": 1,
        "effective_at": T0,
    }
    if regime == "fast":
        fields |= {"incident_ref": "INC-001", "expires_at": T0 + timedelta(days=2)}
    if regime == "experimental":
        fields["hypothesis_ref"] = "H01-01"
    fields.update(overrides)
    return RegimeBinding.model_validate(fields)


def _debt(gate: GateClass = GateClass.REVIEW, **overrides: Any) -> VerificationDebt:
    fields: dict[str, Any] = {
        "key": "VDT-0001",
        "scope_ref": MILESTONE,
        "incident_ref": "INC-001",
        "deferred_gate": gate,
        "opened_at": T0,
    }
    fields.update(overrides)
    return VerificationDebt.model_validate(fields)


# ---- PLAN-019: the enum and its bindings ---------------------------------------


def test_plan_019_delivery_regime_has_exactly_three_values() -> None:
    assert {r.value for r in DeliveryRegime} == {"steady", "fast", "experimental"}


@pytest.mark.parametrize("value", ["promoted", "hotfix", "", "STEADY"])
def test_plan_019_unknown_or_promoted_value_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        RegimeBinding.model_validate(
            {"regime": value, "scope_ref": MILESTONE, "policy_revision": 1, "effective_at": T0}
        )


def test_plan_019_every_regime_binds_with_its_own_reference() -> None:
    assert _binding("steady").incident_ref is None
    assert _binding("fast").incident_ref == "INC-001"
    assert _binding("experimental").hypothesis_ref == "H01-01"


@pytest.mark.parametrize(
    ("regime", "overrides"),
    [
        ("fast", {"incident_ref": None}),
        ("fast", {"expires_at": None}),
        ("fast", {"expires_at": T0}),
        ("experimental", {"hypothesis_ref": None}),
        ("steady", {"incident_ref": "INC-001"}),
        ("steady", {"hypothesis_ref": "H01-01"}),
        ("steady", {"verification_debt_refs": ("VDT-0001",)}),
        ("experimental", {"verification_debt_refs": ("VDT-0001",)}),
    ],
)
def test_plan_019_a_reference_its_regime_does_not_admit_is_refused(
    regime: str, overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        _binding(regime, **overrides)


def test_plan_019_a_binding_is_immutable() -> None:
    binding = _binding("steady")
    with pytest.raises(ValidationError):
        binding.regime = DeliveryRegime.FAST


def _successor(
    regime: str, prior: str, *, audit: str | None = None, prior_revision: int = 1
) -> RegimeBinding:
    succeeds = {
        "prior_regime": prior,
        "prior_policy_revision": prior_revision,
        "promotion_audit_ref": audit,
    }
    return _binding(regime, policy_revision=2, succeeds=succeeds)


@pytest.mark.parametrize("successor", ["steady", "fast"])
def test_plan_019_experimental_promotion_creates_a_successor(successor: str) -> None:
    prior = _binding("experimental")
    new = _successor(successor, "experimental", audit="AUD-001")
    assert new.succeeds is not None
    assert new.succeeds.promotion_audit_ref == "AUD-001"
    assert prior.regime is DeliveryRegime.EXPERIMENTAL


def test_plan_019_experimental_promotion_needs_the_audit_of_its_verdict() -> None:
    with pytest.raises(ValidationError, match="promotion_audit_ref"):
        _successor("steady", "experimental")


def test_plan_019_only_an_experimental_prior_names_a_promotion_audit() -> None:
    with pytest.raises(ValidationError, match="promotion_audit_ref"):
        _successor("steady", "fast", audit="AUD-001")


@pytest.mark.parametrize(
    ("prior", "successor"),
    [("fast", "experimental"), ("steady", "fast"), ("steady", "experimental"), ("fast", "fast")],
)
def test_plan_019_loosening_or_sideways_moves_are_illegal(prior: str, successor: str) -> None:
    with pytest.raises(ValidationError, match="cannot be re-bound"):
        _successor(successor, prior)


def test_plan_019_experimental_cannot_be_re_bound_as_experimental() -> None:
    with pytest.raises(ValidationError, match="cannot be re-bound"):
        _successor("experimental", "experimental", audit="AUD-001")


def test_plan_019_fast_to_steady_and_stricter_steady_are_legal() -> None:
    assert _successor("steady", "fast").regime is DeliveryRegime.STEADY
    assert _successor("steady", "steady").regime is DeliveryRegime.STEADY


@pytest.mark.parametrize("prior_revision", [2, 3])
def test_plan_019_a_successor_at_or_before_its_prior_revision_is_stale(
    prior_revision: int,
) -> None:
    with pytest.raises(ValidationError, match=r"later|after"):
        _successor("steady", "fast", prior_revision=prior_revision)


def test_plan_019_a_first_binding_names_no_prior() -> None:
    assert _binding("steady").succeeds is None


@pytest.mark.parametrize("gate", sorted(ALWAYS_REQUIRED[DeliveryRegime.FAST]))
def test_plan_019_fast_cannot_defer_an_always_required_gate(gate: GateClass) -> None:
    with pytest.raises(ValidationError, match="always required"):
        _debt(gate)


def test_plan_019_fast_keeps_security_and_migration() -> None:
    required = ALWAYS_REQUIRED[DeliveryRegime.FAST]
    assert {GateClass.SECURITY, GateClass.MIGRATION_ROLLBACK} <= required
    assert ALWAYS_REQUIRED[DeliveryRegime.STEADY] == frozenset(GateClass)


# ---- PLAN-006: quota, expiry, debt and approval --------------------------------


def test_plan_006_fast_incident_defers_a_ceremony_gate_as_open_debt() -> None:
    debt = _debt(GateClass.REVIEW)
    binding = _binding("fast", verification_debt_refs=(debt.key,))
    assert debt.status is VerificationDebtStatus.OPEN
    assert binding.verification_debt_refs == ("VDT-0001",)


def test_plan_006_quota_admits_up_to_its_bound() -> None:
    in_force = [_binding("fast", scope_ref=OTHER_MILESTONE)]
    admit_fast_binding(_binding("fast"), in_force=in_force, quota=2, at=T0)


def test_plan_006_quota_refuses_one_past_its_bound() -> None:
    in_force = [_binding("fast", scope_ref=OTHER_MILESTONE), _binding("fast")]
    with pytest.raises(RegimeError) as caught:
        admit_fast_binding(_binding("fast"), in_force=in_force, quota=2, at=T0)
    assert caught.value.code == "fast_regime_quota_exhausted"


def test_plan_006_a_lapsed_fast_binding_does_not_count_against_the_quota() -> None:
    lapsed = _binding("fast", expires_at=T0 + timedelta(hours=1))
    admit_fast_binding(_binding("fast"), in_force=[lapsed], quota=1, at=T0 + timedelta(hours=1))


def test_plan_006_an_expired_fast_binding_is_refused() -> None:
    with pytest.raises(RegimeError) as caught:
        admit_fast_binding(_binding("fast"), in_force=[], quota=1, at=T0 + timedelta(days=2))
    assert caught.value.code == "fast_regime_expired"


def test_plan_006_quota_applies_to_fast_only() -> None:
    with pytest.raises(RegimeError) as caught:
        admit_fast_binding(_binding("steady"), in_force=[], quota=1, at=T0)
    assert caught.value.code == "regime_not_fast"


def test_plan_006_zero_quota_admits_nothing() -> None:
    with pytest.raises(RegimeError):
        admit_fast_binding(_binding("fast"), in_force=[], quota=0, at=T0)


def test_plan_006_open_debt_blocks_stable_release_approval() -> None:
    assert stable_release_blockers([_debt()]) == ("VDT-0001",)


def test_plan_006_no_debt_blocks_nothing() -> None:
    assert stable_release_blockers([]) == ()


def test_plan_006_discharged_debt_unblocks_approval() -> None:
    debt = _debt().discharge(
        head="a" * 40, evidence_ref=parse_qualified_urn(EVIDENCE), at=T0 + timedelta(hours=3)
    )
    assert debt.status is VerificationDebtStatus.DISCHARGED
    assert stable_release_blockers([debt]) == ()


def test_plan_006_cancelled_debt_unblocks_approval() -> None:
    debt = _debt().cancel(reason="the hotfix was superseded", at=T0 + timedelta(hours=3))
    assert stable_release_blockers([debt]) == ()


def test_plan_006_a_closed_debt_cannot_be_discharged_again() -> None:
    debt = _debt().cancel(reason="the hotfix was superseded", at=T0)
    with pytest.raises(RegimeError) as caught:
        debt.discharge(head="a" * 40, evidence_ref=parse_qualified_urn(EVIDENCE), at=T0)
    assert caught.value.code == "verification_debt_closed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "DISCHARGED", "closed_at": T0},
        {"discharged_head": "a" * 40},
        {"status": "CANCELLED", "closed_at": T0},
        {"cancelled_reason": "moot"},
        {"closed_at": T0},
        {"key": "VDT-1"},
    ],
)
def test_plan_006_debt_closure_fields_match_status(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _debt(**overrides)
