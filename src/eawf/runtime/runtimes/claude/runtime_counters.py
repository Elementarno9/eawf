"""Claude Code runtime counter parser.

Statusline payloads expose session runtime/cost figures under ``cost`` and
last-call token figures under ``context_window.current_usage``. This module
normalises that runtime-owned shape into a small typed model while degrading
like the statusline token helpers: absent or wrong-typed fields are omitted
rather than raising.

The ``cost`` block is *statusline* data. Claude Code's hook payloads carry no
such block, so this parser is not the counter source on the hook path -- see
:mod:`eawf.runtime.runtimes.claude.transcript_counters`, which aggregates the
session transcript the Stop payload points at. Parsing is therefore gated on
finding at least one **usable** counter (a duration, a cost, or a token tally)
rather than on the presence of a ``cost`` mapping: EU is derived from duration,
not cost, so a payload carrying duration but no cost still has runtime worth
capturing.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Stable harness id stamped on every parsed counter set. This module parses the
#: Claude Code statusline / SessionEnd payload, so the harness is always Claude
#: Code; the model id is read per-payload off ``model`` (see :func:`_model_id`).
_HARNESS_ID = "claude-code"

#: The measured fields that make a parse worth capturing. ``harness`` / ``model``
#: are attribution, not measurement, so a payload yielding only those is treated
#: as carrying no counters at all.
_COUNTER_FIELDS = frozenset(
    {
        "api_duration_ms",
        "total_duration_ms",
        "cost_usd",
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
)


class RuntimeCounters(BaseModel):
    """Typed runtime counters parsed from a Claude Code payload.

    ``harness`` and ``model`` carry the attribution that makes the captured
    counters calibratable: the agent harness id (``"claude-code"``) and the
    model id the runtime billed against. Both are optional because a payload
    without a recognisable ``model`` block omits the model id, and an
    out-of-band ``model_validate`` may construct counters with no attribution.

    ``measure_version`` records WHICH definition of the counters produced them.
    Cumulative counters are only comparable against a baseline taken under the
    same definition: when the definition changes, the difference between two
    snapshots is not work, it is the change. Inferring that from the direction the
    number moved catches only half the cases -- a falling measure looks like a
    regression, while a rising one looks exactly like a productive week and
    silently banks it. The version makes the change a fact rather than a guess.
    """

    model_config = ConfigDict(extra="forbid")

    api_duration_ms: int | None = Field(default=None, ge=0)
    total_duration_ms: int | None = Field(default=None, ge=0)
    cost_usd: Decimal | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)
    harness: str | None = None
    model: str | None = None
    measure_version: int | None = None


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


def _model_id(claude_payload: dict[str, Any]) -> str | None:
    """Return the billed model id from *claude_payload*, or ``None`` when absent.

    Accepts either ``model: "<id>"`` (string) or ``model: {"id":"..."}`` /
    ``model: {"display_name":"..."}`` (mapping) -- Claude Code has shipped both
    shapes. The ``id`` key wins over ``display_name`` so calibration keys on the
    canonical model string rather than the human label. A missing or
    wrong-typed ``model`` block yields ``None`` so the field stays nullable.
    """
    raw = claude_payload.get("model")
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        for key in ("id", "display_name", "name"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def parse_runtime_counters(claude_payload: dict[str, Any]) -> RuntimeCounters | None:
    """Parse Claude Code runtime counters, returning ``None`` without usable counters.

    Args:
        claude_payload: Decoded Claude Code statusline/hook payload.

    Returns:
        Parsed :class:`RuntimeCounters` when the payload yields at least one
        usable counter -- a duration, a cost, or a token tally -- otherwise
        ``None``. The ``harness`` attribution is always stamped
        (``"claude-code"``) and the ``model`` id is read off the payload when
        present, but attribution alone is not a counter: a payload carrying only
        a model id yields ``None`` so the caller degrades rather than capturing
        an empty snapshot. Wrong-typed fields are omitted, not raised.
    """
    cost = claude_payload.get("cost")
    cost_block = cost if isinstance(cost, dict) else {}
    current_usage = _current_usage_block(claude_payload)
    data: dict[str, int | Decimal | str] = {"harness": _HARNESS_ID}

    model_id = _model_id(claude_payload)
    if model_id is not None:
        data["model"] = model_id

    api_duration_ms = _first_int(cost_block, "api_duration_ms", "total_api_duration_ms")
    if api_duration_ms is not None:
        data["api_duration_ms"] = api_duration_ms

    total_duration_ms = _first_int(cost_block, "total_duration_ms")
    if total_duration_ms is not None:
        data["total_duration_ms"] = total_duration_ms

    cost_usd = _first_decimal(cost_block, "cost_usd", "total_cost_usd")
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

    if not _COUNTER_FIELDS & data.keys():
        return None
    return RuntimeCounters.model_validate(data)


__all__ = [
    "RuntimeCounters",
    "parse_runtime_counters",
]
