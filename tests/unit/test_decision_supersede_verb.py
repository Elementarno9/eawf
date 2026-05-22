"""Unit tests for the ``decision supersede`` verb + the supersede-link invariant.

Covers the standalone :func:`eawf.evidence.decision.supersede_decision`
mutator (both ends of the link flip) and
:func:`eawf.validate.invariants.check_decision_supersede_link` (both
directions of the status/link agreement rule).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from eawf.cli import errors as cli_errors
from eawf.evidence import _io, decision
from eawf.state.enums import DecisionStatus
from eawf.state.models import State
from eawf.validate.invariants import Violation, check_decision_supersede_link

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _state_path(tmp_path: Path) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return target


def _codes(violations: list[Violation]) -> set[str]:
    return {v.code for v in violations}


def _seed_two_decisions(tmp_path: Path) -> State:
    state = _io.load_state(_state_path(tmp_path))
    decision.add_decision(state, decision_id="D010", scope_id="QR", summary="old", rationale="r1")
    decision.add_decision(state, decision_id="D011", scope_id="QR", summary="new", rationale="r2")
    return state


# ---- supersede_decision mutator --------------------------------------------


def test_supersede_decision_flips_both_ends(tmp_path: Path) -> None:
    state = _seed_two_decisions(tmp_path)

    record, event = decision.supersede_decision(state, old_id="D010", new_id="D011")

    old = state.decisions["D010"]
    new = state.decisions["D011"]
    assert old.status == DecisionStatus.SUPERSEDED
    assert old.superseded_by == "D011"
    # The superseding decision is untouched.
    assert new.status == DecisionStatus.ACTIVE
    assert new.superseded_by is None
    assert record.payload["superseded_by"] == "D011"
    assert event.payload["event_type"] == "decision.supersede"
    assert "superseded by D011" in event.summary


def test_supersede_decision_unknown_old_raises(tmp_path: Path) -> None:
    state = _seed_two_decisions(tmp_path)
    with pytest.raises(cli_errors.NotFound, match="decision 'D999' not found"):
        decision.supersede_decision(state, old_id="D999", new_id="D011")


def test_supersede_decision_unknown_new_raises(tmp_path: Path) -> None:
    state = _seed_two_decisions(tmp_path)
    with pytest.raises(cli_errors.NotFound, match="superseding decision 'D999' not found"):
        decision.supersede_decision(state, old_id="D010", new_id="D999")


def test_supersede_decision_self_raises(tmp_path: Path) -> None:
    state = _seed_two_decisions(tmp_path)
    with pytest.raises(cli_errors.InvalidInput, match="cannot supersede itself"):
        decision.supersede_decision(state, old_id="D010", new_id="D010")


def test_supersede_decision_already_superseded_raises(tmp_path: Path) -> None:
    state = _seed_two_decisions(tmp_path)
    decision.add_decision(state, decision_id="D012", scope_id="QR", summary="newer", rationale="r3")
    decision.supersede_decision(state, old_id="D010", new_id="D011")
    with pytest.raises(cli_errors.InvalidInput, match="only ACTIVE decisions can be superseded"):
        decision.supersede_decision(state, old_id="D010", new_id="D012")


# ---- check_decision_supersede_link invariant -------------------------------


def _decision_payload(
    decision_id: str,
    *,
    status: str,
    superseded_by: str | None,
) -> dict[str, Any]:
    return {
        "id": decision_id,
        "scope_id": "QR",
        "title": f"decision {decision_id}",
        "rationale": "because",
        "alternatives": [],
        "consequences": [],
        "status": status,
        "created_at": "2026-05-08T00:00:00Z",
        "superseded_by": superseded_by,
    }


def _base_state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant-research",
            "title": "Quant Research",
            "description": "",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def test_check_decision_supersede_link_passes_correctly_linked_pair() -> None:
    payload = _base_state_payload()
    payload["decisions"] = {
        "D010": _decision_payload("D010", status="superseded", superseded_by="D011"),
        "D011": _decision_payload("D011", status="active", superseded_by=None),
    }
    state = State.model_validate(payload)
    assert list(check_decision_supersede_link(state)) == []


def test_check_decision_supersede_link_flags_link_without_superseded() -> None:
    payload = _base_state_payload()
    # Link set but status still active — half-applied flip.
    payload["decisions"] = {
        "D010": _decision_payload("D010", status="active", superseded_by="D011"),
        "D011": _decision_payload("D011", status="active", superseded_by=None),
    }
    state = State.model_validate(payload)
    codes = _codes(list(check_decision_supersede_link(state)))
    assert "INV.DECISION.LINK_WITHOUT_SUPERSEDED" in codes


def test_check_decision_supersede_link_flags_superseded_without_link() -> None:
    payload = _base_state_payload()
    # Status superseded but no link — the unlinked superseded row the wave targets.
    payload["decisions"] = {
        "D010": _decision_payload("D010", status="superseded", superseded_by=None),
    }
    state = State.model_validate(payload)
    codes = _codes(list(check_decision_supersede_link(state)))
    assert "INV.DECISION.SUPERSEDED_WITHOUT_LINK" in codes


def test_check_decision_supersede_link_ignores_plain_active_decision() -> None:
    payload = _base_state_payload()
    payload["decisions"] = {
        "D010": _decision_payload("D010", status="active", superseded_by=None),
    }
    state = State.model_validate(payload)
    assert list(check_decision_supersede_link(state)) == []
