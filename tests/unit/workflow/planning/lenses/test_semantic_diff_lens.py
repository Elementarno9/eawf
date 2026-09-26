"""Lens 12: semantic diff.

Scoped to the one category the design brief's fuller diff (add/move/edit/
delete, invalidated approvals, migration impact) reduces to when a Task's
only stable key is its URN: a parent Task this revision's own Tasks no
longer create, and that ``dropped_tasks`` does not name, is a hidden
deletion. See :mod:`eawf.workflow.planning.lenses.governance` for why the
richer diff categories stay out of scope until Task identity carries more
than "same URN, present or absent".
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
MILESTONE = f"{SLOT}/milestone/MLS-0049"
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


def _task(urn: str) -> dict[str, Any]:
    """Return one loader-valid Task payload, keyed off *urn*'s own suffix."""
    suffix = urn.rsplit("-", 1)[-1]
    return {
        "urn": urn,
        "batch_ref": BATCH,
        "priority": "P1",
        "intent": "do the described work",
        "criteria": [_criterion(f"CR-{suffix}")],
    }


def body_payload(**overrides: Any) -> dict[str, Any]:
    """Return a loader-valid single-Batch, single-Task plan body payload."""
    payload: dict[str, Any] = {
        "milestone_urn": MILESTONE,
        "milestone": {
            "key": "MLS-0049",
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


def _findings(body: PlanBody, *, parent: PlanBody | None) -> list[Any]:
    return [f for f in run_plan_lenses(body, parent=parent) if f.lens is PlanLens.SEMANTIC_DIFF]


# ---- boundary: nothing to diff against, or nothing dropped -----------------------


def test_semantic_diff_lens_admits_a_plan_with_no_parent() -> None:
    """A fresh, non-repair plan has nothing to diff against."""
    assert _findings(make_body(), parent=None) == []


def test_semantic_diff_lens_admits_a_repair_that_keeps_every_parent_task() -> None:
    """A repair that still creates every parent Task drops nothing."""
    parent = make_body(tasks=[_task(TASK)])
    child = make_body(tasks=[_task(TASK), _task(SECOND_TASK)])

    assert _findings(child, parent=parent) == []


def test_semantic_diff_lens_admits_a_repair_that_types_its_drop() -> None:
    """A repair naming its dropped parent Task in dropped_tasks is not hidden."""
    parent = make_body(tasks=[_task(TASK)])
    child = make_body(
        tasks=[_task(SECOND_TASK)],
        dropped_tasks=[{"task_ref": TASK, "reason": "superseded by the new Task"}],
    )

    assert _findings(child, parent=parent) == []


# ---- error path: a parent Task vanishes with no typed drop -----------------------


def test_semantic_diff_lens_flags_a_hidden_parent_task_deletion() -> None:
    """A parent Task absent from the repair, and untyped, is a hidden deletion."""
    parent = make_body(tasks=[_task(TASK)])
    child = make_body(tasks=[_task(SECOND_TASK)])

    findings = _findings(child, parent=parent)

    assert len(findings) == 1
    assert findings[0].code is PlanFindingCode.SEMANTIC_DIFF_HIDDEN_TASK_DELETION
    assert findings[0].entity_refs == (TASK,)


def test_semantic_diff_lens_flags_every_hidden_deletion_together() -> None:
    """Two vanished parent Tasks are named in one finding, sorted by URN."""
    third_task = f"{SLOT}/task/EAWF-0003"
    parent = make_body(tasks=[_task(TASK), _task(third_task)])
    child = make_body(tasks=[_task(SECOND_TASK)])

    findings = _findings(child, parent=parent)

    assert len(findings) == 1
    assert findings[0].entity_refs == (TASK, third_task)
