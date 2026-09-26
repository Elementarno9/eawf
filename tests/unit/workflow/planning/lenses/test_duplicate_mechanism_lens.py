"""Lens 10: duplicate mechanism.

Criterion ids are scoped per Task, matching the approval gate's own
task-scoped identity (``f"{task.urn}/{criterion.id}"`` in
:func:`~eawf.workflow.planning.revision.ungrounded_approval_criteria`).
Two criterion rows sharing one id on the SAME Task give one identity two
canonical owners, which is the "same persisted truth under two names"
defect the design brief describes. Two different Tasks each naming their
own criterion the same id are not that defect: a reused id across Tasks
names two different mechanisms, not one mechanism with two owners (F7).
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
MILESTONE = f"{SLOT}/milestone/MLS-0048"
BATCH = f"{SLOT}/batch/BAT-0001"
TASK = f"{SLOT}/task/EAWF-0001"
SECOND_TASK = f"{SLOT}/task/EAWF-0002"
REPOSITORY = f"{SLOT}/repository/REP-EAWF"

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


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


def _task(urn: str, **overrides: Any) -> dict[str, Any]:
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
            "key": "MLS-0048",
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
        "tasks": [_task(TASK)],
        "citations": [],
    }
    payload.update(overrides)
    return payload


def make_body(**overrides: Any) -> PlanBody:
    """Return a validated plan body."""
    return PlanBody.model_validate(body_payload(**overrides))


def _findings(body: PlanBody) -> list[Any]:
    return [f for f in run_plan_lenses(body) if f.lens is PlanLens.DUPLICATE_MECHANISM]


# ---- boundary: every criterion id is unique ---------------------------------------


def test_duplicate_mechanism_lens_admits_a_single_task_plan() -> None:
    """One Task, one criterion id: nothing to collide with."""
    assert _findings(make_body()) == []


def test_duplicate_mechanism_lens_admits_two_tasks_with_distinct_ids() -> None:
    """Two Tasks naming two different criterion ids share no identity."""
    body = make_body(tasks=[_task(TASK), _task(SECOND_TASK)])

    assert _findings(body) == []


# ---- boundary: an id shared across Tasks is not a defect (F7) ---------------


def test_duplicate_mechanism_lens_admits_the_same_id_shared_across_two_tasks() -> None:
    """Two different Tasks each declaring their own criterion the same id submits.

    F7: reusing an id across Tasks names two different mechanisms, not
    one mechanism with two owners -- the approval gate's own criterion
    identity is the Task/id pair, not the id alone.
    """
    body = make_body(
        tasks=[
            {
                "urn": TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work",
                "criteria": [_criterion("CR-SHARED")],
            },
            {
                "urn": SECOND_TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work too",
                "criteria": [_criterion("CR-SHARED")],
            },
        ]
    )

    assert _findings(body) == []


# ---- error path: one id, two owners on the SAME Task ------------------------


def test_duplicate_mechanism_lens_flags_the_same_id_declared_twice_on_one_task() -> None:
    """The same Task declaring one criterion id twice is still two canonical rows."""
    body = make_body(
        tasks=[
            {
                "urn": TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work",
                "criteria": [_criterion("CR-SHARED"), _criterion("CR-SHARED")],
            }
        ]
    )

    findings = _findings(body)

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.DUPLICATE_CRITERION_ID
    assert findings[0].entity_refs == ("CR-SHARED", TASK)


def test_duplicate_mechanism_lens_flags_each_task_separately() -> None:
    """Two Tasks each repeating a different id internally earn two findings.

    Proves the lens is scoped per Task rather than collapsing every
    repeated id in the plan into one finding: each Task's own defect is
    reported on its own row.
    """
    body = make_body(
        tasks=[
            {
                "urn": TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work",
                "criteria": [_criterion("CR-A"), _criterion("CR-A")],
            },
            {
                "urn": SECOND_TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work too",
                "criteria": [_criterion("CR-B"), _criterion("CR-B")],
            },
        ]
    )

    findings = _findings(body)

    assert [f.entity_refs for f in findings] == [("CR-A", TASK), ("CR-B", SECOND_TASK)]
