"""Tests: watcher liveness probe + stall deadline (P30-I17-W05).

The lane watcher polled the on-disk wave status FOREVER, so a dead agent (its
process group reaped without the wave ever flipping terminal) wedged the lane
and stalled the whole drain. These assertions confirm the W05 fix: the watcher
probes the lane's process-group liveness each poll and resolves a dead-but-
unflipped lane to a fork once a bounded stall deadline elapses, while a healthy
slow lane is NOT killed by the probe.

- C1: a lane whose pgid reads dead without the wave flipping resolves to a
  watcher fork within the deadline rather than wedging.
- C2: a healthy slow lane (pgid alive, wave still in progress) is NOT forked by
  the probe -- it keeps polling and closes cleanly when the wave flips.
- C3: a lane with no addressable pgid keeps the pre-W05 status-poll behaviour
  (no liveness signal to act on).
- C4: a transiently-dead-then-alive lane does not fork (the deadline resets).

The liveness probe, clock, and sleep are INJECTED so the deadline is exercised
deterministically -- no real process, no real wall-clock wait.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import FleetLane, State
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import build_liveness_watcher
from eawf.runtime.lock import portalock
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I17-W01"
_FROZEN_TS = "2026-06-11T00:00:00Z"


def _state_payload(*, status: str = "in_progress") -> dict[str, Any]:
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
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P30-I17",
                "title": "In-flight wave",
                "status": status,
                "deps": [],
                "blocks": [],
                "file_scopes": [],
                "success_criteria": [],
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": "ses-x",
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-06-11T00:00:00Z",
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, *, status: str = "in_progress") -> Path:
    state = State.model_validate(_state_payload(status=status))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        event_path=state_path.parent / "store" / "event.jsonl",
        state_path=state_path,
    )


def _lane(*, pgid: int | None = 9001) -> FleetLane:
    return FleetLane(
        wave_id=_WAVE_ID,
        attempt=1,
        session_id="ses-x",
        pgid=pgid,
        dispatched_at=datetime(2026, 6, 11, tzinfo=UTC),
    )


def _flip_wave(state_path: Path, status: WaveStatus) -> None:
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        state.waves[_WAVE_ID].status = status
        state.updated_at = datetime.now(UTC)
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))


class _Clock:
    """A controllable monotonic clock that advances a fixed step per read."""

    def __init__(self, step: float) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        t = self._t
        self._t += self._step
        return t


# ---- C1: a dead lane resolves to a fork within the deadline ------------------


def test_dead_lane_forks_within_deadline(tmp_path: Path) -> None:
    """C1: a lane whose pgid reads dead without flipping resolves to a fork.

    The wave never flips terminal and the pgid probe reads dead, so once the
    stall deadline elapses the watcher resolves the wedged lane to a fork rather
    than polling forever.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    # Clock advances 10s per read; deadline 30s -> forks after a few polls.
    watcher = build_liveness_watcher(
        is_alive=lambda pgid: False,  # the agent process is gone
        stall_deadline=30.0,
        poll_seconds=0.0,
        clock=_Clock(step=10.0),
        sleep=lambda _s: None,
    )
    outcome = watcher(ctx, _lane())
    assert outcome == "forked"


# ---- C2: a healthy slow lane is NOT killed by the probe ----------------------


def test_healthy_slow_lane_not_killed_by_probe(tmp_path: Path) -> None:
    """C2: a healthy slow lane (pgid alive) is not forked; it closes when flipped.

    The pgid probe reads alive on every poll, so the watcher keeps polling the
    status rather than forking. After a few polls the wave flips CLOSED in state
    and the watcher returns ``closed`` -- the slow lane was never killed.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    polls = [0]

    def _alive(_pgid: int) -> bool:
        return True  # the agent is alive, just slow

    def _sleep(_s: float) -> None:
        polls[0] += 1
        # After 3 polls the slow agent finishes and the wave flips CLOSED.
        if polls[0] == 3:
            _flip_wave(state_path, WaveStatus.CLOSED)

    watcher = build_liveness_watcher(
        is_alive=_alive,
        stall_deadline=30.0,
        poll_seconds=0.0,
        clock=_Clock(step=100.0),  # far past the deadline -- proves alive never forks
        sleep=_sleep,
    )
    outcome = watcher(ctx, _lane())
    assert outcome == "closed"
    assert polls[0] >= 3


# ---- C3: a no-pgid lane keeps the pre-W05 status-poll behaviour --------------


def test_no_pgid_lane_polls_status_only(tmp_path: Path) -> None:
    """C3: a lane with no addressable pgid is not liveness-probed.

    An unkillable lane (``pgid=None``) has nothing to probe, so the watcher only
    polls the status -- a dead probe could not apply. The wave flips CLOSED after
    a poll and the watcher returns ``closed``.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    probed: list[int] = []

    def _probe(pgid: int) -> bool:
        probed.append(pgid)
        return False

    def _sleep(_s: float) -> None:
        _flip_wave(state_path, WaveStatus.CLOSED)

    watcher = build_liveness_watcher(
        is_alive=_probe,
        stall_deadline=1.0,
        poll_seconds=0.0,
        clock=_Clock(step=100.0),
        sleep=_sleep,
    )
    outcome = watcher(ctx, _lane(pgid=None))
    assert outcome == "closed"
    # The probe was never consulted -- there is no addressable pgid.
    assert probed == []


# ---- C4: a transiently-dead-then-alive lane does not fork --------------------


def test_transient_dead_then_alive_does_not_fork(tmp_path: Path) -> None:
    """C4: a probe that reads dead once then alive resets the deadline.

    A single dead read (a transiently-unreadable group that recovered) must not
    trip the stall fork: the deadline resets when the probe reads alive again,
    so the lane keeps polling and closes cleanly when the wave flips.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    reads = [0]

    def _probe(_pgid: int) -> bool:
        reads[0] += 1
        # Dead on the first read, alive afterwards.
        return reads[0] != 1

    def _sleep(_s: float) -> None:
        if reads[0] >= 4:
            _flip_wave(state_path, WaveStatus.CLOSED)

    watcher = build_liveness_watcher(
        is_alive=_probe,
        stall_deadline=5.0,
        poll_seconds=0.0,
        clock=_Clock(step=100.0),  # would trip the deadline if it never reset
        sleep=_sleep,
    )
    outcome = watcher(ctx, _lane())
    # The single dead read reset to alive, so the lane never forked.
    assert outcome == "closed"


def test_dead_lane_that_flips_closed_within_grace_does_not_fork(tmp_path: Path) -> None:
    """C1 boundary: a clean exit that closes the wave within the grace closes, not forks.

    A dead pgid whose wave flips CLOSED before the deadline elapses returns
    ``closed`` -- the grace window gives the post-exit close-write time to land,
    so a clean agent exit is not mistaken for a wedged lane.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    polls = [0]

    def _sleep(_s: float) -> None:
        polls[0] += 1
        # The agent exited (pgid dead) and its close-write lands on poll 2,
        # within the grace window.
        if polls[0] == 2:
            _flip_wave(state_path, WaveStatus.CLOSED)

    watcher = build_liveness_watcher(
        is_alive=lambda pgid: False,
        stall_deadline=30.0,
        poll_seconds=0.0,
        clock=_Clock(step=5.0),  # 5s/read, under the 30s deadline before the flip
        sleep=_sleep,
    )
    outcome = watcher(ctx, _lane())
    assert outcome == "closed"
