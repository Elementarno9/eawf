"""Strict committed and local contracts for deterministic gate evidence."""

from __future__ import annotations

import hashlib
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import GateReceiptResult
from eawf.kernel.state.models import DigestStr, IdStr, ShaStr
from eawf.kernel.state.types import UtcDatetime

_TAIL_MAX_CHARS = 16_384
_DETAILS_MAX_CHARS = 4_000
_COMMAND_MAX_CHARS = 2_000
_ARGV_MAX_ITEMS = 512
_ARGV_ITEM_MAX_CHARS = 4_096

GateReceiptIdStr = Annotated[str, Field(pattern=r"^GR-[A-Za-z0-9_.-]+$")]
GateDiagnosticIdStr = Annotated[str, Field(pattern=r"^GD-GR-[A-Za-z0-9_.-]+$")]
GateIdentityStr = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.-]+$")]
_GATE_DIGEST_PATTERN = r"^(?:[a-z0-9][a-z0-9+.-]{0,15}:)?[0-9a-f]{32,128}$"
_GATE_DIGEST_RE = re.compile(_GATE_DIGEST_PATTERN)
GateDigestStr = Annotated[str, Field(pattern=_GATE_DIGEST_PATTERN)]


def canonical_gate_digest(value: str) -> str:
    """Return a path/URL-safe digest, preserving already canonical values."""
    if _GATE_DIGEST_RE.fullmatch(value):
        return value
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


class GateReceipt(BaseModel):
    """PII-safe, freshness-bound proof committed to ``gate_receipt.jsonl``.

    Raw commands, output, free-form details, and filesystem references are
    deliberately absent. Those observations belong to :class:`GateDiagnostic`
    under the gitignored ``.ea/local/gate-diagnostics`` directory.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["2.0"] = "2.0"
    id: GateReceiptIdStr
    scope_id: GateIdentityStr
    criterion_id: GateIdentityStr | None = None
    gate_id: GateIdentityStr
    integration_id: GateIdentityStr
    integrated_sha: ShaStr
    tree_sha: ShaStr
    contract_digest: GateDigestStr
    criteria_digest: GateDigestStr
    gate_manifest_digest: GateDigestStr
    policy_digest: GateDigestStr
    dependency_binding_digest: GateDigestStr
    runner_environment_digest: GateDigestStr
    runner_digest: GateDigestStr
    environment_digest: GateDigestStr
    freshness_key: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    argv_digest: GateDigestStr | None = None
    timeout_class: Literal["quick", "standard", "slow", "very_slow"] | None = None
    resolved_timeout_seconds: Annotated[float, Field(gt=0.0)] | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_ms: Annotated[int, Field(ge=0)]
    result: GateReceiptResult
    exit_status: int | None = None
    stdout_digest: GateDigestStr | None = None
    stderr_digest: GateDigestStr | None = None
    selected_file_digest: GateDigestStr | None = None
    collected_nodeid_digest: GateDigestStr | None = None
    residual_manifest_digest: GateDigestStr | None = None

    @model_validator(mode="after")
    def _ended_not_before_started(self) -> GateReceipt:
        """Reject reversed execution time."""
        if self.ended_at < self.started_at:
            raise ValueError("ended_at cannot precede started_at")
        return self


class GateDiagnostic(BaseModel):
    """Daemon-local raw observations for one committed gate receipt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    id: GateDiagnosticIdStr
    receipt_id: GateReceiptIdStr
    attempt_id: IdStr | None = None
    scope_id: GateIdentityStr
    criterion_id: GateIdentityStr | None = None
    gate_id: GateIdentityStr
    captured_at: UtcDatetime
    argv: (
        Annotated[
            list[Annotated[str, Field(min_length=1, max_length=_ARGV_ITEM_MAX_CHARS)]],
            Field(min_length=1, max_length=_ARGV_MAX_ITEMS),
        ]
        | None
    ) = None
    command: Annotated[str, Field(min_length=1, max_length=_COMMAND_MAX_CHARS)] | None = None
    details: Annotated[str, Field(max_length=_DETAILS_MAX_CHARS)] | None = None
    stdout_tail: Annotated[str, Field(max_length=_TAIL_MAX_CHARS)] | None = None
    stderr_tail: Annotated[str, Field(max_length=_TAIL_MAX_CHARS)] | None = None
    source_log_ref: Annotated[str, Field(min_length=1, max_length=500)] | None = None
    log_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    log_present: bool = False

    @model_validator(mode="after")
    def _log_facts_are_complete(self) -> GateDiagnostic:
        """Require copied-log presence and digest to agree."""
        if self.log_present != (self.log_digest is not None):
            raise ValueError("log_present and log_digest must be provided together")
        return self


class LegacyGateReceipt(BaseModel):
    """Strict pre-v0.6.5 receipt accepted only by daemon scrub migration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: GateReceiptIdStr
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
    command: Annotated[str, Field(min_length=1, max_length=_COMMAND_MAX_CHARS)] | None = None
    timeout_class: Literal["quick", "standard", "slow", "very_slow"] | None = None
    resolved_timeout_seconds: Annotated[float, Field(gt=0.0)] | None = None
    started_at: UtcDatetime
    ended_at: UtcDatetime
    duration_ms: Annotated[int, Field(ge=0)]
    result: GateReceiptResult
    details: Annotated[str, Field(max_length=_DETAILS_MAX_CHARS)] | None = None
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
    def _legacy_facts_are_consistent(self) -> LegacyGateReceipt:
        """Reject reversed time and incomplete command facts."""
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


__all__ = [
    "GateDiagnostic",
    "GateDigestStr",
    "GateReceipt",
    "LegacyGateReceipt",
    "canonical_gate_digest",
]
