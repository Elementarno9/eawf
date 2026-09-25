"""Whether a standing submission has earned a bundle yet.

The six checks of :class:`~eawf.kernel.runtime.candidate.SealCheck` are
compiled total at import here, the same way the resume guards are: a
member with no evaluator is a startup failure rather than a check that
silently passes at the one moment it was supposed to refuse. Each
evaluator fails closed, so an absent report binding and an unreadable
lease are unmet checks rather than unknown ones.

Nothing in this module opens a session or writes a line. It is handed the
records the caller already read and the lease the caller already
resolved, and it answers with the checks that did not hold; appending the
bundle belongs to the daemon verb, so the decision and the write stay
separable and the decision stays testable without a tree.

The candidate's three record kinds share the run collection's ledger
rather than a collection of their own. They are facts about one Run's
episode, they are read in the same pass as the attempt and the lineage
that surround them, and a prefixed key keeps them distinguishable from
the compacted Run records that ledger also holds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, model_validator

from eawf.kernel.runtime.candidate import (
    DELIVERABLE_VERDICTS,
    CandidateBundle,
    CandidateId,
    CandidateReportBinding,
    CandidateSubmission,
    SealCheck,
)
from eawf.kernel.runtime.lease import WorkLease
from eawf.kernel.state.epoch2.run import WriteSetPath
from eawf.kernel.store.ledger import LedgerRecord
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)


#: The ledger-line key prefix a submission is filed under. Prefixed for
#: the reason every other runtime line is: the run collection also holds
#: compacted Run records, and a shared key would make one record look
#: like it was in the document and the ledger at once.
SUBMISSION_KEY_PREFIX: Final = "CSB-"

#: The ledger-line key prefix an accepted report binding is filed under.
BINDING_KEY_PREFIX: Final = "CRB-"

#: The ledger-line key prefix a sealed bundle is filed under.
BUNDLE_KEY_PREFIX: Final = "CBN-"

#: The status a submission line records: the submission's own
#: ``report_binding``, which never reads anything else.
SUBMISSION_STATUS: Final = "pending"

#: The status a binding line records.
BINDING_STATUS: Final = "bound"

#: The status a bundle line records.
BUNDLE_STATUS: Final = "sealed"


class SealCheckTableError(ValueError):
    """The seal-check table is not total over the declared checks."""


@dataclass(frozen=True, slots=True)
class SealInputs:
    """Everything a seal check is allowed to read.

    Attributes:
        submission: The standing claim being judged.
        binding: The accepted report bound to it, or ``None`` when no
            report has been accepted yet.
        lease: The Run's live lease, or ``None`` when it holds none.
    """

    submission: CandidateSubmission
    binding: CandidateReportBinding | None
    lease: WorkLease | None


CheckEvaluator = Callable[[SealInputs], bool]


class SealOutcome(BaseModel):
    """What one seal decision concluded about a standing submission.

    Attributes:
        candidate_ref: The candidate the decision is about.
        sealed: Whether a bundle now exists for it.
        failed_checks: Every check that did not hold, in declaration
            order. Empty exactly when the candidate sealed.
        bundle: The bundle the seal produced, or ``None``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_ref: CandidateId
    sealed: bool
    failed_checks: tuple[SealCheck, ...] = ()
    bundle: CandidateBundle | None = None

    @model_validator(mode="after")
    def _seal_and_its_evidence_agree(self) -> Self:
        """Tie the verdict to the bundle and the failures, both ways.

        Raises:
            ValueError: A sealed outcome names a failed check or carries
                no bundle, or an unsealed one carries a bundle or names
                no failure. Either would report a seal that the checks
                did not take.
        """
        if self.sealed and (self.failed_checks or self.bundle is None):
            raise ValueError("a sealed outcome carries its bundle and no failed check")
        if not self.sealed and (self.bundle is not None or not self.failed_checks):
            raise ValueError("an unsealed outcome names at least one failed check and no bundle")
        return self


def path_within_roots(path: str, roots: Sequence[WriteSetPath]) -> bool:
    """Return whether *path* lies at or under one of *roots*.

    A prefix match alone is not enough: ``src`` must not admit
    ``srcfoo``, so a root matches only the whole path or a path whose
    next character starts a new segment.

    Args:
        path: A repository-relative path.
        roots: The roots a write may land under.

    Returns:
        Whether some root contains *path*.
    """
    return any(path == root or path.startswith(f"{root.rstrip('/')}/") for root in roots)


def _report_bound(inputs: SealInputs) -> bool:
    """Return whether a terminal report has been accepted for the candidate."""
    return inputs.binding is not None


def _tree_digest_agrees(inputs: SealInputs) -> bool:
    """Return whether the report was accepted about the submitted tree."""
    binding = inputs.binding
    return binding is not None and (
        binding.resulting_tree_digest == inputs.submission.resulting_tree_digest
    )


def _verdict_admits_delivery(inputs: SealInputs) -> bool:
    """Return whether the accepted verdict proposes the work for integration."""
    binding = inputs.binding
    return binding is not None and binding.verdict in DELIVERABLE_VERDICTS


def _paths_within_write_set(inputs: SealInputs) -> bool:
    """Return whether every claimed path lies under the lease's roots."""
    lease = inputs.lease
    if lease is None:
        return False
    return all(
        path_within_roots(path, lease.writable_roots) for path in inputs.submission.changed_paths
    )


def _workspace_generation_current(inputs: SealInputs) -> bool:
    """Return whether the tree was produced in the lease's current materialization."""
    lease = inputs.lease
    return (
        lease is not None and lease.workspace_generation == inputs.submission.workspace_generation
    )


def _base_commit_agrees(inputs: SealInputs) -> bool:
    """Return whether the claim started from the commit the lease names."""
    lease = inputs.lease
    return lease is not None and lease.base_commit == inputs.submission.base_commit


def compile_seal_checks(
    evaluators: Mapping[SealCheck, CheckEvaluator],
) -> Mapping[SealCheck, CheckEvaluator]:
    """Return *evaluators* once it answers every declared seal check.

    Args:
        evaluators: One predicate per check.

    Returns:
        The table, unchanged.

    Raises:
        SealCheckTableError: A check has no evaluator, which would let a
            bundle seal without the proof that check names.
    """
    missing = sorted(check.value for check in SealCheck if check not in evaluators)
    if missing:
        raise SealCheckTableError(f"no evaluator declared for seal check {', '.join(missing)}")
    return evaluators


#: The total check table, compiled at import. Each predicate fails
#: closed: an absent binding or lease is an unmet check, because the
#: record that is missing is exactly the proof the check asks for.
SEAL_CHECKS: Final[Mapping[SealCheck, CheckEvaluator]] = compile_seal_checks(
    {
        SealCheck.REPORT_BOUND: _report_bound,
        SealCheck.TREE_DIGEST_AGREES: _tree_digest_agrees,
        SealCheck.VERDICT_ADMITS_DELIVERY: _verdict_admits_delivery,
        SealCheck.PATHS_WITHIN_WRITE_SET: _paths_within_write_set,
        SealCheck.WORKSPACE_GENERATION_CURRENT: _workspace_generation_current,
        SealCheck.BASE_COMMIT_AGREES: _base_commit_agrees,
    }
)


def failed_seal_checks(inputs: SealInputs) -> tuple[SealCheck, ...]:
    """Return every seal check that does not hold, in declaration order."""
    return tuple(check for check in SealCheck if not SEAL_CHECKS[check](inputs))


def seal_candidate(inputs: SealInputs, *, now: datetime) -> SealOutcome:
    """Decide whether one standing submission has earned a bundle.

    Args:
        inputs: The submission, the report bound to it, and the Run's
            lease.
        now: The instant a seal would be stamped at.

    Returns:
        The decision. A single unmet check leaves the submission
        standing and unsealed, naming every check that did not hold.
    """
    failed = failed_seal_checks(inputs)
    submission = inputs.submission
    if failed:
        logger.info(
            f"seal_candidate unsealed candidate={submission.candidate_ref} failed={len(failed)}"
        )
        return SealOutcome(
            candidate_ref=submission.candidate_ref, sealed=False, failed_checks=failed
        )
    binding = inputs.binding
    assert binding is not None, "report_bound held, so a binding was read"
    bundle = CandidateBundle(
        candidate_ref=submission.candidate_ref,
        run_ref=submission.run_ref,
        task_ref=submission.task_ref,
        submission_ref=submission.submission_ref,
        report_digest=binding.report_digest,
        verdict=binding.verdict,
        changed_paths=submission.changed_paths,
        resulting_tree_digest=submission.resulting_tree_digest,
        base_commit=submission.base_commit,
        workspace_generation=submission.workspace_generation,
        checks_passed=tuple(SealCheck),
        sealed_at=now,
    )
    return SealOutcome(candidate_ref=submission.candidate_ref, sealed=True, bundle=bundle)


def submission_of(
    records: tuple[LedgerRecord, ...], candidate_ref: str
) -> CandidateSubmission | None:
    """Return the standing submission of one candidate, or ``None``.

    Raises:
        ValidationError: A line claims to be a submission and does not
            validate as one, which means the ledger is corrupt rather
            than merely unfamiliar.
    """
    for item in records:
        if item.payload.get("payload_kind") != "candidate_submission":
            continue
        submission = CandidateSubmission.model_validate(item.payload)
        if submission.candidate_ref == candidate_ref:
            return submission
    return None


def binding_of(
    records: tuple[LedgerRecord, ...], candidate_ref: str
) -> CandidateReportBinding | None:
    """Return the accepted report binding of one candidate, or ``None``."""
    for item in records:
        if item.payload.get("payload_kind") != "candidate_report_binding":
            continue
        binding = CandidateReportBinding.model_validate(item.payload)
        if binding.candidate_ref == candidate_ref:
            return binding
    return None


def bundle_of(records: tuple[LedgerRecord, ...], candidate_ref: str) -> CandidateBundle | None:
    """Return the sealed bundle of one candidate, or ``None``."""
    for item in records:
        if item.payload.get("payload_kind") != "candidate_bundle":
            continue
        bundle = CandidateBundle.model_validate(item.payload)
        if bundle.candidate_ref == candidate_ref:
            return bundle
    return None


def submission_record(submission: CandidateSubmission) -> LedgerRecord:
    """Return the run-ledger line one candidate submission is filed as."""
    return _run_line(
        key=f"{SUBMISSION_KEY_PREFIX}{submission.candidate_ref}",
        status=SUBMISSION_STATUS,
        recorded_at=submission.submitted_at,
        payload=submission.model_dump(mode="json"),
    )


def binding_record(binding: CandidateReportBinding) -> LedgerRecord:
    """Return the run-ledger line one accepted report binding is filed as."""
    return _run_line(
        key=f"{BINDING_KEY_PREFIX}{binding.candidate_ref}",
        status=BINDING_STATUS,
        recorded_at=binding.bound_at,
        payload=binding.model_dump(mode="json"),
    )


def bundle_record(bundle: CandidateBundle) -> LedgerRecord:
    """Return the run-ledger line one sealed bundle is filed as."""
    return _run_line(
        key=f"{BUNDLE_KEY_PREFIX}{bundle.candidate_ref}",
        status=BUNDLE_STATUS,
        recorded_at=bundle.sealed_at,
        payload=bundle.model_dump(mode="json"),
    )


def _run_line(
    *, key: str, status: str, recorded_at: datetime, payload: dict[str, Any]
) -> LedgerRecord:
    """Return one candidate line of the run collection's ledger."""
    return LedgerRecord(
        collection=Epoch2Collection.RUN,
        record_key=key,
        status=status,
        recorded_at=recorded_at,
        payload=payload,
    )


__all__ = [
    "BINDING_KEY_PREFIX",
    "BUNDLE_KEY_PREFIX",
    "SEAL_CHECKS",
    "SUBMISSION_KEY_PREFIX",
    "CheckEvaluator",
    "SealCheckTableError",
    "SealInputs",
    "SealOutcome",
    "binding_of",
    "binding_record",
    "bundle_of",
    "bundle_record",
    "compile_seal_checks",
    "failed_seal_checks",
    "path_within_roots",
    "seal_candidate",
    "submission_of",
    "submission_record",
]
