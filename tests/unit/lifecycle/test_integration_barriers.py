"""Wave integration generation and dependency barrier tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import (
    DependencyStage,
    WaveIntegrationKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    State,
    WaveDependencyBarrier,
    WaveIntegration,
    wave_dependency_key,
)
from eawf.kernel.state.wave_graph import blocked_by
from eawf.workflow.lifecycle.integration import (
    DependencyBarrierError,
    bind_start_dependencies,
    create_wave_integration,
    evaluate_dependency_barriers,
    latest_wave_integration,
    mark_wave_integration_verified,
    require_land_dependencies,
)
from eawf.workflow.lifecycle.wave import LifecycleError, start_wave
from tests.daemon.test_close_lock_split import _state_payload

_UPSTREAM = "P30-I23-W09"
_DOWNSTREAM = "P30-I23-W99"
_SHA_A = "a" * 40
_SHA_B = "b" * 40
_SHA_C = "c" * 40
_SHA_D = "d" * 40
_T0 = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _state() -> State:
    state = State.model_validate(_state_payload())
    upstream = state.waves[_UPSTREAM]
    downstream = state.waves[_DOWNSTREAM]
    upstream.status = WaveStatus.IN_PROGRESS
    downstream.deps = [_UPSTREAM]
    upstream.blocks = [_DOWNSTREAM]
    return state


def _integration(
    state: State,
    *,
    base_sha: str = _SHA_A,
    candidate_sha: str = _SHA_C,
    integrated_sha: str = _SHA_B,
    tree_sha: str = _SHA_D,
    diff_digest: str = "diff-digest",
    spec_digest: str = "spec-digest",
    kind: WaveIntegrationKind = WaveIntegrationKind.LAND,
    reason: str | None = None,
) -> WaveIntegration:
    return create_wave_integration(
        state,
        wave_id=_UPSTREAM,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        integrated_sha=integrated_sha,
        tree_sha=tree_sha,
        diff_digest=diff_digest,
        spec_digest=spec_digest,
        kind=kind,
        reason=reason,
        now=_T0,
    )


def test_missing_barrier_preserves_legacy_closed_closed_without_fact() -> None:
    state = _state()

    blocked = evaluate_dependency_barriers(state, wave_id=_DOWNSTREAM)
    assert blocked.unmet == (f"{_UPSTREAM}:closed",)

    state.waves[_UPSTREAM].status = WaveStatus.CLOSED
    assert evaluate_dependency_barriers(state, wave_id=_DOWNSTREAM).satisfied
    assert bind_start_dependencies(state, wave_id=_DOWNSTREAM, now=_T0) == ()
    assert state.wave_integrations == {}
    assert state.wave_dependency_bindings == {}


def test_relaxed_start_binds_exact_generation_and_new_generation_stales_land() -> None:
    state = _state()
    edge_key = wave_dependency_key(_DOWNSTREAM, _UPSTREAM)
    state.wave_dependency_barriers[edge_key] = WaveDependencyBarrier(
        wave_id=_DOWNSTREAM,
        dep_wave_id=_UPSTREAM,
        start_after=DependencyStage.INTEGRATED,
        land_after=DependencyStage.INTEGRATED,
        reason="isolated execution and landing may follow immutable integration",
    )
    first = _integration(state)

    bindings = bind_start_dependencies(state, wave_id=_DOWNSTREAM, now=_T0)
    assert len(bindings) == 1
    assert bindings[0].integration_id == first.id
    require_land_dependencies(state, wave_id=_DOWNSTREAM)

    second = _integration(
        state,
        candidate_sha="f" * 40,
        integrated_sha="e" * 40,
    )
    assert second.generation == 2
    assert second.supersedes_id == first.id
    assert latest_wave_integration(state, _UPSTREAM) is second
    assert second.candidate_sha == "f" * 40
    with pytest.raises(DependencyBarrierError, match="bound-generation=1"):
        require_land_dependencies(state, wave_id=_DOWNSTREAM)


def test_integrated_start_closed_land_allows_overlap_but_blocks_integration() -> None:
    state = _state()
    edge_key = wave_dependency_key(_DOWNSTREAM, _UPSTREAM)
    state.wave_dependency_barriers[edge_key] = WaveDependencyBarrier(
        wave_id=_DOWNSTREAM,
        dep_wave_id=_UPSTREAM,
        start_after=DependencyStage.INTEGRATED,
        land_after=DependencyStage.CLOSED,
        reason="dependent may execute in isolation but cannot land before acceptance",
    )
    _integration(state)

    bind_start_dependencies(state, wave_id=_DOWNSTREAM, now=_T0)
    with pytest.raises(DependencyBarrierError, match="closed"):
        require_land_dependencies(state, wave_id=_DOWNSTREAM)

    state.waves[_UPSTREAM].status = WaveStatus.CLOSED
    require_land_dependencies(state, wave_id=_DOWNSTREAM)


def test_verified_stage_requires_verified_integration() -> None:
    state = _state()
    edge_key = wave_dependency_key(_DOWNSTREAM, _UPSTREAM)
    state.wave_dependency_barriers[edge_key] = WaveDependencyBarrier(
        wave_id=_DOWNSTREAM,
        dep_wave_id=_UPSTREAM,
        start_after=DependencyStage.VERIFIED,
        land_after=DependencyStage.VERIFIED,
        reason="downstream consumes exact verified gate evidence",
    )
    integration = _integration(state)

    assert not evaluate_dependency_barriers(state, wave_id=_DOWNSTREAM).satisfied
    mark_wave_integration_verified(state, integration_id=integration.id)
    assert evaluate_dependency_barriers(state, wave_id=_DOWNSTREAM).satisfied


def test_create_wave_integration_is_idempotent_for_same_facts() -> None:
    state = _state()
    first = _integration(state)
    second = _integration(state)

    assert first is second
    assert len(state.wave_integrations) == 1


@pytest.mark.parametrize(
    ("changed_fact", "changed_value"),
    [
        ("base_sha", "1" * 40),
        ("candidate_sha", "2" * 40),
        ("integrated_sha", "3" * 40),
        ("tree_sha", "4" * 40),
        ("diff_digest", "changed-diff"),
        ("spec_digest", "changed-spec"),
        ("kind", WaveIntegrationKind.ADOPT),
        ("reason", "changed reason"),
    ],
)
def test_create_wave_integration_appends_for_each_identity_fact_change(
    changed_fact: str,
    changed_value: str | WaveIntegrationKind,
) -> None:
    state = _state()
    first = _integration(state, reason="initial reason")
    facts: dict[str, str | WaveIntegrationKind | None] = {
        "base_sha": _SHA_A,
        "candidate_sha": _SHA_C,
        "integrated_sha": _SHA_B,
        "tree_sha": _SHA_D,
        "diff_digest": "diff-digest",
        "spec_digest": "spec-digest",
        "kind": WaveIntegrationKind.LAND,
        "reason": "initial reason",
    }
    facts[changed_fact] = changed_value

    second = create_wave_integration(
        state,
        wave_id=_UPSTREAM,
        base_sha=str(facts["base_sha"]),
        candidate_sha=str(facts["candidate_sha"]),
        integrated_sha=str(facts["integrated_sha"]),
        tree_sha=str(facts["tree_sha"]),
        diff_digest=str(facts["diff_digest"]),
        spec_digest=str(facts["spec_digest"]),
        kind=WaveIntegrationKind(facts["kind"]),
        reason=str(facts["reason"]),
        now=_T0,
    )

    assert second.generation == 2
    assert second.supersedes_id == first.id
    assert latest_wave_integration(state, _UPSTREAM) is second


def test_create_wave_integration_contract_only_change_appends_generation() -> None:
    state = _state()
    first = _integration(state)

    second = _integration(state, spec_digest="changed-spec")

    assert second.generation == 2
    assert second.supersedes_id == first.id


def test_relaxed_start_updates_live_blocked_by_view() -> None:
    state = _state()
    edge_key = wave_dependency_key(_DOWNSTREAM, _UPSTREAM)
    state.wave_dependency_barriers[edge_key] = WaveDependencyBarrier(
        wave_id=_DOWNSTREAM,
        dep_wave_id=_UPSTREAM,
        start_after=DependencyStage.INTEGRATED,
        land_after=DependencyStage.VERIFIED,
        reason="execute after immutable integration and land after proof",
    )

    assert blocked_by(_DOWNSTREAM, state) == (_UPSTREAM,)
    _integration(state)
    assert blocked_by(_DOWNSTREAM, state) == ()


def test_execution_continuation_rejects_stale_bound_generation() -> None:
    state = _state()
    edge_key = wave_dependency_key(_DOWNSTREAM, _UPSTREAM)
    state.wave_dependency_barriers[edge_key] = WaveDependencyBarrier(
        wave_id=_DOWNSTREAM,
        dep_wave_id=_UPSTREAM,
        start_after=DependencyStage.INTEGRATED,
        land_after=DependencyStage.VERIFIED,
        reason="continue only on the exact integrated generation",
    )
    _integration(state)
    bind_start_dependencies(state, wave_id=_DOWNSTREAM, now=_T0)
    state.waves[_DOWNSTREAM].status = WaveStatus.CLAIMED
    _integration(state, integrated_sha="e" * 40)

    with pytest.raises(LifecycleError, match="bound-generation=1"):
        start_wave(state, wave_id=_DOWNSTREAM)
