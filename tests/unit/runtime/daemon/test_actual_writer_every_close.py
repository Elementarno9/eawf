"""Every production wave close writes the actual record its summary points at.

A close that creates the wave's :class:`ActualSummary` names a store record
(``current_store_record_id``). These tests drive each close path that persists
state itself -- the fleet close-on-behalf, the fleet fork approve-close, and
the wave land -- and assert that ``actual.jsonl`` carries that record. A
source scan pins the invariant for paths not written yet: outside the daemon
mutation pipeline, nothing calls the bare in-memory close.
"""

from __future__ import annotations

import ast
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

import eawf
from eawf.kernel.state.enums import ActualStatus, RiskTier, WaveStatus
from eawf.kernel.state.models import (
    ActualSummary,
    FleetFork,
    FleetForkReason,
    FleetRun,
    FleetRunState,
    State,
)
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.fleet import FleetForkResolution, _Loop, resolve_fork
from eawf.runtime.worktree.create import create_worktree
from eawf.runtime.worktree.wave_land import wave_land
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.wave_actual import close_wave_recording_actual
from tests.integration.test_wave_land import _commit_in
from tests.integration.test_worktree_create import _claimed_state, _make_repo

_WAVE_ID = "P05-I01-W01"
_T0 = datetime(2026, 9, 25, tzinfo=UTC)

#: The only modules allowed to call the bare in-memory close: the wrapper
#: that pairs it with the store write, and the daemon mutation pipeline, whose
#: commit step writes the record against its WAL.
_BARE_CLOSE_CALLERS = frozenset(
    {
        "eawf/workflow/lifecycle/wave_actual.py",
        "eawf/runtime/daemon/methods/state_apply.py",
    }
)


def _actual_records(state_path: Path) -> list[dict[str, object]]:
    path = state_path.parent / "store" / "actual.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_summary_backed(state: State, state_path: Path) -> None:
    summary = (state.actuals or {})[_WAVE_ID]
    records = _actual_records(state_path)
    assert [r["id"] for r in records] == [summary.current_store_record_id]
    assert records[0]["kind"] == "actual"
    assert records[0]["scope_id"] == _WAVE_ID


def _write_state(tmp_path: Path, state: State) -> Path:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def _load(state_path: Path) -> State:
    return State.model_validate_json(state_path.read_text(encoding="utf-8"))


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-09-25T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.7.0",
        event_path=state_path.parent / "store" / "event.jsonl",
        state_path=state_path,
    )


def test_close_wave_recording_actual_writes_record_for_created_summary(
    tmp_path: Path,
) -> None:
    state = _claimed_state()
    state_path = tmp_path / ".ea" / "state.json"

    wave = close_wave_recording_actual(
        state, state_path=state_path, wave_id=_WAVE_ID, outcome="done", actual_elapsed_eu=0.5
    )

    assert wave.status is WaveStatus.CLOSED
    _assert_summary_backed(state, state_path)
    assert _actual_records(state_path)[0]["payload"]["elapsed_eu"] == pytest.approx(0.5)


def test_close_wave_recording_actual_skips_operator_authored_summary(tmp_path: Path) -> None:
    state = _claimed_state()
    state.actuals = {
        _WAVE_ID: ActualSummary(
            id=f"ACT-{_WAVE_ID}",
            scope_id=_WAVE_ID,
            status=ActualStatus.ACTIVE,
            elapsed_eu=1.0,
            current_store_record_id="REC-operator",
            updated_at=_T0,
        )
    }
    state_path = tmp_path / ".ea" / "state.json"

    close_wave_recording_actual(state, state_path=state_path, wave_id=_WAVE_ID, outcome="done")

    assert _actual_records(state_path) == []
    assert state.actuals[_WAVE_ID].current_store_record_id == "REC-operator"


def test_close_wave_recording_actual_unknown_wave_raises_and_writes_nothing(
    tmp_path: Path,
) -> None:
    state = _claimed_state()
    state_path = tmp_path / ".ea" / "state.json"

    with pytest.raises(LifecycleError, match="unknown wave"):
        close_wave_recording_actual(
            state, state_path=state_path, wave_id="P05-I01-W99", outcome="done"
        )

    assert _actual_records(state_path) == []


def test_close_wave_recording_actual_negative_value_raises_and_writes_nothing(
    tmp_path: Path,
) -> None:
    state = _claimed_state()
    state_path = tmp_path / ".ea" / "state.json"

    with pytest.raises(LifecycleError, match="must be non-negative"):
        close_wave_recording_actual(
            state, state_path=state_path, wave_id=_WAVE_ID, outcome="done", actual_cost_usd=-0.01
        )

    assert _actual_records(state_path) == []


def test_fleet_close_on_behalf_writes_actual_record(tmp_path: Path) -> None:
    state_path = _write_state(tmp_path, _claimed_state())
    loop = _Loop(
        ctx=_ctx(state_path),
        run=FleetRun(run_state=FleetRunState.DRAINING, armed_at=_T0),
        spawn=lambda *a, **k: None,
        watch=lambda *a, **k: "closed",
    )

    loop._close_wave_on_disk(_WAVE_ID)

    closed = _load(state_path)
    assert closed.waves[_WAVE_ID].status is WaveStatus.CLOSED
    _assert_summary_backed(closed, state_path)


def test_fleet_fork_approve_close_writes_actual_record(tmp_path: Path) -> None:
    state = _claimed_state()
    state.fleet_run = FleetRun(
        run_state=FleetRunState.DRAINING,
        armed_at=_T0,
        forks=[
            FleetFork(
                wave_id=_WAVE_ID,
                attempt=1,
                risk_tier=RiskTier.HIGH,
                reason=FleetForkReason.HIGH_RISK_CLOSE,
                forked_at=_T0,
            )
        ],
    )
    state_path = _write_state(tmp_path, state)

    resolve_fork(
        _ctx(state_path),
        wave_id=_WAVE_ID,
        attempt=1,
        resolution=FleetForkResolution.APPROVE_CLOSE,
    )

    closed = _load(state_path)
    assert closed.waves[_WAVE_ID].status is WaveStatus.CLOSED
    _assert_summary_backed(closed, state_path)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for wave land")
def test_wave_land_close_writes_actual_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id=_WAVE_ID)
    _commit_in(repo / record.path, name="hello.txt", content="x\n", msg="add hello")

    result = wave_land(state, repo_root=repo, wave_id=_WAVE_ID)

    assert result.closed is True
    _assert_summary_backed(state, repo / ".ea" / "state.json")


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required for wave land")
def test_wave_land_deferred_close_writes_no_actual_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id=_WAVE_ID)
    _commit_in(repo / record.path, name="hello.txt", content="x\n", msg="add hello")

    result = wave_land(state, repo_root=repo, wave_id=_WAVE_ID, defer_close=True)

    assert result.closed is False
    assert _actual_records(repo / ".ea" / "state.json") == []


def test_close_wave_bare_callers_are_limited_to_the_recording_paths() -> None:
    src_root = Path(eawf.__file__).resolve().parent.parent
    callers: set[str] = set()
    for path in (src_root / "eawf").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "close_wave":
                callers.add(path.relative_to(src_root).as_posix())

    assert callers == _BARE_CLOSE_CALLERS
