"""Durable evidence and store bindings for asynchronous Wave close."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    GateReceiptResult,
    StoreKind,
)
from eawf.kernel.state.models import CloseAttempt, State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_receipt import (
    GateDiagnostic,
    GateReceipt,
    canonical_gate_digest,
)
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.gate_receipt_hygiene import (
    append_gate_receipt,
    diagnostic_id,
    scrub_gate_receipt_store,
    write_gate_diagnostic,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.workflow.audit_dsl.models import CheckResult, GateFreshnessInput

if TYPE_CHECKING:
    from eawf.workflow.dispatch.verdict import DurableAuditContext

logger = logging.getLogger(__name__)


def _state_path(ctx: MethodContext, repo_root: Path) -> Path:
    from eawf.surfaces.cli.scope import resolve_state_path

    if ctx.state_path is not None:
        configured = Path(ctx.state_path).resolve()
        if configured.parent.parent == repo_root:
            return configured
    return resolve_state_path(repo_root)


def _load_state(ctx: MethodContext, repo_root: Path) -> State:
    from eawf.runtime.daemon.methods.state import _read_state

    state, _payload = _read_state(_state_path(ctx, repo_root))
    return state


def _digest(value: Any) -> str:
    return hashlib.sha256(orjson.dumps(value, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _commit_attempt(
    ctx: MethodContext,
    *,
    repo_root: Path,
    attempt_id: str,
    updates: dict[str, Any],
    command: str,
) -> CloseAttempt:
    """Persist one immutable replacement of a durable close attempt."""
    from eawf.runtime.daemon.methods.state import _commit_worktree_state

    holder: list[CloseAttempt] = []

    def _apply(state: State) -> dict[str, Any]:
        current = state.close_attempts.get(attempt_id)
        if current is None:
            raise ValueError(f"unknown close attempt: {attempt_id!r}")
        payload = current.model_dump(mode="json")
        payload.update(updates)
        payload["updated_at"] = datetime.now(UTC)
        updated = CloseAttempt.model_validate(payload)
        state.close_attempts[attempt_id] = updated
        holder.append(updated)
        return {
            "attempt": updated.id,
            "wave": updated.wave_id,
            "status": updated.status.value,
        }

    _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params={"attempt_id": attempt_id, "status": str(updates.get("status", ""))},
        command=command,
        scope_id=attempt_id,
        apply_func=_apply,
    )
    return holder[0]


def gate_freshness_inputs(
    state: State,
    *,
    attempt_id: str,
) -> dict[str, GateFreshnessInput]:
    """Return immutable close-attempt facts keyed by required gate id."""
    attempt = state.close_attempts.get(attempt_id)
    if attempt is None:
        return {}
    return {
        gate_id: GateFreshnessInput(
            scope_id=attempt.wave_id,
            integration_id=attempt.integration_id,
            integrated_commit=attempt.integrated_sha,
            tree_digest=attempt.tree_sha,
            contract_digest=attempt.spec_digest,
            criteria_digest=attempt.criteria_digest,
            gate_manifest_digest=attempt.gate_manifest_digest,
            policy_digest=attempt.policy_digest,
            dependency_binding_digest=attempt.dependency_binding_digest,
            runner_environment_digest=attempt.runner_environment_digest,
            full_log_ref=(
                Path(".ea")
                / "local"
                / "gate-diagnostics"
                / "incoming"
                / attempt.id
                / f"gate-{hashlib.sha256(gate_id.encode()).hexdigest()[:16]}.log"
            ).as_posix(),
        )
        for gate_id in attempt.required_gate_ids
    }


def _gate_receipt_result(result: CheckResult) -> GateReceiptResult:
    """Map a check result onto the durable receipt result vocabulary."""
    if result.status == "pass":
        return GateReceiptResult.PASS
    if result.status == "blocked":
        if result.details is not None and "timeout" in result.details.lower():
            return GateReceiptResult.TIMEOUT
        return GateReceiptResult.BLOCKED
    return GateReceiptResult.FAIL


def _load_gate_receipt(path: Path, receipt_id: str) -> GateReceipt | None:
    """Return one validated receipt by id, ignoring unrelated/corrupt rows."""
    if not path.is_file():
        return None
    for line in path.read_bytes().splitlines():
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


def _reuse_existing_gate_receipt(
    ctx: MethodContext,
    *,
    repo_root: Path,
    attempt: CloseAttempt,
    criterion_id: str,
    gate_id: str,
    freshness_key: str,
    receipt_path: Path,
    receipt_id: str,
) -> tuple[bool, str | None]:
    """Validate and bind an existing exact receipt without recopying proof."""
    existing = _load_gate_receipt(receipt_path, receipt_id)
    if existing is None:
        return False, None
    exact = (
        existing.freshness_key == freshness_key
        and existing.scope_id == attempt.wave_id
        and existing.criterion_id == criterion_id
        and existing.gate_id == gate_id
        and existing.integration_id == attempt.integration_id
        and existing.integrated_sha == attempt.integrated_sha
        and existing.tree_sha == attempt.tree_sha
        and existing.contract_digest == canonical_gate_digest(attempt.spec_digest)
        and existing.criteria_digest == canonical_gate_digest(attempt.criteria_digest)
        and existing.gate_manifest_digest == canonical_gate_digest(attempt.gate_manifest_digest)
        and existing.policy_digest == canonical_gate_digest(attempt.policy_digest)
        and existing.dependency_binding_digest
        == canonical_gate_digest(attempt.dependency_binding_digest)
        and existing.runner_environment_digest
        == canonical_gate_digest(attempt.runner_environment_digest)
    )
    if not exact:
        logger.warning(
            f"persist_gate_receipt status=skip attempt={attempt.id!r} gate={gate_id!r} "
            "reason=existing-receipt-mismatch"
        )
        return True, None
    row = _load_state(ctx, repo_root).close_attempts.get(attempt.id)
    if row is not None and existing.id not in row.gate_receipt_ids:
        _commit_attempt(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt.id,
            updates={"gate_receipt_ids": [*row.gate_receipt_ids, existing.id]},
            command="close.gate_receipt",
        )
    return True, existing.id


def reusable_pass_gate_ids(
    ctx: MethodContext,
    *,
    repo_root: Path,
    attempt_id: str,
) -> set[str]:
    """Return gates with pass receipts bound to the attempt's frozen inputs."""
    scrub_gate_receipt_store(_state_path(ctx, repo_root))
    state = _load_state(ctx, repo_root)
    attempt = state.close_attempts.get(attempt_id)
    if attempt is None or not attempt.gate_receipt_ids:
        return set()
    wanted = set(attempt.gate_receipt_ids)
    path = store_path(_state_path(ctx, repo_root), StoreKind.GATE_RECEIPT)
    if not path.is_file():
        return set()
    reusable: set[str] = set()
    for line in path.read_bytes().splitlines():
        if not line:
            continue
        try:
            envelope = Envelope.model_validate(orjson.loads(line))
            if envelope.id not in wanted:
                continue
            receipt = GateReceipt.model_validate(envelope.payload)
        except orjson.JSONDecodeError, ValueError:
            continue
        if (
            receipt.result is GateReceiptResult.PASS
            and receipt.id == f"GR-{receipt.freshness_key[:32]}"
            and receipt.integration_id == attempt.integration_id
            and receipt.integrated_sha == attempt.integrated_sha
            and receipt.tree_sha == attempt.tree_sha
            and receipt.contract_digest == canonical_gate_digest(attempt.spec_digest)
            and receipt.criteria_digest == canonical_gate_digest(attempt.criteria_digest)
            and receipt.gate_manifest_digest == canonical_gate_digest(attempt.gate_manifest_digest)
            and receipt.policy_digest == canonical_gate_digest(attempt.policy_digest)
            and receipt.dependency_binding_digest
            == canonical_gate_digest(attempt.dependency_binding_digest)
            and receipt.runner_environment_digest
            == canonical_gate_digest(attempt.runner_environment_digest)
        ):
            reusable.add(receipt.gate_id)
    return reusable


def reusable_bound_audit_report_id(
    state_path: Path,
    *,
    attempt_id: str,
    durable_context: DurableAuditContext,
) -> str | None:
    """Return the exact bound auditor report, refusing stale/missing proof."""
    from eawf.workflow.agent_report.rollup import find_agent_report
    from eawf.workflow.dispatch.verdict import parse_auditor_report_body

    state = State.model_validate_json(state_path.read_bytes())
    attempt = state.close_attempts.get(attempt_id)
    if attempt is None:
        raise ValueError(f"unknown close attempt: {attempt_id!r}")
    report_id = attempt.audit_report_id
    if report_id is None:
        return None
    expected_context = {
        "close_attempt_id": attempt.id,
        "integration_id": attempt.integration_id,
        "integrated_sha": attempt.integrated_sha,
        "tree_sha": attempt.tree_sha,
        "spec_digest": attempt.spec_digest,
        "criteria_digest": attempt.criteria_digest,
        "gate_manifest_digest": attempt.gate_manifest_digest,
        "policy_digest": attempt.policy_digest,
        "runner_digest": attempt.runner_environment_digest,
        "dependency_binding_digest": attempt.dependency_binding_digest,
    }
    mismatched = [
        field
        for field, expected in expected_context.items()
        if getattr(durable_context, field) != expected
    ]
    if durable_context.wave_id != attempt.wave_id:
        mismatched.append("wave_id")
    if mismatched:
        raise ValueError(f"bound audit context changed: {', '.join(sorted(mismatched))}")
    row = find_agent_report(
        state_path,
        report_id,
        role=AgentSessionRole.AUDITOR,
    )
    if row is None:
        raise ValueError(f"bound audit report missing: {report_id!r}")
    if row.payload.header.report_id != report_id or row.payload.header.base_id != attempt.wave_id:
        raise ValueError(f"bound audit report binding mismatch: {report_id!r}")
    try:
        body = parse_auditor_report_body(
            row.payload.body.model_dump(mode="json"),
            durable_context=durable_context,
        )
    except ValueError as exc:
        raise ValueError(f"bound audit report context mismatch: {report_id!r}") from exc
    if body.verdict not in {
        AgentReportVerdict.PASS,
        AgentReportVerdict.PASS_WITH_FOLLOWUPS,
    }:
        raise ValueError(f"bound audit report is not close-ready: {report_id!r}")
    return report_id


def persist_gate_receipt(
    ctx: MethodContext,
    *,
    repo_root: Path,
    execution_root: Path,
    attempt_id: str,
    criterion_id: str,
    gate_id: str,
    result: CheckResult,
) -> str | None:
    """Persist one complete deterministic receipt and bind it to its attempt."""
    scrub_gate_receipt_store(_state_path(ctx, repo_root))
    state = _load_state(ctx, repo_root)
    attempt = state.close_attempts.get(attempt_id)
    if attempt is None:
        return None
    freshness = result.freshness
    expected_freshness = {
        "scope_id": attempt.wave_id,
        "criterion_id": criterion_id,
        "integration_id": attempt.integration_id,
        "integrated_commit": attempt.integrated_sha,
        "tree_digest": attempt.tree_sha,
        "contract_digest": attempt.spec_digest,
        "criteria_digest": attempt.criteria_digest,
        "gate_manifest_digest": attempt.gate_manifest_digest,
        "policy_digest": attempt.policy_digest,
        "dependency_binding_digest": attempt.dependency_binding_digest,
        "runner_environment_digest": attempt.runner_environment_digest,
    }
    if freshness is None or any(
        getattr(freshness, field) != expected for field, expected in expected_freshness.items()
    ):
        logger.warning(
            f"persist_gate_receipt status=skip attempt={attempt_id!r} gate={gate_id!r} "
            "reason=freshness-mismatch"
        )
        return None
    required = (
        result.started_at,
        result.ended_at,
        result.duration_ms,
        result.runner_fingerprint,
        result.environment_fingerprint,
        result.full_log_ref,
        result.freshness_key,
    )
    if any(value is None for value in required):
        logger.warning(
            f"persist_gate_receipt status=skip attempt={attempt_id!r} gate={gate_id!r} "
            "reason=incomplete-observations"
        )
        return None
    assert result.started_at is not None
    assert result.ended_at is not None
    assert result.duration_ms is not None
    assert result.runner_fingerprint is not None
    assert result.environment_fingerprint is not None
    assert result.full_log_ref is not None
    assert result.freshness_key is not None
    receipt_id = f"GR-{result.freshness_key[:32]}"
    receipt_path = store_path(_state_path(ctx, repo_root), StoreKind.GATE_RECEIPT)
    found, existing_id = _reuse_existing_gate_receipt(
        ctx,
        repo_root=repo_root,
        attempt=attempt,
        criterion_id=criterion_id,
        gate_id=gate_id,
        freshness_key=result.freshness_key,
        receipt_path=receipt_path,
        receipt_id=receipt_id,
    )
    if found:
        return existing_id
    source_log = execution_root / result.full_log_ref
    if not source_log.is_file():
        logger.warning(
            f"persist_gate_receipt status=skip attempt={attempt_id!r} gate={gate_id!r} "
            f"reason=proof-missing ref={result.full_log_ref!r}"
        )
        return None
    log_bytes = source_log.read_bytes()
    local_diagnostic = GateDiagnostic(
        id=diagnostic_id(receipt_id),
        receipt_id=receipt_id,
        attempt_id=attempt_id,
        scope_id=attempt.wave_id,
        criterion_id=criterion_id,
        gate_id=gate_id,
        captured_at=result.ended_at,
        argv=result.argv,
        command=result.command,
        details=result.details,
        stdout_tail=result.stdout_tail,
        stderr_tail=result.stderr_tail,
        source_log_ref=result.full_log_ref,
        log_digest=hashlib.sha256(log_bytes).hexdigest(),
        log_present=True,
    )
    write_gate_diagnostic(
        _state_path(ctx, repo_root),
        local_diagnostic,
        log_bytes=log_bytes,
    )
    receipt = GateReceipt(
        id=receipt_id,
        scope_id=attempt.wave_id,
        criterion_id=criterion_id,
        gate_id=gate_id,
        integration_id=attempt.integration_id,
        integrated_sha=attempt.integrated_sha,
        tree_sha=attempt.tree_sha,
        contract_digest=canonical_gate_digest(attempt.spec_digest),
        criteria_digest=canonical_gate_digest(attempt.criteria_digest),
        gate_manifest_digest=canonical_gate_digest(attempt.gate_manifest_digest),
        policy_digest=canonical_gate_digest(attempt.policy_digest),
        dependency_binding_digest=canonical_gate_digest(attempt.dependency_binding_digest),
        runner_environment_digest=canonical_gate_digest(attempt.runner_environment_digest),
        runner_digest=canonical_gate_digest(result.runner_fingerprint),
        environment_digest=canonical_gate_digest(result.environment_fingerprint),
        freshness_key=result.freshness_key,
        argv_digest=_digest(result.argv) if result.argv is not None else None,
        timeout_class=result.timeout_class,
        resolved_timeout_seconds=(
            float(result.resolved_timeout_seconds)
            if result.resolved_timeout_seconds is not None
            else None
        ),
        started_at=result.started_at,
        ended_at=result.ended_at,
        duration_ms=result.duration_ms,
        result=_gate_receipt_result(result),
        exit_status=result.exit_status,
        stdout_digest=(
            canonical_gate_digest(result.stdout_digest)
            if result.stdout_digest is not None
            else None
        ),
        stderr_digest=(
            canonical_gate_digest(result.stderr_digest)
            if result.stderr_digest is not None
            else None
        ),
        selected_file_digest=(
            canonical_gate_digest(result.selected_file_digest)
            if result.selected_file_digest is not None
            else None
        ),
        collected_nodeid_digest=(
            canonical_gate_digest(result.collected_nodeid_digest)
            if result.collected_nodeid_digest is not None
            else None
        ),
        residual_manifest_digest=(
            canonical_gate_digest(result.residual_manifest_digest)
            if result.residual_manifest_digest is not None
            else None
        ),
    )
    append_gate_receipt(_state_path(ctx, repo_root), receipt)
    row = _load_state(ctx, repo_root).close_attempts.get(attempt_id)
    if row is not None and receipt.id not in row.gate_receipt_ids:
        _commit_attempt(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt_id,
            updates={"gate_receipt_ids": [*row.gate_receipt_ids, receipt.id]},
            command="close.gate_receipt",
        )
    return receipt.id
