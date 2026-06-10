"""Claude Code runtime counter parser.

Statusline payloads expose session runtime/cost figures under ``cost`` and
last-call token figures under ``context_window.current_usage``. This module
normalises that runtime-owned shape into a small typed model while degrading
like the statusline token helpers: absent or wrong-typed fields are omitted
rather than raising.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RuntimeCounters(BaseModel):
    """Typed runtime counters parsed from a Claude Code payload."""

    model_config = ConfigDict(extra="forbid")

    api_duration_ms: int | None = Field(default=None, ge=0)
    total_duration_ms: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)


def _coerce_non_negative_int(raw: Any) -> int | None:
    """Return *raw* when it is a non-negative JSON integer, else ``None``."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    return raw


def _coerce_non_negative_decimal(raw: Any) -> Decimal | None:
    """Return *raw* as an exact non-negative decimal, else ``None``."""
    if isinstance(raw, bool) or not isinstance(raw, int | float | Decimal):
        return None
    try:
        value = Decimal(str(raw))
    except InvalidOperation, ValueError:
        return None
    if not value.is_finite() or value < 0:
        return None
    return value


def _first_int(mapping: dict[str, Any], *keys: str) -> int | None:
    """Return first valid non-negative integer among *keys* in *mapping*."""
    for key in keys:
        value = _coerce_non_negative_int(mapping.get(key))
        if value is not None:
            return value
    return None


def _first_decimal(mapping: dict[str, Any], *keys: str) -> Decimal | None:
    """Return first valid non-negative decimal among *keys* in *mapping*."""
    for key in keys:
        value = _coerce_non_negative_decimal(mapping.get(key))
        if value is not None:
            return value
    return None


def _current_usage_block(claude_payload: dict[str, Any]) -> dict[str, Any]:
    """Return Claude's current usage block, or an empty mapping."""
    context_window = claude_payload.get("context_window")
    if not isinstance(context_window, dict):
        return {}
    current_usage = context_window.get("current_usage")
    if not isinstance(current_usage, dict):
        return {}
    return current_usage


def parse_runtime_counters(claude_payload: dict[str, Any]) -> RuntimeCounters | None:
    """Parse Claude Code runtime counters, returning ``None`` without a cost block.

    Args:
        claude_payload: Decoded Claude Code statusline/hook payload.

    Returns:
        Parsed :class:`RuntimeCounters` when ``payload["cost"]`` is a mapping,
        otherwise ``None``. Wrong-typed fields are omitted, not raised.
    """
    cost = claude_payload.get("cost")
    if not isinstance(cost, dict):
        return None

    current_usage = _current_usage_block(claude_payload)
    data: dict[str, int | Decimal] = {}

    api_duration_ms = _first_int(cost, "api_duration_ms", "total_api_duration_ms")
    if api_duration_ms is not None:
        data["api_duration_ms"] = api_duration_ms

    total_duration_ms = _first_int(cost, "total_duration_ms")
    if total_duration_ms is not None:
        data["total_duration_ms"] = total_duration_ms

    cost_usd = _first_decimal(cost, "cost_usd", "total_cost_usd")
    if cost_usd is not None:
        data["cost_usd"] = cost_usd

    for field_name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        value = _coerce_non_negative_int(current_usage.get(field_name))
        if value is not None:
            data[field_name] = value

    return RuntimeCounters.model_validate(data)


__all__ = [
    "RuntimeCounters",
    "parse_runtime_counters",
]
