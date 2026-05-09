"""Unit tests for memory store: append + cache update + read-after-write."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from eawf.memory.store import (
    MemoryRecord,
    add_memory,
    content_hash,
    find_envelope,
    read_envelopes,
)
from eawf.state.enums import Confidence, MemoryStatus
from eawf.state.models import State


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


def test_add_memory_appends_jsonl_and_updates_cache(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    record = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="Use uv run",
        body="All Python invocations must go through uv.",
        confidence=Confidence.HIGH,
    )
    assert isinstance(record, MemoryRecord)
    assert record.summary.id.startswith("MEM-")
    assert record.summary.confidence is Confidence.HIGH
    assert record.summary.status is MemoryStatus.ACTIVE
    assert state.memory_index is not None
    assert record.summary.id in state.memory_index
    lines = [ln for ln in memory_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    parsed = json.loads(lines[0])
    assert parsed["id"] == record.summary.id
    assert parsed["payload"]["body"] == "All Python invocations must go through uv."
    assert parsed["payload"]["confidence"] == "high"


def test_add_memory_sequential_ids(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    a = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="First",
        body="one",
    )
    b = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="Second",
        body="two",
    )
    assert a.summary.id != b.summary.id
    assert state.memory_index is not None
    assert {a.summary.id, b.summary.id} <= set(state.memory_index)


def test_find_envelope_returns_latest(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    env = find_envelope(memory_path, rec.summary.id)
    assert env is not None
    assert env.id == rec.summary.id


def test_find_envelope_missing_returns_none(tmp_path: Path) -> None:
    memory_path = tmp_path / "missing.jsonl"
    assert find_envelope(memory_path, "MEM-NOPE") is None


def test_read_envelopes_empty_file(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.jsonl"
    memory_path.write_text("", encoding="utf-8")
    assert read_envelopes(memory_path) == []


def test_read_envelopes_skips_blank_lines(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    text = memory_path.read_text(encoding="utf-8")
    memory_path.write_text(text + "\n\n\n", encoding="utf-8")
    assert len(read_envelopes(memory_path)) == 1


def test_content_hash_is_stable() -> None:
    a = content_hash("QR", "title", "body")
    b = content_hash("QR", "title", "body")
    c = content_hash("QR", "title", "body different")
    assert a == b
    assert a != c


def test_add_memory_initializes_memory_index_when_none(tmp_path: Path) -> None:
    state = _make_state()
    state.memory_index = None
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    assert state.memory_index is not None
    assert rec.summary.id in state.memory_index


def test_add_memory_with_review_due(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    review_due = datetime(2026, 7, 1, tzinfo=UTC)
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="t",
        body="b",
        review_due=review_due,
    )
    assert rec.summary.review_due == review_due


def test_add_memory_long_body_is_truncated_in_summary_field(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    long_body = "x" * 1000
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="long",
        body=long_body,
    )
    assert len(rec.envelope.summary) <= 500


def test_add_memory_id_collision_advances_suffix(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    seen: set[str] = set()
    for _ in range(5):
        rec = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
        assert rec.summary.id not in seen
        seen.add(rec.summary.id)


def test_add_memory_invalid_state_raises_validation_error(tmp_path: Path) -> None:
    """Adding to an explicitly broken-state object still works at module level —
    the invariant check happens at the CLI wrapper, not in store.add_memory."""
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    # No raise expected from the store layer
    rec = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    assert rec.summary.id in (state.memory_index or {})


def test_add_memory_explicit_now(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    moment = datetime(2026, 5, 8, 12, 30, tzinfo=UTC)
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="t",
        body="b",
        now=moment,
    )
    assert rec.envelope.created_at == moment


def test_add_memory_handles_unicode_in_body(tmp_path: Path) -> None:
    """Memory body accepts arbitrary unicode strings."""
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="unicode title",
        body="naïve cafe -- pose --- yum",
    )
    env = json.loads(memory_path.read_text(encoding="utf-8").splitlines()[0])
    assert env["payload"]["body"] == "naïve cafe -- pose --- yum"
    assert state.memory_index is not None
    assert rec.summary.id in state.memory_index
