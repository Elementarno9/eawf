"""What a Batch must have proved, by whom, and on exactly which head.

A deterministic gate answers whether a command exited zero. It cannot
answer whether the change is the change that was asked for, and no
aggregate of agent reports can answer it either: the author of the work
is the author of the report. :class:`BatchAudit` is therefore the other
half of the evidence -- one criterion's verdict, reached by a Run that
did not produce the work, bound to the exact revision the auditor read.
A required row that came back false, or that the auditor could not
verify, blocks the Batch whatever any report says it achieved.

Independence is a property of the reviewing Run, so it is recorded on
the row rather than asserted beside it.
:class:`ReviewerAttestation` carries the reviewing Run's resolved tool
grants and refuses any grant that could write a workspace or seal a
candidate: a reviewer that could change the code it is judging is not
reviewing it. The attestation also carries the revision the reviewer
read, and a row whose auditor and reviewer bound different heads does
not validate -- two people looking at two trees agree about nothing in
particular.

:class:`BatchVerificationCycle` is the walk those rows drive. It is
bound to one exact head, and every stage move it admits is taken on that
head: checking the deterministic gates, then the independent audit and
review, then the changes a blocker requires, then the bounded repair
that answers them. Leaving a stage is not the same as moving the code,
so the head-moving edge is a separate operation -- :meth:`rebase` --
which drops whatever the cycle had reached and names, criterion by
criterion, what the move cost. A criterion the moving generation did not
invalidate keeps its verdict; a blanket reset would throw away exactly
the evidence the affected/carried split exists to preserve.

The models are pure. Deciding which stage comes next, dispatching a
reviewer, and filing a cycle live in
:mod:`eawf.workflow.delivery.verification_cycle`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import ConfigDict, StringConstraints, model_validator

from eawf.kernel.delivery.integration import ConflictExit, ConflictExitKind
from eawf.kernel.delivery.receipts import RevisionBinding, RevisionRefKind
from eawf.kernel.runtime.semantic import SEMANTIC_TOOL_CATALOG, SemanticToolId
from eawf.kernel.state.epoch2.base import (
    Epoch2Model,
    NonEmptyStr,
    Sha256DigestStr,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, RunPurpose
from eawf.kernel.state.epoch2.urns import BatchUrn, RunUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr


class _FrozenModel(Epoch2Model):
    """Strict and immutable: an audit edited after the fact is not an audit."""

    model_config = ConfigDict(extra="forbid", frozen=True)


#: A ``BAU-######`` batch audit key.
BatchAuditKey = Annotated[str, StringConstraints(strict=True, pattern=r"^BAU-\d{6}$")]

#: The tools that let a Run change a repository worktree or seal a
#: candidate from it. Derived from the one catalog rather than listed
#: again, so a tool that becomes mutating becomes denied to reviewers in
#: the same edit.
CANDIDATE_MUTATION_TOOLS: Final[frozenset[SemanticToolId]] = frozenset(
    tool_id
    for tool_id, contract in SEMANTIC_TOOL_CATALOG.items()
    if contract.requires_mutating_task
)


class AuditVerdict(StrEnum):
    """What an independent auditor concluded about one criterion.

    ``UNVERIFIED`` is a verdict and not a missing value: an auditor who
    looked and could not tell has said something load-bearing, and
    folding that into "no row yet" is how an unanswered question becomes
    a pass.
    """

    VERIFIED_TRUE = "verified_true"
    VERIFIED_FALSE = "verified_false"
    UNVERIFIED = "unverified"


#: The verdicts that do not clear a criterion. Both of them block when
#: the row is required: a false answer and an unreachable one leave the
#: same question open.
BLOCKING_VERDICTS: Final[frozenset[AuditVerdict]] = frozenset(
    {AuditVerdict.VERIFIED_FALSE, AuditVerdict.UNVERIFIED}
)


class ReviewerAttestation(_FrozenModel):
    """The Run that reached one audit verdict, and what it was allowed to do.

    The tool grants are the resolved set the Run actually reaches, not
    the requested one, so a denial that took a grant away is already
    accounted for. Recording them on the row is what makes independence
    checkable after the fact instead of a claim made once at dispatch.
    """

    run_ref: RunUrn
    purpose: RunPurpose
    reviewed_revision: RevisionBinding
    capsule_digest: Sha256DigestStr
    tool_grants: tuple[SemanticToolId, ...] = ()

    @model_validator(mode="after")
    def _reviewer_cannot_change_what_it_judges(self) -> Self:
        """Refuse a mutating purpose or any workspace-writing tool grant.

        Raises:
            ValueError: The Run was dispatched to change a repository, or
                it reaches a tool that applies a patch or seals a
                candidate, either of which would let the reviewer edit
                the very tree its verdict is about.
        """
        if self.purpose in MUTATING_PURPOSES:
            raise ValueError(
                f"purpose {self.purpose.value} changes a repository, so a Run under it cannot "
                "independently review one"
            )
        granted = sorted(tool.value for tool in set(self.tool_grants) & CANDIDATE_MUTATION_TOOLS)
        if granted:
            raise ValueError(
                f"reviewer {self.run_ref.entity_key} is granted {', '.join(granted)}, which "
                "writes the tree its verdict is about"
            )
        if len(set(self.tool_grants)) != len(self.tool_grants):
            raise ValueError(f"reviewer {self.run_ref.entity_key} repeats a tool grant")
        return self


class BatchAudit(_FrozenModel):
    """One criterion's independent verdict, bound to the revision it was read at.

    A blocking verdict must carry the finding that justifies it, and a
    clear one must not: "it holds, and here is what is wrong with it" is
    not a state an auditor can be in.
    """

    id: BatchAuditKey
    batch_ref: BatchUrn
    criterion_id: GateIdentityStr
    required: bool
    verdict: AuditVerdict
    audited_revision: RevisionBinding
    review: ReviewerAttestation
    finding: NonEmptyStr | None = None
    recorded_at: UtcDatetime

    @property
    def blocks(self) -> bool:
        """Return whether this row stops the Batch on its own."""
        return self.required and self.verdict in BLOCKING_VERDICTS

    @property
    def generation(self) -> int:
        """Return the Batch ordinal the row was taken on."""
        return self.audited_revision.integration_generation

    @model_validator(mode="after")
    def _audit_and_review_bind_one_head(self) -> Self:
        """Require the auditor and the reviewer to have read the same exact code.

        Raises:
            ValueError: The row is filed under another Batch than it was
                audited on, or the auditor and the reviewer bound
                different commits, trees, ordinals or ref kinds -- in
                which case the verdict is about a tree the review never
                saw.
        """
        audited = self.audited_revision
        reviewed = self.review.reviewed_revision
        if audited.batch_ref != self.batch_ref:
            raise ValueError(
                f"audit {self.id!r} is filed under {self.batch_ref} but was taken on "
                f"{audited.batch_ref}"
            )
        differing = sorted(
            field
            for field in ("head_sha", "tree_sha", "integration_generation", "ref_kind")
            if getattr(audited, field) != getattr(reviewed, field)
        )
        if differing:
            raise ValueError(
                f"audit {self.id!r} and its review disagree on {', '.join(differing)}, so the "
                "verdict is not about the revision that was reviewed"
            )
        return self

    @model_validator(mode="after")
    def _blocking_verdict_names_its_finding(self) -> Self:
        """Require a finding exactly on a verdict that does not clear the criterion.

        Raises:
            ValueError: A false or unverified row says nothing about what
                is wrong, or a verified-true row carries a complaint.
        """
        blocking = self.verdict in BLOCKING_VERDICTS
        if blocking != (self.finding is not None):
            raise ValueError(
                f"verdict {self.verdict.value} requires a finding exactly when it does not clear "
                f"criterion {self.criterion_id}"
            )
        return self


class BatchVerificationStage(StrEnum):
    """Where one Batch stands in proving itself on its current head."""

    CHECKING = "checking"
    AUDIT_REVIEW = "audit_review"
    CHANGES_REQUIRED = "changes_required"
    REPAIR = "repair"
    READY_TO_MERGE = "ready_to_merge"
    OPERATOR_DECISION = "operator_decision"


_G = BatchVerificationStage

#: Every stage move that keeps the cycle on one exact head. A move that
#: is not listed does not exist. ``REPAIR`` and ``READY_TO_MERGE`` have
#: no successor here on purpose: a repair produces different code and a
#: merge-ready Batch is only unseated by code moving underneath it, so
#: both leave through :meth:`BatchVerificationCycle.rebase` rather than
#: through a same-head edge.
BATCH_VERIFICATION_EDGES: Final[
    Mapping[BatchVerificationStage, frozenset[BatchVerificationStage]]
] = {
    _G.CHECKING: frozenset({_G.AUDIT_REVIEW, _G.CHANGES_REQUIRED}),
    _G.AUDIT_REVIEW: frozenset({_G.READY_TO_MERGE, _G.CHANGES_REQUIRED}),
    _G.CHANGES_REQUIRED: frozenset({_G.REPAIR, _G.OPERATOR_DECISION}),
    _G.REPAIR: frozenset(),
    _G.READY_TO_MERGE: frozenset(),
    _G.OPERATOR_DECISION: frozenset(),
}

#: The exit kind each stage must carry, for the stages that are defined
#: by having opened one. Every other stage carries none: an exit is the
#: record of a way out that was actually created.
STAGE_EXIT_KINDS: Final[Mapping[BatchVerificationStage, ConflictExitKind]] = {
    _G.REPAIR: ConflictExitKind.REPAIR_TASK,
    _G.OPERATOR_DECISION: ConflictExitKind.OPERATOR_DECISION,
}

#: The stage a Batch re-enters whenever its head moves. Code that moved
#: is unchecked code, whatever the cycle had reached.
REBASE_STAGE: Final = _G.CHECKING


class HeadInvalidation(_FrozenModel):
    """What a head move cost a cycle, named criterion by criterion.

    The two id lists partition the criteria the cycle held rows for, so a
    reader can see both what has to be re-audited and what survived
    rather than being told only that something was reset.
    """

    batch_ref: BatchUrn
    dropped_from: BatchVerificationStage
    from_generation: StrictPositiveInt
    to_generation: StrictPositiveInt
    invalidated_criterion_ids: tuple[GateIdentityStr, ...] = ()
    invalidated_audit_ids: tuple[BatchAuditKey, ...] = ()
    carried_criterion_ids: tuple[GateIdentityStr, ...] = ()

    @property
    def unseated_merge_readiness(self) -> bool:
        """Return whether the move took the Batch out of merge readiness."""
        return self.dropped_from is BatchVerificationStage.READY_TO_MERGE

    @model_validator(mode="after")
    def _move_goes_forward_and_names_each_criterion_once(self) -> Self:
        """Require a forward move whose two id lists do not overlap.

        Raises:
            ValueError: The head moved to an ordinal at or before the one
                it left, or a criterion is reported both invalidated and
                carried.
        """
        if self.to_generation <= self.from_generation:
            raise ValueError(
                f"a head move goes forward, but generation {self.to_generation} does not follow "
                f"{self.from_generation}"
            )
        both = sorted(set(self.invalidated_criterion_ids) & set(self.carried_criterion_ids))
        if both:
            raise ValueError(f"criteria {', '.join(both)} are both invalidated and carried")
        return self


class BatchVerificationCycle(_FrozenModel):
    """One Batch proving itself, on one exact head, one stage at a time.

    The head is required rather than derived: a cycle that read its head
    from wherever the Batch happens to point would silently re-target
    itself when the code moved, which is the one thing
    :meth:`rebase` exists to make visible.
    """

    batch_ref: BatchUrn
    head: RevisionBinding
    stage: BatchVerificationStage
    audits: tuple[BatchAudit, ...] = ()
    repairs_spent: StrictNonNegativeInt = 0
    repair_budget: StrictNonNegativeInt = 1
    exit: ConflictExit | None = None

    @property
    def generation(self) -> int:
        """Return the Batch ordinal the cycle is bound to."""
        return self.head.integration_generation

    @property
    def blocking_audits(self) -> tuple[BatchAudit, ...]:
        """Return every required row that came back false or unverified."""
        return tuple(item for item in self.audits if item.blocks)

    @property
    def blocking_criterion_ids(self) -> tuple[GateIdentityStr, ...]:
        """Return the criteria a required row leaves open, sorted."""
        return tuple(sorted({item.criterion_id for item in self.blocking_audits}))

    @property
    def repair_available(self) -> bool:
        """Return whether the cycle may still open a repair without asking."""
        return self.repairs_spent < self.repair_budget

    def audit_of(self, criterion_id: str) -> BatchAudit | None:
        """Return the row held for *criterion_id*, or ``None`` when none is.

        Args:
            criterion_id: The criterion to look up.

        Returns:
            The single row for that criterion, or ``None``.
        """
        for item in self.audits:
            if item.criterion_id == criterion_id:
                return item
        return None

    @model_validator(mode="after")
    def _head_is_this_batch(self) -> Self:
        """Require the bound head to be an integration revision of this Batch.

        Raises:
            ValueError: The head belongs to another Batch, or it was
                taken on a ref whose class is not the integrated one, so
                the cycle would be proving a tree nobody is delivering.
        """
        if self.head.batch_ref != self.batch_ref:
            raise ValueError(f"head binds {self.head.batch_ref} instead of {self.batch_ref}")
        if self.head.ref_kind is not RevisionRefKind.INTEGRATION:
            raise ValueError("a cycle head must be bound on the integration ref")
        return self

    @model_validator(mode="after")
    def _one_row_per_criterion_of_this_batch(self) -> Self:
        """Require each held row to be this Batch's, and each criterion audited once.

        Raises:
            ValueError: A row belongs to another Batch, an id repeats, a
                criterion carries two verdicts, or a row was taken on an
                ordinal past the head the cycle is bound to.
        """
        ids = [item.id for item in self.audits]
        if len(set(ids)) != len(ids):
            raise ValueError("an audit row is held twice")
        criteria = [item.criterion_id for item in self.audits]
        repeated = sorted({item for item in criteria if criteria.count(item) > 1})
        if repeated:
            raise ValueError(f"criteria {', '.join(repeated)} carry more than one verdict")
        for item in self.audits:
            if item.batch_ref != self.batch_ref:
                raise ValueError(f"audit {item.id!r} belongs to {item.batch_ref}")
            if item.generation > self.generation:
                raise ValueError(
                    f"audit {item.id!r} was taken on generation {item.generation}, past the head "
                    f"{self.generation} the cycle is bound to"
                )
        return self

    @model_validator(mode="after")
    def _stage_carries_what_it_means(self) -> Self:
        """Tie the exit and the merge claim to the stage that implies them.

        Raises:
            ValueError: A stage defined by an exit carries none or carries
                another kind, a stage that opened nothing carries an exit,
                the repair budget is overspent, or a merge-ready cycle
                still holds a required row that came back false or
                unverified.
        """
        expected = STAGE_EXIT_KINDS.get(self.stage)
        if expected is None and self.exit is not None:
            raise ValueError(f"stage {self.stage.value} opens no exit to record")
        if expected is not None and (self.exit is None or self.exit.kind is not expected):
            raise ValueError(f"stage {self.stage.value} requires a {expected.value} exit")
        if self.repairs_spent > self.repair_budget:
            raise ValueError(
                f"{self.repairs_spent} repair(s) were taken against a budget of "
                f"{self.repair_budget}"
            )
        blocking = self.blocking_criterion_ids
        if self.stage is BatchVerificationStage.READY_TO_MERGE and blocking:
            raise ValueError(
                f"criteria {', '.join(blocking)} carry a required verdict that does not clear "
                "them, so the Batch is not ready to merge"
            )
        return self

    def advance(
        self,
        to: BatchVerificationStage,
        *,
        audits: Sequence[BatchAudit] | None = None,
        exit: ConflictExit | None = None,
        spend_repair: bool = False,
    ) -> Self:
        """Return the cycle one stage on, still bound to the same exact head.

        Args:
            to: The stage to move into.
            audits: The rows the cycle holds after the move; unchanged
                when omitted.
            exit: The way out the move opened, for the stages that are
                defined by having opened one.
            spend_repair: Whether the move consumes one repair from the
                budget.

        Returns:
            The advanced cycle.

        Raises:
            ValueError: The move is not an edge of the same-head stage
                machine, or the advanced cycle breaks a cycle rule -- an
                exit that does not match the stage, an overspent repair
                budget, or a merge claim over an open required row.
        """
        if to not in BATCH_VERIFICATION_EDGES[self.stage]:
            raise ValueError(f"stage {self.stage.value} does not move to {to.value} on one head")
        update: dict[str, object] = {
            "stage": to,
            "exit": exit,
            "repairs_spent": self.repairs_spent + (1 if spend_repair else 0),
        }
        if audits is not None:
            update["audits"] = tuple(audits)
        return self.model_validate({**self.model_dump(), **update})

    def rebase(
        self, *, head: RevisionBinding, invalidated_criterion_ids: Iterable[str]
    ) -> tuple[Self, HeadInvalidation]:
        """Return the cycle re-opened on *head*, and what the move invalidated.

        The rows whose criteria the move named are dropped and reported;
        every other row keeps the verdict it already earned, still bound
        to the older revision it was actually taken at. The repair budget
        is carried across, because a repair that produced this very head
        was spent whether or not the head then changed.

        Args:
            head: The revision the Batch has moved to.
            invalidated_criterion_ids: The criteria the move invalidated,
                as the moving generation itself names them.

        Returns:
            The re-opened cycle and the named invalidation.

        Raises:
            ValueError: The new head is not a forward integration
                revision of this Batch.
        """
        named = frozenset(invalidated_criterion_ids)
        kept = tuple(item for item in self.audits if item.criterion_id not in named)
        dropped = tuple(item for item in self.audits if item.criterion_id in named)
        invalidation = HeadInvalidation(
            batch_ref=self.batch_ref,
            dropped_from=self.stage,
            from_generation=self.generation,
            to_generation=head.integration_generation,
            invalidated_criterion_ids=tuple(sorted(item.criterion_id for item in dropped)),
            invalidated_audit_ids=tuple(sorted(item.id for item in dropped)),
            carried_criterion_ids=tuple(sorted(item.criterion_id for item in kept)),
        )
        rebased = self.model_validate(
            {
                **self.model_dump(),
                "head": head.model_dump(),
                "stage": REBASE_STAGE,
                "audits": [item.model_dump() for item in kept],
                "exit": None,
            }
        )
        return rebased, invalidation


__all__ = [
    "BATCH_VERIFICATION_EDGES",
    "BLOCKING_VERDICTS",
    "CANDIDATE_MUTATION_TOOLS",
    "REBASE_STAGE",
    "STAGE_EXIT_KINDS",
    "AuditVerdict",
    "BatchAudit",
    "BatchAuditKey",
    "BatchVerificationCycle",
    "BatchVerificationStage",
    "HeadInvalidation",
    "ReviewerAttestation",
]
