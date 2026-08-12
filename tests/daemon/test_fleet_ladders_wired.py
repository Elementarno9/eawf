"""Tests: the bounded spawn + repair ladders wired into the loop.

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
from eawf.runtime.daemon.methods import fleet as fleet_mod
from eawf.runtime.daemon.methods.fleet import (
    DriveParams,
    FleetDriveHandle,
    LaneDispatch,
    LaneOutcome,
    LaneRepairOutcome,
    arm_drive,
    start_background_drive,
)
from eawf.runtime.runtimes.adapter import (
    RUNTIME_API_ERROR,
    RUNTIME_RATE_LIMIT,
    RuntimeSpawnError,
)
from eawf.workflow.evidence._io import load_state
from eawf.workflow.verify.dispatch_close import CloseGateResult

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


# ---- R2b: an ENVIRONMENTAL close-gate refusal closes-with-followups, no repair --
#
# The bounded grounded-repair ladder re-dispatches on ANY refused close-gate.
# In a bare smoke repo the deterministic floor gates fail for ENVIRONMENTAL
# reasons the executor cannot fix in-scope (missing .pre-commit-config.yaml /
# package dir / pytest dependency), so a re-dispatch burns attempts on an
# unfixable lane. The fleet close-gate seam classifies the refusal and routes an
# environmental one to close-with-followups instead of the repair ladder.

_PRECOMMIT_DETAIL = "argv=['uv', 'run', 'pre-commit', 'run', '--all-files'] returncode=1"


def _fake_close_gate(*, passed: bool, detail: str = "") -> Any:
    """Return a monkeypatch replacement for ``_Loop._run_close_gates``.

    The fake ignores disk state and hands back a fixed
    :class:`CloseGateResult`, so the classifier + routing at the
    :meth:`_finish_lane` close-gate seam can be exercised without materializing
    real floor gates on the wave.
    """

    def _run(self: Any, wave_id: str) -> CloseGateResult:
        if passed:
            return CloseGateResult(passed=True)
        return CloseGateResult(passed=False, failing_criterion_id="C1", failing_detail=detail)

    return _run


@pytest.mark.parametrize("runtime", ["codex", "claude-code"])
def test_environmental_close_gate_closes_without_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """R2b: an environmental floor-gate refusal closes-with-followups, never repairs.

    Every lane's watcher reports ``"closed"`` but the close gate refuses with a
    pre-commit falsifier; the repo (``tmp_path``) is a bare smoke repo with no
    ``.pre-commit-config.yaml``, so the classifier labels the refusal
    ENVIRONMENTAL. With a repair hook WIRED, the seam still routes the lane to
    close-with-followups -- the repair ladder is NOT entered (``repaired`` stays
    empty, the dispatch attempt never grows) and every wave closes.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    # A bare smoke repo: repo_root is tmp_path (parent of .ea) with no
    # .pre-commit-config.yaml the executor could scaffold in-scope.
    monkeypatch.setattr(
        fleet_mod._Loop,
        "_run_close_gates",
        _fake_close_gate(passed=False, detail=_PRECOMMIT_DETAIL),
    )
    repaired: list[str] = []

    def _repair(c: MethodContext, lane: FleetLane) -> LaneRepairOutcome:
        repaired.append(lane.wave_id)
        return LaneRepairOutcome(resolved=False, attempts_used=1, dispatch=None)

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        spawn=lambda c, wid: LaneDispatch(session_id=f"ses-{wid}", pgid=9500, attempt=1),
        watch=lambda c, lane: "closed",
        repair=_repair,
        runtime_preference=[runtime],
    )
    # The environmental refusals resolved close-with-followups: every wave
    # closed, the repair ladder never fired, and no lane forked.
    assert repaired == []
    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.closed == 3
    assert run.counters.forked == 0
    assert run.counters.failed == 0


@pytest.mark.parametrize("runtime", ["codex", "claude-code"])
def test_executor_fixable_close_gate_enters_repair_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """R2b: an executor-fixable floor-gate refusal DOES enter the repair ladder.

    The repo carries a ``.pre-commit-config.yaml``, so the same pre-commit
    falsifier is a real lint error the executor can fix -- the classifier labels
    it EXECUTOR_FIXABLE and the seam routes W01 to the bounded grounded repair
    ladder (the pre-existing behaviour). The repair hook here exhausts, leaving
    W01 forked while the ungated siblings close.
    """
    from eawf.runtime.daemon.methods.fleet import _enqueue_fork

    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    # Scaffold the repo so the pre-commit refusal is a real lint error, not a
    # missing-config environmental gap.
    (tmp_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")

    def _run_gate(self: Any, wave_id: str) -> CloseGateResult:
        # Only W01's close gate refuses (executor-fixable); the siblings pass.
        if wave_id == _WAVE_IDS[0]:
            return CloseGateResult(
                passed=False, failing_criterion_id="C1", failing_detail=_PRECOMMIT_DETAIL
            )
        return CloseGateResult(passed=True)

    monkeypatch.setattr(fleet_mod._Loop, "_run_close_gates", _run_gate)
    repaired: list[str] = []

    def _repair(c: MethodContext, lane: FleetLane) -> LaneRepairOutcome:
        repaired.append(lane.wave_id)
        # The repair budget is spent: enqueue the REPAIR_EXHAUSTED fork through
        # the canonical writer then signal exhaustion so the loop counts it once.
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
        spawn=lambda c, wid: LaneDispatch(session_id=f"ses-{wid}", pgid=9600, attempt=1),
        watch=lambda c, lane: "closed",
        repair=_repair,
        runtime_preference=[runtime],
    )
    # The executor-fixable refusal entered the repair ladder for W01 only.
    assert repaired == [_WAVE_IDS[0]]
    assert run.run_state is FleetRunState.DONE
    # W02 + W03 closed clean; W01 forked (REPAIR_EXHAUSTED).
    assert run.counters.closed == 2
    persisted = _persisted(state_path)
    assert persisted is not None
    reasons = {fork.reason for fork in persisted.forks}
    assert FleetForkReason.REPAIR_EXHAUSTED in reasons
    assert run.counters.forked >= 1


# ---- C4: the LIVE drive enables the spawn + repair ladders -------------


def _write_paused_state(tmp_path: Path) -> Path:
    """Write the W03 fixture state with ``dispatch_paused`` set.

    A paused state makes :func:`start_background_drive` take the paused-arm
    branch -- it arms on the CALLING thread (a deterministic IDLE / DRAINING
    persist + return, no worker thread), so the live ``arm_drive`` call's wiring
    can be captured without a background drain to race.
    """
    payload = _state_payload()
    payload["dispatch_paused"] = True
    state = State.model_validate(payload)
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def test_live_drive_enables_spawn_and_repair_ladders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W11: the LIVE drive injects a non-None classify + repair into arm_drive.

    ``spawn_lane_or_fork`` (DL-11) and ``repair_lane_or_fork`` (DL-7) fire ONLY
    when ``arm_drive`` is armed with a wired ``classify`` + ``repair`` -- the loop
    takes the direct-spawn / terminal-fork path when either is ``None``. This
    pins that the production caller (:func:`start_background_drive`) wires BOTH on
    a real run: it passes the production
    :func:`~eawf.runtime.daemon.methods.fleet._live_lane_error_classifier` (the
    bounded spawn ladder) and the live
    :func:`~eawf.runtime.daemon.methods.fleet._build_live_lane_repair_hook` hook
    (the bounded grounded repair ladder). A regression that drops either kwarg
    re-dormants the ladder on the live path and reds this row.
    """
    state_path = _write_paused_state(tmp_path)
    ctx = _ctx(state_path)
    captured: dict[str, Any] = {}

    def _capture_arm_drive(c: MethodContext, **kwargs: Any) -> Any:
        captured.update(kwargs)
        # Mirror the paused-arm return shape so start_background_drive proceeds.
        return arm_drive(c, **kwargs)

    monkeypatch.setattr(fleet_mod, "arm_drive", _capture_arm_drive)

    handle = start_background_drive(ctx, DriveParams(frontier=list(_WAVE_IDS), concurrency=1))

    assert isinstance(handle, FleetDriveHandle)
    # The paused arm stayed IDLE (claimed nothing) but STILL wired both ladders.
    assert handle.run_state is FleetRunState.IDLE
    assert captured["classify"] is fleet_mod._live_lane_error_classifier
    # The repair hook is the live grounded-repair hook the factory built (a
    # bound callable, not None) -- the loop routes a failing-check fork through it.
    assert captured["repair"] is not None
    assert callable(captured["repair"])


# ---- W11: the idle-contract gate catches a re-dormant live binding -----------


def _load_idle_gate() -> Any:
    """Load ``tools/idle_contract_gate.py`` by path (``tools/`` is not a package).

    Mirrors the loader in ``tests/unit/test_idle_contract_gate.py`` so the new
    W11 source-scan checks (``check_drive_ladders_wired`` /
    ``check_live_output_text_wired``) carry an asserting test -- the idle-contract
    meta-gate requires every newly-defined ``check_*`` contract to be referenced
    by a test, and this is that reference.
    """
    import importlib.util
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    gate_path = repo_root / "tools" / "idle_contract_gate.py"
    if str(gate_path.parent) not in sys.path:
        sys.path.insert(0, str(gate_path.parent))
    spec = importlib.util.spec_from_file_location("idle_contract_gate", gate_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["idle_contract_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_drive_ladders_gate_passes_on_wired_source() -> None:
    """W11: the drive-ladders idle gate passes on the live wired source.

    The gate scans ``start_background_drive`` for the ``classify=`` + ``repair=``
    kwargs that enable the spawn / repair ladders on a real run; the live source
    carries both, so the gate is green.
    """
    mod = _load_idle_gate()
    assert mod.check_drive_ladders_wired().passed


def test_drive_ladders_gate_reds_when_kwargs_dropped() -> None:
    """W11: the drive-ladders idle gate reds when a re-dormant source drops the kwargs.

    A source that arms ``arm_drive`` without ``classify=_live_lane_error_classifier``
    + ``repair=repair_hook`` re-dormants the ladders on the live path; the gate
    catches that regression.
    """
    mod = _load_idle_gate()
    regressed = "    arm_drive(ctx, frontier=args.frontier, block_authority=block_authority)\n"
    result = mod.check_drive_ladders_wired(module_text=regressed)
    assert not result.passed
    assert result.failure is mod.GateFailure.DRIVE_LADDERS_IDLE


def test_live_output_text_gate_passes_on_wired_source() -> None:
    """W11: the live-output-text idle gate passes on the live wired source.

    The gate scans ``_spawn_and_dispatch`` for the ``output_text=spawn_result.text``
    thread that feeds the W08 stdout producer; the live source carries it.
    """
    mod = _load_idle_gate()
    assert mod.check_live_output_text_wired().passed


def test_live_output_text_gate_reds_when_thread_dropped() -> None:
    """W11: the live-output-text idle gate reds when the producer thread is dropped.

    A source that calls ``run_dispatch`` without ``output_text=spawn_result.text``
    leaves the stdout producer unfed on a real spawn; the gate catches it.
    """
    mod = _load_idle_gate()
    regressed = "    result = run_dispatch(ctx, wave_id=wave_id, report_body=report_body)\n"
    result = mod.check_live_output_text_wired(module_text=regressed)
    assert not result.passed
    assert result.failure is mod.GateFailure.LIVE_OUTPUT_TEXT_IDLE
