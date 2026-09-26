"""Lens 7: role authority.

A task-scoped Run is the only :class:`~eawf.kernel.state.epoch2.run.RunScope`
variant permitted to mutate a worktree, and it refuses construction with a
mutating purpose and an empty write set. This lens raises the same
refusal at plan time: a Task that declares a mutating ``run_purpose`` but
no ``write_claims`` would only fail once a worker tried to compile a Run
for it. A Task that declares no ``run_purpose`` at all makes no claim
this lens can check, so it earns no finding either way -- this is what
keeps every pre-``run_purpose`` fixture in this package green.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.state.epoch2.plan_revision import PlanBody
from eawf.workflow.planning.apply import PlanRevisionProposal, validate_plan_proposal
from eawf.workflow.planning.lenses import PlanFindingCode, PlanLens, run_plan_lenses
from eawf.workflow.planning.revision import PlanRefusal

pytestmark = pytest.mark.unit

SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0044"
BATCH = f"{SLOT}/batch/BAT-0001"
TASK = f"{SLOT}/task/EAWF-0001"
REPOSITORY = f"{SLOT}/repository/REP-EAWF"
HEAD = "a" * 40

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
OPERATOR: dict[str, str] = {"principal_kind": "operator", "principal_id": "OP-0001"}


def _criterion(id_: str) -> dict[str, Any]:
    """Return one loader-valid criterion payload keyed *id_*."""
    return {
        "id": id_,
        "text": "the described behaviour holds",
        "kind": "functional_suitability",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "quality_dimension": "functional_suitability",
        "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
    }


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
            "key": "MLS-0044",
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
    return [f for f in run_plan_lenses(body) if f.lens is PlanLens.ROLE_AUTHORITY]


def make_document() -> dict[str, Any]:
    """Return a locked-document fixture that resolves every world binding."""
    return {
        "track": {"TRK-RUNTIME": {"revision": 1, "status": "ACTIVE", "policy": {"revision": 1}}},
        "repository": {"REP-EAWF": {"head_sha": HEAD}},
    }


def make_proposal(body: PlanBody, *, key: str = "PRV-0001") -> PlanRevisionProposal:
    """Return the strict proposal *body* is submitted as."""
    return PlanRevisionProposal.model_validate(
        {"key": key, "author": OPERATOR, "body": body.model_dump(mode="json")}
    )


# ---- undeclared run_purpose is opt-out, not a violation ----------------------------


def test_role_authority_lens_admits_a_task_with_no_declared_purpose() -> None:
    """A Task that declares no run_purpose makes no claim this lens can check."""
    assert _findings(make_body()) == []


def test_role_authority_lens_admits_a_non_mutating_purpose_with_no_claims() -> None:
    """A review-purpose Task has nothing to write, so an empty claim set is fine."""
    assert _findings(make_body(run_purpose="review")) == []


# ---- a mutating purpose requires a write claim -------------------------------------


def test_role_authority_lens_admits_a_mutating_purpose_with_a_claim() -> None:
    """A mutating Task that names what it writes clears the lens."""
    body = make_body(run_purpose="implement", write_claims=["src/eawf/product/base.py"])

    assert _findings(body) == []


def test_role_authority_lens_flags_a_mutating_purpose_with_no_claim() -> None:
    """A mutating Task with no write_claims cannot seed the TaskScope it will need."""
    body = make_body(run_purpose="implement")

    findings = _findings(body)

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.TASK_WRITE_CLAIM_MISSING
    assert TASK in findings[0].entity_refs


@pytest.mark.parametrize("purpose", ["implement", "integrate", "repair"])
def test_role_authority_lens_flags_every_mutating_purpose_with_no_claim(purpose: str) -> None:
    """Every mutating purpose, not just implement, is checked the same way."""
    findings = _findings(make_body(run_purpose=purpose))

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.TASK_WRITE_CLAIM_MISSING


# ---- gate-fire proof: the finding actually refuses a submit -------------------------


def test_submit_refuses_a_mutating_task_with_no_write_claim_writing_nothing() -> None:
    """A mutating Task with no write_claims blocks at submit and leaves the document untouched.

    Deleting the role-authority-lens call in ``validate_plan_proposal``
    reds this test: the proposal is otherwise a well-typed,
    fully-resolving plan. This is what proves the lens runs from the real
    submit path, not only from a direct ``run_plan_lenses`` call.
    """
    body = make_body(run_purpose="implement")
    document = make_document()

    outcome = validate_plan_proposal(document, proposal=make_proposal(body), at=AT)

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_role_authority"
    assert document.get("batch", {}) == {}
    assert document.get("task", {}) == {}
