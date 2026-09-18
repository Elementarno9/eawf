"""The verification cycle's stage machine, and what a head move costs it.

Two questions are asked of the pure models here. The first is whether the
stage walk is a walk at all: checking, then audit and review, then the
changes a blocker requires, then the bounded repair -- every step of it
on the one revision the cycle is bound to, with the moves that are not
edges refused rather than tolerated. The second is what happens when the
code moves underneath a Batch that had already cleared: the cycle must
come out of merge readiness, and it must say which criteria it lost and
which it kept, because "everything is stale now" throws away exactly the
evidence the affected/carried split exists to preserve.

Nothing here reads a clock, opens a socket or writes a file.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.batch_proof import (
    BATCH_VERIFICATION_EDGES,
    BLOCKING_VERDICTS,
    CANDIDATE_MUTATION_TOOLS,
    AuditVerdict,
    BatchAudit,
    BatchVerificationCycle,
    BatchVerificationStage,
    HeadInvalidation,
    ReviewerAttestation,
)
from eawf.kernel.delivery.integration import ConflictExit, ConflictExitKind
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind, canonical_digest
from eawf.kernel.runtime.semantic import SEMANTIC_TOOL_CATALOG, SemanticToolId
from eawf.kernel.state.epoch2.run import RunPurpose

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
REPOSITORY: Final = f"{CONTAINER}/repository/REP-EAWF"
BATCH: Final = f"{CONTAINER}/batch/BAT-0007"
OTHER_BATCH: Final = f"{CONTAINER}/batch/BAT-0008"
REVIEWER: Final = f"{CONTAINER}/run/RUN-00000020"
REPAIR_TASK: Final = f"{CONTAINER}/task/EAWF-0077"
OPERATOR_ACTION: Final = f"{CONTAINER}/pending-action/ACT-0003"

AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
SECOND_HEAD: Final = "1a" * 20
THIRD_HEAD: Final = "2b" * 20
TREE: Final = "3c" * 20
DIGEST: Final = canonical_digest("batch-proof")

#: The stage walk the cycle is required to make on one exact head.
WALK: Final = (
    BatchVerificationStage.AUDIT_REVIEW,
    BatchVerificationStage.CHANGES_REQUIRED,
    BatchVerificationStage.REPAIR,
)


def binding(
    *,
    generation: int = 2,
    head_sha: str = SECOND_HEAD,
    batch_ref: str = BATCH,
    tree_sha: str = TREE,
    ref_kind: RevisionRefKind = RevisionRefKind.INTEGRATION,
) -> RevisionBinding:
    """Return the revision one ordinal of a Batch is bound at."""
    return RevisionBinding(
        repository_ref=REPOSITORY,
        ref_kind=ref_kind,
        head_sha=head_sha,
        tree_sha=tree_sha,
        parent_sha=None,
        batch_ref=batch_ref,
        integration_generation=generation,
        manifest_digest=DIGEST,
        criteria_digest=DIGEST,
        policy_digest=DIGEST,
        environment_digest=None,
        bound_at=AT,
    )


def attestation(
    *,
    reviewed: RevisionBinding | None = None,
    run_ref: str = REVIEWER,
    purpose: RunPurpose = RunPurpose.REVIEW,
    tool_grants: tuple[SemanticToolId, ...] = (
        SemanticToolId.REPO_READ,
        SemanticToolId.DIFF_READ,
    ),
) -> ReviewerAttestation:
    """Return the attestation of one reviewing Run."""
    return ReviewerAttestation(
        run_ref=run_ref,
        purpose=purpose,
        reviewed_revision=binding() if reviewed is None else reviewed,
        capsule_digest=DIGEST,
        tool_grants=tool_grants,
    )


def audit(
    criterion_id: str,
    *,
    verdict: AuditVerdict = AuditVerdict.VERIFIED_TRUE,
    required: bool = True,
    audited: RevisionBinding | None = None,
    review: ReviewerAttestation | None = None,
    audit_id: str | None = None,
    batch_ref: str = BATCH,
) -> BatchAudit:
    """Return one criterion's independent verdict at a revision."""
    revision = binding() if audited is None else audited
    blocking = verdict in BLOCKING_VERDICTS
    derived = int(canonical_digest(criterion_id).removeprefix("sha256:")[:8], 16) % 1000000
    return BatchAudit(
        id=audit_id or f"BAU-{derived:06d}",
        batch_ref=batch_ref,
        criterion_id=criterion_id,
        required=required,
        verdict=verdict,
        audited_revision=revision,
        review=attestation(reviewed=revision) if review is None else review,
        finding=f"criterion {criterion_id} does not hold on the reviewed tree"
        if blocking
        else None,
        recorded_at=AT,
    )


def cycle(
    *,
    stage: BatchVerificationStage = BatchVerificationStage.CHECKING,
    audits: tuple[BatchAudit, ...] = (),
    head: RevisionBinding | None = None,
    repairs_spent: int = 0,
    repair_budget: int = 1,
    exit: ConflictExit | None = None,
) -> BatchVerificationCycle:
    """Return one Batch proving itself on an exact head."""
    return BatchVerificationCycle(
        batch_ref=BATCH,
        head=binding() if head is None else head,
        stage=stage,
        audits=audits,
        repairs_spent=repairs_spent,
        repair_budget=repair_budget,
        exit=exit,
    )


# ---- the stage walk on one exact head ---------------------------------------


def test_cycle_stage_walks_checking_through_repair_on_one_head() -> None:
    """The stage walks checking, audit review, changes required, repair."""
    walking = cycle(audits=(audit("CR-01", verdict=AuditVerdict.VERIFIED_FALSE),))
    assert walking.stage is BatchVerificationStage.CHECKING
    head = walking.head
    reached = []
    for stage in WALK:
        spend = stage is BatchVerificationStage.REPAIR
        walking = walking.advance(
            stage,
            exit=(
                ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=REPAIR_TASK) if spend else None
            ),
            spend_repair=spend,
        )
        reached.append(walking.stage)
        assert walking.head == head, "a stage move must not change the head"
    assert tuple(reached) == WALK
    assert walking.repairs_spent == 1


def test_cycle_advance_refuses_a_move_that_is_not_an_edge() -> None:
    """A stage move the machine does not list is refused, not tolerated."""
    with pytest.raises(ValueError, match="does not move to ready_to_merge on one head"):
        cycle().advance(BatchVerificationStage.READY_TO_MERGE)


def test_cycle_advance_refuses_leaving_a_head_moving_stage() -> None:
    """Repair and merge readiness have no same-head successor to advance to."""
    repaired = cycle(
        stage=BatchVerificationStage.REPAIR,
        repairs_spent=1,
        exit=ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=REPAIR_TASK),
    )
    with pytest.raises(ValueError, match="does not move to checking on one head"):
        repaired.advance(BatchVerificationStage.CHECKING)


def test_cycle_advance_to_ready_to_merge_carries_a_clear_audit() -> None:
    """A cycle whose required rows all cleared reaches merge readiness."""
    clear = cycle(audits=(audit("CR-01"), audit("CR-02"))).advance(
        BatchVerificationStage.AUDIT_REVIEW
    )
    assert clear.advance(BatchVerificationStage.READY_TO_MERGE).stage is (
        BatchVerificationStage.READY_TO_MERGE
    )


@pytest.mark.parametrize("verdict", sorted(BLOCKING_VERDICTS))
def test_cycle_refuses_merge_readiness_over_a_required_blocking_row(
    verdict: AuditVerdict,
) -> None:
    """One required false or unverified row blocks merge readiness."""
    blocked = cycle(audits=(audit("CR-01", verdict=verdict),)).advance(
        BatchVerificationStage.AUDIT_REVIEW
    )
    with pytest.raises(ValidationError, match="not ready to merge"):
        blocked.advance(BatchVerificationStage.READY_TO_MERGE)


def test_cycle_admits_merge_readiness_over_an_unrequired_blocking_row() -> None:
    """A row that is not required does not stop the Batch on its own."""
    advisory = audit("CR-02", verdict=AuditVerdict.VERIFIED_FALSE, required=False)
    assert not advisory.blocks
    reviewed = cycle(audits=(audit("CR-01"), advisory)).advance(BatchVerificationStage.AUDIT_REVIEW)
    cleared = reviewed.advance(BatchVerificationStage.READY_TO_MERGE)
    assert cleared.stage is BatchVerificationStage.READY_TO_MERGE
    assert cleared.blocking_criterion_ids == ()


def test_cycle_stage_exit_must_match_the_stage_that_opened_it() -> None:
    """A repair stage carries a repair exit and an operator stage an action."""
    with pytest.raises(ValidationError, match="requires a repair_task exit"):
        cycle(
            stage=BatchVerificationStage.REPAIR,
            repairs_spent=1,
            exit=ConflictExit(kind=ConflictExitKind.OPERATOR_DECISION, ref=OPERATOR_ACTION),
        )


def test_cycle_refuses_an_exit_on_a_stage_that_opened_none() -> None:
    """A stage that creates no way out may not record one."""
    with pytest.raises(ValidationError, match="opens no exit to record"):
        cycle(exit=ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=REPAIR_TASK))


def test_cycle_refuses_an_overspent_repair_budget() -> None:
    """A repair taken past the budget is refused rather than counted."""
    with pytest.raises(ValidationError, match="against a budget of 1"):
        cycle(
            stage=BatchVerificationStage.REPAIR,
            repairs_spent=2,
            repair_budget=1,
            exit=ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=REPAIR_TASK),
        )


def test_cycle_edges_name_every_stage_exactly_once() -> None:
    """Every stage has a row, so a new one cannot be silently unreachable."""
    assert set(BATCH_VERIFICATION_EDGES) == set(BatchVerificationStage)


# ---- what a head move costs --------------------------------------------------


def test_cycle_rebase_drops_merge_readiness_and_names_its_invalidations() -> None:
    """A head move unseats a merge-ready Batch and names what it lost."""
    reviewed = cycle(audits=(audit("CR-01"), audit("CR-02"))).advance(
        BatchVerificationStage.AUDIT_REVIEW
    )
    ready = reviewed.advance(BatchVerificationStage.READY_TO_MERGE)
    assert ready.stage is BatchVerificationStage.READY_TO_MERGE
    moved, invalidation = ready.rebase(
        head=binding(generation=3, head_sha=THIRD_HEAD), invalidated_criterion_ids={"CR-01"}
    )
    assert moved.stage is BatchVerificationStage.CHECKING
    assert invalidation.unseated_merge_readiness is True
    assert invalidation.dropped_from is BatchVerificationStage.READY_TO_MERGE
    assert invalidation.invalidated_criterion_ids == ("CR-01",)
    assert invalidation.carried_criterion_ids == ("CR-02",)
    assert [item.criterion_id for item in moved.audits] == ["CR-02"]


def test_cycle_rebase_keeps_an_uninvalidated_row_at_the_revision_it_was_taken_on() -> None:
    """A carried verdict keeps its own binding rather than being re-forged."""
    ready = cycle(audits=(audit("CR-02"),))
    moved, _invalidation = ready.rebase(
        head=binding(generation=3, head_sha=THIRD_HEAD), invalidated_criterion_ids=()
    )
    carried = moved.audits[0]
    assert carried.audited_revision.head_sha == SECOND_HEAD
    assert carried.generation == 2
    assert moved.head.head_sha == THIRD_HEAD


def test_cycle_rebase_names_nothing_when_the_move_invalidated_nothing() -> None:
    """A move that names no criterion is not a blanket reset of the rows."""
    moved, invalidation = cycle(audits=(audit("CR-01"), audit("CR-02"))).rebase(
        head=binding(generation=3, head_sha=THIRD_HEAD), invalidated_criterion_ids=()
    )
    assert invalidation.invalidated_criterion_ids == ()
    assert invalidation.carried_criterion_ids == ("CR-01", "CR-02")
    assert len(moved.audits) == 2


def test_cycle_rebase_carries_the_repair_budget_across_the_move() -> None:
    """A repair spent before the move stays spent after it."""
    repaired = cycle(
        stage=BatchVerificationStage.REPAIR,
        repairs_spent=1,
        exit=ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=REPAIR_TASK),
    )
    moved, _invalidation = repaired.rebase(
        head=binding(generation=3, head_sha=THIRD_HEAD), invalidated_criterion_ids=()
    )
    assert moved.repairs_spent == 1
    assert moved.repair_available is False
    assert moved.exit is None


def test_cycle_rebase_refuses_a_head_that_does_not_move_forward() -> None:
    """A rebase onto the ordinal it already stands on is refused."""
    with pytest.raises(ValidationError, match="does not follow 2"):
        cycle().rebase(head=binding(generation=2), invalidated_criterion_ids=())


def test_head_invalidation_refuses_a_criterion_named_both_ways() -> None:
    """A criterion cannot be reported invalidated and carried at once."""
    with pytest.raises(ValidationError, match="both invalidated and carried"):
        HeadInvalidation(
            batch_ref=BATCH,
            dropped_from=BatchVerificationStage.READY_TO_MERGE,
            from_generation=2,
            to_generation=3,
            invalidated_criterion_ids=("CR-01",),
            carried_criterion_ids=("CR-01",),
        )


# ---- the rows the cycle holds ------------------------------------------------


def test_audit_requires_its_auditor_and_reviewer_to_bind_one_head() -> None:
    """A verdict about one tree reviewed against another does not validate."""
    with pytest.raises(ValidationError, match="disagree on head_sha"):
        audit("CR-01", review=attestation(reviewed=binding(head_sha=THIRD_HEAD)))


def test_audit_requires_the_reviewer_to_bind_the_same_ordinal() -> None:
    """A review taken on another generation is not this row's review."""
    with pytest.raises(ValidationError, match="integration_generation"):
        audit("CR-01", review=attestation(reviewed=binding(generation=3, head_sha=SECOND_HEAD)))


@pytest.mark.parametrize("verdict", sorted(BLOCKING_VERDICTS))
def test_audit_blocking_verdict_requires_a_finding(verdict: AuditVerdict) -> None:
    """A row that does not clear its criterion must say what is wrong."""
    with pytest.raises(ValidationError, match="requires a finding"):
        BatchAudit(
            id="BAU-000001",
            batch_ref=BATCH,
            criterion_id="CR-01",
            required=True,
            verdict=verdict,
            audited_revision=binding(),
            review=attestation(),
            finding=None,
            recorded_at=AT,
        )


def test_audit_clear_verdict_refuses_a_finding() -> None:
    """A cleared criterion cannot come with a complaint attached."""
    with pytest.raises(ValidationError, match="requires a finding"):
        BatchAudit(
            id="BAU-000001",
            batch_ref=BATCH,
            criterion_id="CR-01",
            required=True,
            verdict=AuditVerdict.VERIFIED_TRUE,
            audited_revision=binding(),
            review=attestation(),
            finding="something is off",
            recorded_at=AT,
        )


def test_audit_refuses_a_row_filed_under_another_batch() -> None:
    """A verdict taken on one Batch cannot be filed against another."""
    with pytest.raises(ValidationError, match="was taken on"):
        audit("CR-01", batch_ref=OTHER_BATCH)


@pytest.mark.parametrize("tool", sorted(CANDIDATE_MUTATION_TOOLS))
def test_attestation_refuses_a_candidate_mutation_grant(tool: SemanticToolId) -> None:
    """A reviewer granted a tool that writes the tree it judges is refused."""
    assert SEMANTIC_TOOL_CATALOG[tool].requires_mutating_task is True
    with pytest.raises(ValidationError, match="writes the tree its verdict is about"):
        attestation(tool_grants=(SemanticToolId.REPO_READ, tool))


def test_attestation_refuses_a_mutating_purpose() -> None:
    """A Run dispatched to change a repository cannot independently review it."""
    with pytest.raises(ValidationError, match="changes a repository"):
        attestation(purpose=RunPurpose.IMPLEMENT)


def test_attestation_refuses_a_repeated_tool_grant() -> None:
    """A grant listed twice is a defect rather than a stronger grant."""
    with pytest.raises(ValidationError, match="repeats a tool grant"):
        attestation(tool_grants=(SemanticToolId.REPO_READ, SemanticToolId.REPO_READ))


def test_attestation_admits_a_read_only_reviewer() -> None:
    """The grants a reviewer legitimately needs are admitted unchanged."""
    granted = (
        SemanticToolId.REPO_READ,
        SemanticToolId.REPO_SEARCH,
        SemanticToolId.DIFF_READ,
        SemanticToolId.EAWF_STATE_QUERY,
        SemanticToolId.SUBMIT_REPORT,
    )
    assert attestation(tool_grants=granted).tool_grants == granted


def test_cycle_refuses_two_verdicts_for_one_criterion() -> None:
    """A criterion audited twice would make the blocking question ambiguous."""
    with pytest.raises(ValidationError, match="more than one verdict"):
        cycle(
            audits=(
                audit("CR-01", audit_id="BAU-000001"),
                audit("CR-01", audit_id="BAU-000002", verdict=AuditVerdict.VERIFIED_FALSE),
            )
        )


def test_cycle_refuses_a_row_taken_past_its_head() -> None:
    """A verdict about a newer tree than the cycle is bound to is refused."""
    with pytest.raises(ValidationError, match="past the head 2"):
        cycle(audits=(audit("CR-01", audited=binding(generation=3, head_sha=THIRD_HEAD)),))


def test_cycle_refuses_a_head_bound_on_another_ref_kind() -> None:
    """A cycle head is the integrated revision, not a candidate branch."""
    with pytest.raises(ValidationError, match="bound on the integration ref"):
        cycle(head=binding(ref_kind=RevisionRefKind.CANDIDATE))


def test_cycle_refuses_a_head_of_another_batch() -> None:
    """A cycle cannot prove itself on a revision of somebody else's Batch."""
    with pytest.raises(ValidationError, match="instead of"):
        cycle(head=binding(batch_ref=OTHER_BATCH))


def test_cycle_audit_of_returns_none_for_an_unaudited_criterion() -> None:
    """Looking up a criterion nothing has answered yields nothing."""
    held = cycle(audits=(audit("CR-01"),))
    assert held.audit_of("CR-01") is not None
    assert held.audit_of("CR-02") is None


def test_cycle_with_no_audits_holds_nothing_and_blocks_on_nothing() -> None:
    """An empty cycle is legal: it has simply not been audited yet."""
    empty = cycle()
    assert empty.audits == ()
    assert empty.blocking_audits == ()
    assert empty.blocking_criterion_ids == ()
    assert empty.repair_available is True
