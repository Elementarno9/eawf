"""Tests: the auto-drain loop bounds lane-spawn failures to a clean fork (P30-I12-W11 / DL-11).

The fleet auto-drain loop spawns agents UNATTENDED, so a lane whose agent-cli
spawn keeps failing must terminate cleanly rather than respawn forever. This
suite pins the bounded agent-cli failure taxonomy
(:func:`~eawf.runtime.daemon.methods.fleet.spawn_lane_or_fork`):

* C1: a lane that keeps failing a RECOVERABLE spawn is retried at most
  ``max_total_attempts`` times (RETRY_SAME then SWITCH), then HALTED to a
  ``RETRY_EXHAUSTED`` fork rather than respawned forever. The bounded ladder
  stops on an auth HALT, a switch ladder run out of runtimes, or the attempt
  ceiling -- every terminal enqueues the typed fork and raises
  :class:`~eawf.runtime.daemon.methods.fleet.LaneRetryExhaustedError`.
* C2: a RUNTIME_SPAWN_ERROR (ENOENT / permission launch failure) and a
  SUBPROCESS_OOM each TERMINATE the lane cleanly on the FIRST failure with a
  typed reason + a failure-class fork (``runtime_spawn_error`` /
  ``subprocess_oom``) -- no retry, no infinite loop, no silent drop.

Every spawn is an INJECTED fake -- these tests never fork a real subprocess (no
network, no auth, no cost). The error classifier is a scripted stub standing in
for the resolved adapter's ``parse_error``.
"""

from __future__ import annotations

import asyncio
import errno
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import RiskTier
from eawf.kernel.state.models import FleetForkReason, State
from eawf.runtime.daemon.dispatch_runner import (
    SpawnFailureClass,
    classify_spawn_failure,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import (
    DEFAULT_MAX_TOTAL_ATTEMPTS,
    LaneDispatch,
    LaneRetryExhaustedError,
    spawn_exhausted_fork,
    spawn_failure_fork,
    spawn_lane_or_fork,
)
from eawf.runtime.runtimes.adapter import RuntimeSpawnError
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I12-W11"
_PRIMARY = "claude-code"
_FALLBACK = "codex"


# ---------------------------------------------------------------------------
# Scaffolding: a CLAIMED wave under an armed DRAINING run holding its lane.
# ---------------------------------------------------------------------------


def _state_payload() -> dict[str, Any]:
    """One CLAIMED wave under an armed DRAINING run holding its in-flight lane.

    The lane is registered in-flight on the run so a termination / exhaustion
    fork can drop it into the fork queue; the no-silent-drop assertions then read
    the resulting (lanes, forks) pair.
    """
    waves = {
        _WAVE_ID: {
            "id": _WAVE_ID,
            "iter_id": "P30-I12",
            "title": "Drive-loop failure-bounds wave",
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
    fleet_run = {
        "run_state": "draining",
        "concurrency": 1,
        "frontier": [],
        "lanes": {
            _WAVE_ID: {
                "wave_id": _WAVE_ID,
                "attempt": 1,
                "session_id": f"ses-{_WAVE_ID}",
                "pgid": None,
                "dispatched_at": "2026-06-11T00:00:00Z",
            }
        },
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


# ---------------------------------------------------------------------------
# Injected spawn fakes -- NEVER a real subprocess.
# ---------------------------------------------------------------------------


class _RecordingSpawn:
    """Records each spawn call; raises a scripted RuntimeSpawnError on demand.

    Tracks the runtimes the loop spawned against so the test asserts both the
    bound (call count) and the ladder shape (which runtimes were tried). A
    ``limit`` guards against an unbounded respawn: exceeding it raises an
    ``AssertionError`` so a regression that loops forever fails loud instead of
    hanging.
    """

    def __init__(self, *, error: RuntimeSpawnError, limit: int) -> None:
        self.runtimes: list[str] = []
        self._error = error
        self._limit = limit

    async def __call__(self, _ctx: MethodContext, _wave_id: str, runtime: str) -> LaneDispatch:
        if len(self.runtimes) >= self._limit:
            raise AssertionError(
                f"lane spawn called {len(self.runtimes) + 1} times but only "
                f"{self._limit} call(s) allowed (unbounded respawn?)"
            )
        self.runtimes.append(runtime)
        raise self._error


def _classify_rate_limit(_exc: RuntimeSpawnError, _runtime: str) -> str:
    """Classifier stub: every failure is a rate limit (RETRY_SAME)."""
    return "RUNTIME_RATE_LIMIT"


def _classify_server_error(_exc: RuntimeSpawnError, _runtime: str) -> str:
    """Classifier stub: every failure is a server error (SWITCH_RUNTIME)."""
    return "RUNTIME_SERVER_ERROR"


def _classify_auth_error(_exc: RuntimeSpawnError, _runtime: str) -> str:
    """Classifier stub: every failure is an auth error (HALT)."""
    return "RUNTIME_AUTH_ERROR"


def _recoverable_error() -> RuntimeSpawnError:
    """A transient, retryable spawn failure (ordinary non-zero exit)."""
    return RuntimeSpawnError("transient runtime failure", exit_status=1)


def _enoent_error() -> RuntimeSpawnError:
    """A HARD launch failure: the agent CLI binary is missing (ENOENT)."""
    exc = RuntimeSpawnError("cannot launch agent cli")
    exc.__cause__ = FileNotFoundError(errno.ENOENT, "no such file or directory")
    return exc


def _oom_error() -> RuntimeSpawnError:
    """A HARD subprocess-OOM failure: the child was SIGKILL-reaped by the OOM-killer."""
    return RuntimeSpawnError("subprocess killed", exit_status=-int(signal.SIGKILL))


# ---------------------------------------------------------------------------
# classify_spawn_failure: the pure taxonomy split (C2 grounding).
# ---------------------------------------------------------------------------


def test_classify_spawn_failure_enoent_launch_is_runtime_spawn_error() -> None:
    """A chained FileNotFoundError (ENOENT launch) classifies RUNTIME_SPAWN_ERROR."""
    assert classify_spawn_failure(_enoent_error()) is SpawnFailureClass.RUNTIME_SPAWN_ERROR


def test_classify_spawn_failure_permission_launch_is_runtime_spawn_error() -> None:
    """A chained PermissionError (EACCES launch) classifies RUNTIME_SPAWN_ERROR."""
    exc = RuntimeSpawnError("not executable")
    exc.__cause__ = PermissionError(errno.EACCES, "permission denied")
    assert classify_spawn_failure(exc) is SpawnFailureClass.RUNTIME_SPAWN_ERROR


def test_classify_spawn_failure_sigkill_exit_is_subprocess_oom() -> None:
    """A SIGKILL-reaped child (negative and 128+ conventions) classifies SUBPROCESS_OOM."""
    assert classify_spawn_failure(_oom_error()) is SpawnFailureClass.SUBPROCESS_OOM
    shell = RuntimeSpawnError("killed", exit_status=128 + int(signal.SIGKILL))
    assert classify_spawn_failure(shell) is SpawnFailureClass.SUBPROCESS_OOM


def test_classify_spawn_failure_ordinary_nonzero_exit_is_recoverable() -> None:
    """An ordinary non-zero exit (NOT an errno) classifies RECOVERABLE, not a launch failure.

    The boundary case: subprocess exit ``1`` must not be confused with the
    ``EPERM`` errno (also ``1``) -- the launch-failure signal is a chained
    OSError, not the exit code.
    """
    assert (
        classify_spawn_failure(RuntimeSpawnError("failed", exit_status=1))
        is SpawnFailureClass.RECOVERABLE
    )


def test_classify_spawn_failure_no_exit_status_is_recoverable() -> None:
    """A parse-level failure with no exit status classifies RECOVERABLE (the default path)."""
    assert (
        classify_spawn_failure(RuntimeSpawnError("unparseable envelope"))
        is SpawnFailureClass.RECOVERABLE
    )


# ---------------------------------------------------------------------------
# Pure fork builders -- the typed reason + evidence ref each terminal carries.
# ---------------------------------------------------------------------------


def test_spawn_failure_fork_runtime_spawn_error_carries_reason_and_detail() -> None:
    """The RUNTIME_SPAWN_ERROR builder produces a typed fork carrying the launch detail."""
    fork = spawn_failure_fork(
        SpawnFailureClass.RUNTIME_SPAWN_ERROR,
        "cannot launch agent cli: no such file",
        wave_id=_WAVE_ID,
        attempt=1,
        risk_tier=RiskTier.MECH,
    )
    assert fork.wave_id == _WAVE_ID
    assert fork.reason is FleetForkReason.RUNTIME_SPAWN_ERROR
    assert fork.risk_tier is RiskTier.MECH
    assert fork.evidence_ref == "cannot launch agent cli: no such file"


def test_spawn_failure_fork_subprocess_oom_maps_to_oom_reason() -> None:
    """The SUBPROCESS_OOM builder maps to the subprocess_oom fork reason."""
    fork = spawn_failure_fork(
        SpawnFailureClass.SUBPROCESS_OOM,
        "subprocess killed",
        wave_id=_WAVE_ID,
        attempt=2,
        risk_tier=RiskTier.HIGH,
    )
    assert fork.reason is FleetForkReason.SUBPROCESS_OOM
    assert fork.attempt == 2
    assert fork.risk_tier is RiskTier.HIGH


def test_spawn_failure_fork_rejects_recoverable_class() -> None:
    """A RECOVERABLE class is never terminated to a fork -- the builder raises KeyError."""
    with pytest.raises(KeyError):
        spawn_failure_fork(
            SpawnFailureClass.RECOVERABLE,
            "transient",
            wave_id=_WAVE_ID,
            attempt=1,
            risk_tier=RiskTier.MECH,
        )


def test_spawn_failure_fork_normalises_multiline_detail() -> None:
    """A multi-line stderr dump is single-lined onto the evidence ref."""
    fork = spawn_failure_fork(
        SpawnFailureClass.RUNTIME_SPAWN_ERROR,
        "line one\n  line two\t  line three",
        wave_id=_WAVE_ID,
        attempt=1,
        risk_tier=RiskTier.MECH,
    )
    assert fork.evidence_ref == "line one line two line three"
    assert "\n" not in (fork.evidence_ref or "")


def test_spawn_exhausted_fork_carries_error_class_and_detail() -> None:
    """The RETRY_EXHAUSTED builder carries the terminal error class + last detail."""
    fork = spawn_exhausted_fork(
        "RUNTIME_RATE_LIMIT",
        "rate limited; retry after 60s",
        wave_id=_WAVE_ID,
        attempt=1,
        risk_tier=RiskTier.MECH,
    )
    assert fork.reason is FleetForkReason.RETRY_EXHAUSTED
    assert fork.evidence_ref == "RUNTIME_RATE_LIMIT: rate limited; retry after 60s"


# ---------------------------------------------------------------------------
# C1: a lane that keeps failing is retried at most max_total_attempts then HALTED.
# ---------------------------------------------------------------------------


def test_retry_same_lane_is_bounded_then_halts_to_fork(tmp_path: Path) -> None:
    """C1: a RETRY_SAME (rate-limit) lane is bounded then HALTED to a RETRY_EXHAUSTED fork."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    max_total = 3
    # limit == max_total so a 4th spawn (an unbounded respawn) trips the guard.
    spawn = _RecordingSpawn(error=_recoverable_error(), limit=max_total)

    with pytest.raises(LaneRetryExhaustedError) as excinfo:
        asyncio.run(
            spawn_lane_or_fork(
                ctx,
                runtime=_PRIMARY,
                preference=[_PRIMARY],
                spawn=spawn,
                classify=_classify_rate_limit,
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.MECH,
                max_total_attempts=max_total,
            )
        )

    # Bounded: exactly max_total spawns, all on the SAME runtime (RETRY_SAME).
    assert spawn.runtimes == [_PRIMARY, _PRIMARY, _PRIMARY]
    exc = excinfo.value
    assert exc.wave_id == _WAVE_ID
    assert exc.attempts_used == max_total
    assert exc.error_class == "RUNTIME_RATE_LIMIT"

    # The lane HALTED to exactly one queued RETRY_EXHAUSTED fork (no respawn).
    run = load_state(state_path).fleet_run
    assert run is not None
    assert len(run.forks) == 1
    fork = run.forks[0]
    assert fork.wave_id == _WAVE_ID
    assert fork.reason is FleetForkReason.RETRY_EXHAUSTED
    # The in-flight lane was dropped into the fork queue, not left dangling.
    assert _WAVE_ID not in run.lanes
    assert run.counters.forked == 1


def test_switch_ladder_run_out_halts_to_fork(tmp_path: Path) -> None:
    """C1: a SWITCH_RUNTIME lane walks the preference ladder then HALTS when it runs out."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    # Two runtimes in the ladder, so the loop spawns primary then the fallback,
    # then has nowhere left to switch -> exhausts BEFORE the attempt ceiling.
    spawn = _RecordingSpawn(error=_recoverable_error(), limit=2)

    with pytest.raises(LaneRetryExhaustedError):
        asyncio.run(
            spawn_lane_or_fork(
                ctx,
                runtime=_PRIMARY,
                preference=[_PRIMARY, _FALLBACK],
                spawn=spawn,
                classify=_classify_server_error,
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.MECH,
                max_total_attempts=5,
            )
        )

    # The ladder switched primary -> fallback, then ran out (no third spawn).
    assert spawn.runtimes == [_PRIMARY, _FALLBACK]
    run = load_state(state_path).fleet_run
    assert run is not None
    assert len(run.forks) == 1
    assert run.forks[0].reason is FleetForkReason.RETRY_EXHAUSTED


def test_auth_halt_stops_at_once_to_fork(tmp_path: Path) -> None:
    """C1: an auth HALT stops on the FIRST failure -- no retry burn -- and forks."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawn = _RecordingSpawn(error=_recoverable_error(), limit=1)

    with pytest.raises(LaneRetryExhaustedError):
        asyncio.run(
            spawn_lane_or_fork(
                ctx,
                runtime=_PRIMARY,
                preference=[_PRIMARY, _FALLBACK],
                spawn=spawn,
                classify=_classify_auth_error,
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.MECH,
                max_total_attempts=5,
            )
        )

    # Auth never auto-retries: exactly one spawn, then HALT to a fork.
    assert spawn.runtimes == [_PRIMARY]
    run = load_state(state_path).fleet_run
    assert run is not None
    assert run.forks[0].reason is FleetForkReason.RETRY_EXHAUSTED


def test_clean_spawn_returns_dispatch_without_retry(tmp_path: Path) -> None:
    """The happy path: a clean spawn returns immediately with no fork enqueued."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    calls: list[str] = []

    async def _spawn(_ctx: MethodContext, _wave_id: str, runtime: str) -> LaneDispatch:
        calls.append(runtime)
        return LaneDispatch(session_id="sess-1", pgid=4321, attempt=1)

    result = asyncio.run(
        spawn_lane_or_fork(
            ctx,
            runtime=_PRIMARY,
            preference=[_PRIMARY],
            spawn=_spawn,
            classify=_classify_rate_limit,
            wave_id=_WAVE_ID,
            attempt=1,
            risk_tier=RiskTier.MECH,
        )
    )
    assert result.session_id == "sess-1"
    assert calls == [_PRIMARY]
    run = load_state(state_path).fleet_run
    assert run is not None
    assert run.forks == []


def test_spawn_lane_or_fork_rejects_zero_attempts(tmp_path: Path) -> None:
    """max_total_attempts < 1 fails fast at the boundary."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    async def _spawn(_ctx: MethodContext, _wave_id: str, _runtime: str) -> LaneDispatch:
        raise AssertionError("spawn must not run for an invalid attempt ceiling")

    with pytest.raises(ValueError, match="max_total_attempts must be >= 1"):
        asyncio.run(
            spawn_lane_or_fork(
                ctx,
                runtime=_PRIMARY,
                preference=[_PRIMARY],
                spawn=_spawn,
                classify=_classify_rate_limit,
                wave_id=_WAVE_ID,
                max_total_attempts=0,
            )
        )


def test_default_max_total_attempts_is_bounded() -> None:
    """The default attempt ceiling is a small, finite bound (never unbounded)."""
    assert DEFAULT_MAX_TOTAL_ATTEMPTS >= 1
    assert DEFAULT_MAX_TOTAL_ATTEMPTS <= 5


# ---------------------------------------------------------------------------
# C2: a hard spawn-error / OOM terminates the lane cleanly on the FIRST failure.
# ---------------------------------------------------------------------------


def test_runtime_spawn_error_terminates_lane_on_first_failure(tmp_path: Path) -> None:
    """C2: an ENOENT launch failure terminates the lane on the FIRST spawn -- no retry."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    # limit == 1: a SECOND spawn (any retry) would trip the unbounded-respawn guard.
    spawn = _RecordingSpawn(error=_enoent_error(), limit=1)

    with pytest.raises(RuntimeSpawnError):
        asyncio.run(
            spawn_lane_or_fork(
                ctx,
                runtime=_PRIMARY,
                preference=[_PRIMARY, _FALLBACK],
                spawn=spawn,
                classify=_classify_rate_limit,
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.MECH,
                max_total_attempts=5,
            )
        )

    # Exactly one spawn -- no retry, no switch (a missing binary cannot be retried).
    assert spawn.runtimes == [_PRIMARY]
    run = load_state(state_path).fleet_run
    assert run is not None
    assert len(run.forks) == 1
    fork = run.forks[0]
    assert fork.reason is FleetForkReason.RUNTIME_SPAWN_ERROR
    # The lane was dropped into the fork queue (clean termination, no silent drop).
    assert _WAVE_ID not in run.lanes
    assert run.counters.forked == 1


def test_subprocess_oom_terminates_lane_on_first_failure(tmp_path: Path) -> None:
    """C2: a SIGKILL-reaped (OOM) child terminates the lane on the FIRST spawn -- no respawn."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawn = _RecordingSpawn(error=_oom_error(), limit=1)

    with pytest.raises(RuntimeSpawnError):
        asyncio.run(
            spawn_lane_or_fork(
                ctx,
                runtime=_PRIMARY,
                preference=[_PRIMARY, _FALLBACK],
                spawn=spawn,
                classify=_classify_rate_limit,
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.HIGH,
                max_total_attempts=5,
            )
        )

    assert spawn.runtimes == [_PRIMARY]
    run = load_state(state_path).fleet_run
    assert run is not None
    assert len(run.forks) == 1
    fork = run.forks[0]
    assert fork.reason is FleetForkReason.SUBPROCESS_OOM
    assert fork.risk_tier is RiskTier.HIGH
    assert _WAVE_ID not in run.lanes


def test_hard_failure_never_silently_drops_the_lane(tmp_path: Path) -> None:
    """C2 (no-silent-drop): a hard-failed lane is removed ONLY into the fork queue.

    The load-bearing negative path: the in-flight lane is NOT dropped without a
    queued fork. The only terminal the lane reaches is the typed termination
    fork -- one queued fork, one fewer in-flight lane, balanced.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    before = load_state(state_path).fleet_run
    assert before is not None
    assert _WAVE_ID in before.lanes
    spawn = _RecordingSpawn(error=_enoent_error(), limit=1)

    with pytest.raises(RuntimeSpawnError):
        asyncio.run(
            spawn_lane_or_fork(
                ctx,
                runtime=_PRIMARY,
                preference=[_PRIMARY],
                spawn=spawn,
                classify=_classify_rate_limit,
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.MECH,
                max_total_attempts=3,
            )
        )

    after = load_state(state_path).fleet_run
    assert after is not None
    # The lane left the registry ONLY into the fork queue -- nothing dropped.
    assert _WAVE_ID not in after.lanes
    assert [f.wave_id for f in after.forks] == [_WAVE_ID]


def test_fork_attempt_key_matches_dispatch_attempt(tmp_path: Path) -> None:
    """A non-1 dispatch attempt is carried onto the termination fork's key."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    spawn = _RecordingSpawn(error=_oom_error(), limit=1)

    with pytest.raises(RuntimeSpawnError):
        asyncio.run(
            spawn_lane_or_fork(
                ctx,
                runtime=_PRIMARY,
                preference=[_PRIMARY],
                spawn=spawn,
                classify=_classify_rate_limit,
                wave_id=_WAVE_ID,
                attempt=2,
                risk_tier=RiskTier.MECH,
                max_total_attempts=3,
            )
        )

    run = load_state(state_path).fleet_run
    assert run is not None
    assert run.forks[0].attempt == 2


def test_forked_datetime_is_timezone_aware(tmp_path: Path) -> None:
    """The termination fork stamps a timezone-aware forked_at (no naive datetime)."""
    fork = spawn_failure_fork(
        SpawnFailureClass.SUBPROCESS_OOM,
        "killed",
        wave_id=_WAVE_ID,
        attempt=1,
        risk_tier=RiskTier.MECH,
    )
    assert fork.forked_at.tzinfo is not None
    assert fork.forked_at <= datetime.now(UTC)
