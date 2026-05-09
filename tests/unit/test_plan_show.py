"""Unit tests for :mod:`eawf.render.plan_view`.

Bypasses the CLI; exercises ``build_view``, ``render_markdown``, and
``render_json`` directly. State instances are constructed inline via
``State.model_validate`` against minimal payloads.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.render.plan_view import (
    PlanSection,
    PlanViewNotFound,
    build_view,
    render_json,
    render_markdown,
)
from eawf.state.models import State


def _base_state() -> dict[str, Any]:
    """Return a minimal, schema-valid state with one phase and one iter."""
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
            "phase_id": "P05",
            "iter_id": "P05-I01",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P05": {
                "id": "P05",
                "scope_id": "QR",
                "title": "Phase Five",
                "status": "active",
                "iter_ids": ["P05-I01"],
                "outcome_ids": [],
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P05-I01": {
                "id": "P05-I01",
                "phase_id": "P05",
                "title": "Iter One",
                "status": "active",
                "wave_ids": [],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-08T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _wave(
    wave_id: str,
    *,
    iter_id: str = "P05-I01",
    status: str = "pending",
    deps: list[str] | None = None,
    title: str | None = None,
    claim_session_id: str | None = None,
    commit: str | None = None,
    outcome: str | None = None,
    closed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": iter_id,
        "title": title or f"Wave {wave_id}",
        "status": status,
        "deps": deps or [],
        "file_scopes": [],
        "claim_session_id": claim_session_id,
        "worktree_id": None,
        "commit": commit,
        "outcome": outcome,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": closed_at,
    }


def _add_wave(s: dict[str, Any], wave: dict[str, Any]) -> None:
    s["waves"][wave["id"]] = wave
    s["iters"]["P05-I01"]["wave_ids"].append(wave["id"])


def test_build_view_resolves_iter_when_id_provided() -> None:
    state = State.model_validate(_base_state())
    view = build_view(state, "P05-I01")
    assert view.iter.id == "P05-I01"
    assert view.phase is not None
    assert view.phase.id == "P05"


def test_build_view_unknown_iter_raises_not_found() -> None:
    state = State.model_validate(_base_state())
    with pytest.raises(PlanViewNotFound):
        build_view(state, "P99-I99")


def test_build_view_empty_iter_yields_empty_collections() -> None:
    state = State.model_validate(_base_state())
    view = build_view(state, "P05-I01")
    assert view.waves == []
    assert view.dag.nodes == []
    assert view.dag.edges == []
    assert view.dag.topo_order == []  # acyclic, just empty
    assert view.dag.cycle is None
    assert view.checks == []
    assert view.risks == []
    assert view.summary.wave_count == 0
    assert view.summary.blocked_waves == []


def test_build_view_topo_orders_dag() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W00", status="closed", closed_at="2026-05-08T01:00:00Z"))
    _add_wave(s, _wave("P05-I01-W01", deps=["P05-I01-W00"]))
    _add_wave(s, _wave("P05-I01-W02", deps=["P05-I01-W01"]))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert view.dag.cycle is None
    assert view.dag.topo_order == ["P05-I01-W00", "P05-I01-W01", "P05-I01-W02"]


def test_build_view_detects_dag_cycle() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01", deps=["P05-I01-W02"]))
    _add_wave(s, _wave("P05-I01-W02", deps=["P05-I01-W01"]))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert view.dag.topo_order is None
    assert view.dag.cycle is not None
    assert set(view.dag.cycle) == {"P05-I01-W01", "P05-I01-W02"}


def test_build_view_collects_iter_audit_checks() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    s["iters"]["P05-I01"]["audit_id"] = "AU-1"
    s["audits"] = {
        "AU-1": {
            "id": "AU-1",
            "scope_id": "P05-I01",
            "kind": "evaluation",
            "status": "complete",
            "report_artifact_id": None,
            "check_results": [
                {"name": "ruff_clean", "passed": True, "details": None},
                {"name": "mypy_strict", "passed": False, "details": "10 errors"},
            ],
            "integrity_results": [],
            "created_at": "2026-05-08T00:00:00Z",
            "verdict": "minor",
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.checks) == 2
    assert all(c.source == "iter_audit" for c in view.checks)
    names = {c.name for c in view.checks}
    assert names == {"ruff_clean", "mypy_strict"}
    failed = next(c for c in view.checks if not c.passed)
    assert failed.details == "10 errors"


def test_build_view_collects_wave_audit_checks() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    s["audits"] = {
        "AU-W1": {
            "id": "AU-W1",
            "scope_id": "P05-I01-W01",
            "kind": "review",
            "status": "complete",
            "report_artifact_id": None,
            "check_results": [
                {"name": "tests_green", "passed": True, "details": None},
            ],
            "integrity_results": [],
            "created_at": "2026-05-08T00:00:00Z",
            "verdict": "pass",
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.checks) == 1
    cv = view.checks[0]
    assert cv.source == "wave_audit"
    assert cv.wave_id == "P05-I01-W01"
    assert cv.audit_id == "AU-W1"


def test_build_view_synthesises_wave_outcome_check() -> None:
    s = _base_state()
    _add_wave(
        s,
        _wave(
            "P05-I01-W01",
            status="closed",
            closed_at="2026-05-08T02:00:00Z",
            outcome="ok",
            commit="deadbeef1234567",
        ),
    )
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    outcome_checks = [c for c in view.checks if c.source == "wave_outcome"]
    assert len(outcome_checks) == 1
    assert outcome_checks[0].name == "ok"
    assert outcome_checks[0].passed is True


def test_collect_risks_p0_backlog() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    s["backlog"] = {
        "BL-1": {
            "id": "BL-1",
            "scope_id": "P05-I01",
            "title": "fix flake on W01",
            "priority": "P0",
            "status": "open",
            "created_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "resolution": None,
            "commit": None,
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.risks) == 1
    r = view.risks[0]
    assert r.kind == "backlog"
    assert r.id == "BL-1"
    assert r.severity == "P0"


def test_collect_risks_open_incident_high() -> None:
    s = _base_state()
    s["incidents"] = {
        "INC-1": {
            "id": "INC-1",
            "scope_id": "P05",
            "severity": "high",
            "title": "lock collision",
            "status": "open",
            "opened_at": "2026-05-08T00:00:00Z",
            "closed_at": None,
            "root_cause": None,
            "corrective_action_ids": [],
            "report_artifact_id": None,
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.risks) == 1
    r = view.risks[0]
    assert r.kind == "incident"
    assert r.severity == "high"


def test_collect_risks_rejected_hypothesis() -> None:
    s = _base_state()
    s["hypotheses"] = {
        "H05-01": {
            "id": "H05-01",
            "scope_id": "P05-I01",
            "text": "tried approach X; failed",
            "metric": "throughput",
            "confirm": ">100",
            "reject": "<50",
            "status": "rejected",
            "verdict": "rejected",
            "audit_id": None,
            "source_artifact_id": None,
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.risks) == 1
    r = view.risks[0]
    assert r.kind == "hypothesis_rejected"
    assert r.severity == "rejected"


def test_render_markdown_ascii_dag_handles_disjoint_components() -> None:
    s = _base_state()
    # Two trees: W00 -> W01; W02 -> W03 (no shared nodes).
    _add_wave(s, _wave("P05-I01-W00", status="closed", closed_at="2026-05-08T01:00:00Z"))
    _add_wave(s, _wave("P05-I01-W01", deps=["P05-I01-W00"]))
    _add_wave(s, _wave("P05-I01-W02", status="closed", closed_at="2026-05-08T01:00:00Z"))
    _add_wave(s, _wave("P05-I01-W03", deps=["P05-I01-W02"]))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    md = render_markdown(view, ascii_dag=True)
    # Both roots present at level 0 (no leading whitespace).
    lines = md.splitlines()
    assert "P05-I01-W00 (closed)" in lines
    assert "P05-I01-W02 (closed)" in lines
    # Children indented under their parent.
    assert "  -> P05-I01-W01 (pending)" in lines
    assert "  -> P05-I01-W03 (pending)" in lines


def test_render_markdown_empty_iter_uses_friendly_paragraph() -> None:
    state = State.model_validate(_base_state())
    view = build_view(state, "P05-I01")
    md = render_markdown(view)
    assert "# Plan: P05-I01" in md
    assert "No waves planned yet." in md
    assert "## Summary" not in md


def test_render_markdown_show_section_emits_only_named_block() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    md = render_markdown(view, sections=PlanSection.RISKS)
    assert "## Risks" in md
    assert "## Summary" not in md
    assert "## DAG" not in md


def test_render_json_envelope_contains_all_keys() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    env = render_json(view)
    assert set(env.keys()) == {"iter", "phase", "waves", "dag", "checks", "risks", "summary"}


def test_render_json_section_filter_keeps_envelope_shape() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    env = render_json(view, sections=PlanSection.WAVES)
    # Shape stable, but body sections empty.
    assert set(env.keys()) == {"iter", "phase", "waves", "dag", "checks", "risks", "summary"}
    assert env["waves"]
    assert env["risks"] == []
    assert env["checks"] == []
    assert env["dag"]["nodes"] == []


def test_parse_check_result_skips_malformed_row() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W01"))
    s["iters"]["P05-I01"]["audit_id"] = "AU-1"
    s["audits"] = {
        "AU-1": {
            "id": "AU-1",
            "scope_id": "P05-I01",
            "kind": "evaluation",
            "status": "complete",
            "report_artifact_id": None,
            "check_results": [
                {"name": "ruff_clean", "passed": True, "details": None},
                {"missing_keys": True},  # malformed: skipped, no raise
                "wholly_unstructured",  # malformed: skipped
            ],
            "integrity_results": [],
            "created_at": "2026-05-08T00:00:00Z",
            "verdict": "pass",
        }
    }
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert len(view.checks) == 1
    assert view.checks[0].name == "ruff_clean"


def test_blocked_waves_lists_pending_waves_with_open_deps() -> None:
    s = _base_state()
    _add_wave(s, _wave("P05-I01-W00", status="closed", closed_at="2026-05-08T01:00:00Z"))
    _add_wave(s, _wave("P05-I01-W01", deps=["P05-I01-W00"]))  # closed dep — not blocked
    _add_wave(s, _wave("P05-I01-W02", deps=["P05-I01-W01"]))  # pending dep — blocked
    state = State.model_validate(s)
    view = build_view(state, "P05-I01")
    assert view.summary.blocked_waves == ["P05-I01-W02"]


@settings(max_examples=30, deadline=None)
@given(
    wave_count=st.integers(min_value=2, max_value=6),
)
def test_build_view_topo_idempotent(wave_count: int) -> None:
    """Property: same input produces identical topo_order regardless of dict ordering."""
    s = _base_state()
    base_order = [f"P05-I01-W{n:02d}" for n in range(wave_count)]
    # Linear chain.
    _add_wave(s, _wave(base_order[0], status="closed", closed_at="2026-05-08T01:00:00Z"))
    for i in range(1, wave_count):
        _add_wave(s, _wave(base_order[i], deps=[base_order[i - 1]]))

    state_a = State.model_validate(s)
    view_a = build_view(state_a, "P05-I01")

    # Permute dict insertion order: rebuild waves dict in reverse order.
    s2 = deepcopy(s)
    s2["waves"] = {wid: s2["waves"][wid] for wid in reversed(list(s2["waves"].keys()))}
    state_b = State.model_validate(s2)
    view_b = build_view(state_b, "P05-I01")

    assert view_a.dag.topo_order == view_b.dag.topo_order
    assert view_a.dag.cycle == view_b.dag.cycle
