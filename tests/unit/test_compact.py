"""Tests for store.compact.compact_store."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.state.enums import StoreKind
from eawf.store import compact
from eawf.store.compact import CompactReport, compact_store
from eawf.store.envelope import Envelope


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
