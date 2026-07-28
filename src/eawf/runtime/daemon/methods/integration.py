"""Daemon-owned Wave integration and dependency-barrier mutations."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import DependencyStage, WaveIntegrationKind, WaveStatus
from eawf.kernel.state.models import (
    State,
    WaveDependencyBarrier,
    wave_dependency_key,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.workflow.lifecycle.integration import (
    create_wave_integration,
    digest_wave_contract,
)

_GIT_TIMEOUT_SECONDS = 30.0


class IntegrationAdoptParams(BaseModel):
    """Parameters for ``integration.adopt``."""

    model_config = ConfigDict(extra="forbid")

    repo_root: str = Field(min_length=1)
    wave_id: str = Field(min_length=1)
    commit: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


class DependencyBarrierSetParams(BaseModel):
    """Parameters for ``dependency_barrier.set``."""

    model_config = ConfigDict(extra="forbid")

    repo_root: str = Field(min_length=1)
    wave_id: str = Field(min_length=1)
    dep_wave_id: str = Field(min_length=1)
    start_after: DependencyStage
    land_after: DependencyStage
    reason: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class _AdoptionFacts:
    """Git facts pinned by one explicit adoption."""

    base_sha: str
    candidate_sha: str
    integrated_sha: str
    tree_sha: str
    diff_digest: str


def _git(
    repo_root: Path,
    *args: str,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[bytes]:
    """Run one bounded Git query without invoking a shell."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        check=False,
        timeout=_GIT_TIMEOUT_SECONDS,
    )
    if result.returncode not in allowed_returncodes:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ValueError(f"git verification failed: {detail or 'unknown error'}")
    return result


def _stdout(result: subprocess.CompletedProcess[bytes]) -> str:
    """Decode and trim one Git query result."""
    return result.stdout.decode("utf-8", errors="strict").strip()


def _verify_adopted_commit(repo_root: Path, commit_ref: str) -> _AdoptionFacts:
    """Resolve and prove an already-integrated commit against current ``HEAD``."""
    resolved = _stdout(
        _git(
            repo_root,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{commit_ref}^{{commit}}",
        )
    )
    ancestry = _git(
        repo_root,
        "merge-base",
        "--is-ancestor",
        resolved,
        "HEAD",
        allowed_returncodes=frozenset({0, 1}),
    )
    if ancestry.returncode != 0:
        raise ValueError(f"commit {commit_ref!r} is not integrated into current HEAD")
    tree_sha = _stdout(_git(repo_root, "rev-parse", "--verify", f"{resolved}^{{tree}}"))
    _git(repo_root, "cat-file", "-e", f"{tree_sha}^{{tree}}")
    lineage = _stdout(_git(repo_root, "rev-list", "--parents", "-n", "1", resolved)).split()
    base_sha = lineage[1] if len(lineage) > 1 else resolved
    diff = _git(repo_root, "diff", "--binary", base_sha, resolved).stdout
    return _AdoptionFacts(
        base_sha=base_sha,
        candidate_sha=resolved,
        integrated_sha=resolved,
        tree_sha=tree_sha,
        diff_digest=hashlib.sha256(diff).hexdigest(),
    )


@register("integration.adopt")
async def adopt(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Persist an explicit, ancestry-verified ADOPT integration fact."""
    from eawf.runtime.daemon.methods.state import _commit_worktree_state

    args = IntegrationAdoptParams.model_validate(params)
    repo_root = Path(args.repo_root).resolve()
    facts = _verify_adopted_commit(repo_root, args.commit)

    def _apply(state: State) -> dict[str, Any]:
        if args.wave_id not in state.waves:
            raise DaemonValidationError(f"validation_failed: unknown wave: {args.wave_id!r}")
        integration = create_wave_integration(
            state,
            wave_id=args.wave_id,
            base_sha=facts.base_sha,
            candidate_sha=facts.candidate_sha,
            integrated_sha=facts.integrated_sha,
            tree_sha=facts.tree_sha,
            diff_digest=facts.diff_digest,
            spec_digest=digest_wave_contract(state, wave_id=args.wave_id),
            kind=WaveIntegrationKind.ADOPT,
            reason=args.reason,
        )
        if integration.kind is not WaveIntegrationKind.ADOPT:
            raise DaemonValidationError(
                "validation_failed: exact revision already has a non-adopt integration fact"
            )
        return {
            "wave_id": args.wave_id,
            "integration": integration.model_dump(mode="json"),
            "ancestry_verified": True,
            "tree_verified": True,
        }

    return _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params=args.model_dump(mode="json"),
        command="integration.adopt",
        scope_id=args.wave_id,
        apply_func=_apply,
    )


@register("dependency_barrier.set")
async def set_dependency_barrier(
    ctx: MethodContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Author or revise one explicit dependency barrier on a PENDING Wave."""
    from eawf.runtime.daemon.methods.state import _commit_worktree_state

    args = DependencyBarrierSetParams.model_validate(params)
    repo_root = Path(args.repo_root).resolve()

    def _apply(state: State) -> dict[str, Any]:
        wave = state.waves.get(args.wave_id)
        if wave is None:
            raise DaemonValidationError(f"validation_failed: unknown wave: {args.wave_id!r}")
        if wave.status is not WaveStatus.PENDING:
            raise DaemonValidationError(
                f"validation_failed: dependency barrier target {args.wave_id!r} "
                f"must be pending (status={wave.status.value!r})"
            )
        if args.dep_wave_id not in state.waves:
            raise DaemonValidationError(
                f"validation_failed: unknown dependency wave: {args.dep_wave_id!r}"
            )
        if args.dep_wave_id not in wave.deps:
            raise DaemonValidationError(
                f"validation_failed: {args.dep_wave_id!r} is not a dependency of {args.wave_id!r}"
            )
        barrier = WaveDependencyBarrier(
            wave_id=args.wave_id,
            dep_wave_id=args.dep_wave_id,
            start_after=args.start_after,
            land_after=args.land_after,
            reason=args.reason,
        )
        key = wave_dependency_key(args.wave_id, args.dep_wave_id)
        state.wave_dependency_barriers[key] = barrier
        return {
            "wave_id": args.wave_id,
            "dep_wave_id": args.dep_wave_id,
            "barrier_key": key,
            "barrier": barrier.model_dump(mode="json"),
        }

    return _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params=args.model_dump(mode="json"),
        command="dependency_barrier.set",
        scope_id=args.wave_id,
        apply_func=_apply,
    )


__all__ = [
    "adopt",
    "set_dependency_barrier",
]
