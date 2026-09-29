"""Every measured span names its producer or why it has none, over a closed phase set.

An aggregate over a Run's spans reports the unattributed share beside its
total, so time no producer saw can neither vanish from the total nor be
filed under a phase.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.measurement import (
    MeasuredSpan,
    SpanPhase,
    summarize_spans,
)
from eawf.observability.measurement.transcript_spans import (
    TRANSCRIPT_SPAN_PRODUCER,
    transcript_spans,
)

T0 = datetime(2026, 9, 8, 1, 0, 0, tzinfo=UTC)


def _at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


def _span(
    start: float, end: float, phase: str = "plain_request", **attribution: Any
) -> MeasuredSpan:
    fields = attribution or {"producer": TRANSCRIPT_SPAN_PRODUCER}
    return MeasuredSpan.model_validate(
        {"phase": phase, "started_at": _at(start), "ended_at": _at(end), **fields}
    )


def test_meas_050_phase_set_is_exactly_the_produced_values() -> None:
    assert {phase.value for phase in SpanPhase} == {
        "reasoning_request",
        "plain_request",
        "tool_call",
        "idle",
    }


@pytest.mark.parametrize("phase", ["failed_step", "operator_wait", "", "IDLE"])
def test_meas_050_span_with_undeclared_phase_fails_ingestion(phase: str) -> None:
    with pytest.raises(ValidationError):
        _span(0, 1, phase=phase)


def test_meas_050_unattributed_span_requires_a_reason() -> None:
    with pytest.raises(ValidationError, match="producer or the reason"):
        MeasuredSpan(phase=SpanPhase.IDLE, started_at=_at(0), ended_at=_at(1))


def test_meas_050_span_cannot_name_both_producer_and_reason() -> None:
    with pytest.raises(ValidationError, match="producer or the reason"):
        _span(0, 1, producer="claude-transcript", unattributed_reason="unknown")


def test_meas_050_span_cannot_end_before_it_starts() -> None:
    with pytest.raises(ValidationError, match="end before"):
        _span(2, 1)


def test_meas_050_span_refuses_unknown_field() -> None:
    with pytest.raises(ValidationError):
        MeasuredSpan.model_validate(
            {
                "phase": "idle",
                "started_at": _at(0),
                "ended_at": _at(1),
                "producer": "p",
                "weight": 2,
            }
        )


def test_meas_050_summary_reports_unattributed_share_beside_total() -> None:
    spans = [
        _span(0, 3, "reasoning_request"),
        _span(3, 5, "tool_call"),
        _span(5, 8, "idle", unattributed_reason="no transcript row covers the interval"),
    ]

    summary = summarize_spans(spans)

    assert (summary.total_ms, summary.attributed_ms, summary.unattributed_ms) == (8000, 5000, 3000)
    assert summary.unattributed_share == Decimal(3) / Decimal(8)
    assert summary.phase_ms[SpanPhase.IDLE] == 0
    assert summary.phase_ms[SpanPhase.REASONING_REQUEST] == 3000


def test_meas_050_summary_of_single_zero_length_span_has_zero_share() -> None:
    summary = summarize_spans([_span(1, 1)])

    assert summary.total_ms == 0
    assert summary.unattributed_share == 0


def test_meas_050_summary_refuses_empty_collection() -> None:
    with pytest.raises(ValueError, match="empty"):
        summarize_spans([])


def _row(kind: str, second: float, **message: Any) -> dict[str, Any]:
    stamp = _at(second).isoformat().replace("+00:00", "Z")
    return {"type": kind, "timestamp": stamp, "message": message}


def test_meas_050_transcript_spans_tile_the_window_with_phases() -> None:
    rows = [
        _row("user", 1, content="go"),
        _row("assistant", 3, id="m1", content=[{"type": "thinking", "thinking": "..."}]),
        _row("assistant", 4, id="m1", content=[{"type": "tool_use", "id": "t1"}]),
        _row("user", 7, content=[{"type": "tool_result", "tool_use_id": "t1"}]),
        _row("assistant", 8, id="m2", content=[{"type": "text", "text": "done"}]),
    ]

    spans = transcript_spans(rows, window_start=_at(0), window_end=_at(10))
    summary = summarize_spans(spans)

    assert summary.total_ms == 10_000
    assert summary.phase_ms[SpanPhase.REASONING_REQUEST] == 3000
    assert summary.phase_ms[SpanPhase.TOOL_CALL] == 3000
    assert summary.phase_ms[SpanPhase.PLAIN_REQUEST] == 1000
    assert summary.unattributed_ms == 3000
    assert all(
        span.unattributed_reason == "no transcript row covers the interval"
        for span in spans
        if span.producer is None
    )


def test_meas_050_transcript_spans_clip_rows_outside_the_window() -> None:
    rows = [_row("user", 0, content="a"), _row("assistant", 10, id="m", content=[])]

    spans = transcript_spans(rows, window_start=_at(4), window_end=_at(6))

    assert [(s.started_at, s.ended_at, s.producer) for s in spans] == [
        (_at(4), _at(6), TRANSCRIPT_SPAN_PRODUCER)
    ]


def test_meas_050_window_with_no_rows_is_wholly_unattributed() -> None:
    spans = transcript_spans([], window_start=_at(0), window_end=_at(5))

    assert len(spans) == 1
    assert spans[0].producer is None
    assert summarize_spans(spans).unattributed_share == 1


def test_meas_050_empty_window_yields_no_spans() -> None:
    assert transcript_spans([], window_start=_at(1), window_end=_at(1)) == ()


def test_meas_050_inverted_window_is_refused() -> None:
    with pytest.raises(ValueError, match="end before"):
        transcript_spans([], window_start=_at(2), window_end=_at(1))
