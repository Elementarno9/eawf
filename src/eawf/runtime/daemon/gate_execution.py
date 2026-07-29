"""Crash-safe execution claims for deterministic close gates.

Claims live under daemon-local ``.ea/local`` state and are keyed by the full
freshness digest. A claim is written atomically before a subprocess starts.
Only a persisted terminal :class:`GateReceipt` completes it. Therefore a daemon
restart can reuse a terminal result, while an orphaned pre-execution claim is
reported indeterminate and never rerun.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import GateReceiptResult, StoreKind
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import GateReceipt
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.gate_receipt_hygiene import (
    diagnostic_log_path,
    load_gate_diagnostic,
    scrub_gate_receipt_store,
)
from eawf.runtime.lock import portalock
from eawf.workflow.audit_dsl.models import (
    CheckResult,
    CheckSpec,
)


class GateExecutionClaim(BaseModel):
    """One durable freshness-key execution claim."""

    model_config = ConfigDict(extra="forbid")

    attempt_id: str = Field(min_length=1)
    criterion_id: str = Field(min_length=1)
    gate_id: str = Field(min_length=1)
    freshness_key: str = Field(min_length=64, max_length=64)
    claimed_at: datetime
    receipt_id: str | None = None
    result_payload: dict[str, Any] | None = None
    completed_at: datetime | None = None


def gate_receipt_id(freshness_key: str) -> str:
    """Return the canonical receipt id for *freshness_key*."""
    return f"GR-{freshness_key[:32]}"


def claim_path(
    state_path: Path,
    *,
    attempt_id: str,
    freshness_key: str,
) -> Path:
    """Return the daemon-local claim path for one global freshness key."""
    del attempt_id
    return state_path.parent / "local" / "gate-claims" / f"{freshness_key}.json"


def _load_claim(path: Path) -> GateExecutionClaim | None:
    if not path.is_file():
        return None
    try:
        return GateExecutionClaim.model_validate(orjson.loads(path.read_bytes()))
    except (OSError, orjson.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"gate execution claim unreadable: {path.name!r}") from exc


def _load_receipt(state_path: Path, receipt_id: str) -> GateReceipt | None:
    scrub_gate_receipt_store(state_path)
    path = store_path(state_path, StoreKind.GATE_RECEIPT)
    if not path.is_file():
        return None
    try:
        lines = path.read_bytes().splitlines()
    except OSError:
        return None
    for line in lines:
        if not line:
            continue
        try:
            envelope = Envelope.model_validate(orjson.loads(line))
            if envelope.id != receipt_id:
                continue
            return GateReceipt.model_validate(envelope.payload)
        except orjson.JSONDecodeError, ValueError:
            continue
    return None


def _receipt_status(
    receipt: GateReceipt,
) -> tuple[bool, Literal["pass", "fail", "blocked"]]:
    if receipt.result is GateReceiptResult.PASS:
        return True, "pass"
    if receipt.result in {
        GateReceiptResult.BLOCKED,
        GateReceiptResult.TIMEOUT,
        GateReceiptResult.ERROR,
        GateReceiptResult.CANCELLED,
    }:
        return False, "blocked"
    return False, "fail"


def _result_from_receipt(
    receipt: GateReceipt,
    *,
    state_path: Path,
    spec: CheckSpec,
    freshness_key: str,
) -> CheckResult:
    passed, status = _receipt_status(receipt)
    diagnostic = load_gate_diagnostic(state_path, receipt.id)
    local_log = diagnostic_log_path(state_path, receipt.id)
    log_ref: str | None = None
    if local_log.is_file():
        try:
            log_ref = local_log.relative_to(state_path.parent.parent).as_posix()
        except ValueError:
            log_ref = None
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=passed,
        status=status,
        details=(
            diagnostic.details if diagnostic is not None else f"reused gate receipt {receipt.id}"
        ),
        started_at=receipt.started_at,
        ended_at=receipt.ended_at,
        duration_ms=receipt.duration_ms,
        timeout_class=receipt.timeout_class,
        resolved_timeout_seconds=(
            int(receipt.resolved_timeout_seconds)
            if receipt.resolved_timeout_seconds is not None
            else None
        ),
        exit_status=receipt.exit_status,
        argv=diagnostic.argv if diagnostic is not None else None,
        command=diagnostic.command if diagnostic is not None else None,
        stdout_tail=diagnostic.stdout_tail if diagnostic is not None else None,
        stderr_tail=diagnostic.stderr_tail if diagnostic is not None else None,
        stdout_digest=receipt.stdout_digest,
        stderr_digest=receipt.stderr_digest,
        selected_file_digest=receipt.selected_file_digest,
        collected_nodeid_digest=receipt.collected_nodeid_digest,
        residual_manifest_digest=receipt.residual_manifest_digest,
        runner_fingerprint=receipt.runner_digest,
        environment_fingerprint=receipt.environment_digest,
        full_log_ref=log_ref,
        freshness_key=freshness_key,
        freshness=spec.freshness,
    )


def _indeterminate_result(spec: CheckSpec, freshness_key: str) -> CheckResult:
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        status="blocked",
        details=(
            "indeterminate gate execution: freshness claim exists without "
            f"terminal receipt ({gate_receipt_id(freshness_key)})"
        ),
        freshness_key=freshness_key,
        freshness=spec.freshness,
    )


def _receipt_collision_result(spec: CheckSpec, freshness_key: str) -> CheckResult:
    """Fail closed when a truncated receipt id resolves to another full key."""
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        status="blocked",
        details=(
            "indeterminate gate receipt: receipt id collision for full "
            f"freshness key {freshness_key}"
        ),
        freshness_key=freshness_key,
        freshness=spec.freshness,
    )


def claim_gate_execution(
    state_path: Path,
    *,
    attempt_id: str,
    criterion_id: str,
    gate_id: str,
    spec: CheckSpec,
    freshness_key: str,
) -> CheckResult | None:
    """Claim one freshness key or return a reusable/indeterminate result.

    ``None`` means this caller wrote the first claim and may execute. A returned
    result means the subprocess must not start.
    """
    receipt_id = gate_receipt_id(freshness_key)
    receipt = _load_receipt(state_path, receipt_id)
    path = claim_path(
        state_path,
        attempt_id=attempt_id,
        freshness_key=freshness_key,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with portalock.acquire(path, timeout=5.0):
        claim = _load_claim(path)
        receipt = receipt or _load_receipt(state_path, receipt_id)
        if receipt is not None:
            if receipt.freshness_key != freshness_key:
                return _receipt_collision_result(spec, freshness_key)
            if (
                claim is not None
                and claim.receipt_id == receipt.id
                and claim.result_payload is not None
            ):
                return CheckResult.model_validate(claim.result_payload)
            return _result_from_receipt(
                receipt,
                state_path=state_path,
                spec=spec,
                freshness_key=freshness_key,
            )
        if claim is not None:
            return _indeterminate_result(spec, freshness_key)
        atomic_write_json_locked(
            path,
            GateExecutionClaim(
                attempt_id=attempt_id,
                criterion_id=criterion_id,
                gate_id=gate_id,
                freshness_key=freshness_key,
                claimed_at=datetime.now(UTC),
            ).model_dump(mode="json"),
        )
    return None


def complete_gate_execution(
    state_path: Path,
    *,
    attempt_id: str,
    freshness_key: str,
    receipt_id: str,
    result: CheckResult,
) -> None:
    """Complete a claim only after its terminal receipt is durable."""
    receipt = _load_receipt(state_path, receipt_id)
    if receipt is None:
        raise ValueError(f"gate receipt missing while completing claim: {receipt_id!r}")
    if receipt.freshness_key != freshness_key:
        raise ValueError("gate receipt freshness mismatch")
    path = claim_path(
        state_path,
        attempt_id=attempt_id,
        freshness_key=freshness_key,
    )
    if not path.is_file():
        return
    with portalock.acquire(path, timeout=5.0):
        claim = _load_claim(path)
        if claim is None:
            return
        if claim.freshness_key != freshness_key:
            raise ValueError("gate execution claim freshness mismatch")
        updated = claim.model_copy(
            update={
                "receipt_id": receipt_id,
                "result_payload": result.model_dump(mode="json"),
                "completed_at": datetime.now(UTC),
            }
        )
        atomic_write_json_locked(path, updated.model_dump(mode="json"))


__all__ = [
    "GateExecutionClaim",
    "claim_gate_execution",
    "claim_path",
    "complete_gate_execution",
    "gate_receipt_id",
]
