"""Typed result models shared by doctor checks and renderers."""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

from eawf.kernel.runtime.certification import ConformanceStage
from eawf.kernel.runtime.provider import ArtifactUrn

CheckStatus = Literal["ok", "warn", "fail"]

#: The daemon verbs that can produce a runtime-tuple verdict. Closed,
#: because a verdict no verb wrote is a verdict nothing observed.
HealthProducer = Literal[
    "conformance.probe",
    "conformance.canary",
    "conformance.certify",
    "conformance.quarantine",
    "conformance.rollback",
]

#: Name prefix of every runtime-tuple health check. A check under this
#: prefix reports what the conformance runner concluded rather than a
#: condition doctor measured itself, so it has to say which stage of which
#: verb it is repeating.
RUNTIME_TUPLE_CHECK_PREFIX: Final = "runtime_tuple"


class HealthProvenance(BaseModel):
    """Which run of which verb a check's verdict is repeating.

    Attributes:
        producer: The daemon verb that wrote the stage record.
        stage: The conformance stage the verdict was reached at.
        evidence_ref: The artifact that stage filed its evidence under.
    """

    model_config = ConfigDict(extra="forbid")

    producer: HealthProducer
    stage: ConformanceStage
    evidence_ref: ArtifactUrn


class CheckResult(BaseModel):
    """Single doctor check outcome.

    Attributes:
        name: Stable machine identifier (``"tools_available"``, ...).
        status: ``ok`` (everything fine), ``warn`` (functional but degraded),
            or ``fail`` (broken — the doctor surface still completes, but the
            CLI exits non-zero).
        detail: Short human message. ``None`` when the check has nothing
            interesting to add beyond ``status``.
        provenance: Where the verdict came from. Required of a
            runtime-tuple check, absent from a check doctor measured
            itself.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: CheckStatus
    detail: str | None = None
    provenance: HealthProvenance | None = None

    @model_validator(mode="after")
    def _tuple_check_carries_its_provenance(self) -> Self:
        """Require a runtime-tuple check to name the run behind it.

        Raises:
            ValueError: A check under
                :data:`RUNTIME_TUPLE_CHECK_PREFIX` carries no provenance,
                which would report a conformance verdict the operator
                cannot trace back to the stage that reached it.
        """
        if self.name.startswith(RUNTIME_TUPLE_CHECK_PREFIX) and self.provenance is None:
            raise ValueError(f"{self.name!r} is a runtime-tuple check and requires provenance")
        return self


__all__ = [
    "RUNTIME_TUPLE_CHECK_PREFIX",
    "CheckResult",
    "CheckStatus",
    "HealthProducer",
    "HealthProvenance",
]
