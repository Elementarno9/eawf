"""Wave integration generations and two-stage dependency barriers."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

import orjson

from eawf.kernel.state.enums import (
    DependencyStage,
    WaveIntegrationKind,
    WaveIntegrationStatus,
    WaveStatus,
)
from eawf.kernel.state.models import (
    State,
    WaveDependencyBarrier,
    WaveDependencyBinding,
    WaveIntegration,
    wave_dependency_key,
)

_ACTIVE_INTEGRATION_STATUSES = frozenset(
    {WaveIntegrationStatus.INTEGRATED, WaveIntegrationStatus.VERIFIED}
)


class DependencyBarrierError(ValueError):
    """Raised when a wave cannot cross a dependency barrier."""


@dataclass(frozen=True)
class DependencyEvaluation:
    """Result of evaluating one wave's dependency frontier."""

    wave_id: str
    stage: str
    unmet: tuple[str, ...]
    stale: tuple[str, ...]

    @property
    def satisfied(self) -> bool:
        """Return whether every dependency passed without stale bindings."""
        return not self.unmet and not self.stale


def dependency_barrier(
    state: State,
    *,
    wave_id: str,
    dep_wave_id: str,
) -> WaveDependencyBarrier:
    """Return an explicit barrier or the strict legacy ``closed/closed`` rule."""
    key = wave_dependency_key(wave_id, dep_wave_id)
    explicit = state.wave_dependency_barriers.get(key)
    if explicit is not None:
        return explicit
    return WaveDependencyBarrier(
        wave_id=wave_id,
        dep_wave_id=dep_wave_id,
        start_after=DependencyStage.CLOSED,
        land_after=DependencyStage.CLOSED,
        reason="legacy dependency preserves closed-before-start and closed-before-land",
    )


def latest_wave_integration(state: State, wave_id: str) -> WaveIntegration | None:
    """Return the newest active integration generation for *wave_id*."""
    rows = [
        row
        for row in state.wave_integrations.values()
        if row.wave_id == wave_id and row.status in _ACTIVE_INTEGRATION_STATUSES
    ]
    if not rows:
        return None
    rows.sort(key=lambda row: (row.generation, row.created_at, row.id))
    return rows[-1]


def _stage_satisfied(
    state: State,
    *,
    dep_wave_id: str,
    required: DependencyStage,
) -> bool:
    dep = state.waves.get(dep_wave_id)
    if dep is None:
        return False
    if required is DependencyStage.CLOSED:
        return dep.status is WaveStatus.CLOSED
    integration = latest_wave_integration(state, dep_wave_id)
    if integration is None:
        return False
    if required is DependencyStage.VERIFIED:
        return integration.status is WaveIntegrationStatus.VERIFIED
    return integration.status in _ACTIVE_INTEGRATION_STATUSES


def _binding_stale(
    state: State,
    *,
    binding: WaveDependencyBinding,
) -> bool:
    integration = latest_wave_integration(state, binding.dep_wave_id)
    if integration is None:
        return True
    return (
        integration.id != binding.integration_id
        or integration.generation != binding.generation
        or integration.integrated_sha != binding.integrated_sha
        or integration.tree_sha != binding.tree_sha
    )


def evaluate_dependency_barriers(
    state: State,
    *,
    wave_id: str,
    for_land: bool = False,
) -> DependencyEvaluation:
    """Evaluate start or land barriers plus pinned-generation freshness."""
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")
    unmet: list[str] = []
    stale: list[str] = []
    for dep_wave_id in wave.deps:
        if dep_wave_id not in state.waves:
            unmet.append(f"{dep_wave_id}:missing")
            continue
        barrier = dependency_barrier(
            state,
            wave_id=wave_id,
            dep_wave_id=dep_wave_id,
        )
        required = barrier.land_after if for_land else barrier.start_after
        if not _stage_satisfied(state, dep_wave_id=dep_wave_id, required=required):
            unmet.append(f"{dep_wave_id}:{required.value}")
            continue
        binding = state.wave_dependency_bindings.get(wave_dependency_key(wave_id, dep_wave_id))
        if binding is not None and _binding_stale(state, binding=binding):
            stale.append(
                f"{dep_wave_id}:bound-generation={binding.generation}:"
                f"integration={binding.integration_id}"
            )
        elif binding is None and barrier.start_after is not DependencyStage.CLOSED and for_land:
            stale.append(f"{dep_wave_id}:missing-start-binding")
    return DependencyEvaluation(
        wave_id=wave_id,
        stage="land" if for_land else "start",
        unmet=tuple(unmet),
        stale=tuple(stale),
    )


def bind_start_dependencies(
    state: State,
    *,
    wave_id: str,
    now: datetime | None = None,
) -> tuple[WaveDependencyBinding, ...]:
    """Validate start barriers and pin every available upstream generation."""
    evaluation = evaluate_dependency_barriers(state, wave_id=wave_id)
    if not evaluation.satisfied:
        detail = [*evaluation.unmet, *evaluation.stale]
        raise DependencyBarrierError(f"wave {wave_id!r} start barrier blocked: {detail}")
    wave = state.waves[wave_id]
    bound_at = now or datetime.now(UTC)
    bindings: list[WaveDependencyBinding] = []
    for dep_wave_id in wave.deps:
        integration = latest_wave_integration(state, dep_wave_id)
        if integration is None:
            # Migrated legacy closed/closed edges deliberately carry no
            # invented integration fact or binding.
            continue
        key = wave_dependency_key(wave_id, dep_wave_id)
        existing = state.wave_dependency_bindings.get(key)
        if existing is not None:
            if _binding_stale(state, binding=existing):
                raise DependencyBarrierError(
                    f"wave {wave_id!r} dependency binding is stale: {dep_wave_id!r}"
                )
            bindings.append(existing)
            continue
        binding = WaveDependencyBinding(
            wave_id=wave_id,
            dep_wave_id=dep_wave_id,
            integration_id=integration.id,
            generation=integration.generation,
            integrated_sha=integration.integrated_sha,
            tree_sha=integration.tree_sha,
            start_fact_ref=f"integration:{integration.id}",
            land_fact_ref=None,
            bound_at=bound_at,
        )
        state.wave_dependency_bindings[key] = binding
        bindings.append(binding)
    return tuple(bindings)


def require_start_dependencies(state: State, *, wave_id: str) -> None:
    """Raise when execution cannot start or continue on its bound frontier."""
    evaluation = evaluate_dependency_barriers(state, wave_id=wave_id)
    if evaluation.satisfied:
        return
    detail = [*evaluation.unmet, *evaluation.stale]
    raise DependencyBarrierError(f"wave {wave_id!r} start barrier blocked: {detail}")


def require_land_dependencies(state: State, *, wave_id: str) -> None:
    """Raise when a landing frontier is unmet or generation-stale."""
    evaluation = evaluate_dependency_barriers(state, wave_id=wave_id, for_land=True)
    if evaluation.satisfied:
        return
    detail = [*evaluation.unmet, *evaluation.stale]
    raise DependencyBarrierError(f"wave {wave_id!r} land barrier blocked: {detail}")


def _integration_id(
    *,
    wave_id: str,
    generation: int,
    integrated_sha: str,
    tree_sha: str,
) -> str:
    material = f"{wave_id}\0{generation}\0{integrated_sha}\0{tree_sha}".encode()
    return f"integration-{hashlib.sha256(material).hexdigest()[:24]}"


def create_wave_integration(
    state: State,
    *,
    wave_id: str,
    base_sha: str,
    candidate_sha: str,
    integrated_sha: str,
    tree_sha: str,
    diff_digest: str,
    spec_digest: str,
    kind: WaveIntegrationKind = WaveIntegrationKind.LAND,
    reason: str | None = None,
    now: datetime | None = None,
) -> WaveIntegration:
    """Append one idempotent immutable integration generation."""
    if wave_id not in state.waves:
        raise KeyError(f"unknown wave: {wave_id!r}")
    previous = latest_wave_integration(state, wave_id)
    if (
        previous is not None
        and previous.wave_id == wave_id
        and previous.base_sha == base_sha
        and previous.candidate_sha == candidate_sha
        and previous.integrated_sha == integrated_sha
        and previous.tree_sha == tree_sha
        and previous.diff_digest == diff_digest
        and previous.spec_digest == spec_digest
        and previous.kind == kind
        and previous.reason == reason
    ):
        return previous
    generation = 1 if previous is None else previous.generation + 1
    integration_id = _integration_id(
        wave_id=wave_id,
        generation=generation,
        integrated_sha=integrated_sha,
        tree_sha=tree_sha,
    )
    integration = WaveIntegration(
        id=integration_id,
        wave_id=wave_id,
        generation=generation,
        status=WaveIntegrationStatus.INTEGRATED,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        integrated_sha=integrated_sha,
        tree_sha=tree_sha,
        diff_digest=diff_digest,
        spec_digest=spec_digest,
        created_at=now or datetime.now(UTC),
        supersedes_id=previous.id if previous is not None else None,
        kind=kind,
        reason=reason,
    )
    state.wave_integrations[integration.id] = integration
    return integration


def mark_wave_integration_verified(
    state: State,
    *,
    integration_id: str,
) -> WaveIntegration:
    """Replace one integration row with its verified immutable projection."""
    integration = state.wave_integrations.get(integration_id)
    if integration is None:
        raise KeyError(f"unknown wave integration: {integration_id!r}")
    if integration.status is WaveIntegrationStatus.VERIFIED:
        return integration
    if integration.status is not WaveIntegrationStatus.INTEGRATED:
        raise ValueError(
            f"integration {integration_id!r} cannot be verified "
            f"(status={integration.status.value!r})"
        )
    verified = integration.model_copy(update={"status": WaveIntegrationStatus.VERIFIED})
    state.wave_integrations[integration_id] = verified
    return verified


def digest_wave_contract(state: State, *, wave_id: str) -> str:
    """Return a stable digest of the Wave-owned verification contract."""
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")
    payload = {
        "wave_id": wave.id,
        "success_criteria": [
            criterion.model_dump(mode="json") for criterion in wave.success_criteria
        ],
        "gates": [gate.model_dump(mode="json") for gate in wave.gates],
        "file_scopes": list(wave.file_scopes),
    }
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


__all__ = [
    "DependencyBarrierError",
    "DependencyEvaluation",
    "bind_start_dependencies",
    "create_wave_integration",
    "dependency_barrier",
    "digest_wave_contract",
    "evaluate_dependency_barriers",
    "latest_wave_integration",
    "mark_wave_integration_verified",
    "require_land_dependencies",
    "require_start_dependencies",
]
