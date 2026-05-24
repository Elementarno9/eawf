"""Tests for store.compact.compact_store."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store import compact
from eawf.kernel.store.compact import CompactReport, compact_store
from eawf.kernel.store.envelope import Envelope


def _write_env(path: Path, env: Envelope) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(env.model_dump_json())
        fh.write("\n")


def _make_env(env_id: str, summary: str = "test") -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.MEMORY,
        scope_id=None,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        summary=summary,
        payload={"body": summary, "confidence": "high", "review_due": None},
    )


def test_compact_dedupes_by_id(tmp_path: Path) -> None:
    store = tmp_path / "memory.jsonl"
    a_first = _make_env("A", summary="first write of A")
    b = _make_env("B", summary="only B")
    a_updated = _make_env("A", summary="second write of A")

    _write_env(store, a_first)
    _write_env(store, b)
    _write_env(store, a_updated)

    report = compact_store(store)

    lines = [ln for ln in store.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert report.records_in == 3
    assert report.records_out == 2
    assert report.dedup_count == 1

    # surviving A should be the later write
    ids_and_summaries = {json.loads(ln)["id"]: json.loads(ln)["summary"] for ln in lines}
    assert ids_and_summaries["A"] == "second write of A"


def test_compact_idempotent(tmp_path: Path) -> None:
    store = tmp_path / "memory.jsonl"
    a = _make_env("A", summary="record A")
    b = _make_env("B", summary="record B")
    a2 = _make_env("A", summary="record A updated")

    _write_env(store, a)
    _write_env(store, b)
    _write_env(store, a2)

    compact_store(store)
    after_first = store.read_text()

    compact_store(store)
    assert store.read_text() == after_first


def test_compact_preserves_order_by_first_seen(tmp_path: Path) -> None:
    store = tmp_path / "memory.jsonl"
    b = _make_env("B", summary="first B")
    a = _make_env("A", summary="only A")
    b_updated = _make_env("B", summary="updated B")

    _write_env(store, b)
    _write_env(store, a)
    _write_env(store, b_updated)

    compact_store(store)

    lines = [ln for ln in store.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    ids_in_order = [json.loads(ln)["id"] for ln in lines]
    # B was first-seen before A, so output order must be B, A
    assert ids_in_order == ["B", "A"]
    # B record should carry the updated summary
    assert json.loads(lines[0])["summary"] == "updated B"


def test_compact_empty_file_returns_zero_report(tmp_path: Path) -> None:
    store = tmp_path / "empty.jsonl"
    store.write_text("")
    report = compact_store(store)
    assert report == CompactReport(records_in=0, records_out=0, dedup_count=0)


def test_compact_missing_file_returns_zero_report(tmp_path: Path) -> None:
    store = tmp_path / "missing.jsonl"
    report = compact_store(store)
    assert report == CompactReport(records_in=0, records_out=0, dedup_count=0)


def test_compact_single_record_no_change(tmp_path: Path) -> None:
    store = tmp_path / "single.jsonl"
    env = _make_env("X", summary="singleton")
    _write_env(store, env)
    original = store.read_text()

    compact_store(store)

    assert store.read_text() == original


def test_compact_rejects_kind_drift(tmp_path: Path) -> None:
    path = tmp_path / "store.jsonl"
    rec1 = {
        "schema_version": "1.0",
        "id": "REC-1",
        "kind": "research",
        "scope_id": "QR",
        "created_at": "2026-05-08T00:00:00Z",
        "summary": "first",
        "payload": {"topic": "lit-review", "findings": ["tbd"], "sources": []},
        "blob_refs": [],
        "artifact_ids": [],
    }
    rec2 = dict(rec1)
    rec2["kind"] = "memory"
    rec2["payload"] = {"body": "x", "confidence": "high", "review_due": None}
    path.write_text(json.dumps(rec1) + "\n" + json.dumps(rec2) + "\n")
    with pytest.raises(ValueError, match="kind drift"):
        compact.compact_store(path)


def test_compact_rejects_invalid_payload(tmp_path: Path) -> None:
    path = tmp_path / "store.jsonl"
    rec = {
        "schema_version": "1.0",
        "id": "REC-1",
        "kind": "research",
        "scope_id": "QR",
        "created_at": "2026-05-08T00:00:00Z",
        "summary": "bad payload",
        "payload": {"not": "a real research payload"},
        "blob_refs": [],
        "artifact_ids": [],
    }
    path.write_text(json.dumps(rec) + "\n")
    with pytest.raises(ValidationError):
        compact.compact_store(path)


# ---------------------------------------------------------------------------
# C09 typed event-payload compatibility (P27-I02-W06)
#
# compact_store re-validates each EVENT row's inner payload. Before this
# wave it only knew the flat EventPayload, so a typed C09 row (disjoint
# field set) raised ValidationError the moment Cluster D wired the
# emitters. These cover the round-trip, the no-regression boundary, and
# the still-rejected-garbage error path.
# ---------------------------------------------------------------------------


def _runtime_switched_payload() -> dict[str, object]:
    return {
        "event_type": "runtime_switched",
        "timestamp": "2026-05-22T00:00:00+00:00",
        "wave_id": "W06",
        "attempt_id_from": "att-1",
        "attempt_id_to": "att-2",
        "runtime_from": "codex",
        "runtime_to": "claude",
        "cause": "RUNTIME_RATE_LIMIT",
        "error_detail": "<scrubbed>",
        "idempotency_key": "idem-1",
    }


def _dispatch_cost_payload() -> dict[str, object]:
    return {
        "event_type": "dispatch_cost",
        "timestamp": "2026-05-22T00:00:00+00:00",
        "wave_id": "W06",
        "attempt_id": "att-2",
        "runtime": "claude",
        "model": "claude-opus-4-7",
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_creation_input_tokens": 8000,
        "cache_read_input_tokens": 64000,
        "cost_usd": "0.123456",
        "pricing_version": "2026.05.17",
    }


def _flat_event_payload() -> dict[str, object]:
    return {
        "timestamp": "2026-05-22T00:00:00+00:00",
        "event_type": "state_mutated",
        "actor": "cli",
        "command": "eawf wave claim",
        "args_hash": "abc123",
        "status": "ok",
        "message": "claimed W06",
    }


def _event_env(env_id: str, payload: dict[str, object]) -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id="W06",
        created_at=datetime(2026, 5, 22, tzinfo=UTC),
        updated_at=None,
        summary=f"event {env_id}",
        payload=payload,
    )


def test_compact_round_trips_c09_dispatch_rows(tmp_path: Path) -> None:
    store = tmp_path / "event.jsonl"
    _write_env(store, _event_env("EV-switched", _runtime_switched_payload()))
    _write_env(store, _event_env("EV-cost", _dispatch_cost_payload()))
    _write_env(store, _event_env("EV-flat", _flat_event_payload()))

    report = compact_store(store)

    lines = [ln for ln in store.read_text().splitlines() if ln.strip()]
    assert report.records_in == 3
    assert report.records_out == 3
    assert report.dedup_count == 0

    by_id = {json.loads(ln)["id"]: json.loads(ln)["payload"] for ln in lines}
    # Typed C09 rows survive intact (disjoint fields preserved).
    assert by_id["EV-switched"]["event_type"] == "runtime_switched"
    assert by_id["EV-switched"]["runtime_to"] == "claude"
    assert by_id["EV-cost"]["event_type"] == "dispatch_cost"
    assert by_id["EV-cost"]["cost_usd"] == "0.123456"
    assert by_id["EV-cost"]["input_tokens"] == 1200
    # The normal flat row is untouched too.
    assert by_id["EV-flat"]["event_type"] == "state_mutated"
    assert by_id["EV-flat"]["actor"] == "cli"


def test_compact_dedupes_c09_rows_keeping_last(tmp_path: Path) -> None:
    store = tmp_path / "event.jsonl"
    first = _dispatch_cost_payload()
    second = dict(first)
    second["cost_usd"] = "9.999999"
    _write_env(store, _event_env("EV-cost", first))
    _write_env(store, _event_env("EV-cost", second))

    report = compact_store(store)

    lines = [ln for ln in store.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    assert report.dedup_count == 1
    # Last write wins, and the typed payload still round-trips.
    assert json.loads(lines[0])["payload"]["cost_usd"] == "9.999999"


def test_compact_only_flat_event_rows_no_regression(tmp_path: Path) -> None:
    store = tmp_path / "event.jsonl"
    _write_env(store, _event_env("EV-a", _flat_event_payload()))
    _write_env(store, _event_env("EV-b", _flat_event_payload()))
    original = store.read_text()

    report = compact_store(store)

    assert report.records_in == 2
    assert report.records_out == 2
    # No dedup, no rewrite drift on a pure-flat log.
    assert store.read_text() == original


def test_compact_rejects_malformed_c09_payload(tmp_path: Path) -> None:
    store = tmp_path / "event.jsonl"
    # A dispatch_cost tag worn over a body missing its required fields:
    # validation must still fire, not be a no-op for real garbage.
    bad: dict[str, object] = {
        "event_type": "dispatch_cost",
        "timestamp": "2026-05-22T00:00:00+00:00",
    }
    _write_env(store, _event_env("EV-bad", bad))
    with pytest.raises(ValidationError):
        compact_store(store)


def test_compact_rejects_c09_tag_over_wrong_shape(tmp_path: Path) -> None:
    store = tmp_path / "event.jsonl"
    # A dispatch_cost-shaped body wearing the runtime_switched tag: the
    # discriminator dispatch must reject the tag/body mismatch.
    mismatched = dict(_dispatch_cost_payload())
    mismatched["event_type"] = "runtime_switched"
    _write_env(store, _event_env("EV-mismatch", mismatched))
    with pytest.raises(ValidationError):
        compact_store(store)


def test_compact_rejects_unknown_event_type(tmp_path: Path) -> None:
    store = tmp_path / "event.jsonl"
    # An unknown event_type is not a C09 tag, so it routes to the flat
    # EventPayload arm, which rejects it for missing required fields.
    rogue: dict[str, object] = {
        "event_type": "not_a_real_event_kind",
        "timestamp": "2026-05-22T00:00:00+00:00",
    }
    _write_env(store, _event_env("EV-rogue", rogue))
    with pytest.raises(ValidationError):
        compact_store(store)
