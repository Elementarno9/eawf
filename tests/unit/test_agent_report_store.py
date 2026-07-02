"""Unit tests for append-only agent report storage."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, Confidence
from eawf.kernel.state.models import State
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportEvidenceRef,
    AgentReportPayload,
    AuditorReportBody,
    ExecutorReportBody,
    ResearcherReportBody,
    report_record_id,
)
from eawf.kernel.store.paths import store_path
from eawf.workflow.agent_report.store import (
    AgentReportRoleMismatchError,
    AgentReportScrubError,
    append_agent_report,
)


def _state(
    *,
    role: AgentSessionRole = AgentSessionRole.EXECUTOR,
    agent_principal_id: str | None = None,
) -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": "2026-05-14T00:00:00Z",
            "project": {
                "code": "QR",
                "slug": "qr",
                "title": "QR",
                "domains": [],
                "default_branch": "main",
                "status": "active",
                "repo_urn": "urn:eawf:v1:repo:QR",
            },
            "current": {
                "project_code": "QR",
                "track_id": None,
                "phase_id": None,
                "iter_id": None,
                "active_wave_ids": [],
                "active_session_ids": ["SES-001"],
            },
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {
                "SES-001": {
                    "id": "SES-001",
                    "role": role.value,
                    "runtime": "codex",
                    "scope_id": "P18-I01-W04",
                    "status": "active",
                    "claimed_wave_ids": [],
                    "worktree_ids": [],
                    "artifact_ids": [],
                    "started_at": "2026-05-14T00:00:00Z",
                    "ended_at": None,
                    "summary": None,
                    "agent_principal_id": agent_principal_id,
                }
            },
            "plugins": {},
            "indexes": {},
        }
    )


def _body(summary: str = "implemented report writer") -> ExecutorReportBody:
    return ExecutorReportBody(
        role="executor",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary=summary,
        wave_id="P18-I01-W04",
        outcome="done",
    )


def _researcher_body() -> ResearcherReportBody:
    return ResearcherReportBody(
        role="researcher",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="researched report writer evidence",
        evidence_refs=[AgentReportEvidenceRef(kind="artifact", ref="docs/source.md")],
        question="which writer path?",
        recommendation="use the append-only writer",
    )


def _researcher_body_without_evidence() -> ResearcherReportBody:
    return ResearcherReportBody(
        role="researcher",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="researched report writer evidence",
        question="which writer path?",
        recommendation="use the append-only writer",
    )


def _state_path(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    path = state_dir / "state.json"
    path.write_text("{}", encoding="utf-8")
    return path


def test_append_agent_report_writes_attempt_one(tmp_path: Path) -> None:
    result = append_agent_report(
        state=_state(),
        state_path=_state_path(tmp_path),
        session_id="SES-001",
        base_id="P18-I01-W04",
        body=_body(),
        generated_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    assert result.attempt == 1
    assert result.store_kind == "executor_report"
    assert result.urn.endswith("/executor_report/AR-executor-P18-I01-W04-01")
    path = store_path(tmp_path / ".ea" / "state.json", result.envelope.kind)
    parsed = Envelope.model_validate_json(path.read_text(encoding="utf-8").splitlines()[0])
    payload = AgentReportPayload.model_validate(parsed.payload)
    assert payload.header.attempt == 1
    assert payload.header.role is AgentSessionRole.EXECUTOR


_AUDITOR_ID_RE = re.compile(r"^AR-auditor-(?!auditor-)")


@pytest.mark.parametrize("base_id", ["P30-I20-W58", "auditor-P30-I20-W58"])
def test_report_record_id_role_prefix_appears_once(base_id: str) -> None:
    """A plain or role-prefixed base_id mints an id with one role prefix.

    CR-02: minting an auditor id from a base_id already carrying the role
    token used to double it (AR-auditor-auditor-...); it now matches
    ^AR-auditor-(?!auditor-) and both inputs collapse to the same id.
    """
    report_id = report_record_id(role=AgentSessionRole.AUDITOR, base_id=base_id, attempt=1)
    assert _AUDITOR_ID_RE.match(report_id)
    assert report_id == "AR-auditor-P30-I20-W58-01"


def test_append_agent_report_normalizes_role_prefixed_base_id(tmp_path: Path) -> None:
    """The append path mints a single-prefix auditor id for a role-prefixed base.

    CR-02: the store-layer id-shape assertion carries the invariant end to
    end -- appending with a role-prefixed base_id writes a header id
    matching ^AR-auditor-(?!auditor-), never a doubled prefix.
    """
    body = AuditorReportBody(
        role="auditor",
        verdict=AgentReportVerdict.PASS,
        confidence=Confidence.HIGH,
        summary="re-read the diff against the criteria",
        target_id="P30-I20-W58",
    )
    result = append_agent_report(
        state=_state(role=AgentSessionRole.AUDITOR),
        state_path=_state_path(tmp_path),
        session_id="SES-001",
        base_id="auditor-P30-I20-W58",
        body=body,
        generated_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    assert _AUDITOR_ID_RE.match(result.envelope.id)
    assert result.envelope.id == "AR-auditor-P30-I20-W58-01"
    assert result.urn.endswith("/auditor_report/AR-auditor-P30-I20-W58-01")


def test_append_agent_report_copies_agent_principal_id_to_header(tmp_path: Path) -> None:
    result = append_agent_report(
        state=_state(agent_principal_id="u-12345678"),
        state_path=_state_path(tmp_path),
        session_id="SES-001",
        base_id="P18-I01-W04",
        body=_body(),
        generated_at=datetime(2026, 5, 14, tzinfo=UTC),
    )

    path = store_path(tmp_path / ".ea" / "state.json", result.envelope.kind)
    parsed = Envelope.model_validate_json(path.read_text(encoding="utf-8").splitlines()[0])
    payload = AgentReportPayload.model_validate(parsed.payload)
    assert payload.header.agent_principal_id == "u-12345678"


def test_append_agent_report_increments_attempt_for_same_role_base(tmp_path: Path) -> None:
    state_path = _state_path(tmp_path)
    append_agent_report(
        state=_state(),
        state_path=state_path,
        session_id="SES-001",
        base_id="P18-I01-W04",
        body=_body("first"),
    )
    second = append_agent_report(
        state=_state(),
        state_path=state_path,
        session_id="SES-001",
        base_id="P18-I01-W04",
        body=_body("second"),
    )
    assert second.attempt == 2


def test_append_agent_report_rejects_role_mismatch(tmp_path: Path) -> None:
    body = _body().model_copy(update={"role": "reviewer"})
    with pytest.raises(AgentReportRoleMismatchError, match="does not match session role"):
        append_agent_report(
            state=_state(),
            state_path=_state_path(tmp_path),
            session_id="SES-001",
            base_id="P18-I01-W04",
            body=body,
        )


def test_append_agent_report_writes_researcher_report_with_evidence_refs(tmp_path: Path) -> None:
    result = append_agent_report(
        state=_state(role=AgentSessionRole.RESEARCHER),
        state_path=_state_path(tmp_path),
        session_id="SES-001",
        base_id="P18-I01-W04",
        body=_researcher_body(),
        generated_at=datetime(2026, 5, 14, tzinfo=UTC),
    )
    assert result.store_kind == "researcher_report"
    path = store_path(tmp_path / ".ea" / "state.json", result.envelope.kind)
    parsed = Envelope.model_validate_json(path.read_text(encoding="utf-8").splitlines()[0])
    payload = AgentReportPayload.model_validate(parsed.payload)
    assert payload.header.role is AgentSessionRole.RESEARCHER
    assert payload.body.evidence_refs


def test_append_agent_report_rejects_researcher_pass_without_evidence_refs(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="researcher pass requires non-empty"):
        append_agent_report(
            state=_state(role=AgentSessionRole.RESEARCHER),
            state_path=_state_path(tmp_path),
            session_id="SES-001",
            base_id="P18-I01-W04",
            body=_researcher_body_without_evidence(),
        )


def test_append_agent_report_rejects_unscrubbed_body(tmp_path: Path) -> None:
    with pytest.raises(AgentReportScrubError, match="absolute_posix_path"):
        append_agent_report(
            state=_state(),
            state_path=_state_path(tmp_path),
            session_id="SES-001",
            base_id="P18-I01-W04",
            body=_body("opened /tmp/local-file during execution"),
        )


def test_executor_report_body_verdict_outside_enum_raises_validationerror() -> None:
    with pytest.raises(ValidationError):
        ExecutorReportBody.model_validate(
            {
                "role": "executor",
                "verdict": "approved",
                "confidence": Confidence.HIGH,
                "summary": "implemented report writer",
                "wave_id": "P18-I01-W04",
                "outcome": "done",
            }
        )
