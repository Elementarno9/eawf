"""Unit tests for :mod:`eawf.render.pr_body`."""

from __future__ import annotations

from typing import Any

import pytest

from eawf.render.pr_body import PrBodyNotFound, build_pr_body
from eawf.state.models import State


def _base_state() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": "",
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
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


def _phase(phase_id: str = "P00", title: str = "test phase") -> dict[str, Any]:
    return {
        "id": phase_id,
        "scope_id": "ZZ",
        "subproject_id": None,
        "title": title,
        "status": "closed",
        "iter_ids": [],
        "outcome_ids": [],
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T00:01:00Z",
        "audit_id": None,
    }


def _iter(iter_id: str, phase_id: str = "P00", title: str = "") -> dict[str, Any]:
    return {
        "id": iter_id,
        "phase_id": phase_id,
        "title": title or f"iter {iter_id}",
        "status": "closed",
        "wave_ids": [],
        "estimate_id": None,
        "audit_id": None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T00:01:00Z",
    }


def _wave(
    wave_id: str, iter_id: str, *, commit: str | None = None, outcome: str = ""
) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": f"Wave {wave_id}",
        "status": "closed",
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "claim_session_id": None,
        "worktree_id": None,
        "token_budget": None,
        "tokens_consumed": 0,
        "commit": commit,
        "outcome": outcome or None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T00:01:00Z",
    }


def test_build_pr_body_unknown_phase_raises() -> None:
    state = State.model_validate(_base_state())
    with pytest.raises(PrBodyNotFound):
        build_pr_body(state, "P99")


def test_build_pr_body_phase_with_iters_and_waves() -> None:
    payload = _base_state()
    payload["phases"] = {"P00": _phase("P00", title="bootstrap")}
    payload["iters"] = {"P00-I01": _iter("P00-I01", title="first iter")}
    payload["waves"] = {
        "P00-I01-W01": _wave(
            "P00-I01-W01",
            "P00-I01",
            commit="abcdef0123",
            outcome="did stuff",
        ),
    }
    state = State.model_validate(payload)
    body = build_pr_body(state, "P00")
    assert body.startswith("# P00: bootstrap\n")
    assert "## Summary\n" in body
    assert "- first iter" in body
    assert "## Phase deliverables" in body
    assert "| `P00-I01-W01` | Wave P00-I01-W01 | `abcdef0` | did stuff |" in body
    assert "## Test plan" in body
    assert "- [ ] `uv run pytest -q`" in body


def test_build_pr_body_phase_with_no_waves_shows_placeholder() -> None:
    payload = _base_state()
    payload["phases"] = {"P00": _phase("P00")}
    state = State.model_validate(payload)
    body = build_pr_body(state, "P00")
    assert "_(no waves recorded for this phase)_" in body
