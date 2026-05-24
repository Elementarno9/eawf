"""Unit tests for ``add_decision(..., supersedes=...)`` (P22-W03)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from eawf.kernel.state.enums import DecisionStatus
from eawf.surfaces.cli import errors as cli_errors
from eawf.workflow.evidence import _io, decision

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _state_path(tmp_path: Path) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return target


def test_add_decision_supersedes_flips_parent(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    decision.add_decision(
        state, decision_id="D010", scope_id="QR", summary="old", rationale="legacy choice"
    )

    record, event = decision.add_decision(
        state,
        decision_id="D011",
        scope_id="QR",
        summary="replacement",
        rationale="superior approach",
        supersedes="D010",
    )

    parent = state.decisions["D010"]
    child = state.decisions["D011"]
    assert parent.status == DecisionStatus.SUPERSEDED
    assert parent.superseded_by == "D011"
    assert child.status == DecisionStatus.ACTIVE
    assert child.superseded_by is None
    assert record.payload["supersedes"] == "D010"
    assert event.payload["event_type"] == "decision.add"
    assert "supersedes D010" in event.summary


def test_add_decision_supersedes_unknown_parent_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="unknown decision to supersede"):
        decision.add_decision(
            state,
            decision_id="D011",
            scope_id="QR",
            summary="orphan",
            rationale="no parent here",
            supersedes="D999",
        )


def test_add_decision_supersedes_self_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.UserError, match="cannot supersede itself"):
        decision.add_decision(
            state,
            decision_id="D012",
            scope_id="QR",
            summary="circular",
            rationale="self-reference",
            supersedes="D012",
        )


def test_add_decision_supersedes_already_superseded_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    decision.add_decision(state, decision_id="D010", scope_id="QR", summary="v1", rationale="r1")
    decision.add_decision(
        state,
        decision_id="D011",
        scope_id="QR",
        summary="v2",
        rationale="r2",
        supersedes="D010",
    )
    with pytest.raises(cli_errors.UserError, match="only ACTIVE decisions can be superseded"):
        decision.add_decision(
            state,
            decision_id="D012",
            scope_id="QR",
            summary="v3",
            rationale="r3",
            supersedes="D010",
        )
