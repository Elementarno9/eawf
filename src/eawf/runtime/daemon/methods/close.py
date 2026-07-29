"""Durable asynchronous wave-close RPCs."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import platform
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf import __version__
from eawf.kernel.state.enums import (
    AuditRequirement,
    CloseAttemptStatus,
    CloseOperatorAction,
    WaveStatus,
)
from eawf.kernel.state.models import CloseAttempt, State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.runtime.daemon.close_workspace import (
    CloseWorkspaceError,
    cleanup_close_workspace,
    prepare_close_workspace,
)
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.close_evidence import (
    _commit_attempt as _commit_attempt,
)
from eawf.runtime.daemon.methods.close_evidence import (
    _digest as _digest,
)
from eawf.runtime.daemon.methods.close_evidence import (
    _load_state as _load_state,
)
from eawf.runtime.daemon.methods.close_evidence import (
    _state_path as _state_path,
)
from eawf.runtime.daemon.methods.close_evidence import (
    gate_freshness_inputs as gate_freshness_inputs,
)
from eawf.runtime.daemon.methods.close_evidence import (
    persist_gate_receipt as persist_gate_receipt,
)
from eawf.runtime.daemon.methods.close_evidence import (
    reusable_bound_audit_report_id as reusable_bound_audit_report_id,
)
from eawf.runtime.daemon.methods.close_evidence import (
    reusable_pass_gate_ids as reusable_pass_gate_ids,
)
from eawf.workflow.dispatch.verdict import verdict_requirement
from eawf.workflow.lifecycle.integration import (
    latest_wave_integration,
    mark_wave_integration_verified,
    require_land_dependencies,
)

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset(
    {
        CloseAttemptStatus.CLOSED,
        CloseAttemptStatus.BLOCKED,
        CloseAttemptStatus.STALE,
        CloseAttemptStatus.FAILED,
        CloseAttemptStatus.CANCELLED,
    }
)
_RESUMABLE_STATUSES = frozenset(
    {
        CloseAttemptStatus.QUEUED,
        CloseAttemptStatus.PREPARING,
        CloseAttemptStatus.CHECKING,
        CloseAttemptStatus.AUDITING,
        CloseAttemptStatus.READY,
        CloseAttemptStatus.APPLYING,
        CloseAttemptStatus.FAILED,
    }
)
_INTERRUPTED_STATUSES = _RESUMABLE_STATUSES - {CloseAttemptStatus.FAILED}
_CLOSE_TASKS: dict[tuple[Path, str], asyncio.Task[None]] = {}
_SHUTTING_DOWN: bool = False


class CloseSubmitParams(BaseModel):
    """Parameters for ``close.submit``."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str = Field(min_length=1)
    outcome: str = Field(min_length=1, max_length=1000)
    commit: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    commit_identity_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    tokens_consumed: int | None = Field(default=None, ge=0)
    repo_root: str | None = None
    no_runtime_waiver: bool = False


class CloseAttemptRefParams(BaseModel):
    """Attempt-or-wave reference used by close control RPCs."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(min_length=1)
    repo_root: str | None = None


class CloseCancelParams(CloseAttemptRefParams):
    """Parameters for ``close.cancel``."""

    reason: str | None = Field(default=None, max_length=500)


class CloseStatusResult(BaseModel):
    """Stable close-attempt response envelope."""

    model_config = ConfigDict(extra="forbid")

    attempt: dict[str, Any]
    backgrounded: bool


def _repo_root(ctx: MethodContext, explicit: str | None) -> Path:
    if explicit is not None:
        return Path(explicit).resolve()
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    return Path(ctx.state_path).resolve().parent.parent


def _close_task_key(repo_root: Path, attempt_id: str) -> tuple[Path, str]:
    """Return the process-local worker key for one repository attempt."""
    return repo_root.resolve(), attempt_id


def _policy_digest(
    state: State,
    *,
    repo_root: Path,
    wave_id: str,
) -> str:
    """Digest the canonical effective verification policy for one Wave."""
    from eawf.workflow.verify.readiness import (
        load_active_verify_block,
        resolve_wave_verify_block,
    )

    wave = state.waves.get(wave_id)
    if wave is None:
        raise ValueError(f"unknown wave: {wave_id!r}")
    verify_block = resolve_wave_verify_block(
        load_active_verify_block(
            wave_id,
            state,
            repo_root=repo_root,
            config_root=repo_root,
        ),
        wave,
    )
    return _digest(
        {"verify": (None if verify_block is None else verify_block.model_dump(mode="json"))}
    )


def _runner_environment_digest() -> str:
    from eawf.runtime.daemon import gate_execution
    from eawf.workflow.audit_dsl import models as gate_models
    from eawf.workflow.audit_dsl import registry as gate_registry
    from eawf.workflow.audit_dsl import runner as gate_runner

    runner_sources = {
        "gate_execution": Path(gate_execution.__file__),
        "models": Path(gate_models.__file__),
        "registry": Path(gate_registry.__file__),
        "runner": Path(gate_runner.__file__),
    }
    return _digest(
        {
            "eawf": __version__,
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": sys.platform,
            "gate_runner": {
                name: hashlib.sha256(path.read_bytes()).hexdigest()
                for name, path in sorted(runner_sources.items())
            },
        }
    )


def _dependency_binding_digest(state: State, *, wave_id: str) -> str:
    """Digest the exact upstream generations bound to one Wave."""
    rows = sorted(
        (
            binding.model_dump(mode="json")
            for binding in state.wave_dependency_bindings.values()
            if binding.wave_id == wave_id
        ),
        key=lambda row: (row["dep_wave_id"], row["integration_id"]),
    )
    return _digest(rows)


def _attempt_id(idempotency_key: str) -> str:
    return f"close-{hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]}"


def _latest_attempt_for_wave(state: State, wave_id: str) -> CloseAttempt | None:
    rows = [row for row in state.close_attempts.values() if row.wave_id == wave_id]
    if not rows:
        return None
    rows.sort(key=lambda row: (row.generation, row.requested_at, row.id))
    return rows[-1]


def _attempt_digest_mismatches(
    state: State,
    *,
    repo_root: Path,
    attempt: CloseAttempt,
) -> list[str]:
    """Compare every frozen content digest against current authority."""
    wave = state.waves[attempt.wave_id]
    active = latest_wave_integration(state, attempt.wave_id)
    comparisons = {
        "wave revision changed": (
            _digest(wave.model_dump(mode="json")),
            attempt.wave_revision_digest,
        ),
        "criteria changed": (
            _digest([criterion.model_dump(mode="json") for criterion in wave.success_criteria]),
            attempt.criteria_digest,
        ),
        "gate manifest changed": (
            _digest([gate.model_dump(mode="json") for gate in wave.gates]),
            attempt.gate_manifest_digest,
        ),
        "spec digest changed": (
            active.spec_digest if active is not None else "",
            attempt.spec_digest,
        ),
        "verification policy changed": (
            _policy_digest(
                state,
                repo_root=repo_root,
                wave_id=attempt.wave_id,
            ),
            attempt.policy_digest,
        ),
        "runner environment changed": (
            _runner_environment_digest(),
            attempt.runner_environment_digest,
        ),
        "dependency binding changed": (
            _dependency_binding_digest(state, wave_id=attempt.wave_id),
            attempt.dependency_binding_digest,
        ),
    }
    return [cause for cause, (current, expected) in comparisons.items() if current != expected]


def _attempt_invalidation_causes(
    state: State,
    *,
    repo_root: Path,
    attempt: CloseAttempt,
) -> list[str]:
    """Name every frozen close input that no longer matches live authority."""
    causes: list[str] = []
    wave = state.waves.get(attempt.wave_id)
    if wave is None:
        return [f"wave {attempt.wave_id!r} no longer exists"]
    active = latest_wave_integration(state, attempt.wave_id)
    if active is None or active.id != attempt.integration_id:
        causes.append("integration generation changed")
    elif active.integrated_sha != attempt.integrated_sha or active.tree_sha != attempt.tree_sha:
        causes.append("integrated revision changed")
    causes.extend(
        _attempt_digest_mismatches(
            state,
            repo_root=repo_root,
            attempt=attempt,
        )
    )
    try:
        require_land_dependencies(state, wave_id=attempt.wave_id)
    except ValueError as exc:
        causes.append(str(exc))
    return causes


def _resolve_attempt(state: State, ref: str) -> CloseAttempt:
    direct = state.close_attempts.get(ref)
    if direct is not None:
        return direct
    latest = _latest_attempt_for_wave(state, ref)
    if latest is not None:
        return latest
    raise ValueError(f"unknown close attempt or wave: {ref!r}")


def _attempt_payload(attempt: CloseAttempt) -> dict[str, Any]:
    return attempt.model_dump(mode="json")


def _create_attempt(
    ctx: MethodContext,
    *,
    repo_root: Path,
    args: CloseSubmitParams,
) -> CloseAttempt:
    from eawf.runtime.daemon.methods.state import _commit_worktree_state

    state = _load_state(ctx, repo_root)
    wave = state.waves.get(args.wave_id)
    if wave is None:
        raise ValueError(f"unknown wave: {args.wave_id!r}")
    if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
        raise ValueError(f"wave {args.wave_id!r} cannot start close (status={wave.status.value!r})")
    integration = latest_wave_integration(state, args.wave_id)
    if integration is None:
        raise ValueError(
            f"wave {args.wave_id!r} has no integration fact; land or explicitly adopt "
            "the exact integrated revision before close"
        )
    if args.commit is not None and args.commit != integration.integrated_sha:
        raise ValueError(
            f"close commit {args.commit!r} does not match active integration "
            f"{integration.integrated_sha!r}"
        )
    criteria_digest = _digest(
        [criterion.model_dump(mode="json") for criterion in wave.success_criteria]
    )
    wave_revision_digest = _digest(wave.model_dump(mode="json"))
    gate_manifest_digest = _digest([gate.model_dump(mode="json") for gate in wave.gates])
    policy_digest = _policy_digest(
        state,
        repo_root=repo_root,
        wave_id=args.wave_id,
    )
    runner_digest = _runner_environment_digest()
    dependency_binding_digest = _dependency_binding_digest(
        state,
        wave_id=args.wave_id,
    )
    from eawf.workflow.lifecycle.wave_sha import commit_identity_digest

    identity_digest = args.commit_identity_digest or commit_identity_digest(
        integration.integrated_sha,
        repo_root=repo_root,
    )
    idempotency_key = _digest(
        {
            "wave": args.wave_id,
            "integration": integration.id,
            "tree": integration.tree_sha,
            "spec": integration.spec_digest,
            "criteria": criteria_digest,
            "gates": gate_manifest_digest,
            "wave_revision": wave_revision_digest,
            "dependency_bindings": dependency_binding_digest,
            "policy": policy_digest,
            "runner": runner_digest,
            "outcome": args.outcome,
            "tokens_consumed": args.tokens_consumed,
            "commit_identity_digest": identity_digest,
            "no_runtime_waiver": args.no_runtime_waiver,
        }
    )
    existing = next(
        (
            row
            for row in state.close_attempts.values()
            if row.idempotency_key == idempotency_key
            and row.status is not CloseAttemptStatus.CANCELLED
        ),
        None,
    )
    if existing is not None:
        return existing
    previous = _latest_attempt_for_wave(state, args.wave_id)
    now = datetime.now(UTC)
    attempt = CloseAttempt(
        id=_attempt_id(idempotency_key),
        wave_id=args.wave_id,
        outcome=args.outcome,
        tokens_consumed=args.tokens_consumed,
        generation=1 if previous is None else previous.generation + 1,
        supersedes_id=previous.id if previous is not None else None,
        status=CloseAttemptStatus.QUEUED,
        integration_id=integration.id,
        candidate_sha=integration.candidate_sha,
        integrated_sha=integration.integrated_sha,
        commit_identity_digest=identity_digest,
        tree_sha=integration.tree_sha,
        wave_revision_digest=wave_revision_digest,
        spec_digest=integration.spec_digest,
        criteria_digest=criteria_digest,
        gate_manifest_digest=gate_manifest_digest,
        policy_digest=policy_digest,
        runner_environment_digest=runner_digest,
        dependency_binding_digest=dependency_binding_digest,
        required_gate_ids=[gate.id for gate in wave.gates],
        gate_receipt_ids=[],
        audit_requirement=(
            AuditRequirement.REQUIRED
            if verdict_requirement(wave) == "always"
            else AuditRequirement.NONE
        ),
        audit_report_id=None,
        no_runtime_waiver=args.no_runtime_waiver,
        repair_wave_id=None,
        repair_generation=None,
        repair_budget_remaining=1,
        infrastructure_retry_budget_remaining=1,
        required_operator_actions=[],
        waiver_decision_ids=[],
        usage_receipt_ids=[],
        artifact_refs=[],
        failure_kind=None,
        failure_detail_ref=None,
        invalidation_causes=[],
        requested_at=now,
        started_at=None,
        updated_at=now,
        terminal_at=None,
        idempotency_key=idempotency_key,
        apply_event_id=None,
    )

    def _apply(current: State) -> dict[str, Any]:
        duplicate = next(
            (
                row
                for row in current.close_attempts.values()
                if row.idempotency_key == idempotency_key
                and row.status is not CloseAttemptStatus.CANCELLED
            ),
            None,
        )
        chosen = duplicate or attempt
        current.close_attempts.setdefault(chosen.id, chosen)
        return {
            "attempt": chosen.id,
            "wave": chosen.wave_id,
            "status": chosen.status.value,
        }

    result = _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params={"wave_id": args.wave_id, "integration_id": integration.id},
        command="close.submit",
        scope_id=args.wave_id,
        apply_func=_apply,
    )
    return _resolve_attempt(_load_state(ctx, repo_root), str(result["attempt"]))


def _create_repair_attempt(
    ctx: MethodContext,
    *,
    repo_root: Path,
    blocked_attempt_id: str,
) -> CloseAttempt:
    """Create one linked repair generation without rewriting its BLOCKED parent."""
    from eawf.runtime.daemon.methods.state import _commit_worktree_state

    holder: list[CloseAttempt] = []

    def _apply(state: State) -> dict[str, Any]:
        source = state.close_attempts.get(blocked_attempt_id)
        if source is None:
            raise ValueError(f"unknown close attempt: {blocked_attempt_id!r}")
        existing = next(
            (
                row
                for row in state.close_attempts.values()
                if row.supersedes_id == source.id
                and row.wave_id == source.wave_id
                and row.repair_wave_id == source.wave_id
                and row.repair_generation == (source.repair_generation or 0) + 1
            ),
            None,
        )
        if existing is not None:
            holder.append(existing)
            return {
                "attempt": existing.id,
                "wave": existing.wave_id,
                "status": existing.status.value,
            }
        if source.status is not CloseAttemptStatus.BLOCKED:
            raise ValueError(
                f"close attempt {source.id!r} is not blocked (status={source.status.value!r})"
            )
        if source.repair_budget_remaining <= 0:
            actions = ", ".join(action.value for action in source.required_operator_actions)
            raise ValueError(
                f"close attempt {source.id!r} repair budget exhausted; "
                f"operator action required: {actions}"
            )
        latest = _latest_attempt_for_wave(state, source.wave_id)
        if latest is None or latest.id != source.id:
            raise ValueError(
                f"close attempt {source.id!r} is superseded by "
                f"{latest.id if latest is not None else 'unknown'!r}"
            )

        source_integration = state.wave_integrations.get(source.integration_id)
        repair_integration = latest_wave_integration(state, source.wave_id)
        if (
            source_integration is None
            or repair_integration is None
            or repair_integration.wave_id != source.wave_id
            or repair_integration.generation <= source_integration.generation
        ):
            raise ValueError(
                f"close attempt {source.id!r} has no newer repair integration; "
                "land a repair integration first"
            )

        wave = state.waves.get(source.wave_id)
        if wave is None:
            raise ValueError(f"unknown wave: {source.wave_id!r}")
        criteria_digest = _digest(
            [criterion.model_dump(mode="json") for criterion in wave.success_criteria]
        )
        gate_manifest_digest = _digest([gate.model_dump(mode="json") for gate in wave.gates])
        wave_revision_digest = _digest(wave.model_dump(mode="json"))
        policy_digest = _policy_digest(
            state,
            repo_root=repo_root,
            wave_id=source.wave_id,
        )
        runner_digest = _runner_environment_digest()
        dependency_binding_digest = _dependency_binding_digest(
            state,
            wave_id=source.wave_id,
        )
        repair_generation = (source.repair_generation or 0) + 1
        idempotency_key = _digest(
            {
                "kind": "close_repair",
                "supersedes_id": source.id,
                "repair_generation": repair_generation,
                "integration_id": repair_integration.id,
                "tree_sha": repair_integration.tree_sha,
                "spec_digest": repair_integration.spec_digest,
                "wave_revision_digest": wave_revision_digest,
                "criteria_digest": criteria_digest,
                "gate_manifest_digest": gate_manifest_digest,
                "policy_digest": policy_digest,
                "runner_environment_digest": runner_digest,
                "dependency_binding_digest": dependency_binding_digest,
            }
        )
        now = datetime.now(UTC)
        payload = source.model_dump(mode="json")
        payload.update(
            {
                "id": _attempt_id(idempotency_key),
                "generation": source.generation + 1,
                "supersedes_id": source.id,
                "status": CloseAttemptStatus.QUEUED,
                "integration_id": repair_integration.id,
                "candidate_sha": repair_integration.candidate_sha,
                "integrated_sha": repair_integration.integrated_sha,
                "tree_sha": repair_integration.tree_sha,
                "wave_revision_digest": wave_revision_digest,
                "spec_digest": repair_integration.spec_digest,
                "criteria_digest": criteria_digest,
                "gate_manifest_digest": gate_manifest_digest,
                "policy_digest": policy_digest,
                "runner_environment_digest": runner_digest,
                "dependency_binding_digest": dependency_binding_digest,
                "required_gate_ids": [gate.id for gate in wave.gates],
                "gate_receipt_ids": [],
                "audit_requirement": (
                    AuditRequirement.REQUIRED
                    if verdict_requirement(wave) == "always"
                    else AuditRequirement.NONE
                ),
                "audit_report_id": None,
                "repair_wave_id": source.wave_id,
                "repair_generation": repair_generation,
                "repair_budget_remaining": source.repair_budget_remaining - 1,
                "infrastructure_retry_budget_remaining": 1,
                "required_operator_actions": [],
                "waiver_decision_ids": [],
                "usage_receipt_ids": [],
                "artifact_refs": [],
                "failure_kind": None,
                "failure_detail_ref": None,
                "invalidation_causes": [],
                "requested_at": now,
                "started_at": None,
                "updated_at": now,
                "terminal_at": None,
                "idempotency_key": idempotency_key,
                "apply_event_id": None,
            }
        )
        repair = CloseAttempt.model_validate(payload)
        state.close_attempts[repair.id] = repair
        holder.append(repair)
        return {
            "attempt": repair.id,
            "wave": repair.wave_id,
            "status": repair.status.value,
        }

    _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params={"blocked_attempt_id": blocked_attempt_id},
        command="close.repair_generation",
        scope_id=blocked_attempt_id,
        apply_func=_apply,
    )
    return holder[0]


def _failure_ref(repo_root: Path, attempt_id: str, detail: str) -> str:
    relative = Path(".ea") / "local" / "close-logs" / f"{attempt_id}.log"
    path = repo_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(detail, encoding="utf-8")
    return relative.as_posix()


def _failure_status(detail: str) -> tuple[CloseAttemptStatus, str]:
    lowered = detail.lower()
    if "stale" in lowered or "tree mismatch" in lowered or "worktree is dirty" in lowered:
        return CloseAttemptStatus.STALE, "stale_input"
    if "validation_failed" in lowered or "blocked close" in lowered or "refused" in lowered:
        return CloseAttemptStatus.BLOCKED, "verification_blocked"
    return CloseAttemptStatus.FAILED, "infrastructure_failure"


def transition_attempt_stage(
    ctx: MethodContext,
    *,
    repo_root: Path,
    attempt_id: str,
    status: CloseAttemptStatus,
) -> CloseAttempt:
    """Persist one truthful close-worker stage transition."""
    return _commit_attempt(
        ctx,
        repo_root=repo_root,
        attempt_id=attempt_id,
        updates={"status": status},
        command="close.transition",
    )


def mark_attempt_ready(
    ctx: MethodContext,
    *,
    repo_root: Path,
    attempt_id: str,
    expected_wave_payload: dict[str, Any] | None,
) -> CloseAttempt:
    """CAS verified integration and READY attempt against the preflight wave."""
    from eawf.runtime.daemon.methods.state import _commit_worktree_state

    holder: list[CloseAttempt] = []

    def _apply(state: State) -> dict[str, Any]:
        attempt = state.close_attempts.get(attempt_id)
        if attempt is None:
            raise ValueError(f"unknown close attempt: {attempt_id!r}")
        wave = state.waves.get(attempt.wave_id)
        current_payload = wave.model_dump(mode="json") if wave is not None else None
        if current_payload != expected_wave_payload:
            raise ValueError(
                f"close attempt stale: wave {attempt.wave_id!r} changed during preflight"
            )
        causes = _attempt_invalidation_causes(
            state,
            repo_root=repo_root,
            attempt=attempt,
        )
        if causes:
            raise ValueError(f"close attempt stale: {'; '.join(causes)}")
        mark_wave_integration_verified(state, integration_id=attempt.integration_id)
        payload = attempt.model_dump(mode="json")
        payload.update(
            {
                "status": CloseAttemptStatus.READY,
                "updated_at": datetime.now(UTC),
            }
        )
        updated = CloseAttempt.model_validate(payload)
        state.close_attempts[attempt_id] = updated
        holder.append(updated)
        return {
            "attempt": updated.id,
            "wave": updated.wave_id,
            "status": updated.status.value,
            "integration": updated.integration_id,
        }

    _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params={"attempt_id": attempt_id},
        command="close.ready",
        scope_id=attempt_id,
        apply_func=_apply,
    )
    return holder[0]


async def _run_attempt(  # noqa: C901
    ctx: MethodContext,
    *,
    repo_root: Path,
    attempt_id: str,
) -> None:
    workspace_created = False
    infrastructure_retry_queued = False
    try:
        state = _load_state(ctx, repo_root)
        attempt = _resolve_attempt(state, attempt_id)
        if attempt.status in _TERMINAL_STATUSES:
            return
        wave = state.waves.get(attempt.wave_id)
        if wave is None:
            raise ValueError(f"unknown wave: {attempt.wave_id!r}")
        if wave.status is WaveStatus.CLOSED:
            await asyncio.to_thread(
                cleanup_close_workspace,
                repo_root,
                attempt_id=attempt.id,
            )
            _commit_attempt(
                ctx,
                repo_root=repo_root,
                attempt_id=attempt.id,
                updates={
                    "status": CloseAttemptStatus.CLOSED,
                    "terminal_at": datetime.now(UTC),
                },
                command="close.reconcile",
            )
            return
        causes = _attempt_invalidation_causes(
            state,
            repo_root=repo_root,
            attempt=attempt,
        )
        if causes:
            raise CloseWorkspaceError(f"close attempt stale: {'; '.join(causes)}")
        _commit_attempt(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt.id,
            updates={
                "status": CloseAttemptStatus.PREPARING,
                "started_at": attempt.started_at or datetime.now(UTC),
            },
            command="close.transition",
        )
        workspace = await asyncio.to_thread(
            prepare_close_workspace,
            repo_root,
            attempt_id=attempt.id,
            commit_ref=attempt.integrated_sha,
            expected_tree_sha=attempt.tree_sha,
        )
        workspace_created = True
        from eawf.runtime.daemon.methods.state import mutate as state_mutate

        mutation = Mutation(
            kind=MutationKind.WAVE_CLOSE,
            scope_id=attempt.wave_id,
            mutation_id=uuid.uuid4().hex,
            idempotency_key=attempt.idempotency_key,
            params={
                "wave_id": attempt.wave_id,
                "outcome": attempt.outcome,
                "commit": attempt.integrated_sha,
                "commit_identity_digest": attempt.commit_identity_digest,
                "tokens_consumed": attempt.tokens_consumed,
                "close_attempt_id": attempt.id,
                "verification_repo_root": str(workspace.path),
                "no_runtime_waiver": attempt.no_runtime_waiver,
            },
        )
        result = await state_mutate(
            ctx,
            {
                "mutation": mutation.model_dump(mode="json"),
                "repo_root": str(repo_root),
            },
        )
        event = result.get("event") or {}
        event_id = event.get("id")
        if workspace_created:
            await asyncio.to_thread(
                cleanup_close_workspace,
                repo_root,
                attempt_id=attempt.id,
            )
            workspace_created = False

        from eawf.runtime.daemon.methods.state import _commit_worktree_state

        def _finish(current: State) -> dict[str, Any]:
            row = current.close_attempts[attempt.id]
            payload = row.model_dump(mode="json")
            payload.update(
                {
                    "status": CloseAttemptStatus.CLOSED,
                    "apply_event_id": event_id,
                    "updated_at": datetime.now(UTC),
                    "terminal_at": datetime.now(UTC),
                }
            )
            current.close_attempts[attempt.id] = CloseAttempt.model_validate(payload)
            if attempt.integration_id in current.wave_integrations:
                mark_wave_integration_verified(
                    current,
                    integration_id=attempt.integration_id,
                )
            return {
                "attempt": attempt.id,
                "wave": attempt.wave_id,
                "status": CloseAttemptStatus.CLOSED.value,
            }

        _commit_worktree_state(
            ctx=ctx,
            repo_root=repo_root,
            params={"attempt_id": attempt.id, "apply_event_id": event_id},
            command="close.complete",
            scope_id=attempt.id,
            apply_func=_finish,
        )
    except asyncio.CancelledError:
        cancelled_status = (
            CloseAttemptStatus.QUEUED if _SHUTTING_DOWN else CloseAttemptStatus.CANCELLED
        )
        with contextlib.suppress(Exception):
            _commit_attempt(
                ctx,
                repo_root=repo_root,
                attempt_id=attempt_id,
                updates={
                    "status": cancelled_status,
                    "terminal_at": (None if _SHUTTING_DOWN else datetime.now(UTC)),
                    "failure_kind": ("daemon_shutdown" if _SHUTTING_DOWN else "operator_cancelled"),
                },
                command="close.interrupted" if _SHUTTING_DOWN else "close.cancelled",
            )
        raise
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc!s}"
        status, failure_kind = _failure_status(detail)
        failure_ref = _failure_ref(repo_root, attempt_id, detail)
        logger.exception(
            f"_run_attempt failure=exception attempt={attempt_id!r} "
            f"status={status.value!r} detail_ref={failure_ref!r}"
        )
        current_attempt = _load_state(ctx, repo_root).close_attempts.get(attempt_id)
        auto_retry = (
            status is CloseAttemptStatus.FAILED
            and current_attempt is not None
            and current_attempt.infrastructure_retry_budget_remaining > 0
            and not _SHUTTING_DOWN
        )
        terminal_status = CloseAttemptStatus.QUEUED if auto_retry else status
        required_operator_actions = (
            [
                CloseOperatorAction.SPLIT,
                CloseOperatorAction.DEFER,
                CloseOperatorAction.ABORT,
            ]
            if status is CloseAttemptStatus.BLOCKED
            and current_attempt is not None
            and current_attempt.repair_budget_remaining == 0
            else []
        )
        try:
            _commit_attempt(
                ctx,
                repo_root=repo_root,
                attempt_id=attempt_id,
                updates={
                    "status": terminal_status,
                    "failure_kind": ("infrastructure_retry" if auto_retry else failure_kind),
                    "failure_detail_ref": failure_ref,
                    "invalidation_causes": [detail] if status is CloseAttemptStatus.STALE else [],
                    "required_operator_actions": required_operator_actions,
                    "terminal_at": None if auto_retry else datetime.now(UTC),
                    "infrastructure_retry_budget_remaining": (
                        current_attempt.infrastructure_retry_budget_remaining - 1
                        if auto_retry and current_attempt is not None
                        else (
                            current_attempt.infrastructure_retry_budget_remaining
                            if current_attempt is not None
                            else 0
                        )
                    ),
                },
                command="close.failed",
            )
            infrastructure_retry_queued = auto_retry
        except Exception:
            logger.exception(f"_run_attempt status=terminal-persist-failed attempt={attempt_id!r}")
    finally:
        if workspace_created:
            with contextlib.suppress(CloseWorkspaceError):
                await asyncio.to_thread(
                    cleanup_close_workspace,
                    repo_root,
                    attempt_id=attempt_id,
                )
        task_key = _close_task_key(repo_root, attempt_id)
        current = _CLOSE_TASKS.get(task_key)
        if current is asyncio.current_task():
            _CLOSE_TASKS.pop(task_key, None)
        if infrastructure_retry_queued:
            _schedule(
                ctx,
                repo_root=repo_root,
                attempt_id=attempt_id,
            )


def _schedule(
    ctx: MethodContext,
    *,
    repo_root: Path,
    attempt_id: str,
) -> bool:
    task_key = _close_task_key(repo_root, attempt_id)
    existing = _CLOSE_TASKS.get(task_key)
    if existing is not None and not existing.done():
        return False
    task = asyncio.create_task(
        _run_attempt(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt_id,
        ),
        name=f"close-{attempt_id}-{hashlib.sha256(str(task_key[0]).encode()).hexdigest()[:8]}",
    )
    _CLOSE_TASKS[task_key] = task
    return True


@register("close.submit")
async def submit(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Persist and schedule an idempotent exact-revision close attempt."""
    args = CloseSubmitParams.model_validate(params)
    repo_root = _repo_root(ctx, args.repo_root)
    attempt = _create_attempt(ctx, repo_root=repo_root, args=args)
    backgrounded = False
    if attempt.status not in _TERMINAL_STATUSES:
        backgrounded = _schedule(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt.id,
        )
    return CloseStatusResult(
        attempt=_attempt_payload(attempt),
        backgrounded=backgrounded,
    ).model_dump(mode="json")


@register("close.status")
async def status(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return durable status by attempt id or latest attempt for a wave."""
    args = CloseAttemptRefParams.model_validate(params)
    repo_root = _repo_root(ctx, args.repo_root)
    attempt = _resolve_attempt(_load_state(ctx, repo_root), args.ref)
    task = _CLOSE_TASKS.get(_close_task_key(repo_root, attempt.id))
    return CloseStatusResult(
        attempt=_attempt_payload(attempt),
        backgrounded=task is not None and not task.done(),
    ).model_dump(mode="json")


@register("close.resume")
async def resume(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Resume one interrupted/infrastructure-failed close attempt."""
    args = CloseAttemptRefParams.model_validate(params)
    repo_root = _repo_root(ctx, args.repo_root)
    attempt = _resolve_attempt(_load_state(ctx, repo_root), args.ref)
    blocked_retry = (
        attempt.status is CloseAttemptStatus.BLOCKED and attempt.repair_budget_remaining > 0
    )
    if attempt.status is CloseAttemptStatus.BLOCKED and attempt.repair_budget_remaining == 0:
        actions = ", ".join(action.value for action in attempt.required_operator_actions)
        raise ValueError(
            f"close attempt {attempt.id!r} repair budget exhausted; "
            f"operator action required: {actions}"
        )
    if attempt.status not in _RESUMABLE_STATUSES and not blocked_retry:
        raise ValueError(
            f"close attempt {attempt.id!r} is not resumable (status={attempt.status.value!r})"
        )
    if blocked_retry:
        attempt = _create_repair_attempt(
            ctx,
            repo_root=repo_root,
            blocked_attempt_id=attempt.id,
        )
    else:
        attempt = _commit_attempt(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt.id,
            updates={
                "status": CloseAttemptStatus.QUEUED,
                "failure_kind": None,
                "failure_detail_ref": None,
                "terminal_at": None,
            },
            command="close.resume",
        )
    backgrounded = False
    if attempt.status not in _TERMINAL_STATUSES:
        backgrounded = _schedule(ctx, repo_root=repo_root, attempt_id=attempt.id)
    return CloseStatusResult(
        attempt=_attempt_payload(attempt),
        backgrounded=backgrounded,
    ).model_dump(mode="json")


@register("close.cancel")
async def cancel(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Cancel a non-applying close attempt."""
    args = CloseCancelParams.model_validate(params)
    repo_root = _repo_root(ctx, args.repo_root)
    attempt = _resolve_attempt(_load_state(ctx, repo_root), args.ref)
    if attempt.status is CloseAttemptStatus.APPLYING:
        raise ValueError(f"close attempt {attempt.id!r} is applying and cannot be cancelled")
    if attempt.status in _TERMINAL_STATUSES:
        return CloseStatusResult(
            attempt=_attempt_payload(attempt),
            backgrounded=False,
        ).model_dump(mode="json")
    task = _CLOSE_TASKS.get(_close_task_key(repo_root, attempt.id))
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    attempt = _resolve_attempt(_load_state(ctx, repo_root), attempt.id)
    if attempt.status is not CloseAttemptStatus.CANCELLED:
        attempt = _commit_attempt(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt.id,
            updates={
                "status": CloseAttemptStatus.CANCELLED,
                "failure_kind": "operator_cancelled",
                "failure_detail_ref": (
                    _failure_ref(repo_root, attempt.id, args.reason) if args.reason else None
                ),
                "terminal_at": datetime.now(UTC),
            },
            command="close.cancel",
        )
    return CloseStatusResult(
        attempt=_attempt_payload(attempt),
        backgrounded=False,
    ).model_dump(mode="json")


def resume_durable_close_attempts(ctx: MethodContext) -> int:
    """Schedule unfinished attempts after daemon restart."""
    if ctx.state_path is None:
        return 0
    state_path = Path(ctx.state_path)
    if not state_path.exists():
        logger.info(
            f"resume_durable_close_attempts state-absent path={state_path.name!r} resumed=0"
        )
        return 0
    repo_root = state_path.resolve().parent.parent
    state = _load_state(ctx, repo_root)
    resumed = 0
    for attempt in state.close_attempts.values():
        if attempt.status not in _INTERRUPTED_STATUSES:
            continue
        if attempt.status is not CloseAttemptStatus.QUEUED:
            _commit_attempt(
                ctx,
                repo_root=repo_root,
                attempt_id=attempt.id,
                updates={
                    "status": CloseAttemptStatus.QUEUED,
                    "terminal_at": None,
                    "failure_kind": "daemon_restart_resume",
                },
                command="close.restart_resume",
            )
        if _schedule(
            ctx,
            repo_root=repo_root,
            attempt_id=attempt.id,
        ):
            resumed += 1
    logger.info(f"resume_durable_close_attempts resumed={resumed}")
    return resumed


async def shutdown_close_attempts() -> None:
    """Cancel process-local workers; durable rows remain resumable."""
    global _SHUTTING_DOWN

    _SHUTTING_DOWN = True
    tasks = [task for task in _CLOSE_TASKS.values() if not task.done()]
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


__all__ = [
    "cancel",
    "resume",
    "resume_durable_close_attempts",
    "shutdown_close_attempts",
    "status",
    "submit",
]
