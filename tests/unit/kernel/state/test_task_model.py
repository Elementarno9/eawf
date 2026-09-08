"""The Task draft head: what a backlog row holds and what it may not.

A backlog row is a DRAFT Task, so one identifier survives from first idea
to completed delivery. These tests pin the two halves of that promise.
A draft holds a key, an intent and a priority and nothing that belongs to
a plan, so a draft carrying criteria or a Batch is refused rather than
quietly treated as dispatchable. A planned Task holds the full contract,
so a PLANNED row missing its Batch, its criteria, or its due scope is
refused rather than dispatched against an unspecified base.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.common import CriterionSpec, QualityDimension
from eawf.kernel.state.epoch2 import Task, TaskPriority, TaskStatus

pytestmark = pytest.mark.unit

TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
BATCH_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
MILESTONE_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
RELEASE_URN = "eawf://WSP-MAIN/PRJ-EAWF/_/release/REL-0.7.0"
TRACK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"
RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"

BINDING: dict[str, Any] = {
    "head_sha": "a" * 40,
    "tree_sha": "b" * 40,
    "contract_digest": "sha256:" + "c" * 64,
    "policy_revision": 1,
    "evidence_digest": "sha256:" + "d" * 64,
}


def _criterion() -> CriterionSpec:
    """Build one real typed criterion for a promoted Task."""
    return CriterionSpec(
        id="CR-01",
        text="the published wheel installs into a clean environment",
        kind="functional_suitability",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-01"],
        quality_dimension=QualityDimension.FUNCTIONAL_SUITABILITY,
        measurable_signal="uv run pytest tests/unit/kernel/state exits zero",
    )


def _draft_fields(**overrides: Any) -> dict[str, Any]:
    """Build the field mapping of a valid DRAFT Task."""
    fields: dict[str, Any] = {
        "uid": "1b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "key": "EAWF-0042",
        "urn": TASK_URN,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": "2026-09-08T00:00:00Z",
        "updated_at": "2026-09-08T00:00:00Z",
        "priority": "P1",
        "intent": "Publish the wheel to the index",
        "contract_revision": 1,
        "status": "DRAFT",
    }
    fields.update(overrides)
    return fields


def _planned_fields(**overrides: Any) -> dict[str, Any]:
    """Build the field mapping of a valid PLANNED Task."""
    fields = _draft_fields(
        status="PLANNED",
        batch_ref=BATCH_URN,
        due_scope=MILESTONE_URN,
        criteria=[_criterion()],
    )
    fields.update(overrides)
    return fields


# ---- the draft head ---------------------------------------------------------


def test_task_draft_validates_with_priority_intent_and_no_batch() -> None:
    task = Task.model_validate(_draft_fields())
    assert task.status is TaskStatus.DRAFT
    assert task.batch_ref is None
    assert task.due_scope is None
    assert task.criteria == ()
    assert task.priority is TaskPriority.P1
    assert task.intent == "Publish the wheel to the index"


def test_task_draft_rejects_a_batch_ref() -> None:
    with pytest.raises(ValidationError, match="unplaced and takes no batch_ref"):
        Task.model_validate(_draft_fields(batch_ref=BATCH_URN))


def test_task_draft_rejects_criteria() -> None:
    with pytest.raises(ValidationError, match="carries no criteria until it is promoted"):
        Task.model_validate(_draft_fields(criteria=[_criterion()]))


def test_task_draft_requires_an_intent() -> None:
    with pytest.raises(ValidationError):
        Task.model_validate(_draft_fields(intent="   "))


def test_task_deferred_validates_unplaced_and_undated() -> None:
    task = Task.model_validate(_draft_fields(status="DEFERRED"))
    assert task.status is TaskStatus.DEFERRED
    assert task.due_scope is None


def test_task_dropped_keeps_its_priority_and_intent_readable() -> None:
    task = Task.model_validate(_draft_fields(status="DROPPED"))
    assert task.status is TaskStatus.DROPPED
    assert task.priority is TaskPriority.P1


# ---- the planned contract ---------------------------------------------------


def test_task_planned_validates_with_the_full_contract() -> None:
    task = Task.model_validate(_planned_fields())
    assert task.status is TaskStatus.PLANNED
    assert str(task.batch_ref) == BATCH_URN
    assert len(task.criteria) == 1


def test_task_planned_without_batch_ref_is_rejected() -> None:
    fields = _planned_fields()
    del fields["batch_ref"]
    with pytest.raises(ValidationError, match="requires batch_ref"):
        Task.model_validate(fields)


def test_task_planned_without_criteria_is_rejected() -> None:
    with pytest.raises(ValidationError, match="requires at least one criterion"):
        Task.model_validate(_planned_fields(criteria=[]))


def test_task_planned_without_due_scope_is_rejected() -> None:
    fields = _planned_fields()
    del fields["due_scope"]
    with pytest.raises(ValidationError, match="due_scope is optional only in the backlog"):
        Task.model_validate(fields)


@pytest.mark.parametrize("status", ["CLAIMED", "RUNNING", "READY_TO_INTEGRATE"])
def test_task_later_states_still_require_a_due_scope(status: str) -> None:
    fields = _planned_fields(status=status)
    del fields["due_scope"]
    with pytest.raises(ValidationError, match="due_scope is optional only in the backlog"):
        Task.model_validate(fields)


@pytest.mark.parametrize("due_scope", [MILESTONE_URN, BATCH_URN, RELEASE_URN])
def test_task_due_scope_admits_each_horizon(due_scope: str) -> None:
    task = Task.model_validate(_planned_fields(due_scope=due_scope))
    assert str(task.due_scope) == due_scope


def test_task_due_scope_rejects_a_track_urn() -> None:
    with pytest.raises(ValidationError, match="identity_kind_mismatch"):
        Task.model_validate(_planned_fields(due_scope=TRACK_URN))


# ---- priority ---------------------------------------------------------------


@pytest.mark.parametrize("priority", ["P0", "P1", "P2", "P3"])
def test_task_priority_admits_each_band(priority: str) -> None:
    task = Task.model_validate(_draft_fields(priority=priority))
    assert task.priority.value == priority


@pytest.mark.parametrize("priority", ["P4", "p0", "P-1", "", "0"])
def test_task_priority_rejects_a_band_outside_the_ladder(priority: str) -> None:
    with pytest.raises(ValidationError):
        Task.model_validate(_draft_fields(priority=priority))


def test_task_requires_a_priority_from_creation() -> None:
    fields = _draft_fields()
    del fields["priority"]
    with pytest.raises(ValidationError, match="priority"):
        Task.model_validate(fields)


# ---- identity and the integration proof -------------------------------------


def test_task_rejects_a_urn_addressing_another_task() -> None:
    other = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0043"
    with pytest.raises(ValidationError, match="carries a URN addressing"):
        Task.model_validate(_draft_fields(urn=other))


def test_task_rejects_a_batch_ref_addressing_a_milestone() -> None:
    with pytest.raises(ValidationError, match="identity_kind_mismatch"):
        Task.model_validate(_planned_fields(batch_ref=MILESTONE_URN))


def test_task_rejects_a_non_string_urn() -> None:
    with pytest.raises(ValidationError, match="expected a qualified URN string"):
        Task.model_validate(_draft_fields(urn=42))


def test_task_completed_requires_an_integrated_binding() -> None:
    with pytest.raises(ValidationError, match="requires integrated_binding"):
        Task.model_validate(_planned_fields(status="COMPLETED"))


def test_task_completed_validates_with_its_integrated_binding() -> None:
    task = Task.model_validate(_planned_fields(status="COMPLETED", integrated_binding=BINDING))
    assert task.integrated_binding is not None
    assert task.integrated_binding.policy_revision == 1


def test_task_rejects_an_integrated_binding_before_completion() -> None:
    with pytest.raises(ValidationError, match="belongs to a COMPLETED Task"):
        Task.model_validate(_planned_fields(integrated_binding=BINDING))


def test_task_accepts_an_active_run_reference() -> None:
    task = Task.model_validate(_planned_fields(status="RUNNING", active_run_ref=RUN_URN))
    assert str(task.active_run_ref) == RUN_URN


def test_task_rejects_a_zero_contract_revision() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        Task.model_validate(_draft_fields(contract_revision=0))


def test_task_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match=r"extra_forbidden|Extra inputs"):
        Task.model_validate(_draft_fields(assignee="OP-0001"))


def test_task_renders_its_urn_through_the_identity_formatter() -> None:
    task = Task.model_validate(_planned_fields())
    dumped = task.model_dump(mode="json")
    assert dumped["urn"] == TASK_URN
    assert dumped["batch_ref"] == BATCH_URN
    assert dumped["due_scope"] == MILESTONE_URN
