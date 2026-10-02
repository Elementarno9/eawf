"""MachineCertification -- payload for :attr:`StoreKind.RUNTIME_CERTIFICATION` records.

One row per conformance probe of one runtime version installed on this machine.
The rows live in the machine-local store tier (``<state_dir>/local/``), never in
the committed ``store/`` directory: a certification describes the binary
installed on one machine, and a clone certifies its own installation.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.runtime.certification import (
    CapabilityCertification,
    CertificationFailureCode,
    ConformanceStageRecord,
)
from eawf.kernel.runtime.compiled import BoundedText
from eawf.kernel.runtime.provider import Digest, DriverCertificationUrn
from eawf.kernel.state.epoch2.run import RuntimeVersion
from eawf.kernel.state.types import UtcDatetime


class MachineCertification(BaseModel):
    """What one conformance probe of one installed runtime version concluded.

    Attributes:
        certification_urn: The URN a control decision cites this row by.
        runtime_id: The runtime, such as ``claude-code`` or ``codex``.
        harness_version: The installed version the probe read off the binary.
        tuple_digest: The digest the probe stage is journaled under, which a
            later quarantine of the tuple is filed under too.
        outcome: ``certified`` for a passed probe, ``quarantined`` for a
            failed one.
        capabilities: Each capability the probe has a rule for, verified or
            unsupported; empty for a quarantined version.
        reason_code: Why the probe failed; ``None`` for a certified version.
        findings: What the probe found missing, one line per capability;
            empty for a certified version.
        reason: One sentence an operator reads about the outcome.
        probe: The probe stage record the runner journaled.
        verified_at: When the probe completed.
        expires_at: When a certification stops being current; ``None`` for a
            quarantine, which holds until a later probe passes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    certification_urn: DriverCertificationUrn
    runtime_id: Annotated[str, Field(min_length=1, max_length=64)]
    harness_version: RuntimeVersion
    tuple_digest: Digest
    outcome: Literal["certified", "quarantined"]
    capabilities: tuple[CapabilityCertification, ...] = ()
    reason_code: CertificationFailureCode | None = None
    findings: tuple[BoundedText, ...] = ()
    reason: BoundedText
    probe: ConformanceStageRecord
    verified_at: UtcDatetime
    expires_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _outcome_carries_its_facts(self) -> Self:
        """Bind the capabilities, the failure and the expiry to the outcome.

        Raises:
            ValueError: A certified row lacks capabilities or an expiry after
                its verification, or carries a failure; a quarantined row
                lacks its reason code or findings, or carries capabilities or
                an expiry; or the probe's own outcome disagrees.
        """
        if self.outcome == "certified":
            if not self.capabilities or self.probe.outcome != "passed":
                raise ValueError("a certified version rests on a passed probe and its capabilities")
            if self.reason_code is not None or self.findings:
                raise ValueError("a certified version carries no reason_code or findings")
            if self.expires_at is None or self.expires_at <= self.verified_at:
                raise ValueError("a certified version expires after it was verified")
            return self
        if self.probe.outcome != "failed" or self.reason_code is None or not self.findings:
            raise ValueError("a quarantined version rests on a failed probe and names its findings")
        if self.capabilities or self.expires_at is not None:
            raise ValueError("a quarantined version carries no capabilities or expiry")
        return self


__all__ = ["MachineCertification"]
