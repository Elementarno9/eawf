"""Five token classes, the total identity, and the price source on every Run.

The usage row a dispatch records is the ``dispatch_cost`` event payload and
the :class:`TelemetryDispatchCost` row projected from it. Both must refuse a
row whose classes do not sum to its total and a cost that names no price
source; the chain from a live spawn to ``eawf metrics`` must carry the
reasoning class and the source end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.store.kinds.events import DispatchCostPayload
from eawf.observability.telemetry.exporter import build_snapshot
from eawf.observability.telemetry.models import (
    PriceSourceKind,
    TelemetryDispatchCost,
    TokenClass,
    check_price_source,
    check_token_identity,
)
from eawf.observability.telemetry.pricing import (
    PRICING_VERSION,
    UNPRICED_MODELS,
    lookup_pricing,
    resolve_price_source,
)
from eawf.observability.telemetry.sources.dispatch_cost import DispatchCostSessionSource
from eawf.observability.telemetry.store import SqliteMetricsStore
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.dispatch_runner import DispatchTokens, emit_dispatch_cost
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.runtime.runtimes.metering import price_spawn_result

_TS = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _classes(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_read_tokens": 1000,
        "cache_write_tokens": 200,
        "reasoning_tokens": 10,
        "total_tokens": 1340,
    }
    base.update(overrides)
    return base


def _row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "envelope_id": "env-1",
        "runtime": "codex",
        "model": "gpt-5",
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 1000,
        "reasoning_tokens": 10,
        "total_tokens": 1340,
        "cost_usd": Decimal("0.01"),
        "price_source": PriceSourceKind.LIST_RECONSTRUCTED,
        "rate_table_version": PRICING_VERSION,
        "pricing_version": PRICING_VERSION,
    }
    base.update(overrides)
    return base


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_type": "dispatch_cost",
        "timestamp": _TS,
        "wave_id": "P01-I01-W01",
        "attempt_id": "a-1",
        "runtime": "codex",
        "model": "gpt-5",
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_creation_input_tokens": 200,
        "cache_read_input_tokens": 1000,
        "cost_usd": Decimal("0.01"),
        "pricing_version": PRICING_VERSION,
    }
    base.update(overrides)
    return base


def _spawn(**overrides: Any) -> SpawnResult:
    base: dict[str, Any] = {
        "session_id": "sess-1",
        "runtime": "codex",
        "model": "gpt-5",
        "subprocess_pid": 4242,
        "exit_status": 0,
        "text": "done",
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
        "cache_read_input_tokens": 1000,
        "reasoning_output_tokens": 10,
        "started_at": _TS,
        "ended_at": _TS,
    }
    base.update(overrides)
    return SpawnResult(**base)


def _ctx(tmp_path: Path) -> tuple[MethodContext, Path]:
    event_path = tmp_path / "store" / "event.jsonl"
    ctx = MethodContext(
        started_at="2026-09-01T00:00:00+00:00",
        pid=4321,
        protocol_version="1",
        version="0.7.0",
        bus=EventBus(),
        event_path=event_path,
    )
    return ctx, event_path


# -- check_token_identity ---------------------------------------------------


def test_check_token_identity_exact_sum_passes() -> None:
    check_token_identity(**_classes())


def test_check_token_identity_all_zero_passes() -> None:
    check_token_identity(**_classes(**dict.fromkeys(_classes(), 0)))


def test_check_token_identity_single_class_passes() -> None:
    check_token_identity(
        **_classes(
            input_tokens=0,
            output_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=None,
            total_tokens=1,
        )
    )


@pytest.mark.parametrize("total", [1339, 1341])
def test_check_token_identity_off_by_one_total_raises(total: int) -> None:
    with pytest.raises(ValueError, match="reasoning is never a summand"):
        check_token_identity(**_classes(total_tokens=total))


def test_check_token_identity_reasoning_added_as_summand_raises() -> None:
    with pytest.raises(ValueError, match="total_tokens 1350"):
        check_token_identity(**_classes(total_tokens=1350))


def test_check_token_identity_reasoning_equal_to_output_passes() -> None:
    check_token_identity(**_classes(reasoning_tokens=40))


def test_check_token_identity_reasoning_above_output_raises() -> None:
    with pytest.raises(ValueError, match="reasoning is a subset of output"):
        check_token_identity(**_classes(reasoning_tokens=41))


def test_check_token_identity_unknown_reasoning_passes() -> None:
    check_token_identity(**_classes(reasoning_tokens=None))


# -- check_price_source -----------------------------------------------------


def test_check_price_source_missing_source_raises() -> None:
    with pytest.raises(ValueError, match="carries no price_source"):
        check_price_source(cost_usd=Decimal("1"), price_source=None, rate_table_version=None)


def test_check_price_source_reconstructed_without_version_raises() -> None:
    with pytest.raises(ValueError, match="rate_table_version"):
        check_price_source(
            cost_usd=Decimal("1"),
            price_source=PriceSourceKind.LIST_RECONSTRUCTED,
            rate_table_version=None,
        )


def test_check_price_source_unpriced_nonzero_cost_raises() -> None:
    with pytest.raises(ValueError, match="unpriced row cannot carry"):
        check_price_source(
            cost_usd=Decimal("0.01"),
            price_source=PriceSourceKind.UNPRICED,
            rate_table_version=None,
        )


def test_check_price_source_unpriced_zero_and_billed_pass() -> None:
    check_price_source(
        cost_usd=Decimal("0"), price_source=PriceSourceKind.UNPRICED, rate_table_version=None
    )
    check_price_source(
        cost_usd=Decimal("3"), price_source=PriceSourceKind.BILLED, rate_table_version=None
    )


def test_price_source_kind_is_closed_three_value_set() -> None:
    assert {k.value for k in PriceSourceKind} == {"billed", "list-reconstructed", "unpriced"}
    assert {k.value for k in TokenClass} == {
        "input",
        "output",
        "cache_read",
        "cache_write",
        "reasoning",
    }


# -- TelemetryDispatchCost (the projected Run usage row) --------------------


def test_telemetry_dispatch_cost_valid_row_validates() -> None:
    row = TelemetryDispatchCost(**_row())
    assert row.total_tokens == 1340
    assert row.price_source is PriceSourceKind.LIST_RECONSTRUCTED


def test_telemetry_dispatch_cost_classes_not_summing_raises() -> None:
    with pytest.raises(ValidationError, match="reasoning is never a summand"):
        TelemetryDispatchCost(**_row(total_tokens=1350))


def test_telemetry_dispatch_cost_without_price_source_raises() -> None:
    fields = _row()
    del fields["price_source"]
    with pytest.raises(ValidationError, match="price_source"):
        TelemetryDispatchCost(**fields)


def test_telemetry_dispatch_cost_none_price_source_raises() -> None:
    with pytest.raises(ValidationError):
        TelemetryDispatchCost(**_row(price_source=None))


def test_telemetry_dispatch_cost_unknown_price_source_raises() -> None:
    with pytest.raises(ValidationError):
        TelemetryDispatchCost(**_row(price_source="guessed"))


def test_telemetry_dispatch_cost_wrong_type_total_raises() -> None:
    with pytest.raises(ValidationError):
        TelemetryDispatchCost(**_row(total_tokens="many"))


# -- DispatchCostPayload (the persisted Run usage event) --------------------


def test_dispatch_cost_payload_legacy_row_still_validates() -> None:
    payload = DispatchCostPayload.model_validate(_payload())
    assert payload.total_tokens is None
    assert payload.price_source is None


def test_dispatch_cost_payload_split_row_validates() -> None:
    payload = DispatchCostPayload.model_validate(
        _payload(
            reasoning_tokens=10,
            total_tokens=1340,
            price_source="list-reconstructed",
            rate_table_version=PRICING_VERSION,
        )
    )
    assert payload.price_source is PriceSourceKind.LIST_RECONSTRUCTED


def test_dispatch_cost_payload_classes_not_summing_raises() -> None:
    with pytest.raises(ValidationError, match="reasoning is never a summand"):
        DispatchCostPayload.model_validate(
            _payload(
                total_tokens=1350,
                price_source="list-reconstructed",
                rate_table_version=PRICING_VERSION,
            )
        )


def test_dispatch_cost_payload_total_without_price_source_raises() -> None:
    with pytest.raises(ValidationError, match="written together"):
        DispatchCostPayload.model_validate(_payload(total_tokens=1340))


# -- pricing ----------------------------------------------------------------


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5", "claude-opus-5-5-2026"])
def test_lookup_pricing_unpriced_generation_resolves_none(model: str) -> None:
    assert lookup_pricing(model) is None


def test_lookup_pricing_priced_generation_still_resolves() -> None:
    row = lookup_pricing("claude-opus-4-7-20260514")
    assert row is not None
    assert row.input_per_token == Decimal("5e-6")
    assert lookup_pricing("claude-opus") is not None


def test_unpriced_models_have_no_pricing_row() -> None:
    from eawf.observability.telemetry.pricing import PRICING

    assert not UNPRICED_MODELS & set(PRICING)


def test_resolve_price_source_priced_and_unpriced() -> None:
    assert resolve_price_source("claude-opus-4-7") == (
        PriceSourceKind.LIST_RECONSTRUCTED,
        PRICING_VERSION,
    )
    assert resolve_price_source("claude-opus-5-5") == (PriceSourceKind.UNPRICED, None)
    assert resolve_price_source("") == (PriceSourceKind.UNPRICED, None)


# -- producer: metering -----------------------------------------------------


def test_price_spawn_result_carries_reasoning_and_source() -> None:
    metered = price_spawn_result(_spawn())
    assert metered.reasoning_tokens == 10
    assert metered.price_source is PriceSourceKind.LIST_RECONSTRUCTED
    assert metered.rate_table_version == PRICING_VERSION


def test_price_spawn_result_unknown_model_is_unpriced() -> None:
    metered = price_spawn_result(_spawn(runtime="claude-code", model="claude-opus-5-5"))
    assert metered.price_source is PriceSourceKind.UNPRICED
    assert metered.cost_usd == Decimal("0")
    assert metered.rate_table_version is None


def test_price_spawn_result_no_reasoning_counter_stays_unknown() -> None:
    assert price_spawn_result(_spawn(reasoning_output_tokens=None)).reasoning_tokens is None


def test_price_spawn_result_reasoning_above_output_dropped() -> None:
    metered = price_spawn_result(_spawn(reasoning_output_tokens=41))
    assert metered.reasoning_tokens is None


# -- producer chain: spawn -> dispatch_cost event -> telemetry row -> metrics


def _emit_from_spawn(ctx: MethodContext, spawn: SpawnResult) -> None:
    metered = price_spawn_result(spawn)
    emit_dispatch_cost(
        ctx,
        wave_id="P01-I01-W01",
        attempt_id="a-1",
        runtime="codex",
        model=metered.model,
        tokens=DispatchTokens(
            input_tokens=metered.input_tokens,
            output_tokens=metered.output_tokens,
            cache_creation_input_tokens=metered.cache_creation_input_tokens,
            cache_read_input_tokens=metered.cache_read_input_tokens,
            reasoning_tokens=metered.reasoning_tokens,
        ),
        cost_usd=metered.cost_usd,
        pricing_version=metered.pricing_version,
        price_source=metered.price_source,
    )


def test_emit_dispatch_cost_records_five_classes_and_source(tmp_path: Path) -> None:
    ctx, event_path = _ctx(tmp_path)
    _emit_from_spawn(ctx, _spawn())
    rows = list(DispatchCostSessionSource().iter_rows(event_path))
    assert len(rows) == 1
    row = rows[0]
    assert (row.input_tokens, row.output_tokens, row.reasoning_tokens) == (100, 40, 10)
    assert row.total_tokens == 1140
    assert row.price_source is PriceSourceKind.LIST_RECONSTRUCTED
    assert row.rate_table_version == PRICING_VERSION


def test_emit_dispatch_cost_unpriced_model_without_source_resolves(tmp_path: Path) -> None:
    ctx, event_path = _ctx(tmp_path)
    emit_dispatch_cost(
        ctx,
        wave_id=None,
        attempt_id=None,
        runtime="claude",
        model="claude-sonnet-5",
        tokens=DispatchTokens(1, 1, 0, 0),
        cost_usd=Decimal("0"),
        pricing_version=PRICING_VERSION,
    )
    (row,) = DispatchCostSessionSource().iter_rows(event_path)
    assert row.price_source is PriceSourceKind.UNPRICED


def test_emit_dispatch_cost_nonzero_cost_for_unpriced_model_raises(tmp_path: Path) -> None:
    ctx, _ = _ctx(tmp_path)
    with pytest.raises(ValidationError, match="unpriced row cannot carry"):
        emit_dispatch_cost(
            ctx,
            wave_id=None,
            attempt_id=None,
            runtime="claude",
            model="claude-opus-5-5",
            tokens=DispatchTokens(1, 1, 0, 0),
            cost_usd=Decimal("0.5"),
            pricing_version=PRICING_VERSION,
        )


def test_dispatch_cost_source_legacy_line_derives_source(tmp_path: Path) -> None:
    ctx, event_path = _ctx(tmp_path)
    emit_dispatch_cost(
        ctx,
        wave_id=None,
        attempt_id=None,
        runtime="claude",
        model="claude-opus-4-7",
        tokens=DispatchTokens(10, 5, 0, 0),
        cost_usd=Decimal("0.2"),
        pricing_version=PRICING_VERSION,
    )
    line = event_path.read_text(encoding="utf-8")
    for key in ('"total_tokens":15,', '"price_source":"list-reconstructed",'):
        assert key in line
        line = line.replace(key, "")
    line = line.replace('"rate_table_version":"2026.05.17",', "").replace(
        '"reasoning_tokens":null,', ""
    )
    event_path.write_text(line, encoding="utf-8")
    (row,) = DispatchCostSessionSource().iter_rows(event_path)
    assert row.total_tokens == 15
    assert row.price_source is PriceSourceKind.LIST_RECONSTRUCTED
    assert row.rate_table_version == PRICING_VERSION


def test_build_snapshot_emits_token_class_and_price_source_families(tmp_path: Path) -> None:
    ctx, event_path = _ctx(tmp_path)
    _emit_from_spawn(ctx, _spawn())
    _emit_from_spawn(ctx, _spawn(model="claude-opus-5-5", reasoning_output_tokens=None))
    store = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    for row in DispatchCostSessionSource().iter_rows(event_path):
        store.upsert("telemetry_dispatch_costs", row)
    store.commit()
    families = {f.name: f for f in build_snapshot(store, scope="repo/x").families}
    tokens = {
        dict(s.labels)["token_class"]: s.value for s in families["eawf_run_tokens_total"].samples
    }
    assert tokens == {
        "input": Decimal(200),
        "output": Decimal(80),
        "cache_read": Decimal(2000),
        "cache_write": Decimal(0),
        "reasoning": Decimal(10),
    }
    sources = {dict(s.labels)["price_source"] for s in families["eawf_run_cost_usd_total"].samples}
    assert sources == {"list-reconstructed", "unpriced"}
    store.close()


def test_build_snapshot_no_runs_emits_empty_families(tmp_path: Path) -> None:
    store = SqliteMetricsStore(tmp_path / "telemetry.db")
    store.init_schema()
    families = {f.name: f for f in build_snapshot(store, scope="repo/x").families}
    assert families["eawf_run_tokens_total"].samples == ()
    assert families["eawf_run_cost_usd_total"].samples == ()
    store.close()


def test_init_schema_recreates_table_missing_new_columns(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "telemetry.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE telemetry_dispatch_costs (envelope_id TEXT PRIMARY KEY, runtime TEXT)"
    )
    conn.execute("INSERT INTO telemetry_dispatch_costs VALUES ('old', 'claude')")
    conn.execute(
        "CREATE TABLE telemetry_file_meta (jsonl_path TEXT PRIMARY KEY, mtime REAL, "
        "size INTEGER, last_offset INTEGER, last_scan_ts TEXT)"
    )
    conn.execute("INSERT INTO telemetry_file_meta VALUES ('p', 1.0, 10, 10, '2026-01-01')")
    conn.commit()
    conn.close()
    store = SqliteMetricsStore(db)
    store.init_schema()
    assert store.fetch_all("telemetry_dispatch_costs", TelemetryDispatchCost) == []
    assert store._query("SELECT COUNT(*) FROM telemetry_file_meta") == [(0,)]
    store.init_schema()
    store.close()
