"""Read and roll up typed agent report store records."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from eawf.state.enums import AgentSessionRole
from eawf.store.envelope import Envelope
from eawf.store.kinds.agent_report import AgentReportPayload, store_kind_for_role
from eawf.store.paths import store_path


@dataclass(frozen=True)
class AgentReportRow:
    """One loaded report row."""

    envelope: Envelope
    payload: AgentReportPayload
    store_kind: str

    def as_summary(self) -> dict[str, object]:
        """Return a compact JSON-friendly summary."""
        return {
            "id": self.envelope.id,
            "role": self.payload.header.role.value,
            "scope_id": self.payload.header.scope_id,
            "base_id": self.payload.header.base_id,
            "attempt": self.payload.header.attempt,
            "verdict": self.payload.body.verdict.value,
            "confidence": self.payload.body.confidence.value,
            "summary": self.payload.header.summary,
            "store_kind": self.store_kind,
            "created_at": self.envelope.created_at.isoformat(),
        }


def _report_store_kinds(role: AgentSessionRole | None = None) -> list[tuple[AgentSessionRole, str]]:
    roles = [role] if role is not None else list(AgentSessionRole)
    return [(item, store_kind_for_role(item).value) for item in roles]


def iter_agent_reports(
    state_path: Path,
    *,
    role: AgentSessionRole | None = None,
    base_id: str | None = None,
    scope_id: str | None = None,
) -> list[AgentReportRow]:
    """Return report rows matching the optional filters."""
    rows: list[AgentReportRow] = []
    for role_item, kind_value in _report_store_kinds(role):
        path = store_path(state_path, store_kind_for_role(role_item))
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            envelope = Envelope.model_validate_json(raw_line)
            payload = AgentReportPayload.model_validate(envelope.payload)
            if base_id is not None and payload.header.base_id != base_id:
                continue
            if scope_id is not None and payload.header.scope_id != scope_id:
                continue
            rows.append(AgentReportRow(envelope=envelope, payload=payload, store_kind=kind_value))
    rows.sort(key=lambda row: (row.envelope.created_at, row.envelope.id))
    return rows


def find_agent_report(
    state_path: Path,
    report_id: str,
    *,
    role: AgentSessionRole | None = None,
) -> AgentReportRow | None:
    """Return the report row with *report_id*, or ``None``."""
    for row in iter_agent_reports(state_path, role=role):
        if row.envelope.id == report_id:
            return row
    return None


def operator_rollup(state_path: Path, phase_id: str) -> dict[str, object]:
    """Return a phase-oriented rollup over all reports scoped below *phase_id*."""
    rows = [
        row
        for row in iter_agent_reports(state_path)
        if row.payload.header.scope_id == phase_id
        or row.payload.header.scope_id.startswith(f"{phase_id}-")
        or row.payload.header.base_id == phase_id
        or row.payload.header.base_id.startswith(f"{phase_id}-")
    ]
    by_role = Counter(row.payload.header.role.value for row in rows)
    latest = [row.as_summary() for row in rows[-10:]]
    return {
        "phase_id": phase_id,
        "report_count": len(rows),
        "by_role": dict(sorted(by_role.items())),
        "latest": latest,
    }
