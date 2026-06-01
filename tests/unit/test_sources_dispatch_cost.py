"""Tests for the dispatch_cost telemetry source adapter (P29-I01-W02).

Covers :class:`DispatchCostSessionSource` (protocol conformance, discover,
``iter_rows`` parse / filter / skip / boundary paths), the
:class:`TelemetryDispatchCost` row model, its store-table registration, the
projector routing branch, and the W02 *spike* finding: ``dispatch_cost``
events carry no ``session_id`` and so do not map 1:1 onto
``Wave.sessions[*].session_id``.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import StoreKind, WaveStatus
from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.events.dispatch_cost import DispatchCostPayload
from eawf.kernel.store.paths import store_path
from eawf.observability.telemetry.models import TelemetryDispatchCost
from eawf.observability.telemetry.projector import RebuildMode, SourceSpec, rebuild
from eawf.observability.telemetry.sources import (
    DispatchCostSessionSource,
    SessionSource,
)
from eawf.observability.telemetry.sources.base import SessionSource as SessionSourceBase
from eawf.observability.telemetry.store.base import (
    TABLES,
    AbstractMetricsStore,
    open_store,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
_STORE_FIXTURES = _FIXTURES / "store"
_SOURCE_LOGGER = "eawf.observability.telemetry.sources.dispatch_cost"


# --------------------------------------------------------------------------- #
# Envelope builders.
# --------------------------------------------------------------------------- #


def _dispatch_cost_envelope(
    envelope_id: str,
    *,
    wave_id: str | None = "P29-I01-W02",
    attempt_id: str | None = "attempt-aaaa",
    runtime: str = "claude",
    model: str = "claude-opus-4-8",
    input_tokens: int = 1200,
    output_tokens: int = 340,
    cache_creation_input_tokens: int = 1500,
    cache_read_input_tokens: int = 17100,
    cost_usd: str = "0.4231",
    pricing_version: str = "2026-05-01",
    created_at: str = "2026-05-30T12:00:00Z",
) -> str:
    """Return one JSONL line wrapping a canonical ``DispatchCostPayload``."""
    payload = DispatchCostPayload(
        timestamp=datetime.fromisoformat(created_at.replace("Z", "+00:00")),
        wave_id=wave_id,
        attempt_id=attempt_id,
        runtime=runtime,  # type: ignore[arg-type]
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cost_usd=Decimal(cost_usd),
        pricing_version=pricing_version,
    )
    envelope = Envelope(
        id=envelope_id,
        kind=StoreKind.EVENT,
        scope_id=wave_id,
        created_at=datetime.fromisoformat(created_at.replace("Z", "+00:00")),
        summary=f"dispatch_cost wave={wave_id}",
        payload=payload.model_dump(mode="json"),
    )
    return envelope.model_dump_json()


def _non_dispatch_event_line(envelope_id: str) -> str:
    """Return a valid event envelope whose ``event_type`` is not dispatch_cost."""
    envelope = Envelope(
        id=envelope_id,
        kind=StoreKind.EVENT,
        scope_id="ABC",
        created_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        summary="some other event",
        payload={"event_type": "project init", "timestamp": "2026-05-30T12:00:00Z"},
    )
    return envelope.model_dump_json()


def _write(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "event.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Protocol conformance.
# --------------------------------------------------------------------------- #


def test_dispatch_cost_source_is_session_source() -> None:
    assert isinstance(DispatchCostSessionSource(), SessionSource)


def test_dispatch_cost_source_reexported_matches_base() -> None:
    assert SessionSource is SessionSourceBase


def test_dispatch_cost_source_name_is_stable() -> None:
    assert DispatchCostSessionSource().source_name == "dispatch_cost"


# --------------------------------------------------------------------------- #
# iter_rows — happy path + field capture.
# --------------------------------------------------------------------------- #


def test_iter_rows_projects_dispatch_cost_into_row(tmp_path: Path) -> None:
    path = _write(tmp_path, _dispatch_cost_envelope("EV-0001"))
    rows = list(DispatchCostSessionSource().iter_rows(path))
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, TelemetryDispatchCost)
    assert row.envelope_id == "EV-0001"
    assert row.wave_id == "P29-I01-W02"
    assert row.attempt_id == "attempt-aaaa"
    assert row.runtime == "claude"
    assert row.model == "claude-opus-4-8"


def test_iter_rows_captures_token_and_cost_figures(tmp_path: Path) -> None:
    path = _write(tmp_path, _dispatch_cost_envelope("EV-0001"))
    row = next(iter(DispatchCostSessionSource().iter_rows(path)))
    assert row.input_tokens == 1200
    assert row.output_tokens == 340
    assert row.cache_creation_input_tokens == 1500
    assert row.cache_read_input_tokens == 17100
    assert row.cost_usd == Decimal("0.4231")
    assert row.pricing_version == "2026-05-01"
    assert row.ts == datetime(2026, 5, 30, 12, 0, tzinfo=UTC)


def test_iter_rows_yields_multiple_attempts_distinctly(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _dispatch_cost_envelope("EV-0001", attempt_id="attempt-aaaa"),
        _dispatch_cost_envelope("EV-0002", attempt_id="attempt-bbbb"),
    )
    rows = list(DispatchCostSessionSource().iter_rows(path))
    assert [r.envelope_id for r in rows] == ["EV-0001", "EV-0002"]
    assert {r.attempt_id for r in rows} == {"attempt-aaaa", "attempt-bbbb"}


def test_iter_rows_accepts_interactive_session_with_null_keys(tmp_path: Path) -> None:
    # An interactive (non-wave) CLI session carries wave_id + attempt_id None.
    path = _write(tmp_path, _dispatch_cost_envelope("EV-0001", wave_id=None, attempt_id=None))
    row = next(iter(DispatchCostSessionSource().iter_rows(path)))
    assert row.wave_id is None
    assert row.attempt_id is None
    assert row.envelope_id == "EV-0001"


# --------------------------------------------------------------------------- #
# iter_rows — filtering + skip paths.
# --------------------------------------------------------------------------- #


def test_iter_rows_skips_non_dispatch_cost_event(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        _non_dispatch_event_line("EV-other"),
        _dispatch_cost_envelope("EV-0001"),
    )
    rows = list(DispatchCostSessionSource().iter_rows(path))
    assert [r.envelope_id for r in rows] == ["EV-0001"]


def test_iter_rows_skips_corrupt_line_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = _write(
        tmp_path,
        _dispatch_cost_envelope("EV-0001"),
        "{not valid json",
        _dispatch_cost_envelope("EV-0002"),
    )
    with caplog.at_level(logging.WARNING, logger=_SOURCE_LOGGER):
        rows = list(DispatchCostSessionSource().iter_rows(path))
    assert [r.envelope_id for r in rows] == ["EV-0001", "EV-0002"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "line=2" in warnings[0].message
    assert "skipped corrupt envelope" in warnings[0].message


def test_iter_rows_skips_dispatch_cost_typed_event_with_wrong_payload_shape(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # An event_type=dispatch_cost line whose payload is the generic event-meta
    # shape (no runtime / model / pricing_version) is malformed for this
    # adapter and skipped with a warning, not adopted with junk fields.
    bad = Envelope(
        id="EV-bad",
        kind=StoreKind.EVENT,
        scope_id="ABC",
        created_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        summary="wave W01 dispatched",
        payload={"event_type": "dispatch_cost", "actor": "daemon", "status": "ok"},
    ).model_dump_json()
    path = _write(tmp_path, bad, _dispatch_cost_envelope("EV-0001"))
    with caplog.at_level(logging.WARNING, logger=_SOURCE_LOGGER):
        rows = list(DispatchCostSessionSource().iter_rows(path))
    assert [r.envelope_id for r in rows] == ["EV-0001"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "malformed dispatch_cost" in warnings[0].message


def test_iter_rows_skips_dispatch_cost_with_unknown_runtime(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A payload carrying a runtime outside the closed RuntimeName set is
    # malformed (the row model's RuntimeName literal would reject it); the
    # adapter skips it before construction rather than raising.
    payload: dict[str, Any] = {
        "event_type": "dispatch_cost",
        "timestamp": "2026-05-30T12:00:00Z",
        "wave_id": "P29-I01-W02",
        "attempt_id": "attempt-aaaa",
        "runtime": "gemini",
        "model": "g-1",
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cost_usd": "0.1",
        "pricing_version": "v1",
    }
    bad = Envelope(
        id="EV-bad",
        kind=StoreKind.EVENT,
        scope_id="P29-I01-W02",
        created_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        summary="bad runtime",
        payload=payload,
    ).model_dump_json()
    path = _write(tmp_path, bad)
    with caplog.at_level(logging.WARNING, logger=_SOURCE_LOGGER):
        rows = list(DispatchCostSessionSource().iter_rows(path))
    assert rows == []
    assert any("malformed dispatch_cost" in r.message for r in caplog.records)


def test_iter_rows_skips_non_event_kind_envelope(tmp_path: Path) -> None:
    # An audit-kind envelope that happens to carry event_type=dispatch_cost in
    # its payload is not an event-store row; the kind guard skips it.
    audit = Envelope(
        id="AU-0001",
        kind=StoreKind.AUDIT,
        scope_id="ABC",
        created_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        summary="audit",
        payload={"event_type": "dispatch_cost"},
    ).model_dump_json()
    path = _write(tmp_path, audit)
    assert list(DispatchCostSessionSource().iter_rows(path)) == []


def test_iter_rows_coerces_missing_token_fields_to_zero(tmp_path: Path) -> None:
    # A dispatch_cost payload missing the optional token fields still projects:
    # the source defaults each absent tally to 0 rather than skipping the row.
    payload = {
        "event_type": "dispatch_cost",
        "timestamp": "2026-05-30T12:00:00Z",
        "wave_id": "P29-I01-W02",
        "attempt_id": "attempt-aaaa",
        "runtime": "codex",
        "model": "codex-1",
        "cost_usd": "0.10",
        "pricing_version": "v1",
    }
    envelope = Envelope(
        id="EV-0001",
        kind=StoreKind.EVENT,
        scope_id="P29-I01-W02",
        created_at=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
        summary="dispatch_cost",
        payload=payload,
    ).model_dump_json()
    path = _write(tmp_path, envelope)
    row = next(iter(DispatchCostSessionSource().iter_rows(path)))
    assert row.input_tokens == 0
    assert row.output_tokens == 0
    assert row.cache_creation_input_tokens == 0
    assert row.cache_read_input_tokens == 0
    assert row.cost_usd == Decimal("0.10")


# --------------------------------------------------------------------------- #
# iter_rows — boundary cases.
# --------------------------------------------------------------------------- #


def test_iter_rows_missing_path_yields_nothing() -> None:
    rows = list(DispatchCostSessionSource().iter_rows(_STORE_FIXTURES / "does-not-exist.jsonl"))
    assert rows == []


def test_iter_rows_empty_file_yields_nothing(tmp_path: Path) -> None:
    empty = tmp_path / "event.jsonl"
    empty.write_text("", encoding="utf-8")
    assert list(DispatchCostSessionSource().iter_rows(empty)) == []


def test_iter_rows_blank_lines_are_ignored(tmp_path: Path) -> None:
    line = _dispatch_cost_envelope("EV-0001")
    path = tmp_path / "event.jsonl"
    path.write_text(f"\n{line}\n\n", encoding="utf-8")
    rows = list(DispatchCostSessionSource().iter_rows(path))
    assert [r.envelope_id for r in rows] == ["EV-0001"]


def test_iter_rows_on_real_fixture_skips_legacy_dispatch_cost_meta(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The committed event.jsonl fixture has one event_type=dispatch_cost line
    # whose payload is the generic event-meta shape (no runtime/model). The
    # adapter skips it as malformed and yields no rows from that fixture.
    with caplog.at_level(logging.WARNING, logger=_SOURCE_LOGGER):
        rows = list(DispatchCostSessionSource().iter_rows(_STORE_FIXTURES / "event.jsonl"))
    assert rows == []
    messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("malformed dispatch_cost" in m for m in messages)
    assert any("skipped corrupt envelope" in m for m in messages)


# --------------------------------------------------------------------------- #
# discover.
# --------------------------------------------------------------------------- #


def test_discover_finds_event_store() -> None:
    state_path = _FIXTURES / "state.json"
    found = [p.name for p in DispatchCostSessionSource().discover(state_path)]
    assert found == ["event.jsonl"]


def test_discover_missing_store_dir_yields_nothing(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    assert list(DispatchCostSessionSource().discover(state_path)) == []


# --------------------------------------------------------------------------- #
# Row model + store-table registration (capture INTO the store).
# --------------------------------------------------------------------------- #


def test_dispatch_costs_table_registered() -> None:
    by_name = {spec.name: spec for spec in TABLES}
    assert "telemetry_dispatch_costs" in by_name
    spec = by_name["telemetry_dispatch_costs"]
    assert spec.model is TelemetryDispatchCost
    assert spec.primary_key == ("envelope_id",)


def test_dispatch_cost_row_round_trips_through_store(tmp_path: Path) -> None:
    store: AbstractMetricsStore = open_store("sqlite", tmp_path / "telemetry.db")
    store.init_schema()
    row = TelemetryDispatchCost(
        envelope_id="EV-0001",
        wave_id="P29-I01-W02",
        attempt_id="attempt-aaaa",
        runtime="claude",
        model="claude-opus-4-8",
        input_tokens=1200,
        output_tokens=340,
        cache_creation_input_tokens=1500,
        cache_read_input_tokens=17100,
        cost_usd=Decimal("0.4231"),
        pricing_version="2026-05-01",
        ts=datetime(2026, 5, 30, 12, 0, tzinfo=UTC),
    )
    store.upsert("telemetry_dispatch_costs", row)
    store.commit()
    fetched = store.fetch_all("telemetry_dispatch_costs", TelemetryDispatchCost)
    store.close()
    assert len(fetched) == 1
    got = fetched[0]
    assert isinstance(got, TelemetryDispatchCost)
    assert got == row
    # Decimal cost must round-trip exactly (stored as TEXT, no float drift).
    assert got.cost_usd == Decimal("0.4231")


def test_rebuild_captures_dispatch_cost_rows_into_store(tmp_path: Path) -> None:
    # End-to-end: the source -> projector -> store path persists EU-forward
    # rows, satisfying the "captures into the telemetry store" criterion.
    state_dir = tmp_path / ".ea"
    state_path = state_dir / "state.json"
    event_path = store_path(state_path, StoreKind.EVENT)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(
        _dispatch_cost_envelope("EV-0001", attempt_id="attempt-aaaa")
        + "\n"
        + _dispatch_cost_envelope("EV-0002", attempt_id="attempt-bbbb")
        + "\n",
        encoding="utf-8",
    )
    store: AbstractMetricsStore = open_store("sqlite", tmp_path / "telemetry.db")
    store.init_schema()
    spec = SourceSpec(
        source=DispatchCostSessionSource(),
        root=state_path,
        project_id="proj-1",
    )
    report = rebuild(store, [spec], mode=RebuildMode.FULL)
    fetched = store.fetch_all("telemetry_dispatch_costs", TelemetryDispatchCost)
    store.close()
    assert report.dispatch_costs == 2
    assert {r.envelope_id for r in fetched} == {"EV-0001", "EV-0002"}


def test_rebuild_is_idempotent_on_replay(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    event_path = store_path(state_path, StoreKind.EVENT)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.write_text(_dispatch_cost_envelope("EV-0001") + "\n", encoding="utf-8")
    store: AbstractMetricsStore = open_store("sqlite", tmp_path / "telemetry.db")
    store.init_schema()
    spec = SourceSpec(source=DispatchCostSessionSource(), root=state_path, project_id="proj-1")
    rebuild(store, [spec], mode=RebuildMode.FULL)
    rebuild(store, [spec], mode=RebuildMode.FULL)
    fetched = store.fetch_all("telemetry_dispatch_costs", TelemetryDispatchCost)
    store.close()
    # Keyed upsert: a replay produces the same single row, not a duplicate.
    assert len(fetched) == 1


# --------------------------------------------------------------------------- #
# W02 spike — dispatch_cost.session_id is NOT 1:1 with Wave.sessions[*].session_id.
# --------------------------------------------------------------------------- #


def test_spike_dispatch_cost_payload_has_no_session_id_field() -> None:
    # The decisive spike fact: the DispatchCostPayload schema carries no
    # session_id field at all. Its correlation keys are wave_id + attempt_id.
    fields = set(DispatchCostPayload.model_fields)
    assert "session_id" not in fields
    assert {"wave_id", "attempt_id"} <= fields


def test_spike_session_attempt_carries_session_id_dispatch_cost_lacks() -> None:
    # Wave.sessions values are SessionAttempt rows keyed by session_id; the
    # dispatch_cost row keys on the source envelope id because the event has
    # no session_id to join on. This pins the asymmetry the spike found.
    now = datetime(2026, 5, 30, 12, 0, tzinfo=UTC)
    wave = Wave(
        id="P29-I01-W02",
        iter_id="P29-I01",
        title="add dispatch cost session source",
        status=WaveStatus.CLAIMED,
        opened_at=now,
        sessions={
            1: SessionAttempt(
                attempt=1,
                runtime="claude",
                session_id="sess-uuid-1",
                session_log_handle="urn:eawf:v1:session-log:claude:sess-uuid-1",
                started_at=now,
            )
        },
    )
    session_ids = {att.session_id for att in wave.sessions.values()}
    assert session_ids == {"sess-uuid-1"}
    # The dispatch_cost payload offers attempt_id (a per-dispatch UUID), not a
    # session_id; there is no field on the payload that equals "sess-uuid-1".
    payload = DispatchCostPayload(
        timestamp=now,
        wave_id="P29-I01-W02",
        attempt_id="attempt-uuid-xyz",
        runtime="claude",
        model="claude-opus-4-8",
        input_tokens=1,
        output_tokens=1,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        cost_usd=Decimal("0.1"),
        pricing_version="v1",
    )
    dumped = json.loads(payload.model_dump_json())
    assert "session_id" not in dumped
    assert dumped["attempt_id"] not in session_ids
