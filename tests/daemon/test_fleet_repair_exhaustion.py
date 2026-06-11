"""Tests: exhausted grounded repair escalates to a fork, never a drop (P30-I12-W07 / DL-7).

Extends the I06-W07 grounded repair: when a lane's bounded repair loop spends its
whole attempt budget without the refused criterion passing, the loop ESCALATES the
lane to an operator-resolved :class:`~eawf.kernel.state.models.FleetFork` tagged
:attr:`~eawf.kernel.state.models.FleetForkReason.REPAIR_EXHAUSTED` ("repair
exhausted -- your call") carrying the last failing check, rather than silently
dropping the lane or re-dispatching forever.

The success criteria under test:

* C1: a wave that fails its repair budget (every attempt spent) raises a
  :class:`~eawf.workflow.dispatch.retry.RepairExhaustedError` carrying the refused
  criterion + the last failing check, which the fleet loop turns into a queued
  ``REPAIR_EXHAUSTED`` fork whose evidence ref carries that last failing check.
* C2: on repair exhaustion the loop NEVER leaves the lane in any state other than
  fork -- no path transitions the exhausted lane to PENDING and no path drops it
  without a queued fork (the load-bearing no-silent-drop invariant). The
  exhausted lane's in-flight slot is removed ONLY into the fork queue.

The repair spawn is ALWAYS a recording stub -- these tests never fork a real
subprocess (no network, no auth, no cost). The verifier is a scripted stub
standing in for the close-gate oracle re-run.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.spec.common import CriterionSpec, QualityDimension
from eawf.kernel.state.enums import RiskTier, WaveStatus
from eawf.kernel.state.models import FleetForkReason, State
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import (
    repair_exhausted_fork,
    repair_lane_or_fork,
)
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.retry import RepairExhaustedError
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._errors import LifecycleError

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 11, 12, 0, 5, tzinfo=UTC)

_WAVE_ID = "P30-I12-W07"
_CRITERION_TEXT = "the close gate passes the cold-path import budget criterion"
_FAILING_DETAIL = "command_exit_zero gate exit=1: registry imported on the CLI cold path"


def _criterion(cid: str = "CR-07") -> CriterionSpec:
    """Build a typed success criterion whose text grounds the repair prompt."""
    return CriterionSpec(
        id=cid,
        text=_CRITERION_TEXT,
        kind="behavior",
        acceptance_style="binary",
        evidence_kind="deterministic",
        required=True,
        quality_dimension=QualityDimension.PERFORMANCE_EFFICIENCY,
        measurable_signal="the cold-path import gate scores this criterion deterministically",
    )


def _spawn_result(runtime: str = "claude-code") -> SpawnResult:
    """Build an otherwise-valid :class:`SpawnResult` for one repair re-dispatch."""
    return SpawnResult(
        session_id=f"sess-{runtime}",
        runtime=runtime,
        model="opus",
        subprocess_pid=4321,
        exit_status=0,
        text="repaired",
        started_at=_T0,
        ended_at=_T1,
    )


class _RecordingSpawn:
    """Records each grounded repair prompt; raises past the bound (no infinite loop)."""

    def __init__(self, *, limit: int) -> None:
        self.prompts: list[str] = []
        self._limit = limit

    async def __call__(self, prompt: str) -> SpawnResult:
        if len(self.prompts) >= self._limit:
            raise AssertionError(
                f"repair spawn called {len(self.prompts) + 1} times but "
                f"only {self._limit} call(s) allowed (unbounded loop?)"
            )
        self.prompts.append(prompt)
        return _spawn_result()


def _state_payload(*, lane_in_flight: bool) -> dict[str, Any]:
    """One CLAIMED wave under an armed DRAINING run, optionally holding its lane.

    When *lane_in_flight* the wave's lane is registered in-flight on the run so
    the escalation can drop it into the fork queue; the no-silent-drop assertions
    then read the resulting (lanes, forks, wave-status) triple.
    """
    waves = {
        _WAVE_ID: {
            "id": _WAVE_ID,
            "iter_id": "P30-I12",
            "title": "Repair-exhaustion escalation wave",
            "status": "claimed",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "gates": [],
            "agent_role": "executor",
            "effort_bucket": "M",
            "claim_session_id": f"ses-{_WAVE_ID}",
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": None,
            "opened_at": "2026-06-11T00:00:00Z",
            "closed_at": None,
        }
    }
    lanes: dict[str, Any] = {}
    if lane_in_flight:
        lanes[_WAVE_ID] = {
            "wave_id": _WAVE_ID,
            "attempt": 1,
            "session_id": f"ses-{_WAVE_ID}",
            "pgid": None,
            "dispatched_at": "2026-06-11T00:00:00Z",
        }
    fleet_run = {
        "run_state": "draining",
        "concurrency": 1,
        "frontier": [],
        "lanes": lanes,
        "forks": [],
        "counters": {},
        "convergence": "drain",
        "kclean_k": 2,
        "eu_cap": None,
        "usd_cap": None,
        "waves_cap": None,
        "hard_halt": False,
        "terminal_reason": None,
        "ended_at": None,
        "elapsed_hours": None,
        "throughput": None,
        "armed_at": "2026-06-11T00:00:00Z",
    }
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-06-11T00:00:00Z",
        "dispatch_paused": False,
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "track_id": None,
            "phase_id": "P30",
            "iter_id": "P30-I12",
            "active_wave_ids": [_WAVE_ID],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "Binding pass",
                "status": "active",
                "iter_ids": ["P30-I12"],
                "outcome_ids": [],
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I12": {
                "id": "P30-I12",
                "phase_id": "P30",
                "title": "Fleet auto-drain loop",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": waves,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
        "fleet_run": fleet_run,
    }


def _write_state(tmp_path: Path, *, lane_in_flight: bool = True) -> Path:
    state = State.model_validate(_state_payload(lane_in_flight=lane_in_flight))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    event_path = state_path.parent / "store" / "event.jsonl"
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        event_path=event_path,
        state_path=state_path,
    )


def _always_failing(_result: SpawnResult) -> str:
    """Verifier that always reports the criterion still failing (forces exhaustion)."""
    return _FAILING_DETAIL


# ---------------------------------------------------------------------------
# The retry-side typed exhaustion carries the criterion + last failing check.
# ---------------------------------------------------------------------------


def test_repair_exhausted_error_carries_criterion_and_last_failing_detail() -> None:
    """The spent repair loop raises a RepairExhaustedError naming the criterion + last check."""
    from eawf.workflow.dispatch.retry import repair_until_resolved

    criterion = _criterion()
    max_attempts = 2
    spawn = _RecordingSpawn(limit=max_attempts)

    with pytest.raises(RepairExhaustedError) as excinfo:
        asyncio.run(
            repair_until_resolved(
                criterion,
                _FAILING_DETAIL,
                base_prompt="ORIGINAL DISPATCH PROMPT",
                spawn=spawn,
                verify=_always_failing,
                max_attempts=max_attempts,
            )
        )
    exc = excinfo.value
    assert exc.criterion_id == criterion.id
    assert exc.last_failing_detail == _FAILING_DETAIL
    # Bounded: the budget is spent at the cap, not looped past it.
    assert len(spawn.prompts) == max_attempts
    assert exc.attempts == max_attempts


# ---------------------------------------------------------------------------
# C1: the pure escalation builds a REPAIR_EXHAUSTED fork carrying the check.
# ---------------------------------------------------------------------------


def test_repair_exhausted_fork_builds_typed_fork_carrying_last_check() -> None:
    """The pure builder produces a REPAIR_EXHAUSTED fork carrying the last failing check."""
    exc = RepairExhaustedError(
        criterion_id="CR-07",
        last_failing_detail=_FAILING_DETAIL,
        attempts=2,
        failures=[],
        notice=_dummy_notice(),
    )
    fork = repair_exhausted_fork(
        exc, wave_id=_WAVE_ID, attempt=1, risk_tier=RiskTier.HIGH
    )
    assert fork.wave_id == _WAVE_ID
    assert fork.attempt == 1
    assert fork.risk_tier is RiskTier.HIGH
    assert fork.reason is FleetForkReason.REPAIR_EXHAUSTED
    # C1: the fork carries the concrete last failing check.
    assert fork.evidence_ref == _FAILING_DETAIL


def test_repair_exhausted_fork_normalises_multiline_detail() -> None:
    """A multi-line oracle dump is single-lined + bounded onto the evidence ref."""
    exc = RepairExhaustedError(
        criterion_id="CR-07",
        last_failing_detail="line one\n  line two\t  line three",
        attempts=2,
        failures=[],
        notice=_dummy_notice(),
    )
    fork = repair_exhausted_fork(
        exc, wave_id=_WAVE_ID, attempt=1, risk_tier=RiskTier.MECH
    )
    assert fork.evidence_ref == "line one line two line three"
    assert "\n" not in (fork.evidence_ref or "")


def _dummy_notice() -> Any:
    """Build a minimal FailureNotice for the pure-builder tests."""
    from eawf.workflow.dispatch.retry import FailureNotice, FailureTier

    return FailureNotice(
        tier=FailureTier.TRANSIENT_RETRYABLE,
        runtime="claude-code",
        error_class="REPAIR_CRITERION_STILL_FAILING",
        attempts_used=2,
        message="spent",
    )


# ---------------------------------------------------------------------------
# C1 + C2: the wired entry point escalates to a queued fork, never a drop.
# ---------------------------------------------------------------------------


def test_repair_lane_or_fork_escalates_to_queued_repair_exhausted_fork(tmp_path: Path) -> None:
    """C1: budget exhaustion enqueues a REPAIR_EXHAUSTED fork carrying the last check."""
    state_path = _write_state(tmp_path, lane_in_flight=True)
    ctx = _ctx(state_path)
    criterion = _criterion()
    spawn = _RecordingSpawn(limit=2)

    with pytest.raises(RepairExhaustedError):
        asyncio.run(
            repair_lane_or_fork(
                ctx,
                criterion,
                _FAILING_DETAIL,
                base_prompt="ORIGINAL DISPATCH PROMPT",
                spawn=spawn,
                verify=_always_failing,
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.HIGH,
                max_attempts=2,
            )
        )

    run = load_state(state_path).fleet_run
    assert run is not None
    # Exactly one queued fork, tagged REPAIR_EXHAUSTED, carrying the last check.
    assert len(run.forks) == 1
    fork = run.forks[0]
    assert fork.wave_id == _WAVE_ID
    assert fork.reason is FleetForkReason.REPAIR_EXHAUSTED
    assert fork.risk_tier is RiskTier.HIGH
    assert fork.evidence_ref == _FAILING_DETAIL
    assert run.counters.forked == 1
    assert run.counters.blocked == 1


def test_repair_lane_or_fork_never_drops_or_re_pends_the_exhausted_lane(tmp_path: Path) -> None:
    """C2 (no-silent-drop): an exhausted lane is removed ONLY into the fork queue.

    The load-bearing negative path: after exhaustion the wave is NOT reset to
    PENDING and the in-flight lane is NOT dropped without a queued fork. The only
    terminal the lane reaches is the queued REPAIR_EXHAUSTED fork.
    """
    state_path = _write_state(tmp_path, lane_in_flight=True)
    ctx = _ctx(state_path)
    criterion = _criterion()
    spawn = _RecordingSpawn(limit=2)

    with pytest.raises(RepairExhaustedError):
        asyncio.run(
            repair_lane_or_fork(
                ctx,
                criterion,
                _FAILING_DETAIL,
                base_prompt="ORIGINAL DISPATCH PROMPT",
                spawn=spawn,
                verify=_always_failing,
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.MECH,
                max_attempts=2,
            )
        )

    state = load_state(state_path)
    run = state.fleet_run
    assert run is not None
    # The in-flight slot was removed -- but ONLY into the fork queue.
    assert _WAVE_ID not in run.lanes
    assert len(run.forks) == 1
    assert run.forks[0].wave_id == _WAVE_ID
    # The wave was NEVER reset to PENDING -- it stays CLAIMED, held by the fork.
    assert state.waves[_WAVE_ID].status is WaveStatus.CLAIMED
    # No lane vanished without a fork: every removed lane is accounted for as a fork.
    assert run.counters.forked == 1


def test_repair_lane_or_fork_returns_result_when_repair_resolves(tmp_path: Path) -> None:
    """A repair the verifier accepts returns the re-dispatch result and queues NO fork."""
    state_path = _write_state(tmp_path, lane_in_flight=True)
    ctx = _ctx(state_path)
    criterion = _criterion()
    spawn = _RecordingSpawn(limit=1)

    result = asyncio.run(
        repair_lane_or_fork(
            ctx,
            criterion,
            _FAILING_DETAIL,
            base_prompt="ORIGINAL DISPATCH PROMPT",
            spawn=spawn,
            verify=lambda _result: None,  # first re-dispatch resolves the refusal
            wave_id=_WAVE_ID,
            attempt=1,
            risk_tier=RiskTier.MECH,
        )
    )
    assert result.runtime == "claude-code"
    run = load_state(state_path).fleet_run
    assert run is not None
    # The repair resolved -- no escalation, the lane is untouched by this path.
    assert run.forks == []
    assert _WAVE_ID in run.lanes


def test_repair_lane_or_fork_no_run_armed_raises(tmp_path: Path) -> None:
    """The escalation fails loud when no fleet run is armed -- never drops the fork."""
    state_path = _write_state(tmp_path, lane_in_flight=True)
    # Clear the armed run so the escalation has nowhere to enqueue.
    state = load_state(state_path)
    state.fleet_run = None
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    ctx = _ctx(state_path)
    criterion = _criterion()
    spawn = _RecordingSpawn(limit=2)

    with pytest.raises(LifecycleError, match="no fleet run armed"):
        asyncio.run(
            repair_lane_or_fork(
                ctx,
                criterion,
                _FAILING_DETAIL,
                base_prompt="ORIGINAL DISPATCH PROMPT",
                spawn=spawn,
                verify=_always_failing,
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.MECH,
                max_attempts=2,
            )
        )
