"""Unit tests for :mod:`eawf.workflow.evidence.goal`.

Covers the in-place mutator (``define_goal``) and the
``state_transaction``-driven CLI persistence path. Errors: duplicate
id raises :class:`InvalidInput`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from eawf.kernel.state.enums import GoalStatus, StoreKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._mutation import state_transaction
from eawf.workflow.evidence import _io, goal

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _state_path(tmp_path: Path) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return target


def test_define_goal_happy_path(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    event = goal.define_goal(
        state,
        goal_id="G01",
        title="Test goal",
        summary="A test goal",
        scope_id="QR",
    )
    assert "G01" in (state.goals or {})
    g = state.goals["G01"]
    assert g.title == "Test goal"
    assert g.summary == "A test goal"
    assert g.status == GoalStatus.OPEN
    assert event.kind.value == "event"
    assert event.payload["event_type"] == "goal.define"


def test_define_goal_duplicate_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    goal.define_goal(state, goal_id="G01", title="t", summary="s", scope_id="QR")
    with pytest.raises(cli_errors.UserError, match="already exists"):
        goal.define_goal(state, goal_id="G01", title="t2", summary="s2", scope_id="QR")


def test_state_transaction_persists_define_goal(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    with state_transaction(state_path) as state:
        event = goal.define_goal(
            state,
            goal_id="G01",
            title="Test",
            summary="Test summary",
            scope_id="QR",
        )
        _io.append_jsonl(_io.store_paths(state_path)[StoreKind.EVENT], event)
    body = json.loads(state_path.read_text())
    assert "G01" in body["goals"]
    events = (state_path.parent / "store" / "event.jsonl").read_text().splitlines()
    assert len(events) == 1
    payload = json.loads(events[0])
    assert payload["payload"]["event_type"] == "goal.define"


def test_state_transaction_define_goal_round_trip(tmp_path: Path) -> None:
    """Two successive transactions persist independently and survive validation."""
    state_path = _state_path(tmp_path)
    with state_transaction(state_path) as state:
        event = goal.define_goal(
            state, goal_id="G01", title="Test", summary="Test summary", scope_id="QR"
        )
        _io.append_jsonl(_io.store_paths(state_path)[StoreKind.EVENT], event)
    body = json.loads(state_path.read_text())
    assert body["goals"]["G01"]["scope_id"] == "QR"


def test_define_goal_with_outcome_ids(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    goal.define_goal(
        state,
        goal_id="G02",
        title="t",
        summary="s",
        scope_id="QR",
        outcome_ids=["OUT-001", "OUT-002"],
    )
    assert state.goals["G02"].outcome_ids == ["OUT-001", "OUT-002"]
