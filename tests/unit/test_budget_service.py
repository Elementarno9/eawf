"""Unit tests for :mod:`eawf.runtime.budget.service`.

The service layer mutates a live :class:`State` instance without touching
disk. These tests build a minimal in-memory state with one wave and
exercise the documented error and happy paths.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import ScopeKind, WaveStatus
from eawf.kernel.state.models import State, Wave
from eawf.runtime.budget.policy import BLOCK_TAG, WARN_TAG
from eawf.runtime.budget.service import (
    check_budget,
    record_consumption,
    set_budget,
)


def _state_with_wave(wave_id: str = "P01-I01-W01") -> State:
    """Build an in-memory ``State`` carrying one ``pending`` wave."""
    now = datetime.now(UTC)
    wave = Wave(
        id=wave_id,
        iter_id="P01-I01",
        title="t",
        status=WaveStatus.PENDING,
        opened_at=now,
    )
    payload = {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": now.isoformat(),
        "project": None,
        "current": {
            "project_code": None,
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {wave_id: wave.model_dump(mode="json")},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def test_set_budget_happy() -> None:
    state = _state_with_wave()
    wave = set_budget(state, "P01-I01-W01", 1000)
    assert wave.token_budget == 1000


def test_set_budget_negative_raises_value_error() -> None:
    state = _state_with_wave()
    with pytest.raises(ValueError, match="non-negative"):
        set_budget(state, "P01-I01-W01", -1)


def test_set_budget_unknown_wave_raises_key_error() -> None:
    state = _state_with_wave()
    with pytest.raises(KeyError, match="unknown wave"):
        set_budget(state, "P09-I09-W09", 1000)


def test_record_consumption_returns_warning_at_75() -> None:
    state = _state_with_wave()
    set_budget(state, "P01-I01-W01", 1000)
    wave, tag = record_consumption(state, "P01-I01-W01", 750)
    assert wave.tokens_consumed == 750
    assert tag == WARN_TAG


def test_record_consumption_returns_block_at_100() -> None:
    state = _state_with_wave()
    set_budget(state, "P01-I01-W01", 1000)
    wave, tag = record_consumption(state, "P01-I01-W01", 1000)
    assert wave.tokens_consumed == 1000
    assert tag == BLOCK_TAG


def test_record_consumption_returns_none_when_no_budget() -> None:
    state = _state_with_wave()
    wave, tag = record_consumption(state, "P01-I01-W01", 500)
    assert wave.tokens_consumed == 500
    assert tag is None


def test_record_consumption_negative_raises() -> None:
    state = _state_with_wave()
    with pytest.raises(ValueError, match="non-negative"):
        record_consumption(state, "P01-I01-W01", -5)


def test_record_consumption_unknown_wave_raises_key_error() -> None:
    state = _state_with_wave()
    with pytest.raises(KeyError, match="unknown wave"):
        record_consumption(state, "P09-I09-W09", 100)


def test_check_budget_readonly() -> None:
    state = _state_with_wave()
    set_budget(state, "P01-I01-W01", 1000)
    record_consumption(state, "P01-I01-W01", 800)
    # Repeated check does not mutate.
    assert check_budget(state, "P01-I01-W01") == WARN_TAG
    assert check_budget(state, "P01-I01-W01") == WARN_TAG
    assert state.waves["P01-I01-W01"].tokens_consumed == 800


def test_check_budget_unknown_wave_raises_key_error() -> None:
    state = _state_with_wave()
    with pytest.raises(KeyError, match="unknown wave"):
        check_budget(state, "P09-I09-W09")
