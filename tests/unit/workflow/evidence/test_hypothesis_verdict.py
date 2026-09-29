"""PLAN-014: a hypothesis verdict is audit-grounded and immutable.

Confirm, reject and inconclusive each land on a complete audit taken after the
hypothesis was stated; an absent, incomplete or stale audit writes no verdict;
and a reversal is refused on the verdicted row, so re-testing means a new row.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import AuditKind, AuditVerdict, HypothesisStatus, HypothesisVerdict
from eawf.kernel.state.models import Artifact, State
from eawf.surfaces.cli.errors import UserError, ValidationError
from eawf.workflow.evidence import _io, audit, hypothesis

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


@pytest.fixture
def state(tmp_path: Path) -> State:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    loaded = _io.load_state(target)
    loaded.artifacts = {
        "ART-001": Artifact(
            id="ART-001",
            kind="audit_report",
            uri="repo:.ea/artifacts/ART-001.md",
            urn="urn:eawf:v1:artifact:QR/ART-001",
            created_at=loaded.updated_at,
        )
    }
    return loaded


def _define(state: State, hypothesis_id: str = "H03-12") -> None:
    hypothesis.define_hypothesis(
        state,
        hypothesis_id=hypothesis_id,
        scope_id="QR",
        text="Replay keeps event order",
        metric="inversions",
        confirm="== 0",
        reject="> 0",
    )


def _audit(state: State, audit_id: str = "AUD-001", *, complete: bool = True) -> None:
    audit.add_audit(
        state,
        audit_id=audit_id,
        scope_id="QR",
        kind=AuditKind.EVALUATION,
        report_artifact_id="ART-001" if complete else None,
        verdict=AuditVerdict.PASS if complete else None,
    )


@pytest.mark.parametrize(
    ("verdict", "status"),
    [
        (HypothesisVerdict.CONFIRMED, HypothesisStatus.CONFIRMED),
        (HypothesisVerdict.REJECTED, HypothesisStatus.REJECTED),
        (HypothesisVerdict.INCONCLUSIVE, HypothesisStatus.INCONCLUSIVE),
    ],
)
def test_plan_014_each_verdict_lands_on_a_complete_audit(
    state: State, verdict: HypothesisVerdict, status: HypothesisStatus
) -> None:
    _define(state)
    _audit(state)
    hypothesis.set_verdict(state, hypothesis_id="H03-12", verdict=verdict, audit_id="AUD-001")
    row = state.hypotheses["H03-12"]
    assert (row.verdict, row.status, row.audit_id) == (verdict, status, "AUD-001")


def test_plan_014_define_stamps_when_the_hypothesis_was_stated(state: State) -> None:
    _define(state)
    assert state.hypotheses["H03-12"].defined_at is not None


def test_plan_014_an_absent_audit_is_denied(state: State) -> None:
    _define(state)
    with pytest.raises(ValidationError, match=r"INV.AUDIT.UNKNOWN"):
        hypothesis.set_verdict(
            state, hypothesis_id="H03-12", verdict=HypothesisVerdict.CONFIRMED, audit_id="AUD-404"
        )
    assert state.hypotheses["H03-12"].verdict is None


def test_plan_014_an_incomplete_audit_is_denied(state: State) -> None:
    _define(state)
    _audit(state, complete=False)
    with pytest.raises(ValidationError, match=r"INV.AUDIT.NOT_COMPLETE"):
        hypothesis.set_verdict(
            state, hypothesis_id="H03-12", verdict=HypothesisVerdict.CONFIRMED, audit_id="AUD-001"
        )


def test_plan_014_a_stale_audit_is_denied(state: State) -> None:
    _audit(state)
    _define(state)
    stated = state.hypotheses["H03-12"].defined_at
    assert stated is not None
    state.audits["AUD-001"] = state.audits["AUD-001"].model_copy(
        update={"created_at": stated - timedelta(seconds=1)}
    )
    with pytest.raises(ValidationError, match=r"INV.AUDIT.STALE"):
        hypothesis.set_verdict(
            state, hypothesis_id="H03-12", verdict=HypothesisVerdict.CONFIRMED, audit_id="AUD-001"
        )
    assert state.hypotheses["H03-12"].status is HypothesisStatus.PENDING


def test_plan_014_an_audit_at_the_statement_instant_is_not_stale(state: State) -> None:
    _define(state)
    _audit(state)
    stated = state.hypotheses["H03-12"].defined_at
    state.audits["AUD-001"] = state.audits["AUD-001"].model_copy(update={"created_at": stated})
    hypothesis.set_verdict(
        state, hypothesis_id="H03-12", verdict=HypothesisVerdict.REJECTED, audit_id="AUD-001"
    )


def test_plan_014_a_hypothesis_stated_before_the_stamp_is_not_dated(state: State) -> None:
    _define(state)
    state.hypotheses["H03-12"] = state.hypotheses["H03-12"].model_copy(update={"defined_at": None})
    _audit(state)
    hypothesis.set_verdict(
        state, hypothesis_id="H03-12", verdict=HypothesisVerdict.CONFIRMED, audit_id="AUD-001"
    )


def test_plan_014_a_verdict_is_immutable_and_reversal_is_a_new_row(state: State) -> None:
    _define(state)
    _audit(state)
    hypothesis.set_verdict(
        state, hypothesis_id="H03-12", verdict=HypothesisVerdict.CONFIRMED, audit_id="AUD-001"
    )
    with pytest.raises(UserError, match="immutable") as caught:
        hypothesis.set_verdict(
            state, hypothesis_id="H03-12", verdict=HypothesisVerdict.REJECTED, audit_id="AUD-001"
        )
    assert caught.value.kind == "InvalidInput"
    assert state.hypotheses["H03-12"].verdict is HypothesisVerdict.CONFIRMED

    _define(state, "H03-13")
    _audit(state, "AUD-002")
    hypothesis.set_verdict(
        state, hypothesis_id="H03-13", verdict=HypothesisVerdict.REJECTED, audit_id="AUD-002"
    )
    assert state.hypotheses["H03-12"].verdict is HypothesisVerdict.CONFIRMED
    assert state.hypotheses["H03-13"].verdict is HypothesisVerdict.REJECTED


def test_plan_014_an_unknown_hypothesis_is_not_found(state: State) -> None:
    with pytest.raises(UserError) as caught:
        hypothesis.set_verdict(
            state, hypothesis_id="H99-99", verdict=HypothesisVerdict.CONFIRMED, audit_id="AUD-001"
        )
    assert caught.value.kind == "NotFound"
