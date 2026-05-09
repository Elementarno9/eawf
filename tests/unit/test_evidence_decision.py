"""Unit tests for :mod:`eawf.evidence.decision`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.evidence import _io, decision
from eawf.state.enums import DecisionStatus, StoreKind

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _state_path(tmp_path: Path) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return target


def test_add_decision_happy(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    record, event = decision.add_decision(
        state,
        decision_id="D012",
        scope_id="QR",
        summary="Use phase-bundled PR",
        rationale="Coupled refactor",
        alternatives=["per-wave PR"],
    )
    d = state.decisions["D012"]
    assert d.status == DecisionStatus.ACTIVE
    assert d.alternatives == ["per-wave PR"]
    assert record.payload["rationale"] == "Coupled refactor"
    assert event.payload["event_type"] == "decision.add"


def test_add_decision_duplicate_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    decision.add_decision(state, decision_id="D012", scope_id="QR", summary="s", rationale="r")
    with pytest.raises(cli_errors.InvalidInput, match="already exists"):
        decision.add_decision(
            state, decision_id="D012", scope_id="QR", summary="s2", rationale="r2"
        )


def test_add_decision_empty_rationale_raises(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    with pytest.raises(cli_errors.InvalidInput, match="rationale"):
        decision.add_decision(
            state, decision_id="D012", scope_id="QR", summary="s", rationale="   "
        )


def test_list_decisions_filters_by_scope(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    state = _io.load_state(state_path)
    decision.add_decision(state, decision_id="D001", scope_id="QR", summary="a", rationale="r")
    decision.add_decision(state, decision_id="D002", scope_id="OTHER", summary="b", rationale="r")
    qr_only = decision.list_decisions(state, scope_id="QR")
    assert {d.id for d in qr_only} == {"D001"}


def test_state_transaction_persists_add_decision(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    paths = _io.store_paths(state_path)
    with state_transaction(state_path) as state:
        record, event = decision.add_decision(
            state, decision_id="D012", scope_id="QR", summary="s", rationale="r"
        )
        _io.append_jsonl(paths[StoreKind.DECISION], record)
        _io.append_jsonl(paths[StoreKind.EVENT], event)
    body = json.loads(state_path.read_text())
    assert "D012" in body["decisions"]
    assert len(paths[StoreKind.DECISION].read_text().splitlines()) == 1
    assert len(paths[StoreKind.EVENT].read_text().splitlines()) == 1
