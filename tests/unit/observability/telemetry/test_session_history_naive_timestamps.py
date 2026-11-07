"""Tests that a timestamp without an offset is read as UTC by both history parsers.

Every transcript here is a synthetic fixture written under ``tmp_path``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.telemetry.models import ObservedSession
from eawf.observability.telemetry.projector import sweep_session_history
from eawf.observability.telemetry.sources.codex_session import CodexSessionSource, _parse_ts
from eawf.observability.telemetry.store import SqliteMetricsStore

_AWARE_TS = "2026-11-06T10:00:00Z"
_NAIVE_TS = "2026-11-06T10:05:00"
_START = datetime(2026, 11, 6, 10, 0, tzinfo=UTC)
_END = datetime(2026, 11, 6, 10, 5, tzinfo=UTC)


def _write(path: Path, records: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _claude_records() -> list[dict[str, Any]]:
    return [
        {
            "type": "user",
            "sessionId": "naive-session",
            "timestamp": _AWARE_TS,
            "message": {"content": "x"},
        },
        {
            "type": "assistant",
            "sessionId": "naive-session",
            "timestamp": _NAIVE_TS,
            "message": {
                "id": "m1",
                "model": "claude-opus-4-7",
                "content": [],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
    ]


def test_claude_naive_timestamp_reads_as_utc(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    root = tmp_path / "history"
    _write(root / "naive.jsonl", _claude_records())

    funnel = sweep_session_history(store, root, project_id="repo/demo")

    assert funnel.drops == []
    rows = store.fetch_all("telemetry_observed_sessions", ObservedSession)
    sessions = [r for r in rows if isinstance(r, ObservedSession)]
    assert len(sessions) == 1
    session = sessions[0]
    assert session.started_at is not None
    assert session.ended_at is not None
    assert session.started_at.tzinfo is not None
    assert session.ended_at.tzinfo is not None
    assert session.started_at == _START
    assert session.ended_at == _END
    assert session.started_at.isoformat() == "2026-11-06T10:00:00+00:00"
    assert session.ended_at.isoformat() == "2026-11-06T10:05:00+00:00"


def test_codex_naive_timestamp_reads_as_utc(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "rollout-naive.jsonl",
        [
            {
                "timestamp": _AWARE_TS,
                "type": "session_meta",
                "payload": {"id": "codex-naive", "cwd": "repo"},
            },
            {
                "timestamp": _NAIVE_TS,
                "type": "turn_context",
                "payload": {"model": "gpt-5"},
            },
        ],
    )

    rows = list(CodexSessionSource().iter_rows(path))

    assert len(rows) == 1
    started_at = rows[0].started_at
    assert started_at is not None
    assert started_at.tzinfo is not None
    assert started_at.isoformat() == "2026-11-06T10:00:00+00:00"
    parsed = _parse_ts(_NAIVE_TS)
    assert parsed == _END
    assert parsed is not None
    assert parsed.utcoffset() is not None
    assert parsed.utcoffset() == UTC.utcoffset(None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-11-06T10:05:00", "2026-11-06T10:05:00+00:00"),
        ("2026-11-06T10:05:00Z", "2026-11-06T10:05:00+00:00"),
        ("2026-11-06T12:05:00+02:00", "2026-11-06T12:05:00+02:00"),
        ("2026-11-06", "2026-11-06T00:00:00+00:00"),
    ],
)
def test_codex_parse_ts_keeps_offsets_and_defaults_utc(raw: str, expected: str) -> None:
    parsed = _parse_ts(raw)
    assert parsed is not None
    assert parsed.isoformat() == expected


@pytest.mark.parametrize("raw", [None, "", 0, "not-a-time", ["2026-11-06"]])
def test_codex_parse_ts_rejects_absent_or_malformed(raw: object) -> None:
    assert _parse_ts(raw) is None
