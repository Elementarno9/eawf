"""DeliveryBatch status-dependent fields and task membership.

A Batch's target branch, exact head binding, and failure reason are each
required only from the status that makes them facts. These tests pin
both sides of every such rule: the status that needs the field refuses
its absence, and the status that precedes the fact accepts it missing,
so a caller is never forced to invent a head binding early.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2 import BatchStatus, DeliveryBatch

pytestmark = pytest.mark.unit

BATCH_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
OTHER_TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0043"
BRANCH = "feature/eawf-v0.7"

BINDING: dict[str, Any] = {
    "head_sha": "a" * 40,
    "tree_sha": "b" * 40,
    "contract_digest": "sha256:" + "c" * 64,
    "policy_revision": 1,
    "evidence_digest": "sha256:" + "d" * 64,
}

REASON: dict[str, Any] = {
    "code": "host-refused-the-merge",
    "message": "The host refused the merge because the base branch moved.",
}

#: The statuses that claim mergeability or beyond, and so bind a head.
HEAD_BOUND = ["READY_TO_MERGE", "MERGING", "MERGED_PENDING_RECONCILIATION", "COMPLETED"]


def _batch_fields(**overrides: Any) -> dict[str, Any]:
    """Build the field mapping of a valid PLANNED DeliveryBatch."""
    fields: dict[str, Any] = {
        "uid": "3b4e28ba-2fa1-11d2-883f-0016d3cca427",
        "key": "BAT-0007",
        "urn": BATCH_URN,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": "2026-09-08T00:00:00Z",
        "updated_at": "2026-09-08T00:00:00Z",
        "milestone_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030",
        "repository_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF",
        "status": "PLANNED",
    }
    fields.update(overrides)
    return fields


def test_delivery_batch_planned_validates_without_a_target_branch() -> None:
    batch = DeliveryBatch.model_validate(_batch_fields())
    assert batch.status is BatchStatus.PLANNED
    assert batch.target_branch is None
    assert batch.task_refs == ()


def test_delivery_batch_active_validates_with_a_target_branch_and_no_binding() -> None:
    batch = DeliveryBatch.model_validate(_batch_fields(status="ACTIVE", target_branch=BRANCH))
    assert batch.target_branch == BRANCH
    assert batch.current_head_binding is None


# The binding and failure are supplied so the branch is the only field the
# status still lacks; otherwise a later rule could mask the one under test.
@pytest.mark.parametrize("status", ["ACTIVE", "CANCELLED", "FAILED", *HEAD_BOUND])
def test_delivery_batch_rejects_an_activated_status_without_a_target_branch(
    status: str,
) -> None:
    failure = REASON if status == "FAILED" else None
    fields = _batch_fields(status=status, current_head_binding=BINDING, failure=failure)
    with pytest.raises(ValidationError, match=f"status {status} requires target_branch"):
        DeliveryBatch.model_validate(fields)


@pytest.mark.parametrize("status", HEAD_BOUND)
def test_delivery_batch_head_bound_status_validates_with_its_binding(status: str) -> None:
    fields = _batch_fields(status=status, target_branch=BRANCH, current_head_binding=BINDING)
    batch = DeliveryBatch.model_validate(fields)
    assert batch.current_head_binding is not None
    assert batch.current_head_binding.policy_revision == 1


@pytest.mark.parametrize("status", HEAD_BOUND)
def test_delivery_batch_head_bound_status_without_a_binding_is_rejected(status: str) -> None:
    fields = _batch_fields(status=status, target_branch=BRANCH)
    with pytest.raises(ValidationError, match=f"status {status} requires current_head_binding"):
        DeliveryBatch.model_validate(fields)


def test_delivery_batch_failed_validates_with_a_failure_reason() -> None:
    batch = DeliveryBatch.model_validate(
        _batch_fields(status="FAILED", target_branch=BRANCH, failure=REASON)
    )
    assert batch.failure is not None
    assert batch.failure.code == "host-refused-the-merge"


def test_delivery_batch_failed_without_a_failure_reason_is_rejected() -> None:
    fields = _batch_fields(status="FAILED", target_branch=BRANCH)
    with pytest.raises(ValidationError, match="a FAILED Batch requires a failure reason"):
        DeliveryBatch.model_validate(fields)


@pytest.mark.parametrize("status", ["PLANNED", "ACTIVE", "CANCELLED"])
def test_delivery_batch_rejects_a_failure_reason_outside_failed(status: str) -> None:
    fields = _batch_fields(status=status, target_branch=BRANCH, failure=REASON)
    with pytest.raises(
        ValidationError, match=f"a failure reason belongs to a FAILED Batch, not {status}"
    ):
        DeliveryBatch.model_validate(fields)


def test_delivery_batch_accepts_distinct_task_refs() -> None:
    batch = DeliveryBatch.model_validate(_batch_fields(task_refs=[TASK_URN, OTHER_TASK_URN]))
    assert [str(ref) for ref in batch.task_refs] == [TASK_URN, OTHER_TASK_URN]


def test_delivery_batch_rejects_a_repeated_task_ref() -> None:
    with pytest.raises(ValidationError, match="task_refs names the same record twice"):
        DeliveryBatch.model_validate(_batch_fields(task_refs=[TASK_URN, TASK_URN]))


def test_delivery_batch_rejects_an_unknown_status() -> None:
    with pytest.raises(ValidationError, match="status"):
        DeliveryBatch.model_validate(_batch_fields(status="PAUSED"))


def test_delivery_batch_rejects_a_branch_name_git_refuses() -> None:
    fields = _batch_fields(status="ACTIVE", target_branch="feature/eawf~v0.7")
    with pytest.raises(ValidationError, match="target_branch"):
        DeliveryBatch.model_validate(fields)
