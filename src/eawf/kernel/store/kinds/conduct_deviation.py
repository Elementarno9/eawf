"""ConductDeviation -- payload for :attr:`StoreKind.CONDUCT_DEVIATION` records.

One row per observed breach of a conduct obligation. The rows live in the
machine-local store tier (``<state_dir>/local/``), never in the committed
``store/`` directory: a deviation is an observation about how one machine's
runs behaved, not a fact about the repository.

Whether ``obligation_id`` names a real conduct obligation is only knowable
against the compiled rule graph, which this kernel model does not import. A
writer passes the known obligation set as validation context under
:data:`KNOWN_OBLIGATIONS_CONTEXT`; a reader re-validating an existing row
passes none, so a row stays readable after its obligation is retired.
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationInfo, field_validator

from eawf.kernel.state.enums import IncidentSeverity
from eawf.kernel.store.kinds.events.base import RuntimeTriple

#: The validation-context key carrying the obligation identifiers a new
#: deviation may name.
KNOWN_OBLIGATIONS_CONTEXT: Final = "known_obligations"

DeviationDetection = Literal["practice_trigger", "review", "gate", "self_report"]
DeviationDisposition = Literal["open", "corrected", "accepted", "waived"]

_Token = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]


class ConductDeviation(BaseModel):
    """One recorded breach of a conduct obligation.

    Attributes:
        obligation_id: The conduct obligation that was breached.
        scope_id: The lifecycle scope the breaching run served, such as a
            wave identifier.
        run_id: The run that produced the deviation.
        runtime: The runtime the run executed on, so the rate splits by
            runtime.
        detection: How the deviation was noticed.
        severity: How much the breach matters.
        evidence_ref: Where the evidence of the breach can be read.
        disposition: What was done about it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    obligation_id: Annotated[
        str,
        StringConstraints(
            strict=True, max_length=128, pattern=r"^[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)*$"
        ),
    ]
    scope_id: _Token
    run_id: _Token
    runtime: RuntimeTriple
    detection: DeviationDetection
    severity: IncidentSeverity
    evidence_ref: _Token
    disposition: DeviationDisposition = "open"

    @field_validator("obligation_id")
    @classmethod
    def _known_obligation(cls, value: str, info: ValidationInfo) -> str:
        """Refuse an obligation the writer's compiled graph does not define.

        Args:
            value: The shape-checked obligation identifier.
            info: Carries the known obligation set under
                :data:`KNOWN_OBLIGATIONS_CONTEXT` when a writer validates.

        Returns:
            ``value`` unchanged.

        Raises:
            ValueError: When a known set is supplied and ``value`` is not in it.
        """
        context = info.context or {}
        known = context.get(KNOWN_OBLIGATIONS_CONTEXT)
        if known is not None and value not in known:
            raise ValueError(f"{value!r} is not a compiled conduct obligation")
        return value


__all__ = [
    "KNOWN_OBLIGATIONS_CONTEXT",
    "ConductDeviation",
    "DeviationDetection",
    "DeviationDisposition",
]
