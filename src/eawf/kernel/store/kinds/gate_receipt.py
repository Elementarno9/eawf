"""Strict payload contract for immutable ``gate_receipt.jsonl`` rows."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.models import DigestStr, IdStr, ShaStr
from eawf.kernel.state.types import UtcDatetime

_TAIL_MAX_CHARS = 16_384


class GateReceipt(BaseModel):
    """Freshness-bound deterministic proof for one gate execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: IdStr
    scope_id: Annotated[str, Field(min_length=1)]
    criterion_id: IdStr | None = None
    gate_id: IdStr
    integration_id: IdStr
    integrated_sha: ShaStr
    tree_sha: ShaStr
    contract_digest: DigestStr
    criteria_digest: DigestStr
    gate_manifest_digest: DigestStr
    policy_digest: DigestStr
    dependency_binding_digest: DigestStr
    runner_environment_digest: DigestStr
    runner_digest: DigestStr
    environment_digest: DigestStr
    freshness_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    argv: list[str] | None = None
    argv_digest: DigestStr | None = None
    command: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    timeout_class: Literal["quick", "standard", "slow", "very_slow"] | None = None
    resolved_timeout_seconds: Annotated[float, Field(gt=0.0)] | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_ms: Annotated[int, Field(ge=0)]
    result: GateReceiptResult
    details: Annotated[str, Field(max_length=4000)] | None = None
    exit_status: int | None = None
    stdout_tail: Annotated[str, Field(max_length=_TAIL_MAX_CHARS)] | None = None
    stderr_tail: Annotated[str, Field(max_length=_TAIL_MAX_CHARS)] | None = None
    stdout_digest: DigestStr | None = None
    stderr_digest: DigestStr | None = None
    full_log_ref: Annotated[str, Field(min_length=1, max_length=500)]
    selected_file_digest: DigestStr | None = None
    collected_nodeid_digest: DigestStr | None = None
    residual_manifest_digest: DigestStr | None = None

    @model_validator(mode="after")
    def _ended_not_before_started(self) -> GateReceipt:
        """Reject reversed time or incomplete optional command facts."""
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot precede started_at")
        if (self.argv is None) != (self.argv_digest is None):
            raise ValueError("argv and argv_digest must be provided together")
        if self.argv is not None and (
            not self.argv or any(not isinstance(arg, str) or not arg for arg in self.argv)
        ):
            raise ValueError("argv must contain non-empty strings")
        if self.argv is None and (self.command is not None or self.timeout_class is not None):
            raise ValueError("command and timeout_class require argv")
        return self


__all__ = ["GateReceipt"]
