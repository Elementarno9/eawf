"""The conformance runner: the sole writer of certification records.

One runtime tuple reaches a :class:`DriverCertification` only by walking
``probe`` then ``canary`` then ``certify``, in that order, with nothing
between. The runner appends one stage record per stage and builds the
certification from the records it appended, so a certification cannot
exist without the history that earned it.

The probe stage wraps the drift detector that already ships: a declared
capability is confronted with what the installed binary advertises, and
a capability whose verdict rests on no probe rule is uncovered rather
than passed. The canary stage compiles from the same
:class:`CompiledRunSpec` the tuple will serve and is rejected before
dispatch when its grants, denials, sandbox or environment differ, since
a canary run under other authority certifies another contract. The
certify stage refuses while a canary for the tuple is in flight and
refuses when any evidence it would cite has expired.

Refusals are returned as typed stage results rather than logged: the
caller gets the reason code, and the same reason is what the operator
surface renders.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, Protocol, Self

from pydantic import AfterValidator, Field, StrictBool, StringConstraints, model_validator

from eawf.kernel.runtime.certification import (
    CapabilityCertification,
    CertificationFailureCode,
    CertificationId,
    CertifiedRuntimeFacts,
    ConformanceStage,
    ConformanceStageRecord,
    DriverCertification,
    InstallTrust,
    QuarantineTrigger,
    StageOutcome,
)
from eawf.kernel.runtime.compiled import CompiledRunSpec, canonical_digest
from eawf.kernel.runtime.provider import (
    AgentProviderProfile,
    ArtifactUrn,
    AuthKind,
    BoundedIdentifier,
    CapabilityId,
    Digest,
    DriverManifestUrn,
    ProviderProfileId,
    RoutePolicy,
    RuntimeRecord,
    SemVer,
    reject_repeats,
)
from eawf.kernel.state.types import UtcDatetime
from eawf.runtime.runtimes.capabilities import CapabilityMatrix, ProbeResult, detect_drift
from eawf.runtime.runtimes.containment import (
    ContainmentCallOutcome,
    ContainmentProbeResult,
    run_containment_probes,
)
from eawf.runtime.runtimes.quarantine import (
    DependentDisablement,
    LastKnownGoodPin,
    MemoryPinLedger,
    PinLedger,
    RollbackHorizon,
    RollbackSelection,
    disable_dependents,
    failure_code_of,
    is_quarantined,
    reissue_profile,
    select_last_known_good,
)

logger = logging.getLogger(__name__)

#: One advertised CLI token the live probe saw. Bounded and strict so a
#: probe report cannot smuggle a payload through the flag list.
ObservedFlag = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]

#: A capability row name of the cross-runtime matrix.
MatrixCapability = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9_]{0,63}$")]

#: The stages a certification's history is built from, in order.
CERTIFIED_STAGES: Final[tuple[ConformanceStage, ...]] = ("probe", "canary")


class ConformanceSequenceError(ValueError):
    """Raised when a stage is run out of the closed stage sequence.

    A canary without a passed probe, or a certify without a passed
    canary, is a caller walking the machine wrongly rather than a verdict
    about the tuple, so it raises instead of producing a stage record.
    """


class ParityAxis(StrEnum):
    """An axis on which a canary spec must match the spec it stands for."""

    TOOL_GRANTS = "tool_grants"
    TOOL_DENIALS = "tool_denials"
    SANDBOX_POLICY = "sandbox_policy"
    ENVIRONMENT_DIGEST = "environment_digest"


class RuntimeTuple(RuntimeRecord):
    """The runtime tuple one certification covers.

    Any change to any member is a different tuple and therefore a new
    certification, never an edit of an existing one, which is why the
    record is frozen and addressed by its digest.
    """

    manifest_ref: DriverManifestUrn
    manifest_digest: Digest
    distribution_version: SemVer
    sdk_or_server_version: SemVer
    auth_kind: AuthKind
    model_family: BoundedIdentifier
    os_class: Literal["linux", "macos", "windows"]
    architecture: Literal["x86_64", "aarch64"]
    managed_profile_digest: Digest
    conformance_suite_version: SemVer

    @property
    def tuple_digest(self) -> str:
        """Return the digest that addresses this tuple in the journal."""
        return canonical_digest(self.model_dump(mode="json"))


class ProtocolFacts(RuntimeRecord):
    """The three protocol versions a certification records."""

    worker_protocol_version: SemVer
    semantic_protocol_version: SemVer
    event_codec_version: SemVer


class ProbeRequest(RuntimeRecord):
    """What the probe stage is asked to confront the declaration with."""

    runtime_tuple: RuntimeTuple
    runtime_id: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
    installed: StrictBool
    observed_flags: Annotated[tuple[ObservedFlag, ...], AfterValidator(reject_repeats)] = ()
    required_capabilities: Annotated[
        tuple[MatrixCapability, ...], Field(min_length=1), AfterValidator(reject_repeats)
    ]
    evidence_ref: ArtifactUrn


class CanaryRequest(RuntimeRecord):
    """What the canary stage compiles from and what it reported."""

    runtime_tuple: RuntimeTuple
    served_spec: CompiledRunSpec
    canary_spec: CompiledRunSpec
    outcomes: tuple[ContainmentCallOutcome, ...] = ()
    evidence_ref: ArtifactUrn

    @model_validator(mode="after")
    def _one_outcome_per_attempt(self) -> Self:
        """Refuse two reports of the same attempted call.

        Raises:
            ValueError: An attempt is reported more than once, where
                keeping one silently would drop a reported escape.
        """
        reject_repeats(tuple(row.attempt for row in self.outcomes))
        return self


class CertificationRequest(RuntimeRecord):
    """What the certify stage would record, if the history admits it.

    ``profile_ids`` names the profiles this certification becomes the
    last-known-good of. A certification nobody runs on pins nothing, so
    the field is empty by default rather than defaulting to every profile.
    """

    runtime_tuple: RuntimeTuple
    protocol: ProtocolFacts
    certification_id: CertificationId
    capabilities: Annotated[tuple[CapabilityCertification, ...], Field(min_length=1)]
    runtime_facts: CertifiedRuntimeFacts
    install_trust: InstallTrust
    evidence_bundle_ref: ArtifactUrn
    expires_at: UtcDatetime
    quarantine_trigger: QuarantineTrigger | None = None
    profile_ids: Annotated[tuple[ProviderProfileId, ...], AfterValidator(reject_repeats)] = ()


class QuarantineRequest(RuntimeRecord):
    """What one fired trigger asks the runner to take out of service."""

    runtime_tuple: RuntimeTuple
    trigger: QuarantineTrigger
    certification: DriverCertification
    profiles: tuple[AgentProviderProfile, ...] = ()
    routes: tuple[RoutePolicy, ...] = ()
    evidence_ref: ArtifactUrn

    @model_validator(mode="after")
    def _certification_covers_the_tuple(self) -> Self:
        """Refuse a certification taken of some other tuple.

        Raises:
            ValueError: The certification names another driver manifest,
                where quarantining it would take the wrong tuple out of
                service and leave the tripped one running.
        """
        certification = self.certification
        runtime_tuple = self.runtime_tuple
        if certification.manifest_ref != runtime_tuple.manifest_ref:
            raise ValueError(
                f"certification covers {certification.manifest_ref!r}, "
                f"not {runtime_tuple.manifest_ref!r}"
            )
        if certification.manifest_digest != runtime_tuple.manifest_digest:
            raise ValueError("certification manifest_digest differs from the tuple's")
        return self


class RollbackRequest(RuntimeRecord):
    """Which profile to walk back to its last-known-good certification."""

    profile: AgentProviderProfile
    evidence_ref: ArtifactUrn


class ProbeStageResult(RuntimeRecord):
    """The probe stage's record and the capabilities that produced it."""

    record: ConformanceStageRecord
    drifted: tuple[MatrixCapability, ...] = ()
    uncovered: tuple[MatrixCapability, ...] = ()

    @property
    def passed(self) -> bool:
        """Whether the probe stage passed."""
        return self.record.outcome == "passed"


class CanaryStageResult(RuntimeRecord):
    """The canary stage's record, its parity verdict and its containment."""

    record: ConformanceStageRecord
    parity_mismatches: tuple[ParityAxis, ...] = ()
    containment: ContainmentProbeResult | None = None
    quarantine_trigger: QuarantineTrigger | None = None

    @property
    def passed(self) -> bool:
        """Whether the canary stage passed."""
        return self.record.outcome == "passed"


class CertifyStageResult(RuntimeRecord):
    """The certify stage's record and the certification it wrote, if any."""

    record: ConformanceStageRecord
    certification: DriverCertification | None = None
    expired: tuple[CapabilityId, ...] = ()

    @model_validator(mode="after")
    def _certification_follows_the_outcome(self) -> Self:
        """Bind the written record to the stage outcome.

        Raises:
            ValueError: A passed certify wrote no certification, or a
                refused one wrote a certification anyway.
        """
        if (self.record.outcome == "passed") != (self.certification is not None):
            raise ValueError("a certification is written by a passed certify stage and only then")
        return self


class QuarantineStageResult(RuntimeRecord):
    """The quarantine's stage record, its certification and its fallout."""

    record: ConformanceStageRecord
    certification: DriverCertification
    disabled: DependentDisablement

    @model_validator(mode="after")
    def _certification_is_quarantined(self) -> Self:
        """Require the returned certification to be the quarantined one.

        Raises:
            ValueError: The certification still admits dispatch, which
                would report a quarantine that did not happen.
        """
        if self.certification.install_trust != "quarantined":
            raise ValueError("a quarantine returns a certification of quarantined install trust")
        return self


class RollbackStageResult(RuntimeRecord):
    """Which pin the rollback bound, and the profile it hands back.

    A refused rollback names no tuple, so it appends no stage record: the
    refusal is a fact about the profile rather than about any one tuple.
    """

    selection: RollbackSelection
    record: ConformanceStageRecord | None = None
    profile: AgentProviderProfile | None = None
    applies_to: RollbackHorizon = "next_compiled_run"

    @model_validator(mode="after")
    def _effect_follows_the_selection(self) -> Self:
        """Bind the record and the re-enabled profile to the selection.

        Raises:
            ValueError: A selected pin left no stage record or no
                profile, or a refusal produced either.
        """
        selected = self.selection.pin is not None
        if selected != (self.record is not None) or selected != (self.profile is not None):
            raise ValueError("a rollback records a stage and a profile exactly when it selects one")
        return self


class StageJournal(Protocol):
    """The append-only store the runner records its stages in."""

    def append(self, *, tuple_digest: str, record: ConformanceStageRecord) -> None:
        """Append one stage record for one tuple."""

    def records(self, *, tuple_digest: str) -> tuple[ConformanceStageRecord, ...]:
        """Return every stage record of one tuple, in append order."""


def canary_parity_mismatches(
    *, served: CompiledRunSpec, canary: CompiledRunSpec
) -> tuple[ParityAxis, ...]:
    """Return the axes on which *canary* differs from *served*.

    The four axes are the ones that decide what a Run may reach: what it
    may call, what it may never call, the isolation it runs inside, and
    the environment it sees. A canary that differs on any of them
    exercised a different contract from the one the tuple will serve, so
    its evidence does not transfer.

    Args:
        served: The compiled spec the certified tuple will serve.
        canary: The compiled spec the canary would be dispatched with.

    Returns:
        The differing axes in :class:`ParityAxis` order; empty when the
        two specs agree on all four.
    """
    mismatches: list[ParityAxis] = []
    if served.tool_policy.allow != canary.tool_policy.allow:
        mismatches.append(ParityAxis.TOOL_GRANTS)
    if served.tool_policy.deny != canary.tool_policy.deny:
        mismatches.append(ParityAxis.TOOL_DENIALS)
    if served.sandbox != canary.sandbox:
        mismatches.append(ParityAxis.SANDBOX_POLICY)
    served_environment = canonical_digest(served.environment.model_dump(mode="json"))
    canary_environment = canonical_digest(canary.environment.model_dump(mode="json"))
    if served_environment != canary_environment:
        mismatches.append(ParityAxis.ENVIRONMENT_DIGEST)
    return tuple(mismatches)


def certifiable_stages(
    records: Sequence[ConformanceStageRecord],
) -> tuple[ConformanceStageRecord, ...] | None:
    """Return the passed probe and canary a certification may cite.

    Refused stages are skipped: a refusal means nothing ran, so a canary
    refused for parity does not break the run a later canary completes.
    A failed stage does break it, because the tuple was exercised and
    found wanting. What remains must be exactly one passed probe followed
    by one passed canary, since a canary is evidence only of the probe it
    followed and a re-run starts from probe.

    Args:
        records: Every stage record of one tuple, in append order.

    Returns:
        The ``(probe, canary)`` pair, or ``None`` when the history does
        not end in exactly that contiguous pair.
    """
    effective = [row for row in records if row.outcome != "refused"]
    probes = [index for index, row in enumerate(effective) if row.stage == "probe"]
    if not probes:
        return None
    tail = tuple(effective[probes[-1] :])
    if tuple(row.stage for row in tail) != CERTIFIED_STAGES:
        return None
    if any(row.outcome != "passed" for row in tail):
        return None
    return tail


def _utcnow() -> datetime:
    """Return the current instant in UTC."""
    return datetime.now(UTC)


class ConformanceRunner:
    """The daemon's one walker of the probe, canary and certify stages.

    Attributes:
        journal: The append-only store every stage record lands in.
    """

    def __init__(
        self,
        *,
        journal: StageJournal,
        now: Callable[[], datetime] = _utcnow,
        matrix: CapabilityMatrix | None = None,
        pins: PinLedger | None = None,
    ) -> None:
        """Bind the runner to its journal, its clock, its matrix and its pins.

        Args:
            journal: The append-only store of stage records.
            now: Source of the stage timestamps.
            matrix: Capability matrix the probe stage confronts; the
                packaged matrix when absent.
            pins: Ledger of per-profile last-known-good certifications; a
                process-lived ledger when absent, which keeps the pins
                beside the canary leases this runner already holds.
        """
        self.journal = journal
        self._now = now
        self._matrix = matrix
        self._pins: PinLedger = pins if pins is not None else MemoryPinLedger()
        self._canaries_in_flight: set[str] = set()

    def stage_history(self, runtime_tuple: RuntimeTuple) -> tuple[ConformanceStageRecord, ...]:
        """Return every stage recorded for *runtime_tuple*, in append order.

        Args:
            runtime_tuple: The tuple whose history is read.

        Returns:
            The stage records, oldest first.
        """
        return self.journal.records(tuple_digest=runtime_tuple.tuple_digest)

    def canary_in_flight(self, runtime_tuple: RuntimeTuple) -> bool:
        """Report whether a canary for *runtime_tuple* is running now.

        Args:
            runtime_tuple: The tuple asked about.

        Returns:
            ``True`` while a lease on the tuple is held.
        """
        return runtime_tuple.tuple_digest in self._canaries_in_flight

    @contextmanager
    def canary_lease(self, runtime_tuple: RuntimeTuple) -> Iterator[None]:
        """Hold the one canary lease of *runtime_tuple* for the block.

        Args:
            runtime_tuple: The tuple the canary runs against.

        Yields:
            Nothing; the lease is the effect.

        Raises:
            ConformanceSequenceError: A canary for the tuple is already
                in flight, and two canaries would certify each other's
                evidence.
        """
        digest = runtime_tuple.tuple_digest
        if digest in self._canaries_in_flight:
            raise ConformanceSequenceError(f"a canary for tuple {digest} is already in flight")
        self._canaries_in_flight.add(digest)
        try:
            yield
        finally:
            self._canaries_in_flight.discard(digest)

    def run_probe(self, request: ProbeRequest) -> ProbeStageResult:
        """Confront the declared capabilities with what the binary advertises.

        Args:
            request: The tuple, the live probe facts and the capabilities
                the certification would cover.

        Returns:
            The appended stage record, with the drifted and uncovered
            capabilities that produced it.

        Raises:
            ValueError: The runtime id is unknown, or a required
                capability is not a row of the matrix, so no verdict
                about it could exist.
        """
        started_at = self._now()
        rows = {
            row.capability: row
            for row in detect_drift(
                request.runtime_id,
                ProbeResult(
                    runtime_id=request.runtime_id,
                    installed=request.installed,
                    observed_flags=request.observed_flags,
                ),
                matrix=self._matrix,
            )
        }
        missing = tuple(name for name in request.required_capabilities if name not in rows)
        if missing:
            raise ValueError(f"capability matrix has no row for {list(missing)!r}")
        required = tuple(rows[name] for name in request.required_capabilities)
        drifted = tuple(row.capability for row in required if row.status == "DRIFT")
        uncovered = tuple(row.capability for row in required if row.status == "UNKNOWN")
        reason = _probe_reason(installed=request.installed, drifted=drifted, uncovered=uncovered)
        record = self._append(
            tuple_digest=request.runtime_tuple.tuple_digest,
            stage="probe",
            outcome="passed" if reason is None else "failed",
            reason_code=reason,
            evidence_ref=request.evidence_ref,
            started_at=started_at,
        )
        return ProbeStageResult(record=record, drifted=drifted, uncovered=uncovered)

    def run_canary(self, request: CanaryRequest) -> CanaryStageResult:
        """Run the containment probe set under the spec the tuple will serve.

        Args:
            request: The tuple, the served and canary specs, and what the
                canary Run reported for each attempted call.

        Returns:
            The appended stage record with the parity verdict and, when
            the canary was dispatched, its graded containment.

        Raises:
            ValueError: The canary would run as the Run the tuple serves,
                which is the one Run the probe set may never touch.
            ConformanceSequenceError: No passed probe precedes this
                canary, or a canary for the tuple is already in flight.
        """
        if request.canary_spec.run_ref == request.served_spec.run_ref:
            raise ValueError(
                "the canary must be its own Run; the probe set never runs against a served Run"
            )
        self._require_passed_probe(request.runtime_tuple)
        started_at = self._now()
        mismatches = canary_parity_mismatches(
            served=request.served_spec, canary=request.canary_spec
        )
        if mismatches:
            return self._refused_canary(request, mismatches=mismatches, started_at=started_at)
        with self.canary_lease(request.runtime_tuple):
            containment = run_containment_probes(
                outcomes={row.attempt: row for row in request.outcomes},
                evidence_ref=request.evidence_ref,
                observed_at=started_at,
            )
        record = self._append(
            tuple_digest=request.runtime_tuple.tuple_digest,
            stage="canary",
            outcome="passed" if containment.passed else "failed",
            reason_code=containment.failure_code,
            evidence_ref=request.evidence_ref,
            started_at=started_at,
        )
        return CanaryStageResult(
            record=record,
            containment=containment,
            quarantine_trigger=None if containment.passed else QuarantineTrigger.CANARY_FAILURE,
        )

    def certify(self, request: CertificationRequest) -> CertifyStageResult:
        """Write the certification the passed probe and canary earned.

        Args:
            request: The tuple, the observed capabilities and the facts
                the record would carry.

        Returns:
            The appended stage record and the certification, or a refusal
            naming the reason code and no certification.

        Raises:
            ConformanceSequenceError: The tuple's history does not end in
                a passed probe followed by a passed canary.
        """
        started_at = self._now()
        history = certifiable_stages(self.stage_history(request.runtime_tuple))
        if history is None:
            raise ConformanceSequenceError(
                "certify requires a passed probe followed by a passed canary"
            )
        if self.canary_in_flight(request.runtime_tuple):
            return self._refused_certify(
                request,
                reason=CertificationFailureCode.CANARY_IN_PROGRESS,
                started_at=started_at,
            )
        expired = tuple(
            row.capability_id for row in request.capabilities if row.expires_at <= started_at
        )
        if expired:
            return self._refused_certify(
                request,
                reason=CertificationFailureCode.EVIDENCE_EXPIRED,
                started_at=started_at,
                expired=expired,
            )
        record = self._append(
            tuple_digest=request.runtime_tuple.tuple_digest,
            stage="certify",
            outcome="passed",
            reason_code=None,
            evidence_ref=request.evidence_bundle_ref,
            started_at=started_at,
        )
        certification = _build_certification(
            request, stage_history=(*history, record), verified_at=started_at
        )
        self._pin_last_known_good(request, certification=certification, pinned_at=started_at)
        logger.info(
            f"certify certification_id={request.certification_id!r} "
            f"tuple={request.runtime_tuple.tuple_digest!r} "
            f"capabilities={len(request.capabilities)}"
        )
        return CertifyStageResult(record=record, certification=certification)

    def quarantine(self, request: QuarantineRequest) -> QuarantineStageResult:
        """Take a tripped tuple and everything that needs it out of service.

        The stage record and the quarantined certification are produced in
        this one call, so there is no window in which the trigger has
        fired and the tuple is still dispatchable.

        Args:
            request: The tuple, the trigger that fired, the certification
                in force, and the declared profiles and routes to search
                for dependents.

        Returns:
            The appended stage record, the quarantined certification, and
            disabled copies of every dependent profile and route.
        """
        started_at = self._now()
        record = self._append(
            tuple_digest=request.runtime_tuple.tuple_digest,
            stage="rollback",
            outcome="failed",
            reason_code=failure_code_of(request.trigger),
            evidence_ref=request.evidence_ref,
            started_at=started_at,
        )
        certification = _quarantined_certification(
            request.certification, trigger=request.trigger, record=record, revoked_at=started_at
        )
        disabled = disable_dependents(
            manifest_ref=request.runtime_tuple.manifest_ref,
            profiles=request.profiles,
            routes=request.routes,
        )
        logger.info(
            f"quarantine tuple={request.runtime_tuple.tuple_digest!r} "
            f"trigger={request.trigger.value!r} profiles={len(disabled.profiles)} "
            f"routes={len(disabled.routes)}"
        )
        return QuarantineStageResult(record=record, certification=certification, disabled=disabled)

    def roll_back(self, request: RollbackRequest) -> RollbackStageResult:
        """Walk one profile back to its newest admissible pin.

        What the rollback changes is the next compiled Run: the re-enabled
        profile and the pinned certification are what the compiler reads,
        and a Run already sealed keeps the authority it was compiled
        under.

        Args:
            request: The profile to roll back and the artifact its
                rollback is filed under.

        Returns:
            The selected pin with the re-enabled profile and the appended
            stage record, or a refusal that leaves the profile as it was.
        """
        started_at = self._now()
        pins = self._pins.pins(profile_id=request.profile.profile_id)
        quarantined = tuple(
            pin.tuple_digest
            for pin in pins
            if is_quarantined(self.journal.records(tuple_digest=pin.tuple_digest))
        )
        selection = select_last_known_good(pins=pins, quarantined=quarantined, now=started_at)
        if selection.pin is None:
            logger.info(
                f"roll_back refused profile={request.profile.profile_id!r} "
                f"reason={selection.refusal!r}"
            )
            return RollbackStageResult(selection=selection)
        record = self._append(
            tuple_digest=selection.pin.tuple_digest,
            stage="rollback",
            outcome="passed",
            reason_code=None,
            evidence_ref=request.evidence_ref,
            started_at=started_at,
        )
        logger.info(
            f"roll_back profile={request.profile.profile_id!r} tuple={selection.pin.tuple_digest!r}"
        )
        return RollbackStageResult(
            selection=selection,
            record=record,
            profile=reissue_profile(request.profile, disabled=False),
        )

    def _pin_last_known_good(
        self,
        request: CertificationRequest,
        *,
        certification: DriverCertification,
        pinned_at: datetime,
    ) -> None:
        """Record *certification* as the last-known-good of each named profile."""
        for profile_id in request.profile_ids:
            self._pins.pin(
                pin=LastKnownGoodPin(
                    profile_id=profile_id,
                    tuple_digest=request.runtime_tuple.tuple_digest,
                    certification=certification,
                    pinned_at=pinned_at,
                )
            )

    def _require_passed_probe(self, runtime_tuple: RuntimeTuple) -> None:
        """Refuse a canary the probe stage never cleared.

        Args:
            runtime_tuple: The tuple the canary would run against.

        Raises:
            ConformanceSequenceError: The tuple's effective history does
                not end in a passed probe.
        """
        effective = [row for row in self.stage_history(runtime_tuple) if row.outcome != "refused"]
        if not effective or effective[-1].stage != "probe" or effective[-1].outcome != "passed":
            raise ConformanceSequenceError("a canary starts only from a passed probe")

    def _refused_canary(
        self,
        request: CanaryRequest,
        *,
        mismatches: tuple[ParityAxis, ...],
        started_at: datetime,
    ) -> CanaryStageResult:
        """Record a canary refused before dispatch and return the refusal."""
        named = ", ".join(axis.value for axis in mismatches)
        logger.info(f"run_canary refused parity mismatches={named}")
        record = self._append(
            tuple_digest=request.runtime_tuple.tuple_digest,
            stage="canary",
            outcome="refused",
            reason_code=CertificationFailureCode.SCHEMA_MISMATCH,
            evidence_ref=request.evidence_ref,
            started_at=started_at,
        )
        return CanaryStageResult(record=record, parity_mismatches=mismatches, containment=None)

    def _refused_certify(
        self,
        request: CertificationRequest,
        *,
        reason: CertificationFailureCode,
        started_at: datetime,
        expired: tuple[CapabilityId, ...] = (),
    ) -> CertifyStageResult:
        """Record a refused certify stage and return the refusal."""
        logger.info(
            f"certify refused tuple={request.runtime_tuple.tuple_digest!r} reason={reason.value!r}"
        )
        record = self._append(
            tuple_digest=request.runtime_tuple.tuple_digest,
            stage="certify",
            outcome="refused",
            reason_code=reason,
            evidence_ref=request.evidence_bundle_ref,
            started_at=started_at,
        )
        return CertifyStageResult(record=record, certification=None, expired=expired)

    def _append(
        self,
        *,
        tuple_digest: str,
        stage: ConformanceStage,
        outcome: StageOutcome,
        reason_code: CertificationFailureCode | None,
        evidence_ref: ArtifactUrn,
        started_at: datetime,
    ) -> ConformanceStageRecord:
        """Append one stage record and return it."""
        record = ConformanceStageRecord(
            stage=stage,
            outcome=outcome,
            reason_code=reason_code,
            evidence_ref=evidence_ref,
            started_at=started_at,
            completed_at=self._now(),
        )
        self.journal.append(tuple_digest=tuple_digest, record=record)
        return record


def _probe_reason(
    *,
    installed: bool,
    drifted: tuple[str, ...],
    uncovered: tuple[str, ...],
) -> CertificationFailureCode | None:
    """Return why the probe stage failed, or ``None`` when it passed.

    An absent binary is graded first: every other verdict about it would
    be a verdict about nothing.
    """
    if not installed:
        return CertificationFailureCode.TUPLE_NOT_INSTALLABLE
    if drifted:
        return CertificationFailureCode.CAPABILITY_NOT_OBSERVED
    if uncovered:
        return CertificationFailureCode.CAPABILITY_EVIDENCE_UNCOVERED
    return None


def _build_certification(
    request: CertificationRequest,
    *,
    stage_history: tuple[ConformanceStageRecord, ...],
    verified_at: datetime,
) -> DriverCertification:
    """Return the certification *request* earned, over *stage_history*."""
    runtime_tuple = request.runtime_tuple
    return DriverCertification(
        schema_version="driver-certification/v1",
        certification_id=request.certification_id,
        manifest_ref=runtime_tuple.manifest_ref,
        manifest_digest=runtime_tuple.manifest_digest,
        distribution_version=runtime_tuple.distribution_version,
        sdk_or_server_version=runtime_tuple.sdk_or_server_version,
        auth_kind=runtime_tuple.auth_kind,
        model_family=runtime_tuple.model_family,
        os_class=runtime_tuple.os_class,
        architecture=runtime_tuple.architecture,
        managed_profile_digest=runtime_tuple.managed_profile_digest,
        worker_protocol_version=request.protocol.worker_protocol_version,
        semantic_protocol_version=request.protocol.semantic_protocol_version,
        event_codec_version=request.protocol.event_codec_version,
        conformance_suite_version=runtime_tuple.conformance_suite_version,
        capabilities=request.capabilities,
        overall_status="verified",
        install_trust=request.install_trust,
        runtime_facts=request.runtime_facts,
        stage_history=stage_history,
        evidence_bundle_ref=request.evidence_bundle_ref,
        verified_at=verified_at,
        expires_at=request.expires_at,
        quarantine_trigger=request.quarantine_trigger,
    )


def _quarantined_certification(
    certification: DriverCertification,
    *,
    trigger: QuarantineTrigger,
    record: ConformanceStageRecord,
    revoked_at: datetime,
) -> DriverCertification:
    """Return *certification* rewritten as the quarantined record.

    The record is rebuilt through validation rather than copied field by
    field, so the quarantine invariants -- a trigger and quarantined trust
    appear together, the stage history stays in stage order -- are proved
    on the result instead of assumed.
    """
    document = certification.model_dump(mode="json")
    document.update(
        overall_status="revoked",
        install_trust="quarantined",
        quarantine_trigger=trigger.value,
        revoked_at=revoked_at,
        revocation_reason=f"quarantined on {trigger.value}",
        stage_history=[*document["stage_history"], record.model_dump(mode="json")],
    )
    return DriverCertification.model_validate(document)


__all__ = [
    "CERTIFIED_STAGES",
    "CanaryRequest",
    "CanaryStageResult",
    "CertificationRequest",
    "CertifyStageResult",
    "ConformanceRunner",
    "ConformanceSequenceError",
    "ParityAxis",
    "ProbeRequest",
    "ProbeStageResult",
    "ProtocolFacts",
    "QuarantineRequest",
    "QuarantineStageResult",
    "RollbackRequest",
    "RollbackStageResult",
    "RuntimeTuple",
    "StageJournal",
    "canary_parity_mismatches",
    "certifiable_stages",
]
