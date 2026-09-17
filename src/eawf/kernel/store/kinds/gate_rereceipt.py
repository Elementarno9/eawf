"""Append-only binding of a closed wave's gates to a fresh receipt set.

A wave that closed with required gates and zero receipts carries no proof
that its gates ever ran. The repair cannot be a receipt alone: a bare
``gate_receipt`` row says nothing about which landed revision produced it
or which wave row it was taken against, so nothing downstream could tell
a close-time receipt from one re-run months later.

This binding is that missing statement. One row per re-run names the
wave, the landed commit the gates ran at, every receipt the run produced,
and the two manifest digests (criteria and gates) that fix which version
of the wave's verification contract was replayed. The row is append-only
and the wave row is never touched, so a re-run adds evidence and can
never rewrite a recorded verdict.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.models import DigestStr, ShaStr
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.kinds.gate_receipt import (
    GateIdentityStr,
    GateReceiptIdStr,
)

GateRereceiptIdStr = Annotated[str, Field(pattern=r"^GRR-[0-9a-f]{12}$")]


class GateRereceiptOutcome(BaseModel):
    """What one re-run gate produced, receipt or refusal.

    ``receipt_id`` is ``None`` when the gate produced no durable receipt:
    a runner that crashed, a gate whose criterion does not compile to a
    runnable spec, or a run whose observations came back incomplete. The
    row keeps the gate rather than dropping it so the binding always
    accounts for every required gate.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: GateIdentityStr
    criterion_id: GateIdentityStr | None = None
    result: GateReceiptResult
    receipt_id: GateReceiptIdStr | None = None


class GateRereceiptBinding(BaseModel):
    """One re-run of a CLOSED wave's required gates at its landed commit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    id: GateRereceiptIdStr
    wave_id: GateIdentityStr
    landed_sha: ShaStr
    landed_tree_sha: ShaStr
    criteria_digest: DigestStr
    gate_manifest_digest: DigestStr
    wave_fingerprint_digest: DigestStr
    receipt_ids: list[GateReceiptIdStr] = Field(default_factory=list)
    gates: Annotated[list[GateRereceiptOutcome], Field(min_length=1)]
    ran_at: UtcDatetime

    @model_validator(mode="after")
    def _receipt_ids_match_gates(self) -> GateRereceiptBinding:
        """Reject a receipt list that does not come from the gate rows.

        The two views exist because readers want both: a flat receipt
        list to resolve, and a per-gate breakdown to explain. Letting
        them disagree would let a binding claim a receipt no gate
        produced.
        """
        produced = [gate.receipt_id for gate in self.gates if gate.receipt_id is not None]
        if self.receipt_ids != produced:
            raise ValueError("receipt_ids must list exactly the gate receipt ids, in gate order")
        if len(set(self.receipt_ids)) != len(self.receipt_ids):
            raise ValueError("receipt_ids must not repeat a receipt")
        if len({gate.gate_id for gate in self.gates}) != len(self.gates):
            raise ValueError("gates must not repeat a gate id")
        return self


__all__ = [
    "GateRereceiptBinding",
    "GateRereceiptIdStr",
    "GateRereceiptOutcome",
]
