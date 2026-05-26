"""Integration: dispatch runner routes the agent_end report by role (P28-I01-W13).

W13 generalises :func:`eawf.runtime.daemon.dispatch_runner.emit_agent_end_report`
so the report body type AND the destination store kind are driven by
the dispatched session's role. The executor flow is unchanged
(:class:`ExecutorReportBody` → ``executor_report.jsonl``); the seven
other roles emit role-typed bodies that land in their own per-role
stores via :func:`eawf.kernel.store.kinds.agent_report.store_kind_for_role`.

This module pins the load-bearing assertion behind success criterion 4:
an auditor-role session dispatched through the runner lands an
:class:`AuditorReportBody` envelope in the ``auditor_report`` store,
NOT an :class:`ExecutorReportBody` in the ``executor_report`` store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportPayload,
    AuditorReportBody,
    ExecutorReportBody,
    OperatorReportBody,
    PolisherReportBody,
    ResearcherReportBody,
    ReviewerReportBody,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.dispatch_runner import (
    _build_completion_body,
    emit_agent_end_report,
)
from eawf.runtime.daemon.methods import MethodContext

pytestmark = pytest.mark.integration

_WAVE_ID = "P27-I03-W10"


def _state_payload(*, role: str, session_id: str) -> dict[str, Any]:
    """Minimal state with one wave + one session whose role is *role*."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-05-26T00:00:00Z",
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "Eawf",
            "description": "",
            "domains": ["workflow"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "subproject_id": None,
            "phase_id": "P27",
            "iter_id": "P27-I03",
            "active_wave_ids": [],
            "active_session_ids": [session_id],
        },
        "workspace": None,
        "phases": {
            "P27": {
                "id": "P27",
                "scope_id": "EAWF",
                "title": "Observability",
                "status": "active",
                "iter_ids": ["P27-I03"],
                "outcome_ids": [],
                "opened_at": "2026-05-26T00:00:00Z",
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P27-I03": {
                "id": "P27-I03",
                "phase_id": "P27",
                "title": "Build-out",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": "2026-05-26T00:00:00Z",
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P27-I03",
                "title": "Role-routed dispatch wave",
                "status": "in_progress",
                "deps": [],
                "blocks": [],
                "file_scopes": ["src/"],
                "success_criteria": [],
                "agent_role": role,
                "effort_bucket": "M",
                "claim_session_id": session_id,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "opened_at": "2026-05-26T00:00:00Z",
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {
            session_id: {
                "id": session_id,
                "role": role,
                "runtime": "claude",
                "scope_id": _WAVE_ID,
                "status": "active",
                "claimed_wave_ids": [_WAVE_ID],
                "worktree_ids": [],
                "artifact_ids": [],
                "started_at": "2026-05-26T00:00:00Z",
                "ended_at": None,
                "summary": None,
            }
        },
        "plugins": {},
        "indexes": {},
    }


def _write_state(tmp_path: Path, *, role: str, session_id: str) -> Path:
    state = State.model_validate(_state_payload(role=role, session_id=session_id))
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text(state.model_dump_json(), encoding="utf-8")
    return path


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-05-26T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.3.0",
        bus=None,
        event_path=state_path.parent / "store" / "event.jsonl",
        state_path=state_path,
    )


def _read_single_envelope(state_path: Path, *, store_kind: StoreKind) -> Envelope:
    path = store_path(state_path, store_kind)
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1, lines
    return Envelope.model_validate_json(lines[0])


# ---------------------------------------------------------------------------
# Auditor session lands AUDITOR_REPORT, NOT EXECUTOR_REPORT (criterion 4)
# ---------------------------------------------------------------------------


def test_emit_agent_end_report_for_auditor_session_lands_auditor_store(
    tmp_path: Path,
) -> None:
    """An auditor session's dispatch lands an ``auditor_report`` envelope."""
    state_path = _write_state(tmp_path, role="auditor", session_id="SES-auditor")
    ctx = _ctx(state_path)

    report_id = emit_agent_end_report(
        ctx,
        session_id="SES-auditor",
        wave_id=_WAVE_ID,
        commit_sha="abcdef1",
        outcome="audit complete: verdict pass",
        runtime="claude",
    )

    # The envelope MUST live in the auditor_report store …
    envelope = _read_single_envelope(state_path, store_kind=StoreKind.AUDITOR_REPORT)
    assert envelope.id == report_id
    assert envelope.kind is StoreKind.AUDITOR_REPORT

    # … and NOT in the executor_report store.
    executor_store_path = store_path(state_path, StoreKind.EXECUTOR_REPORT)
    assert not executor_store_path.exists(), (
        f"executor_report.jsonl unexpectedly exists at {executor_store_path}"
    )

    # And the body MUST be the auditor-typed body, not the executor body.
    payload = AgentReportPayload.model_validate(envelope.payload)
    assert isinstance(payload.body, AuditorReportBody)
    assert not isinstance(payload.body, ExecutorReportBody)
    assert payload.body.target_id == _WAVE_ID


# ---------------------------------------------------------------------------
# Executor path unchanged from a behaviour perspective (criterion 3)
# ---------------------------------------------------------------------------


def test_emit_agent_end_report_for_executor_session_still_lands_executor_store(
    tmp_path: Path,
) -> None:
    """The executor flow is byte-equivalent to the pre-W13 surface."""
    state_path = _write_state(tmp_path, role="executor", session_id="SES-executor")
    ctx = _ctx(state_path)

    report_id = emit_agent_end_report(
        ctx,
        session_id="SES-executor",
        wave_id=_WAVE_ID,
        commit_sha="cafebab",
        outcome="executor wave landed",
        files_changed=["src/eawf/runtime/daemon/dispatch_runner.py"],
        tests_run=["uv run pytest tests/integration/test_dispatch_runner_role_routing.py -q"],
        runtime="claude",
    )

    envelope = _read_single_envelope(state_path, store_kind=StoreKind.EXECUTOR_REPORT)
    assert envelope.id == report_id
    assert envelope.kind is StoreKind.EXECUTOR_REPORT

    payload = AgentReportPayload.model_validate(envelope.payload)
    assert isinstance(payload.body, ExecutorReportBody)
    assert payload.body.commit_sha == "cafebab"
    assert payload.body.wave_id == _WAVE_ID
    assert payload.body.files_changed == ["src/eawf/runtime/daemon/dispatch_runner.py"]


# ---------------------------------------------------------------------------
# All 8 roles route to their own store kind via store_kind_for_role()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected_kind"),
    [
        (AgentSessionRole.RESEARCHER, StoreKind.RESEARCHER_REPORT),
        (AgentSessionRole.PLANNER, StoreKind.PLANNER_REPORT),
        (AgentSessionRole.EXECUTOR, StoreKind.EXECUTOR_REPORT),
        (AgentSessionRole.AUDITOR, StoreKind.AUDITOR_REPORT),
        (AgentSessionRole.REVIEWER, StoreKind.REVIEWER_REPORT),
        (AgentSessionRole.POLISHER, StoreKind.POLISHER_REPORT),
        (AgentSessionRole.OPERATOR, StoreKind.OPERATOR_REPORT),
        (AgentSessionRole.DOMAIN_SPECIALIST, StoreKind.DOMAIN_SPECIALIST_REPORT),
    ],
    ids=[
        "researcher",
        "planner",
        "executor",
        "auditor",
        "reviewer",
        "polisher",
        "operator",
        "domain-specialist",
    ],
)
def test_store_kind_for_role_covers_every_registered_role(
    role: AgentSessionRole, expected_kind: StoreKind
) -> None:
    """``store_kind_for_role`` returns the per-role kind for every registered role."""
    assert store_kind_for_role(role) is expected_kind


# ---------------------------------------------------------------------------
# _build_completion_body returns the right typed body per role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected_body_cls"),
    [
        (AgentSessionRole.RESEARCHER, ResearcherReportBody),
        (AgentSessionRole.AUDITOR, AuditorReportBody),
        (AgentSessionRole.REVIEWER, ReviewerReportBody),
        (AgentSessionRole.POLISHER, PolisherReportBody),
        (AgentSessionRole.OPERATOR, OperatorReportBody),
    ],
    ids=["researcher", "auditor", "reviewer", "polisher", "operator"],
)
def test_build_completion_body_returns_role_typed_body(
    role: AgentSessionRole, expected_body_cls: type
) -> None:
    """The body factory returns an instance of the role-matched body class."""
    body = _build_completion_body(
        role=role,
        wave_id=_WAVE_ID,
        commit_sha="abcdef1",
        outcome="dispatch outcome",
        files_changed=None,
        tests_run=None,
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
    )
    assert isinstance(body, expected_body_cls)
    assert body.role == role.value


def test_build_completion_body_executor_carries_rich_fields() -> None:
    """The executor body keeps commit_sha + wave_id + files_changed + tests_run."""
    body = _build_completion_body(
        role=AgentSessionRole.EXECUTOR,
        wave_id=_WAVE_ID,
        commit_sha="abcdef1",
        outcome="executor landed wave",
        files_changed=["src/a.py", "src/b.py"],
        tests_run=["uv run pytest -q"],
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
    )
    assert isinstance(body, ExecutorReportBody)
    assert body.commit_sha == "abcdef1"
    assert body.wave_id == _WAVE_ID
    assert body.files_changed == ["src/a.py", "src/b.py"]
    assert body.tests_run == ["uv run pytest -q"]
    assert body.outcome == "executor landed wave"


# ---------------------------------------------------------------------------
# Round-trip: reviewer session lands reviewer envelope
# ---------------------------------------------------------------------------


def test_emit_agent_end_report_for_reviewer_session_lands_reviewer_store(
    tmp_path: Path,
) -> None:
    """A reviewer session's dispatch lands a ``reviewer_report`` envelope."""
    state_path = _write_state(tmp_path, role="reviewer", session_id="SES-reviewer")
    ctx = _ctx(state_path)

    emit_agent_end_report(
        ctx,
        session_id="SES-reviewer",
        wave_id=_WAVE_ID,
        commit_sha="0123456",
        outcome="review pass",
        runtime="claude",
    )

    envelope = _read_single_envelope(state_path, store_kind=StoreKind.REVIEWER_REPORT)
    assert envelope.kind is StoreKind.REVIEWER_REPORT
    payload = AgentReportPayload.model_validate(envelope.payload)
    assert isinstance(payload.body, ReviewerReportBody)
    assert payload.body.target_id == _WAVE_ID
