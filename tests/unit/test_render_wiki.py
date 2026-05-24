"""Unit tests for :mod:`eawf.surfaces.render.wiki`."""

from __future__ import annotations

from typing import Any

from eawf.kernel.state.models import State
from eawf.surfaces.render.wiki import build_wiki


def _base_state() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "Test Project",
            "description": "A test description.",
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


def _phase(phase_id: str, *, status: str = "closed", audit_id: str | None = None) -> dict[str, Any]:
    return {
        "id": phase_id,
        "scope_id": "ZZ",
        "subproject_id": None,
        "title": f"phase {phase_id}",
        "status": status,
        "iter_ids": [],
        "outcome_ids": [],
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T00:01:00Z" if status == "closed" else None,
        "audit_id": audit_id,
    }


def _audit(audit_id: str, *, verdict: str = "pass") -> dict[str, Any]:
    return {
        "id": audit_id,
        "scope_id": "ZZ",
        "kind": "ship-gate",
        "status": "complete",
        "report_artifact_id": None,
        "check_results": [],
        "integrity_results": [],
        "created_at": "2026-05-08T00:00:00Z",
        "verdict": verdict,
    }


def test_build_wiki_skips_open_phases() -> None:
    payload = _base_state()
    payload["phases"] = {
        "P00": _phase("P00", status="closed"),
        "P01": _phase("P01", status="active"),
    }
    state = State.model_validate(payload)
    body = build_wiki(state)
    h1_lines = [line for line in body.splitlines() if line.startswith("# ")]
    # Project H1 + 1 closed phase H1 = 2.
    assert len(h1_lines) == 2
    assert any("P00" in line for line in h1_lines)
    assert all("P01" not in line for line in h1_lines)


def test_build_wiki_multi_phase_with_audit_verdict() -> None:
    payload = _base_state()
    payload["phases"] = {
        "P00": _phase("P00", audit_id="A01"),
        "P01": _phase("P01", audit_id="A02"),
    }
    payload["audits"] = {
        "A01": _audit("A01", verdict="pass"),
        "A02": _audit("A02", verdict="minor"),
    }
    state = State.model_validate(payload)
    body = build_wiki(state)
    assert "# P00 — phase P00" in body
    assert "# P01 — phase P01" in body
    assert "Audit `A01` verdict: **pass**." in body
    assert "Audit `A02` verdict: **minor**." in body
