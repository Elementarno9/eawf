"""Unit tests for the metering writer.

Pins the metering writer: :func:`price_spawn_result` prices a transient
:class:`~eawf.runtime.runtimes.adapter.SpawnResult` through the embedded
Decimal pricing snapshot into a typed
:class:`~eawf.runtime.runtimes.metering.MeteredCost`, and
:func:`meter_and_emit` drives the ``dispatch_cost`` emit step with the real,
token-derived cost so the ledger is no longer ``$0``.

No subprocess and no daemon are exercised — the spawn result is constructed
directly and the emit step is a recording stub, so the cost arithmetic + the
emit-with-real-cost contract are observable in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from eawf.observability.telemetry.pricing import PRICING_VERSION, lookup_pricing
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.runtime.runtimes.metering import (
    MeteredCost,
    meter_and_emit,
    price_spawn_result,
)

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_T1 = datetime(2026, 6, 1, 12, 0, 5, tzinfo=UTC)


def _spawn_result(**overrides: object) -> SpawnResult:
    """Build a SpawnResult with sane defaults; *overrides* replace fields.

    Defaults price against ``claude-opus-4-8`` with a representative token
    spread (non-cached input, output, both cache-write TTL tiers, cache
    read) so the per-class cost arithmetic has every term non-zero.
    """
    base: dict[str, object] = {
        "session_id": "sess-abc123",
        "runtime": "claude",
        "model": "claude-opus-4-8",
        "resolved_model": "claude-opus-4-8",
        "subprocess_pid": 4321,
        "exit_status": 0,
        "text": "the answer text",
        "input_tokens": 100,
        "output_tokens": 42,
        "cache_creation_input_tokens": 80,
        "cache_creation_5m_input_tokens": 50,
        "cache_creation_1h_input_tokens": 30,
        "cache_read_input_tokens": 200,
        "started_at": _T0,
        "ended_at": _T1,
    }
    base.update(overrides)
    return SpawnResult.model_validate(base)


# --------------------------------------------------------------------------- #
# Cost computed from a SpawnResult: real token x pricing.
# --------------------------------------------------------------------------- #


def test_price_spawn_result_computes_cost_from_tokens_times_pricing() -> None:
    """The cost is the exact Decimal sum of every token class x its rate."""
    result = _spawn_result()
    pricing = lookup_pricing("claude-opus-4-8")
    assert pricing is not None
    expected = (
        100 * pricing.input_per_token
        + 42 * pricing.output_per_token
        + 50 * pricing.cache_write_5m_per_token
        + 30 * pricing.cache_write_1h_per_token
        + 200 * pricing.cache_read_per_token
    )

    metered = price_spawn_result(result)

    assert metered.cost_usd == expected
    assert metered.priced is True
    assert metered.cost_usd > Decimal("0")
    assert metered.pricing_version == PRICING_VERSION


def test_price_spawn_result_cost_is_decimal_not_float() -> None:
    """The priced cost is an exact Decimal (no binary-float drift)."""
    metered = price_spawn_result(_spawn_result())
    assert isinstance(metered.cost_usd, Decimal)


def test_price_spawn_result_prices_cache_write_tiers_independently() -> None:
    """5m and 1h cache-write tokens price against their own distinct rates."""
    pricing = lookup_pricing("claude-opus-4-8")
    assert pricing is not None
    # Same total cache-write tokens, opposite TTL split -> different cost,
    # because the 1h rate (2x base input) exceeds the 5m rate (1.25x).
    mostly_5m = price_spawn_result(
        _spawn_result(
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_5m_input_tokens=100,
            cache_creation_1h_input_tokens=0,
        )
    )
    mostly_1h = price_spawn_result(
        _spawn_result(
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_5m_input_tokens=0,
            cache_creation_1h_input_tokens=100,
        )
    )
    assert mostly_1h.cost_usd > mostly_5m.cost_usd
    assert mostly_5m.cost_usd == 100 * pricing.cache_write_5m_per_token
    assert mostly_1h.cost_usd == 100 * pricing.cache_write_1h_per_token


def test_price_spawn_result_combines_cache_creation_total_for_payload() -> None:
    """The recorded cache-creation scalar is the sum of the two TTL tiers."""
    metered = price_spawn_result(
        _spawn_result(
            cache_creation_5m_input_tokens=50,
            cache_creation_1h_input_tokens=30,
        )
    )
    assert metered.cache_creation_input_tokens == 80


# --------------------------------------------------------------------------- #
# Model resolution: resolved_model preferred, alias fallback.
# --------------------------------------------------------------------------- #


def test_price_spawn_result_prices_against_resolved_model() -> None:
    """When the runtime discloses resolved_model, the cost prices against it."""
    # Requested a bare alias but the runtime billed the dated opus id.
    result = _spawn_result(model="opus", resolved_model="claude-opus-4-8")
    metered = price_spawn_result(result)
    assert metered.model == "claude-opus-4-8"
    assert metered.priced is True


def test_price_spawn_result_falls_back_to_requested_model_alias() -> None:
    """With no resolved_model, a bare alias still resolves via prefix fallback."""
    result = _spawn_result(model="opus", resolved_model=None)
    pricing = lookup_pricing("opus")
    assert pricing is not None
    metered = price_spawn_result(result)
    assert metered.model == "opus"
    assert metered.priced is True
    expected = (
        100 * pricing.input_per_token
        + 42 * pricing.output_per_token
        + 50 * pricing.cache_write_5m_per_token
        + 30 * pricing.cache_write_1h_per_token
        + 200 * pricing.cache_read_per_token
    )
    assert metered.cost_usd == expected


def test_price_spawn_result_prices_dated_variant_via_longest_prefix() -> None:
    """A dated model id with no exact row prices via the family prefix row."""
    result = _spawn_result(model="claude-opus-4-8", resolved_model="claude-opus-4-8-20260101")
    metered = price_spawn_result(result)
    # The longest-prefix resolver binds the dated id to the claude-opus-4-8 row.
    assert metered.priced is True
    assert metered.cost_usd > Decimal("0")


# --------------------------------------------------------------------------- #
# Boundary: zero-token spawn prices to a genuine $0 (priced=True).
# --------------------------------------------------------------------------- #


def test_price_spawn_result_zero_tokens_is_genuine_zero_cost() -> None:
    """A zero-token spawn prices to Decimal('0') and is flagged priced."""
    result = _spawn_result(
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        cache_read_input_tokens=0,
    )
    metered = price_spawn_result(result)
    assert metered.cost_usd == Decimal("0")
    # priced=True distinguishes a real billed zero from an unpriced fallback.
    assert metered.priced is True
    assert metered.pricing_version == PRICING_VERSION


# --------------------------------------------------------------------------- #
# Error path: unknown model -> $0 fallback, priced=False, no raise.
# --------------------------------------------------------------------------- #


def test_price_spawn_result_unknown_model_falls_back_to_unpriced_zero() -> None:
    """An unpriceable model yields cost 0 with priced=False (no raise)."""
    # A model id that matches no PRICING key nor any prefix alias.
    assert lookup_pricing("totally-unknown-model-xyz") is None
    result = _spawn_result(model="totally-unknown-model-xyz", resolved_model=None)
    metered = price_spawn_result(result)
    assert metered.cost_usd == Decimal("0")
    assert metered.priced is False
    # The snapshot tag is still stamped so the row pins a known version.
    assert metered.pricing_version == PRICING_VERSION


def test_price_spawn_result_unknown_model_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The unpriced fallback logs a WARNING so the gap is observable."""
    with caplog.at_level("WARNING", logger="eawf.runtime.runtimes.metering"):
        price_spawn_result(_spawn_result(model="no-such-model", resolved_model=None))
    assert any("pricing=unresolved" in r.message for r in caplog.records)


def test_price_spawn_result_surfaces_session_id_for_correlation() -> None:
    """The runtime session id rides on MeteredCost for trace correlation."""
    metered = price_spawn_result(_spawn_result(session_id="sess-zzz"))
    assert metered.session_id == "sess-zzz"


# --------------------------------------------------------------------------- #
# Cross-vendor pricing: a real codex / opencode spawn must price
# honestly (priced=True) rather than silently fall back to priced=False / $0.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("runtime", "model"),
    [
        ("codex", "gpt-5-codex"),
        ("codex", "gpt-5"),
        ("codex", "gpt-5-mini"),
        ("opencode", "anthropic/claude-opus-4-8"),
        ("opencode", "anthropic/claude-sonnet-4-6"),
    ],
)
def test_price_spawn_result_prices_cross_vendor_models_honestly(runtime: str, model: str) -> None:
    """A codex / opencode spawn prices priced=True against its own vendor model.

    Boundary: each runtime resolves its OWN model id to a real pricing row, so
    ``price_spawn_result`` no longer silently returns priced=False for a real
    cross-vendor spawn.
    """
    pricing = lookup_pricing(model)
    assert pricing is not None
    result = _spawn_result(runtime=runtime, model=model, resolved_model=None)
    metered = price_spawn_result(result)

    assert metered.priced is True
    assert metered.model == model
    assert metered.cost_usd > Decimal("0")
    expected = (
        100 * pricing.input_per_token
        + 42 * pricing.output_per_token
        + 50 * pricing.cache_write_5m_per_token
        + 30 * pricing.cache_write_1h_per_token
        + 200 * pricing.cache_read_per_token
    )
    assert metered.cost_usd == expected


def test_price_spawn_result_codex_dated_model_prices_via_prefix() -> None:
    """A suffixed codex id prices via its longest-prefix tier row (priced)."""
    result = _spawn_result(
        runtime="codex", model="gpt-5-codex", resolved_model="gpt-5-codex-preview"
    )
    metered = price_spawn_result(result)
    assert metered.priced is True
    assert metered.cost_usd > Decimal("0")


def test_price_spawn_result_unknown_codex_model_still_degrades_honestly() -> None:
    """Error path: an unknown OpenAI id (no tier row) degrades to priced=False.

    The honest-degrade contract holds for the cross-vendor lane too: a model id
    that matches no pricing row yields cost 0 with priced=False (no raise, no
    pretend-billed zero), distinct from a real codex spawn that now prices.
    """
    assert lookup_pricing("gpt-4o") is None
    result = _spawn_result(runtime="codex", model="gpt-4o", resolved_model=None)
    metered = price_spawn_result(result)
    assert metered.cost_usd == Decimal("0")
    assert metered.priced is False


# --------------------------------------------------------------------------- #
# meter_and_emit: the emitted dispatch_cost carries the real (non-$0) cost.
# --------------------------------------------------------------------------- #


class _RecordingEmitter:
    """A DispatchCostEmitter stub recording the kwargs it was called with."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return f"EV-stub-{len(self.calls)}"


def test_meter_and_emit_emits_real_nonzero_cost() -> None:
    """The emitted dispatch_cost carries the token-derived cost, not $0."""
    emitter = _RecordingEmitter()
    result = _spawn_result()
    expected = price_spawn_result(result).cost_usd
    assert expected > Decimal("0")

    metered = meter_and_emit(
        result,
        wave_id="P29-I01-W22",
        attempt_id="attempt-uuid-xyz",
        emit=emitter,
    )

    assert len(emitter.calls) == 1
    call = emitter.calls[0]
    assert call["cost_usd"] == expected
    assert call["cost_usd"] > Decimal("0")
    assert metered.cost_usd == expected


def test_meter_and_emit_passes_wave_and_attempt_correlation() -> None:
    """The emit keys on wave_id + attempt_id (the W02-ratified correlation)."""
    emitter = _RecordingEmitter()
    meter_and_emit(
        _spawn_result(),
        wave_id="P29-I01-W22",
        attempt_id="attempt-uuid-xyz",
        emit=emitter,
    )
    call = emitter.calls[0]
    assert call["wave_id"] == "P29-I01-W22"
    assert call["attempt_id"] == "attempt-uuid-xyz"


def test_meter_and_emit_does_not_leak_session_id_into_emit_keys() -> None:
    """session_id is NOT among the emitted payload kwargs (W02 spike)."""
    emitter = _RecordingEmitter()
    meter_and_emit(
        _spawn_result(session_id="sess-should-not-appear"),
        wave_id="P29-I01-W22",
        attempt_id="attempt-uuid-xyz",
        emit=emitter,
    )
    call = emitter.calls[0]
    assert "session_id" not in call
    assert "sess-should-not-appear" not in call.values()


def test_meter_and_emit_forwards_runtime_model_and_tokens() -> None:
    """The emit receives the runtime, priced model, and per-class token tallies."""
    emitter = _RecordingEmitter()
    meter_and_emit(
        _spawn_result(model="opus", resolved_model="claude-opus-4-8"),
        wave_id="P29-I01-W22",
        attempt_id="att-1",
        emit=emitter,
    )
    call = emitter.calls[0]
    assert call["runtime"] == "claude"
    assert call["model"] == "claude-opus-4-8"
    assert call["input_tokens"] == 100
    assert call["output_tokens"] == 42
    assert call["cache_creation_input_tokens"] == 80
    assert call["cache_read_input_tokens"] == 200
    assert call["pricing_version"] == PRICING_VERSION


def test_meter_and_emit_interactive_session_allows_none_correlation() -> None:
    """An interactive (non-wave) session emits with wave_id/attempt_id None."""
    emitter = _RecordingEmitter()
    metered = meter_and_emit(
        _spawn_result(),
        wave_id=None,
        attempt_id=None,
        emit=emitter,
    )
    call = emitter.calls[0]
    assert call["wave_id"] is None
    assert call["attempt_id"] is None
    assert metered.cost_usd > Decimal("0")


def test_metered_cost_is_frozen() -> None:
    """MeteredCost is immutable — a metered cost is a settled fact."""
    metered = price_spawn_result(_spawn_result())
    with pytest.raises(ValidationError):
        metered.cost_usd = Decimal("1")  # type: ignore[misc]


def test_metered_cost_rejects_negative_cost() -> None:
    """The cost field enforces the ge=0 floor at the model boundary."""
    with pytest.raises(ValidationError):
        MeteredCost(
            session_id="s",
            model="claude-opus-4-8",
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            cost_usd=Decimal("-1"),
            pricing_version=PRICING_VERSION,
            priced=True,
        )
