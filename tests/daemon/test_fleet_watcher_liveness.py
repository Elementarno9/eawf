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

from eawf.kernel.state.enums import AgentReportVerdict, Confidence, WaveStatus
from eawf.kernel.state.models import FleetLane, State
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.kinds.agent_report import ExecutorReportBody
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import build_liveness_watcher
from eawf.runtime.lock import portalock
from eawf.workflow.agent_report.store import append_agent_report
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I17-W01"
_FROZEN_TS = "2026-06-11T00:00:00Z"


def _state_payload(*, status: str = "in_progress", runtime: str = "codex") -> dict[str, Any]:
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
        "agent_sessions": {
            "ses-x": {
                "id": "ses-x",
                "role": "executor",
                "runtime": runtime,
                "scope_id": _WAVE_ID,
                "status": "active",
                "claimed_wave_ids": [],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": "2026-06-11T00:00:00Z",
                "ended_at": None,
                "summary": None,
                "agent_principal_id": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, *, status: str = "in_progress", runtime: str = "codex") -> Path:
    state = State.model_validate(_state_payload(status=status, runtime=runtime))
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


# ---- W49: a dead lane with a close-ready report resolves CLOSED, not forked ---


def _write_report(state_path: Path, *, verdict: AgentReportVerdict) -> None:
    """Append an executor report for the watched wave to the report store."""
    append_agent_report(
        state=load_state(state_path),
        state_path=state_path,
        session_id="ses-x",
        base_id=_WAVE_ID,
        body=ExecutorReportBody(
            role="executor",
            verdict=verdict,
            confidence=Confidence.MEDIUM,
            summary="greeting created",
            wave_id=_WAVE_ID,
            outcome="greeting.txt written",
        ),
    )


def test_dead_lane_with_close_ready_report_resolves_closed(tmp_path: Path) -> None:
    """W49: a dead pgid whose wave has a close-ready report resolves CLOSED.

    A SANDBOXED headless agent runs to completion in the synchronous dispatch
    (so its process group is legitimately dead here) but cannot run
    ``eawf wave close`` itself, so its wave stays IN_PROGRESS. The persisted
    close-ready report is the evidence the agent SUCCEEDED, so the watcher
    resolves ``"closed"`` instead of forking the wave on the stall deadline.
    """
    state_path = _write_state(tmp_path)
    _write_report(state_path, verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS)
    ctx = _ctx(state_path)
    watcher = build_liveness_watcher(
        is_alive=lambda pgid: False,  # the agent process is gone
        stall_deadline=30.0,
        poll_seconds=0.0,
        clock=_Clock(step=10.0),
        sleep=lambda _s: None,
    )
    assert watcher(ctx, _lane()) == "closed"


@pytest.mark.parametrize("runtime", ["codex", "claude-code"])
def test_dead_lane_close_on_report_is_runtime_generic(tmp_path: Path, runtime: str) -> None:
    """P30-I25-W01: dead-after-report resolves to one CLOSE (no re-dispatch), per runtime.

    The dispatch/bind/liveness chain carries no ``codex``-vs-``claude`` branch --
    a spawn that ran to completion (its process group reaped) but could not run
    ``eawf wave close`` itself is classified as NORMAL completion once its wave
    carries a close-ready report, identically for both runtimes. This pins that
    runtime-generic invariant: the watcher resolves ``"closed"`` (one close) and
    never ``"forked"`` (which is what drives a re-dispatch) whether the spawned
    executor was codex or headless claude.

    The close-on-report resolution itself shipped in I20-W49; this asserts the
    runtime-parity the P30-I25 lifecycle-consistency iter is premised on. The
    load-bearing R1 re-dispatch-storm fix lives in W02 (post-close dispatch drop)
    and W04 (repair gates on executor-fixable failures), not here.
    """
    state_path = _write_state(tmp_path, runtime=runtime)
    _write_report(state_path, verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS)
    ctx = _ctx(state_path)
    watcher = build_liveness_watcher(
        is_alive=lambda pgid: False,  # the spawned process group is gone
        stall_deadline=30.0,
        poll_seconds=0.0,
        clock=_Clock(step=10.0),
        sleep=lambda _s: None,
    )
    # A single watcher call yields exactly one terminal outcome; "closed" is the
    # no-re-dispatch resolution ("forked" is what a stall would return to drive
    # the repair ladder), so this is the one-close / zero-re-dispatch assertion.
    assert watcher(ctx, _lane()) == "closed"


def test_dead_lane_with_fail_report_still_forks(tmp_path: Path) -> None:
    """W49 boundary: a FAIL report is NOT close-ready, so the dead lane still forks.

    The report-aware close only fires when
    :func:`~eawf.workflow.verify.dispatch_close.verify_close_readiness` passes
    (a PASS / PASS_WITH_FOLLOWUPS verdict). A FAIL verdict is not close-ready,
    so a dead pgid resolves to a fork exactly as a report-less stall does -- the
    no-silent-close-on-failure invariant holds.
    """
    state_path = _write_state(tmp_path)
    _write_report(state_path, verdict=AgentReportVerdict.FAIL)
    ctx = _ctx(state_path)
    watcher = build_liveness_watcher(
        is_alive=lambda pgid: False,
        stall_deadline=30.0,
        poll_seconds=0.0,
        clock=_Clock(step=10.0),
        sleep=lambda _s: None,
    )
    assert watcher(ctx, _lane()) == "forked"


def test_close_on_behalf_finalizes_executor_session(tmp_path: Path) -> None:
    """W52: closing a wave on behalf of a sandboxed agent also closes its session.

    Close-on-behalf flips the WAVE to closed, but the sandboxed agent never ran
    its own session teardown, so its executor ``AgentSession`` would stay ACTIVE
    and the Watch parity grid would keep surfacing the closed wave as a live
    lane. The close-on-disk path must move the session to CLOSED so the watch
    surface drops it.
    """
    from eawf.kernel.state.enums import AgentSessionStatus
    from eawf.kernel.state.models import FleetRun, FleetRunState
    from eawf.runtime.daemon.methods.fleet import _Loop
    from eawf.surfaces.tui.modes.agent_watch import active_executor_sessions

    state_path = _write_state(tmp_path)  # wave IN_PROGRESS, ses-x ACTIVE executor
    (state_path.parent / "store").mkdir(parents=True, exist_ok=True)
    ctx = _ctx(state_path)
    loop = _Loop(
        ctx=ctx,
        run=FleetRun(
            run_state=FleetRunState.DRAINING,
            armed_at=datetime(2026, 6, 11, tzinfo=UTC),
        ),
        spawn=lambda *a, **k: None,  # unused by _close_wave_on_disk
        watch=lambda *a, **k: "closed",  # unused by _close_wave_on_disk
    )

    loop._close_wave_on_disk(_WAVE_ID)

    state = load_state(state_path)
    assert state.waves[_WAVE_ID].status is WaveStatus.CLOSED
    assert state.agent_sessions["ses-x"].status is AgentSessionStatus.CLOSED
    # The Watch parity grid lays out one tile per ACTIVE executor session, so a
    # finalized session drops the closed wave off the live-lane surface.
    assert active_executor_sessions(state) == []


def test_close_on_behalf_records_actuals_from_runtime_delta(tmp_path: Path) -> None:
    """W56: close-on-behalf records elapsed_eu + cost + tokens from captured runtime.

    W50 stamps the wave's runtime baseline + latest from the headless spawn, but
    close_wave records actuals only from passed params. Without threading the
    delta a headless wave closes with elapsed_eu=0 / cost=0 -- home effort reads
    0.0, variance -100%, the cost tab has no model. Seed a captured runtime and
    assert the close-on-behalf path records the real spend.
    """
    from datetime import timedelta

    from eawf.kernel.state.models import (
        FleetRun,
        FleetRunState,
        RuntimeBaseline,
        RuntimeLatest,
    )
    from eawf.runtime.daemon.methods.fleet import _Loop

    state_path = _write_state(tmp_path)
    (state_path.parent / "store").mkdir(parents=True, exist_ok=True)
    t0 = datetime(2026, 6, 11, tzinfo=UTC)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        wave = state.waves[_WAVE_ID]
        wave.runtime_baseline = RuntimeBaseline(
            api_duration_ms=0,
            cost_usd=0.0,
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            harness="codex",
            model="gpt-5.3-codex-spark",
            captured_at=t0,
        )
        wave.runtime_latest = RuntimeLatest(
            api_duration_ms=120_000,
            cost_usd=0.25,
            input_tokens=100,
            output_tokens=50,
            harness="codex",
            model="gpt-5.3-codex-spark",
            captured_at=t0 + timedelta(seconds=120),
        )
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    ctx = _ctx(state_path)
    loop = _Loop(
        ctx=ctx,
        run=FleetRun(run_state=FleetRunState.DRAINING, armed_at=t0),
        spawn=lambda *a, **k: None,
        watch=lambda *a, **k: "closed",
    )

    loop._close_wave_on_disk(_WAVE_ID)

    actual = load_state(state_path).actuals[_WAVE_ID]
    assert actual.elapsed_eu > 0.0  # derived from the 120s api-duration delta
    assert actual.actual_cost_usd == pytest.approx(0.25)
    assert actual.actual_tokens == 150  # (100 + 50) token delta
    assert actual.model == "gpt-5.3-codex-spark"


# ---- P30-I25-W03: orphan-claim reaper fails a lane-less wedged wave -----------
#
# Every other failure path is LANE-based (the liveness watcher forks a dead lane)
# or PROCESS-based (the kill ladder reaps a live lane's group). A wave that was
# CLAIMED + dispatched but never registered an in-flight lane -- its dispatch
# wedged before the lane landed -- has NO lane for either path, so it stays
# CLAIMED / IN_PROGRESS forever even after its driving run drained and its agent
# process group died (the R4 gap). The reaper closes that gap: at the drained-run
# seam it fails each genuinely-dead lane-less orphan, while sparing a healthy
# wave (a live pgid, no addressable pgid, or a still-in-flight lane).


def _seed_dead_session(state_path: Path, *, pid: int | None, runtime: str) -> None:
    """Attach a dispatched ``SessionAttempt`` (its child pid) to the watched wave.

    The lane-less pgid source the reaper probes: the spawned child records its
    pid on the matching ``SessionAttempt.subprocess_pid`` and is its own
    process-group leader, so that pid IS the pgid. ``pid=None`` seeds an attempt
    with no addressable group.
    """
    from eawf.kernel.state.models import SessionAttempt

    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        state.waves[_WAVE_ID].sessions[1] = SessionAttempt(
            attempt=1,
            runtime=runtime,
            session_id="ses-x",
            session_log_handle=f"urn:eawf:v1:session-log:{runtime}:abcdef",
            started_at=datetime(2026, 6, 11, tzinfo=UTC),
            subprocess_pid=pid,
        )
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))


def _drained_loop(ctx: MethodContext, *, liveness: Any, lanes: dict[str, Any] | None = None) -> Any:
    """Build a ``_Loop`` over a DRAINED (empty-lane) run with an injected probe."""
    from eawf.kernel.state.models import FleetRun, FleetRunState
    from eawf.runtime.daemon.methods.fleet import _Loop

    return _Loop(
        ctx=ctx,
        run=FleetRun(
            run_state=FleetRunState.DRAINING,
            armed_at=datetime(2026, 6, 11, tzinfo=UTC),
            lanes=lanes if lanes is not None else {},
        ),
        spawn=lambda *a, **k: None,  # unused by the reaper
        watch=lambda *a, **k: "closed",  # unused by the reaper
        liveness=liveness,
    )


@pytest.mark.parametrize("runtime", ["codex", "claude-code"])
@pytest.mark.parametrize("status", ["claimed", "in_progress"])
def test_orphan_claim_reaper_fails_dead_lane_less_wave(
    tmp_path: Path, runtime: str, status: str
) -> None:
    """R4: a lane-less CLAIMED / IN_PROGRESS wave with a dead pgid is driven FAILED.

    The wave dispatched (its child pid is on the SessionAttempt) but never
    registered an in-flight lane, so no lane-watcher fork nor kill-ladder path
    applies. Its process group reads dead and the driving run has drained, so the
    reaper drives it to a clean terminal FAILED (dropping it off
    ``active_wave_ids``) rather than leaving it wedged -- runtime-generic, since
    the dispatch/liveness chain carries no codex-vs-claude branch.
    """
    state_path = _write_state(tmp_path, status=status, runtime=runtime)
    _seed_dead_session(state_path, pid=9001, runtime=runtime)
    ctx = _ctx(state_path)
    loop = _drained_loop(ctx, liveness=lambda _pgid: False)  # the agent group is gone

    loop._reap_orphan_claims()

    state = load_state(state_path)
    assert state.waves[_WAVE_ID].status is WaveStatus.FAILED
    assert "9001" in (state.waves[_WAVE_ID].outcome or "")
    # A clean failure drops the reaped wave off the active-wave pointer.
    assert _WAVE_ID not in state.current.active_wave_ids


def test_orphan_claim_reaper_spares_live_pgid(tmp_path: Path) -> None:
    """R4 negative: a lane-less wave whose pgid reads ALIVE is never reaped.

    A live process group is a healthy in-flight agent (a slow lane the run lost
    track of, say) -- the reaper must not reap a wave it cannot prove is dead, so
    the wave stays CLAIMED exactly as the watcher spares a live lane.
    """
    state_path = _write_state(tmp_path, status="claimed")
    _seed_dead_session(state_path, pid=9001, runtime="codex")
    ctx = _ctx(state_path)
    loop = _drained_loop(ctx, liveness=lambda _pgid: True)  # the agent is alive

    loop._reap_orphan_claims()

    assert load_state(state_path).waves[_WAVE_ID].status is WaveStatus.CLAIMED


def test_orphan_claim_reaper_spares_wave_without_pgid(tmp_path: Path) -> None:
    """R4 negative: a wave with no addressable pgid is not probed and not reaped.

    A wave with no dispatched session (or an attempt that recorded no pid) has no
    process group to probe -- there is no liveness signal to act on, so the
    reaper leaves it untouched and never consults the probe (mirroring the
    watcher's no-pgid care).
    """
    state_path = _write_state(tmp_path, status="in_progress")  # no SessionAttempt seeded
    ctx = _ctx(state_path)
    probed: list[int] = []

    def _probe(pgid: int) -> bool:
        probed.append(pgid)
        return False

    loop = _drained_loop(ctx, liveness=_probe)
    loop._reap_orphan_claims()

    assert load_state(state_path).waves[_WAVE_ID].status is WaveStatus.IN_PROGRESS
    assert probed == []  # no addressable pgid -> the probe was never consulted


def test_orphan_claim_reaper_spares_wave_with_in_flight_lane(tmp_path: Path) -> None:
    """R4 negative: a wave that still holds an in-flight lane is not an orphan.

    The reap targets ONLY lane-less waves; a wave with a live lane in the current
    run is the lane-watcher's to resolve. Even with a dead-reading pgid the reaper
    skips it, so a healthy in-flight lane is never double-resolved.
    """
    state_path = _write_state(tmp_path, status="in_progress")
    _seed_dead_session(state_path, pid=9001, runtime="codex")
    ctx = _ctx(state_path)
    loop = _drained_loop(
        ctx,
        liveness=lambda _pgid: False,
        lanes={_WAVE_ID: _lane(pgid=9001)},  # the wave still holds a lane
    )

    loop._reap_orphan_claims()

    assert load_state(state_path).waves[_WAVE_ID].status is WaveStatus.IN_PROGRESS
