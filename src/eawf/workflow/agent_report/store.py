"""Append-only writer for typed agent report store records."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportBody,
    AgentReportHeader,
    AgentReportPayload,
    report_record_id,
    report_store_urn,
    store_kind_for_role,
)
from eawf.kernel.store.paths import store_path
from eawf.platform.scrub.scan import ScrubFinding, scan_text

_BODY_ADAPTER: TypeAdapter[AgentReportBody] = TypeAdapter(AgentReportBody)


class AgentReportRoleMismatchError(ValueError):
    """Raised when report body role disagrees with the session role."""


class AgentReportScrubError(ValueError):
    """Raised when report body text contains local or sensitive tokens."""


@dataclass(frozen=True)
class ReportAuthor:
    """The identity one agent report is filed under.

    Attributes:
        session_id: The session, or native Run key, the report speaks for.
        role: The role the authority behind the session grants; a body that
            claims another role is refused.
        scope_id: The scope the report is filed against.
        agent_principal_id: The principal behind the agent, when known.
    """

    session_id: str
    role: AgentSessionRole
    scope_id: str
    agent_principal_id: str | None = None


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


def scrub_finding_kinds(body: AgentReportBody) -> tuple[str, ...]:
    """Return the distinct scrub finding kinds in *body*'s text, sorted.

    This is the check :func:`append_report_as` refuses a body on, exposed
    so a producer can run it before the append and re-ask for a clean body.
    Only the kinds are returned, never the matched text, so a caller can name
    the failure without echoing the sensitive value back.

    Args:
        body: The validated report body to scan.

    Returns:
        The sorted distinct finding kinds; empty when the body is clean.
    """
    return tuple(sorted({finding.kind for finding in _scrub_findings(body)}))


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


def append_report_as(
    author: ReportAuthor,
    *,
    state_path: Path,
    base_id: str,
    body: AgentReportBody,
    runtime: str,
    generated_at: datetime | None = None,
    artifact_ids: list[str] | None = None,
    blob_refs: list[str] | None = None,
) -> AgentReportAppendResult:
    """Append one typed agent report under an already resolved *author*.

    Args:
        author: Who the report is filed as; its role is the authority the
            body's own role must match.
        state_path: Path to ``state.json``; the role store resolves under its
            sibling ``store/`` directory, and the document itself is not read.
        base_id: Report ``base_id``; groups monotonic attempts per
            ``(role, base_id)``.
        body: The validated role-report body.
        runtime: Runtime label recorded on the report header.
        generated_at: Generation timestamp; defaults to ``now(UTC)``.
        artifact_ids: Artifact ids attached to the row.
        blob_refs: Blob refs attached to the row.

    Returns:
        The appended envelope, its store URN, its attempt and its store kind.

    Raises:
        AgentReportRoleMismatchError: When body.role differs from the author's role.
        AgentReportScrubError: When body text contains local or sensitive tokens.
    """
    if body.role != author.role.value:
        raise AgentReportRoleMismatchError(
            f"body role {body.role!r} does not match session role {author.role.value!r}"
        )
    kinds = scrub_finding_kinds(body)
    if kinds:
        raise AgentReportScrubError(f"agent report body failed scrub: {', '.join(kinds)}")

    moment = generated_at if generated_at is not None else datetime.now(UTC)
    store_kind = store_kind_for_role(author.role)
    path = store_path(state_path, store_kind)
    attempt = _next_attempt(path, base_id=base_id, role_value=author.role.value)
    report_id = report_record_id(role=author.role, base_id=base_id, attempt=attempt)
    # Fail-fast id-shape invariant: the role token appears exactly once. A
    # doubled prefix (AR-auditor-auditor-...) means a role-prefixed base_id
    # slipped past mint normalization.
    role_token = author.role.value.replace("-", "_")
    expected_prefix = f"AR-{role_token}-"
    if not report_id.startswith(expected_prefix) or report_id[len(expected_prefix) :].startswith(
        f"{role_token}-"
    ):
        raise ValueError(f"minted report id doubles the role prefix: {report_id!r}")
    artifact_list = list(artifact_ids or [])
    blob_list = list(blob_refs or [])
    header = AgentReportHeader(
        report_id=report_id,
        role=author.role,
        session_id=author.session_id,
        scope_id=author.scope_id,
        base_id=base_id,
        attempt=attempt,
        runtime=runtime,
        generated_at=moment,
        summary=body.summary[:500],
        artifact_ids=artifact_list,
        blob_refs=blob_list,
        agent_principal_id=author.agent_principal_id,
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind,
        scope_id=author.scope_id,
        created_at=moment,
        updated_at=None,
        summary=header.summary,
        payload=payload.model_dump(mode="json"),
        blob_refs=blob_list,
        artifact_ids=artifact_list,
    )
    append_envelope(path, envelope)
    urn = report_store_urn(scope_id=author.scope_id, role=author.role, report_id=report_id)
    return AgentReportAppendResult(
        envelope=envelope,
        urn=urn,
        attempt=attempt,
        store_kind=store_kind.value,
    )
