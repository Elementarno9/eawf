"""DEL-013: slow work outside the lock, and a short swap inside it.

Nothing here measures time, and nothing sleeps. The lock discipline is
asserted structurally: the workspace and the generation store are handed
the same recorder the lock writes to, and each of them reads whether the
lock is held at the instant it is called. What comes out is a list of
what ran and whether the lock was up while it ran, which is a fact about
the call graph rather than about how fast anything happened.

The declared table is the oracle. The phases the table calls unlocked
must all have been observed with the lock down, and the one phase it
calls locked must be the only thing observed with it up -- so adding a
phase, or moving one across the line, changes an answer here instead of
passing quietly.

The swap's two other properties are driven directly: presenting the same
generation twice writes once, and a generation prepared on a head that
has since moved is refused rather than merged over.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Final

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.integration import (
    AgentAuthority,
    ConflictFile,
    ConflictHunk,
    ConflictSide,
    IntegrationGeneration,
    IntegrationGenerationLedger,
)
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind
from eawf.kernel.runtime.candidate import CandidateBundle, SealCheck, candidate_identity
from eawf.kernel.state.enums import AgentReportVerdict
from eawf.runtime.integration.apply import (
    ApplyDisposition,
    CandidateApplication,
    IntegrationRefusal,
    IntegrationRefusedError,
    integration_order,
)
from eawf.runtime.integration.recovery import (
    LOCKED_PHASE,
    PHASE_LOCKS,
    PHASE_ORDER,
    UNLOCKED_PHASES,
    CasDisposition,
    DeliveredRevision,
    IntegrationOutcome,
    IntegrationOutcomeKind,
    IntegrationPhase,
    IntegrationPlan,
    LockDiscipline,
    PhaseLockTableError,
    build_integration_generation,
    compile_phase_locks,
    generation_record_key,
    integrate_batch,
    integration_generation_id,
    plan_deliveries,
    select_delivered_generation,
)

pytestmark = pytest.mark.integration


CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
REPOSITORY: Final = f"{CONTAINER}/repository/REP-EAWF"
BATCH: Final = f"{CONTAINER}/batch/BAT-0001"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"
BASE_COMMIT: Final = "9f" * 20
DELIVERED_SHA: Final = "1a" * 20
DELIVERED_TREE: Final = "2b" * 20
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
LATER: Final = AT + timedelta(hours=1)
DIGEST: Final = f"sha256:{'c' * 64}"


def bundle(*, task: str = "EAWF-0001", digest: str = "a") -> CandidateBundle:
    """Return one sealed candidate for *task*."""
    urn = f"{CONTAINER}/task/{task}"
    resulting = f"sha256:{digest * 64}"
    return CandidateBundle.model_validate(
        {
            "candidate_ref": candidate_identity(task_ref=urn, resulting_tree_digest=resulting),
            "run_ref": RUN,
            "task_ref": urn,
            "submission_ref": "artifact://candidate/executor-success",
            "report_digest": f"sha256:{'b' * 64}",
            "verdict": AgentReportVerdict.PASS,
            "changed_paths": (f"src/{task.lower()}.py",),
            "resulting_tree_digest": resulting,
            "base_commit": BASE_COMMIT,
            "workspace_generation": 1,
            "checks_passed": tuple(SealCheck),
            "sealed_at": AT,
        }
    )


def binding(*, generation: int = 1, head: str = BASE_COMMIT) -> RevisionBinding:
    """Return the Batch base binding at *generation*."""
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


def plans_for(
    *bundles: CandidateBundle,
    unit: str = "batch",
    target: RevisionBinding | None = None,
    parent: str | None = None,
) -> tuple[IntegrationPlan, ...]:
    """Return the planned deliveries of *bundles*."""
    ordered = integration_order(bundles)
    base = target if target is not None else binding()
    return plan_deliveries(
        ordered,
        repository_ref=REPOSITORY,
        batch_ref=BATCH,
        source_base=binding(),
        target_base=base,
        parent_generation_id=parent,
        subjects={item.candidate_ref: f"deliver {item.task_ref.entity_key}" for item in ordered},
        batch_subject="deliver the batch",
        unit=unit,  # type: ignore[arg-type]
        task_reference="trailer",
    )


def revision(*, head: str = DELIVERED_SHA, parent: str | None = BASE_COMMIT) -> DeliveredRevision:
    """Return the commit object a workspace reports for a delivery."""
    return DeliveredRevision(
        head_sha=head,
        tree_sha=DELIVERED_TREE,
        parent_sha=parent,
        patch_digest=DIGEST,
        diff_digest=DIGEST,
        tree_digest=DIGEST,
    )


def conflict_file(path: str = "src/eawf-0001.py") -> ConflictFile:
    """Return a one-hunk conflict frame for *path*."""
    side = ConflictSide(
        authority=AgentAuthority(kind="agent", batch_ref=BATCH),
        at=AT,
        sha="1" * 40,
        lines=("ours",),
    )
    return ConflictFile(
        path=path,
        hunks=(
            ConflictHunk(
                index=1,
                ours=side,
                theirs=side.model_copy(update={"sha": "2" * 40, "lines": ("theirs",)}),
            ),
        ),
    )


class Recorder:
    """What ran, and whether the canonical state lock was up while it ran."""

    def __init__(self) -> None:
        """Start with the lock down and nothing observed."""
        self.held = False
        self.holds = 0
        self.seen: list[tuple[str, bool]] = []

    def note(self, what: str) -> None:
        """Record *what* against the lock state at this exact instant."""
        self.seen.append((what, self.held))

    @property
    def under_lock(self) -> list[str]:
        """Return what was observed while the lock was held, in order."""
        return [what for what, held in self.seen if held]

    @property
    def released(self) -> list[str]:
        """Return what was observed with the lock down, in order."""
        return [what for what, held in self.seen if not held]


class RecordingLock:
    """A state lock that tells the recorder when it is up."""

    def __init__(self, recorder: Recorder) -> None:
        """Bind the lock to the recorder every collaborator reads."""
        self._recorder = recorder

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Raise the lock for the block and lower it afterwards."""
        self._recorder.held = True
        self._recorder.holds += 1
        try:
            yield
        finally:
            self._recorder.held = False


class RecordingWorkspace:
    """A workspace that reads the lock at the instant it is called."""

    def __init__(self, recorder: Recorder, *, conflict_on: str | None = None) -> None:
        """Bind the workspace to the recorder and the candidate to block."""
        self._recorder = recorder
        self._conflict_on = conflict_on
        self.commits: list[str] = []

    def materialize(self, *, base_commit: str) -> str:
        """Record the materialization and return the base unchanged."""
        self._recorder.note(IntegrationPhase.MATERIALIZE.value)
        return base_commit

    def apply(self, bundle_to_apply: CandidateBundle) -> CandidateApplication:
        """Record the application and conflict on the nominated candidate."""
        self._recorder.note(IntegrationPhase.APPLY.value)
        if bundle_to_apply.candidate_ref == self._conflict_on:
            return CandidateApplication(
                candidate_ref=bundle_to_apply.candidate_ref,
                disposition=ApplyDisposition.CONFLICTED,
                conflict_files=(conflict_file(),),
                ahead=1,
                behind=2,
            )
        return CandidateApplication(
            candidate_ref=bundle_to_apply.candidate_ref, disposition=ApplyDisposition.APPLIED
        )

    def commit(self, delivery: object) -> DeliveredRevision:
        """Record the commit and return the object it produced."""
        self._recorder.note(IntegrationPhase.COMMIT.value)
        self.commits.append(str(delivery))
        return revision(head=f"{len(self.commits):02d}" * 20)


class RecordingStore:
    """A generation store that reads the lock on every read and write."""

    def __init__(self, recorder: Recorder, ledger: IntegrationGenerationLedger) -> None:
        """Bind the store to the recorder and its starting history."""
        self._recorder = recorder
        self.ledger = ledger
        self.writes = 0

    def read(self) -> IntegrationGenerationLedger:
        """Record the read and return the history."""
        self._recorder.note(f"{LOCKED_PHASE.value}:read")
        return self.ledger

    def write(self, ledger: IntegrationGenerationLedger) -> None:
        """Record the write and keep the new history."""
        self._recorder.note(f"{LOCKED_PHASE.value}:write")
        self.writes += 1
        self.ledger = ledger


def empty_ledger() -> IntegrationGenerationLedger:
    """Return a Batch with no generation yet."""
    return IntegrationGenerationLedger(batch_ref=BATCH)


def run(
    plans: Sequence[IntegrationPlan], *, conflict_on: str | None = None
) -> tuple[Recorder, RecordingWorkspace, RecordingStore, IntegrationOutcome]:
    """Integrate the first plan and return every collaborator plus the outcome."""
    recorder = Recorder()
    workspace = RecordingWorkspace(recorder, conflict_on=conflict_on)
    store = RecordingStore(recorder, empty_ledger())
    outcome = integrate_batch(
        plans[0],
        workspace=workspace,
        lock=RecordingLock(recorder),
        store=store,
        now=AT,
    )
    return recorder, workspace, store, outcome


# ---- the declared lock discipline ---------------------------------------------


def test_phase_lock_table_answers_for_every_phase_once() -> None:
    """The table is total and the order lists each phase exactly once."""
    assert set(PHASE_LOCKS) == set(IntegrationPhase)
    assert tuple(dict.fromkeys(PHASE_ORDER)) == PHASE_ORDER
    assert set(PHASE_ORDER) == set(IntegrationPhase)
    assert LOCKED_PHASE is IntegrationPhase.SELECT
    assert IntegrationPhase.SELECT not in UNLOCKED_PHASES


def test_compile_phase_locks_refuses_a_phase_with_no_discipline() -> None:
    """Error path: an undeclared phase would run wherever it is called."""
    partial = dict.fromkeys(PHASE_ORDER[:-1], LockDiscipline.UNLOCKED)

    with pytest.raises(PhaseLockTableError, match="no lock discipline"):
        compile_phase_locks(partial)


def test_compile_phase_locks_refuses_two_locked_phases() -> None:
    """Error path: a second locked phase is slow work under the lock."""
    two = dict.fromkeys(PHASE_ORDER, LockDiscipline.STATE_LOCKED)

    with pytest.raises(PhaseLockTableError, match="exactly one phase"):
        compile_phase_locks(two)


def test_compile_phase_locks_refuses_a_table_that_locks_nothing() -> None:
    """Boundary: a swap that takes no lock is not a swap."""
    none = dict.fromkeys(PHASE_ORDER, LockDiscipline.UNLOCKED)

    with pytest.raises(PhaseLockTableError, match="found none"):
        compile_phase_locks(none)


# ---- DEL-013: the slow phases run with the lock released ----------------------


def test_every_unlocked_phase_is_observed_with_the_lock_released() -> None:
    """Materialization, application and commit all run with the lock down."""
    plans = plans_for(bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b"))

    recorder, _workspace, _store, outcome = run(plans)

    assert outcome.kind is IntegrationOutcomeKind.DELIVERED
    assert recorder.released == [
        IntegrationPhase.MATERIALIZE.value,
        IntegrationPhase.APPLY.value,
        IntegrationPhase.APPLY.value,
        IntegrationPhase.COMMIT.value,
    ]
    assert {phase.value for phase in UNLOCKED_PHASES} == set(recorder.released)


def test_the_locked_phase_is_one_read_and_one_write() -> None:
    """The whole effect under the lock is a compare-and-swap, nothing else."""
    plans = plans_for(bundle(task="EAWF-0001"))

    recorder, _workspace, store, _outcome = run(plans)

    assert recorder.under_lock == [
        f"{LOCKED_PHASE.value}:read",
        f"{LOCKED_PHASE.value}:write",
    ]
    assert recorder.holds == 1
    assert store.writes == 1


def test_a_conflicting_candidate_authors_no_commit_and_takes_no_lock() -> None:
    """A block moves no canonical ref: nothing is committed and nothing swapped."""
    first, second = bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b")
    plans = plans_for(first, second)
    blocking = plans[0].ordered[1]

    recorder, workspace, store, outcome = run(plans, conflict_on=blocking.candidate_ref)

    assert outcome.kind is IntegrationOutcomeKind.BLOCKED
    assert outcome.applied.blocked_on == blocking.candidate_ref
    assert (outcome.applied.ahead, outcome.applied.behind) == (1, 2)
    assert workspace.commits == []
    assert recorder.holds == 0
    assert store.writes == 0
    assert store.ledger.head is None


# ---- DEL-013: the swap is idempotent and refuses a stale descendant -----------


def test_selecting_the_same_generation_twice_writes_once() -> None:
    """The same effect asked for twice replays instead of appending again."""
    plans = plans_for(bundle(task="EAWF-0001"))
    recorder, _workspace, store, outcome = run(plans)
    assert outcome.generation is not None

    replay = select_delivered_generation(store, outcome.generation, expected_head=None)

    assert replay.disposition is CasDisposition.REPLAYED
    assert store.writes == 1
    assert recorder.holds == 1


def test_a_descendant_of_a_superseded_generation_is_refused() -> None:
    """A delivery prepared on a head that has since moved does not land."""
    plans = plans_for(bundle(task="EAWF-0001"))
    _recorder, _workspace, store, first = run(plans)
    assert first.generation is not None
    stale = plans_for(
        bundle(task="EAWF-0002", digest="b"),
        target=binding(generation=1),
        parent=None,
    )
    descendant = build_integration_generation(stale[0], revision(head="4d" * 20), now=LATER)

    with pytest.raises(IntegrationRefusedError) as error:
        select_delivered_generation(store, descendant, expected_head=None)

    assert error.value.code is IntegrationRefusal.GENERATION_SUPERSEDED
    assert store.writes == 1
    assert store.ledger.head is not None
    assert store.ledger.head.id == first.generation.id


def test_a_delivery_prepared_on_the_current_head_lands_on_top() -> None:
    """Boundary: the successor of the current head extends the history."""
    plans = plans_for(bundle(task="EAWF-0001"))
    recorder, workspace, store, first = run(plans)
    assert first.generation is not None
    successor = plans_for(
        bundle(task="EAWF-0002", digest="b"),
        target=first.generation.integrated_revision,
        parent=first.generation.id,
    )

    outcome = integrate_batch(
        successor[0],
        workspace=workspace,
        lock=RecordingLock(recorder),
        store=store,
        now=LATER,
    )

    assert outcome.swap is not None
    assert outcome.swap.disposition is CasDisposition.SELECTED
    assert store.ledger.head is not None
    assert store.ledger.head.generation == first.generation.generation + 1
    assert [item.selected for item in store.ledger.generations] == [False, True]


def test_select_refuses_a_generation_that_is_not_marked_selected() -> None:
    """Error path: the head is the selected generation, by definition."""
    plans = plans_for(bundle(task="EAWF-0001"))
    recorder = Recorder()
    store = RecordingStore(recorder, empty_ledger())
    unselected = build_integration_generation(plans[0], revision(), now=AT).model_copy(
        update={"selected": False}
    )

    with pytest.raises(ValueError, match="must be selected"):
        select_delivered_generation(store, unselected, expected_head=None)


# ---- generations and plans ----------------------------------------------------


def test_generation_id_is_the_zero_padded_ordinal() -> None:
    """Boundary: the narrowest and widest ordinals both render."""
    assert integration_generation_id(1) == "ING-000001"
    assert integration_generation_id(999999) == "ING-999999"


@pytest.mark.parametrize("ordinal", [0, -1, 1000000])
def test_generation_id_refuses_an_ordinal_the_key_cannot_carry(ordinal: int) -> None:
    """Error path: an ordinal outside the key grammar is refused."""
    with pytest.raises(ValueError, match="does not fit"):
        integration_generation_id(ordinal)


def test_generation_record_key_separates_two_batches_at_one_ordinal() -> None:
    """The ordinal counts per Batch, so the key carries the Batch too."""
    plans = plans_for(bundle(task="EAWF-0001"))
    generation = build_integration_generation(plans[0], revision(), now=AT)

    assert generation_record_key(generation) == f"{generation.id}-BAT-0001"


def test_build_generation_binds_the_revision_to_this_ordinal() -> None:
    """The integrated revision is this generation on the integration ref."""
    plans = plans_for(bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b"))

    generation = build_integration_generation(plans[0], revision(), now=AT)

    integrated = generation.integrated_revision
    assert integrated.ref_kind is RevisionRefKind.INTEGRATION
    assert integrated.integration_generation == generation.generation
    assert generation.changed_paths == ("src/eawf-0001.py", "src/eawf-0002.py")
    assert generation.integration_policy_digest == plans[0].policy_digest
    assert isinstance(generation, IntegrationGeneration)


def test_batch_unit_plans_one_delivery_for_every_candidate() -> None:
    """Under ``batch`` the whole Batch is one plan and one ordinal."""
    plans = plans_for(bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b"), unit="batch")

    assert len(plans) == 1
    assert plans[0].generation == 2
    assert len(plans[0].ordered) == 2


def test_task_unit_plans_one_delivery_per_candidate_and_chains_them() -> None:
    """Under ``task`` each plan follows the one before it."""
    plans = plans_for(bundle(task="EAWF-0001"), bundle(task="EAWF-0002", digest="b"), unit="task")

    assert [plan.generation for plan in plans] == [2, 3]
    assert plans[0].parent_generation_id is None
    assert plans[1].parent_generation_id == integration_generation_id(2)
    assert plans[1].target_base.integration_generation == 2


def test_plan_refuses_a_commit_that_names_another_manifest() -> None:
    """Error path: a plan whose commit is about another delivery is refused."""
    first = plans_for(bundle(task="EAWF-0001"))[0]
    other = plans_for(bundle(task="EAWF-0002", digest="b"))[0]

    with pytest.raises(ValidationError, match="names another manifest"):
        IntegrationPlan.model_validate(
            {**first.model_dump(), "delivery": other.delivery.model_dump()}
        )


def test_outcome_refuses_a_blocked_run_that_carries_a_generation() -> None:
    """Error path: a block that reports a generation contradicts itself."""
    plans = plans_for(bundle(task="EAWF-0001"))
    _recorder, _workspace, _store, delivered = run(plans)

    with pytest.raises(ValidationError, match="carries no revision"):
        IntegrationOutcome(
            kind=IntegrationOutcomeKind.BLOCKED,
            applied=delivered.applied,
            generation=delivered.generation,
        )
