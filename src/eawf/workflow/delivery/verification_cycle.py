"""Driving a Batch's verification cycle, and keeping its reviewer independent.

Three questions are settled here, and each of them is settled by refusing
rather than by scoring.

Who may review. A reviewer is independent when it could not have written
what it is judging and was not handed the producer's account of it. Both
halves are structural. The tool half is a property of the authority
capsule the reviewing Run is dispatched under: a capsule sealed at a
non-task scope cannot carry a workspace-writing tool at all, because
:class:`~eawf.kernel.runtime.capsule.AuthorityCapsule` refuses to
validate one, and the checks here reject the grant a second time on the
way into the record so a hand-built row cannot slip past the dispatcher.
The transcript half is a property of the brief: :class:`ReviewerBrief`
has no field a producing Run's output could be put in, and its one prose
field -- the criterion statement the reviewer is asked to check -- is
scanned for Run references rather than trusted, because prose is exactly
where a pasted transcript would arrive.

What clears the Batch. A required audit row that came back false, or that
its auditor could not verify, blocks -- and it blocks whatever the
aggregate of the producing Runs' reports claims, because that aggregate
is the authors marking their own work. A criterion with no row at all is
refused too, and refused separately: an unasked question and an
unanswerable one are different failures, and only one of them is fixed by
asking.

What happens next. The first required blocker opens one bounded repair
Task; a further blocker once the budget is spent stops autonomous repair
and opens an operator question. Both exits are
:class:`~eawf.kernel.delivery.integration.ConflictExit` values, because
"a blocked delivery needs a typed way out" is one problem whether the
block came from a merge conflict or from a reviewer, and two enumerations
of it would drift.

Nothing here runs a gate, dispatches a Run or writes a record. The
decision names the next stage and the exit it needs; filing the cycle is
:mod:`eawf.runtime.daemon.methods.delivery`'s job.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, model_validator

from eawf.kernel.delivery.batch_proof import (
    CANDIDATE_MUTATION_TOOLS,
    BatchAudit,
    BatchVerificationCycle,
    BatchVerificationStage,
    HeadInvalidation,
    ReviewerAttestation,
)
from eawf.kernel.delivery.integration import (
    ConflictExit,
    ConflictExitKind,
    IntegrationGeneration,
)
from eawf.kernel.delivery.receipts import RevisionBinding
from eawf.kernel.identity import QualifiedUrn
from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.state.epoch2.base import Epoch2Model, NonEmptyStr
from eawf.kernel.state.epoch2.urns import BatchUrn
from eawf.kernel.store.kinds.gate_receipt import GateIdentityStr
from eawf.runtime.integration.apply import IntegrationRefusal
from eawf.workflow.delivery.completion import affected_criterion_ids

logger = logging.getLogger(__name__)


#: A Run reference in any form a pasted transcript would carry it. The
#: bare key is matched rather than the full URN path, because an excerpt
#: is usually quoted without its prefix -- which is precisely the case a
#: stricter pattern would miss.
_RUN_REFERENCE: Final = re.compile(r"\bRUN-\d{8}\b")


class VerificationRefusal(StrEnum):
    """The stable codes a Batch verification cycle is refused with.

    ``EXIT_UNNAMED`` is bound to the integration vocabulary rather than
    re-spelled: a conflict frame with no way out and a blocking review
    with no way out are the same fact, and a caller that already routes
    on the integration code must keep routing when the refusal arrives
    from a reviewer instead of from a merge.
    """

    AUDIT_UNCOVERED = "verification_audit_uncovered"
    REVIEWER_IS_PRODUCER = "verification_reviewer_is_producer"
    REVIEWER_MUTATION_GRANTED = "verification_reviewer_mutation_granted"
    REVIEWER_PRODUCER_TRANSCRIPT = "verification_reviewer_producer_transcript"
    REVIEWER_BRIEF_MISBOUND = "verification_reviewer_brief_misbound"
    EXIT_UNNAMED = IntegrationRefusal.EXIT_UNNAMED


class VerificationRefusedError(ValueError):
    """One verification step refused, with the code that says which rule.

    Attributes:
        code: The stable refusal code a caller routes on.
    """

    def __init__(self, code: VerificationRefusal, detail: str) -> None:
        """Keep the code beside the sentence an operator reads.

        Args:
            code: The stable refusal code.
            detail: One sentence naming what was refused and why.
        """
        self.code = code
        super().__init__(f"{code.value}: {detail}")


class ReviewerBrief(Epoch2Model):
    """Everything one reviewer Run is given before it forms a verdict.

    The brief is the reviewer's whole starting context, and it is a
    closed model: there is no field for a producing Run's transcript, its
    report, or any opaque payload, so a dispatcher cannot hand one over
    by filling something in, and ``extra="forbid"`` refuses one added on
    the way past. What remains is the Batch, the criterion, the exact
    revision to read, and the statement to check -- and the statement is
    the one place free text enters, which is why it is scanned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: BatchUrn
    criterion_id: GateIdentityStr
    reviewed_revision: RevisionBinding
    statement: NonEmptyStr

    @model_validator(mode="after")
    def _revision_is_this_batch(self) -> Self:
        """Require the revision to be one of the Batch the brief names.

        Raises:
            ValueError: The reviewer would be pointed at another Batch's
                code than the criterion it is asked about belongs to.
        """
        if self.reviewed_revision.batch_ref != self.batch_ref:
            raise ValueError(
                f"brief for {self.batch_ref} points the reviewer at "
                f"{self.reviewed_revision.batch_ref}"
            )
        return self


def _refuse_producer_reference(statement: str, *, criterion_id: str) -> None:
    """Refuse prose that carries a Run reference.

    A criterion statement describes what must hold about the code. It has
    no legitimate reason to name a Run, so a Run key appearing in one is
    read as a producing Run's account having been pasted in.

    Raises:
        VerificationRefusedError: The statement names a Run.
    """
    found = sorted(set(_RUN_REFERENCE.findall(statement)))
    if found:
        raise VerificationRefusedError(
            VerificationRefusal.REVIEWER_PRODUCER_TRANSCRIPT,
            f"the brief for criterion {criterion_id} names run(s) {', '.join(found)}, so it "
            "carries a producing Run's account rather than the criterion to check",
        )


def _refuse_mutation_grant(tools: Iterable[object], *, run_key: str) -> None:
    """Refuse a reviewer that reaches a tool which could write the tree it judges.

    Raises:
        VerificationRefusedError: A granted tool applies a patch or seals
            a candidate.
    """
    mutating = sorted(str(tool) for tool in set(tools) & CANDIDATE_MUTATION_TOOLS)
    if mutating:
        raise VerificationRefusedError(
            VerificationRefusal.REVIEWER_MUTATION_GRANTED,
            f"run {run_key} reaches {', '.join(mutating)}, so it could change the candidate it "
            "is reviewing",
        )


def _refuse_producer_reviewer(
    run_ref: QualifiedUrn, producers: Iterable[QualifiedUrn], *, subject: str
) -> None:
    """Refuse a reviewer that is one of the Runs which produced the work.

    Raises:
        VerificationRefusedError: The reviewing Run produced a candidate
            of the Batch under review, so its verdict is its own report.
    """
    if str(run_ref) in {str(item) for item in producers}:
        raise VerificationRefusedError(
            VerificationRefusal.REVIEWER_IS_PRODUCER,
            f"run {run_ref.entity_key} produced the work under review, so its verdict on "
            f"{subject} would be its own report",
        )


def reviewer_attestation(
    capsule: AuthorityCapsule,
    *,
    brief: ReviewerBrief,
    producer_run_refs: Iterable[QualifiedUrn],
) -> ReviewerAttestation:
    """Return the attestation of an independent reviewer, or refuse the Run.

    Building the attestation is the only way to put a reviewer on an audit
    row, so every dispatch-time independence rule is checked here once
    rather than at each call site that might have remembered to. The
    resolved tool set is recorded, not the requested one, so a denial that
    took a grant away is already accounted for.

    Args:
        capsule: The sealed authority the reviewing Run is dispatched
            under.
        brief: Everything the reviewer is given.
        producer_run_refs: The Runs that produced the work under review.

    Returns:
        The attestation to record on the audit row.

    Raises:
        VerificationRefusedError: The reviewer is one of the producing
            Runs, it reaches a tool that could write the tree it is
            judging, its capsule is sealed for another scope than the
            brief names, or the brief carries a producing Run's account.
        pydantic.ValidationError: The capsule's purpose breaks a
            :class:`~eawf.kernel.delivery.batch_proof.ReviewerAttestation`
            rule.
    """
    _refuse_producer_reviewer(
        capsule.run_ref, producer_run_refs, subject=f"criterion {brief.criterion_id}"
    )
    if str(capsule.scope_ref) != str(brief.batch_ref):
        raise VerificationRefusedError(
            VerificationRefusal.REVIEWER_BRIEF_MISBOUND,
            f"run {capsule.run_ref.entity_key} is sealed for {capsule.scope_ref.entity_key} but "
            f"the brief is for {brief.batch_ref.entity_key}",
        )
    resolved = capsule.semantic_tools
    _refuse_mutation_grant(resolved, run_key=capsule.run_ref.entity_key)
    _refuse_producer_reference(brief.statement, criterion_id=brief.criterion_id)
    logger.info(
        f"reviewer_attestation run={capsule.run_ref.entity_key} "
        f"criterion={brief.criterion_id} tools={len(resolved)}"
    )
    return ReviewerAttestation(
        run_ref=capsule.run_ref,
        purpose=capsule.purpose,
        reviewed_revision=brief.reviewed_revision,
        capsule_digest=capsule.contract_digest,
        tool_grants=resolved,
    )


def require_reviewer_independence(
    audits: Sequence[BatchAudit], *, producer_run_refs: Iterable[QualifiedUrn]
) -> None:
    """Refuse any presented row whose reviewer produced the work it judges.

    Only this rule is re-checked, and only here. What a reviewer could do
    is already unconstructible: neither
    :class:`~eawf.kernel.runtime.capsule.AuthorityCapsule` nor
    :class:`~eawf.kernel.delivery.batch_proof.ReviewerAttestation` will
    validate a workspace-writing grant on a reviewing Run, so a row that
    reaches this function cannot be carrying one. Who produced the work
    is different: it is a fact about the Batch's ledger, which the record
    itself has no way to know, so a row built elsewhere has to be held
    against the producers actually on file.

    Args:
        audits: The rows presented for the Batch.
        producer_run_refs: The Runs that sealed a candidate of it.

    Raises:
        VerificationRefusedError: A row's reviewer produced a candidate of
            the Batch, so its verdict is its own report.
    """
    for row in audits:
        _refuse_producer_reviewer(row.review.run_ref, producer_run_refs, subject=f"audit {row.id}")


class BatchVerificationDecision(BaseModel):
    """What one Batch's verification cycle does next, and why.

    Attributes:
        batch_ref: The Batch being verified.
        stage: The stage the cycle stands in after the decision.
        head_generation: The ordinal the whole decision was taken on.
        blocking_criterion_ids: The criteria a required row leaves open.
        settled_criterion_ids: The judgment criteria an audit row cleared,
            which no deterministic proof could have settled.
        repair_criterion_ids: What a repair opened by this decision is
            bounded to address; empty unless a repair was opened.
        repairs_spent: How much of the repair budget the Batch has used.
        exit: The typed way out this decision opened, or ``None``.
        invalidation: What the head move that preceded this decision cost,
            or ``None`` when the head did not move.
        reason: One sentence an operator reads.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_ref: str
    stage: BatchVerificationStage
    head_generation: int
    blocking_criterion_ids: tuple[GateIdentityStr, ...] = ()
    settled_criterion_ids: tuple[GateIdentityStr, ...] = ()
    repair_criterion_ids: tuple[GateIdentityStr, ...] = ()
    repairs_spent: int
    exit: ConflictExit | None = None
    invalidation: HeadInvalidation | None = None
    reason: str

    @property
    def merge_ready(self) -> bool:
        """Return whether the cycle reached merge readiness on this head."""
        return self.stage is BatchVerificationStage.READY_TO_MERGE

    @property
    def needs_operator(self) -> bool:
        """Return whether autonomous repair stopped and an operator must answer."""
        return self.stage is BatchVerificationStage.OPERATOR_DECISION


def rebase_cycle(
    cycle: BatchVerificationCycle,
    *,
    head: RevisionBinding,
    moved_through: Sequence[IntegrationGeneration],
) -> tuple[BatchVerificationCycle, HeadInvalidation]:
    """Re-open *cycle* on *head*, dropping only what the move actually invalidated.

    Which criteria a generation invalidated is read from the generation
    itself through
    :func:`~eawf.workflow.delivery.completion.affected_criterion_ids`,
    which refuses a line whose named set does not digest to the criterion
    component of its own revision binding. A widened set would otherwise
    let a later integration reach back and throw away audit rows it never
    committed to invalidating.

    Args:
        cycle: The cycle as it stood on the old head.
        head: The revision the Batch has moved to.
        moved_through: The generations the Batch passed through to reach
            *head*, that head's own generation included.

    Returns:
        The re-opened cycle and the criterion-by-criterion invalidation.

    Raises:
        CompletionRefusedError: A generation's affected set is not the one
            its own freshness key was written under.
        ValueError: The new head does not follow the cycle's ordinal, or
            it is not an integration revision of this Batch.
    """
    named: set[str] = set()
    for item in moved_through:
        named |= affected_criterion_ids(item)
    rebased, invalidation = cycle.rebase(head=head, invalidated_criterion_ids=named)
    logger.info(
        f"rebase_cycle batch={cycle.batch_ref.entity_key} "
        f"from={invalidation.from_generation} to={invalidation.to_generation} "
        f"dropped_from={invalidation.dropped_from.value} "
        f"invalidated={len(invalidation.invalidated_criterion_ids)} "
        f"carried={len(invalidation.carried_criterion_ids)}"
    )
    return rebased, invalidation


def _require_audit_coverage(
    cycle: BatchVerificationCycle, judgment_criterion_ids: Sequence[str]
) -> tuple[BatchAudit, ...]:
    """Return the rows settling the judgment criteria, refusing any that has none.

    Raises:
        VerificationRefusedError: A criterion no deterministic proof can
            settle carries no audit row, so the Batch would reach a
            verdict by nobody having looked.
    """
    settling: list[BatchAudit] = []
    missing: list[str] = []
    for criterion_id in judgment_criterion_ids:
        row = cycle.audit_of(criterion_id)
        if row is None:
            missing.append(criterion_id)
        else:
            settling.append(row)
    if missing:
        raise VerificationRefusedError(
            VerificationRefusal.AUDIT_UNCOVERED,
            f"criteria {', '.join(sorted(missing))} of batch {cycle.batch_ref.entity_key} are "
            "settled by judgment and carry no audit row, so nothing has looked at them",
        )
    return tuple(settling)


def _exit_of(
    kind: ConflictExitKind, exits: Mapping[ConflictExitKind, QualifiedUrn], *, batch_key: str
) -> ConflictExit:
    """Return the typed exit of *kind*, refusing when the caller named none.

    Raises:
        VerificationRefusedError: No reference was named for the exit the
            decision needs, so the cycle would stall with no way out.
    """
    ref = exits.get(kind)
    if ref is None:
        raise VerificationRefusedError(
            VerificationRefusal.EXIT_UNNAMED,
            f"batch {batch_key} needs a {kind.value} reference and the request names none, so "
            "the cycle would stop with no way out",
        )
    return ConflictExit(kind=kind, ref=ref)


def _open_repair_or_ask(
    reviewed: BatchVerificationCycle,
    *,
    exits: Mapping[ConflictExitKind, QualifiedUrn],
) -> tuple[BatchVerificationCycle, tuple[GateIdentityStr, ...], str]:
    """Open the bounded repair a blocker earns, or the operator question it forces.

    Returns:
        The advanced cycle, the criteria a repair is bounded to, and the
        sentence describing what happened.

    Raises:
        VerificationRefusedError: The exit the outcome needs is unnamed.
    """
    batch_key = reviewed.batch_ref.entity_key
    blocking = reviewed.blocking_criterion_ids
    changes = reviewed.advance(BatchVerificationStage.CHANGES_REQUIRED)
    if changes.repair_available:
        opened = _exit_of(ConflictExitKind.REPAIR_TASK, exits, batch_key=batch_key)
        repaired = changes.advance(BatchVerificationStage.REPAIR, exit=opened, spend_repair=True)
        return (
            repaired,
            blocking,
            f"criteria {', '.join(blocking)} are open, so one repair bounded to them is opened "
            f"for batch {batch_key}",
        )
    asked = changes.advance(
        BatchVerificationStage.OPERATOR_DECISION,
        exit=_exit_of(ConflictExitKind.OPERATOR_DECISION, exits, batch_key=batch_key),
    )
    return (
        asked,
        (),
        f"criteria {', '.join(blocking)} are still open after {changes.repairs_spent} repair(s), "
        f"so batch {batch_key} stops and asks the operator",
    )


def decide_batch_verification(
    cycle: BatchVerificationCycle,
    *,
    judgment_criterion_ids: Sequence[str],
    exits: Mapping[ConflictExitKind, QualifiedUrn],
    invalidation: HeadInvalidation | None = None,
) -> tuple[BatchVerificationCycle, BatchVerificationDecision]:
    """Walk *cycle* from checking to wherever its audit rows take it.

    The walk is taken on one exact head: the cycle's own. Every stage move
    made here is a same-head edge, so a decision reads as "given this
    code, here is where the Batch stands" without a caller having to ask
    which revision each step was about.

    Args:
        cycle: The cycle to advance, standing in the checking stage.
        judgment_criterion_ids: The criteria no deterministic proof can
            settle, which the audit must therefore cover.
        exits: Where each exit kind lands. Named by the caller because
            nothing here mints a Task or a pending action.
        invalidation: What the head move that preceded this walk cost,
            carried onto the decision so the answer says why the cycle
            re-opened.

    Returns:
        The advanced cycle and the decision describing it.

    Raises:
        VerificationRefusedError: A judgment criterion carries no audit
            row, or the walk needs an exit the request does not name.
        ValueError: The cycle is not standing in the checking stage, so
            the walk would start midway through.
    """
    if cycle.stage is not BatchVerificationStage.CHECKING:
        raise ValueError(
            f"a verification walk starts in {BatchVerificationStage.CHECKING.value}, not in "
            f"{cycle.stage.value}"
        )
    settling = _require_audit_coverage(cycle, judgment_criterion_ids)
    settled = tuple(sorted(item.criterion_id for item in settling if not item.blocks))
    batch_key = cycle.batch_ref.entity_key
    reviewed = cycle.advance(BatchVerificationStage.AUDIT_REVIEW)
    if reviewed.blocking_audits:
        advanced, repair_criterion_ids, reason = _open_repair_or_ask(reviewed, exits=exits)
    else:
        advanced = reviewed.advance(BatchVerificationStage.READY_TO_MERGE)
        repair_criterion_ids = ()
        reason = (
            f"batch {batch_key} is audited clear on generation {advanced.generation} and "
            f"{len(settled)} judgment criterion(s) are settled"
        )
    decision = BatchVerificationDecision(
        batch_ref=str(advanced.batch_ref),
        stage=advanced.stage,
        head_generation=advanced.generation,
        blocking_criterion_ids=advanced.blocking_criterion_ids,
        settled_criterion_ids=settled,
        repair_criterion_ids=repair_criterion_ids,
        repairs_spent=advanced.repairs_spent,
        exit=advanced.exit,
        invalidation=invalidation,
        reason=reason,
    )
    logger.info(
        f"decide_batch_verification batch={batch_key} head={advanced.generation} "
        f"stage={advanced.stage.value} blocking={len(decision.blocking_criterion_ids)} "
        f"settled={len(settled)} repairs_spent={advanced.repairs_spent}"
    )
    return advanced, decision


__all__ = [
    "BatchVerificationDecision",
    "ReviewerBrief",
    "VerificationRefusal",
    "VerificationRefusedError",
    "decide_batch_verification",
    "rebase_cycle",
    "require_reviewer_independence",
    "reviewer_attestation",
]
