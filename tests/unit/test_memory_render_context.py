"""Unit tests for memory.render_context: budget honoured, ranking, skipped count."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eawf.memory.render_context import (
    DEFAULT_BUDGET,
    estimate_tokens,
    render_context,
)
from eawf.memory.store import add_memory
from eawf.state.enums import Confidence
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


def test_estimate_tokens_basic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") >= 2  # 2 words * 1.3 ~= 3 tokens


def test_default_budget_constant() -> None:
    assert DEFAULT_BUDGET == 4096


def test_render_context_empty_state_returns_empty(tmp_path: Path) -> None:
    state = _make_state()
    result = render_context(state=state, memory_path=tmp_path / "memory.jsonl", budget=4096)
    assert result.text == ""
    assert result.included_ids == []
    assert result.skipped_ids == []
    assert result.budget == 4096


def test_render_context_honours_token_budget(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    # Add 5 entries with sizable bodies; budget tight so most must be skipped.
    for i in range(5):
        add_memory(
            state=state,
            memory_path=memory_path,
            scope_id="QR",
            title=f"entry {i}",
            body=" ".join(["word"] * 50),
            confidence=Confidence.HIGH,
        )
    # Each entry yields ~150 tokens (100 body words duplicated). Budget=200
    # admits one entry, skips the rest.
    result = render_context(state=state, memory_path=memory_path, budget=200)
    assert result.tokens_used <= 200
    assert len(result.included_ids) >= 1
    assert len(result.skipped_ids) >= 1


def test_render_context_high_confidence_ranks_first(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    low = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="low",
        body="low body",
        confidence=Confidence.LOW,
    )
    high = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="high",
        body="high body",
        confidence=Confidence.HIGH,
    )
    result = render_context(state=state, memory_path=memory_path, budget=4096)
    # Both included — but the high entry should appear before low.
    assert high.summary.id in result.included_ids
    assert low.summary.id in result.included_ids
    assert result.included_ids.index(high.summary.id) < result.included_ids.index(low.summary.id)


def test_render_context_anchor_scope_boosts_matching(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    other = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="OTHER",
        title="other-scope",
        body="other body",
        confidence=Confidence.MEDIUM,
    )
    matching = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="P01-I01",
        title="matching",
        body="matching body",
        confidence=Confidence.MEDIUM,
    )
    result = render_context(
        state=state,
        memory_path=memory_path,
        anchor_scope="P01-I01",
        budget=4096,
    )
    # Matching scope should appear before other-scope in the included list.
    assert result.included_ids.index(matching.summary.id) < result.included_ids.index(
        other.summary.id
    )


def test_render_context_skipped_count_matches(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    for i in range(10):
        add_memory(
            state=state,
            memory_path=memory_path,
            scope_id="QR",
            title=f"e{i}",
            body=" ".join(["w"] * 50),
        )
    result = render_context(state=state, memory_path=memory_path, budget=100)
    assert len(result.included_ids) + len(result.skipped_ids) == 10


def test_render_context_skips_inactive_entries(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    # Manually flip status to STALE in the cache.
    from eawf.state.enums import MemoryStatus

    assert state.memory_index is not None
    state.memory_index[rec.summary.id] = state.memory_index[rec.summary.id].model_copy(
        update={"status": MemoryStatus.STALE}
    )
    result = render_context(state=state, memory_path=memory_path, budget=4096)
    assert rec.summary.id not in result.included_ids


def test_render_context_explicit_now_decay(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="t",
        body="b",
        review_due=datetime(2026, 1, 1, tzinfo=UTC),
    )
    # Compute renderer at a moment well past review_due — recency weight drops
    # but entry should still be considered (just lower-ranked).
    result = render_context(
        state=state,
        memory_path=memory_path,
        budget=4096,
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert rec.summary.id in result.included_ids
