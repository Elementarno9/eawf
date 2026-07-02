"""Tests for agent-report rollup helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    Confidence,
    DispatchNote,
    WaveStatus,
)
from eawf.kernel.state.models import DispatchAnnotation, SessionAttempt, Wave
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import (
    AgentReportHeader,
    AgentReportPayload,
    ExecutorReportBody,
    store_kind_for_role,
)
from eawf.workflow.agent_report.rollup import AgentReportRow, per_wave_attempt_rollup

NOW = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


def _session(
    attempt: int,
    *,
    session_id: str,
    exit_status: int | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
) -> SessionAttempt:
    return SessionAttempt(
        attempt=attempt,
        runtime="codex",
        session_id=session_id,
        session_log_handle=f"urn:eawf:v1:session-log:codex:{session_id}",
        started_at=NOW + timedelta(minutes=attempt),
        ended_at=NOW + timedelta(minutes=attempt + 1),
        exit_status=exit_status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
    )


def _wave() -> Wave:
    return Wave(
        id="P28-I03-W27",
        iter_id="P28-I03",
        title="show wave attempts",
        status=WaveStatus.IN_PROGRESS,
        opened_at=NOW,
        sessions={
            1: _session(
                1,
                session_id="sess-1",
                exit_status=0,
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=1,
                cache_read_input_tokens=2,
            ),
            2: _session(
                2,
                session_id="sess-2",
                exit_status=9,
                input_tokens=20,
                output_tokens=7,
                cache_creation_input_tokens=3,
                cache_read_input_tokens=4,
            ),
        },
        dispatch_history=[
            DispatchAnnotation(
                attempt=1,
                note=DispatchNote.FRESH_DISPATCH,
                runtime_to="codex",
                occurred_at=NOW,
            ),
            DispatchAnnotation(
                attempt=2,
                note=DispatchNote.SWITCH_ON_ERROR,
                runtime_from="codex",
                runtime_to="claude-code",
                occurred_at=NOW + timedelta(minutes=2),
                reason="timeout",
            ),
        ],
    )


def _report(*, attempt: int, verdict: AgentReportVerdict) -> AgentReportRow:
    body = ExecutorReportBody(
        role="executor",
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary="attempt completed",
        wave_id="P28-I03-W27",
        outcome="done",
    )
    report_id = f"AR-executor-P28-I03-W27-{attempt:02d}"
    header = AgentReportHeader(
        report_id=report_id,
        role=AgentSessionRole.EXECUTOR,
        session_id=f"SES-{attempt}",
        scope_id="P28-I03-W27",
        base_id="P28-I03-W27",
        attempt=attempt,
        runtime="codex",
        generated_at=NOW,
        summary=body.summary,
    )
    payload = AgentReportPayload(header=header, body=body)
    envelope = Envelope(
        id=report_id,
        kind=store_kind_for_role(AgentSessionRole.EXECUTOR),
        scope_id="P28-I03-W27",
        created_at=NOW + timedelta(minutes=attempt),
        updated_at=None,
        summary=body.summary,
        payload=payload.model_dump(mode="json"),
    )
    return AgentReportRow(
        envelope=envelope,
        payload=payload,
        store_kind=store_kind_for_role(AgentSessionRole.EXECUTOR).value,
    )


def test_per_wave_attempt_rollup_counts_retry_blocked_tokens_and_error_kinds() -> None:
    rollup = per_wave_attempt_rollup(
        _wave(),
        reports=[
            _report(attempt=1, verdict=AgentReportVerdict.PASS),
            _report(attempt=2, verdict=AgentReportVerdict.BLOCKED),
        ],
        error_kind_by_attempt={2: ["timeout", "timeout", "network_error"]},
    )

    assert rollup.wave_id == "P28-I03-W27"
    assert rollup.attempt_count == 2
    assert rollup.retry_count == 1
    assert rollup.blocked_count == 1
    assert rollup.token_total == 52
    assert rollup.error_kind_breakdown == {"network_error": 1, "timeout": 2}
    assert [row.retry for row in rollup.attempts] == ["initial", "switch"]
    assert [row.blocked for row in rollup.attempts] == ["no", "yes"]
    assert [row.tokens for row in rollup.attempts] == ["18", "34"]


def test_per_wave_attempt_rollup_renders_compact_utc_timestamps() -> None:
    """Attempt started / ended cells show compact UTC, never full isoformat.

    The shared compact-UTC formatter renders ``YYYY-MM-DD HH:MM:SS`` -- no
    microseconds, no ``+00:00`` offset -- so a full attempt row fits the
    operator-facing tables (P30-I22-W09).
    """
    rollup = per_wave_attempt_rollup(_wave())

    for row in rollup.attempts:
        for cell in (row.started, row.ended):
            assert "+00:00" not in cell
            assert "." not in cell
            assert "T" not in cell
    assert rollup.attempts[0].started == "2026-05-27 12:01:00"
    assert rollup.attempts[0].ended == "2026-05-27 12:02:00"


def test_per_wave_attempt_rollup_handles_no_attempts() -> None:
    wave = _wave().model_copy(update={"sessions": {}, "dispatch_history": []})
    rollup = per_wave_attempt_rollup(wave)

    assert rollup.attempts == ()
    assert rollup.attempt_count == 0
    assert rollup.retry_count == 0
    assert rollup.blocked_count == 0
    assert rollup.token_total == 0
    assert rollup.error_kind_breakdown == {}
