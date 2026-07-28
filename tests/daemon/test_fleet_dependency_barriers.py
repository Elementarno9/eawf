"""Fleet frontier coverage for relaxed immutable dependency barriers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import (
    DependencyStage,
    WaveStatus,
)
from eawf.kernel.state.models import (
    FleetRun,
    FleetRunState,
    State,
    WaveDependencyBarrier,
    wave_dependency_key,
)
from eawf.runtime.daemon.methods.fleet import _Loop
from eawf.workflow.lifecycle.integration import (
    create_wave_integration,
    mark_wave_integration_verified,
)
from tests.daemon.test_close_lock_split import (
    _WAVE,
    _build_ctx,
    _state_payload,
)

_DOWNSTREAM = "P30-I23-W99"
_NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def test_fleet_frontier_accepts_integrated_dependency_before_wave_close(
    tmp_path: Path,
) -> None:
    """Fleet uses the shared barrier evaluator, not a CLOSED-only shortcut."""
    state = State.model_validate(_state_payload())
    upstream = state.waves[_WAVE]
    downstream = state.waves[_DOWNSTREAM]
    upstream.status = WaveStatus.IN_PROGRESS
    downstream.deps = [_WAVE]
    upstream.blocks = [_DOWNSTREAM]
    state.wave_dependency_barriers[wave_dependency_key(_DOWNSTREAM, _WAVE)] = WaveDependencyBarrier(
        wave_id=_DOWNSTREAM,
        dep_wave_id=_WAVE,
        start_after=DependencyStage.INTEGRATED,
        land_after=DependencyStage.VERIFIED,
        reason="execute on immutable integration while upstream close runs",
    )
    integration = create_wave_integration(
        state,
        wave_id=_WAVE,
        base_sha="a" * 40,
        candidate_sha="b" * 40,
        integrated_sha="c" * 40,
        tree_sha="d" * 40,
        diff_digest="diff-digest",
        spec_digest="spec-digest",
        now=_NOW,
    )
    mark_wave_integration_verified(state, integration_id=integration.id)

    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    loop = _Loop(
        ctx=_build_ctx(tmp_path, state_path),
        run=FleetRun(
            run_state=FleetRunState.DRAINING,
            frontier=[],
            concurrency=1,
            armed_at=_NOW,
        ),
        spawn=lambda _ctx, wave_id: f"session-{wave_id}",
        watch=lambda _ctx, _lane: "running",
    )

    loop._recompute_frontier()

    assert state.waves[_WAVE].status is WaveStatus.IN_PROGRESS
    assert loop.run.frontier == [_DOWNSTREAM]
