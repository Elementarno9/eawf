"""DEL-032: a conflicting candidate leaves a frame with a way out.

The suite drives a two-hunk conflict through the serial integrator and
then through the blocker, so what is asserted is the record a real block
produces rather than one assembled by hand: both sides of both hunks,
each side's typed authority, and the one typed exit resolution lands at.

Each exit kind gets its own case, because the route is what makes a
frame usable. The cause is read off the attempt -- a base that has moved
rebases, a collision on a surface two plans both claimed is an operator's
call, and anything else is the candidate's own text -- so a case here
fails when a route is rewired rather than when a message is reworded.

No canonical ref moves. The run that blocks is asserted to have authored
no commit, taken no lock and left the Batch with the generation history
it started with, which is the property the whole read-only frame rests
on: there is nothing to undo because nothing was done.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.integration import (
    EXIT_REF_KINDS,
    AgentAuthority,
    ConflictExit,
    ConflictExitKind,
    ConflictFile,
    ConflictHunk,
    ConflictSide,
    IntegrationAttempt,
    IntegrationAttemptStatus,
    IntegrationConflict,
    IntegrationFailureKind,
    IntegrationGenerationLedger,
    PrincipalAuthority,
)
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind
from eawf.kernel.identity import EntityKind, QualifiedUrn, parse_qualified_urn
from eawf.kernel.runtime.candidate import CandidateBundle, SealCheck, candidate_identity
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.runtime.integration.apply import (
    ApplyDisposition,
    CandidateApplication,
    integration_order,
)
from eawf.runtime.integration.generations import IllegalIntegrationTransitionError
from eawf.runtime.integration.recovery import (
    DeliveredRevision,
    IntegrationOutcomeKind,
    integrate_batch,
    plan_deliveries,
)
from eawf.workflow.integration.conflict import (
    EXIT_BY_CAUSE,
    ConflictCause,
    ExitRouteError,
    block_attempt,
    compile_exit_routes,
    conflict_cause,
)

pytestmark = pytest.mark.integration


CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
REPOSITORY: Final = f"{CONTAINER}/repository/REP-EAWF"
BATCH: Final = f"{CONTAINER}/batch/BAT-0001"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"
TASK: Final = f"{CONTAINER}/task/EAWF-0001"
REPAIR_TASK: Final = f"{CONTAINER}/task/EAWF-0900"
REBASE_TASK: Final = f"{CONTAINER}/task/EAWF-0901"
DECISION: Final = f"{CONTAINER}/pending-action/ACT-0001"
EVIDENCE: Final = f"{CONTAINER}/evidence/EVD-0001"
BASE_COMMIT: Final = "9f" * 20
MOVED_COMMIT: Final = "7e" * 20
BRANCH: Final = "integration/BAT-0001"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LATER: Final = AT + timedelta(minutes=5)
DIGEST: Final = f"sha256:{'c' * 64}"
CONFLICT_PATH: Final = "src/eawf-0001.py"


def urn(value: str) -> QualifiedUrn:
    """Return one parsed entity URN."""
    return parse_qualified_urn(value)


def binding(*, generation: int = 1, head: str = BASE_COMMIT) -> RevisionBinding:
    """Return one Batch revision binding."""
    return RevisionBinding(
        repository_ref=REPOSITORY,
        ref_kind=RevisionRefKind.INTEGRATION,
        head_sha=head,
        tree_sha="3c" * 20,
        parent_sha=None,
        batch_ref=BATCH,
        integration_generation=generation,
        manifest_digest=DIGEST,
        criteria_digest=DIGEST,
        policy_digest=DIGEST,
        environment_digest=None,
        bound_at=AT,
    )


def attempt(**overrides: Any) -> IntegrationAttempt:
    """Return one attempt that is applying a candidate."""
    fields: dict[str, Any] = {
        "id": "INA-000001",
        "operation_attempt_id": "OPR-INT-000001",
        "batch_ref": BATCH,
        "task_ref": TASK,
        "candidate_bundle_id": "CB-00000001",
        "generation": 2,
        "source_base": binding(),
        "selected_batch_base": binding(),
        "candidate_patch_digest": DIGEST,
        "candidate_tree_digest": DIGEST,
        "changed_paths": (CONFLICT_PATH, "src/other.py"),
        "idempotency_key": "integrate-01",
        "status": IntegrationAttemptStatus.APPLYING,
        "requested_at": AT,
        "updated_at": AT,
    }
    fields.update(overrides)
    return IntegrationAttempt.model_validate(fields)


def side(*, sha: str, line: str) -> ConflictSide:
    """Return one side of a hunk, written by the Batch's agent."""
    return ConflictSide(
        authority=AgentAuthority(kind="agent", batch_ref=BATCH),
        at=AT,
        sha=sha,
        lines=(line,),
    )


def two_hunk_file(path: str = CONFLICT_PATH) -> ConflictFile:
    """Return one conflicting file carrying two numbered hunks."""
    return ConflictFile(
        path=path,
        hunks=(
            ConflictHunk(
                index=1,
                ours=side(sha="1" * 40, line="ours first"),
                theirs=side(sha="2" * 40, line="theirs first"),
            ),
            ConflictHunk(
                index=2,
                ours=side(sha="1" * 40, line="ours second"),
                theirs=side(sha="2" * 40, line="theirs second"),
            ),
        ),
    )


def exits(kind: ConflictExitKind) -> QualifiedUrn:
    """Return the record each exit kind lands at."""
    return {
        ConflictExitKind.REPAIR_TASK: urn(REPAIR_TASK),
        ConflictExitKind.REBASE_TASK: urn(REBASE_TASK),
        ConflictExitKind.OPERATOR_DECISION: urn(DECISION),
    }[kind]


def block(attempt_to_block: IntegrationAttempt, **overrides: Any) -> Any:
    """Block *attempt_to_block* on the two-hunk conflict."""
    fields: dict[str, Any] = {
        "conflict_id": "INC-000001",
        "repository_ref": REPOSITORY,
        "branch": BRANCH,
        "ahead": 2,
        "behind": 1,
        "files": (two_hunk_file(),),
        "diagnostic_ref": EVIDENCE,
        "exits": exits,
        "at": LATER,
    }
    fields.update(overrides)
    return block_attempt(attempt_to_block, **fields)


# ---- the frame a block leaves behind ------------------------------------------


def test_a_two_hunk_conflict_keeps_both_sides_of_every_hunk() -> None:
    """Every hunk carries both sides, and neither is chosen or retracted."""
    blocked = block(attempt())

    conflict = blocked.conflict
    assert [file.path for file in conflict.files] == [CONFLICT_PATH]
    hunks = conflict.files[0].hunks
    assert [hunk.index for hunk in hunks] == [1, 2]
    assert [hunk.ours.lines[0] for hunk in hunks] == ["ours first", "ours second"]
    assert [hunk.theirs.lines[0] for hunk in hunks] == ["theirs first", "theirs second"]
    assert all(hunk.ours.sha != hunk.theirs.sha for hunk in hunks)


def test_every_side_names_a_typed_authority_and_never_a_display_name() -> None:
    """A side's author is a typed reference the record can be routed on."""
    blocked = block(attempt())

    for hunk in blocked.conflict.files[0].hunks:
        for hunk_side in (hunk.ours, hunk.theirs):
            assert isinstance(hunk_side.authority, AgentAuthority | PrincipalAuthority)
            assert hunk_side.authority.kind in {"agent", "principal"}


def test_blocking_moves_the_attempt_to_its_terminal_conflicted_state() -> None:
    """The attempt ends blocked on a conflict and names its diagnostic."""
    blocked = block(attempt())

    assert blocked.attempt.status is IntegrationAttemptStatus.BLOCKED
    assert blocked.attempt.failure_kind is IntegrationFailureKind.CONFLICT
    assert blocked.attempt.terminal_at == LATER
    assert str(blocked.attempt.diagnostic_ref) == EVIDENCE


def test_the_frame_counts_the_candidate_against_the_selected_base() -> None:
    """Ahead and behind are the workspace's counts, carried verbatim."""
    blocked = block(attempt(), ahead=7, behind=3)

    assert (blocked.conflict.ahead, blocked.conflict.behind) == (7, 3)
    assert blocked.conflict.branch == BRANCH
    assert blocked.conflict.cleared_at is None


# ---- the typed exit, one per cause --------------------------------------------


def test_a_candidate_whose_own_text_conflicts_exits_to_a_repair_task() -> None:
    """Neither the base nor a claim moved, so the candidate is repaired."""
    blocked = block(attempt())

    assert conflict_cause(blocked.attempt) is ConflictCause.CANDIDATE_TEXT
    assert blocked.conflict.exit.kind is ConflictExitKind.REPAIR_TASK
    assert str(blocked.conflict.exit.ref) == REPAIR_TASK


def test_a_candidate_written_against_a_moved_base_exits_to_a_rebase_task() -> None:
    """The base the candidate expected is not the one it was applied over."""
    moved = attempt(selected_batch_base=binding(head=MOVED_COMMIT))

    blocked = block(moved)

    assert conflict_cause(moved) is ConflictCause.BASE_MOVED
    assert blocked.conflict.exit.kind is ConflictExitKind.REBASE_TASK
    assert str(blocked.conflict.exit.ref) == REBASE_TASK


def test_a_collision_on_a_claimed_surface_exits_to_an_operator_decision() -> None:
    """Two plans claimed the surface, so neither side is the wrong one."""
    claimed = attempt(conflict_claim_ids=("surface.parser",))

    blocked = block(claimed)

    assert conflict_cause(claimed) is ConflictCause.CLAIMED_SURFACE
    assert blocked.conflict.exit.kind is ConflictExitKind.OPERATOR_DECISION
    assert str(blocked.conflict.exit.ref) == DECISION


def test_every_exit_kind_is_reached_by_exactly_one_cause() -> None:
    """The routing table is a bijection, so no exit is a dead promise."""
    assert set(EXIT_BY_CAUSE) == set(ConflictCause)
    assert set(EXIT_BY_CAUSE.values()) == set(ConflictExitKind)
    assert len(set(EXIT_BY_CAUSE.values())) == len(EXIT_BY_CAUSE)


def test_compile_exit_routes_refuses_a_cause_that_leads_nowhere() -> None:
    """Error path: a frame with no way out invites a hand edit."""
    partial = {ConflictCause.BASE_MOVED: ConflictExitKind.REBASE_TASK}

    with pytest.raises(ExitRouteError, match="no exit declared"):
        compile_exit_routes(partial)


def test_compile_exit_routes_refuses_an_exit_no_cause_reaches() -> None:
    """Error path: an unreachable exit is a promise nothing keeps."""
    collapsed = dict.fromkeys(ConflictCause, ConflictExitKind.REPAIR_TASK)

    with pytest.raises(ExitRouteError, match="no conflict cause reaches"):
        compile_exit_routes(collapsed)


def test_an_exit_must_reference_the_entity_kind_its_route_requires() -> None:
    """Error path: a repair exit pointing at a pending action is refused."""
    assert EXIT_REF_KINDS[ConflictExitKind.REPAIR_TASK] is EntityKind.TASK

    with pytest.raises(ValidationError, match="must reference a task"):
        ConflictExit(kind=ConflictExitKind.REPAIR_TASK, ref=urn(DECISION))


def test_blocking_refuses_an_exit_reference_of_the_wrong_kind() -> None:
    """Error path: a minted reference of another kind never reaches a frame."""
    with pytest.raises(ValidationError, match="must reference a task"):
        block(attempt(), exits=lambda _kind: urn(DECISION))


# ---- refusals the frame itself makes ------------------------------------------


def test_blocking_refuses_a_conflict_that_names_no_file() -> None:
    """Boundary: a frame with nothing in it is not a conflict."""
    with pytest.raises(ValueError, match="no conflicting file"):
        block(attempt(), files=())


def test_blocking_refuses_a_path_the_attempt_never_changed() -> None:
    """Error path: a conflict outside the change set describes another run."""
    with pytest.raises(ValueError, match="are not changed by"):
        block(attempt(), files=(two_hunk_file(path="src/untouched.py"),))


def test_blocking_refuses_an_attempt_that_is_already_terminal() -> None:
    """Error path: a terminal attempt takes no further edge."""
    already = attempt(
        status=IntegrationAttemptStatus.FAILED,
        failure_kind=IntegrationFailureKind.APPLY_ERROR,
        terminal_at=AT,
    )

    with pytest.raises(IllegalIntegrationTransitionError):
        block(already)


def test_a_cleared_frame_states_when_a_successor_integrated() -> None:
    """Boundary: the record is kept and dated rather than removed."""
    blocked = block(attempt())

    cleared = blocked.conflict.model_copy(update={"cleared_at": LATER})

    assert IntegrationConflict.model_validate(cleared.model_dump(mode="json")).cleared_at == LATER


# ---- a blocked run moves no canonical ref -------------------------------------


def bundle(*, task: str = "EAWF-0001", digest: str = "a") -> CandidateBundle:
    """Return one sealed candidate for *task*."""
    task_urn = f"{CONTAINER}/task/{task}"
    resulting = f"sha256:{digest * 64}"
    return CandidateBundle.model_validate(
        {
            "candidate_ref": candidate_identity(task_ref=task_urn, resulting_tree_digest=resulting),
            "run_ref": RUN,
            "task_ref": task_urn,
            "submission_ref": "artifact://candidate/executor-success",
            "report_digest": f"sha256:{'b' * 64}",
            "verdict": AgentReportVerdict.PASS,
            "changed_paths": (CONFLICT_PATH,),
            "resulting_tree_digest": resulting,
            "base_commit": BASE_COMMIT,
            "workspace_generation": 1,
            "checks_passed": tuple(SealCheck),
            "sealed_at": AT,
        }
    )


class ConflictingWorkspace:
    """A workspace whose first candidate always conflicts."""

    def __init__(self) -> None:
        """Start with nothing materialized and nothing committed."""
        self.commits: list[str] = []
        self.materialized: list[str] = []

    def materialize(self, *, base_commit: str) -> str:
        """Record the materialization and return the base unchanged."""
        self.materialized.append(base_commit)
        return base_commit

    def apply(self, bundle_to_apply: CandidateBundle) -> CandidateApplication:
        """Report the conflict the frame is written from."""
        return CandidateApplication(
            candidate_ref=bundle_to_apply.candidate_ref,
            disposition=ApplyDisposition.CONFLICTED,
            conflict_files=(two_hunk_file(),),
            ahead=2,
            behind=1,
        )

    def commit(self, delivery: object) -> DeliveredRevision:
        """Fail loudly: a blocked run must never author a commit."""
        raise AssertionError("a blocked integration authored a commit")


class RefusingLock:
    """A lock that fails loudly: a blocked run must never take it."""

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Refuse to be taken."""
        raise AssertionError("a blocked integration took the state lock")
        yield


class UntouchedStore:
    """A generation history that fails loudly on any write."""

    def __init__(self) -> None:
        """Start with a Batch that has no generation."""
        self.ledger = IntegrationGenerationLedger(batch_ref=BATCH)

    def read(self) -> IntegrationGenerationLedger:
        """Return the untouched history."""
        return self.ledger

    def write(self, ledger: IntegrationGenerationLedger) -> None:
        """Fail loudly: a blocked run selects no generation."""
        raise AssertionError("a blocked integration selected a generation")


def test_a_blocked_integration_leaves_the_batch_head_where_it_was() -> None:
    """The whole read-only frame rests on nothing having been done."""
    candidate = bundle()
    ordered = integration_order([candidate])
    plans = plan_deliveries(
        ordered,
        repository_ref=REPOSITORY,
        batch_ref=BATCH,
        source_base=binding(),
        target_base=binding(),
        parent_generation_id=None,
        subjects={candidate.candidate_ref: "deliver the candidate"},
        batch_subject="deliver the batch",
        unit="batch",
        task_reference="trailer",
    )
    workspace = ConflictingWorkspace()
    store = UntouchedStore()

    outcome = integrate_batch(
        plans[0], workspace=workspace, lock=RefusingLock(), store=store, now=AT
    )

    assert outcome.kind is IntegrationOutcomeKind.BLOCKED
    assert outcome.revision is None and outcome.generation is None and outcome.swap is None
    assert workspace.commits == []
    assert store.ledger.head is None
    assert workspace.materialized == [BASE_COMMIT]


def test_the_frame_written_from_a_blocked_run_describes_that_run() -> None:
    """The record the driver's block produces validates against its attempt."""
    candidate = bundle()
    ordered = integration_order([candidate])
    plans = plan_deliveries(
        ordered,
        repository_ref=REPOSITORY,
        batch_ref=BATCH,
        source_base=binding(),
        target_base=binding(),
        parent_generation_id=None,
        subjects={candidate.candidate_ref: "deliver the candidate"},
        batch_subject="deliver the batch",
        unit="batch",
        task_reference="trailer",
    )
    outcome = integrate_batch(
        plans[0],
        workspace=ConflictingWorkspace(),
        lock=RefusingLock(),
        store=UntouchedStore(),
        now=AT,
    )

    blocked = block(attempt(changed_paths=(CONFLICT_PATH,)), files=outcome.applied.conflict_files)

    assert blocked.conflict.attempt_id == blocked.attempt.id
    assert outcome.applied.blocked_on == candidate.candidate_ref
    assert len(blocked.conflict.files[0].hunks) == 2
