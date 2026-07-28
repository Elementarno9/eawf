"""Exact runtime counters from one Codex rollout transcript.

Hook payloads provide the exact rollout path. This reader consumes only that
path and, when supplied, verifies the provider session id from ``session_meta``.
It never discovers a "latest" transcript. Missing, unreadable, malformed, or
identity-mismatched input produces an explicit unavailable result with nullable
counters; absence is never projected as zero usage.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from eawf.kernel.state.enums import MeasurementQuality, MeasurementStatus
from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters

CODEX_ROLLOUT_MEASURE_VERSION = 201


class CodexRolloutCapture(BaseModel):
    """Typed result of reading one exact Codex rollout path."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    counters: RuntimeCounters | None = None
    measurement_quality: MeasurementQuality
    measurement_status: MeasurementStatus
    measurement_reason: str | None = None


@dataclass
class _RolloutFold:
    session_id: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    latest_totals: dict[str, Any] | None = None
    record_count: int = 0
    malformed: bool = False


def _non_negative_int(raw: object) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def _timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _unavailable(reason: str, *, session_id: str | None = None) -> CodexRolloutCapture:
    return CodexRolloutCapture(
        session_id=session_id,
        counters=None,
        measurement_quality=MeasurementQuality.UNAVAILABLE,
        measurement_status=MeasurementStatus.USAGE_UNAVAILABLE,
        measurement_reason=reason,
    )


def _usage_counters(
    totals: dict[str, Any] | None,
    *,
    started_at: datetime | None,
    ended_at: datetime | None,
    model: str | None,
) -> RuntimeCounters:
    total_duration_ms: int | None = None
    if started_at is not None and ended_at is not None:
        total_duration_ms = max(int((ended_at - started_at).total_seconds() * 1000), 0)

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    if totals is not None:
        provider_input = _non_negative_int(totals.get("input_tokens"))
        provider_cached = _non_negative_int(totals.get("cached_input_tokens"))
        provider_output = _non_negative_int(totals.get("output_tokens"))
        if provider_input is not None and provider_cached is not None:
            input_tokens = max(provider_input - provider_cached, 0)
        cache_read_input_tokens = provider_cached
        # Codex's cumulative output_tokens already includes the reasoning
        # subset. Keep the provider total intact instead of double-counting
        # reasoning_output_tokens.
        output_tokens = provider_output

    return RuntimeCounters(
        total_duration_ms=total_duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=None,
        cache_read_input_tokens=cache_read_input_tokens,
        cost_usd=None,
        harness="codex",
        model=model,
        measure_version=CODEX_ROLLOUT_MEASURE_VERSION,
    )


def _fold_timestamp(fold: _RolloutFold, raw: object) -> None:
    occurred_at = _timestamp(raw)
    if occurred_at is None:
        return
    if fold.started_at is None or occurred_at < fold.started_at:
        fold.started_at = occurred_at
    if fold.ended_at is None or occurred_at > fold.ended_at:
        fold.ended_at = occurred_at


def _fold_provider_record(fold: _RolloutFold, record: dict[str, Any]) -> None:
    fold.record_count += 1
    _fold_timestamp(fold, record.get("timestamp"))
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return
    record_type = record.get("type")
    if record_type == "session_meta" and fold.session_id is None:
        raw_session_id = payload.get("id")
        if isinstance(raw_session_id, str) and raw_session_id:
            fold.session_id = raw_session_id
        return
    if record_type == "turn_context" and fold.model is None:
        raw_model = payload.get("model")
        if isinstance(raw_model, str) and raw_model:
            fold.model = raw_model
        return
    if record_type != "event_msg" or payload.get("type") != "token_count":
        return
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    totals = info.get("total_token_usage")
    if isinstance(totals, dict):
        fold.latest_totals = totals


def _read_rollout(path: Path) -> tuple[_RolloutFold | None, str | None]:
    fold = _RolloutFold()
    try:
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    fold.malformed = True
                    continue
                if not isinstance(record, dict):
                    fold.malformed = True
                    continue
                _fold_provider_record(fold, record)
    except (OSError, UnicodeError) as exc:
        return None, f"unreadable_transcript:{type(exc).__name__}"
    if fold.malformed:
        return fold, "unreadable_transcript:malformed_json"
    if fold.record_count == 0:
        return fold, "unreadable_transcript:empty"
    return fold, None


def read_codex_rollout_counters(
    path: Path,
    *,
    expected_session_id: str | None = None,
) -> CodexRolloutCapture:
    """Read exact counters from *path* without transcript discovery.

    Args:
        path: Provider-supplied rollout JSONL path.
        expected_session_id: Provider session or subagent id that must equal
            ``session_meta.payload.id`` when supplied.

    Returns:
        Exact usage when a valid cumulative ``token_count`` row exists. A
        parseable rollout without token evidence retains model/duration
        counters but reports ``no_token_evidence``. Missing, unreadable,
        malformed, and identity-mismatched rollouts return no counters.
    """
    if not path.exists():
        return _unavailable("missing_transcript")

    fold, error = _read_rollout(path)
    if fold is None:
        assert error is not None
        return _unavailable(error)
    if error is not None:
        return _unavailable(error, session_id=fold.session_id)
    if expected_session_id is not None:
        if fold.session_id is None:
            return _unavailable("session_identity_missing")
        if fold.session_id != expected_session_id:
            return _unavailable("session_identity_mismatch", session_id=fold.session_id)

    counters = _usage_counters(
        fold.latest_totals,
        started_at=fold.started_at,
        ended_at=fold.ended_at,
        model=fold.model,
    )
    if fold.latest_totals is None:
        return CodexRolloutCapture(
            session_id=fold.session_id,
            counters=counters,
            measurement_quality=MeasurementQuality.UNAVAILABLE,
            measurement_status=MeasurementStatus.NO_TOKEN_EVIDENCE,
            measurement_reason="no_token_evidence",
        )
    if all(
        value is None
        for value in (
            counters.input_tokens,
            counters.output_tokens,
            counters.cache_read_input_tokens,
        )
    ):
        return _unavailable("invalid_token_evidence", session_id=fold.session_id)
    return CodexRolloutCapture(
        session_id=fold.session_id,
        counters=counters,
        measurement_quality=MeasurementQuality.EXACT,
        measurement_status=MeasurementStatus.USAGE_OBSERVED,
        measurement_reason=None,
    )


__all__ = [
    "CODEX_ROLLOUT_MEASURE_VERSION",
    "CodexRolloutCapture",
    "read_codex_rollout_counters",
]
