"""Lens 9: policy/constitution.

Scoped to the one policy statement :class:`~eawf.kernel.spec.common.CriterionSpec`
lets a pure plan-body function check today: a ``required`` criterion's
gate is not the one a plan is allowed to waive. See
:mod:`eawf.workflow.planning.lenses.governance` for why the fuller
active-Decision / stale-supersession check the design brief describes is
out of scope until a Decision becomes an addressable reference a lens
can resolve.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.state.epoch2.plan_revision import PlanBody
from eawf.workflow.planning.lenses import PlanFindingCode, PlanLens, run_plan_lenses

pytestmark = pytest.mark.unit

SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0047"
BATCH = f"{SLOT}/batch/BAT-0001"
TASK = f"{SLOT}/task/EAWF-0001"
REPOSITORY = f"{SLOT}/repository/REP-EAWF"

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _criterion(id_: str, **overrides: Any) -> dict[str, Any]:
    """Return one loader-valid criterion payload keyed *id_*."""
    payload: dict[str, Any] = {
        "id": id_,
        "text": "the described behaviour holds",
        "kind": "functional_suitability",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "quality_dimension": "functional_suitability",
        "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
    }
    payload.update(overrides)
    return payload


def body_payload(**task_overrides: Any) -> dict[str, Any]:
    """Return a loader-valid single-Batch, single-Task plan body payload."""
    task: dict[str, Any] = {
        "urn": TASK,
        "batch_ref": BATCH,
        "priority": "P1",
        "intent": "do the described work",
        "criteria": [_criterion("CR-0001")],
    }
    task.update(task_overrides)
    return {
        "milestone_urn": MILESTONE,
        "milestone": {
            "key": "MLS-0047",
            "primary_track_ref": TRACK,
            "title": "Ship the described behaviour",
            "outcome": "An operator observes the described behaviour end to end.",
            "appetite": "M",
            "exclusions": ["none"],
            "acceptance_journey": [
                {
                    "step_id": "AS-01",
                    "actor": "operator",
                    "action": "observe the described behaviour",
                    "expected_observation": "the behaviour is present",
                    "evidence_kinds": ["artifact"],
                }
            ],
        },
        "batches": [{"urn": BATCH, "repository_ref": REPOSITORY}],
        "tasks": [task],
        "citations": [],
    }


def make_body(**task_overrides: Any) -> PlanBody:
    """Return a validated plan body with one Task carrying *task_overrides*."""
    return PlanBody.model_validate(body_payload(**task_overrides))


def _findings(body: PlanBody) -> list[Any]:
    return [f for f in run_plan_lenses(body) if f.lens is PlanLens.POLICY_CONSTITUTION]


# ---- boundary: no waiver at all --------------------------------------------------


def test_policy_constitution_lens_admits_a_required_criterion_with_no_waiver() -> None:
    """A required criterion with no waiver_reason makes no claim this lens can check."""
    assert _findings(make_body()) == []


def test_policy_constitution_lens_admits_an_optional_criterion_with_a_waiver() -> None:
    """A non-mandatory criterion is free to waive its own gate."""
    body = make_body(
        criteria=[_criterion("CR-0001", required=False, waiver_reason="not load-bearing")]
    )

    assert _findings(body) == []


# ---- error path: a mandatory criterion waives its own gate -----------------------


def test_policy_constitution_lens_flags_a_required_criterion_with_a_waiver() -> None:
    """A required criterion cannot waive the gate it is mandatory for."""
    body = make_body(criteria=[_criterion("CR-0001", required=True, waiver_reason="skip for now")])

    findings = _findings(body)

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.UNAUTHORIZED_CRITERION_WAIVER
    assert findings[0].entity_refs == (TASK, "CR-0001")


def test_policy_constitution_lens_flags_every_offending_criterion() -> None:
    """Two mandatory-and-waived criteria on the same Task each earn their own finding."""
    body = make_body(
        criteria=[
            _criterion("CR-0001", required=True, waiver_reason="skip"),
            _criterion("CR-0002", required=True, waiver_reason="skip too"),
        ]
    )

    findings = _findings(body)

    assert len(findings) == 2
    assert {f.entity_refs[1] for f in findings} == {"CR-0001", "CR-0002"}
