"""Tests: fleet lane-only blocking-fork raise + FleetFork inbox queue (P30-I12-W06 / DL-6).

Exercises the DL-6 blocking-fork path the fleet auto-drain loop applies when a
lane must pause for operator judgement -- a high-risk (UI) close, an
uncalibrated-jury (HIGH) advisory, or a needs-user split. The loop pauses ONLY
the offending lane (removes it from the in-flight slot, enqueues a typed
:class:`~eawf.kernel.state.models.FleetFork`) while the sibling lanes keep
draining, and the operator resolves each queued fork via one of the four closed
:class:`~eawf.kernel.state.models.FleetForkResolution` paths.

The success criteria under test:

* C1: a fork pauses exactly its own lane (one :class:`FleetFork` carrying the
  wave, RiskTier, reason, and evidence ref is appended) while the sibling lanes
  keep draining clean.
* C2: ``approve_close`` resolves the fork to ``CLOSED``, ``re_dispatch``
  re-queues the wave onto the frontier, ``skip`` leaves it ``PENDING`` and frees
  the lane, ``abort_run`` halts the whole run; an unmatched fork raises a typed
  :class:`~eawf.workflow.lifecycle._errors.LifecycleError`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import RiskTier, WaveStatus
from eawf.kernel.state.models import (
    FleetFork,
    FleetForkReason,
    FleetForkResolution,
    FleetLane,
    FleetRunState,
    State,
)
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import (
    arm_drive,
    classify_fork_reason,
    resolve_fork,
    resolve_fork_in_queue,
)
from eawf.runtime.lock import portalock
from eawf.workflow.evidence._io import load_state
from eawf.workflow.lifecycle._errors import LifecycleError
from tests._session_helpers import claim_wave_with_session as claim_wave
from tests.conftest import make_claim_criterion

pytestmark = pytest.mark.integration

# A mech (file_exists), a HIGH jury (jury_verdict), a UI visual (tui_flow), and
# a second mech sibling that always drains clean.
_WAVE_KINDS = {
    "P30-I12-W01": "file_exists",
    "P30-I12-W02": "jury_verdict",
    "P30-I12-W03": "tui_flow",
    "P30-I12-W04": "file_exists",
}
_WAVE_IDS = list(_WAVE_KINDS)


def _gate_payload(kind: str) -> dict[str, Any]:
    return {
        "id": "GATE-01",
        "criterion_id": "CR-01",
        "kind": kind,
        "args": {},
        "policy": "block",
        "cadence": "every-wave",
        "required": True,
        "timeout_s": None,
    }


def _state_payload() -> dict[str, Any]:
    waves: dict[str, Any] = {}
    for wid, kind in _WAVE_KINDS.items():
        criterion = make_claim_criterion("CR-01").model_copy(update={"gate_ids": ["GATE-01"]})
        waves[wid] = {
            "id": wid,
            "iter_id": "P30-I12",
            "title": f"Frontier wave {wid[-3:]}",
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [criterion.model_dump(mode="json")],
            "gates": [_gate_payload(kind)],
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
            "iter_id": "P30-I12",
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


def _claiming_spawn(ctx: MethodContext, wave_id: str) -> str:
    """Test spawner that REALLY claims the wave (mirrors the live default).

    The fork-resolution paths drive a real wave transition (``approve_close``
    closes the wave, ``skip`` / ``re_dispatch`` reset it to PENDING), so the
    enqueued lane must leave its wave in a CLAIMED status. This persists the
    claim through the state portalock so the on-disk wave is CLAIMED when the
    fork enqueues.
    """
    assert ctx.state_path is not None
    state_path = Path(ctx.state_path)
    with portalock.acquire(state_path, timeout=5.0):
        state = load_state(state_path)
        claim_wave(state, wave_id=wave_id, session_id=f"ses-{wave_id}", out_of_order=True)
        atomic_write_json_locked(state_path, state.model_dump(mode="json"))
    return f"ses-{wave_id}"


def _drive_to_forks(state_path: Path) -> None:
    """Arm the drive under advisory authority so the HIGH + UI lanes fork."""
    ctx = _ctx(state_path)
    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=4,
        spawn=_claiming_spawn,
        watch=lambda c, lane: "closed",
        block_authority=BlockAuthority.ADVISORY,
    )
    assert run.run_state is FleetRunState.DONE


# --- C1: a blocking fork pauses exactly its own lane; siblings keep draining ---


def test_fork_pauses_own_lane_and_enqueues_typed_fork(tmp_path: Path) -> None:
    """C1: under advisory authority the HIGH + UI lanes pause to typed forks while
    the two mech siblings drain clean.
    """
    state_path = _write_state(tmp_path)
    _drive_to_forks(state_path)
    run = load_state(state_path).fleet_run
    assert run is not None
    # The two mech siblings auto-closed; the HIGH + UI lanes were held.
    assert run.counters.closed == 2
    assert run.counters.forked == 2
    assert run.counters.blocked == 2
    # No lane is left in-flight -- the held lanes moved to the fork queue.
    assert run.lanes == {}
    # Exactly one typed FleetFork per held lane, each carrying its wave + tier +
    # reason + a non-empty evidence ref.
    by_wave = {fork.wave_id: fork for fork in run.forks}
    assert set(by_wave) == {"P30-I12-W02", "P30-I12-W03"}
    high = by_wave["P30-I12-W02"]
    assert high.risk_tier is RiskTier.HIGH
    assert high.reason is FleetForkReason.UNCALIBRATED_JURY
    assert high.evidence_ref is not None and high.evidence_ref != ""
    ui = by_wave["P30-I12-W03"]
    assert ui.risk_tier is RiskTier.UI
    assert ui.reason is FleetForkReason.HIGH_RISK_CLOSE
    assert ui.evidence_ref is not None and ui.evidence_ref != ""


def test_needs_user_split_pauses_lane(tmp_path: Path) -> None:
    """C1: a needs-user watch pauses that one lane to a NEEDS_USER_SPLIT fork while
    the sibling lanes drain.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)

    def _watch(c: MethodContext, lane: FleetLane) -> str:
        # The first mech wave reports a needs-user split; every other lane closes.
        return "needs_user" if lane.wave_id == "P30-I12-W01" else "closed"

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=4,
        spawn=_claiming_spawn,
        watch=_watch,
        block_authority=BlockAuthority.BLOCKING,
    )
    assert run.run_state is FleetRunState.DONE
    # W01 paused needs-user; the HIGH + UI lanes auto-closed under blocking; W04
    # closed clean -> exactly one fork.
    by_wave = {fork.wave_id: fork for fork in run.forks}
    assert set(by_wave) == {"P30-I12-W01"}
    assert by_wave["P30-I12-W01"].reason is FleetForkReason.NEEDS_USER_SPLIT
    assert run.counters.closed == 3


def test_evidence_ref_can_be_pinned_by_reader(tmp_path: Path) -> None:
    """C1: the injected fork-evidence reader supplies the queued fork's ref."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    arm_drive(
        ctx,
        frontier=["P30-I12-W02"],
        concurrency=1,
        spawn=_claiming_spawn,
        watch=lambda c, lane: "closed",
        block_authority=BlockAuthority.ADVISORY,
        fork_evidence=lambda c, wid, reason: f"docs/forks/{wid}.md",
    )
    run = load_state(state_path).fleet_run
    assert run is not None
    assert run.forks[0].evidence_ref == "docs/forks/P30-I12-W02.md"


# --- C2: the four resolutions + the typed unknown-fork error -------------------


def test_resolve_approve_close_closes_wave(tmp_path: Path) -> None:
    """C2: approve-close resolves the fork to CLOSED + dequeues it."""
    state_path = _write_state(tmp_path)
    _drive_to_forks(state_path)
    ctx = _ctx(state_path)
    result = resolve_fork(
        ctx,
        wave_id="P30-I12-W02",
        attempt=1,
        resolution=FleetForkResolution.APPROVE_CLOSE,
    )
    assert result.run_state is FleetRunState.DONE
    state = load_state(state_path)
    assert state.waves["P30-I12-W02"].status is WaveStatus.CLOSED
    run = state.fleet_run
    assert run is not None
    assert resolve_fork_in_queue(run, wave_id="P30-I12-W02", attempt=1) is None
    assert run.counters.forks_resolved == 1


def test_resolve_re_dispatch_requeues_wave(tmp_path: Path) -> None:
    """C2: re-dispatch resets the wave to PENDING + re-queues it onto the frontier."""
    state_path = _write_state(tmp_path)
    _drive_to_forks(state_path)
    ctx = _ctx(state_path)
    resolve_fork(
        ctx,
        wave_id="P30-I12-W03",
        attempt=1,
        resolution=FleetForkResolution.RE_DISPATCH,
    )
    state = load_state(state_path)
    assert state.waves["P30-I12-W03"].status is WaveStatus.PENDING
    run = state.fleet_run
    assert run is not None
    assert "P30-I12-W03" in run.frontier
    assert run.counters.forks_resolved == 1


def test_resolve_skip_leaves_wave_pending(tmp_path: Path) -> None:
    """C2: skip leaves the wave PENDING + frees the lane WITHOUT re-queuing it."""
    state_path = _write_state(tmp_path)
    _drive_to_forks(state_path)
    ctx = _ctx(state_path)
    resolve_fork(
        ctx,
        wave_id="P30-I12-W02",
        attempt=1,
        resolution=FleetForkResolution.SKIP,
    )
    state = load_state(state_path)
    assert state.waves["P30-I12-W02"].status is WaveStatus.PENDING
    run = state.fleet_run
    assert run is not None
    # Not re-queued, and the fork is dequeued; the other fork stays queued.
    assert "P30-I12-W02" not in run.frontier
    assert resolve_fork_in_queue(run, wave_id="P30-I12-W02", attempt=1) is None
    assert resolve_fork_in_queue(run, wave_id="P30-I12-W03", attempt=1) is not None
    # Skip is not a resolution -- it leaves the wave for a later decision.
    assert run.counters.forks_resolved == 0


def test_resolve_abort_halts_run(tmp_path: Path) -> None:
    """C2: abort-run clears every queued fork and halts the whole run."""
    state_path = _write_state(tmp_path)
    _drive_to_forks(state_path)
    ctx = _ctx(state_path)
    result = resolve_fork(
        ctx,
        wave_id="P30-I12-W02",
        attempt=1,
        resolution=FleetForkResolution.ABORT_RUN,
    )
    assert result.run_state is FleetRunState.HALTED
    assert result.forks_open == 0
    run = load_state(state_path).fleet_run
    assert run is not None
    assert run.run_state is FleetRunState.HALTED
    assert run.forks == []


def test_resolve_unknown_fork_raises(tmp_path: Path) -> None:
    """C2: resolving a fork that does not match any queued (wave, attempt) raises."""
    state_path = _write_state(tmp_path)
    _drive_to_forks(state_path)
    ctx = _ctx(state_path)
    # W01 auto-closed -- there is no fork queued for it.
    with pytest.raises(LifecycleError, match="no fork queued for wave"):
        resolve_fork(
            ctx,
            wave_id="P30-I12-W01",
            attempt=1,
            resolution=FleetForkResolution.APPROVE_CLOSE,
        )
    # A stale attempt on a real fork wave also misses.
    with pytest.raises(LifecycleError, match="no fork queued for wave"):
        resolve_fork(
            ctx,
            wave_id="P30-I12-W02",
            attempt=99,
            resolution=FleetForkResolution.APPROVE_CLOSE,
        )


def test_resolve_no_run_armed_raises(tmp_path: Path) -> None:
    """C2 (error path): resolving with no fleet run armed raises a typed error."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    with pytest.raises(LifecycleError, match="no fleet run armed"):
        resolve_fork(
            ctx,
            wave_id="P30-I12-W02",
            attempt=1,
            resolution=FleetForkResolution.SKIP,
        )


# --- the pure classify_fork_reason matrix -------------------------------------


def test_classify_fork_reason_needs_user_any_tier() -> None:
    """A needs-user watch forks every tier under any authority."""
    for tier in RiskTier:
        for authority in (BlockAuthority.ADVISORY, BlockAuthority.BLOCKING):
            assert (
                classify_fork_reason("needs_user", tier, block_authority=authority)
                is FleetForkReason.NEEDS_USER_SPLIT
            )


def test_classify_fork_reason_high_ui_under_advisory() -> None:
    """A clean close of a HIGH / UI lane under advisory authority is split by band."""
    assert (
        classify_fork_reason("closed", RiskTier.HIGH, block_authority=BlockAuthority.ADVISORY)
        is FleetForkReason.UNCALIBRATED_JURY
    )
    assert (
        classify_fork_reason("closed", RiskTier.UI, block_authority=BlockAuthority.ADVISORY)
        is FleetForkReason.HIGH_RISK_CLOSE
    )


def test_classify_fork_reason_no_pause_on_clean_or_genuine_fork() -> None:
    """A clean auto-closing close + a genuine watcher fork do NOT pause to a fork."""
    # mech / med always auto-close, and high / ui auto-close under blocking.
    assert (
        classify_fork_reason("closed", RiskTier.MECH, block_authority=BlockAuthority.ADVISORY)
        is None
    )
    assert (
        classify_fork_reason("closed", RiskTier.MED, block_authority=BlockAuthority.ADVISORY)
        is None
    )
    assert (
        classify_fork_reason("closed", RiskTier.HIGH, block_authority=BlockAuthority.BLOCKING)
        is None
    )
    # A genuine watcher fork is a terminal failure, never an operator pause.
    for tier in RiskTier:
        assert classify_fork_reason("forked", tier, block_authority=BlockAuthority.ADVISORY) is None


def test_resolve_fork_in_queue_matches_wave_attempt() -> None:
    """``resolve_fork_in_queue`` resolves only the matching (wave, attempt) pair."""
    from eawf.kernel.state.models import FleetRun

    fork = FleetFork(
        wave_id="P30-I12-W02",
        attempt=2,
        risk_tier=RiskTier.HIGH,
        reason=FleetForkReason.UNCALIBRATED_JURY,
        evidence_ref="urn:eawf:v1:fork:P30-I12-W02:uncalibrated_jury",
        forked_at=datetime.now(UTC),
    )
    run = FleetRun(forks=[fork], armed_at=datetime.now(UTC))
    assert resolve_fork_in_queue(run, wave_id="P30-I12-W02", attempt=2) is fork
    # A stale attempt or an unknown wave misses; a None run misses.
    assert resolve_fork_in_queue(run, wave_id="P30-I12-W02", attempt=1) is None
    assert resolve_fork_in_queue(run, wave_id="P30-I12-W99", attempt=2) is None
    assert resolve_fork_in_queue(None, wave_id="P30-I12-W02", attempt=2) is None
