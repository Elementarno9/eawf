"""Tests: RiskTier classifier + auto-close / fork gate (P30-I12-W05 / DL-5).

Covers the typed criteria C1..C3 of the DL-5 wave:

* C1: :func:`classify_risk_tier` assigns a deterministic-only wave
  :attr:`RiskTier.MECH`, an auditor-gated wave :attr:`RiskTier.MED`, and a
  jury / UI-band wave :attr:`RiskTier.HIGH` / :attr:`RiskTier.UI`.
* C2 (the LOAD-BEARING SAFETY INVARIANT): :func:`risk_tier_auto_closes` NEVER
  lets a high / ui wave auto-close while the jury authority is
  :attr:`BlockAuthority.ADVISORY` (unearned); once
  :attr:`BlockAuthority.BLOCKING` is granted, a high-tier wave auto-closes via
  the jury path.
* C3: :class:`RiskTier` is a closed StrEnum ``{mech, med, high, ui}``
  (extra-free, distinct from :class:`OracleTier`); the classifier is a PURE
  function of the gate kinds (no IO, no mutation, an empty gate set classifies
  MECH, and the same input always yields the same output).
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.kernel.spec.common import GateSpec, OracleTier
from eawf.kernel.state.enums import RiskTier
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.workflow.verify.oracle import classify_risk_tier, risk_tier_auto_closes


def _gate(gate_id: str, kind: str) -> GateSpec:
    """Build a minimal valid :class:`GateSpec` of the given *kind*.

    Argv-bearing kinds (``command_exit_zero``) carry an allowlisted ``argv`` so
    the GateSpec L0-policy validator accepts the row; every other kind needs no
    args.
    """
    args: dict[str, Any] = {}
    if kind == "command_exit_zero":
        args = {"argv": ["pytest", "-q"]}
    return GateSpec(
        id=gate_id,
        criterion_id="CR-01",
        kind=kind,
        args=args,
        policy="block",
        cadence="every-wave",
    )


# --- C1: the classifier assigns mech / med / high / ui --------------------


def test_classify_deterministic_only_is_mech() -> None:
    """C1: a wave whose every gate is a deterministic falsifier -> MECH."""
    gates = [
        _gate("G1", "file_exists"),
        _gate("G2", "regex_in_file"),
        _gate("G3", "command_exit_zero"),
        _gate("G4", "schema_validate"),
    ]
    assert classify_risk_tier(gates) is RiskTier.MECH


def test_classify_auditor_gated_is_med() -> None:
    """C1: a wave carrying a human-approval (auditor) gate -> MED."""
    gates = [_gate("G1", "file_exists"), _gate("G2", "auditor_verdict")]
    assert classify_risk_tier(gates) is RiskTier.MED


def test_classify_human_approval_gate_is_med() -> None:
    """C1: the ``human_approval`` gate kind also classifies MED."""
    assert classify_risk_tier([_gate("G1", "human_approval")]) is RiskTier.MED


def test_classify_jury_gated_is_high() -> None:
    """C1: a wave carrying a non-UI jury gate -> HIGH."""
    assert classify_risk_tier([_gate("G1", "jury_verdict")]) is RiskTier.HIGH
    assert classify_risk_tier([_gate("G1", "cross_vendor_jury")]) is RiskTier.HIGH


@pytest.mark.parametrize(
    "ui_kind",
    [
        "tui_flow",
        "svg_well_formed",
        "svg_pixel_diff",
        "mockup_golden_diff",
        "affordance_parity",
        "transition_coverage",
    ],
)
def test_classify_ui_band_gate_is_ui(ui_kind: str) -> None:
    """C1: a wave carrying any UI / visual-band gate -> UI."""
    assert classify_risk_tier([_gate("G1", ui_kind)]) is RiskTier.UI


def test_classify_takes_riskiest_gate_not_cheapest() -> None:
    """C1: a wave mixing a deterministic gate with a jury / ui gate is classified
    by its RISKIEST gate, never its cheapest -- the deterministic floor never
    masks an unmet jury / ui requirement.
    """
    # deterministic + jury -> HIGH (the jury wins over the cheap gate).
    assert (
        classify_risk_tier([_gate("G1", "file_exists"), _gate("G2", "jury_verdict")])
        is RiskTier.HIGH
    )
    # jury + ui -> UI (the UI band outranks the bare jury).
    assert (
        classify_risk_tier([_gate("G1", "jury_verdict"), _gate("G2", "svg_pixel_diff")])
        is RiskTier.UI
    )
    # auditor + jury -> HIGH (jury outranks auditor).
    assert (
        classify_risk_tier([_gate("G1", "auditor_verdict"), _gate("G2", "jury_verdict")])
        is RiskTier.HIGH
    )


# --- C2: the high / ui fork safety invariant ------------------------------


def test_high_never_auto_closes_under_advisory() -> None:
    """C2 (LOAD-BEARING): a HIGH wave NEVER auto-closes while the jury authority
    is advisory (unearned) -- it always forks.
    """
    assert risk_tier_auto_closes(RiskTier.HIGH, block_authority=BlockAuthority.ADVISORY) is False


def test_ui_never_auto_closes_under_advisory() -> None:
    """C2 (LOAD-BEARING): a UI wave NEVER auto-closes under advisory authority."""
    assert risk_tier_auto_closes(RiskTier.UI, block_authority=BlockAuthority.ADVISORY) is False


def test_high_auto_closes_once_blocking_granted() -> None:
    """C2: once blocking authority is earned, a HIGH wave auto-closes via the
    jury path.
    """
    assert risk_tier_auto_closes(RiskTier.HIGH, block_authority=BlockAuthority.BLOCKING) is True


def test_ui_auto_closes_once_blocking_granted() -> None:
    """C2: a UI wave auto-closes once blocking authority is earned."""
    assert risk_tier_auto_closes(RiskTier.UI, block_authority=BlockAuthority.BLOCKING) is True


def test_mech_and_med_always_auto_close() -> None:
    """C2: MECH (deterministic pass) and MED (auditor verdict) always auto-close,
    regardless of jury authority -- they need no jury sign-off.
    """
    for authority in (BlockAuthority.ADVISORY, BlockAuthority.BLOCKING):
        assert risk_tier_auto_closes(RiskTier.MECH, block_authority=authority) is True
        assert risk_tier_auto_closes(RiskTier.MED, block_authority=authority) is True


# --- C3: closed StrEnum, distinct from OracleTier, pure classifier --------


def test_risk_tier_is_closed_strenum() -> None:
    """C3: RiskTier is the closed StrEnum {mech, med, high, ui}."""
    assert {t.value for t in RiskTier} == {"mech", "med", "high", "ui"}
    with pytest.raises(ValueError, match="not a valid"):
        RiskTier("auditor")


def test_risk_tier_is_distinct_from_oracle_tier() -> None:
    """C3: RiskTier {mech,med,high,ui} shares no value with OracleTier T1..T7."""
    risk_values = {t.value for t in RiskTier}
    oracle_values = {str(int(t)) for t in OracleTier} | {t.name for t in OracleTier}
    assert risk_values.isdisjoint(oracle_values)


def test_classify_empty_gate_set_is_mech() -> None:
    """C3 (boundary): an empty gate set classifies MECH -- a wave with no gate
    has nothing needing human judgement, so the least-risk band.
    """
    assert classify_risk_tier([]) is RiskTier.MECH


def test_classify_is_pure_no_mutation() -> None:
    """C3: the classifier mutates neither its input nor any shared state -- the
    same input yields the same output and the gate list is untouched.
    """
    gates = [_gate("G1", "file_exists"), _gate("G2", "jury_verdict")]
    before = [g.model_dump() for g in gates]
    first = classify_risk_tier(gates)
    second = classify_risk_tier(gates)
    assert first is second is RiskTier.HIGH
    assert [g.model_dump() for g in gates] == before
