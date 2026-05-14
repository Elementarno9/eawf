"""Append-only writer for typed agent report store records."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from eawf.scrub.scan import ScrubFinding, scan_text
from eawf.state.models import AgentSession, State
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.kinds.agent_report import (
    AgentReportBody,
    AgentReportHeader,
    AgentReportPayload,
    report_record_id,
    report_store_urn,
    store_kind_for_role,
)
from eawf.store.paths import store_path

_BODY_ADAPTER: TypeAdapter[AgentReportBody] = TypeAdapter(AgentReportBody)


class AgentReportRoleMismatchError(ValueError):
    """Raised when report body role disagrees with the session role."""


class AgentReportScrubError(ValueError):
    """Raised when report body text contains local or sensitive tokens."""


@dataclass(frozen=True)
class AgentReportAppendResult:
    """Result of appending one agent report."""

    envelope: Envelope
    urn: str
    attempt: int
    store_kind: str


def parse_agent_report_body(raw: Any) -> AgentReportBody:
    """Validate *raw* as one of the role-specific agent report bodies."""
    return _BODY_ADAPTER.validate_python(raw)


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _scrub_findings(body: AgentReportBody) -> list[ScrubFinding]:
    findings: list[ScrubFinding] = []
    for text in _iter_strings(body.model_dump(mode="json")):
        findings.extend(scan_text(text))
    return findings


def _session_for(state: State, session_id: str) -> AgentSession:
    session = state.agent_sessions.get(session_id)
    if session is None:
        raise KeyError(f"unknown agent session: {session_id!r}")
    return session


def _next_attempt(path: Path, *, base_id: str, role_value: str) -> int:
    if not path.exists():
        return 1
    max_attempt = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        payload = AgentReportPayload.model_validate(envelope.payload)
        if payload.header.base_id == base_id and payload.header.role.value == role_value:
            max_attempt = max(max_attempt, payload.header.attempt)
    return max_attempt + 1


def append_agent_report(
    *,
    state: State,
    state_path: Path,
    session_id: str,
    base_id: str,
    body: AgentReportBody,
    runtime: str | None = None,
    generated_at: datetime | None = None,
    artifact_ids: list[str] | None = None,
    blob_refs: list[str] | None = None,
) -> AgentReportAppendResult:
    """Append one typed agent report using the session as authority.

    Raises:
        KeyError: When *session_id* is not present in state.
        AgentReportRoleMismatchError: When body.role differs from the session role.
        AgentReportScrubError: When body text contains local or sensitive tokens.
    """
    session = _session_for(state, session_id)
    if body.role != session.role.value:
        raise AgentReportRoleMismatchError(
            f"body role {body.role!r} does not match session role {session.role.value!r}"
        )
    findings = _scrub_findings(body)
    if findings:
        kinds = ", ".join(sorted({finding.kind for finding in findings}))
        raise AgentReportScrubError(f"agent report body failed scrub: {kinds}")

    moment = generated_at if generated_at is not None else datetime.now(UTC)
    store_kind = store_kind_for_role(session.role)
    path = store_path(state_path, store_kind)
    attempt = _next_attempt(path, base_id=base_id, role_value=session.role.value)
    report_id = report_record_id(role=session.role, base_id=base_id, attempt=attempt)
    artifact_list = list(artifact_ids or [])
    blob_list = list(blob_refs or [])
    header = AgentReportHeader(
        report_id=report_id,
        role=session.role,
        session_id=session.id,
        scope_id=session.scope_id,
        base_id=base_id,
        attempt=attempt,
        runtime=runtime or session.runtime,
        generated_at=moment,
        summary=body.summary[:500],
        artifact_ids=artifact_list,
        blob_refs=blob_list,
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind,
        scope_id=session.scope_id,
        created_at=moment,
        updated_at=None,
        summary=header.summary,
        payload=payload.model_dump(mode="json"),
        blob_refs=blob_list,
        artifact_ids=artifact_list,
    )
    append_envelope(path, envelope)
    urn = report_store_urn(scope_id=session.scope_id, role=session.role, report_id=report_id)
    return AgentReportAppendResult(
        envelope=envelope,
        urn=urn,
        attempt=attempt,
        store_kind=store_kind.value,
    )
