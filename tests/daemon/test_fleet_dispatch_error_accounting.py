"""Tests: a fleet dispatch error records a FAILED outcome + keeps the drive alive (P30-I20-W42).

The autopilot fleet drive loop only forks the narrow
:class:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError` /
:class:`~eawf.runtime.daemon.methods.fleet.LaneRetryExhaustedError` set that the
bounded spawn ladder models. An error raised DEEP in dispatch -- e.g. an
:class:`~eawf.workflow.dispatch.llm_assist.LLMAssistError` mid-prompt -- used to
escape :func:`~eawf.runtime.daemon.methods.fleet._fill_lanes` uncaught: the wave
was already CLAIMED on disk + popped off the frontier, but no counter bumped and
no lane registered, so the run was left at ``DRAINING`` with
``terminal_reason=None`` and the run-summary card falsely rendered
``0 closed / 0 failed / 0 blocked``.

This suite pins the fix: a dispatch exception records a terminal FAILED outcome
for the wave (bumping ``failed`` + ``dispatched``), advances the wave off CLAIMED
to a terminal FAILED status on disk, and lets the drive finish cleanly with a
real terminal reason -- so the summary invariant
``closed + failed + blocked == dispatched`` holds and the tally is non-empty.

Every spawn is an INJECTED fake -- these tests never fork a real subprocess (no
network, no auth, no cost).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import FleetRunState, FleetTerminalReason, State
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import LaneDispatch, arm_drive
from eawf.runtime.runtimes.adapter import RuntimeSpawnError
from eawf.workflow.dispatch.llm_assist import LLMAssistError, SchemaAttemptFailure
from eawf.workflow.evidence._io import load_state
from tests._session_helpers import claim_wave_with_session as claim_wave

pytestmark = pytest.mark.integration

_WAVE_IDS = ["P30-I20-W01", "P30-I20-W02", "P30-I20-W03"]


# ---------------------------------------------------------------------------
# Scaffolding: a 3-wave PENDING frontier under an active iter.
# ---------------------------------------------------------------------------


def _state_payload() -> dict[str, Any]:
    waves: dict[str, Any] = {}
    for wid in _WAVE_IDS:
        waves[wid] = {
            "id": wid,
            "iter_id": "P30-I20",
            "title": f"Dispatch-error frontier wave {wid[-3:]}",
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
            "iter_id": "P30-I20",
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
                "iter_ids": ["P30-I20"],
                "outcome_ids": [],
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I20": {
                "id": "P30-I20",
                "phase_id": "P30",
                "title": "Headless dispatch tail",
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


def _llm_assist_error() -> LLMAssistError:
    """A typed dispatch-deep failure the bounded spawn ladder does NOT model."""
    return LLMAssistError(
        attempts=2,
        failures=[
            SchemaAttemptFailure(
                attempt=1,
                reason="schema_mismatch",
                detail="forced report body rejected",
            )
        ],
    )


def _classify_rate_limit(_exc: RuntimeSpawnError, _runtime: str) -> str:
    """Classifier stub for the bounded ladder -- never reached by a non-spawn error."""
    return "RUNTIME_RATE_LIMIT"


class _ClaimThenDispatchError:
    """Claims the wave on disk (like the live spawner) then raises on dispatch.

    Mirrors :func:`~eawf.runtime.daemon.methods.fleet._default_spawner`: the wave
    is CLAIMED through the canonical lifecycle transition BEFORE the dispatch
    runs, so a dispatch error leaves the wave stuck CLAIMED -- exactly the corrupt
    state the fix must clean up. The first targeted wave raises; the rest spawn
    clean so the boundary (a healthy wave still tallies) is exercised in one run.
    """

    def __init__(self, *, fail_wave_id: str, error: BaseException) -> None:
        self._fail_wave_id = fail_wave_id
        self._error = error
        self.spawned: list[str] = []

    def __call__(self, ctx: MethodContext, wave_id: str) -> LaneDispatch | None:
        self.spawned.append(wave_id)
        # Claim the wave on disk first (the live spawner claims before dispatch),
        # so a raise leaves the wave CLAIMED -- the bug's corrupt state.
        assert ctx.state_path is not None
        state_path = Path(ctx.state_path)
        state = load_state(state_path)
        claim_wave(state, wave_id=wave_id, session_id=f"ses-{wave_id}", out_of_order=True)
        state_path.write_text(state.model_dump_json(), encoding="utf-8")
        if wave_id == self._fail_wave_id:
            raise self._error
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=None, attempt=1)


# ---------------------------------------------------------------------------
# Invariant: a dispatch error records FAILED, the drive finishes, wave un-CLAIMED.
# ---------------------------------------------------------------------------


def test_dispatch_error_records_failed_and_finishes_clean(tmp_path: Path) -> None:
    """A wave whose dispatch raises records FAILED, advances off CLAIMED, drains clean.

    The load-bearing invariant: the loop does not raise; the failed tally is
    non-empty; ``closed + failed + blocked == dispatched`` holds; the run reaches
    a terminal state with a real reason; the failed wave is no longer CLAIMED.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _ClaimThenDispatchError(fail_wave_id=_WAVE_IDS[0], error=_llm_assist_error())

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        classify=_classify_rate_limit,
        runtime_preference=["claude-code"],
    )

    # The loop did not raise: it returned a terminal run.
    assert run.run_state is FleetRunState.DONE
    # A real terminal reason is stamped -- never DRAINING-with-None.
    assert run.terminal_reason is not None
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # The failed tally is non-empty: the dispatch error was recorded.
    assert run.counters.failed >= 1
    assert run.counters.failed == 1
    # The summary invariant holds: every dispatched wave landed in one bucket.
    counters = run.counters
    assert counters.closed + counters.failed + counters.blocked == counters.dispatched
    # All three frontier waves were dispatched (one failed, two closed clean).
    assert counters.dispatched == 3
    assert counters.closed == 2
    # The failed wave is no longer CLAIMED on disk -- a reattach never re-claims it.
    failed_wave = load_state(state_path).waves[_WAVE_IDS[0]]
    assert failed_wave.status is WaveStatus.FAILED
    assert failed_wave.outcome is not None
    assert "LLMAssistError" in failed_wave.outcome
    # The failed wave dropped off the active list (fail_wave un-tracks it).
    assert _WAVE_IDS[0] not in load_state(state_path).current.active_wave_ids


def test_dispatch_error_does_not_leave_wave_in_lanes(tmp_path: Path) -> None:
    """The dispatch-failed wave is never registered as an in-flight lane.

    The lane registration happens AFTER the spawn returns; a raised dispatch
    never reaches it, so the failed wave must not linger in ``run.lanes`` (which
    would make it an un-resolvable phantom lane on the terminal run).
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _ClaimThenDispatchError(fail_wave_id=_WAVE_IDS[1], error=_llm_assist_error())

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        classify=_classify_rate_limit,
        runtime_preference=["claude-code"],
    )

    assert run.lanes == {}
    assert _WAVE_IDS[1] not in run.lanes
    # The persisted run agrees (no phantom lane left on disk).
    persisted = load_state(state_path).fleet_run
    assert persisted is not None
    assert persisted.lanes == {}


# ---------------------------------------------------------------------------
# Boundary: a clean run still tallies as before (no regression).
# ---------------------------------------------------------------------------


class _CleanSpawner:
    """A spawner that claims + spawns every wave cleanly (the happy path)."""

    def __init__(self) -> None:
        self.spawned: list[str] = []

    def __call__(self, ctx: MethodContext, wave_id: str) -> LaneDispatch:
        self.spawned.append(wave_id)
        assert ctx.state_path is not None
        state_path = Path(ctx.state_path)
        state = load_state(state_path)
        claim_wave(state, wave_id=wave_id, session_id=f"ses-{wave_id}", out_of_order=True)
        state_path.write_text(state.model_dump_json(), encoding="utf-8")
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=None, attempt=1)


def test_clean_run_tallies_all_closed_no_failures(tmp_path: Path) -> None:
    """Boundary: a run with no dispatch error tallies every wave closed, zero failed."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _CleanSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        classify=_classify_rate_limit,
        runtime_preference=["claude-code"],
    )

    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.closed == 3
    assert run.counters.failed == 0
    assert run.counters.dispatched == 3
    counters = run.counters
    assert counters.closed + counters.failed + counters.blocked == counters.dispatched


# ---------------------------------------------------------------------------
# Boundary: a RuntimeSpawnError still routes to the existing fork path.
# ---------------------------------------------------------------------------


class _SpawnErrorSpawner:
    """Raises a HARD RuntimeSpawnError (ENOENT) -- routes to the spawn-fork ladder."""

    def __init__(self, *, fail_wave_id: str) -> None:
        self._fail_wave_id = fail_wave_id
        self.spawned: list[str] = []

    def __call__(self, ctx: MethodContext, wave_id: str) -> LaneDispatch | None:
        self.spawned.append(wave_id)
        assert ctx.state_path is not None
        state_path = Path(ctx.state_path)
        state = load_state(state_path)
        claim_wave(state, wave_id=wave_id, session_id=f"ses-{wave_id}", out_of_order=True)
        state_path.write_text(state.model_dump_json(), encoding="utf-8")
        if wave_id == self._fail_wave_id:
            exc = RuntimeSpawnError("cannot launch agent cli")
            exc.__cause__ = FileNotFoundError(2, "no such file or directory")
            raise exc
        return LaneDispatch(session_id=f"ses-{wave_id}", pgid=None, attempt=1)


def _write_state_with_closed(tmp_path: Path, closed_wave_id: str) -> Path:
    """Write the 3-wave state but with *closed_wave_id* already CLOSED on disk."""
    payload = _state_payload()
    payload["waves"][closed_wave_id]["status"] = "closed"
    payload["waves"][closed_wave_id]["outcome"] = "already done"
    payload["waves"][closed_wave_id]["closed_at"] = "2026-06-11T01:00:00Z"
    state = State.model_validate(payload)
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def test_terminal_wave_in_frontier_is_parked_not_respawned(tmp_path: Path) -> None:
    """A frontier wave already CLOSED on disk is PARKED (counted), never re-spawned.

    Regression for the stale fleet_run churn (P30-I21-W05): an armed / reattached
    frontier can carry a wave the operator has since closed. Without the park
    guard the loop re-claims it every round (a doomed claim), churning the run
    forever. The guard counts it as a parked failure + drops it so the run
    converges, and the spawner is never invoked for the terminal wave.
    """
    closed = _WAVE_IDS[0]
    state_path = _write_state_with_closed(tmp_path, closed)
    ctx = _ctx(state_path)
    spawner = _CleanSpawner()

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        classify=_classify_rate_limit,
        runtime_preference=["claude-code"],
    )

    # The run converged instead of churning the closed wave.
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # The closed wave was parked: counted as failed, never handed to the spawner.
    assert closed not in spawner.spawned
    assert run.counters.failed == 1
    # The two live waves spawned + closed cleanly.
    assert set(spawner.spawned) == set(_WAVE_IDS[1:])
    assert run.counters.closed == 2
    # The summary invariant still holds across the parked failure.
    counters = run.counters
    assert counters.closed + counters.failed + counters.blocked == counters.dispatched


def test_runtime_spawn_error_still_routes_to_fork_path(tmp_path: Path) -> None:
    """Boundary: a hard RuntimeSpawnError still forks (blocked), NOT counted as failed.

    The existing spawn-fork ladder owns RuntimeSpawnError -- it must keep routing
    through the fork queue (bumping ``forked`` + ``blocked``), NOT through the new
    dispatch-error FAILED path. The summary invariant still holds.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawner = _SpawnErrorSpawner(fail_wave_id=_WAVE_IDS[0])

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=spawner,
        watch=lambda c, lane: "closed",
        classify=_classify_rate_limit,
        runtime_preference=["claude-code"],
    )

    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # The hard spawn error forked (blocked tally), did NOT become a dispatch FAILED.
    assert run.counters.failed == 0
    assert run.counters.blocked >= 1
    assert run.counters.forked >= 1
    # Exactly one queued fork for the hard-failed wave.
    persisted = load_state(state_path).fleet_run
    assert persisted is not None
    assert [f.wave_id for f in persisted.forks] == [_WAVE_IDS[0]]
    # The invariant still holds across both buckets.
    counters = run.counters
    assert counters.closed + counters.failed + counters.blocked == counters.dispatched
