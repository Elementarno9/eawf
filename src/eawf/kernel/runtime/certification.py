"""Certification records: what one runtime tuple was certified to do.

The conformance runner is the only writer of these records, and it writes
them through the closed stage sequence :data:`STAGE_ORDER`. Each record
describes exactly one runtime tuple, so any change to the tuple is a new
record rather than an edit of an existing one, and the records are frozen
to keep that true.

Two axes stay separate on :class:`DriverCertification` and neither
substitutes for the other. ``overall_status`` describes the freshness of
the evidence; ``install_trust`` describes the installation the evidence
was taken from. A managed installation whose certification expired and a
fresh certification of a quarantined installation are both refused for
unattended dispatch, but they are refused on different axes, and
:meth:`DriverCertification.decide_unattended_dispatch` names which one.

:class:`CertifiedRuntimeFacts` records measured properties of the
certified runtime, never of the machine that ran the probe. A fact the
runtime does not report is recorded absent rather than defaulted, and a
locally configured cap is a diagnostic fact that can lower the value the
daemon reads but never raise it: a limit that holds on one machine is not
a limit anything else may depend on.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from eawf.kernel.runtime.compiled import BoundedText
from eawf.kernel.runtime.provider import (
    ArtifactUrn,
    AuthKind,
    BoundedIdentifier,
    CapabilityId,
    ConfigUrn,
    Digest,
    DriverManifestUrn,
    JsonScalar,
    ProviderRecordId,
    RuntimeRecord,
    SemVer,
    WorkflowUrn,
    reject_repeats,
)
from eawf.kernel.state.types import UtcDatetime

#: Certification identifiers share the provider-record grammar.
CertificationId = ProviderRecordId

#: A measured cap. Caps are counts, so zero is not a cap but an absent
#: fact, which :class:`CertifiedRuntimeFacts` records as ``None``.
MeasuredCap = Annotated[StrictInt, Field(ge=1)]

#: Trust in the installation, independent of evidence freshness.
InstallTrust = Literal["managed", "observed", "quarantined", "unsupported"]

#: Freshness of the evidence a certification rests on.
CertificationStatus = Literal["verified", "failed", "revoked", "expired"]

#: Status of one certified capability.
CapabilityCertificationStatus = Literal["verified", "degraded", "unsupported", "failed"]

#: Whether a capability is provided by the runtime itself or emulated on
#: top of it. An emulated capability is degraded by its emulation, so the
#: two are never rendered alike.
CapabilityBasis = Literal["native", "emulated"]

#: One stage of the conformance run.
ConformanceStage = Literal["probe", "canary", "certify", "rollback"]

#: What a stage concluded.
StageOutcome = Literal["passed", "failed", "refused"]

#: Where a measured fact came from. ``runtime_reported`` is the runtime
#: answering for itself; ``observed`` is the runner measuring it.
MeasurementMethod = Literal["runtime_reported", "observed"]

#: The stages in the order the runner appends them.
STAGE_ORDER: Final[tuple[ConformanceStage, ...]] = ("probe", "canary", "certify", "rollback")

#: The caps a certification measures, addressable by name.
CertifiedFactName = Literal[
    "context_window_tokens",
    "auto_compaction_threshold_tokens",
    "project_document_cap_bytes",
    "tool_output_cap_tokens",
]

#: The axis a refusal of unattended dispatch rests on.
DenialAxis = Literal["install_trust", "overall_status", "capability"]

_MAX_REASON_CHARS: Final = 500


class CertificationFailureCode(StrEnum):
    """Why a stage failed or refused, or why a capability is not certified.

    Every member names a refusal one of the four stages can reach, so a
    failed record always says which check produced it rather than
    carrying free prose.
    """

    PROTOCOL_VERSION_MISMATCH = "protocol_version_mismatch"
    SCHEMA_MISMATCH = "schema_mismatch"
    EVENT_CODEC_MISMATCH = "event_codec_mismatch"
    AUTH_KIND_MISMATCH = "auth_kind_mismatch"
    CAPABILITY_NOT_OBSERVED = "capability_not_observed"
    CAPABILITY_EVIDENCE_UNCOVERED = "capability_evidence_uncovered"
    CONTAINMENT_PROBE_ESCAPE = "containment_probe_escape"
    SECRET_MATERIAL_OBSERVABLE = "secret_material_observable"  # pragma: allowlist secret
    EVIDENCE_EXPIRED = "evidence_expired"
    CANARY_IN_PROGRESS = "canary_in_progress"
    NO_LAST_KNOWN_GOOD_PIN = "no_last_known_good_pin"
    TUPLE_NOT_INSTALLABLE = "tuple_not_installable"


class QuarantineTrigger(StrEnum):
    """What put a runtime tuple into quarantine.

    The set is closed because quarantine is automatic and daemon-authored:
    a trigger nobody enumerated could not have been detected.
    """

    PROTOCOL_DRIFT = "protocol_drift"
    SCHEMA_DRIFT = "schema_drift"
    WRONG_AUTH = "wrong_auth"
    AMBIENT_SOURCE_LEAKAGE = "ambient_source_leakage"
    DENIED_TOOL_ESCAPE = "denied_tool_escape"
    TERMINAL_OUTCOME_ANOMALY = "terminal_outcome_anomaly"
    CORRUPT_RESUME = "corrupt_resume"
    UNEXPLAINED_USAGE = "unexplained_usage"
    CANARY_FAILURE = "canary_failure"


class ConformanceStageRecord(RuntimeRecord):
    """One stage of one conformance run, appended and never rewritten."""

    stage: ConformanceStage
    outcome: StageOutcome
    reason_code: CertificationFailureCode | None = None
    evidence_ref: ArtifactUrn
    started_at: UtcDatetime
    completed_at: UtcDatetime

    @model_validator(mode="after")
    def _outcome_carries_its_reason(self) -> Self:
        """Bind the reason code and the interval to the outcome.

        Raises:
            ValueError: A passed stage carries a reason code, a failed or
                refused stage carries none, or the stage completed before
                it started.
        """
        if self.outcome == "passed" and self.reason_code is not None:
            raise ValueError("a passed stage carries no reason_code")
        if self.outcome != "passed" and self.reason_code is None:
            raise ValueError(f"a {self.outcome} stage requires a reason_code")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at precedes started_at")
        return self


class LocalFactOverride(RuntimeRecord):
    """One locally configured cap, recorded as a diagnostic fact."""

    fact: CertifiedFactName
    configured_value: MeasuredCap
    source_ref: ConfigUrn


class CertifiedRuntimeFacts(RuntimeRecord):
    """Caps measured off the certified runtime, never off the machine.

    Every field is required, including the nullable one: a fact the
    runtime does not report is written as ``None`` deliberately rather
    than picked up from a default nobody measured.
    """

    context_window_tokens: MeasuredCap
    auto_compaction_threshold_tokens: MeasuredCap | None
    project_document_cap_bytes: MeasuredCap
    tool_output_cap_tokens: MeasuredCap
    measured_at: UtcDatetime
    measurement_method: MeasurementMethod

    @model_validator(mode="after")
    def _compaction_fits_the_window(self) -> Self:
        """Require the compaction threshold to sit inside the window.

        Raises:
            ValueError: The threshold exceeds the context window, which
                would describe a compaction that can never trigger.
        """
        threshold = self.auto_compaction_threshold_tokens
        if threshold is not None and threshold > self.context_window_tokens:
            raise ValueError("auto_compaction_threshold_tokens exceeds context_window_tokens")
        return self

    def certified_cap(self, fact: CertifiedFactName) -> int | None:
        """Return the measured value of *fact*.

        Args:
            fact: Name of the measured cap.

        Returns:
            The measured value, or ``None`` when the runtime did not
            report that fact.

        Raises:
            KeyError: *fact* is not one of the measured caps.
        """
        caps: dict[str, int | None] = {
            "context_window_tokens": self.context_window_tokens,
            "auto_compaction_threshold_tokens": self.auto_compaction_threshold_tokens,
            "project_document_cap_bytes": self.project_document_cap_bytes,
            "tool_output_cap_tokens": self.tool_output_cap_tokens,
        }
        return caps[fact]

    def effective_cap(self, override: LocalFactOverride) -> int | None:
        """Return the cap to read once *override* has been considered.

        A locally configured value may only lower the certified one. A
        value that would raise it describes one machine rather than the
        certified runtime, so it is recorded as a diagnostic fact and
        otherwise ignored. An override of a fact the runtime never
        reported leaves it absent: an unmeasured cap is not a cap.

        Args:
            override: The locally configured cap.

        Returns:
            The effective cap, or ``None`` when the fact is absent from
            the certification.

        Raises:
            KeyError: The override names a fact that is not a measured
                cap.
        """
        certified = self.certified_cap(override.fact)
        if certified is None:
            return None
        return min(certified, override.configured_value)


class CapabilityCertification(RuntimeRecord):
    """One capability as the conformance runner observed it."""

    capability_id: CapabilityId
    level: JsonScalar
    status: CapabilityCertificationStatus
    basis: CapabilityBasis
    evidence_ref: ArtifactUrn
    verified_at: UtcDatetime
    expires_at: UtcDatetime
    degradation_workflow_ref: WorkflowUrn | None = None
    failure_code: CertificationFailureCode | None = None
    failure_detail_ref: ArtifactUrn | None = None

    @model_validator(mode="after")
    def _status_carries_its_facts(self) -> Self:
        """Bind the degradation workflow and the failure code to the status.

        Raises:
            ValueError: A degraded row names no reduced workflow, a
                failed row names no failure code, a verified row carries
                either, or the evidence expires before it was taken.
        """
        if self.status == "degraded" and self.degradation_workflow_ref is None:
            raise ValueError("a degraded capability requires degradation_workflow_ref")
        if self.status == "failed" and self.failure_code is None:
            raise ValueError("a failed capability requires failure_code")
        if self.status == "verified" and (
            self.degradation_workflow_ref is not None or self.failure_code is not None
        ):
            raise ValueError(
                "a verified capability carries no degradation workflow or failure code"
            )
        if self.expires_at <= self.verified_at:
            raise ValueError("expires_at must follow verified_at")
        return self


class UnattendedDecision(RuntimeRecord):
    """Whether one certification admits unattended dispatch, and why not."""

    admitted: StrictBool
    refused_axis: DenialAxis | None = None
    reason: BoundedText | None = None

    @model_validator(mode="after")
    def _refusal_names_its_axis(self) -> Self:
        """Require a refusal to name its axis and an admission to name none.

        Raises:
            ValueError: An admitted decision carries a refusal, or a
                refused decision omits its axis or its reason.
        """
        if self.admitted and (self.refused_axis is not None or self.reason is not None):
            raise ValueError("an admitted decision carries no refused_axis or reason")
        if not self.admitted and (self.refused_axis is None or self.reason is None):
            raise ValueError("a refused decision requires both refused_axis and reason")
        return self


class DriverCertification(RuntimeRecord):
    """What one runtime tuple was certified to do, and how far that holds."""

    schema_version: Literal["driver-certification/v1"]
    certification_id: CertificationId
    manifest_ref: DriverManifestUrn
    manifest_digest: Digest
    distribution_version: SemVer
    sdk_or_server_version: SemVer
    auth_kind: AuthKind
    model_family: BoundedIdentifier
    os_class: Literal["linux", "macos", "windows"]
    architecture: Literal["x86_64", "aarch64"]
    managed_profile_digest: Digest
    worker_protocol_version: SemVer
    semantic_protocol_version: SemVer
    event_codec_version: SemVer
    conformance_suite_version: SemVer
    capabilities: Annotated[tuple[CapabilityCertification, ...], Field(min_length=1)]
    overall_status: CertificationStatus
    install_trust: InstallTrust
    runtime_facts: CertifiedRuntimeFacts
    stage_history: Annotated[tuple[ConformanceStageRecord, ...], Field(min_length=1)]
    evidence_bundle_ref: ArtifactUrn
    verified_at: UtcDatetime
    expires_at: UtcDatetime
    revoked_at: UtcDatetime | None = None
    revocation_reason: BoundedText | None = None
    quarantine_trigger: QuarantineTrigger | None = None

    @model_validator(mode="after")
    def _record_is_self_consistent(self) -> Self:
        """Bind expiry, revocation, quarantine, capabilities and stages.

        Raises:
            ValueError: Expiry does not follow verification, only one of
                the revocation fields is present, the quarantine trigger
                and the quarantined trust do not appear together, two
                capability rows share an id, or the stage history runs
                out of stage order.
        """
        if self.expires_at <= self.verified_at:
            raise ValueError("expires_at must follow verified_at")
        if (self.revoked_at is None) != (self.revocation_reason is None):
            raise ValueError("revoked_at and revocation_reason appear together or not at all")
        quarantined = self.install_trust == "quarantined"
        if quarantined and self.quarantine_trigger is None:
            raise ValueError("quarantined install_trust requires quarantine_trigger")
        if not quarantined and self.quarantine_trigger is not None:
            raise ValueError("quarantine_trigger requires quarantined install_trust")
        reject_repeats(tuple(row.capability_id for row in self.capabilities))
        ranks = [STAGE_ORDER.index(row.stage) for row in self.stage_history]
        if ranks != sorted(ranks):
            raise ValueError("stage_history must be appended in stage order")
        return self

    def decide_unattended_dispatch(
        self, *, required_capabilities: tuple[CapabilityId, ...]
    ) -> UnattendedDecision:
        """Decide whether this certification admits unattended dispatch.

        The three checks are separate axes rather than one verdict, so a
        refusal names the axis that produced it and the operator repairs
        the right thing: a stale certification of a managed installation
        and a fresh certification of an untrusted one both refuse, and
        they refuse differently.

        Args:
            required_capabilities: The capability ids the dispatch needs,
                each of which must be certified ``verified``.

        Returns:
            An admitted decision, or a refusal naming its axis.
        """
        if self.install_trust != "managed":
            return UnattendedDecision(
                admitted=False,
                refused_axis="install_trust",
                reason=(
                    f"install_trust is {self.install_trust!r}; unattended dispatch "
                    f"requires 'managed'"
                ),
            )
        if self.overall_status != "verified":
            return UnattendedDecision(
                admitted=False,
                refused_axis="overall_status",
                reason=(
                    f"overall_status is {self.overall_status!r}; unattended dispatch "
                    f"requires 'verified'"
                ),
            )
        certified = {row.capability_id: row.status for row in self.capabilities}
        unverified = tuple(
            capability_id
            for capability_id in required_capabilities
            if certified.get(capability_id) != "verified"
        )
        if unverified:
            named = ", ".join(unverified)
            return UnattendedDecision(
                admitted=False,
                refused_axis="capability",
                reason=f"required capabilities are not certified verified: {named}"[
                    :_MAX_REASON_CHARS
                ],
            )
        return UnattendedDecision(admitted=True)
