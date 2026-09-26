"""Lens 8: integration/migration.

Each Batch names exactly one repository, so a Task's cross-Batch
``depends_on`` edge always names a pair of repositories too. Contracting
every Task down to the one repository its Batch integrates into turns
the Task dependency graph into a repository graph; a cycle there means
two repositories each need the other to land first, which no rollback
sequence can satisfy. A same-repository dependency, however deep the
chain, contracts to a self-loop-free single node and is never a finding.
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
#: A repository URN addresses itself: its entity_key must equal its own
#: repository slot, so a second repository needs its own base triple
#: rather than a second key under ``SLOT``.
SLOT_OTHER = "eawf://WSP-MAIN/PRJ-EAWF/REP-OTHER"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0045"
BATCH_A = f"{SLOT}/batch/BAT-0001"
BATCH_B = f"{SLOT}/batch/BAT-0002"
REPO_A = f"{SLOT}/repository/REP-EAWF"
REPO_B = f"{SLOT_OTHER}/repository/REP-OTHER"
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


def _task(urn: str, *, batch_ref: str, depends_on: tuple[str, ...] = ()) -> dict[str, Any]:
    """Return one loader-valid Task payload, keyed off *urn*'s own suffix."""
    suffix = urn.rsplit("-", 1)[-1]
    return {
        "urn": urn,
        "batch_ref": batch_ref,
        "priority": "P1",
        "intent": "do the described work",
        "criteria": [_criterion(f"CR-{suffix}")],
        "depends_on": list(depends_on),
    }


def body_payload(*, tasks: list[dict[str, Any]], repo_b: str = REPO_B) -> dict[str, Any]:
    """Return a loader-valid two-Batch, two-repository plan body payload."""
    return {
        "milestone_urn": MILESTONE,
        "milestone": {
            "key": "MLS-0045",
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
        "batches": [
            {"urn": BATCH_A, "repository_ref": REPO_A},
            {"urn": BATCH_B, "repository_ref": repo_b},
        ],
        "tasks": tasks,
        "citations": [],
    }


def make_body(*, tasks: list[dict[str, Any]], repo_b: str = REPO_B) -> PlanBody:
    """Return a validated plan body."""
    return PlanBody.model_validate(body_payload(tasks=tasks, repo_b=repo_b))


def _findings(body: PlanBody) -> list[Any]:
    return [f for f in run_plan_lenses(body) if f.lens is PlanLens.INTEGRATION_MIGRATION]


def make_document() -> dict[str, Any]:
    """Return a locked-document fixture that resolves both repositories' heads."""
    return {
        "track": {"TRK-RUNTIME": {"revision": 1, "status": "ACTIVE", "policy": {"revision": 1}}},
        "repository": {
            "REP-EAWF": {"head_sha": HEAD},
            "REP-OTHER": {"head_sha": HEAD},
        },
    }


def make_proposal(body: PlanBody, *, key: str = "PRV-0001") -> PlanRevisionProposal:
    """Return the strict proposal *body* is submitted as."""
    return PlanRevisionProposal.model_validate(
        {"key": key, "author": OPERATOR, "body": body.model_dump(mode="json")}
    )


# ---- same-repository dependencies never fire ---------------------------------------


def test_integration_migration_lens_admits_a_same_repository_chain() -> None:
    """A dependency chain that never leaves one repository contracts to one node."""
    first = f"{SLOT}/task/EAWF-0001"
    second = f"{SLOT}/task/EAWF-0002"
    body = make_body(
        tasks=[
            _task(first, batch_ref=BATCH_A),
            _task(second, batch_ref=BATCH_A, depends_on=(first,)),
        ],
        repo_b=REPO_A,
    )

    assert _findings(body) == []


# ---- a one-directional cross-repository dependency is fine --------------------------


def test_integration_migration_lens_admits_a_one_directional_cross_repository_edge() -> None:
    """One repository depending on another, with no edge back, admits an order."""
    upstream = f"{SLOT}/task/EAWF-0001"
    downstream = f"{SLOT}/task/EAWF-0002"
    body = make_body(
        tasks=[
            _task(upstream, batch_ref=BATCH_A),
            _task(downstream, batch_ref=BATCH_B, depends_on=(upstream,)),
        ]
    )

    assert _findings(body) == []


# ---- a mutual cross-repository dependency admits no rollback sequence ---------------


def test_integration_migration_lens_flags_a_repository_cycle() -> None:
    """Two repositories each depending on the other admit no integration order."""
    a1 = f"{SLOT}/task/EAWF-0001"
    b1 = f"{SLOT}/task/EAWF-0002"
    a2 = f"{SLOT}/task/EAWF-0003"
    b2 = f"{SLOT}/task/EAWF-0004"
    body = make_body(
        tasks=[
            _task(a1, batch_ref=BATCH_A, depends_on=(b1,)),
            _task(b1, batch_ref=BATCH_B),
            _task(a2, batch_ref=BATCH_A),
            _task(b2, batch_ref=BATCH_B, depends_on=(a2,)),
        ]
    )

    findings = _findings(body)

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.INTEGRATION_REPOSITORY_CYCLE_DETECTED
    assert set(findings[0].entity_refs) == {REPO_A, REPO_B}


# ---- gate-fire proof: the finding actually refuses a submit -------------------------


def test_submit_refuses_a_repository_cycle_writing_nothing() -> None:
    """A repository dependency cycle blocks at submit and leaves the document untouched.

    Deleting the integration-migration-lens call in
    ``validate_plan_proposal`` reds this test: the proposal is otherwise a
    well-typed, fully-resolving plan. This is what proves the lens runs
    from the real submit path, not only from a direct ``run_plan_lenses``
    call.
    """
    a1 = f"{SLOT}/task/EAWF-0001"
    b1 = f"{SLOT}/task/EAWF-0002"
    a2 = f"{SLOT}/task/EAWF-0003"
    b2 = f"{SLOT}/task/EAWF-0004"
    body = make_body(
        tasks=[
            _task(a1, batch_ref=BATCH_A, depends_on=(b1,)),
            _task(b1, batch_ref=BATCH_B),
            _task(a2, batch_ref=BATCH_A),
            _task(b2, batch_ref=BATCH_B, depends_on=(a2,)),
        ]
    )
    document = make_document()

    outcome = validate_plan_proposal(document, proposal=make_proposal(body), at=AT)

    assert isinstance(outcome, PlanRefusal)
    assert outcome.guard == "plan_lens_integration_migration"
    assert document.get("batch", {}) == {}
    assert document.get("task", {}) == {}


def test_integration_migration_lens_admits_a_single_batch_plan() -> None:
    """A plan with one Batch has no cross-Batch edge to contract at all."""
    only = f"{SLOT}/task/EAWF-0001"
    body = PlanBody.model_validate(
        {
            "milestone_urn": MILESTONE,
            "milestone": {
                "key": "MLS-0045",
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
            "batches": [{"urn": BATCH_A, "repository_ref": REPO_A}],
            "tasks": [_task(only, batch_ref=BATCH_A)],
            "citations": [],
        }
    )

    assert _findings(body) == []
