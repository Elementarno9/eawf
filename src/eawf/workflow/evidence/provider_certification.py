"""Read back the committed native-canary evidence, claim by claim.

The conformance runner of :mod:`eawf.runtime.runtimes.conformance` is the
only writer of a :class:`DriverCertification`, and it writes one into a
daemon's own store. A release readiness sweep runs somewhere else
entirely -- against a checkout, at a pinned revision, often on another
machine -- so it cannot reach that store. What it can read is the
*export*: the runner's records, committed into the repository as an
artifact, exactly as the cutover rehearsal records are.

This module is the consumer of that export. It is what lets the
``provider`` and ``membership`` rows of a checkpoint say "this is proven"
as a fact about evidence on disk rather than as a claim about something
somebody ran once.

Three properties make the reading falsifiable.

**The advertised set is declared, not inferred.** ``advertised`` names
every runtime tuple the checkpoint claims. Grading only the tuples that
happen to carry a certification would make the row pass by deleting a
claim, which is the opposite of evidence.

**A run log is not a certification.** A runtime that started, answered
and exited proves the binary works; it proves nothing about the contract
the checkpoint advertises. An advertised tuple backed only by a run log
is therefore uncertified, and the finding says so in those words rather
than reporting a generic absence.

**A certification cites the stage history that earned it.** The export
carries the records verbatim, so the reader re-checks the one thing the
runner promises: a passed ``certify`` stage sitting on the contiguous
``probe`` then ``canary`` run before it. A record whose history does not
end that way is refused here even though it validated as a model.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, ValidationError

from eawf.kernel.runtime.certification import (
    ConformanceStage,
    ConformanceStageRecord,
    DriverCertification,
)
from eawf.kernel.runtime.provider import (
    ArtifactUrn,
    CapabilityId,
    Digest,
    DriverCertificationUrn,
    DriverManifestUrn,
)
from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import ReferenceStr, ReleaseKeyStr
from eawf.kernel.state.epoch2.authority import CanaryRepositoryRef
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.kernel.state.ids import RE_PROJECT_CODE
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: Where each release's native-canary evidence export is committed,
#: relative to the repo root, by release key. One dated directory per
#: rung rather than a stable filename, because a later rung re-runs the
#: conformance and its export is a second record rather than an edit of
#: the earlier one -- which also keeps an earlier rung's readiness
#: reading the evidence it was cut on.
CANARY_EVIDENCE_DIRS: Final[Mapping[str, tuple[str, ...]]] = {
    "REL-0.7.0.dev3": (
        ".ea",
        "artifacts",
        "evidence",
        "2026-09-18-dev3-conformance",
    ),
    "REL-0.7.0.dev4": (
        ".ea",
        "artifacts",
        "evidence",
        "2026-09-26-dev4-conformance",
    ),
}

#: The export document inside each :data:`CANARY_EVIDENCE_DIRS` entry.
CANARY_EVIDENCE_FILENAME: Final[str] = "native-canary-evidence.json"

#: The stage sequence a certification's history has to end in. The
#: runner appends ``certify`` onto the passed ``probe`` and ``canary`` it
#: cites, so this is the tail the reader re-checks rather than trusting.
CERTIFYING_STAGES: Final[tuple[ConformanceStage, ...]] = ("probe", "canary", "certify")

#: The one project code the canary registry grammar admits for a row that
#: a membership record may name. Re-stated as the model's own pattern so
#: an export naming a code no registry could carry fails at load.
_PROJECT_CODE_PATTERN: Final[str] = RE_PROJECT_CODE.pattern


class CanaryEvidenceGap(StrEnum):
    """Why the committed evidence does not back a checkpoint's claims.

    The split between *absent* and *disagreeing* evidence is the same one
    the cutover rehearsal draws, and for the same reason: an absence
    needs the producer run, a disagreement needs the claim withdrawn or
    the defect fixed.

    Values:
        MISSING_EXPORT: No export is committed at all.
        UNREADABLE_EXPORT: An export is present but is not an export.
        NO_CERTIFICATION: An advertised tuple carries no certification.
        RUN_LOG_ONLY: An advertised tuple carries a run log and nothing
            else, which records that the binary ran rather than that the
            contract holds.
        NO_CERTIFY_STAGE: A certification's stage history does not end in
            a passed ``certify`` on a contiguous ``probe`` and ``canary``.
        CERTIFICATION_NOT_VERIFIED: A certification's freshness or its
            installation trust does not admit the claim.
        CAPABILITY_UNCERTIFIED: A capability the claim requires is not
            certified ``verified``.
        MEMBERSHIP_UNRESOLVED: A declared acceptance bundle resolves to no
            recorded Milestone.
        MILESTONE_INCOMPLETE: A declared bundle's Milestone is recorded
            but has not COMPLETED.
        UNDECLARED_CANARY: A Milestone is recorded in a repository the
            export does not declare a canary.
        ISOLATION_UNRECORDED: No production-root digest pair was recorded
            for the rehearsal.
        PRODUCTION_ROOT_TOUCHED: The recorded pair is not equal, so the
            rehearsal reached outside its canary.
    """

    MISSING_EXPORT = "missing_export"
    UNREADABLE_EXPORT = "unreadable_export"
    NO_CERTIFICATION = "no_certification"
    RUN_LOG_ONLY = "run_log_only"
    NO_CERTIFY_STAGE = "no_certify_stage"
    CERTIFICATION_NOT_VERIFIED = "certification_not_verified"
    CAPABILITY_UNCERTIFIED = "capability_uncertified"
    MEMBERSHIP_UNRESOLVED = "membership_unresolved"
    MILESTONE_INCOMPLETE = "milestone_incomplete"
    UNDECLARED_CANARY = "undeclared_canary"
    ISOLATION_UNRECORDED = "isolation_unrecorded"
    PRODUCTION_ROOT_TOUCHED = "production_root_touched"


#: The gaps that mean evidence was never produced, as opposed to evidence
#: that was produced and disagrees. A sweep reports the first group as
#: unproven and the second as failing.
ABSENCE_GAPS: Final[frozenset[CanaryEvidenceGap]] = frozenset(
    {
        CanaryEvidenceGap.MISSING_EXPORT,
        CanaryEvidenceGap.ISOLATION_UNRECORDED,
    }
)


class CanaryFinding(_StrictModel):
    """One reason the committed evidence falls short of a claim.

    Attributes:
        subject: What the finding is about -- a runtime id, an
            acceptance-bundle reference, or ``""`` when it is about the
            export as a whole.
        gap: Which gap fired.
        detail: One line naming what is missing or wrong.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    gap: CanaryEvidenceGap
    detail: str

    @property
    def is_absence(self) -> bool:
        """Return whether this finding is a missing producer rather than a failure."""
        return self.gap in ABSENCE_GAPS


class RuntimeClaim(_StrictModel):
    """One runtime tuple the checkpoint advertises.

    The claim is what makes the row falsifiable. Without it the reader
    would grade whatever certifications happen to be present, and an
    advertised runtime could be cleared by never mentioning it.

    Attributes:
        runtime_id: The matrix row the tuple belongs to, e.g.
            ``claude-code``.
        manifest_ref: The driver manifest the tuple is addressed by; the
            key a certification is matched on.
        manifest_digest: The manifest's digest, which a certification of
            another build of the same manifest would not carry.
        required_capabilities: The capability ids the advertised contract
            needs, each of which must be certified ``verified``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: Annotated[str, Field(min_length=1, max_length=64)]
    manifest_ref: DriverManifestUrn
    manifest_digest: Digest
    required_capabilities: Annotated[tuple[CapabilityId, ...], Field(min_length=1)]


class RuntimeRunLog(_StrictModel):
    """A record that one advertised runtime ran, which is not a certification.

    Kept as its own row rather than folded into the certification list so
    the reader can tell "nothing was produced" from "something was
    produced and it is the wrong thing". The second is the case a
    checkpoint most easily talks itself into: a green run looks like
    evidence of conformance and is evidence only of execution.

    Attributes:
        runtime_id: The matrix row the run belongs to.
        manifest_ref: The driver manifest the run used.
        ran_at: When the run happened.
        exit_code: What the run exited with.
        log_ref: Where the run's log is filed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: Annotated[str, Field(min_length=1, max_length=64)]
    manifest_ref: DriverManifestUrn
    ran_at: UtcDatetime
    exit_code: int
    log_ref: ArtifactUrn


class CertificationRecord(_StrictModel):
    """One runner-written certification, with the URN it is cited by.

    The certification itself is the kernel record verbatim, so the export
    carries what the runner wrote rather than a summary of it -- including
    every :class:`CapabilityCertification` row and the stage history the
    certify stage was appended to.

    Attributes:
        certification_urn: The URN a readiness row cites this record by.
        runtime_id: The matrix row the certified tuple belongs to.
        tuple_digest: The digest the runner addresses the tuple by in its
            stage journal.
        certification: The record the runner wrote.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    certification_urn: DriverCertificationUrn
    runtime_id: Annotated[str, Field(min_length=1, max_length=64)]
    tuple_digest: Digest
    certification: DriverCertification

    @property
    def stage_sequence(self) -> tuple[str, ...]:
        """Return the stages of the cited history, in append order."""
        return tuple(row.stage for row in self.certification.stage_history)

    def certify_findings(self) -> tuple[CanaryFinding, ...]:
        """Return why this record's stage history does not earn a certification.

        Returns:
            Empty when the history ends in a passed ``certify`` sitting on
            a contiguous passed ``probe`` and ``canary``; one finding
            naming the observed sequence otherwise.
        """
        tail = tuple(self.certification.stage_history[-len(CERTIFYING_STAGES) :])
        if _passed_certifying_tail(tail):
            return ()
        return (
            CanaryFinding(
                subject=self.runtime_id,
                gap=CanaryEvidenceGap.NO_CERTIFY_STAGE,
                detail=(
                    f"the certification cites {list(self.stage_sequence)} rather than a passed "
                    f"{list(CERTIFYING_STAGES)} run, so no certify stage earned it"
                ),
            ),
        )

    def freshness_findings(self) -> tuple[CanaryFinding, ...]:
        """Return why this record's own status does not admit the claim.

        Returns:
            Empty when the record is ``verified`` and its installation is
            not quarantined; one finding per failing axis otherwise. The
            two axes are reported separately because a stale certification
            of a trusted install and a fresh certification of a
            quarantined one need different repairs.
        """
        findings: list[CanaryFinding] = []
        record = self.certification
        if record.overall_status != "verified":
            findings.append(
                CanaryFinding(
                    subject=self.runtime_id,
                    gap=CanaryEvidenceGap.CERTIFICATION_NOT_VERIFIED,
                    detail=(
                        f"overall_status is {record.overall_status!r}; an advertised runtime "
                        f"needs a 'verified' certification"
                    ),
                )
            )
        if record.install_trust == "quarantined":
            trigger = record.quarantine_trigger
            findings.append(
                CanaryFinding(
                    subject=self.runtime_id,
                    gap=CanaryEvidenceGap.CERTIFICATION_NOT_VERIFIED,
                    detail=(
                        f"the installation is quarantined on "
                        f"{'unknown' if trigger is None else trigger.value}; roll the profile "
                        f"back or withdraw the claim"
                    ),
                )
            )
        return tuple(findings)

    def capability_findings(self, required: Sequence[str]) -> tuple[CanaryFinding, ...]:
        """Return the required capabilities this record does not certify.

        Args:
            required: Capability ids the advertised contract needs.

        Returns:
            One finding per capability that is absent from the record or
            certified at anything other than ``verified``.
        """
        certified = {row.capability_id: row.status for row in self.certification.capabilities}
        findings: list[CanaryFinding] = []
        for capability_id in required:
            status = certified.get(capability_id)
            if status == "verified":
                continue
            observed = "absent from the certification" if status is None else repr(status)
            findings.append(
                CanaryFinding(
                    subject=self.runtime_id,
                    gap=CanaryEvidenceGap.CAPABILITY_UNCERTIFIED,
                    detail=f"capability {capability_id!r} is {observed}, not 'verified'",
                )
            )
        return tuple(findings)


class CanaryMilestoneRecord(_StrictModel):
    """One acceptance bundle as the canary recorded it.

    Attributes:
        reference: The acceptance-bundle reference a checkpoint's
            ``membership_refs`` names this Milestone by.
        project_code: The canary the Milestone ran in.
        milestone_id: The Milestone's own identifier inside that canary.
        status: The Milestone's recorded lifecycle status.
        bundle_digest: The digest of the sealed acceptance bundle.
        recorded_at: When the record was taken.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference: ReferenceStr
    project_code: Annotated[str, Field(pattern=_PROJECT_CODE_PATTERN)]
    milestone_id: Annotated[str, Field(min_length=1, max_length=64)]
    status: MilestoneStatus
    bundle_digest: Digest
    recorded_at: UtcDatetime


class ProductionRootIsolation(_StrictModel):
    """The production root's digest before and after a canary rehearsal.

    The pair is the whole record. A rehearsal that reports "nothing
    happened outside the canary" is reporting its own opinion; two
    digests taken over the production root's bytes are a fact anyone can
    recompute, and an unequal pair is the finding whatever the rehearsal
    claims.

    Attributes:
        production_root_label: How the production root is named in the
            record. A label rather than a path, so an export carries no
            machine-specific location.
        rehearsal_scope: What the rehearsal actually drove, so a narrower
            observation cannot be read as the full Milestone rehearsal.
        before_digest: The production root's digest before the rehearsal.
        after_digest: Its digest afterwards.
        observed_at: When the pair was taken.
        evidence_ref: Where the rehearsal's own record is filed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    production_root_label: Annotated[str, Field(min_length=1, max_length=120)]
    rehearsal_scope: Annotated[str, Field(min_length=1, max_length=120)]
    before_digest: Digest
    after_digest: Digest
    observed_at: UtcDatetime
    evidence_ref: ArtifactUrn

    @property
    def untouched(self) -> bool:
        """Return whether the production root's bytes are unchanged."""
        return self.before_digest == self.after_digest


class CanaryEvidence(_StrictModel):
    """One checkpoint's committed native-canary evidence.

    Attributes:
        schema_version: Record schema tag.
        release_key: The checkpoint the evidence was produced for.
        canaries: The disposable repositories the rehearsal ran in. A
            Milestone recorded anywhere else is refused.
        advertised: Every runtime tuple the checkpoint claims.
        certifications: The runner-written records, verbatim.
        run_logs: Runs that happened without producing a certification.
        milestones: The acceptance bundles the canaries recorded.
        isolation: The production-root digest pair, when one was taken.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["native-canary-evidence/v1"]
    release_key: ReleaseKeyStr
    canaries: tuple[CanaryRepositoryRef, ...] = ()
    advertised: tuple[RuntimeClaim, ...] = ()
    certifications: tuple[CertificationRecord, ...] = ()
    run_logs: tuple[RuntimeRunLog, ...] = ()
    milestones: tuple[CanaryMilestoneRecord, ...] = ()
    isolation: ProductionRootIsolation | None = None

    def certification_for(self, manifest_ref: str) -> CertificationRecord | None:
        """Return the record certifying *manifest_ref*, if the export carries one.

        Args:
            manifest_ref: The driver manifest URN a claim names.

        Returns:
            The matching record, or ``None``.
        """
        for record in self.certifications:
            if record.certification.manifest_ref == manifest_ref:
                return record
        return None

    def run_logs_for(self, manifest_ref: str) -> tuple[RuntimeRunLog, ...]:
        """Return every run log taken of *manifest_ref*.

        Args:
            manifest_ref: The driver manifest URN a claim names.

        Returns:
            The matching run logs, in export order.
        """
        return tuple(row for row in self.run_logs if row.manifest_ref == manifest_ref)

    def milestone_for(self, reference: str) -> CanaryMilestoneRecord | None:
        """Return the Milestone recorded under *reference*, if any.

        Args:
            reference: An acceptance-bundle reference.

        Returns:
            The matching record, or ``None``.
        """
        for record in self.milestones:
            if record.reference == reference:
                return record
        return None

    def declares_canary(self, project_code: str) -> bool:
        """Return whether *project_code* is one of the declared canaries.

        Args:
            project_code: The registry code a Milestone record names.

        Returns:
            ``True`` when the export declares a canary under that code.
        """
        return any(ref.project_code == project_code for ref in self.canaries)

    def isolation_findings(self) -> tuple[CanaryFinding, ...]:
        """Return why the recorded digest pair does not clear the canary fence.

        Returns:
            Empty exactly when a pair was recorded and it is equal. An
            absent record is an absence finding -- the rehearsal has not
            run -- and an unequal pair is a failure, because the
            rehearsal ran and reached outside its canary.
        """
        record = self.isolation
        if record is None:
            return (
                CanaryFinding(
                    subject=self.release_key,
                    gap=CanaryEvidenceGap.ISOLATION_UNRECORDED,
                    detail=(
                        "no production-root digest pair was recorded, so the rehearsal has "
                        "not shown the production root was left alone"
                    ),
                ),
            )
        if record.untouched:
            return ()
        return (
            CanaryFinding(
                subject=self.release_key,
                gap=CanaryEvidenceGap.PRODUCTION_ROOT_TOUCHED,
                detail=(
                    f"the {record.production_root_label!r} digest moved from "
                    f"{_short(record.before_digest)} to {_short(record.after_digest)} across "
                    f"the {record.rehearsal_scope!r} rehearsal"
                ),
            ),
        )


def _short(digest: str) -> str:
    """Return the leading hex of *digest*, without its ``sha256:`` prefix.

    Args:
        digest: A ``sha256:``-prefixed digest.

    Returns:
        Twelve hex characters, which is enough for an operator to tell
        two digests apart in one line of prose.
    """
    return digest.removeprefix("sha256:")[:12]


def _passed_certifying_tail(tail: tuple[ConformanceStageRecord, ...]) -> bool:
    """Return whether *tail* is a passed probe, canary and certify run.

    Args:
        tail: The last records of a certification's stage history.

    Returns:
        ``True`` when the tail is exactly the three certifying stages, in
        order, each passed.
    """
    if tuple(row.stage for row in tail) != CERTIFYING_STAGES:
        return False
    return all(row.outcome == "passed" for row in tail)


def canary_evidence_path(repo_root: Path, release_key: str) -> Path:
    """Return the export path of *release_key* in the checkout at *repo_root*.

    Args:
        repo_root: Checkout the export was committed in.
        release_key: The checkpoint whose export is wanted.

    Returns:
        The export document's path, whether or not it exists.

    Raises:
        TypeError: When *repo_root* is not a :class:`~pathlib.Path`.
        KeyError: When no export directory is mapped for *release_key*.
    """
    if not isinstance(repo_root, Path):
        raise TypeError(f"repo_root must be Path; got {type(repo_root).__name__}")
    return repo_root.joinpath(*CANARY_EVIDENCE_DIRS[release_key], CANARY_EVIDENCE_FILENAME)


def load_canary_evidence(repo_root: Path, release_key: str) -> CanaryEvidence | None:
    """Return the committed export of *release_key*, or ``None`` when absent.

    Args:
        repo_root: Checkout the export was committed in.
        release_key: The checkpoint whose export is wanted.

    Returns:
        The validated export, or ``None`` when no export is committed --
        including when no export directory is mapped for *release_key*,
        which is the same absence seen one step earlier.

    Raises:
        TypeError: When *repo_root* is not a :class:`~pathlib.Path`.
        ValueError: When an export is present but is not valid JSON, does
            not validate, or names a release other than *release_key*. An
            unreadable export is not an absent one, so it must never read
            as clean; and one filed under the wrong rung would lend that
            rung evidence recorded for another.
    """
    if release_key not in CANARY_EVIDENCE_DIRS:
        return None
    path = canary_evidence_path(repo_root, release_key)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"canary evidence {path.name!r} is not valid JSON: {exc}") from exc
    try:
        evidence = CanaryEvidence.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            f"canary evidence {path.name!r} does not validate: "
            f"{exc.error_count()} error(s); first: {exc.errors()[0]['msg']}"
        ) from exc
    if evidence.release_key != release_key:
        raise ValueError(
            f"canary evidence {path.name!r} names release {evidence.release_key!r} "
            f"but is filed for {release_key!r}"
        )
    logger.info(
        f"load_canary_evidence release_key={evidence.release_key!r} "
        f"advertised={len(evidence.advertised)} certifications={len(evidence.certifications)} "
        f"milestones={len(evidence.milestones)} "
        f"isolation_findings={len(evidence.isolation_findings())}"
    )
    return evidence


def _claim_findings(evidence: CanaryEvidence, claim: RuntimeClaim) -> tuple[CanaryFinding, ...]:
    """Return every gap between one advertised claim and the export.

    Args:
        evidence: The committed export.
        claim: The advertised runtime tuple being graded.

    Returns:
        The findings, empty when the claim is fully certified.
    """
    record = evidence.certification_for(claim.manifest_ref)
    if record is None:
        logs = evidence.run_logs_for(claim.manifest_ref)
        if logs:
            return (
                CanaryFinding(
                    subject=claim.runtime_id,
                    gap=CanaryEvidenceGap.RUN_LOG_ONLY,
                    detail=(
                        f"{len(logs)} run log(s) are filed for {claim.manifest_ref!r} and no "
                        f"certification; a run log records that the binary ran, never that "
                        f"the advertised contract holds"
                    ),
                ),
            )
        return (
            CanaryFinding(
                subject=claim.runtime_id,
                gap=CanaryEvidenceGap.NO_CERTIFICATION,
                detail=(
                    f"no runner-written certification covers {claim.manifest_ref!r}; run the "
                    f"conformance probe, canary and certify stages for the tuple or withdraw "
                    f"the claim"
                ),
            ),
        )
    findings: list[CanaryFinding] = []
    if record.certification.manifest_digest != claim.manifest_digest:
        findings.append(
            CanaryFinding(
                subject=claim.runtime_id,
                gap=CanaryEvidenceGap.NO_CERTIFICATION,
                detail=(
                    f"the certification of {claim.manifest_ref!r} carries manifest_digest "
                    f"{_short(record.certification.manifest_digest)}, but the claim advertises "
                    f"{_short(claim.manifest_digest)}, so it certifies another build"
                ),
            )
        )
    findings.extend(record.certify_findings())
    findings.extend(record.freshness_findings())
    findings.extend(record.capability_findings(claim.required_capabilities))
    return tuple(findings)


def provider_findings(evidence: CanaryEvidence) -> tuple[CanaryFinding, ...]:
    """Return every gap between the advertised runtimes and their records.

    Args:
        evidence: The committed export.

    Returns:
        The findings in advertised order; empty exactly when every
        advertised tuple carries a runner-written certification that
        earned itself and covers the capabilities the claim requires.
    """
    findings: list[CanaryFinding] = []
    for claim in evidence.advertised:
        findings.extend(_claim_findings(evidence, claim))
    logger.info(f"provider_findings advertised={len(evidence.advertised)} findings={len(findings)}")
    return tuple(findings)


def provider_evidence_refs(evidence: CanaryEvidence) -> tuple[str, ...]:
    """Return the URN of every certification the advertised set rests on.

    Args:
        evidence: The committed export.

    Returns:
        One URN per advertised tuple that carries a certification, in
        advertised order. A readiness row cites these, so an approver
        reads the record rather than the claim about it.
    """
    refs: list[str] = []
    for claim in evidence.advertised:
        record = evidence.certification_for(claim.manifest_ref)
        if record is not None:
            refs.append(record.certification_urn)
    return tuple(refs)


def membership_findings(
    evidence: CanaryEvidence,
    membership_refs: Sequence[str],
) -> tuple[CanaryFinding, ...]:
    """Return every gap between the declared bundles and the recorded Milestones.

    Args:
        evidence: The committed export.
        membership_refs: The acceptance-bundle references the checkpoint
            configuration declares.

    Returns:
        The findings in declaration order; empty exactly when every
        declared reference resolves to a COMPLETED Milestone recorded in
        a canary the export declares.
    """
    findings: list[CanaryFinding] = []
    for reference in membership_refs:
        record = evidence.milestone_for(reference)
        if record is None:
            findings.append(
                CanaryFinding(
                    subject=reference,
                    gap=CanaryEvidenceGap.MEMBERSHIP_UNRESOLVED,
                    detail=(
                        f"the checkpoint declares {reference!r} and no canary recorded a "
                        f"Milestone under it; accept the bundle or drop the reference"
                    ),
                )
            )
            continue
        if not evidence.declares_canary(record.project_code):
            findings.append(
                CanaryFinding(
                    subject=reference,
                    gap=CanaryEvidenceGap.UNDECLARED_CANARY,
                    detail=(
                        f"the Milestone is recorded in {record.project_code!r}, which the "
                        f"export declares no canary for; an acceptance taken outside a "
                        f"declared canary accepts nothing"
                    ),
                )
            )
        if record.status is not MilestoneStatus.COMPLETED:
            findings.append(
                CanaryFinding(
                    subject=reference,
                    gap=CanaryEvidenceGap.MILESTONE_INCOMPLETE,
                    detail=(
                        f"Milestone {record.milestone_id!r} is {record.status.value}, not "
                        f"{MilestoneStatus.COMPLETED.value}; a bundle is exact only once its "
                        f"Milestone finished"
                    ),
                )
            )
    logger.info(f"membership_findings declared={len(membership_refs)} findings={len(findings)}")
    return tuple(findings)


def membership_evidence_refs(
    evidence: CanaryEvidence,
    membership_refs: Sequence[str],
) -> tuple[str, ...]:
    """Return one evidence reference per declared bundle that resolved.

    Args:
        evidence: The committed export.
        membership_refs: The references the checkpoint declares.

    Returns:
        A ``milestone:<project_code>:<milestone_id>:<bundle_digest>``
        reference per resolved bundle, in declaration order. The digest
        is carried whole: an approver comparing a bundle against the one
        the canary sealed needs the digest, not a prefix of it.
    """
    refs: list[str] = []
    for reference in membership_refs:
        record = evidence.milestone_for(reference)
        if record is not None:
            refs.append(
                f"milestone:{record.project_code}:{record.milestone_id}:{record.bundle_digest}"
            )
    return tuple(refs)


def summarise_findings(findings: Sequence[CanaryFinding]) -> str:
    """Return a one-line operator-facing summary of *findings*.

    Args:
        findings: The gaps to summarise.

    Returns:
        A semicolon-joined ``<subject>: <gap> -- <detail>`` list; empty
        when there is nothing to report.
    """
    return "; ".join(
        f"{finding.subject or 'export'}: {finding.gap.value} -- {finding.detail}"
        for finding in findings
    )


__all__ = [
    "ABSENCE_GAPS",
    "CANARY_EVIDENCE_DIRS",
    "CANARY_EVIDENCE_FILENAME",
    "CERTIFYING_STAGES",
    "CanaryEvidence",
    "CanaryEvidenceGap",
    "CanaryFinding",
    "CanaryMilestoneRecord",
    "CertificationRecord",
    "ProductionRootIsolation",
    "RuntimeClaim",
    "RuntimeRunLog",
    "canary_evidence_path",
    "load_canary_evidence",
    "membership_evidence_refs",
    "membership_findings",
    "provider_evidence_refs",
    "provider_findings",
    "summarise_findings",
]
