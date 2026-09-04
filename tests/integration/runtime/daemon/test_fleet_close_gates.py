"""Tests: the fleet clean-close path runs the wave's deterministic gates.

The fleet clean-close path flipped a wave to CLOSED on the agent's own
close-ready report -- a pure status flip that never ran the wave's own gates.
A wave carrying a ``command_exit_zero`` gate therefore auto-closed without the
command ever running (the A5 critical). These assertions confirm the W19 fix:
before ``_close_wave_on_disk``, ``_Loop._finish_lane`` scores the wave's
deterministic gates through the shared ordered oracle, so

- CR-01: a lane whose wave carries a FAILING ``command_exit_zero`` gate never
  flips status on a clean self-report -- it routes to the repair/fork ladder
  (the terminal-fork arm with no repair hook wired, or the repair re-dispatch
  arm with one wired);
- CR-02: a lane whose wave carries a PASSING gate closes AND mints exactly one
  ``deterministic`` / ``pass`` evidence row bound to the wave, while a MECH
  gateless wave keeps today's status-flip close with no evidence minted.

The suite drives the real ordered oracle + real compile-gate + real gate runner
against a throwaway git checkout, so the deterministic-gate pipeline is proven
end-to-end -- no live daemon, no injected gate fake.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.enums import StoreKind, WaveStatus
from eawf.kernel.state.models import (
    FleetLane,
    FleetRun,
    FleetRunState,
    State,
)
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import LaneDispatch, LaneRepairOutcome, _Loop
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.integration

_WAVE_ID = "P30-I23-W19"
_CRITERION = "CR-01"
_GATE = "GATE-01"
_T0 = datetime(2026, 7, 2, tzinfo=UTC)


def _criterion() -> dict[str, Any]:
    """A required deterministic criterion gated by one ``command_exit_zero`` gate."""
    return {
        "id": _CRITERION,
        "text": "the deterministic close command exits zero against the checkout",
        "kind": "contract",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": [_GATE],
        "required": True,
        "quality_dimension": "functional_suitability",
        "measurable_signal": "the close command exits zero under the fleet clean-close gate run",
    }


def _gate(*, argv: list[str]) -> dict[str, Any]:
    """A ``command_exit_zero`` deterministic gate running *argv* over the whole tree."""
    return {
        "id": _GATE,
        "criterion_id": _CRITERION,
        "kind": "command_exit_zero",
        "args": {"argv": argv, "scope": "all"},
        "policy": "block",
        "cadence": "every-wave",
        "required": True,
    }


def _state_payload(
    *,
    criteria: list[dict[str, Any]],
    gates: list[dict[str, Any]],
    status: str = "in_progress",
) -> dict[str, Any]:
    """A minimal valid State with one IN_PROGRESS MECH wave + an ACTIVE session."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _T0.isoformat(),
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
            "iter_id": "P30-I23",
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
                "iter_ids": ["P30-I23"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P30-I23": {
                "id": "P30-I23",
                "phase_id": "P30",
                "title": "Max-hardening",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P30-I23",
                "title": "run deterministic gates on the fleet clean-close path",
                "status": status,
                "deps": [],
                "blocks": [],
                "file_scopes": [],
                "success_criteria": criteria,
                "gates": gates,
                "agent_role": "executor",
                "effort_bucket": "M",
                "claim_session_id": "ses-x",
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {
            "ses-x": {
                "id": "ses-x",
                "role": "executor",
                "runtime": "codex",
                "scope_id": _WAVE_ID,
                "status": "active",
                "claimed_wave_ids": [],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": _T0.isoformat(),
                "ended_at": None,
                "summary": None,
                "agent_principal_id": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }


def _init_git_repo(root: Path) -> None:
    """Init a git repo with one empty commit so the gate's scope resolution passes."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.t",
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=root,
        check=True,
        env=env,
    )


def _write_state(tmp_path: Path, payload: dict[str, Any]) -> Path:
    state = State.model_validate(payload)
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "store").mkdir(parents=True, exist_ok=True)
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-07-02T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.6.0",
        event_path=state_path.parent / "store" / "event.jsonl",
        state_path=state_path,
    )


def _lane() -> FleetLane:
    return FleetLane(
        wave_id=_WAVE_ID,
        attempt=1,
        session_id="ses-x",
        pgid=None,
        dispatched_at=_T0,
    )


def _loop(ctx: MethodContext, **kwargs: Any) -> _Loop:
    return _Loop(
        ctx=ctx,
        run=FleetRun(
            run_state=FleetRunState.DRAINING,
            armed_at=_T0,
            lanes={_WAVE_ID: _lane()},
        ),
        spawn=lambda *a, **k: None,  # unused: the lane is already in flight
        watch=lambda *a, **k: "closed",  # the agent left a close-ready report
        **kwargs,
    )


def _deterministic_pass_rows(state_path: Path) -> list[EvidenceRecord]:
    """Decode the ``deterministic`` / ``pass`` evidence rows (empty when absent)."""
    path = store_path(state_path, StoreKind.EVIDENCE)
    if not path.exists():
        return []
    rows: list[EvidenceRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = EvidenceRecord.model_validate(orjson.loads(line)["payload"])
        if record.evidence_kind == "deterministic" and record.status == "pass":
            rows.append(record)
    return rows


# --------------------------------------------------------------------------- #
# CR-01: a failing command_exit_zero gate never closes on a clean self-report.
# --------------------------------------------------------------------------- #


def test_fleet_close_blocks_on_failing_command_exit_zero_gate(tmp_path: Path) -> None:
    """CR-01: a failing gate routes the clean-close lane to the fork ladder.

    The watcher resolved ``"closed"`` from the agent's close-ready report, but
    the wave's ``command_exit_zero`` gate exits non-zero. With no repair hook
    wired the lane takes the terminal-fork arm of the repair/fork ladder: the
    wave stays IN_PROGRESS (never flips to CLOSED on the self-report) and the
    run tallies a genuine failure, and NO deterministic-pass evidence is minted.
    """
    _init_git_repo(tmp_path)
    state_path = _write_state(
        tmp_path,
        _state_payload(
            criteria=[_criterion()],
            gates=[_gate(argv=["git", "rev-parse", "--verify", "eawf-missing-ref"])],
        ),
    )
    loop = _loop(_ctx(state_path))

    outcome = loop._finish_lane(_WAVE_ID)

    assert outcome == "forked"
    assert loop.run.counters.closed == 0
    assert loop.run.counters.forked == 1
    assert loop.run.counters.failed == 1
    assert load_state(state_path).waves[_WAVE_ID].status is WaveStatus.IN_PROGRESS
    assert _deterministic_pass_rows(state_path) == []


def test_fleet_close_failing_gate_routes_to_repair_hook(tmp_path: Path) -> None:
    """CR-01: a failing gate with a repair hook wired enters the repair path.

    A deterministic gate refusal is a genuine failing check, so with a repair
    hook wired the lane is routed through the bounded grounded repair ladder
    (not paused to a DL-6 operator fork). The resolved repair re-registers the
    lane in flight under its incremented attempt, so ``_finish_lane`` reports
    ``"running"`` and the wave is NOT closed on the self-report.
    """
    _init_git_repo(tmp_path)
    state_path = _write_state(
        tmp_path,
        _state_payload(
            criteria=[_criterion()],
            gates=[_gate(argv=["git", "rev-parse", "--verify", "eawf-missing-ref"])],
        ),
    )
    repaired_lanes: list[FleetLane] = []

    def _repair(_ctx_arg: MethodContext, lane: FleetLane) -> LaneRepairOutcome:
        repaired_lanes.append(lane)
        return LaneRepairOutcome(
            resolved=True,
            attempts_used=1,
            dispatch=LaneDispatch(session_id="ses-x", pgid=None, attempt=lane.attempt + 1),
        )

    loop = _loop(_ctx(state_path), repair=_repair)

    outcome = loop._finish_lane(_WAVE_ID)

    assert outcome == "running"
    assert [lane.wave_id for lane in repaired_lanes] == [_WAVE_ID]
    assert loop.run.lanes[_WAVE_ID].attempt == 2  # re-registered up the ladder
    assert loop.run.counters.closed == 0
    assert load_state(state_path).waves[_WAVE_ID].status is WaveStatus.IN_PROGRESS


# --------------------------------------------------------------------------- #
# CR-02: a passing gate closes + mints evidence; a gateless wave flips status.
# --------------------------------------------------------------------------- #


def test_fleet_close_passing_gate_closes_and_mints_evidence(tmp_path: Path) -> None:
    """CR-02: a passing gate closes the wave and mints one bound evidence row.

    The watcher resolved ``"closed"`` and the wave's ``command_exit_zero`` gate
    exits zero, so the lane closes on behalf of the sandboxed agent AND mints
    exactly one ``deterministic`` / ``pass`` :class:`EvidenceRecord` scoped to
    the wave, carrying the gate + criterion refs.
    """
    _init_git_repo(tmp_path)
    state_path = _write_state(
        tmp_path,
        _state_payload(
            criteria=[_criterion()],
            gates=[_gate(argv=["git", "rev-parse", "--verify", "HEAD"])],
        ),
    )
    loop = _loop(_ctx(state_path))

    outcome = loop._finish_lane(_WAVE_ID)

    assert outcome == "closed"
    assert loop.run.counters.closed == 1
    assert loop.run.counters.forked == 0
    assert load_state(state_path).waves[_WAVE_ID].status is WaveStatus.CLOSED

    rows = _deterministic_pass_rows(state_path)
    assert len(rows) == 1
    row = rows[0]
    assert row.scope_id == _WAVE_ID
    assert row.produced_by == "tool"
    assert _GATE in row.refs
    assert _CRITERION in row.refs


def test_fleet_close_gateless_mech_wave_flips_status(tmp_path: Path) -> None:
    """CR-02: a MECH gateless wave keeps today's status-flip close, minting nothing.

    A wave with no success criteria / gates has no deterministic gate to run, so
    the clean-close gate is a no-op: the wave flips to CLOSED exactly as it did
    pre-W19 and no deterministic-pass evidence row is minted.
    """
    _init_git_repo(tmp_path)
    state_path = _write_state(tmp_path, _state_payload(criteria=[], gates=[]))
    loop = _loop(_ctx(state_path))

    outcome = loop._finish_lane(_WAVE_ID)

    assert outcome == "closed"
    assert loop.run.counters.closed == 1
    assert load_state(state_path).waves[_WAVE_ID].status is WaveStatus.CLOSED
    assert _deterministic_pass_rows(state_path) == []
