"""Unit tests for :mod:`eawf.surfaces.render.decision_graph`.

Bypasses the CLI; exercises ``build_decision_graph``, ``render_text``,
``render_dot``, and ``render_mermaid`` directly. State instances are
constructed inline via ``State.model_validate`` against minimal payloads.
"""

from __future__ import annotations

from typing import Any

from eawf.kernel.state.models import State
from eawf.surfaces.render.decision_graph import (
    build_decision_graph,
    render_dot,
    render_mermaid,
    render_text,
)


def _base_state() -> dict[str, Any]:
    """Return a minimal, schema-valid state with no decisions."""
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
        "decisions": {},
    }


def _decision(
    decision_id: str,
    *,
    summary: str = "",
    status: str = "active",
    superseded_by: str | None = None,
    scope_id: str = "ZZ",
) -> dict[str, Any]:
    return {
        "id": decision_id,
        "scope_id": scope_id,
        "title": summary or f"Decision {decision_id}",
        "rationale": "because",
        "alternatives": [],
        "status": status,
        "created_at": "2026-05-08T00:00:00Z",
        "superseded_by": superseded_by,
    }


def test_build_decision_graph_empty_state() -> None:
    state = State.model_validate(_base_state())
    graph = build_decision_graph(state)
    assert graph.nodes == []
    assert graph.edges == []


def test_build_decision_graph_nodes_and_supersede_edge() -> None:
    payload = _base_state()
    payload["decisions"] = {
        "D01": _decision("D01", summary="first"),
        "D02": _decision("D02", summary="newer", superseded_by="D01"),
        # superseded_by points at non-existent D99 -> silently skipped
        "D03": _decision("D03", summary="dangling", superseded_by="D99"),
    }
    state = State.model_validate(payload)
    graph = build_decision_graph(state)
    assert [n.id for n in graph.nodes] == ["D01", "D02", "D03"]
    assert [(e.src, e.dst, e.kind) for e in graph.edges] == [
        ("D02", "D01", "superseded_by"),
    ]


def test_render_text_empty_state_placeholder() -> None:
    state = State.model_validate(_base_state())
    body = render_text(build_decision_graph(state))
    assert body == "(no decisions)"


def test_render_text_two_nodes_one_edge() -> None:
    payload = _base_state()
    payload["decisions"] = {
        "D01": _decision("D01", summary="first"),
        "D02": _decision("D02", summary="newer", superseded_by="D01"),
    }
    state = State.model_validate(payload)
    body = render_text(build_decision_graph(state))
    assert body.startswith("Decision graph (2 nodes, 1 edges):")
    assert "D01 [active]  first" in body
    assert "D02 [active]  newer" in body
    assert "D02 --superseded_by--> D01" in body


def test_render_dot_emits_digraph_block() -> None:
    payload = _base_state()
    payload["decisions"] = {
        "D01": _decision("D01", summary="first"),
        "D02": _decision("D02", summary="newer", superseded_by="D01"),
    }
    state = State.model_validate(payload)
    body = render_dot(build_decision_graph(state))
    assert body.startswith("digraph decisions {")
    assert body.rstrip().endswith("}")
    assert '"D01" [label="D01\\nfirst"];' in body
    assert '"D02" -> "D01" [label="superseded_by"];' in body


def test_render_mermaid_emits_graph_td_block() -> None:
    payload = _base_state()
    payload["decisions"] = {
        "D01": _decision("D01", summary="first"),
        "D02": _decision("D02", summary="newer", superseded_by="D01"),
    }
    state = State.model_validate(payload)
    body = render_mermaid(build_decision_graph(state))
    lines = body.splitlines()
    assert lines[0] == "graph TD"
    assert any(line.strip().startswith("D01[") for line in lines)
    assert any("D02 -->|superseded_by| D01" in line for line in lines)
