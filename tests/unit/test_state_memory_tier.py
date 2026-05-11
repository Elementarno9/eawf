"""Unit tests for ``MemoryTier`` enum + ``MemorySummary.tier`` default."""

from __future__ import annotations

from eawf.state.enums import MemoryTier
from eawf.state.models import MemorySummary


def test_memory_tier_enum_members() -> None:
    assert {t.value for t in MemoryTier} == {"working", "archival", "retrieval"}


def test_memory_summary_default_tier_working() -> None:
    summary = MemorySummary(
        id="MEM-1",
        scope_id="QR",
        summary="hi",
        confidence="medium",
        status="active",
        store_record_id="R1",
    )
    assert summary.tier == MemoryTier.WORKING


def test_memory_summary_round_trip_archival() -> None:
    summary = MemorySummary(
        id="MEM-1",
        scope_id="QR",
        summary="hi",
        confidence="medium",
        status="stale",
        store_record_id="R1",
        tier=MemoryTier.ARCHIVAL,
    )
    dumped = summary.model_dump(mode="json")
    assert dumped["tier"] == "archival"
    rehydrated = MemorySummary.model_validate(dumped)
    assert rehydrated.tier == MemoryTier.ARCHIVAL


def test_memory_summary_backfill_when_tier_missing_in_payload() -> None:
    """Existing state.json rows without ``tier`` must deserialise as WORKING."""
    payload = {
        "id": "MEM-1",
        "scope_id": "QR",
        "summary": "hi",
        "confidence": "medium",
        "status": "active",
        "store_record_id": "R1",
    }
    summary = MemorySummary.model_validate(payload)
    assert summary.tier == MemoryTier.WORKING
