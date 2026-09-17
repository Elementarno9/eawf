"""Integration attempt, generation and conflict records.

A sealed candidate reaches a Batch only through an
:class:`IntegrationAttempt` the daemon runs in an isolated workspace. A
successful attempt yields an :class:`IntegrationGeneration`: the exact
candidate, base and integrated identities plus one ordinal that only
grows, so a later proof can say which generation it was taken on. An
attempt that conflicts is blocked and writes one
:class:`IntegrationConflict` naming the typed exit the daemon opened,
because a conflict frame without a way out invites someone to edit the
canonical workspace by hand.

The models are pure and strict. Applying a status move and selecting a
generation live in :mod:`eawf.runtime.integration.generations`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, model_validator

from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind
from eawf.kernel.identity import EntityKind, QualifiedUrn
from eawf.kernel.state.epoch2.base import (
    BranchName,
    Epoch2Model,
    PrincipalKey,
    Sha256DigestStr,
    ShaStr,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.urns import (
    AnyEntityUrn,
    BatchUrn,
    EvidenceUrn,
    RepositoryUrn,
    TaskUrn,
)
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr


class _FrozenModel(Epoch2Model):
    """Strict and immutable: an integration record is appended, never edited."""

    model_config = ConfigDict(extra="forbid", frozen=True)


# ---- keys and bounded scalars -----------------------------------------------

#: An ``INA-######`` integration attempt key.
IntegrationAttemptKey = Annotated[str, StringConstraints(strict=True, pattern=r"^INA-\d{6}$")]

#: An ``ING-######`` integration generation key.
IntegrationGenerationKey = Annotated[str, StringConstraints(strict=True, pattern=r"^ING-\d{6}$")]

#: An ``INC-######`` integration conflict key.
IntegrationConflictKey = Annotated[str, StringConstraints(strict=True, pattern=r"^INC-\d{6}$")]

#: The durable operation an attempt runs under, such as ``OPR-INT-000004``.
OperationAttemptKey = Annotated[
    str, StringConstraints(strict=True, pattern=r"^OPR-[A-Z]{3}-\d{6}$")
]

#: A daemon-sealed candidate bundle key.
CandidateBundleKey = Annotated[str, StringConstraints(strict=True, pattern=r"^CB-\d{8}$")]

#: A plan-time ownership claim over a shared surface.
ConflictClaimId = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
]

#: One effect per canonical payload; the daemon compares, never displays it.
IdempotencyKey = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
]

#: One line of one side of a conflict hunk. The bound keeps a pasted blob
#: from riding into the record; a newline would split the line count.
ConflictLine = Annotated[str, StringConstraints(strict=True, max_length=1000, pattern=r"^[^\n]*$")]

#: The most lines one hunk side may carry.
MAX_CONFLICT_SIDE_LINES: Final = 400


def _validate_repo_relative(value: str) -> str:
    """Admit only a plain forward-slash path inside the repository.

    Raises:
        ValueError: The path is absolute, climbs out with ``..``, carries a
            NUL or a back-slash, or has an empty segment.
    """
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError(f"path {value!r} must be a repo-relative forward-slash path")
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ValueError(f"path {value!r} has an empty, '.' or '..' segment")
    return value


RepoRelativePath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1024),
    AfterValidator(_validate_repo_relative),
]


def _reject_duplicates(values: Iterable[object], *, field: str) -> None:
    """Raise when *values* repeats an entry.

    Raises:
        ValueError: An entry appears twice.
    """
    seen: set[object] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"{field} repeats {value!s}")
        seen.add(value)


def _same_repository(first: QualifiedUrn, second: QualifiedUrn) -> bool:
    """Return whether two repository-scoped URNs sit under one repository."""
    return (
        first.workspace_key == second.workspace_key
        and first.project_key == second.project_key
        and first.repository_key == second.repository_key
    )


def _require_batch_bindings(batch_ref: QualifiedUrn, **bindings: RevisionBinding) -> None:
    """Require every binding to have been taken on *batch_ref*.

    Raises:
        ValueError: A binding names another Batch.
    """
    for name, binding in bindings.items():
        if binding.batch_ref != batch_ref:
            raise ValueError(f"{name} binds {binding.batch_ref} instead of {batch_ref}")


# ---- attempt state machine ----------------------------------------------------


class IntegrationAttemptStatus(StrEnum):
    """The reducer-owned states of one integration attempt."""

    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    APPLYING = "APPLYING"
    VERIFYING_TREE = "VERIFYING_TREE"
    SUCCEEDED = "SUCCEEDED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"


_S = IntegrationAttemptStatus

#: Every legal status move. A move that is not listed does not exist: in
#: particular a timeout never becomes a cancellation, and nothing leaves a
#: terminal state, so a successor is a new attempt rather than a rewrite.
INTEGRATION_ATTEMPT_EDGES: Final[
    Mapping[IntegrationAttemptStatus, frozenset[IntegrationAttemptStatus]]
] = {
    _S.QUEUED: frozenset({_S.PREPARING, _S.TIMED_OUT, _S.CANCELLED}),
    _S.PREPARING: frozenset({_S.APPLYING, _S.STALE, _S.FAILED, _S.TIMED_OUT, _S.CANCELLED}),
    _S.APPLYING: frozenset({_S.VERIFYING_TREE, _S.STALE, _S.BLOCKED, _S.FAILED, _S.TIMED_OUT}),
    _S.VERIFYING_TREE: frozenset({_S.SUCCEEDED, _S.STALE, _S.FAILED}),
    _S.SUCCEEDED: frozenset(),
    _S.STALE: frozenset(),
    _S.BLOCKED: frozenset(),
    _S.FAILED: frozenset(),
    _S.TIMED_OUT: frozenset(),
    _S.CANCELLED: frozenset(),
}

#: The states an attempt never leaves.
INTEGRATION_TERMINAL_STATUSES: Final = frozenset(
    status for status, targets in INTEGRATION_ATTEMPT_EDGES.items() if not targets
)


class IntegrationFailureKind(StrEnum):
    """Why an attempt ended stale, blocked or failed."""

    BASE_MOVED = "base_moved"
    POLICY_MOVED = "policy_moved"
    CONFLICT = "conflict"
    TREE_MISMATCH = "tree_mismatch"
    SCOPE_VIOLATION = "scope_violation"
    APPLY_ERROR = "apply_error"


_F = IntegrationFailureKind

#: The failure kinds each failing status admits. A status missing here
#: carries no failure kind: a timeout or a cancellation already says what
#: happened, and a success has nothing to explain.
FAILURE_KINDS_BY_STATUS: Final[
    Mapping[IntegrationAttemptStatus, frozenset[IntegrationFailureKind]]
] = {
    _S.STALE: frozenset({_F.BASE_MOVED, _F.POLICY_MOVED}),
    _S.BLOCKED: frozenset({_F.CONFLICT}),
    _S.FAILED: frozenset({_F.TREE_MISMATCH, _F.SCOPE_VIOLATION, _F.APPLY_ERROR}),
}


class IntegrationAttempt(_FrozenModel):
    """One daemon attempt to integrate a sealed candidate into its Batch.

    ``generation`` is the ordinal the attempt would select, so it must be
    past the Batch base the attempt was prepared on. A diagnostic is a
    detail of an outcome, so only a terminal attempt may carry one, and a
    blocked attempt must: its conflict record sits beside it.
    """

    id: IntegrationAttemptKey
    operation_attempt_id: OperationAttemptKey
    batch_ref: BatchUrn
    task_ref: TaskUrn
    candidate_bundle_id: CandidateBundleKey
    generation: StrictPositiveInt
    supersedes_id: IntegrationAttemptKey | None = None
    source_base: RevisionBinding
    selected_batch_base: RevisionBinding
    candidate_patch_digest: Sha256DigestStr
    candidate_tree_digest: Sha256DigestStr
    changed_paths: tuple[RepoRelativePath, ...] = Field(min_length=1)
    conflict_claim_ids: tuple[ConflictClaimId, ...] = ()
    affected_criterion_ids: tuple[GateIdentityStr, ...] = ()
    idempotency_key: IdempotencyKey
    status: IntegrationAttemptStatus
    failure_kind: IntegrationFailureKind | None = None
    diagnostic_ref: EvidenceUrn | None = None
    requested_at: UtcDatetime
    updated_at: UtcDatetime
    terminal_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _subject_is_coherent(self) -> Self:
        """Tie the Task, both bases and the target ordinal to the Batch.

        Raises:
            ValueError: The Task sits in another repository, a base was
                taken on another Batch, the target ordinal does not pass
                the selected base, the attempt supersedes itself, or a
                list repeats an entry.
        """
        if not _same_repository(self.task_ref, self.batch_ref):
            raise ValueError(f"task {self.task_ref} is not in the repository of {self.batch_ref}")
        _require_batch_bindings(
            self.batch_ref,
            source_base=self.source_base,
            selected_batch_base=self.selected_batch_base,
        )
        base_generation = self.selected_batch_base.integration_generation
        if self.generation <= base_generation:
            raise ValueError(
                f"generation {self.generation} must follow the selected base generation "
                f"{base_generation}"
            )
        if self.supersedes_id == self.id:
            raise ValueError(f"attempt {self.id!r} cannot supersede itself")
        _reject_duplicates(self.changed_paths, field="changed_paths")
        _reject_duplicates(self.conflict_claim_ids, field="conflict_claim_ids")
        _reject_duplicates(self.affected_criterion_ids, field="affected_criterion_ids")
        return self

    @model_validator(mode="after")
    def _outcome_matches_status(self) -> Self:
        """Require the failure kind, diagnostic and clock the status implies.

        Raises:
            ValueError: The failure kind is missing, foreign to the status,
                or present where none is admitted; a diagnostic is present
                before the outcome or missing on a blocked attempt;
                ``terminal_at`` disagrees with the status; or the clock runs
                backwards.
        """
        admitted = FAILURE_KINDS_BY_STATUS.get(self.status, frozenset())
        if self.failure_kind is None and admitted:
            raise ValueError(f"status {self.status.value} requires a failure_kind")
        if self.failure_kind is not None and self.failure_kind not in admitted:
            raise ValueError(
                f"failure_kind {self.failure_kind.value} is not admitted in {self.status.value}"
            )
        terminal = self.status in INTEGRATION_TERMINAL_STATUSES
        if self.diagnostic_ref is not None and not terminal:
            raise ValueError(f"status {self.status.value} cannot carry a diagnostic_ref yet")
        if self.status is IntegrationAttemptStatus.BLOCKED and self.diagnostic_ref is None:
            raise ValueError("a blocked attempt requires the diagnostic_ref of its conflict")
        if terminal != (self.terminal_at is not None):
            raise ValueError(
                f"terminal_at must be set exactly when {self.status.value} is terminal"
            )
        if self.updated_at < self.requested_at:
            raise ValueError("updated_at cannot precede requested_at")
        if self.terminal_at is not None and not (
            self.requested_at <= self.terminal_at <= self.updated_at
        ):
            raise ValueError("terminal_at must fall between requested_at and updated_at")
        return self


# ---- generations --------------------------------------------------------------


class IntegrationGeneration(_FrozenModel):
    """One selected result of integrating a candidate onto its Batch.

    The integrated revision is the integration-ref binding of this very
    generation, and the base it was applied on is an earlier one, so the
    ordinal and the bound code cannot disagree. ``selected`` marks the
    current head; :class:`IntegrationGenerationLedger` keeps it unique.
    """

    id: IntegrationGenerationKey
    batch_ref: BatchUrn
    generation: StrictPositiveInt
    parent_generation_id: IntegrationGenerationKey | None
    source_candidate_bundle_id: CandidateBundleKey
    source_base: RevisionBinding
    target_base: RevisionBinding
    integrated_revision: RevisionBinding
    patch_digest: Sha256DigestStr
    diff_digest: Sha256DigestStr
    tree_digest: Sha256DigestStr
    changed_paths: tuple[RepoRelativePath, ...] = Field(min_length=1)
    affected_task_refs: tuple[TaskUrn, ...] = ()
    affected_criterion_ids: tuple[GateIdentityStr, ...] = ()
    integration_policy_digest: Sha256DigestStr
    selected: bool
    created_at: UtcDatetime

    @model_validator(mode="after")
    def _identities_are_coherent(self) -> Self:
        """Bind every revision and affected Task to this Batch and ordinal.

        Raises:
            ValueError: A binding names another Batch, the integrated
                revision is not this generation on the integration ref, the
                target base does not precede it, an affected Task sits in
                another repository, the generation parents itself, or a
                list repeats an entry.
        """
        _require_batch_bindings(
            self.batch_ref,
            source_base=self.source_base,
            target_base=self.target_base,
            integrated_revision=self.integrated_revision,
        )
        integrated = self.integrated_revision
        if integrated.integration_generation != self.generation:
            raise ValueError(
                f"integrated_revision binds generation {integrated.integration_generation}, "
                f"not {self.generation}"
            )
        if integrated.ref_kind is not RevisionRefKind.INTEGRATION:
            raise ValueError("integrated_revision must be bound on the integration ref")
        if self.target_base.integration_generation >= self.generation:
            raise ValueError(
                f"target_base generation {self.target_base.integration_generation} must "
                f"precede generation {self.generation}"
            )
        for task_ref in self.affected_task_refs:
            if not _same_repository(task_ref, self.batch_ref):
                raise ValueError(f"task {task_ref} is not in the repository of {self.batch_ref}")
        if self.parent_generation_id == self.id:
            raise ValueError(f"generation {self.id!r} cannot parent itself")
        _reject_duplicates(self.changed_paths, field="changed_paths")
        _reject_duplicates(self.affected_task_refs, field="affected_task_refs")
        _reject_duplicates(self.affected_criterion_ids, field="affected_criterion_ids")
        return self


class IntegrationGenerationLedger(_FrozenModel):
    """The linear generation history of one Batch, oldest first.

    Each generation parents on the one before it and is applied on that
    generation's code, so the ordinal strictly grows and a descendant bound
    to a superseded generation cannot join. Exactly one generation, the
    newest, is selected whenever the history is not empty.
    """

    batch_ref: BatchUrn
    generations: tuple[IntegrationGeneration, ...] = ()

    @property
    def head(self) -> IntegrationGeneration | None:
        """Return the selected generation, or ``None`` before the first one."""
        return self.generations[-1] if self.generations else None

    @model_validator(mode="after")
    def _history_is_linear(self) -> Self:
        """Require one Batch, a strictly growing ordinal and one selected head.

        Raises:
            ValueError: A generation belongs to another Batch, repeats an
                id, does not parent on and apply over its predecessor, does
                not grow the ordinal, predates its predecessor, or the
                selected flag is not on exactly the newest generation.
        """
        _reject_duplicates((item.id for item in self.generations), field="generations")
        previous: IntegrationGeneration | None = None
        for item in self.generations:
            if item.batch_ref != self.batch_ref:
                raise ValueError(f"generation {item.id!r} belongs to {item.batch_ref}")
            expected_parent = previous.id if previous is not None else None
            if item.parent_generation_id != expected_parent:
                raise ValueError(
                    f"generation {item.id!r} parents on {item.parent_generation_id!r}, "
                    f"expected {expected_parent!r}"
                )
            if previous is not None:
                if item.generation <= previous.generation:
                    raise ValueError(
                        f"generation ordinal {item.generation} does not follow "
                        f"{previous.generation}"
                    )
                if item.target_base.integration_generation != previous.generation:
                    raise ValueError(
                        f"generation {item.id!r} was applied on generation "
                        f"{item.target_base.integration_generation}, not the head "
                        f"{previous.generation}"
                    )
                if item.created_at < previous.created_at:
                    raise ValueError(f"generation {item.id!r} predates {previous.id!r}")
            previous = item
        selected = [item.id for item in self.generations if item.selected]
        if self.generations and selected != [self.generations[-1].id]:
            raise ValueError(
                f"exactly the newest generation must be selected, found {selected or 'none'}"
            )
        return self


# ---- conflicts ----------------------------------------------------------------


class AgentAuthority(_FrozenModel):
    """The agent that produced a side, named by the Batch it works under."""

    kind: Literal["agent"]
    batch_ref: BatchUrn


class PrincipalAuthority(_FrozenModel):
    """A principal that produced a side, by its immutable key."""

    kind: Literal["principal"]
    principal_key: PrincipalKey


#: Who wrote one side of a hunk. A display name is not an authority, so
#: the union admits only the two typed references.
ConflictAuthority = Annotated[AgentAuthority | PrincipalAuthority, Field(discriminator="kind")]


class ConflictSide(_FrozenModel):
    """One side of a conflicting hunk, with who wrote it, when, and where."""

    authority: ConflictAuthority
    at: UtcDatetime
    sha: ShaStr
    lines: tuple[ConflictLine, ...] = Field(max_length=MAX_CONFLICT_SIDE_LINES)


class ConflictHunk(_FrozenModel):
    """Both sides of one conflicting region; neither is chosen here."""

    index: StrictPositiveInt
    ours: ConflictSide
    theirs: ConflictSide


class ConflictFile(_FrozenModel):
    """Every conflicting hunk of one path, numbered from one in file order."""

    path: RepoRelativePath
    hunks: tuple[ConflictHunk, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _hunks_numbered_in_order(self) -> Self:
        """Require hunk indexes to run 1, 2, ... without gaps.

        Raises:
            ValueError: An index is out of sequence.
        """
        indexes = [hunk.index for hunk in self.hunks]
        if indexes != list(range(1, len(indexes) + 1)):
            raise ValueError(f"{self.path} hunk indexes {indexes} must run from 1 in order")
        return self


class ConflictExitKind(StrEnum):
    """Where resolution of a blocked attempt lands."""

    REPAIR_TASK = "repair_task"
    REBASE_TASK = "rebase_task"
    OPERATOR_DECISION = "operator_decision"


#: The entity kind each exit must reference: a repair or rebase is a
#: bounded Task, and an operator decision is a protected pending action.
EXIT_REF_KINDS: Final[Mapping[ConflictExitKind, EntityKind]] = {
    ConflictExitKind.REPAIR_TASK: EntityKind.TASK,
    ConflictExitKind.REBASE_TASK: EntityKind.TASK,
    ConflictExitKind.OPERATOR_DECISION: EntityKind.PENDING_ACTION,
}


class ConflictExit(_FrozenModel):
    """The typed exit the daemon created when it blocked the attempt."""

    kind: ConflictExitKind
    ref: AnyEntityUrn

    @model_validator(mode="after")
    def _ref_matches_kind(self) -> Self:
        """Require the reference to address the record the exit kind names.

        Raises:
            ValueError: The reference addresses another entity kind.
        """
        expected = EXIT_REF_KINDS[self.kind]
        if self.ref.kind is not expected:
            raise ValueError(
                f"exit {self.kind.value} must reference a {expected.value}, "
                f"not a {self.ref.kind.value}"
            )
        return self


class IntegrationConflict(_FrozenModel):
    """The read-only conflict frame a blocked attempt leaves behind.

    The record exists so a surface can show a conflict without reading the
    isolated workspace, and ``exit`` is required so every frame names where
    resolution lands. ``ahead`` and ``behind`` count the candidate against
    the selected Batch base at preparation.
    """

    id: IntegrationConflictKey
    attempt_id: IntegrationAttemptKey
    batch_ref: BatchUrn
    repository_ref: RepositoryUrn
    branch: BranchName
    ahead: StrictNonNegativeInt
    behind: StrictNonNegativeInt
    files: tuple[ConflictFile, ...] = Field(min_length=1)
    exit: ConflictExit
    cleared_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _record_is_coherent(self) -> Self:
        """Place the Batch and the exit in the repository and dedupe paths.

        Raises:
            ValueError: The Batch or a Task exit sits under another
                repository, or a path is listed twice.
        """
        repository = self.repository_ref
        batch = self.batch_ref
        in_repository = (
            batch.workspace_key == repository.workspace_key
            and batch.project_key == repository.project_key
            and batch.repository_key == repository.entity_key
        )
        if not in_repository:
            raise ValueError(f"batch {batch} is not under repository {repository}")
        if self.exit.ref.kind is EntityKind.TASK and not _same_repository(self.exit.ref, batch):
            raise ValueError(f"exit task {self.exit.ref} is not in the repository of {batch}")
        _reject_duplicates((item.path for item in self.files), field="files")
        return self

    def require_attempt(self, attempt: IntegrationAttempt) -> None:
        """Refuse a record that does not describe *attempt*'s conflict.

        Args:
            attempt: The attempt the record claims to have blocked.

        Raises:
            ValueError: The ids or Batches differ, the attempt is not blocked
                on a conflict, or a conflicting path is outside the
                attempt's changed paths.
        """
        if attempt.id != self.attempt_id or attempt.batch_ref != self.batch_ref:
            raise ValueError(f"conflict {self.id!r} does not describe attempt {attempt.id!r}")
        if attempt.failure_kind is not IntegrationFailureKind.CONFLICT:
            raise ValueError(f"attempt {attempt.id!r} is not blocked on a conflict")
        outside = sorted({item.path for item in self.files} - set(attempt.changed_paths))
        if outside:
            raise ValueError(f"conflict paths {outside} are not changed by {attempt.id!r}")


__all__ = [
    "EXIT_REF_KINDS",
    "FAILURE_KINDS_BY_STATUS",
    "INTEGRATION_ATTEMPT_EDGES",
    "INTEGRATION_TERMINAL_STATUSES",
    "MAX_CONFLICT_SIDE_LINES",
    "AgentAuthority",
    "CandidateBundleKey",
    "ConflictAuthority",
    "ConflictClaimId",
    "ConflictExit",
    "ConflictExitKind",
    "ConflictFile",
    "ConflictHunk",
    "ConflictLine",
    "ConflictSide",
    "IdempotencyKey",
    "IntegrationAttempt",
    "IntegrationAttemptKey",
    "IntegrationAttemptStatus",
    "IntegrationConflict",
    "IntegrationConflictKey",
    "IntegrationFailureKind",
    "IntegrationGeneration",
    "IntegrationGenerationKey",
    "IntegrationGenerationLedger",
    "OperationAttemptKey",
    "PrincipalAuthority",
    "RepoRelativePath",
]
