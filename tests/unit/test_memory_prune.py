"""Unit tests for ``memory.prune`` — soft-delete policy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import Confidence, MemoryStatus
from eawf.kernel.state.models import State
from eawf.platform.memory.prune import PruneError, PruneResult, prune_memory
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
            "subproject_id": None,
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
    confidence: Confidence = Confidence.MEDIUM,
    scope_id: str = "QR",
    status: MemoryStatus | None = None,
) -> str:
    """Add a memory entry stamped with *when*; optionally flip status post-add."""
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id=scope_id,
        title=title,
        body=body,
        confidence=confidence,
        now=when,
    )
    if status is not None:
        assert state.memory_index is not None
        summary = state.memory_index[rec.summary.id]
        state.memory_index[rec.summary.id] = summary.model_copy(update={"status": status})
    return rec.summary.id


def test_prune_memory_skips_when_age_below_threshold(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    young = datetime(2026, 5, 1, tzinfo=UTC)
    mid = _add_at(state, memory_path, title="recent", body="b", when=young)
    state.memory_index[mid] = state.memory_index[mid].model_copy(  # type: ignore[index]
        update={"status": MemoryStatus.STALE}
    )
    result = prune_memory(
        state=state,
        memory_path=memory_path,
        age_days=30,
        status_filter=MemoryStatus.STALE,
        scope_id=None,
        now=young + timedelta(days=10),
        dry_run=False,
    )
    assert result.pruned_ids == []
    assert mid in result.skipped_ids
    assert state.memory_index[mid].status == MemoryStatus.STALE  # type: ignore[index]


def test_prune_memory_flips_status_to_pruned(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old = datetime(2026, 1, 1, tzinfo=UTC)
    mid = _add_at(state, memory_path, title="ancient", body="b", when=old)
    state.memory_index[mid] = state.memory_index[mid].model_copy(  # type: ignore[index]
        update={"status": MemoryStatus.STALE}
    )
    result = prune_memory(
        state=state,
        memory_path=memory_path,
        age_days=30,
        status_filter=MemoryStatus.STALE,
        scope_id=None,
        now=datetime(2026, 5, 1, tzinfo=UTC),
        dry_run=False,
    )
    assert result.pruned_ids == [mid]
    assert state.memory_index[mid].status == MemoryStatus.PRUNED  # type: ignore[index]


def test_prune_memory_appends_jsonl_with_expired_at(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old = datetime(2026, 1, 1, tzinfo=UTC)
    mid = _add_at(state, memory_path, title="ancient", body="b", when=old)
    state.memory_index[mid] = state.memory_index[mid].model_copy(  # type: ignore[index]
        update={"status": MemoryStatus.STALE}
    )
    moment = datetime(2026, 5, 1, tzinfo=UTC)
    prune_memory(
        state=state,
        memory_path=memory_path,
        age_days=30,
        status_filter=MemoryStatus.STALE,
        scope_id=None,
        now=moment,
        dry_run=False,
    )
    lines = memory_path.read_text(encoding="utf-8").splitlines()
    # First record is the original add; second is the expired tombstone.
    assert len(lines) == 2
    second = json.loads(lines[-1])
    assert second["id"] == mid
    assert second["payload"]["expired_at"] == moment.isoformat()
    assert (
        second["updated_at"] == moment.isoformat().replace("+00:00", "Z")
        or second["updated_at"] == moment.isoformat()
    )


def test_prune_memory_dry_run_does_not_mutate_state_or_jsonl(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old = datetime(2026, 1, 1, tzinfo=UTC)
    mid = _add_at(state, memory_path, title="ancient", body="b", when=old)
    state.memory_index[mid] = state.memory_index[mid].model_copy(  # type: ignore[index]
        update={"status": MemoryStatus.STALE}
    )
    before = memory_path.read_bytes()
    result = prune_memory(
        state=state,
        memory_path=memory_path,
        age_days=30,
        status_filter=MemoryStatus.STALE,
        scope_id=None,
        now=datetime(2026, 5, 1, tzinfo=UTC),
        dry_run=True,
    )
    assert result.dry_run is True
    assert mid in result.pruned_ids
    # Cache unchanged.
    assert state.memory_index[mid].status == MemoryStatus.STALE  # type: ignore[index]
    # JSONL unchanged.
    assert memory_path.read_bytes() == before


def test_prune_memory_filters_by_scope(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old = datetime(2026, 1, 1, tzinfo=UTC)
    mid_qr = _add_at(state, memory_path, title="q", body="b", when=old, scope_id="QR")
    mid_p = _add_at(state, memory_path, title="p", body="b", when=old, scope_id="P01")
    for mid in (mid_qr, mid_p):
        state.memory_index[mid] = state.memory_index[mid].model_copy(  # type: ignore[index]
            update={"status": MemoryStatus.STALE}
        )
    result = prune_memory(
        state=state,
        memory_path=memory_path,
        age_days=30,
        status_filter=MemoryStatus.STALE,
        scope_id="QR",
        now=datetime(2026, 5, 1, tzinfo=UTC),
        dry_run=False,
    )
    assert result.pruned_ids == [mid_qr]
    assert state.memory_index[mid_qr].status == MemoryStatus.PRUNED  # type: ignore[index]
    assert state.memory_index[mid_p].status == MemoryStatus.STALE  # type: ignore[index]


def test_prune_memory_filters_by_status_filter(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old = datetime(2026, 1, 1, tzinfo=UTC)
    mid_active = _add_at(state, memory_path, title="a", body="b", when=old)
    mid_stale = _add_at(state, memory_path, title="s", body="b", when=old)
    state.memory_index[mid_stale] = state.memory_index[mid_stale].model_copy(  # type: ignore[index]
        update={"status": MemoryStatus.STALE}
    )
    # Only STALE entries should flip; ACTIVE remains untouched.
    result = prune_memory(
        state=state,
        memory_path=memory_path,
        age_days=30,
        status_filter=MemoryStatus.STALE,
        scope_id=None,
        now=datetime(2026, 5, 1, tzinfo=UTC),
        dry_run=False,
    )
    assert result.pruned_ids == [mid_stale]
    assert state.memory_index[mid_active].status == MemoryStatus.ACTIVE  # type: ignore[index]


def test_prune_memory_does_not_double_prune(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old = datetime(2026, 1, 1, tzinfo=UTC)
    mid = _add_at(state, memory_path, title="t", body="b", when=old)
    state.memory_index[mid] = state.memory_index[mid].model_copy(  # type: ignore[index]
        update={"status": MemoryStatus.PRUNED}
    )
    # status_filter=None considers any status; we still expect skip on PRUNED.
    result = prune_memory(
        state=state,
        memory_path=memory_path,
        age_days=30,
        status_filter=None,
        scope_id=None,
        now=datetime(2026, 5, 1, tzinfo=UTC),
        dry_run=False,
    )
    assert result.pruned_ids == []
    assert mid in result.skipped_ids
    assert result.skipped_reasons[mid] == "already-pruned"


def test_prune_memory_idempotent_on_replay(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old = datetime(2026, 1, 1, tzinfo=UTC)
    mid = _add_at(state, memory_path, title="t", body="b", when=old)
    state.memory_index[mid] = state.memory_index[mid].model_copy(  # type: ignore[index]
        update={"status": MemoryStatus.STALE}
    )
    # First prune flips to PRUNED.
    prune_memory(
        state=state,
        memory_path=memory_path,
        age_days=30,
        status_filter=MemoryStatus.STALE,
        scope_id=None,
        now=datetime(2026, 5, 1, tzinfo=UTC),
        dry_run=False,
    )
    # Second prune with status_filter=PRUNED is a no-op (already-pruned skip).
    second: PruneResult = prune_memory(
        state=state,
        memory_path=memory_path,
        age_days=30,
        status_filter=None,
        scope_id=None,
        now=datetime(2026, 5, 2, tzinfo=UTC),
        dry_run=False,
    )
    assert second.pruned_ids == []
    assert state.memory_index[mid].status == MemoryStatus.PRUNED  # type: ignore[index]


def test_prune_memory_negative_age_raises(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    with pytest.raises(PruneError, match=">= 0"):
        prune_memory(
            state=state,
            memory_path=memory_path,
            age_days=-1,
            status_filter=MemoryStatus.STALE,
            scope_id=None,
            now=datetime(2026, 5, 1, tzinfo=UTC),
            dry_run=False,
        )
