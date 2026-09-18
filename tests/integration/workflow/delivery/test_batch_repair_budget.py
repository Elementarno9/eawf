"""Repair is bounded: the first blocker is fixed, the second one is asked about.

An agent that answers every blocking finding by opening another repair
has no terminating condition, and the failure it produces is the worst
kind -- a Batch that looks busy forever. The budget is what ends it. The
first required blocker opens one repair Task bounded to exactly the
criteria that blocked; once that budget is spent, a further required
blocker on the repaired head stops autonomous repair and opens an
operator question instead.

Both ways out are ``ConflictExit`` values, so the rule that a repair
lands on a Task and an operator decision on a pending action is the same
rule a blocked merge already obeys, checked by the same validator rather
than by a second one that could disagree with it.

The loop is driven through the daemon verb against a seeded tree, so the
head the second blocker is judged against is the head the Batch actually
delivers after the repair landed, not one the test asserted into being.

Nothing sleeps, polls, reaches the network or writes outside ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.batch_proof import (
    AuditVerdict,
    BatchAudit,
    BatchVerificationStage,
    ReviewerAttestation,
)
from eawf.kernel.delivery.integration import ConflictExit, ConflictExitKind
from eawf.kernel.delivery.receipts import RevisionBinding, canonical_digest
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery import BatchVerifyParams, verify_batch
from tests.integration.workflow.delivery import _completion_fixtures as world

pytestmark = pytest.mark.integration

REVIEWER_RUN: Final = f"{world.CONTAINER}/run/RUN-00000020"
REPAIR_TASK: Final = f"{world.CONTAINER}/task/EAWF-0077"
OPERATOR_ACTION: Final = f"{world.CONTAINER}/pending-action/ACT-0003"

#: The grants a reviewer legitimately needs.
READ_ONLY_GRANTS: Final = (SemanticToolId.REPO_READ, SemanticToolId.DIFF_READ)

#: Both ways out, so the walk never stops for want of a reference.
EXITS: Final[dict[ConflictExitKind, str]] = {
    ConflictExitKind.REPAIR_TASK: REPAIR_TASK,
    ConflictExitKind.OPERATOR_DECISION: OPERATOR_ACTION,
}


def attestation(revision: RevisionBinding) -> ReviewerAttestation:
    """Return the attestation of a reviewer that read *revision*."""
    return ReviewerAttestation(
        run_ref=REVIEWER_RUN,
        purpose=RunPurpose.REVIEW,
        reviewed_revision=revision,
        capsule_digest=canonical_digest("capsule"),
        tool_grants=READ_ONLY_GRANTS,
    )


def audit(
    criterion_id: str,
    *,
    revision: RevisionBinding,
    verdict: AuditVerdict = AuditVerdict.VERIFIED_FALSE,
    required: bool = True,
    audit_id: str = "BAU-000001",
) -> BatchAudit:
    """Return one criterion's verdict at *revision*."""
    return BatchAudit(
        id=audit_id,
        batch_ref=world.BATCH,
        criterion_id=criterion_id,
        required=required,
        verdict=verdict,
        audited_revision=revision,
        review=attestation(revision),
        finding=(
            f"criterion {criterion_id} does not hold on the reviewed tree"
            if verdict is not AuditVerdict.VERIFIED_TRUE
            else None
        ),
        recorded_at=world.AT,
    )


def native(tmp_path: Path, *generations):
    """Return a canary tree whose Batch delivers *generations*, newest last."""
    context = world.native_tree(tmp_path / "repo", tmp_path / "runtime")
    world.seed_task(context, world.task_row())
    world.seed_lines(context, world.TASK, [world.bundle_line(world.bundle_row())])
    world.seed_lines(context, world.BATCH, [world.generation_line(item) for item in generations])
    return context


def land_repair(context, generation) -> None:
    """Append the generation a landed repair produced to the Batch ledger."""
    world.seed_lines(context, world.BATCH, [world.generation_line(generation)])


def params(**overrides: Any) -> BatchVerifyParams:
    """Return a verification request for the shared Batch."""
    fields: dict[str, Any] = {
        "urn": world.BATCH,
        "actor": "OPERATOR-LOCAL",
        "idempotency_key": "verify-1",
        "audits": (),
        "judgment_criterion_ids": ("CR-01",),
        "exit_refs": EXITS,
        "repair_budget": 1,
    }
    return BatchVerifyParams.model_validate(fields | overrides)


def run_verify(context, **overrides: Any):
    """Walk the Batch's cycle once against the seeded tree."""
    return verify_batch(context, params(**overrides), now=world.AT)


def first_head() -> RevisionBinding:
    """Return the revision the Batch delivers before any repair lands."""
    return world.delivering_generation().integrated_revision


def repaired_head() -> RevisionBinding:
    """Return the revision the landed repair produced."""
    return world.following_generation(affected=("CR-01",)).integrated_revision


# ---- the first blocker opens one bounded repair ------------------------------


def test_first_blocking_finding_opens_one_bounded_repair_task(tmp_path: Path) -> None:
    """One required blocker opens a repair Task bounded to what blocked."""
    context = native(tmp_path, world.delivering_generation())
    answer = run_verify(context, audits=(audit("CR-01", revision=first_head()),))
    assert answer.stage == BatchVerificationStage.REPAIR.value
    assert answer.repairs_spent == 1
    assert answer.repair_criterion_ids == ("CR-01",)
    assert answer.exit == {"kind": ConflictExitKind.REPAIR_TASK.value, "ref": REPAIR_TASK}
    assert answer.merge_ready is False


def test_repair_is_bounded_to_the_criteria_that_blocked(tmp_path: Path) -> None:
    """A criterion that came back true is not swept into the repair's scope."""
    context = native(tmp_path, world.delivering_generation())
    head = first_head()
    answer = run_verify(
        context,
        judgment_criterion_ids=("CR-01", "CR-02"),
        audits=(
            audit("CR-01", revision=head, audit_id="BAU-000001"),
            audit(
                "CR-02",
                revision=head,
                verdict=AuditVerdict.VERIFIED_TRUE,
                audit_id="BAU-000002",
            ),
        ),
    )
    assert answer.repair_criterion_ids == ("CR-01",)
    assert answer.settled_criterion_ids == ("CR-02",)


def test_a_repair_exit_must_reference_a_task() -> None:
    """The exit validator the merge path already uses refuses a mistyped ref."""
    with pytest.raises(ValidationError, match="must reference a task"):
        ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=OPERATOR_ACTION)


def test_an_operator_exit_must_reference_a_pending_action() -> None:
    """An operator question lands on a pending action, not on a Task."""
    with pytest.raises(ValidationError, match="must reference a pending-action"):
        ConflictExit(kind=ConflictExitKind.OPERATOR_DECISION, ref=REPAIR_TASK)


def test_verify_batch_refuses_a_blocker_with_no_repair_exit_named(
    tmp_path: Path,
) -> None:
    """A blocked cycle with nowhere to land is refused rather than stalled."""
    context = native(tmp_path, world.delivering_generation())
    with pytest.raises(DaemonValidationError, match="integration_exit_unnamed"):
        run_verify(
            context,
            audits=(audit("CR-01", revision=first_head()),),
            exit_refs={ConflictExitKind.OPERATOR_DECISION: OPERATOR_ACTION},
        )


# ---- the second blocker on the repaired head asks the operator ---------------


def test_second_blocker_on_the_repaired_head_stops_autonomous_repair(
    tmp_path: Path,
) -> None:
    """The budget ends the loop: the second required blocker asks the operator."""
    context = native(tmp_path, world.delivering_generation())
    opened = run_verify(context, audits=(audit("CR-01", revision=first_head()),))
    assert opened.stage == BatchVerificationStage.REPAIR.value

    land_repair(context, world.following_generation(affected=("CR-01",)))
    asked = run_verify(context, audits=(audit("CR-01", revision=repaired_head()),))
    assert asked.head_generation == 3
    assert asked.stage == BatchVerificationStage.OPERATOR_DECISION.value
    assert asked.repairs_spent == 1
    assert asked.repair_criterion_ids == ()
    assert asked.exit == {
        "kind": ConflictExitKind.OPERATOR_DECISION.value,
        "ref": OPERATOR_ACTION,
    }
    assert "asks the operator" in asked.reason


def test_a_cleared_second_pass_on_the_repaired_head_merges(tmp_path: Path) -> None:
    """A repair that worked lets the Batch clear on the head it produced."""
    context = native(tmp_path, world.delivering_generation())
    run_verify(context, audits=(audit("CR-01", revision=first_head()),))
    land_repair(context, world.following_generation(affected=("CR-01",)))
    cleared = run_verify(
        context,
        audits=(audit("CR-01", revision=repaired_head(), verdict=AuditVerdict.VERIFIED_TRUE),),
    )
    assert cleared.merge_ready is True
    assert cleared.head_generation == 3
    assert cleared.repairs_spent == 1


def test_the_repaired_head_invalidates_only_what_the_move_named(
    tmp_path: Path,
) -> None:
    """A landed repair drops the criterion it named and carries the other."""
    context = native(tmp_path, world.delivering_generation())
    head = first_head()
    run_verify(
        context,
        judgment_criterion_ids=("CR-01", "CR-02"),
        audits=(
            audit("CR-01", revision=head, audit_id="BAU-000001"),
            audit(
                "CR-02",
                revision=head,
                verdict=AuditVerdict.VERIFIED_TRUE,
                audit_id="BAU-000002",
            ),
        ),
    )
    land_repair(context, world.following_generation(affected=("CR-01",)))
    moved = run_verify(
        context,
        judgment_criterion_ids=("CR-01", "CR-02"),
        audits=(
            audit(
                "CR-01",
                revision=repaired_head(),
                verdict=AuditVerdict.VERIFIED_TRUE,
                audit_id="BAU-000003",
            ),
        ),
    )
    assert moved.invalidated_criterion_ids == ("CR-01",)
    assert moved.carried_criterion_ids == ("CR-02",)
    assert moved.settled_criterion_ids == ("CR-01", "CR-02")
    assert moved.merge_ready is True


def test_a_head_move_that_names_nothing_carries_every_verdict(
    tmp_path: Path,
) -> None:
    """A generation invalidating no criterion is not a blanket reset."""
    context = native(tmp_path, world.delivering_generation())
    run_verify(
        context,
        audits=(audit("CR-01", revision=first_head(), verdict=AuditVerdict.VERIFIED_TRUE),),
    )
    land_repair(context, world.following_generation(affected=()))
    moved = run_verify(context, audits=())
    assert moved.invalidated_criterion_ids == ()
    assert moved.carried_criterion_ids == ("CR-01",)
    assert moved.merge_ready is True


# ---- budget boundaries -------------------------------------------------------


def test_a_zero_repair_budget_asks_the_operator_on_the_first_blocker(
    tmp_path: Path,
) -> None:
    """With no repair allowed the first blocker goes straight to the operator."""
    context = native(tmp_path, world.delivering_generation())
    answer = run_verify(context, audits=(audit("CR-01", revision=first_head()),), repair_budget=0)
    assert answer.stage == BatchVerificationStage.OPERATOR_DECISION.value
    assert answer.repairs_spent == 0


def test_a_budget_of_two_opens_a_second_repair_before_asking(tmp_path: Path) -> None:
    """The bound is the budget, not the number one."""
    context = native(tmp_path, world.delivering_generation())
    first = run_verify(context, audits=(audit("CR-01", revision=first_head()),), repair_budget=2)
    assert first.stage == BatchVerificationStage.REPAIR.value

    land_repair(context, world.following_generation(affected=("CR-01",)))
    second = run_verify(context, audits=(audit("CR-01", revision=repaired_head()),))
    assert second.stage == BatchVerificationStage.REPAIR.value
    assert second.repairs_spent == 2


def test_an_unrequired_blocker_opens_no_repair_at_all(tmp_path: Path) -> None:
    """Advisory findings do not spend the budget."""
    context = native(tmp_path, world.delivering_generation())
    answer = run_verify(
        context,
        audits=(audit("CR-01", revision=first_head(), required=False),),
    )
    assert answer.merge_ready is True
    assert answer.repairs_spent == 0
    assert answer.exit is None
