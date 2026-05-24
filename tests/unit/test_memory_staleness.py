"""Unit tests for memory.staleness: age threshold + confidence filter."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from eawf.kernel.state.enums import Confidence, MemoryStatus
from eawf.kernel.state.models import State
from eawf.memory.staleness import find_stale
from eawf.memory.store import add_memory


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


def test_find_stale_returns_aged_low_confidence(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    old_moment = datetime(2026, 1, 1, tzinfo=UTC)
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="t",
        body="b",
        confidence=Confidence.LOW,
        now=old_moment,
    )
    now = datetime(2026, 5, 1, tzinfo=UTC)
    out = find_stale(state=state, memory_path=memory_path, age_days=30, now=now)
    assert any(e.id == rec.summary.id for e in out)


def test_find_stale_excludes_high_confidence(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="t",
        body="b",
        confidence=Confidence.HIGH,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    out = find_stale(
        state=state,
        memory_path=memory_path,
        age_days=30,
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert not any(e.id == rec.summary.id for e in out)


def test_find_stale_excludes_inactive(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="t",
        body="b",
        confidence=Confidence.LOW,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert state.memory_index is not None
    state.memory_index[rec.summary.id] = state.memory_index[rec.summary.id].model_copy(
        update={"status": MemoryStatus.SUPERSEDED}
    )
    out = find_stale(
        state=state,
        memory_path=memory_path,
        age_days=30,
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )
    assert not any(e.id == rec.summary.id for e in out)


def test_find_stale_age_threshold_boundary(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    moment = datetime(2026, 5, 1, tzinfo=UTC)
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="t",
        body="b",
        confidence=Confidence.LOW,
        now=moment - timedelta(days=30),
    )
    # Exactly at threshold — should be flagged.
    out = find_stale(state=state, memory_path=memory_path, age_days=30, now=moment)
    assert any(e.id == rec.summary.id for e in out)


def test_find_stale_below_threshold_excluded(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    moment = datetime(2026, 5, 1, tzinfo=UTC)
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="t",
        body="b",
        confidence=Confidence.LOW,
        now=moment - timedelta(days=29),
    )
    out = find_stale(state=state, memory_path=memory_path, age_days=30, now=moment)
    assert not any(e.id == rec.summary.id for e in out)


def test_find_stale_scope_filter(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    moment = datetime(2026, 5, 1, tzinfo=UTC)
    a = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="a",
        body="a",
        confidence=Confidence.LOW,
        now=moment - timedelta(days=60),
    )
    b = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="OTHER",
        title="b",
        body="b",
        confidence=Confidence.LOW,
        now=moment - timedelta(days=60),
    )
    out = find_stale(
        state=state,
        memory_path=memory_path,
        age_days=30,
        now=moment,
        scope_id="QR",
    )
    ids = {e.id for e in out}
    assert a.summary.id in ids
    assert b.summary.id not in ids


def test_find_stale_uses_review_due_when_set(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    moment = datetime(2026, 5, 1, tzinfo=UTC)
    review = datetime(2025, 1, 1, tzinfo=UTC)
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="t",
        body="b",
        confidence=Confidence.MEDIUM,
        review_due=review,
        now=moment,  # created today
    )
    # review_due is far in the past so the entry should be stale.
    out = find_stale(state=state, memory_path=memory_path, age_days=30, now=moment)
    assert any(e.id == rec.summary.id for e in out)


def test_find_stale_empty_state_returns_empty(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    out = find_stale(
        state=state,
        memory_path=memory_path,
        age_days=30,
        now=datetime.now(UTC),
    )
    assert out == []


def test_find_stale_sorted_by_age_desc(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    moment = datetime(2026, 5, 1, tzinfo=UTC)
    older = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="older",
        body="b",
        confidence=Confidence.LOW,
        now=moment - timedelta(days=120),
    )
    newer = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="newer",
        body="b",
        confidence=Confidence.LOW,
        now=moment - timedelta(days=60),
    )
    out = find_stale(state=state, memory_path=memory_path, age_days=30, now=moment)
    ordered = [e.id for e in out]
    assert ordered.index(older.summary.id) < ordered.index(newer.summary.id)
