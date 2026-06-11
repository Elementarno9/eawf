"""Unit tests for :mod:`eawf.workflow.lifecycle.allocator`.

Covers the smallest-free-suffix invariant for phase/iter/wave id allocation,
boundary cases (empty state, single existing entry), and the saturation /
invalid-parent error paths.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import (
    IterStatus,
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    Iter,
    Phase,
    Project,
    State,
    Wave,
)
from eawf.workflow.lifecycle.allocator import (
    allocate_iter_id,
    allocate_phase_id,
    allocate_wave_id,
)


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _phase(state: State, pid: str) -> None:
    state.phases[pid] = Phase(
        id=pid,
        scope_id="QR",
        track_id=None,
        title=f"phase {pid}",
        status=PhaseStatus.ACTIVE,
        iter_ids=[],
        outcome_ids=[],
        opened_at=datetime.now(UTC),
        closed_at=None,
        audit_id=None,
    )


def _iter(state: State, iid: str, pid: str) -> None:
    state.iters[iid] = Iter(
        id=iid,
        phase_id=pid,
        title=f"iter {iid}",
        status=IterStatus.ACTIVE,
        wave_ids=[],
        estimate_id=None,
        audit_id=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
    )


def _wave(state: State, wid: str, iid: str) -> None:
    state.waves[wid] = Wave(
        id=wid,
        iter_id=iid,
        title=f"wave {wid}",
        status=WaveStatus.PENDING,
        deps=[],
        file_scopes=["src/"],
        claim_session_id=None,
        worktree_id=None,
        outcome=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
    )


# ---- Phase allocation -------------------------------------------------------


def test_allocate_phase_id_empty_state_returns_p01() -> None:
    state = _empty_state()
    assert allocate_phase_id(state) == "P01"


def test_allocate_phase_id_zero_pads_under_ten() -> None:
    state = _empty_state()
    _phase(state, "P01")
    assert allocate_phase_id(state) == "P02"


def test_allocate_phase_id_skips_used_returns_smallest_free() -> None:
    state = _empty_state()
    _phase(state, "P01")
    _phase(state, "P03")
    assert allocate_phase_id(state) == "P02"


def test_allocate_phase_id_max_two_digits() -> None:
    state = _empty_state()
    for n in range(1, 99):
        _phase(state, f"P{n:02d}")
    assert allocate_phase_id(state) == "P99"


def test_allocate_phase_id_saturated_raises() -> None:
    state = _empty_state()
    for n in range(1, 100):
        _phase(state, f"P{n:02d}")
    with pytest.raises(ValueError, match="saturated"):
        allocate_phase_id(state)


# ---- Iter allocation --------------------------------------------------------


def test_allocate_iter_id_empty_returns_i01() -> None:
    state = _empty_state()
    _phase(state, "P01")
    assert allocate_iter_id(state, "P01") == "P01-I01"


def test_allocate_iter_id_zero_padded_increments() -> None:
    state = _empty_state()
    _phase(state, "P01")
    _iter(state, "P01-I01", "P01")
    assert allocate_iter_id(state, "P01") == "P01-I02"


def test_allocate_iter_id_isolates_per_phase() -> None:
    state = _empty_state()
    _phase(state, "P01")
    _phase(state, "P02")
    _iter(state, "P01-I01", "P01")
    _iter(state, "P01-I02", "P01")
    # P02 still empty → next is P02-I01
    assert allocate_iter_id(state, "P02") == "P02-I01"


def test_allocate_iter_id_invalid_phase_raises() -> None:
    state = _empty_state()
    with pytest.raises(ValueError, match="invalid phase"):
        allocate_iter_id(state, "phase-three")


# ---- Wave allocation --------------------------------------------------------


def test_allocate_wave_id_empty_returns_w01() -> None:
    state = _empty_state()
    _phase(state, "P01")
    _iter(state, "P01-I01", "P01")
    assert allocate_wave_id(state, "P01-I01") == "P01-I01-W01"


def test_allocate_wave_id_skips_used() -> None:
    state = _empty_state()
    _phase(state, "P01")
    _iter(state, "P01-I01", "P01")
    _wave(state, "P01-I01-W01", "P01-I01")
    _wave(state, "P01-I01-W02", "P01-I01")
    assert allocate_wave_id(state, "P01-I01") == "P01-I01-W03"


def test_allocate_wave_id_isolates_per_iter() -> None:
    state = _empty_state()
    _phase(state, "P01")
    _iter(state, "P01-I01", "P01")
    _iter(state, "P01-I02", "P01")
    _wave(state, "P01-I01-W01", "P01-I01")
    # P01-I02 still empty → next is W01
    assert allocate_wave_id(state, "P01-I02") == "P01-I02-W01"


def test_allocate_wave_id_invalid_iter_raises() -> None:
    state = _empty_state()
    with pytest.raises(ValueError, match="invalid iter"):
        allocate_wave_id(state, "P01")


def test_allocate_wave_id_saturated_raises() -> None:
    state = _empty_state()
    _phase(state, "P01")
    _iter(state, "P01-I01", "P01")
    for n in range(1, 100):
        _wave(state, f"P01-I01-W{n:02d}", "P01-I01")
    with pytest.raises(ValueError, match="saturated"):
        allocate_wave_id(state, "P01-I01")
