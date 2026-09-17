"""Durable evidence and store bindings for asynchronous Wave close."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
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


def anchor_state_path(ctx: MethodContext, repo_root: Path) -> Path:
    from eawf.surfaces.cli.scope import resolve_state_path

    if ctx.state_path is not None:
        configured = Path(ctx.state_path).resolve()
        if configured.parent.parent == repo_root:
            return configured
    return resolve_state_path(repo_root)


def _load_state(ctx: MethodContext, repo_root: Path) -> State:
    from eawf.runtime.daemon.methods.state_context import read_state

    state, _payload = read_state(anchor_state_path(ctx, repo_root))
    return state


def _digest(value: Any) -> str:
    return hashlib.sha256(orjson.dumps(value, option=orjson.OPT_SORT_KEYS)).hexdigest()


def commit_attempt(
    ctx: MethodContext,
    *,
    repo_root: Path,
    attempt_id: str,
    updates: dict[str, Any],
    command: str,
    append_gate_receipt_id: str | None = None,
) -> CloseAttempt:
    """Persist one immutable replacement of a durable close attempt.

    Args:
        ctx: Daemon method context anchoring the state write.
        repo_root: Repository whose close attempt is replaced.
        attempt_id: Id of the attempt to replace.
        updates: Field values written over the stored attempt payload.
        command: Event command name recorded for the write.
        append_gate_receipt_id: Receipt id to bind to the attempt. It is
            merged against the receipt list read under the state lock,
            never against the caller's own earlier read, so a rival
            receipt bound in between survives this write. Re-binding an
            already-bound id leaves the list unchanged.

    Returns:
        The attempt exactly as persisted.

    Raises:
        ValueError: No attempt with *attempt_id* exists.
    """
    from eawf.runtime.daemon.methods.state_worktree import commit_worktree_state

    holder: list[CloseAttempt] = []

    def _apply(state: State) -> dict[str, Any]:
        current = state.close_attempts.get(attempt_id)
        if current is None:
            raise ValueError(f"unknown close attempt: {attempt_id!r}")
        payload = current.model_dump(mode="json")
        payload.update(updates)
        if append_gate_receipt_id is not None:
            bound: list[Any] = list(payload["gate_receipt_ids"])
            if append_gate_receipt_id not in bound:
                bound.append(append_gate_receipt_id)
            payload["gate_receipt_ids"] = bound
        payload["updated_at"] = datetime.now(UTC)
        updated = CloseAttempt.model_validate(payload)
        state.close_attempts[attempt_id] = updated
        holder.append(updated)
        return {
            "attempt": updated.id,
            "wave": updated.wave_id,
            "status": updated.status.value,
        }

    commit_worktree_state(
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
        commit_attempt(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt.id,
            updates={},
            command="close.gate_receipt",
            append_gate_receipt_id=existing.id,
        )
    return True, existing.id


def reusable_pass_gate_ids(
    ctx: MethodContext,
    *,
    repo_root: Path,
    attempt_id: str,
) -> set[str]:
    """Return gates with pass receipts bound to the attempt's frozen inputs."""
    scrub_gate_receipt_store(anchor_state_path(ctx, repo_root))
    state = _load_state(ctx, repo_root)
    attempt = state.close_attempts.get(attempt_id)
    if attempt is None or not attempt.gate_receipt_ids:
        return set()
    wanted = set(attempt.gate_receipt_ids)
    path = store_path(anchor_state_path(ctx, repo_root), StoreKind.GATE_RECEIPT)
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


@dataclass(frozen=True)
class GateReceiptIdentity:
    """The immutable facts one gate receipt is bound to.

    Every field is a caller-owned fact about what was verified, never an
    observation of the run itself. A durable close fills it from its
    close attempt; a re-receipt of a closed wave fills it from the wave
    row and the landed commit. Keeping the two sources behind one shape
    is what lets both paths write byte-identical receipt structure.

    Attributes:
        scope_id: The wave the gate proves.
        criterion_id: The criterion the gate is bound to, if any.
        gate_id: The gate's own id.
        integration_id: The generation the run claims under. A re-run
            passes its binding id here, so its receipts never collide
            with the close-time ones.
        integrated_sha: The commit the gate ran against.
        tree_sha: That commit's tree.
        contract_digest: Digest of the wave revision under verification.
        criteria_digest: Digest of the wave's success criteria.
        gate_manifest_digest: Digest of the wave's gate manifest.
        policy_digest: Digest of the effective verification policy.
        dependency_binding_digest: Digest of the bound upstream
            generations.
        runner_environment_digest: Digest of the gate-runner sources and
            interpreter the run used.
    """

    scope_id: str
    criterion_id: str | None
    gate_id: str
    integration_id: str
    integrated_sha: str
    tree_sha: str
    contract_digest: str
    criteria_digest: str
    gate_manifest_digest: str
    policy_digest: str
    dependency_binding_digest: str
    runner_environment_digest: str


def build_gate_receipt(
    *,
    identity: GateReceiptIdentity,
    result: CheckResult,
) -> GateReceipt | None:
    """Return the durable receipt for one finished gate.

    Args:
        identity: The caller-owned facts the receipt binds to.
        result: The gate runner's terminal result.

    Returns:
        The validated receipt, or ``None`` when *result* is missing an
        observation the receipt requires -- a receipt is never invented
        from a partial run.
    """
    if (
        result.started_at is None
        or result.ended_at is None
        or result.duration_ms is None
        or result.runner_fingerprint is None
        or result.environment_fingerprint is None
        or result.freshness_key is None
    ):
        return None
    return GateReceipt(
        id=f"GR-{result.freshness_key[:32]}",
        scope_id=identity.scope_id,
        criterion_id=identity.criterion_id,
        gate_id=identity.gate_id,
        integration_id=identity.integration_id,
        integrated_sha=identity.integrated_sha,
        tree_sha=identity.tree_sha,
        contract_digest=canonical_gate_digest(identity.contract_digest),
        criteria_digest=canonical_gate_digest(identity.criteria_digest),
        gate_manifest_digest=canonical_gate_digest(identity.gate_manifest_digest),
        policy_digest=canonical_gate_digest(identity.policy_digest),
        dependency_binding_digest=canonical_gate_digest(identity.dependency_binding_digest),
        runner_environment_digest=canonical_gate_digest(identity.runner_environment_digest),
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
    scrub_gate_receipt_store(anchor_state_path(ctx, repo_root))
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
    receipt_path = store_path(anchor_state_path(ctx, repo_root), StoreKind.GATE_RECEIPT)
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
        anchor_state_path(ctx, repo_root),
        local_diagnostic,
        log_bytes=log_bytes,
    )
    receipt = build_gate_receipt(
        identity=GateReceiptIdentity(
            scope_id=attempt.wave_id,
            criterion_id=criterion_id,
            gate_id=gate_id,
            integration_id=attempt.integration_id,
            integrated_sha=attempt.integrated_sha,
            tree_sha=attempt.tree_sha,
            contract_digest=attempt.spec_digest,
            criteria_digest=attempt.criteria_digest,
            gate_manifest_digest=attempt.gate_manifest_digest,
            policy_digest=attempt.policy_digest,
            dependency_binding_digest=attempt.dependency_binding_digest,
            runner_environment_digest=attempt.runner_environment_digest,
        ),
        result=result,
    )
    if receipt is None:
        return None
    append_gate_receipt(anchor_state_path(ctx, repo_root), receipt)
    row = _load_state(ctx, repo_root).close_attempts.get(attempt_id)
    if row is not None and receipt.id not in row.gate_receipt_ids:
        commit_attempt(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt_id,
            updates={},
            command="close.gate_receipt",
            append_gate_receipt_id=receipt.id,
        )
    return receipt.id
