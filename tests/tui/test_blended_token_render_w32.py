"""P30-I21-W32 (G2): blended effective-token headline + per-class breakdown.

The cost tab surfaced per-attempt tokens but no blended view, so an operator
could not read the effective cache-adjusted token spend or the in/out/cache
split at a glance. This wave appends a cache-adjusted ``eff`` headline (weighted
by the SAME metering pricing ratios the $ figure uses -- no forked ratio table,
no raw-sum headline) plus the raw per-class breakdown.
"""

from __future__ import annotations

from decimal import Decimal

from eawf.observability.telemetry.join import WaveAttemptRollup, WaveSessionRollup
from eawf.surfaces.tui.screens.overlays.detail_cost import (
    cost_tab_rows,
    effective_tokens,
)


def _attempt(**overrides: object) -> WaveAttemptRollup:
    base: dict[str, object] = {
        "attempt": 1,
        "runtime": "claude-code",
        "session_id": "sess-1",
        "telemetry_session_id": "sess-1",
        "duration_ms": 540000,
        "attention_eu": 0.30,
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_tokens": 80,
        "cache_write_tokens": 20,
        "cost_usd": Decimal("0.0123"),
    }
    base.update(overrides)
    return WaveAttemptRollup(**base)  # type: ignore[arg-type]


def _rollup(*attempts: WaveAttemptRollup) -> WaveSessionRollup:
    return WaveSessionRollup(
        wave_id="P01-I01-W01",
        attempts=list(attempts),
        cost_usd=sum((a.cost_usd for a in attempts), Decimal("0")),
        input_tokens=sum(a.input_tokens for a in attempts),
        output_tokens=sum(a.output_tokens for a in attempts),
        cache_read_tokens=sum(a.cache_read_tokens for a in attempts),
        cache_write_tokens=sum(a.cache_write_tokens for a in attempts),
    )


def test_effective_tokens_weights_by_metering_ratios() -> None:
    """eff weights each class by the canonical pricing ratios (opus-4-8).

    Ratios: output ~5x input, cache-write ~1.25x, cache-read ~0.1x. For
    in=100, out=50, cache-write=20, cache-read=80:
    eff = 100 + 50*5 + 20*1.25 + 80*0.1 = 383.
    """
    assert effective_tokens(_rollup(_attempt())) == 383


def test_effective_tokens_cache_read_contributes_little() -> None:
    """A large cache-read tally barely moves eff (weighted ~0.1x)."""
    lean = effective_tokens(_rollup(_attempt(cache_read_tokens=0)))
    fat = effective_tokens(_rollup(_attempt(cache_read_tokens=1000)))
    assert lean is not None and fat is not None
    # 1000 cache-read tokens add only ~100 input-equivalents, not 1000.
    assert fat - lean == 100


def test_cost_tab_rows_carry_eff_headline_and_breakdown() -> None:
    """The cost tab appends an eff headline plus the per-class breakdown."""
    rows = dict(cost_tab_rows(_rollup(_attempt())))
    assert rows["eff"] == "383"
    breakdown = rows["tokens"]
    assert "in 100" in breakdown
    assert "out 50" in breakdown
    assert "cache cr 20" in breakdown
    assert "cache rd 80" in breakdown


def test_cost_tab_rows_empty_rollup_has_no_eff_rows() -> None:
    """An empty rollup surfaces honest absence, not a fabricated eff figure."""
    rows = dict(cost_tab_rows(_rollup()))
    assert "eff" not in rows
    assert "tokens" not in rows
