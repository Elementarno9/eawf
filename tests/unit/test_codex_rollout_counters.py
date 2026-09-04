"""Exact Codex rollout counter parsing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import MeasurementQuality, MeasurementStatus
from eawf.runtime.runtimes.codex.rollout_counters import (
    CODEX_ROLLOUT_MEASURE_VERSION,
    read_codex_rollout_counters,
)

_CODEX_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry" / "codex"

_ROLLOUT = _CODEX_FIXTURES / "rollout-2026-05-14T00-00-00-placeholder-cccc.jsonl"


def _fixture_token_usage_blocks() -> list[tuple[str, dict[str, Any]]]:
    """Yield every ``*_token_usage`` block in the committed codex rollouts."""
    blocks: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(_CODEX_FIXTURES.glob("rollout-*.jsonl")):
        for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            for key in ("total_token_usage", "last_token_usage"):
                usage = info.get(key)
                if isinstance(usage, dict):
                    blocks.append((f"{path.name}:{line_no}:{key}", usage))
    return blocks


def test_codex_rollout_fixtures_hold_vendor_subset_relation() -> None:
    """Every committed rollout must encode the vendor's real token algebra.

    A fixture that instead encodes ``total = input + output + reasoning``
    cannot adjudicate whether the reader double-counts reasoning, so the
    relation is asserted on the fixture itself.
    """
    blocks = _fixture_token_usage_blocks()
    assert blocks, "no token_count blocks found in the codex rollout fixtures"
    for label, usage in blocks:
        input_tokens = usage["input_tokens"]
        output_tokens = usage["output_tokens"]
        reasoning = usage["reasoning_output_tokens"]
        assert usage["total_tokens"] == input_tokens + output_tokens, label
        assert 0 < reasoning <= output_tokens, label


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
