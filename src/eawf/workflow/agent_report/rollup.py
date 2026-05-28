"""Read and roll up typed agent report store records."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from eawf.kernel.state.enums import AgentReportVerdict, AgentSessionRole, DispatchNote
from eawf.kernel.state.models import DispatchAnnotation, SessionAttempt, Wave
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AgentReportPayload, store_kind_for_role
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.models import TelemetrySession, TelemetryToolCall
from eawf.observability.telemetry.store.base import AbstractMetricsStore


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


@dataclass(frozen=True)
class WaveAttemptTimelineRow:
    """One row in a per-wave attempt timeline."""

    attempt: int
    runtime: str
    started: str
    ended: str
    exit_status: str
    retry: str
    blocked: str
    tokens: str


@dataclass(frozen=True)
class PerWaveAttemptRollup:
    """Per-wave attempt/retry/block/token rollup."""

    wave_id: str
    attempts: tuple[WaveAttemptTimelineRow, ...]
    attempt_count: int
    retry_count: int
    blocked_count: int
    token_total: int
    error_kind_breakdown: dict[str, int]


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


def per_wave_attempt_rollup(
    wave: Wave,
    *,
    reports: Iterable[AgentReportRow] = (),
    error_kind_by_attempt: Mapping[int, Iterable[str]] | None = None,
) -> PerWaveAttemptRollup:
    """Return per-attempt timeline facts for *wave*.

    The helper is intentionally store-agnostic: callers that have report or
    telemetry rows pass them in, while the TUI can still render state-only
    session/dispatch attempts.

    Args:
        wave: The wave whose attempts should be folded.
        reports: Optional agent-report rows scoped to the wave.
        error_kind_by_attempt: Optional tool/runtime error kinds keyed by
            attempt number.

    Returns:
        Attempt rows plus aggregate retry/block/token/error-kind counts.
    """
    report_by_attempt = _latest_report_by_attempt(wave.id, reports)
    annotation_by_attempt = _latest_annotation_by_attempt(wave.dispatch_history)
    error_kinds = error_kind_by_attempt or {}
    attempt_numbers = set(wave.sessions) | set(annotation_by_attempt) | set(report_by_attempt)
    attempt_numbers.update(error_kinds)

    rows: list[WaveAttemptTimelineRow] = []
    token_total = 0
    blocked_count = 0
    retry_count = 0
    error_kind_counter: Counter[str] = Counter()
    for attempt_no in sorted(attempt_numbers):
        session = wave.sessions.get(attempt_no)
        report = report_by_attempt.get(attempt_no)
        annotation = annotation_by_attempt.get(attempt_no)
        tokens = _session_token_total(session)
        if tokens is not None:
            token_total += tokens
        retry = _retry_label(attempt_no, annotation)
        if retry != "initial":
            retry_count += 1
        blocked = _blocked_label(report)
        if blocked == "yes":
            blocked_count += 1
        for error_kind in error_kinds.get(attempt_no, ()):
            error_kind_counter[error_kind] += 1
        rows.append(
            WaveAttemptTimelineRow(
                attempt=attempt_no,
                runtime=_runtime_label(session, report, annotation),
                started=_attempt_dt(getattr(session, "started_at", None)),
                ended=_attempt_dt(getattr(session, "ended_at", None)),
                exit_status=_exit_status_label(session),
                retry=retry,
                blocked=blocked,
                tokens=str(tokens) if tokens is not None else "-",
            )
        )

    return PerWaveAttemptRollup(
        wave_id=wave.id,
        attempts=tuple(rows),
        attempt_count=len(rows),
        retry_count=retry_count,
        blocked_count=blocked_count,
        token_total=token_total,
        error_kind_breakdown=dict(sorted(error_kind_counter.items())),
    )


def error_kind_by_attempt_from_store(
    wave: Wave,
    store: AbstractMetricsStore,
) -> dict[int, tuple[str, ...]]:
    """Return telemetry tool-call error kinds keyed by wave attempt."""
    attempt_by_session_id = {
        session.session_id: attempt_no
        for attempt_no, session in wave.sessions.items()
        if session.session_id
    }
    for row in store.fetch_all("telemetry_sessions", TelemetrySession):
        session = row
        if session.wave_id != wave.id:
            continue
        attempt_no = _parse_attempt_id(session.attempt_id)
        if attempt_no is not None:
            attempt_by_session_id[session.session_id] = attempt_no

    kinds_by_attempt: dict[int, list[str]] = {}
    if not attempt_by_session_id:
        return {}
    for row in store.fetch_all("telemetry_tool_calls", TelemetryToolCall):
        call = row
        if not call.is_error:
            continue
        attempt_no = attempt_by_session_id.get(call.session_id)
        if attempt_no is None:
            continue
        kinds_by_attempt.setdefault(attempt_no, []).append(call.error_kind.value)
    return {attempt: tuple(kinds) for attempt, kinds in kinds_by_attempt.items()}


def _parse_attempt_id(value: str | None) -> int | None:
    """Parse telemetry attempt id variants into an integer attempt number."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if stripped.isdigit():
        parsed = int(stripped)
        return parsed if parsed >= 1 else None
    for separator in ("-", "_", ":"):
        tail = stripped.rsplit(separator, 1)[-1]
        if tail.isdigit():
            parsed = int(tail)
            return parsed if parsed >= 1 else None
    return None


def _latest_report_by_attempt(
    wave_id: str,
    reports: Iterable[AgentReportRow],
) -> dict[int, AgentReportRow]:
    """Return latest report row per attempt for *wave_id*."""
    latest: dict[int, AgentReportRow] = {}
    for row in reports:
        if row.payload.header.base_id != wave_id and row.payload.header.scope_id != wave_id:
            continue
        attempt = row.payload.header.attempt
        previous = latest.get(attempt)
        if previous is None or (previous.envelope.created_at, previous.envelope.id) < (
            row.envelope.created_at,
            row.envelope.id,
        ):
            latest[attempt] = row
    return latest


def _latest_annotation_by_attempt(
    annotations: Iterable[DispatchAnnotation],
) -> dict[int, DispatchAnnotation]:
    """Return latest dispatch annotation per attempt."""
    latest: dict[int, DispatchAnnotation] = {}
    for annotation in annotations:
        previous = latest.get(annotation.attempt)
        if previous is None or previous.occurred_at < annotation.occurred_at:
            latest[annotation.attempt] = annotation
    return latest


def _session_token_total(session: SessionAttempt | None) -> int | None:
    """Return total tracked tokens for one session attempt."""
    if session is None:
        return None
    values = (
        session.input_tokens,
        session.output_tokens,
        session.cache_creation_input_tokens,
        session.cache_read_input_tokens,
    )
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def _retry_label(attempt_no: int, annotation: DispatchAnnotation | None) -> str:
    """Return compact retry/switch label for one attempt."""
    if annotation is None:
        return "retry" if attempt_no > 1 else "initial"
    if annotation.note is DispatchNote.FRESH_DISPATCH:
        return "initial" if attempt_no == 1 else "fresh"
    if annotation.note is DispatchNote.CONTINUE_FROM_SESSION:
        return "continue"
    if annotation.note is DispatchNote.CONTINUE_FAILED_FELL_BACK_TO_FRESH:
        return "fallback"
    if annotation.note is DispatchNote.SWITCH_ON_ERROR:
        return "switch"
    if annotation.note is DispatchNote.SWITCH_MANUAL:
        return "manual"
    return annotation.note.value


def _blocked_label(report: AgentReportRow | None) -> str:
    """Return whether the report verdict marks this attempt blocked."""
    if report is None:
        return "no"
    return "yes" if report.payload.body.verdict is AgentReportVerdict.BLOCKED else "no"


def _runtime_label(
    session: SessionAttempt | None,
    report: AgentReportRow | None,
    annotation: DispatchAnnotation | None,
) -> str:
    """Return runtime label from session, report, annotation, or fallback."""
    if session is not None:
        return session.runtime
    if report is not None:
        return report.payload.header.runtime
    if annotation is not None:
        return annotation.runtime_to or annotation.runtime_from or "-"
    return "-"


def _attempt_dt(value: object) -> str:
    """Return compact datetime string for attempt tables."""
    if value is None:
        return "-"
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)


def _exit_status_label(session: SessionAttempt | None) -> str:
    """Return subprocess exit status label."""
    if session is None or session.exit_status is None:
        return "-"
    return str(session.exit_status)


__all__ = [
    "AgentReportRow",
    "PerWaveAttemptRollup",
    "WaveAttemptTimelineRow",
    "error_kind_by_attempt_from_store",
    "find_agent_report",
    "iter_agent_reports",
    "operator_rollup",
    "per_wave_attempt_rollup",
]
