"""Unit tests for :mod:`eawf.render.impact`."""

from __future__ import annotations

from typing import Any

from eawf.render.impact import build_impact_graph, render_dot, render_text
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
        "phases": {
            "P00": {
                "id": "P00",
                "scope_id": "ZZ",
                "subproject_id": None,
                "title": "p0",
                "status": "active",
                "iter_ids": ["P00-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P00-I01": {
                "id": "P00-I01",
                "phase_id": "P00",
                "title": "i1",
                "status": "active",
                "wave_ids": ["P00-I01-W01"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            "P00-I01-W01": {
                "id": "P00-I01-W01",
                "iter_id": "P00-I01",
                "title": "w1",
                "status": "pending",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/eawf/**", "tests/unit/**"],
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
        "decisions": {},
    }


def _decision(decision_id: str, *, scope_id: str = "ZZ", summary: str = "") -> dict[str, Any]:
    return {
        "id": decision_id,
        "scope_id": scope_id,
        "summary": summary or f"Decision {decision_id}",
        "rationale": "because",
        "alternatives": [],
        "status": "active",
        "created_at": "2026-05-08T00:00:00Z",
        "superseded_by": None,
    }


def test_build_impact_graph_empty_state() -> None:
    state = State.model_validate(_base_state())
    graph = build_impact_graph(state)
    assert graph.nodes == []


def test_build_impact_graph_project_scope_joins_all_phases() -> None:
    payload = _base_state()
    payload["decisions"] = {
        "D01": _decision("D01", scope_id="ZZ", summary="cross-cutting"),
    }
    state = State.model_validate(payload)
    graph = build_impact_graph(state)
    assert [n.decision_id for n in graph.nodes] == ["D01"]
    assert graph.nodes[0].wave_ids == ["P00-I01-W01"]
    assert graph.nodes[0].file_globs == ["src/eawf/**", "tests/unit/**"]


def test_build_impact_graph_phase_scope_only_matching_phase() -> None:
    payload = _base_state()
    payload["decisions"] = {
        "D01": _decision("D01", scope_id="P00", summary="P00-only"),
        "D02": _decision("D02", scope_id="P01", summary="P01-only, no phase"),
    }
    state = State.model_validate(payload)
    graph = build_impact_graph(state)
    by_id = {n.decision_id: n for n in graph.nodes}
    assert by_id["D01"].wave_ids == ["P00-I01-W01"]
    # No P01 phase → no waves matched.
    assert by_id["D02"].wave_ids == []
    assert by_id["D02"].file_globs == []


def test_build_impact_graph_filter_by_decision_id() -> None:
    payload = _base_state()
    payload["decisions"] = {
        "D01": _decision("D01", scope_id="ZZ"),
        "D02": _decision("D02", scope_id="ZZ"),
    }
    state = State.model_validate(payload)
    graph = build_impact_graph(state, decision_id="D01")
    assert [n.decision_id for n in graph.nodes] == ["D01"]
    # Missing id yields empty graph.
    graph_empty = build_impact_graph(state, decision_id="D99")
    assert graph_empty.nodes == []


def test_render_text_empty_placeholder() -> None:
    state = State.model_validate(_base_state())
    body = render_text(build_impact_graph(state))
    assert body == "(no impact entries)"


def test_render_dot_emits_digraph_block() -> None:
    payload = _base_state()
    payload["decisions"] = {"D01": _decision("D01", scope_id="ZZ")}
    state = State.model_validate(payload)
    body = render_dot(build_impact_graph(state))
    assert body.startswith("digraph impact {")
    assert body.rstrip().endswith("}")
    assert '"D01" -> "P00-I01-W01";' in body
    assert '"P00-I01-W01" -> "file::src/eawf/**";' in body
