"""Tests: fleet run-summary persistence (P30-I12-W10 / DL-10 / FA7).

Exercises the FA7 run-summary data the daemon-owned fleet auto-drain loop
persists on the terminal :class:`~eawf.kernel.state.models.FleetRun`: the final
counters (``closed`` / ``failed`` / ``blocked`` / ``forks_resolved``), the
EU / USD totals, the throughput (closed waves per hour) computed DAEMON-side,
and the :class:`~eawf.kernel.state.models.FleetTerminalReason`. The cockpit
READS these off the persisted run; the loop never recomputes them in the UI.

The success criteria under test:

* C1: on run end the FleetRun carries the final counters, the EU / USD totals,
  the throughput computed DAEMON-side as ``closed / elapsed_hours``, and the
  terminal_reason is set. A genuine watcher fork bumps ``failed`` while a DL-5
  safety-gate downgrade bumps ``blocked``; a reattach re-dispatch bumps
  ``forks_resolved``.
* C2: ``terminal_reason`` is a closed StrEnum and is ``None`` while the run is
  NOT DONE, so the summary surface can rely on its presence as the
  run-complete signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import eawf.runtime.daemon.methods.fleet as fleet_mod
from eawf.kernel.state.models import (
    FleetCounters,
    FleetLane,
    FleetRun,
    FleetRunState,
    FleetTerminalReason,
    State,
)
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import arm_drive, reattach
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_IDS = ["P30-I12-W01", "P30-I12-W02", "P30-I12-W03"]
_ARMED_AT = "2026-06-11T00:00:00Z"


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


def _state_payload(*, gate_kinds: dict[str, str] | None = None) -> dict[str, Any]:
    waves: dict[str, Any] = {}
    for wid in _WAVE_IDS:
        gates = [_gate_payload(gate_kinds[wid])] if gate_kinds and wid in gate_kinds else []
        waves[wid] = {
            "id": wid,
            "iter_id": "P30-I12",
            "title": f"Frontier wave {wid[-3:]}",
            "status": "pending",
            "deps": [],
            "blocks": [],
            "file_scopes": [],
            "success_criteria": [],
            "gates": gates,
            "agent_role": "executor",
            "effort_bucket": "M",
            "claim_session_id": None,
            "worktree_id": None,
            "token_budget": None,
            "tokens_consumed": 0,
            "outcome": None,
            "opened_at": _ARMED_AT,
            "closed_at": None,
        }
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _ARMED_AT,
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
                "opened_at": _ARMED_AT,
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
                "opened_at": _ARMED_AT,
                "closed_at": None,
            }
        },
        "waves": waves,
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, *, gate_kinds: dict[str, str] | None = None) -> Path:
    state = State.model_validate(_state_payload(gate_kinds=gate_kinds))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path | None) -> MethodContext:
    event_path = state_path.parent / "store" / "event.jsonl" if state_path is not None else None
    return MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        event_path=event_path,
        state_path=state_path,
    )


def _persisted_run(state_path: Path) -> FleetRun | None:
    return load_state(state_path).fleet_run


def _freeze_end_clock(monkeypatch: pytest.MonkeyPatch, *, minutes_after_arm: float) -> None:
    """Pin ``fleet.datetime.now`` so the run's elapsed window is deterministic.

    The loop's FIRST ``datetime.now(UTC)`` call stamps ``armed_at`` (in
    ``arm_drive``); every later call -- including the ``ended_at`` stamp in
    ``_finish_run`` -- returns a fixed instant N minutes later. That fixes the
    elapsed window (and so the throughput division) without touching the loop's
    own timestamps. A two-phase clock (arm instant, then the end instant) makes
    the elapsed window exactly ``minutes_after_arm`` regardless of how many
    intermediate ``now`` calls the loop makes.
    """
    armed = datetime.fromisoformat(_ARMED_AT.replace("Z", "+00:00"))
    ended = armed + timedelta(minutes=minutes_after_arm)
    calls = {"n": 0}

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            calls["n"] += 1
            instant = armed if calls["n"] == 1 else ended
            return instant if tz is None else instant.astimezone(tz)

    monkeypatch.setattr(fleet_mod, "datetime", _FrozenDatetime)


# ---- C1: drained run carries the final summary fields -----------------------


def test_drained_run_persists_summary_counters_and_throughput(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: a drained run carries final counters, totals, throughput, terminal_reason.

    Three clean closes over a 30-minute window: closed == 3, no failed / blocked
    / forks_resolved, the spend totals accrue the injected per-lane EU / USD, and
    the throughput is computed DAEMON-side as ``closed / elapsed_hours`` ==
    3 / 0.5 == 6.0 waves/hour. The terminal_reason is ``drained``.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    _freeze_end_clock(monkeypatch, minutes_after_arm=30.0)

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=3,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=lambda c, lane: "closed",
        spend=lambda c, wid: fleet_mod.LaneSpend(eu=2.0, usd=0.5),
    )

    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # Final counters.
    assert run.counters.closed == 3
    assert run.counters.failed == 0
    assert run.counters.blocked == 0
    assert run.counters.forks_resolved == 0
    # EU / USD totals (3 lanes x 2.0 EU / 0.5 USD).
    assert run.counters.spent_eu == pytest.approx(6.0)
    assert run.counters.spent_usd == pytest.approx(1.5)
    # Throughput computed daemon-side: closed / elapsed_hours == 3 / 0.5 == 6.0.
    assert run.elapsed_hours == pytest.approx(0.5)
    assert run.throughput == pytest.approx(6.0)
    assert run.ended_at is not None
    # The summary round-trips through the daemon canonical writer.
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.counters.closed == 3
    assert persisted.throughput == pytest.approx(6.0)
    assert persisted.terminal_reason is FleetTerminalReason.DRAINED


def test_genuine_fork_bumps_failed_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: a watcher-reported fork bumps ``failed`` (a real failure), not ``blocked``."""
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    _freeze_end_clock(monkeypatch, minutes_after_arm=60.0)

    # W01 forks (watcher reports a genuine fork); W02 + W03 close clean.
    def _watch(c: MethodContext, lane: FleetLane) -> str:
        return "forked" if lane.wave_id == "P30-I12-W01" else "closed"

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=3,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=_watch,
    )

    assert run.terminal_reason is FleetTerminalReason.DRAINED
    assert run.counters.closed == 2
    assert run.counters.forked == 1
    assert run.counters.failed == 1
    assert run.counters.blocked == 0
    # Throughput is over the closed count (2 / 1.0h == 2.0).
    assert run.throughput == pytest.approx(2.0)


def test_safety_gate_downgrade_bumps_blocked_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: a DL-5 safety-gate downgrade of a clean close bumps ``blocked``.

    A HIGH-tier (jury-gated) wave whose watcher reports ``closed`` is DOWNGRADED
    to a fork under the default advisory authority -- the lane never silently
    auto-closes. That fork is a safety BLOCK, not a genuine failure, so it bumps
    ``blocked`` rather than ``failed``.
    """
    state_path = _write_state(tmp_path, gate_kinds={"P30-I12-W01": "jury_verdict"})
    ctx = _ctx(state_path)
    _freeze_end_clock(monkeypatch, minutes_after_arm=60.0)

    run = arm_drive(
        ctx,
        frontier=["P30-I12-W01"],
        concurrency=1,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=lambda c, lane: "closed",
        block_authority=BlockAuthority.ADVISORY,
    )

    assert run.terminal_reason is FleetTerminalReason.DRAINED
    # The HIGH lane's clean close was held back: it forked as a safety block.
    assert run.counters.closed == 0
    assert run.counters.forked == 1
    assert run.counters.blocked == 1
    assert run.counters.failed == 0
    # No clean closes -> zero throughput.
    assert run.throughput == pytest.approx(0.0)


def test_reattach_redispatch_bumps_forks_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: a reattach sweep that re-dispatches a dead in-flight lane bumps forks_resolved.

    Seed a DRAINING run with one in-flight lane whose pgid is dead and whose
    wave is still PENDING. The sweep drops the falsely-running lane and
    re-dispatches it as a fresh lane -- a resolved fork. ``drive_after=False``
    keeps the sweep from driving on, so the counter is read in isolation.
    """
    state_path = _write_state(tmp_path)
    state = load_state(state_path)
    state.fleet_run = FleetRun(
        run_state=FleetRunState.DRAINING,
        concurrency=1,
        frontier=[],
        lanes={
            "P30-I12-W01": FleetLane(
                wave_id="P30-I12-W01",
                attempt=1,
                session_id="ses-P30-I12-W01",
                pgid=999_001,
                dispatched_at=_ARMED_AT,  # type: ignore[arg-type]
            )
        },
        counters=FleetCounters(),
        armed_at=_ARMED_AT,  # type: ignore[arg-type]
    )
    state.updated_at = datetime.now(UTC)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    ctx = _ctx(state_path)

    result = reattach(
        ctx,
        is_alive=lambda pgid: False,  # the seeded lane's child died during the blip
        spawn=lambda c, wid: f"ses-redispatch-{wid}",
        drive_after=False,
    )

    assert len(result.redispatched) == 1
    persisted = _persisted_run(state_path)
    assert persisted is not None
    assert persisted.counters.forks_resolved == 1


# ---- C2: terminal_reason is a closed StrEnum + None while not DONE -----------


def test_terminal_reason_is_closed_strenum() -> None:
    """C2: FleetTerminalReason is the closed StrEnum drained|converged|budget."""
    assert {r.value for r in FleetTerminalReason} == {"drained", "converged", "budget"}
    with pytest.raises(ValueError, match="not a valid"):
        FleetTerminalReason("paused")


def test_terminal_reason_none_until_done() -> None:
    """C2: a fresh / non-DONE run carries terminal_reason None + no summary fields.

    The summary surface relies on terminal_reason's presence as the run-complete
    signal, so it must stay None while the run is IDLE / DRAINING / PAUSED /
    HALTED -- and the daemon-stamped summary fields stay None alongside it.
    """
    for run_state in (
        FleetRunState.IDLE,
        FleetRunState.DRAINING,
        FleetRunState.PAUSED,
        FleetRunState.HALTED,
    ):
        run = FleetRun(run_state=run_state, armed_at=_ARMED_AT)  # type: ignore[arg-type]
        assert run.terminal_reason is None
        assert run.ended_at is None
        assert run.elapsed_hours is None
        assert run.throughput is None


def test_paused_run_has_no_terminal_reason(tmp_path: Path) -> None:
    """C2: a drive armed while dispatch_paused stays IDLE with terminal_reason None."""
    state = State.model_validate(_state_payload())
    state.dispatch_paused = True
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    ctx = _ctx(state_path)

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=2,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=lambda c, lane: "closed",
    )
    # Held IDLE: the run never reached DONE, so terminal_reason is the
    # run-complete signal's absence.
    assert run.run_state is FleetRunState.IDLE
    assert run.terminal_reason is None
    assert run.ended_at is None
    assert run.throughput is None


def test_converged_run_sets_terminal_reason_and_throughput(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1/C2: a kclean-converged run sets terminal_reason converged + the summary.

    A kclean K=1 drive over a single clean wave converges after one clean round
    WITHOUT draining the frontier; the converged terminal still stamps the
    summary fields (closed count, throughput, terminal_reason) daemon-side.
    """
    state_path = _write_state(tmp_path)
    ctx = _ctx(state_path)
    _freeze_end_clock(monkeypatch, minutes_after_arm=15.0)

    run = arm_drive(
        ctx,
        frontier=list(_WAVE_IDS),
        concurrency=1,
        convergence="kclean",
        kclean_k=1,
        spawn=lambda c, wid: f"ses-{wid}",
        watch=lambda c, lane: "closed",
    )

    assert run.run_state is FleetRunState.DONE
    assert run.terminal_reason is FleetTerminalReason.CONVERGED
    assert run.ended_at is not None
    assert run.elapsed_hours == pytest.approx(0.25)
    # One clean close in the converged round -> 1 / 0.25h == 4.0 waves/hour.
    assert run.counters.closed == 1
    assert run.throughput == pytest.approx(4.0)
