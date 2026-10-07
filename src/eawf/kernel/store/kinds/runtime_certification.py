"""MachineCertification -- payload for :attr:`StoreKind.RUNTIME_CERTIFICATION` records.

One row per conformance walk of one runtime version installed on this machine:
the probe stage alone, or the probe followed by the canary and certify stages.
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
from eawf.kernel.runtime.provider import Digest, DriverCertificationUrn, ModelId
from eawf.kernel.state.epoch2.run import RuntimeVersion
from eawf.kernel.state.types import UtcDatetime


class MachineCertification(BaseModel):
    """What one conformance walk of one installed runtime version concluded.

    Attributes:
        certification_urn: The URN a control decision cites this row by.
        runtime_id: The runtime, such as ``claude-code`` or ``codex``.
        harness_version: The installed version the probe read off the binary.
        tuple_digest: The digest the probe stage is journaled under, which a
            later quarantine of the tuple is filed under too.
        outcome: ``certified`` for a passed walk, ``quarantined`` for a
            failed probe or canary.
        capabilities: Each capability the probe has a rule for, verified or
            unsupported, and each one the canary observed; empty for a
            quarantined version.
        reason_code: Why the walk failed; ``None`` for a certified version.
        findings: What the probe found missing, one line per capability;
            empty for a certified version.
        reason: One sentence an operator reads about the outcome.
        probe: The probe stage record the runner journaled.
        stages: The stages walked after the probe, in order: empty for a
            probe alone, else the canary and, after a passed canary, the
            certify stage.
        observed_model: The model the canary's worker ran, as its runtime
            reported it; ``None`` when no canary ran or none was reported.
        verified_at: When the last stage completed.
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
    stages: tuple[ConformanceStageRecord, ...] = ()
    observed_model: ModelId | None = None
    verified_at: UtcDatetime
    expires_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _stages_follow_the_probe(self) -> Self:
        """Require the stages after the probe to be the walk's closed sequence.

        Raises:
            ValueError: The stages are not a canary then at most a certify,
                a canary follows a probe that did not pass, a certify follows
                a canary that did not pass, or a certified walk ends without
                its certify stage.
        """
        walked = tuple(stage.stage for stage in self.stages)
        if walked not in ((), ("canary",), ("canary", "certify")):
            raise ValueError("the stages after the probe are a canary, then at most a certify")
        if walked and self.probe.outcome != "passed":
            raise ValueError("a canary is walked only after a passed probe")
        if len(walked) == 2 and self.stages[0].outcome != "passed":
            raise ValueError("a certify stage is walked only after a passed canary")
        if walked == ("canary",) and self.outcome == "certified":
            raise ValueError("a certified walk ends in its certify stage")
        return self

    @model_validator(mode="after")
    def _outcome_carries_its_facts(self) -> Self:
        """Bind the capabilities, the failure and the expiry to the outcome.

        The deciding stage is the canary when one ran, else the probe.

        Raises:
            ValueError: A certified row lacks capabilities or an expiry after
                its verification, or carries a failure; a quarantined row
                lacks its reason code or findings, or carries capabilities or
                an expiry; or the deciding stage's own outcome disagrees.
        """
        deciding = self.stages[0] if self.stages else self.probe
        if self.outcome == "certified":
            if not self.capabilities or deciding.outcome != "passed":
                raise ValueError("a certified version rests on a passed stage and its capabilities")
            if self.reason_code is not None or self.findings:
                raise ValueError("a certified version carries no reason_code or findings")
            if self.expires_at is None or self.expires_at <= self.verified_at:
                raise ValueError("a certified version expires after it was verified")
            return self
        if deciding.outcome == "passed" or self.reason_code is None or not self.findings:
            raise ValueError(
                "a quarantined version rests on a failed probe or canary and names its findings"
            )
        if self.capabilities or self.expires_at is not None:
            raise ValueError("a quarantined version carries no capabilities or expiry")
        return self


__all__ = ["MachineCertification"]
