"""Unit tests for the Track outcome comparator and the measured-outcome guards.

Covers the keystone :func:`eawf.workflow.evidence.outcome.compute_outcome_status`
(MET / UNMET / REGRESSED for both the higher-is-better ``MAX`` and the
lower-is-better ``MIN`` directions), the ``set_outcome`` derivation (status is
derived from the sample, never hand-set), and the evidence-ref invariants that
forbid a measured outcome from fabricating its own evidence -- both at the
:class:`Outcome` model boundary and at the ``set_outcome`` mutator boundary.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AuditKind,
    AuditVerdict,
    OutcomeDirection,
    OutcomeStatus,
)
from eawf.kernel.state.models import Artifact, Outcome, State
from eawf.surfaces.cli import errors as cli_errors
from eawf.workflow.evidence import _io, audit, outcome
from eawf.workflow.evidence.outcome import OutcomeVerdict, compute_outcome_status

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


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


def _state(tmp_path: Path) -> tuple[Path, State]:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return target, _io.load_state(target)


def _seed_complete_audit(state: State, audit_id: str = "AUD-001") -> None:
    artifacts = dict(state.artifacts)
    artifacts["ART-001"] = Artifact(
        id="ART-001",
        kind="audit_report",
        uri="repo:.ea/artifacts/ART-001.md",
        urn="urn:eawf:v1:artifact:QR/ART-001",
        created_at=datetime.now(UTC),
    )
    state.artifacts = artifacts
    audit.add_audit(
        state,
        audit_id=audit_id,
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001",
        verdict=AuditVerdict.PASS,
    )


def _define(state: State, *, direction: OutcomeDirection, threshold: float) -> None:
    outcome.define_outcome(
        state,
        outcome_id="OUT-001",
        scope_id="QR",
        metric="sharpe",
        threshold=threshold,
        direction=direction,
    )


def test_set_outcome_derives_met(tmp_path: Path) -> None:
    """set_outcome derives MET from a sample that beats the threshold."""
    _, state = _state(tmp_path)
    _define(state, direction=OutcomeDirection.MAX, threshold=1.0)
    _seed_complete_audit(state)
    outcome.set_outcome(
        state,
        outcome_id="OUT-001",
        sample=1.5,
        audit_id="AUD-001",
        evidence_refs=["repo:.ea/artifacts/eval.md"],
    )
    stored = state.outcomes["OUT-001"]
    assert stored.status is OutcomeStatus.MET
    assert stored.sample == pytest.approx(1.5)
    assert stored.best_value == pytest.approx(1.5)


def test_set_outcome_derives_missed(tmp_path: Path) -> None:
    """set_outcome derives MISSED (UNMET) from a sample that fails the threshold."""
    _, state = _state(tmp_path)
    _define(state, direction=OutcomeDirection.MIN, threshold=100.0)
    _seed_complete_audit(state)
    outcome.set_outcome(
        state,
        outcome_id="OUT-001",
        sample=150.0,
        audit_id="AUD-001",
        evidence_refs=["repo:.ea/artifacts/eval.md"],
    )
    assert state.outcomes["OUT-001"].status is OutcomeStatus.MISSED


def test_set_outcome_advances_best_value(tmp_path: Path) -> None:
    """The running best_value advances when a later sample improves on it."""
    _, state = _state(tmp_path)
    _define(state, direction=OutcomeDirection.MAX, threshold=2.0)
    _seed_complete_audit(state)
    outcome.set_outcome(
        state,
        outcome_id="OUT-001",
        sample=0.9,
        audit_id="AUD-001",
        evidence_refs=["repo:.ea/artifacts/eval1.md"],
    )
    assert state.outcomes["OUT-001"].best_value == pytest.approx(0.9)
    # A later worse sample keeps the prior best and reads as a regression.
    outcome.set_outcome(
        state,
        outcome_id="OUT-001",
        sample=0.5,
        audit_id="AUD-001",
        evidence_refs=["repo:.ea/artifacts/eval2.md"],
    )
    assert state.outcomes["OUT-001"].best_value == pytest.approx(0.9)
    assert state.outcomes["OUT-001"].status is OutcomeStatus.MISSED


def test_set_outcome_rejects_missing_evidence(tmp_path: Path) -> None:
    """set_outcome rejects a measurement that cites no evidence ref."""
    _, state = _state(tmp_path)
    _define(state, direction=OutcomeDirection.MAX, threshold=1.0)
    _seed_complete_audit(state)
    with pytest.raises(cli_errors.UserError, match="no evidence ref"):
        outcome.set_outcome(
            state,
            outcome_id="OUT-001",
            sample=1.5,
            audit_id="AUD-001",
            evidence_refs=[],
        )


def test_set_outcome_unknown_outcome_raises(tmp_path: Path) -> None:
    """set_outcome rejects an unknown outcome id."""
    _, state = _state(tmp_path)
    with pytest.raises(cli_errors.UserError, match="OUT-999"):
        outcome.set_outcome(
            state,
            outcome_id="OUT-999",
            sample=1.0,
            audit_id="AUD-001",
            evidence_refs=["repo:.ea/artifacts/eval.md"],
        )
