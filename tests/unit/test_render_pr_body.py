"""Unit tests for :mod:`eawf.render.pr_body`."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.artifacts.references import Citation
from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportBody,
    AgentReportEvidenceRef,
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    OperatorReportBody,
    ResearcherReportBody,
    ReviewerReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.profiles.models import ComposedProfile, RenderBlock
from eawf.render.pr_body import (
    PrBodyInput,
    PrBodyNotFound,
    build_pr_body,
    collect_pr_report_inputs,
    infer_pr_kind,
    resolve_pr_phase_id,
)

NOW = datetime(2026, 5, 14, tzinfo=UTC)


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


def _wave(wave_id: str, iter_id: str, *, outcome: str = "") -> dict[str, Any]:
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
        "outcome": outcome or None,
        "opened_at": "2026-05-08T00:00:00Z",
        "closed_at": "2026-05-08T00:01:00Z",
    }


def _write_report(
    state_path: Path,
    *,
    role: AgentSessionRole,
    body: AgentReportBody,
    scope_id: str,
    base_id: str,
    report_id: str,
) -> None:
    header = AgentReportHeader(
        report_id=report_id,
        role=role,
        session_id=f"SES-{role.value}",
        scope_id=scope_id,
        base_id=base_id,
        attempt=1,
        runtime="codex",
        generated_at=NOW,
        summary=body.summary,
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(role),
        scope_id=scope_id,
        created_at=NOW,
        updated_at=None,
        summary=body.summary,
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, store_kind_for_role(role))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(envelope.model_dump_json() + "\n")


def _state_with_phase_iter() -> State:
    payload = _base_state()
    payload["phases"] = {"P00": _phase("P00", title="bootstrap")}
    payload["iters"] = {"P00-I01": _iter("P00-I01", title="first iter")}
    payload["waves"] = {
        "P00-I01-W01": _wave(
            "P00-I01-W01",
            "P00-I01",
            outcome="did stuff",
        ),
    }
    return State.model_validate(payload)


def test_build_pr_body_unknown_phase_raises() -> None:
    state = State.model_validate(_base_state())
    with pytest.raises(PrBodyNotFound):
        build_pr_body(state, "P99")


def test_build_pr_body_phase_with_iters_and_waves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "eawf.render.pr_body.derive_wave_sha",
        lambda _wid: "abcdef0123456",
    )
    payload = _base_state()
    payload["phases"] = {"P00": _phase("P00", title="bootstrap")}
    payload["iters"] = {"P00-I01": _iter("P00-I01", title="first iter")}
    payload["waves"] = {
        "P00-I01-W01": _wave(
            "P00-I01-W01",
            "P00-I01",
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


def test_pr_body_input_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        PrBodyInput.model_validate(
            {
                "kind": "operator_rollup",
                "title": "rollup",
                "summary": "done",
                "unexpected": True,
            }
        )


def test_infer_pr_kind_recognizes_phase_iter_docs_research_and_incident() -> None:
    assert infer_pr_kind("P17") == "phase"
    assert infer_pr_kind("P17-I01") == "iter"
    assert infer_pr_kind("P17", source="docs_research") == "docs-research"
    assert infer_pr_kind("P17", incident_id="INC-001") == "incident-fix"


def test_resolve_pr_phase_id_resolves_iter_scope() -> None:
    state = _state_with_phase_iter()
    assert resolve_pr_phase_id(state, "P00-I01") == "P00"


def test_build_pr_body_includes_typed_inputs_and_profile_blocks() -> None:
    payload = _base_state()
    payload["phases"] = {"P00": _phase("P00", title="bootstrap")}
    state = State.model_validate(payload)
    composed = ComposedProfile(
        name="test",
        render_blocks=[
            RenderBlock(id="skip", target="AGENTS.md", body_template="skip"),
            RenderBlock(
                id="phase-extra",
                target="pr.phase",
                body_template="## Extra\n\nPhase {{ phase_id }} from profile.",
            ),
        ],
    )
    body = build_pr_body(
        state,
        "P00",
        inputs=[
            PrBodyInput(
                kind="operator_rollup",
                title="Operator",
                summary="Operator summary.",
                bullets=["One"],
            )
        ],
        composed_profile=composed,
    )
    assert "## Operator Rollup" in body
    assert "- One" in body
    assert "Phase P00 from profile." in body
    assert "skip" not in body


def test_build_pr_body_renders_docs_research_citation_source_rows() -> None:
    payload = _base_state()
    payload["phases"] = {"P00": _phase("P00", title="docs research")}
    state = State.model_validate(payload)

    body = build_pr_body(
        state,
        "P00",
        inputs=[
            PrBodyInput(
                kind="docs_research",
                title="Research",
                summary="Docs source review.",
                citations=[
                    Citation(
                        n=1,
                        ref="docs/architecture/workflow.md",
                        kind="repo",
                        title="Workflow docs",
                    )
                ],
            )
        ],
        kind="docs-research",
    )

    assert "## Docs And Research Report" in body
    assert "Source rows:" in body
    assert "| [1] | repo | `docs/architecture/workflow.md` | Workflow docs |" in body


def test_collect_pr_report_inputs_phase_reads_operator_rollup(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state = _state_with_phase_iter()
    _write_report(
        state_path,
        role=AgentSessionRole.OPERATOR,
        body=OperatorReportBody(
            role="operator",
            verdict=AgentReportVerdict.PASS,
            confidence=Confidence.HIGH,
            summary="operator summary",
            phase_id="P00",
            completed_wave_ids=["P00-I01-W01"],
            decisions=[],
            next_actions=[],
        ),
        scope_id="P00",
        base_id="P00",
        report_id="AR-operator-P00-01",
    )

    inputs = collect_pr_report_inputs(state_path, state, "P00", kind="phase")
    body = build_pr_body(state, "P00", inputs=inputs)

    assert [item.kind for item in inputs] == ["operator_rollup"]
    assert "operator: 1" in inputs[0].bullets
    assert "## Operator Rollup" in body


def test_collect_pr_report_inputs_iter_reads_executor_and_reviewer_reports(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state = _state_with_phase_iter()
    _write_report(
        state_path,
        role=AgentSessionRole.EXECUTOR,
        body=ExecutorReportBody(
            role="executor",
            verdict=AgentReportVerdict.PASS,
            confidence=Confidence.HIGH,
            summary="executor summary",
            evidence_refs=[AgentReportEvidenceRef(kind="repo", ref="src/eawf/render/pr_body.py")],
            wave_id="P00-I01-W01",
            files_changed=["src/eawf/render/pr_body.py"],
            tests_run=["uv run pytest tests/unit/test_render_pr_body.py -q"],
            commit_sha="abcdef0123",
            outcome="done",
        ),
        scope_id="P00-I01-W01",
        base_id="P00-I01-W01",
        report_id="AR-executor-P00-I01-W01-01",
    )
    _write_report(
        state_path,
        role=AgentSessionRole.REVIEWER,
        body=ReviewerReportBody(
            role="reviewer",
            verdict=AgentReportVerdict.PASS,
            confidence=Confidence.HIGH,
            summary="reviewer summary",
            evidence_refs=[
                AgentReportEvidenceRef(kind="repo", ref="tests/unit/test_render_pr_body.py")
            ],
            target_id="HEAD",
            findings=[],
            coverage_refs=[AgentReportEvidenceRef(kind="repo", ref="src/eawf/render/pr_body.py:1")],
        ),
        scope_id="P00-I01",
        base_id="P00-I01",
        report_id="AR-reviewer-P00-I01-01",
    )

    inputs = collect_pr_report_inputs(state_path, state, "P00-I01", kind="iter")
    body = build_pr_body(state, "P00", inputs=inputs, kind="iter")

    assert {item.kind for item in inputs} == {"executor_report", "reviewer_report"}
    assert "## Executor Report" in body
    assert "## Reviewer Report" in body
    assert "| [1] | repo | `src/eawf/render/pr_body.py` |  |" in body
    assert "| [2] | repo | `tests/unit/test_render_pr_body.py` |  |" in body


def test_collect_pr_report_inputs_docs_research_reads_researcher_report(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state = _state_with_phase_iter()
    _write_report(
        state_path,
        role=AgentSessionRole.RESEARCHER,
        body=ResearcherReportBody(
            role="researcher",
            verdict=AgentReportVerdict.PASS,
            confidence=Confidence.HIGH,
            summary="research summary",
            evidence_refs=[
                AgentReportEvidenceRef(kind="repo", ref="docs/architecture/agent-reports.md")
            ],
            question="What changed?",
            findings=["typed reports"],
            alternatives=[],
            recommendation="include report summary",
        ),
        scope_id="P00",
        base_id="P00",
        report_id="AR-researcher-P00-01",
    )

    inputs = collect_pr_report_inputs(state_path, state, "P00", kind="docs-research")
    body = build_pr_body(state, "P00", inputs=inputs, kind="docs-research")

    assert [item.kind for item in inputs] == ["docs_research"]
    assert "## Docs And Research Report" in body
    assert "docs/architecture/agent-reports.md" in body
