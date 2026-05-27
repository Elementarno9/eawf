"""Tests for earned-autonomy ceremony-mode recommendations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.kernel.state.models import AgentSession, CurrentPointers, Project, State
from eawf.workflow.lifecycle.ceremony import compute_ceremony
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave

_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
_OPERATOR_SESSION_ID = "SES-operator"


def _empty_state() -> State:
    """Return a minimal state for ceremony tests."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
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


def _seed_phase(state: State) -> None:
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    for idx in range(1, 5):
        plan_wave(
            state,
            wave_id=f"P01-I01-W0{idx}",
            iter_id="P01-I01",
            title=f"Wave {idx}",
            file_scopes=["src/"],
            effort_bucket="M",
        )


def _seed_operator_session(state: State) -> None:
    state.agent_sessions[_OPERATOR_SESSION_ID] = AgentSession(
        id=_OPERATOR_SESSION_ID,
        role=AgentSessionRole.OPERATOR,
        runtime="claude",
        scope_id="QR",
        status=AgentSessionStatus.ACTIVE,
        started_at=_T0,
    )


def _close_wave(
    state: State,
    wave_id: str,
    *,
    offset: int,
    operator_confirmed: bool,
) -> None:
    wave = state.waves[wave_id]
    wave.status = WaveStatus.CLOSED
    wave.closed_at = _T0 + timedelta(minutes=offset)
    wave.claim_session_id = _OPERATOR_SESSION_ID if operator_confirmed else None


def test_compute_ceremony_recommends_mode_c_without_operator_confirmed_history() -> None:
    """No confirmed streak means high ceremony."""
    state = _empty_state()
    _seed_phase(state)

    recommendation = compute_ceremony(state, wave_id="P01-I01-W01")

    assert recommendation.mode == "C"
    assert recommendation.operator_confirmed_counter == 0
    assert recommendation.closed_wave_count == 0
    assert "no operator-confirmed streak" in recommendation.reason


def test_compute_ceremony_recommends_mode_b_after_one_confirmed_wave() -> None:
    """One operator-confirmed predecessor earns mode B."""
    state = _empty_state()
    _seed_phase(state)
    _seed_operator_session(state)
    _close_wave(state, "P01-I01-W01", offset=1, operator_confirmed=True)

    recommendation = compute_ceremony(state, wave_id="P01-I01-W02")

    assert recommendation.mode == "B"
    assert recommendation.operator_confirmed_counter == 1
    assert recommendation.operator_confirmed_wave_ids == ["P01-I01-W01"]


def test_compute_ceremony_recommends_mode_a_after_three_confirmed_waves() -> None:
    """Three consecutive operator-confirmed waves earn mode A."""
    state = _empty_state()
    _seed_phase(state)
    _seed_operator_session(state)
    _close_wave(state, "P01-I01-W01", offset=1, operator_confirmed=True)
    _close_wave(state, "P01-I01-W02", offset=2, operator_confirmed=True)
    _close_wave(state, "P01-I01-W03", offset=3, operator_confirmed=True)

    recommendation = compute_ceremony(state, wave_id="P01-I01-W04")

    assert recommendation.mode == "A"
    assert recommendation.operator_confirmed_counter == 3
    assert recommendation.operator_confirmed_wave_ids == [
        "P01-I01-W03",
        "P01-I01-W02",
        "P01-I01-W01",
    ]


def test_compute_ceremony_counter_recomputes_from_state_on_each_call() -> None:
    """Counter is derived on demand, so later state changes alter the result."""
    state = _empty_state()
    _seed_phase(state)
    _seed_operator_session(state)
    _close_wave(state, "P01-I01-W01", offset=1, operator_confirmed=True)
    _close_wave(state, "P01-I01-W02", offset=2, operator_confirmed=True)
    _close_wave(state, "P01-I01-W03", offset=3, operator_confirmed=True)
    assert compute_ceremony(state, wave_id="P01-I01-W04").operator_confirmed_counter == 3

    state.waves["P01-I01-W03"].claim_session_id = None
    recommendation = compute_ceremony(state, wave_id="P01-I01-W04")

    assert recommendation.operator_confirmed_counter == 0
    assert recommendation.mode == "C"


def test_compute_ceremony_unknown_wave_raises_key_error() -> None:
    """Unknown target wave fails loudly."""
    state = _empty_state()
    _seed_phase(state)

    with pytest.raises(KeyError, match="unknown wave"):
        compute_ceremony(state, wave_id="P01-I01-W99")
