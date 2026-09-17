"""``close.rereceipt`` JSON-RPC method: re-run a CLOSED wave's gates.

Waves have closed carrying required gates and zero receipts, and until now
no verb could go back and produce the missing proof: ``close.submit``
refuses a wave that is not CLAIMED or IN_PROGRESS, and there is no
wave-level reopen. The record simply stayed empty.

This is the narrow repair. The wave row is read, never written: its gates
are replayed in a detached workspace at the commit the wave landed on, one
receipt is persisted per gate, and one append-only ``gate_rereceipt`` row
binds the run to the wave, the landed commit and both manifest digests.
The whole wave row is fingerprinted before the gates run and re-checked
after (:func:`~eawf.workflow.lifecycle.gate_repoint.frozen_wave_fingerprint`);
a wave that moved meanwhile gets no binding at all, so the row can never
describe a verification contract that no longer exists.

A failing gate is evidence too. It lands as a ``fail`` receipt inside the
binding and the wave stays CLOSED: this verb adds proof, it never revises
a recorded verdict.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.state.enums import GateReceiptResult, StoreKind, WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.gate_rereceipt import (
    GateRereceiptBinding,
    GateRereceiptOutcome,
)
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.close_workspace import (
    CloseWorkspaceError,
    cleanup_close_workspace,
    prepare_close_workspace,
)
from eawf.runtime.daemon.gate_receipt_hygiene import (
    append_gate_receipt,
    diagnostic_id,
    write_gate_diagnostic,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.close_evidence import (
    GateReceiptIdentity,
    anchor_state_path,
    build_gate_receipt,
)
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec, GateFreshnessInput
from eawf.workflow.lifecycle.gate_repoint import frozen_wave_fingerprint

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS = 30.0


class CloseRereceiptParams(BaseModel):
    """Params for :func:`rereceipt`.

    Attributes:
        wave_id: The CLOSED wave whose recorded gates are replayed.
        repo_root: Optional absolute repo working-tree path (default: the
            daemon's own anchor).
    """

    model_config = ConfigDict(extra="forbid")

    wave_id: str = Field(min_length=1)
    repo_root: str | None = None


class CloseRereceiptResult(BaseModel):
    """Result shape for the :func:`rereceipt` RPC."""

    model_config = ConfigDict(extra="forbid")

    operation: str
    wave_id: str
    landed_sha: str
    binding_id: str
    receipt_ids: list[str]
    gates: list[dict[str, Any]]
    passed_count: int
    failed_count: int


@dataclass(frozen=True)
class _WaveVerificationFacts:
    """The digests a re-receipt binds its receipts to.

    Derived from the wave row and the repository rather than from a close
    attempt, because a re-receipt exists precisely where no usable attempt
    was ever recorded.
    """

    contract_digest: str
    criteria_digest: str
    gate_manifest_digest: str
    policy_digest: str
    dependency_binding_digest: str
    runner_environment_digest: str


def _digest(value: Any) -> str:
    """Return the canonical sorted-key digest of a JSON-safe value."""
    return hashlib.sha256(orjson.dumps(value, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _require_closed_wave(state: State, *, wave_id: str) -> Wave:
    """Return the wave a re-receipt may replay.

    Args:
        state: Loaded state.
        wave_id: The requested wave.

    Returns:
        The CLOSED wave row carrying at least one gate.

    Raises:
        DaemonValidationError: The wave is unknown, is not CLOSED, or
            records no gates to re-run.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise DaemonValidationError(f"validation_failed: unknown wave: {wave_id!r}")
    if wave.status != WaveStatus.CLOSED:
        raise DaemonValidationError(
            f"validation_failed: wave {wave_id!r} is not closed "
            f"(status={wave.status.value!r}); a re-receipt replays a closed wave's record"
        )
    if not wave.gates:
        raise DaemonValidationError(
            f"validation_failed: wave {wave_id!r} records no gates to re-run"
        )
    return wave


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one bounded read-only Git command against *repo_root*."""
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )


def _require_landed_revision(repo_root: Path, *, wave: Wave) -> tuple[str, str]:
    """Return the landed commit and tree a re-receipt must run at.

    The commit must be reachable from ``HEAD``: receipts bound to a commit
    off the current history would point at work that never landed, which
    is worse than having no receipt at all.

    Args:
        repo_root: Repository holding the landed history.
        wave: The closed wave carrying the pin.

    Returns:
        ``(commit_sha, tree_sha)`` for the landed revision.

    Raises:
        DaemonValidationError: The wave carries no pin, the pin does not
            resolve, or it is not an ancestor of ``HEAD``.
    """
    if wave.commit is None:
        raise DaemonValidationError(
            f"validation_failed: wave {wave.id!r} has no landed commit to re-run its gates at"
        )
    resolved = _git(repo_root, "rev-parse", "--verify", f"{wave.commit}^{{commit}}")
    if resolved.returncode != 0:
        raise DaemonValidationError(
            f"validation_failed: wave {wave.id!r} commit {wave.commit!r} does not resolve "
            "in this repository"
        )
    commit_sha = resolved.stdout.strip()
    ancestry = _git(repo_root, "merge-base", "--is-ancestor", commit_sha, "HEAD")
    if ancestry.returncode != 0:
        raise DaemonValidationError(
            f"validation_failed: wave {wave.id!r} commit {commit_sha} is not an ancestor "
            "of HEAD; a receipt bound to unreachable history proves nothing"
        )
    tree = _git(repo_root, "rev-parse", "--verify", f"{commit_sha}^{{tree}}")
    if tree.returncode != 0:
        raise DaemonValidationError(
            f"validation_failed: wave {wave.id!r} commit {commit_sha} has no resolvable tree"
        )
    return commit_sha, tree.stdout.strip()


def _verification_facts(
    state: State,
    *,
    repo_root: Path,
    wave: Wave,
) -> _WaveVerificationFacts:
    """Derive the digests the re-run's receipts bind to.

    The policy, dependency-binding and runner digests come from the close
    path's own derivations so a re-receipt and a close compute them the
    same way instead of drifting apart.
    """
    from eawf.runtime.daemon.methods.close import (
        dependency_binding_digest,
        policy_digest,
        runner_environment_digest,
    )

    return _WaveVerificationFacts(
        contract_digest=_digest(wave.model_dump(mode="json")),
        criteria_digest=_digest(
            [criterion.model_dump(mode="json") for criterion in wave.success_criteria]
        ),
        gate_manifest_digest=_digest([gate.model_dump(mode="json") for gate in wave.gates]),
        policy_digest=policy_digest(state, repo_root=repo_root, wave_id=wave.id),
        dependency_binding_digest=dependency_binding_digest(state, wave_id=wave.id),
        runner_environment_digest=runner_environment_digest(),
    )


def _gate_freshness(
    *,
    wave: Wave,
    gate: GateSpec,
    binding_id: str,
    landed_sha: str,
    landed_tree_sha: str,
    facts: _WaveVerificationFacts,
) -> GateFreshnessInput:
    """Return the frozen facts one re-run gate claims and receipts under.

    *binding_id* stands in for the integration generation, so a re-run's
    freshness key -- and therefore its receipt id -- can never collide
    with the close-time one or with an earlier re-run's.
    """
    log_name = f"gate-{hashlib.sha256(gate.id.encode()).hexdigest()[:16]}.log"
    return GateFreshnessInput(
        scope_id=wave.id,
        criterion_id=gate.criterion_id,
        integration_id=binding_id,
        integrated_commit=landed_sha,
        tree_digest=landed_tree_sha,
        contract_digest=facts.contract_digest,
        criteria_digest=facts.criteria_digest,
        gate_manifest_digest=facts.gate_manifest_digest,
        policy_digest=facts.policy_digest,
        dependency_binding_digest=facts.dependency_binding_digest,
        runner_environment_digest=facts.runner_environment_digest,
        full_log_ref=(
            Path(".ea") / "local" / "gate-diagnostics" / "incoming" / binding_id / log_name
        ).as_posix(),
    )


def _run_gate_child(
    *,
    compiled: CheckSpec,
    state_path: Path,
    workspace_path: Path,
    binding_id: str,
    criterion_id: str,
    gate_id: str,
) -> CheckResult | None:
    """Run one compiled gate in a child interpreter, or report nothing.

    A child that crashed or reported a typed fault proved nothing about
    the wave, so it yields ``None`` rather than a result that would
    persist as evidence.
    """
    from eawf.runtime.daemon.gate_execution import (
        GateChildCrashError,
        GateExecutionContext,
        run_gate_out_of_process,
    )

    try:
        return run_gate_out_of_process(
            compiled,
            cwd=workspace_path,
            context=GateExecutionContext(state_path=state_path, attempt_id=binding_id),
            criterion_id=criterion_id,
            gate_id=gate_id,
        )
    except (GateChildCrashError, ValueError) as exc:
        logger.warning(
            f"close_rereceipt gate={gate_id!r} status='blocked' runner='out-of-process' "
            f"detail={exc!s}"
        )
        return None


def _persist_rereceipt(
    *,
    state_path: Path,
    execution_root: Path,
    identity: GateReceiptIdentity,
    binding_id: str,
    result: CheckResult,
) -> GateRereceiptOutcome:
    """Persist one re-run gate's durable receipt and local diagnostic."""
    receipt = build_gate_receipt(identity=identity, result=result)
    if receipt is None:
        logger.warning(
            f"close_rereceipt gate={identity.gate_id!r} status='blocked' "
            "reason=incomplete-observations"
        )
        return GateRereceiptOutcome(
            gate_id=identity.gate_id,
            criterion_id=identity.criterion_id,
            result=GateReceiptResult.BLOCKED,
            receipt_id=None,
        )
    _write_diagnostic(
        state_path=state_path,
        execution_root=execution_root,
        receipt_id=receipt.id,
        binding_id=binding_id,
        identity=identity,
        result=result,
    )
    append_gate_receipt(state_path, receipt)
    return GateRereceiptOutcome(
        gate_id=identity.gate_id,
        criterion_id=identity.criterion_id,
        result=receipt.result,
        receipt_id=receipt.id,
    )


def _write_diagnostic(
    *,
    state_path: Path,
    execution_root: Path,
    receipt_id: str,
    binding_id: str,
    identity: GateReceiptIdentity,
    result: CheckResult,
) -> None:
    """Store the run's raw observations beside the committed receipt.

    Best-effort: the committed receipt carries no output, so a missing
    proof log costs triage detail but never the evidence itself.
    """
    from eawf.kernel.store.kinds.gate_receipt import GateDiagnostic

    source = execution_root / result.full_log_ref if result.full_log_ref else None
    log_bytes = source.read_bytes() if source is not None and source.is_file() else None
    write_gate_diagnostic(
        state_path,
        GateDiagnostic(
            id=diagnostic_id(receipt_id),
            receipt_id=receipt_id,
            attempt_id=binding_id,
            scope_id=identity.scope_id,
            criterion_id=identity.criterion_id,
            gate_id=identity.gate_id,
            captured_at=result.ended_at or datetime.now(UTC),
            argv=result.argv,
            command=result.command,
            details=result.details,
            stdout_tail=result.stdout_tail,
            stderr_tail=result.stderr_tail,
            source_log_ref=result.full_log_ref,
            log_digest=(hashlib.sha256(log_bytes).hexdigest() if log_bytes is not None else None),
            log_present=log_bytes is not None,
        ),
        log_bytes=log_bytes,
    )


def _run_wave_gates(
    *,
    repo_root: Path,
    state_path: Path,
    wave: Wave,
    binding_id: str,
    landed_sha: str,
    landed_tree_sha: str,
    facts: _WaveVerificationFacts,
) -> list[GateRereceiptOutcome]:
    """Replay every recorded gate at the landed revision.

    Args:
        repo_root: Repository owning the landed history.
        state_path: Anchor for the receipt store and gate claims.
        wave: The CLOSED wave being re-receipted.
        binding_id: Id of the binding row this run will write.
        landed_sha: The commit the gates run at.
        landed_tree_sha: That commit's tree.
        facts: The digests every produced receipt binds to.

    Returns:
        One outcome per recorded gate, in the wave's gate order.

    Raises:
        DaemonValidationError: The exact-revision workspace could not be
            prepared.
    """
    criteria = {criterion.id: criterion for criterion in wave.success_criteria}
    try:
        workspace = prepare_close_workspace(
            repo_root,
            attempt_id=binding_id,
            commit_ref=landed_sha,
            expected_tree_sha=landed_tree_sha,
        )
    except CloseWorkspaceError as exc:
        raise DaemonValidationError(
            f"validation_failed: wave {wave.id!r} re-receipt workspace refused: {exc}"
        ) from exc
    try:
        return [
            _rerun_gate_row(
                gate=gate,
                criterion=criteria.get(gate.criterion_id or ""),
                state_path=state_path,
                workspace_path=workspace.path,
                wave=wave,
                binding_id=binding_id,
                landed_sha=landed_sha,
                landed_tree_sha=landed_tree_sha,
                facts=facts,
            )
            for gate in wave.gates
        ]
    finally:
        cleanup_close_workspace(repo_root, attempt_id=binding_id)


def _rerun_gate_row(
    *,
    gate: GateSpec,
    criterion: CriterionSpec | None,
    state_path: Path,
    workspace_path: Path,
    wave: Wave,
    binding_id: str,
    landed_sha: str,
    landed_tree_sha: str,
    facts: _WaveVerificationFacts,
) -> GateRereceiptOutcome:
    """Replay one recorded gate and turn its result into a binding row entry."""
    from eawf.workflow.verify.compile import compile_gate

    identity = GateReceiptIdentity(
        scope_id=wave.id,
        criterion_id=gate.criterion_id,
        gate_id=gate.id,
        integration_id=binding_id,
        integrated_sha=landed_sha,
        tree_sha=landed_tree_sha,
        contract_digest=facts.contract_digest,
        criteria_digest=facts.criteria_digest,
        gate_manifest_digest=facts.gate_manifest_digest,
        policy_digest=facts.policy_digest,
        dependency_binding_digest=facts.dependency_binding_digest,
        runner_environment_digest=facts.runner_environment_digest,
    )
    blocked = GateRereceiptOutcome(
        gate_id=gate.id,
        criterion_id=gate.criterion_id,
        result=GateReceiptResult.BLOCKED,
        receipt_id=None,
    )
    if criterion is None:
        logger.warning(f"close_rereceipt gate={gate.id!r} status='blocked' reason=no-criterion")
        return blocked
    compiled = compile_gate(
        gate,
        criterion=criterion,
        freshness=_gate_freshness(
            wave=wave,
            gate=gate,
            binding_id=binding_id,
            landed_sha=landed_sha,
            landed_tree_sha=landed_tree_sha,
            facts=facts,
        ),
    )
    if compiled is None:
        logger.warning(f"close_rereceipt gate={gate.id!r} status='blocked' reason=not-runnable")
        return blocked
    result = _run_gate_child(
        compiled=compiled,
        state_path=state_path,
        workspace_path=workspace_path,
        binding_id=binding_id,
        criterion_id=criterion.id,
        gate_id=gate.id,
    )
    if result is None:
        return blocked
    return _persist_rereceipt(
        state_path=state_path,
        execution_root=workspace_path,
        identity=identity,
        binding_id=binding_id,
        result=result,
    )


def _binding_envelope(binding: GateRereceiptBinding) -> Envelope:
    """Wrap one binding row in its canonical store envelope."""
    passed = sum(1 for gate in binding.gates if gate.result == GateReceiptResult.PASS)
    return Envelope(
        id=binding.id,
        kind=StoreKind.GATE_RERECEIPT,
        scope_id=binding.wave_id,
        created_at=binding.ran_at,
        summary=(
            f"re-receipt {binding.wave_id} at {binding.landed_sha[:12]} "
            f"passed={passed}/{len(binding.gates)}"
        ),
        payload=binding.model_dump(mode="json"),
    )


def _build_binding(
    *,
    binding_id: str,
    wave: Wave,
    landed_sha: str,
    landed_tree_sha: str,
    facts: _WaveVerificationFacts,
    outcomes: list[GateRereceiptOutcome],
) -> GateRereceiptBinding:
    """Assemble the append-only row that binds this run to the wave."""
    return GateRereceiptBinding(
        id=binding_id,
        wave_id=wave.id,
        landed_sha=landed_sha,
        landed_tree_sha=landed_tree_sha,
        criteria_digest=facts.criteria_digest,
        gate_manifest_digest=facts.gate_manifest_digest,
        wave_fingerprint_digest=_digest(frozen_wave_fingerprint(wave)),
        receipt_ids=[gate.receipt_id for gate in outcomes if gate.receipt_id is not None],
        gates=outcomes,
        ran_at=datetime.now(UTC),
    )


def _require_unmoved_wave(state_path: Path, *, wave_id: str, fingerprint: dict[str, Any]) -> Wave:
    """Return the wave row, refusing when it moved while the gates ran.

    Raises:
        DaemonValidationError: The wave vanished or any part of its row
            outside gate argv changed, so no binding is written.
    """
    from eawf.runtime.daemon.methods.state_context import read_state

    state, _payload = read_state(state_path)
    wave = state.waves.get(wave_id)
    if wave is None or frozen_wave_fingerprint(wave) != fingerprint:
        raise DaemonValidationError(
            f"validation_failed: wave {wave_id!r} changed while its gates re-ran; "
            "no re-receipt was bound"
        )
    return wave


@register("close.rereceipt")
async def rereceipt(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Re-run a CLOSED wave's recorded gates and bind the receipts.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`CloseRereceiptParams`.

    Returns:
        Dict matching :class:`CloseRereceiptResult`.

    Raises:
        ValueError: The params do not validate (mapped to ``-32602``).
        DaemonValidationError: The wave is unknown, not CLOSED, carries no
            gates, has no landed commit reachable from ``HEAD``, or moved
            while its gates ran (mapped to ``-32002``).
    """
    from eawf.runtime.daemon.methods.close import resolve_repo_root
    from eawf.runtime.daemon.methods.state_context import read_state

    try:
        args = CloseRereceiptParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    repo_root = resolve_repo_root(ctx, args.repo_root)
    state_path = anchor_state_path(ctx, repo_root)
    state, _payload = read_state(state_path)
    wave = _require_closed_wave(state, wave_id=args.wave_id)
    landed_sha, landed_tree_sha = _require_landed_revision(repo_root, wave=wave)
    fingerprint = frozen_wave_fingerprint(wave)
    facts = _verification_facts(state, repo_root=repo_root, wave=wave)
    binding_id = f"GRR-{uuid.uuid4().hex[:12]}"

    outcomes = await asyncio.to_thread(
        _run_wave_gates,
        repo_root=repo_root,
        state_path=state_path,
        wave=wave,
        binding_id=binding_id,
        landed_sha=landed_sha,
        landed_tree_sha=landed_tree_sha,
        facts=facts,
    )
    bound_wave = _require_unmoved_wave(state_path, wave_id=args.wave_id, fingerprint=fingerprint)
    binding = _build_binding(
        binding_id=binding_id,
        wave=bound_wave,
        landed_sha=landed_sha,
        landed_tree_sha=landed_tree_sha,
        facts=facts,
        outcomes=outcomes,
    )
    append_envelope(store_path(state_path, StoreKind.GATE_RERECEIPT), _binding_envelope(binding))
    passed = sum(1 for gate in outcomes if gate.result == GateReceiptResult.PASS)
    logger.info(
        f"close_rereceipt ok wave={args.wave_id} binding={binding_id} "
        f"commit={landed_sha} passed={passed} gates={len(outcomes)}"
    )
    return CloseRereceiptResult(
        operation="rereceipt",
        wave_id=args.wave_id,
        landed_sha=landed_sha,
        binding_id=binding_id,
        receipt_ids=list(binding.receipt_ids),
        gates=[gate.model_dump(mode="json") for gate in outcomes],
        passed_count=passed,
        failed_count=len(outcomes) - passed,
    ).model_dump(mode="json")


__all__ = [
    "CloseRereceiptParams",
    "CloseRereceiptResult",
    "rereceipt",
]
