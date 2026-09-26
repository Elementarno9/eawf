"""Lens 11 (idle-contract), and the CR-01 gate-fire proofs for lenses 11 and 12.

A :class:`~eawf.kernel.state.epoch2.plan_revision.SurfaceProducerRef` is
the only way a plan names an abstract new surface (a field, model,
event, RPC or gate) independent of a concrete write path; the
idle-contract lens is what refuses one that names nobody to build it.
The semantic-diff gate-fire proof lives here too, per CR-01, rather than
in its own file, because the wave's one gate-fire criterion covers both
lenses at once: a producerless surface and a hidden parent-Task deletion
each block a submit for their own reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from eawf.kernel.state.epoch2.plan_revision import PlanBody, plan_content_digest
from eawf.workflow.planning.apply import PlanRevisionProposal, validate_plan_proposal
from eawf.workflow.planning.lenses import PlanFindingCode, PlanLens, run_plan_lenses
from eawf.workflow.planning.revision import PlanRefusal, PlanRevisionAdvanced

pytestmark = pytest.mark.unit

SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0046"
BATCH = f"{SLOT}/batch/BAT-0001"
TASK = f"{SLOT}/task/EAWF-0001"
SECOND_TASK = f"{SLOT}/task/EAWF-0002"
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


def _task(urn: str = TASK, **overrides: Any) -> dict[str, Any]:
    """Return one loader-valid Task payload, keyed off *urn*'s own suffix."""
    suffix = urn.rsplit("-", 1)[-1]
    task: dict[str, Any] = {
        "urn": urn,
        "batch_ref": BATCH,
        "priority": "P1",
        "intent": "do the described work",
        "criteria": [_criterion(f"CR-{suffix}")],
    }
    task.update(overrides)
    return task


def body_payload(**overrides: Any) -> dict[str, Any]:
    """Return a loader-valid single-Batch, single-Task plan body payload."""
    payload: dict[str, Any] = {
        "milestone_urn": MILESTONE,
        "milestone": {
            "key": "MLS-0046",
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
        "tasks": [_task()],
        "citations": [],
    }
    payload.update(overrides)
    return payload


def make_body(**overrides: Any) -> PlanBody:
    """Return a validated plan body."""
    return PlanBody.model_validate(body_payload(**overrides))


def _idle_findings(body: PlanBody) -> list[Any]:
    return [f for f in run_plan_lenses(body) if f.lens is PlanLens.IDLE_CONTRACT]


# ---- lens 11: idle-contract -----------------------------------------------------


def test_idle_contract_lens_admits_a_plan_with_no_new_surfaces() -> None:
    """A plan that names no new surface has nothing this lens checks."""
    assert _idle_findings(make_body()) == []


def test_idle_contract_lens_admits_a_surface_with_a_producer_this_plan_creates() -> None:
    """A surface naming a Task the plan itself creates clears the lens."""
    body = make_body(new_surfaces=[{"surface_id": "SUR-01", "kind": "rpc", "producer_ref": TASK}])

    assert _idle_findings(body) == []


def test_idle_contract_lens_flags_a_surface_with_no_producer() -> None:
    """A surface with an unset producer_ref names nobody to build it."""
    body = make_body(new_surfaces=[{"surface_id": "SUR-01", "kind": "rpc"}])

    findings = _idle_findings(body)

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.IDLE_CONTRACT_PRODUCER_MISSING
    assert findings[0].entity_refs == ("SUR-01",)


def test_idle_contract_lens_flags_a_surface_whose_producer_is_not_this_plans_task() -> None:
    """A producer_ref naming a Task this plan does not create is unresolved, not named."""
    ghost = f"{SLOT}/task/EAWF-9999"
    body = make_body(
        new_surfaces=[{"surface_id": "SUR-01", "kind": "event", "producer_ref": ghost}]
    )

    findings = _idle_findings(body)

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.IDLE_CONTRACT_PRODUCER_MISSING
    assert findings[0].entity_refs == ("SUR-01",)


def test_idle_contract_lens_flags_every_producerless_surface_together() -> None:
    """Two producerless surfaces are named in one finding, sorted by id."""
    body = make_body(
        new_surfaces=[
            {"surface_id": "SUR-02", "kind": "field"},
            {"surface_id": "SUR-01", "kind": "gate"},
        ]
    )

    findings = _idle_findings(body)

    assert len(findings) == 1
    assert findings[0].entity_refs == ("SUR-01", "SUR-02")


# ---- CR-01: gate-fire proofs ----------------------------------------------------


def make_document(*, parent_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a locked-document fixture that resolves every world binding."""
    document: dict[str, Any] = {
        "track": {"TRK-RUNTIME": {"revision": 1, "status": "ACTIVE", "policy": {"revision": 1}}},
        "repository": {"REP-EAWF": {"head_sha": HEAD}},
    }
    if parent_row is not None:
        document["plan_revision"] = {parent_row["key"]: parent_row}
    return document


def make_proposal(
    body: PlanBody, *, key: str = "PRV-0002", parent_key: str | None = None
) -> PlanRevisionProposal:
    """Return the strict proposal *body* is submitted as."""
    payload: dict[str, Any] = {"key": key, "author": OPERATOR, "body": body.model_dump(mode="json")}
    if parent_key is not None:
        payload["parent_key"] = parent_key
    return PlanRevisionProposal.model_validate(payload)


def _stored_draft_row(*, key: str, body: PlanBody) -> dict[str, Any]:
    """Return a stored DRAFT PlanRevision row a repair can name as its parent.

    DRAFT carries no approval, which keeps the fixture to exactly the
    fields :func:`~eawf.workflow.planning.apply._stored_revision` needs
    to read the row back as a :class:`~eawf.kernel.state.epoch2.plan_revision.PlanRevision`.
    """
    return {
        "key": key,
        "revision": 1,
        "status": "DRAFT",
        "author": OPERATOR,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
        "content_digest": plan_content_digest(body),
        "base_state_revision": 1,
        "policy_revision": 1,
        "head_bindings": [],
        "body": body.model_dump(mode="json"),
    }


def test_submit_refuses_an_idle_contract_surface_writing_nothing() -> None:
    """A producer-less surface blocks at submit and leaves the document untouched.

    Deleting the idle-contract-lens call in ``validate_plan_proposal``
    reds this test: the proposal is otherwise a well-typed, fully-
    resolving plan.
    """
    body = make_body(new_surfaces=[{"surface_id": "SUR-01", "kind": "rpc"}])
    document = make_document()

    outcome = validate_plan_proposal(document, proposal=make_proposal(body), at=AT)

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_idle_contract"
    assert document.get("batch", {}) == {}
    assert document.get("task", {}) == {}


def test_submit_refuses_a_hidden_parent_task_deletion() -> None:
    """Dropping a parent Task with no typed drop blocks at submit.

    The parent revision creates ``TASK``; the repair proposal creates
    only ``SECOND_TASK`` and never mentions the parent's, so the
    semantic-diff lens catches the deletion the repair never enumerated.
    Deleting the semantic-diff-lens call reds this test the same way.
    """
    parent_body = make_body(tasks=[_task(TASK)])
    child_body = make_body(tasks=[_task(SECOND_TASK)])
    document = make_document(parent_row=_stored_draft_row(key="PRV-0001", body=parent_body))

    outcome = validate_plan_proposal(
        document, proposal=make_proposal(child_body, parent_key="PRV-0001"), at=AT
    )

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_semantic_diff"
    assert TASK in outcome.detail
    assert document.get("batch", {}) == {}
    assert document.get("task", {}) == {}


def test_submit_admits_a_repair_that_types_the_drop() -> None:
    """A repair that names its dropped parent Task in dropped_tasks clears the lens."""
    parent_body = make_body(tasks=[_task(TASK)])
    child_body = make_body(
        tasks=[_task(SECOND_TASK)],
        dropped_tasks=[{"task_ref": TASK, "reason": "superseded by the new Task"}],
    )
    document = make_document(parent_row=_stored_draft_row(key="PRV-0001", body=parent_body))

    outcome = validate_plan_proposal(
        document, proposal=make_proposal(child_body, parent_key="PRV-0001"), at=AT
    )

    assert isinstance(outcome, PlanRevisionAdvanced)


def test_submit_refuses_an_unresolvable_parent_key() -> None:
    """Naming a parent revision the document does not hold refuses before any lens runs."""
    child_body = make_body(tasks=[_task(SECOND_TASK)])
    document = make_document()

    outcome = validate_plan_proposal(
        document, proposal=make_proposal(child_body, parent_key="PRV-9999"), at=AT
    )

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "parent_plan_revision_resolved"
