"""Unit tests for ``memory.gc`` — tiered soft-archival policy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import Confidence, MemoryStatus, MemoryTier
from eawf.kernel.state.models import State
from eawf.platform.memory.gc import GcError, GcReport, gc_memory
from eawf.platform.memory.store import add_memory


def _make_state() -> State:
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    return State.model_validate(payload)


def _add_at(
    state: State,
    memory_path: Path,
    *,
    title: str,
    body: str,
    when: datetime,
    status: MemoryStatus | None = None,
    tier: MemoryTier | None = None,
) -> str:
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title=title,
        body=body,
        confidence=Confidence.MEDIUM,
        now=when,
    )
    if status is not None or tier is not None:
        assert state.memory_index is not None
        summary = state.memory_index[rec.summary.id]
        update: dict[str, object] = {}
        if status is not None:
            update["status"] = status
        if tier is not None:
            update["tier"] = tier
        state.memory_index[rec.summary.id] = summary.model_copy(update=update)
    return rec.summary.id


def test_gc_memory_negative_threshold_raises(tmp_path: Path) -> None:
    state = _make_state()
    with pytest.raises(GcError, match="threshold_days must be >= 0"):
        gc_memory(state=state, memory_path=tmp_path / "memory.jsonl", threshold_days=-1)


def test_gc_memory_dry_run_reports_without_mutation(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    older = datetime(2025, 1, 1, tzinfo=UTC)
    mid = _add_at(
        state,
        memory_path,
        title="t1",
        body="b1",
        when=older,
        status=MemoryStatus.STALE,
    )
    now = older + timedelta(days=60)
    report = gc_memory(
        state=state,
        memory_path=memory_path,
        threshold_days=30,
        now=now,
        dry_run=True,
    )
    assert isinstance(report, GcReport)
    assert report.dry_run is True
    assert report.archived_ids == [mid]
    # State unchanged.
    assert state.memory_index is not None
    assert state.memory_index[mid].tier == MemoryTier.WORKING


def test_gc_memory_flips_stale_working_to_archival(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    older = datetime(2025, 1, 1, tzinfo=UTC)
    mid = _add_at(
        state,
        memory_path,
        title="t1",
        body="b1",
        when=older,
        status=MemoryStatus.STALE,
    )
    now = older + timedelta(days=60)
    report = gc_memory(
        state=state,
        memory_path=memory_path,
        threshold_days=30,
        now=now,
    )
    assert report.archived_ids == [mid]
    assert state.memory_index is not None
    assert state.memory_index[mid].tier == MemoryTier.ARCHIVAL


def test_gc_memory_skips_active_status(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    older = datetime(2025, 1, 1, tzinfo=UTC)
    mid = _add_at(
        state,
        memory_path,
        title="t1",
        body="b1",
        when=older,
        status=MemoryStatus.ACTIVE,
    )
    now = older + timedelta(days=60)
    report = gc_memory(
        state=state,
        memory_path=memory_path,
        threshold_days=30,
        now=now,
    )
    assert report.archived_ids == []
    assert mid in report.skipped_ids
    assert report.skipped_reasons[mid] == "status=active"


def test_gc_memory_skips_already_archival(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    older = datetime(2025, 1, 1, tzinfo=UTC)
    mid = _add_at(
        state,
        memory_path,
        title="t1",
        body="b1",
        when=older,
        status=MemoryStatus.STALE,
        tier=MemoryTier.ARCHIVAL,
    )
    now = older + timedelta(days=60)
    report = gc_memory(
        state=state,
        memory_path=memory_path,
        threshold_days=30,
        now=now,
    )
    assert report.archived_ids == []
    assert mid in report.skipped_ids
    assert report.skipped_reasons[mid] == "tier=archival"


def test_gc_memory_skips_younger_than_threshold(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    when = datetime(2025, 1, 1, tzinfo=UTC)
    mid = _add_at(
        state,
        memory_path,
        title="t1",
        body="b1",
        when=when,
        status=MemoryStatus.STALE,
    )
    now = when + timedelta(days=5)
    report = gc_memory(
        state=state,
        memory_path=memory_path,
        threshold_days=30,
        now=now,
    )
    assert report.archived_ids == []
    assert mid in report.skipped_ids
    assert report.skipped_reasons[mid] == "younger-than-threshold"
