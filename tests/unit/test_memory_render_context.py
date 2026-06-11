"""Unit tests for memory.render_context: budget honoured, ranking, skipped count."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import Confidence
from eawf.kernel.state.models import State
from eawf.platform.memory.render_context import (
    DEFAULT_BUDGET,
    estimate_tokens,
    render_context,
)
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
    from eawf.kernel.state.enums import MemoryStatus

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


# --- W03 hardening: budget contract, max_entries, include_superseded ---------


def test_render_context_never_exceeds_budget_when_first_block_too_big(tmp_path: Path) -> None:
    """Even the first block must not overflow the budget — emit zero blocks."""
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    add_memory(
        state=state,
        memory_path=memory_path,
        scope_id="QR",
        title="huge",
        body=" ".join(["word"] * 200),
    )
    # A budget of 5 is far smaller than even one block.
    result = render_context(state=state, memory_path=memory_path, budget=5)
    assert result.tokens_used == 0
    assert result.included_ids == []
    assert len(result.skipped_ids) == 1
    assert result.tokens_used <= result.budget


def test_render_context_deterministic_for_fixed_inputs(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    for i in range(3):
        add_memory(
            state=state,
            memory_path=memory_path,
            scope_id="QR",
            title=f"t{i}",
            body=f"body {i}",
        )
    moment = datetime(2026, 5, 9, 12, tzinfo=UTC)
    a = render_context(state=state, memory_path=memory_path, budget=4096, now=moment)
    b = render_context(state=state, memory_path=memory_path, budget=4096, now=moment)
    assert a.text == b.text
    assert a.included_ids == b.included_ids
    assert a.skipped_ids == b.skipped_ids
    assert a.tokens_used == b.tokens_used


def test_render_context_max_entries_caps_count(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    for i in range(5):
        add_memory(
            state=state,
            memory_path=memory_path,
            scope_id="QR",
            title=f"t{i}",
            body=f"body{i}",
        )
    result = render_context(state=state, memory_path=memory_path, budget=4096, max_entries=2)
    assert len(result.included_ids) == 2
    # The remaining entries land in skipped.
    assert len(result.skipped_ids) == 3
    assert set(result.included_ids) | set(result.skipped_ids) == set(state.memory_index or {})


def test_render_context_excludes_superseded_by_default(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    from eawf.kernel.state.enums import MemoryStatus

    assert state.memory_index is not None
    state.memory_index[rec.summary.id] = state.memory_index[rec.summary.id].model_copy(
        update={"status": MemoryStatus.SUPERSEDED}
    )
    result = render_context(state=state, memory_path=memory_path, budget=4096)
    assert rec.summary.id not in result.included_ids
    assert rec.summary.id not in result.skipped_ids


def test_render_context_include_superseded_admits_them(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    from eawf.kernel.state.enums import MemoryStatus

    assert state.memory_index is not None
    state.memory_index[rec.summary.id] = state.memory_index[rec.summary.id].model_copy(
        update={"status": MemoryStatus.SUPERSEDED}
    )
    result = render_context(
        state=state, memory_path=memory_path, budget=4096, include_superseded=True
    )
    assert rec.summary.id in result.included_ids


def test_render_context_pruned_never_admitted(tmp_path: Path) -> None:
    """PRUNED is excluded even when include_superseded=True."""
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    rec = add_memory(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
    from eawf.kernel.state.enums import MemoryStatus

    assert state.memory_index is not None
    state.memory_index[rec.summary.id] = state.memory_index[rec.summary.id].model_copy(
        update={"status": MemoryStatus.PRUNED}
    )
    result = render_context(
        state=state, memory_path=memory_path, budget=4096, include_superseded=True
    )
    assert rec.summary.id not in result.included_ids
    assert rec.summary.id not in result.skipped_ids
