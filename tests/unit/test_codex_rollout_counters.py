"""Exact Codex rollout counter parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.state.enums import MeasurementQuality, MeasurementStatus
from eawf.runtime.runtimes.codex.rollout_counters import (
    CODEX_ROLLOUT_MEASURE_VERSION,
    read_codex_rollout_counters,
)

_ROLLOUT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "telemetry"
    / "codex"
    / "rollout-2026-05-14T00-00-00-placeholder-cccc.jsonl"
)


def test_read_codex_rollout_counters_returns_exact_provider_classes() -> None:
    capture = read_codex_rollout_counters(
        _ROLLOUT,
        expected_session_id="sess-placeholder-cccc",
    )
    assert capture.measurement_quality is MeasurementQuality.EXACT
    assert capture.measurement_status is MeasurementStatus.USAGE_OBSERVED
    assert capture.measurement_reason is None
    assert capture.counters is not None
    assert capture.counters.input_tokens == 2500
    assert capture.counters.output_tokens == 800
    assert capture.counters.cache_read_input_tokens == 500
    assert capture.counters.cache_creation_input_tokens is None
    assert capture.counters.cost_usd is None
    assert capture.counters.total_duration_ms == 120_000
    assert capture.counters.harness == "codex"
    assert capture.counters.measure_version == CODEX_ROLLOUT_MEASURE_VERSION


def test_read_codex_rollout_counters_missing_is_unavailable(tmp_path: Path) -> None:
    capture = read_codex_rollout_counters(tmp_path / "missing.jsonl")
    assert capture.counters is None
    assert capture.measurement_quality is MeasurementQuality.UNAVAILABLE
    assert capture.measurement_status is MeasurementStatus.USAGE_UNAVAILABLE
    assert capture.measurement_reason == "missing_transcript"


def test_read_codex_rollout_counters_malformed_is_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        '{"type":"session_meta","payload":{"id":"sess-a"}}\n{bad\n',
        encoding="utf-8",
    )
    capture = read_codex_rollout_counters(path, expected_session_id="sess-a")
    assert capture.counters is None
    assert capture.measurement_reason == "unreadable_transcript:malformed_json"


def test_read_codex_rollout_counters_unreadable_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text("{}\n", encoding="utf-8")

    def _raise_open(*args: object, **kwargs: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "open", _raise_open)
    capture = read_codex_rollout_counters(path)
    assert capture.counters is None
    assert capture.measurement_reason == "unreadable_transcript:PermissionError"


def test_read_codex_rollout_counters_rejects_identity_mismatch() -> None:
    capture = read_codex_rollout_counters(_ROLLOUT, expected_session_id="different")
    assert capture.counters is None
    assert capture.session_id == "sess-placeholder-cccc"
    assert capture.measurement_reason == "session_identity_mismatch"


def test_read_codex_rollout_counters_marks_no_token_evidence(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n".join(
            [
                (
                    '{"timestamp":"2026-01-01T00:00:00Z","type":"session_meta",'
                    '"payload":{"id":"sess-a"}}'
                ),
                (
                    '{"timestamp":"2026-01-01T00:00:01Z","type":"turn_context",'
                    '"payload":{"model":"gpt-test"}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    capture = read_codex_rollout_counters(path, expected_session_id="sess-a")
    assert capture.measurement_quality is MeasurementQuality.UNAVAILABLE
    assert capture.measurement_status is MeasurementStatus.NO_TOKEN_EVIDENCE
    assert capture.measurement_reason == "no_token_evidence"
    assert capture.counters is not None
    assert capture.counters.total_duration_ms == 1000
    assert capture.counters.input_tokens is None
    assert capture.counters.output_tokens is None
    assert capture.counters.cache_read_input_tokens is None


def test_read_codex_rollout_counters_does_not_invent_missing_cache_class(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"sess-a"}}',
                (
                    '{"type":"event_msg","payload":{"type":"token_count","info":'
                    '{"total_token_usage":{"input_tokens":7,"output_tokens":3}}}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    capture = read_codex_rollout_counters(path, expected_session_id="sess-a")
    assert capture.counters is not None
    assert capture.counters.input_tokens is None
    assert capture.counters.cache_read_input_tokens is None
    assert capture.counters.output_tokens == 3


def test_read_codex_rollout_counters_does_not_double_count_reasoning(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text(
        "\n".join(
            [
                '{"type":"session_meta","payload":{"id":"sess-a"}}',
                (
                    '{"type":"event_msg","payload":{"type":"token_count","info":'
                    '{"total_token_usage":{"input_tokens":31751,'
                    '"cached_input_tokens":0,"output_tokens":2367,'
                    '"reasoning_output_tokens":413,"total_tokens":34118}}}}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    capture = read_codex_rollout_counters(path, expected_session_id="sess-a")

    assert capture.measurement_quality is MeasurementQuality.EXACT
    assert capture.measurement_status is MeasurementStatus.USAGE_OBSERVED
    assert capture.counters is not None
    assert capture.counters.input_tokens == 31_751
    assert capture.counters.output_tokens == 2_367
    assert capture.counters.cache_read_input_tokens == 0
