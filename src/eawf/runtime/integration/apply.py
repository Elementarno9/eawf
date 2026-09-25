"""Which sealed candidates reach a Batch, in which order, and what applied.

Two decisions live here and neither of them touches a tree.

The first is order. Candidates are produced in parallel and sealed
whenever their Runs happen to finish, so the sequence they are applied in
cannot be the sequence they arrived in: the same three candidates would
integrate one way today and another way tomorrow, and the commit that
came out would be a different object each time. The order is therefore
taken over content alone -- the Task the work addresses and the tree it
produced -- so it is a property of what is being integrated rather than
of when anyone got there.

That choice survives the fact that a candidate's identity is derived
rather than minted. Two Runs that end at byte-identical trees for one
Task are one candidate, so a provider loss, a resume and a linked retry
all present the same key and land in the same position. Nothing about
the Run, the provider, the attempt count or the clock appears in the key,
because all four differ between two productions of the same work and none
of them changes what the work is.

The second decision is how far a run of candidates got. Application is
serial and stops at the first conflict: every later candidate would be
applied onto a tree the blocked one was supposed to have changed, so
whatever those applications concluded would be about a tree that is not
the one being delivered. The blocked candidate and its conflicting files
are handed back for a conflict record to be written about them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.delivery.integration import ConflictFile
from eawf.kernel.runtime.candidate import CandidateBundle, CandidateId
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt

logger = logging.getLogger(__name__)


class IntegrationRefusal(StrEnum):
    """The stable codes a Batch integration is refused with.

    The codes are declared once and imported by every module that raises
    one, so the apply path, the commit policy and the compare-and-swap
    cannot drift into three spellings of one refusal.
    """

    CANDIDATES_ABSENT = "integration_candidates_absent"
    CANDIDATE_REPEATED = "integration_candidate_repeated"
    TASK_AMBIGUOUS = "integration_task_ambiguous"
    BASE_DIVERGENT = "integration_base_divergent"
    SUBJECT_OVERFLOW = "integration_subject_overflow"
    GENERATION_SUPERSEDED = "integration_generation_superseded"
    WORKSPACE_ABSENT = "integration_workspace_absent"
    EXIT_UNNAMED = "integration_exit_unnamed"
    CANDIDATE_UNRESOLVABLE = "integration_candidate_unresolvable"


class IntegrationRefusedError(ValueError):
    """One Batch integration refused, with the code that says which rule.

    Attributes:
        code: The stable refusal code a caller routes on.
    """

    def __init__(self, code: IntegrationRefusal, detail: str) -> None:
        """Keep the code beside the sentence an operator reads.

        Args:
            code: The stable refusal code.
            detail: One sentence naming what was refused and why.
        """
        self.code = code
        super().__init__(f"{code.value}: {detail}")


#: The fields, in the order they are compared, that decide where a
#: candidate sits in the integration sequence. Spelled out as data so a
#: later edit to the key has to change this tuple too, and the test that
#: pins determinism reads the same declaration the sort does.
ORDER_KEY_FIELDS: Final[tuple[str, ...]] = (
    "task_ref",
    "resulting_tree_digest",
    "candidate_ref",
)


def order_key(
    *, task_ref: str, resulting_tree_digest: str, candidate_ref: str
) -> tuple[str, str, str]:
    """Return the position one candidate's content puts it at.

    Args:
        task_ref: The Task the work was done for, as its canonical URN.
        resulting_tree_digest: The digest of the tree the work produced.
        candidate_ref: The candidate's derived identity.

    Returns:
        The comparison key, in :data:`ORDER_KEY_FIELDS` order.
    """
    return (task_ref, resulting_tree_digest, candidate_ref)


def bundle_order_key(bundle: CandidateBundle) -> tuple[str, str, str]:
    """Return the position one sealed bundle takes in the sequence."""
    return order_key(
        task_ref=str(bundle.task_ref),
        resulting_tree_digest=bundle.resulting_tree_digest,
        candidate_ref=bundle.candidate_ref,
    )


def integration_order(bundles: Iterable[CandidateBundle]) -> tuple[CandidateBundle, ...]:
    """Return *bundles* in the one order they may be integrated in.

    Args:
        bundles: The Batch's sealed candidates, in any order.

    Returns:
        The same candidates, sorted by :func:`bundle_order_key`.

    Raises:
        IntegrationRefusedError: Nothing was offered; one candidate was
            offered twice; two candidates name one Task, so which tree is
            that Task's delivery has no answer; or the candidates were
            not all produced from one base, so applying them in sequence
            would stack work onto a tree it was never written against.
    """
    ordered = sorted(bundles, key=bundle_order_key)
    if not ordered:
        raise IntegrationRefusedError(
            IntegrationRefusal.CANDIDATES_ABSENT,
            "no sealed candidate was offered, so there is nothing to integrate",
        )
    _reject_repeated_candidate(ordered)
    _reject_ambiguous_task(ordered)
    _reject_divergent_base(ordered)
    logger.debug(f"integration_order candidates={len(ordered)} base={ordered[0].base_commit}")
    return tuple(ordered)


def _reject_repeated_candidate(ordered: Sequence[CandidateBundle]) -> None:
    """Refuse the same candidate offered twice.

    Raises:
        IntegrationRefusedError: One identity appears more than once,
            which would apply one tree's work twice.
    """
    seen: set[str] = set()
    for bundle in ordered:
        if bundle.candidate_ref in seen:
            raise IntegrationRefusedError(
                IntegrationRefusal.CANDIDATE_REPEATED,
                f"candidate {bundle.candidate_ref} is offered twice, so its work would be "
                "applied twice",
            )
        seen.add(bundle.candidate_ref)


def _reject_ambiguous_task(ordered: Sequence[CandidateBundle]) -> None:
    """Refuse two candidates that both claim to be one Task's delivery.

    Raises:
        IntegrationRefusedError: Two candidates name one Task. Under the
            Batch commit unit that Task gets one trailer, and nothing
            says which of the two trees it should name.
    """
    seen: set[str] = set()
    for bundle in ordered:
        task = str(bundle.task_ref)
        if task in seen:
            raise IntegrationRefusedError(
                IntegrationRefusal.TASK_AMBIGUOUS,
                f"task {bundle.task_ref.entity_key} has two sealed candidates, so which tree is "
                "its delivery has no answer",
            )
        seen.add(task)


def _reject_divergent_base(ordered: Sequence[CandidateBundle]) -> None:
    """Refuse candidates that were not all produced from one base.

    Raises:
        IntegrationRefusedError: Two candidates name different base
            commits.
    """
    base = ordered[0].base_commit
    for bundle in ordered[1:]:
        if bundle.base_commit != base:
            raise IntegrationRefusedError(
                IntegrationRefusal.BASE_DIVERGENT,
                f"candidate {bundle.candidate_ref} was produced from another base than "
                f"{ordered[0].candidate_ref}, so the two cannot be sequenced from one tree",
            )


class ApplyDisposition(StrEnum):
    """What applying one candidate into the integration workspace did."""

    APPLIED = "applied"
    CONFLICTED = "conflicted"


class CandidateApplication(BaseModel):
    """What the integration workspace reported about one candidate.

    Attributes:
        candidate_ref: The candidate that was applied.
        disposition: Whether its work landed.
        conflict_files: Every conflicting path with both sides of each
            hunk. Non-empty exactly when the candidate conflicted, so a
            conflict always carries the frame a record is written from.
        ahead: How far the candidate is ahead of the base it was applied
            over, counted by the workspace that tried it.
        behind: How far it is behind that base.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_ref: CandidateId
    disposition: ApplyDisposition
    conflict_files: tuple[ConflictFile, ...] = ()
    ahead: StrictNonNegativeInt = 0
    behind: StrictNonNegativeInt = 0

    @model_validator(mode="after")
    def _files_match_disposition(self) -> Self:
        """Tie the conflict frame to the disposition, both ways.

        Raises:
            ValueError: A conflict names no file, so nothing could be
                shown to a reader, or an applied candidate names one,
                which would report a conflict that did not block it.
        """
        conflicted = self.disposition is ApplyDisposition.CONFLICTED
        if conflicted and not self.conflict_files:
            raise ValueError("a conflicted candidate names at least one conflicting file")
        if not conflicted and self.conflict_files:
            raise ValueError("an applied candidate names no conflicting file")
        return self


#: How the driver asks the workspace to apply one candidate. The
#: workspace is injected rather than imported so the ordering and the
#: stop-at-first-conflict rule stay testable without a tree.
CandidateApplier = Callable[[CandidateBundle], CandidateApplication]


class SerialApply(BaseModel):
    """How far one serial pass over the ordered candidates got.

    Attributes:
        applied: Every candidate that landed, in integration order.
        blocked_on: The first candidate that conflicted, or ``None`` when
            the whole sequence landed.
        conflict_files: That candidate's conflicting files. Non-empty
            exactly when the pass was blocked.
        ahead: How far the blocking candidate is ahead of the base.
        behind: How far it is behind it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    applied: tuple[CandidateId, ...] = ()
    blocked_on: CandidateId | None = None
    conflict_files: tuple[ConflictFile, ...] = ()
    ahead: StrictNonNegativeInt = 0
    behind: StrictNonNegativeInt = 0

    @property
    def blocked(self) -> bool:
        """Return whether the pass stopped on a conflict."""
        return self.blocked_on is not None

    @model_validator(mode="after")
    def _outcome_is_coherent(self) -> Self:
        """Require the block, its frame and the applied list to agree.

        Raises:
            ValueError: A block carries no conflicting file, an unblocked
                pass carries one, the blocking candidate is also listed
                as applied, or a candidate is listed twice.
        """
        if self.blocked != bool(self.conflict_files):
            raise ValueError("conflict_files is non-empty exactly when the pass is blocked")
        if self.blocked_on is not None and self.blocked_on in self.applied:
            raise ValueError(f"candidate {self.blocked_on} cannot be applied and blocking")
        if len(set(self.applied)) != len(self.applied):
            raise ValueError("applied repeats a candidate")
        if not self.applied and self.blocked_on is None:
            raise ValueError("a pass that applied nothing and blocked on nothing did not run")
        return self


def apply_serially(ordered: Sequence[CandidateBundle], *, applier: CandidateApplier) -> SerialApply:
    """Apply *ordered* one at a time, stopping at the first conflict.

    Args:
        ordered: The candidates in :func:`integration_order` order.
        applier: How the workspace applies one candidate.

    Returns:
        The candidates that landed and, when one conflicted, that
        candidate with its conflicting files. Nothing after a conflict is
        attempted: those applications would be against a tree the blocked
        candidate was supposed to have changed.

    Raises:
        IntegrationRefusedError: *ordered* is empty.
        ValueError: The workspace answered about another candidate than
            the one it was handed.
    """
    if not ordered:
        raise IntegrationRefusedError(
            IntegrationRefusal.CANDIDATES_ABSENT,
            "no sealed candidate was offered, so there is nothing to apply",
        )
    applied: list[CandidateId] = []
    for bundle in ordered:
        application = applier(bundle)
        if application.candidate_ref != bundle.candidate_ref:
            raise ValueError(
                f"the workspace answered about {application.candidate_ref} when handed "
                f"{bundle.candidate_ref}"
            )
        if application.disposition is ApplyDisposition.CONFLICTED:
            logger.info(
                f"apply_serially blocked candidate={bundle.candidate_ref} "
                f"applied={len(applied)} files={len(application.conflict_files)}"
            )
            return SerialApply(
                applied=tuple(applied),
                blocked_on=application.candidate_ref,
                conflict_files=application.conflict_files,
                ahead=application.ahead,
                behind=application.behind,
            )
        applied.append(application.candidate_ref)
    return SerialApply(applied=tuple(applied))


class IntegrationChange(BaseModel):
    """The change set one ordered run of candidates delivers.

    Attributes:
        changed_paths: Every repository-relative path the sequence
            touched, sorted and deduplicated so two candidates that both
            edited one file name it once.
        task_refs: The Tasks whose work the sequence carries, in
            integration order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    changed_paths: tuple[str, ...] = Field(min_length=1)
    task_refs: tuple[str, ...] = Field(min_length=1)


def integration_change(ordered: Sequence[CandidateBundle]) -> IntegrationChange:
    """Return the union of what *ordered* changes and whose work it is.

    Args:
        ordered: The candidates in integration order.

    Returns:
        The deduplicated path set and the Tasks, in integration order.

    Raises:
        IntegrationRefusedError: *ordered* is empty.
    """
    if not ordered:
        raise IntegrationRefusedError(
            IntegrationRefusal.CANDIDATES_ABSENT,
            "no sealed candidate was offered, so there is no change set",
        )
    paths = {path for bundle in ordered for path in bundle.changed_paths}
    return IntegrationChange(
        changed_paths=tuple(sorted(paths)),
        task_refs=tuple(str(bundle.task_ref) for bundle in ordered),
    )


__all__ = [
    "ORDER_KEY_FIELDS",
    "ApplyDisposition",
    "CandidateApplication",
    "CandidateApplier",
    "IntegrationChange",
    "IntegrationRefusal",
    "IntegrationRefusedError",
    "SerialApply",
    "apply_serially",
    "bundle_order_key",
    "integration_change",
    "integration_order",
    "order_key",
]
