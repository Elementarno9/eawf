"""A streamed message's usage is its completing row's, and it ends at its last row.

Claude Code writes one streamed assistant message across several rows that
share a message id. The first row's ``output_tokens`` is the count streamed
so far, so reading it -- or any field-wise maximum across the rows -- books
a record no request produced. These tests drive the real row loop with rows
carrying differing output under one message id.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eawf.runtime.runtimes.claude.transcript_counters import aggregate_transcript_counters

_MODEL = "claude-opus-4-1"


def _assistant(stamp: str, *, output: int, cache_read: int, block: str = "text") -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": stamp,
        "message": {
            "id": "msg_streamed",
            "model": _MODEL,
            "usage": {
                "input_tokens": 12,
                "output_tokens": output,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": cache_read,
            },
            "content": [{"type": block, "text": "x"}],
        },
    }


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def _streamed(tmp_path: Path) -> Path:
    """One message over three rows: output grows, cache read peaks mid-stream."""
    return _write(
        tmp_path / "session.jsonl",
        [
            {"type": "user", "timestamp": "2026-09-08T01:00:00Z", "message": {"content": "go"}},
            _assistant("2026-09-08T01:00:02Z", output=1, cache_read=900, block="thinking"),
            _assistant("2026-09-08T01:00:05Z", output=40, cache_read=950),
            _assistant("2026-09-08T01:00:09Z", output=310, cache_read=500),
        ],
    )


def test_meas_002_streamed_message_usage_is_its_completing_row(tmp_path: Path) -> None:
    counters = aggregate_transcript_counters(_streamed(tmp_path))

    assert counters is not None
    assert counters.output_tokens == 310
    assert counters.input_tokens == 12


def test_meas_002_streamed_usage_is_no_field_wise_maximum(tmp_path: Path) -> None:
    counters = aggregate_transcript_counters(_streamed(tmp_path))

    assert counters is not None
    # 950 is the largest cache read any row shows; the completing row says 500.
    assert counters.cache_read_input_tokens == 500


def test_meas_002_repeated_completed_record_counts_once(tmp_path: Path) -> None:
    rows = [
        _assistant("2026-09-08T01:00:02Z", output=310, cache_read=500),
        _assistant("2026-09-08T01:00:03Z", output=310, cache_read=500),
    ]

    counters = aggregate_transcript_counters(_write(tmp_path / "s.jsonl", rows))

    assert counters is not None
    assert counters.output_tokens == 310


def test_meas_002_single_row_message_reads_that_row(tmp_path: Path) -> None:
    rows = [_assistant("2026-09-08T01:00:02Z", output=7, cache_read=3)]

    counters = aggregate_transcript_counters(_write(tmp_path / "s.jsonl", rows))

    assert counters is not None
    assert (counters.output_tokens, counters.cache_read_input_tokens) == (7, 3)


def test_meas_002_message_cut_mid_stream_reads_its_last_row_before_the_cut(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 9, 8, 1, 0, 6, tzinfo=UTC)

    counters = aggregate_transcript_counters(_streamed(tmp_path), as_of=as_of)

    assert counters is not None
    assert counters.output_tokens == 40


def test_meas_002_streamed_message_span_ends_at_its_last_row(tmp_path: Path) -> None:
    from eawf.observability.measurement.transcript_spans import transcript_spans
    from eawf.runtime.runtimes.claude.transcript_counters import read_transcript

    reading = read_transcript(_streamed(tmp_path))
    assert reading is not None

    spans = transcript_spans(
        reading.rows,
        window_start=datetime(2026, 9, 8, 1, 0, 0, tzinfo=UTC),
        window_end=datetime(2026, 9, 8, 1, 0, 9, tzinfo=UTC),
    )

    request = [span for span in spans if span.producer is not None]
    assert request[-1].ended_at == datetime(2026, 9, 8, 1, 0, 9, tzinfo=UTC)
    assert {span.phase.value for span in request} == {"reasoning_request"}
    assert sum((s.ended_at - s.started_at).total_seconds() for s in request) == 9
