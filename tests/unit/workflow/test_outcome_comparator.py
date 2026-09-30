"""Unit tests for the Track outcome comparator and the measured-outcome guards.

Covers the keystone :func:`eawf.workflow.evidence.outcome.compute_outcome_status`
(MET / UNMET / REGRESSED for both the higher-is-better ``MAX`` and the
lower-is-better ``MIN`` directions), the ``set_outcome`` derivation (status is
derived from the sample, never hand-set), and the evidence-ref invariants that
forbid a measured outcome from fabricating its own evidence -- both at the
:class:`Outcome` model boundary and at the ``set_outcome`` mutator boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    OutcomeDirection,
    OutcomeStatus,
)
from eawf.kernel.state.models import Outcome
from eawf.workflow.evidence.outcome import OutcomeVerdict, compute_outcome_status

# --- compute_outcome_status (C1) --------------------------------------------


def test_compute_status_max_met() -> None:
    """Higher-is-better: a sample at or above the threshold is MET."""
    assert (
        compute_outcome_status(threshold=1.0, sample=1.5, direction=OutcomeDirection.MAX)
        is OutcomeVerdict.MET
    )


def test_compute_status_max_unmet_no_prior_best() -> None:
    """Higher-is-better: a sample below threshold with no prior best is UNMET."""
    assert (
        compute_outcome_status(threshold=1.0, sample=0.5, direction=OutcomeDirection.MAX)
        is OutcomeVerdict.UNMET
    )


def test_compute_status_max_regressed_below_prior_best() -> None:
    """Higher-is-better: a sub-threshold sample strictly below the prior best regressed."""
    assert (
        compute_outcome_status(
            threshold=1.0,
            sample=0.5,
            direction=OutcomeDirection.MAX,
            best_value=0.9,
        )
        is OutcomeVerdict.REGRESSED
    )


def test_compute_status_max_unmet_holds_prior_best() -> None:
    """Higher-is-better: a sub-threshold sample equal to the prior best is UNMET, not regressed."""
    assert (
        compute_outcome_status(
            threshold=1.0,
            sample=0.9,
            direction=OutcomeDirection.MAX,
            best_value=0.9,
        )
        is OutcomeVerdict.UNMET
    )


def test_compute_status_min_met() -> None:
    """Lower-is-better: a sample at or below the threshold is MET."""
    assert (
        compute_outcome_status(threshold=100.0, sample=80.0, direction=OutcomeDirection.MIN)
        is OutcomeVerdict.MET
    )


def test_compute_status_min_unmet_no_prior_best() -> None:
    """Lower-is-better: a sample above threshold with no prior best is UNMET."""
    assert (
        compute_outcome_status(threshold=100.0, sample=150.0, direction=OutcomeDirection.MIN)
        is OutcomeVerdict.UNMET
    )


def test_compute_status_min_regressed_above_prior_best() -> None:
    """Lower-is-better: an over-threshold sample strictly above the prior best regressed."""
    assert (
        compute_outcome_status(
            threshold=100.0,
            sample=150.0,
            direction=OutcomeDirection.MIN,
            best_value=120.0,
        )
        is OutcomeVerdict.REGRESSED
    )


def test_compute_status_rejects_non_comparable_direction() -> None:
    """EQUAL / RANGE directions are not derivable by the comparator."""
    with pytest.raises(ValueError, match="non-comparable outcome direction"):
        compute_outcome_status(
            threshold=1.0,
            sample=1.0,
            direction=OutcomeDirection.EQUAL,
        )


# --- Outcome model evidence-ref invariant (C2) ------------------------------


def _measured_outcome_kwargs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "OUT-001",
        "scope_id": "QR",
        "metric": "sharpe",
        "threshold": 1.0,
        "direction": OutcomeDirection.MAX,
        "value": 1.5,
        "sample": 1.5,
        "best_value": 1.5,
        "status": OutcomeStatus.MET,
        "audit_id": "AUD-001",
        "evidence_refs": ["repo:.ea/artifacts/eval.md"],
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return base


def test_outcome_measured_without_evidence_rejected() -> None:
    """A measured outcome (sample + terminal status) with no evidence ref is rejected."""
    with pytest.raises(ValidationError, match="no resolving evidence ref"):
        Outcome(**_measured_outcome_kwargs(evidence_refs=[]))


def test_outcome_measured_with_evidence_accepted() -> None:
    """A measured outcome that cites an evidence ref validates."""
    out = Outcome(**_measured_outcome_kwargs())
    assert out.evidence_refs == ["repo:.ea/artifacts/eval.md"]
    assert out.best_value == pytest.approx(1.5)


def test_outcome_pending_without_evidence_accepted() -> None:
    """A pending (unmeasured) outcome needs no evidence ref."""
    out = Outcome(
        **_measured_outcome_kwargs(
            status=OutcomeStatus.PENDING,
            sample=None,
            value=None,
            best_value=None,
            evidence_refs=[],
            audit_id=None,
        )
    )
    assert out.status is OutcomeStatus.PENDING


# --- set_outcome derivation + evidence rejection (C1 + C2) -------------------
