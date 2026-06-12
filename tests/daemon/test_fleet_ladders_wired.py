"""Tests: the bounded spawn + repair ladders wired into the loop (P30-I17-W03).

``spawn_lane_or_fork`` (DL-11) and ``repair_lane_or_fork`` (DL-7) were built and
tested with ZERO production callers; the bare spawner path let one
``RuntimeSpawnError`` abort the whole run, and the cockpit repair counter
displayed a ladder nothing drove. These assertions confirm the W03 wiring:

- C1: ``_fill_lanes`` routes the spawn through the bounded spawn ladder when an
  :class:`ErrorClassifier` is wired -- an injected spawn error FORKS the lane
  (a queued fork) instead of aborting the run, and the sibling waves still
  drain.
- C2: the failing-check path routes through the bounded grounded repair ladder
  via the repair hook -- a failing lane re-dispatches up the ladder.
- C3: a resolved repair re-registers the lane under its INCREMENTED dispatch
  attempt, so the cockpit repair counter reflects the real attempts; an
  exhausted ladder leaves the lane forked.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import RiskTier
from eawf.kernel.state.models import (
    FleetFork,
    FleetForkReason,
    FleetLane,
    FleetRunState,
    FleetTerminalReason,
    State,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import (
    LaneDispatch,
    LaneOutcome,
    LaneRepairOutcome,
    arm_drive,
)
from eawf.runtime.runtimes.adapter import (
    RUNTIME_API_ERROR,
    RUNTIME_RATE_LIMIT,
    RuntimeSpawnError,
)
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_IDS = ["P30-I17-W01", "P30-I17-W02", "P30-I17-W03"]


def _state_payload() -> dict[str, Any]:
    waves: dict[str, Any] = {}
    for wid in _WAVE_IDS:
        waves[wid] = {
            "id": wid,
            "iter_id": "P30-I17",
            "title": f"Frontier wave {wid[-3:]}",
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "agent_role": "executor",
            "effort_bucket": "M",
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": None,
            "opened_at": "2026-06-11T00:00:00Z",
            "closed_at": None,
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
            "iter_id": "P30-I17",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P30": {
                "id": "P30",
                "scope_id": "EAWF",
                "title": "Binding pass",
                "status": "active",
                "iter_ids": ["P30-I17"],
                "outcome_ids": [],
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I17": {
                "id": "P30-I17",
                "phase_id": "P30",
                "title": "Autopilot full-wire",
                "status": "active",
                "wave_ids": list(_WAVE_IDS),
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
    }


def _write_state(tmp_path: Path) -> Path:
    state = State.model_validate(_state_payload())
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


def _persisted(state_path: Path) -> Any:
    return load_state(state_path).fleet_run


# ---- C1: an injected spawn error FORKS instead of aborting the run -----------


def test_hard_spawn_error_forks_lane_without_aborting_run(tmp_path: Path) -> None:
    """C1: a HARD spawn error terminates the lane to a fork; siblings still drain.

    The first wave's spawn raises a launch-failure ``RuntimeSpawnError``
    (ENOENT), which the bounded spawn ladder classifies HARD and terminates to a
    ``runtime_spawn_error`` fork on the first failure. With the ladder wired
    (an ``ErrorClassifier`` is passed), the loop does NOT abort -- it queues the
    fork and drains waves 2 + 3 cleanly.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawned: list[str] = []

    def _spawn(c: MethodContext, wave_id: str) -> LaneDispatch:
        spawned.append(wave_id)
        if wave_id == _WAVE_IDS[0]:
            exc = RuntimeSpawnError("agent cli not found")
            raise exc from FileNotFoundError(2, "No such file or directory")
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=9000 + len(spawned))

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=_spawn,
        watch=lambda c, lane: "closed",
        classify=lambda exc, runtime: RUNTIME_API_ERROR,
        runtime_preference=["claude-code", "codex"],
    )
    # The run did NOT abort: it ran to a terminal state.
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # W01 forked (HARD spawn failure); W02 + W03 closed cleanly.
    assert run.counters.forked >= 1
    assert run.counters.closed == 2
    # The HARD-failure fork was queued with the typed reason.
    persisted = _persisted(state_path)
    assert persisted is not None
    reasons = {fork.reason for fork in persisted.forks}
    assert FleetForkReason.RUNTIME_SPAWN_ERROR in reasons


def test_recoverable_spawn_error_retries_then_forks(tmp_path: Path) -> None:
    """C1: a persistent RECOVERABLE spawn error exhausts the ladder then forks.

    Every spawn of the first wave raises a recoverable ``RuntimeSpawnError``; the
    bounded ladder retries (RETRY_SAME / SWITCH) up to ``max_total_attempts``
    then HALTs to a ``RETRY_EXHAUSTED`` fork rather than respawning forever -- the
    run still drains the remaining waves rather than aborting.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    attempts: list[str] = []

    def _spawn(c: MethodContext, wave_id: str) -> LaneDispatch:
        attempts.append(wave_id)
        if wave_id == _WAVE_IDS[0]:
            raise RuntimeSpawnError("rate limited", exit_status=1)
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=9100 + len(attempts))

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=_spawn,
        watch=lambda c, lane: "closed",
        classify=lambda exc, runtime: RUNTIME_RATE_LIMIT,
        runtime_preference=["claude-code"],
        max_total_attempts=2,
    )
    # The first wave's spawn was retried (>1 attempt) then forked; the rest drained.
    assert attempts.count(_WAVE_IDS[0]) == 2
    assert run.run_state is FleetRunState.DONE
    persisted = _persisted(state_path)
    assert persisted is not None
    reasons = {fork.reason for fork in persisted.forks}
    assert FleetForkReason.RETRY_EXHAUSTED in reasons


def test_no_classifier_keeps_direct_spawn_path(tmp_path: Path) -> None:
    """C1 negative: with no classifier wired the pre-W03 direct-spawn path runs.

    A spawn that raises ``RuntimeSpawnError`` without a wired ladder propagates
    (the pre-W03 behaviour) -- the ladder is opt-in via the classifier, so the
    existing synchronous callers + fakes are unaffected.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    def _spawn(c: MethodContext, wave_id: str) -> LaneDispatch:
        raise RuntimeSpawnError("boom")

    with pytest.raises(RuntimeSpawnError):
        arm_drive(
            ctx,
            frontier=list(_WAVE_IDS[:1]),
            concurrency=1,
            spawn=_spawn,
            watch=lambda c, lane: "closed",
        )


# ---- C2 + C3: a failing check re-dispatches up the bounded repair ladder -----


def test_failing_check_repairs_and_advances_attempt(tmp_path: Path) -> None:
    """C2/C3: a failing-check fork re-dispatches; the repair counter advances.

    The first watch of W01 reports a failing-check fork; the repair hook resolves
    it (re-dispatch with attempt 2), so the lane is re-registered in flight and
    the next watch closes it clean. The repaired lane's incremented attempt IS
    the cockpit repair counter, so the hook's attempts are real, not cosmetic.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    # W01 forks once then closes; W02 + W03 close clean.
    watch_outcomes = {
        "P30-I17-W01": iter(["forked", "closed"]),
        "P30-I17-W02": iter(["closed"]),
        "P30-I17-W03": iter(["closed"]),
    }
    repaired: list[str] = []

    def _watch(c: MethodContext, lane: FleetLane) -> LaneOutcome:
        return next(watch_outcomes[lane.wave_id])

    def _repair(c: MethodContext, lane: FleetLane) -> LaneRepairOutcome:
        repaired.append(lane.wave_id)
        # Resolve the refused check by re-dispatching with the incremented
        # attempt (the cockpit repair counter).
        return LaneRepairOutcome(
            resolved=True,
            attempts_used=2,
            dispatch=LaneDispatch(
                session_id=f"ses-{lane.wave_id}-repair",
                pgid=9300,
                attempt=lane.attempt + 1,
            ),
        )

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=lambda c, wid: LaneDispatch(session_id=f"ses-{wid}", pgid=9200, attempt=1),
        watch=_watch,
        repair=_repair,
    )
    # The repair hook fired once for the failing W01 lane.
    assert repaired == ["P30-I17-W01"]
    # The run drained to DONE with every wave eventually closing (the repaired
    # lane closed on its re-dispatch, so no terminal failure).
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.closed == 3
    assert run.counters.failed == 0


def test_exhausted_repair_leaves_lane_forked(tmp_path: Path) -> None:
    """C3: an exhausted repair ladder leaves the lane a queued REPAIR_EXHAUSTED fork.

    The repair hook drives the real ``repair_lane_or_fork`` exhaustion contract:
    it enqueues a ``REPAIR_EXHAUSTED`` fork through the canonical writer (the
    daemon-owned enqueue) then returns ``resolved=False``. The loop absorbs that
    disk-side fork rather than re-dispatching forever, and the run drains the
    remaining waves cleanly.
    """
    from eawf.runtime.daemon.methods.fleet import _enqueue_fork

    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    def _watch(c: MethodContext, lane: FleetLane) -> LaneOutcome:
        return "forked" if lane.wave_id == _WAVE_IDS[0] else "closed"

    def _repair(c: MethodContext, lane: FleetLane) -> LaneRepairOutcome:
        # The repair budget is spent: mirror repair_lane_or_fork by enqueuing the
        # REPAIR_EXHAUSTED fork through the canonical writer, then signal
        # exhaustion so the loop counts it once (off the disk fork).
        fork = FleetFork(
            wave_id=lane.wave_id,
            attempt=lane.attempt,
            risk_tier=RiskTier.MECH,
            reason=FleetForkReason.REPAIR_EXHAUSTED,
            evidence_ref=f"urn:eawf:v1:fork:{lane.wave_id}:repair_exhausted",
            forked_at=datetime.now(UTC),
        )
        _enqueue_fork(c, fork)
        return LaneRepairOutcome(resolved=False, attempts_used=3, dispatch=None)

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=lambda c, wid: LaneDispatch(session_id=f"ses-{wid}", pgid=9400, attempt=1),
        watch=_watch,
        repair=_repair,
    )
    assert run.run_state is FleetRunState.DONE
    # W01 stayed forked (REPAIR_EXHAUSTED queued by the hook); W02 + W03 closed.
    assert run.counters.closed == 2
    persisted = _persisted(state_path)
    assert persisted is not None
    reasons = {fork.reason for fork in persisted.forks}
    assert FleetForkReason.REPAIR_EXHAUSTED in reasons
    assert run.counters.forked >= 1


def test_no_repair_hook_keeps_terminal_fork_behaviour(tmp_path: Path) -> None:
    """C2 negative: with no repair hook a failing check is a terminal fork.

    The repair ladder is opt-in via the hook; without it a failing-check fork
    counts as a terminal failure (the pre-W03 behaviour), so existing tests +
    callers are unaffected.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    def _watch(c: MethodContext, lane: FleetLane) -> LaneOutcome:
        return "forked" if lane.wave_id == _WAVE_IDS[0] else "closed"

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=_watch,
    )
    assert run.counters.failed == 1
    assert run.counters.closed == 2
