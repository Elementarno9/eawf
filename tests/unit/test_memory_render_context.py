"""Unit tests for memory.render_context: budget honoured, ranking, skipped count."""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.state.enums import Confidence, MemoryStatus
from eawf.kernel.store.kinds.memory import MemoryNote
from eawf.platform.memory.book import next_note_id
from eawf.platform.memory.render_context import (
    DEFAULT_BUDGET,
    estimate_tokens,
    render_notes,
)
from eawf.platform.memory.store import summary_text

_NOW = datetime(2026, 5, 8, tzinfo=UTC)


def _note(
    notes: list[MemoryNote],
    *,
    scope_id: str,
    title: str,
    body: str,
    confidence: Confidence = Confidence.MEDIUM,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    review_due: datetime | None = None,
) -> MemoryNote:
    """Append one note to *notes* under the next free id, and return it."""
    note = MemoryNote(
        id=next_note_id((n.id for n in notes), now=_NOW),
        scope_id=scope_id,
        title=title,
        summary=summary_text(title, body),
        body=body,
        confidence=confidence,
        status=status,
        review_due=review_due,
        created_at=_NOW,
    )
    notes.append(note)
    return note


def test_estimate_tokens_basic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") >= 2  # 2 words * 1.3 ~= 3 tokens


def test_default_budget_constant() -> None:
    assert DEFAULT_BUDGET == 4096


def test_render_notes_without_notes_returns_empty() -> None:
    result = render_notes([], budget=4096)
    assert result.text == ""
    assert result.included_ids == []
    assert result.skipped_ids == []
    assert result.budget == 4096


def test_render_notes_honours_token_budget() -> None:
    notes: list[MemoryNote] = []
    # Add 5 entries with sizable bodies; budget tight so most must be skipped.
    for i in range(5):
        _note(
            notes,
            scope_id="QR",
            title=f"entry {i}",
            body=" ".join(["word"] * 50),
            confidence=Confidence.HIGH,
        )
    # Each entry yields ~150 tokens (100 body words duplicated). Budget=200
    # admits one entry, skips the rest.
    result = render_notes(notes, budget=200)
    assert result.tokens_used <= 200
    assert len(result.included_ids) >= 1
    assert len(result.skipped_ids) >= 1


def test_render_notes_high_confidence_ranks_first() -> None:
    notes: list[MemoryNote] = []
    low = _note(notes, scope_id="QR", title="low", body="low body", confidence=Confidence.LOW)
    high = _note(notes, scope_id="QR", title="high", body="high body", confidence=Confidence.HIGH)
    result = render_notes(notes, budget=4096)
    # Both included — but the high entry should appear before low.
    assert high.id in result.included_ids
    assert low.id in result.included_ids
    assert result.included_ids.index(high.id) < result.included_ids.index(low.id)


def test_render_notes_anchor_scope_boosts_matching() -> None:
    notes: list[MemoryNote] = []
    other = _note(notes, scope_id="OTHER", title="other-scope", body="other body")
    matching = _note(notes, scope_id="P01-I01", title="matching", body="matching body")
    result = render_notes(notes, anchor_scope="P01-I01", budget=4096)
    # Matching scope should appear before other-scope in the included list.
    assert result.included_ids.index(matching.id) < result.included_ids.index(other.id)


def test_render_notes_skipped_count_matches() -> None:
    notes: list[MemoryNote] = []
    for i in range(10):
        _note(notes, scope_id="QR", title=f"e{i}", body=" ".join(["w"] * 50))
    result = render_notes(notes, budget=100)
    assert len(result.included_ids) + len(result.skipped_ids) == 10


def test_render_notes_skips_inactive_entries() -> None:
    notes: list[MemoryNote] = []
    rec = _note(notes, scope_id="QR", title="t", body="b", status=MemoryStatus.STALE)
    result = render_notes(notes, budget=4096)
    assert rec.id not in result.included_ids


def test_render_notes_explicit_now_decay() -> None:
    notes: list[MemoryNote] = []
    rec = _note(
        notes, scope_id="QR", title="t", body="b", review_due=datetime(2026, 1, 1, tzinfo=UTC)
    )
    # Compute renderer at a moment well past review_due — recency weight drops
    # but entry should still be considered (just lower-ranked).
    result = render_notes(notes, budget=4096, now=datetime(2027, 1, 1, tzinfo=UTC))
    assert rec.id in result.included_ids


# --- W03 hardening: budget contract, max_entries, include_superseded ---------


def test_render_notes_never_exceeds_budget_when_first_block_too_big() -> None:
    """Even the first block must not overflow the budget — emit zero blocks."""
    notes: list[MemoryNote] = []
    _note(notes, scope_id="QR", title="huge", body=" ".join(["word"] * 200))
    # A budget of 5 is far smaller than even one block.
    result = render_notes(notes, budget=5)
    assert result.tokens_used == 0
    assert result.included_ids == []
    assert len(result.skipped_ids) == 1
    assert result.tokens_used <= result.budget


def test_render_notes_deterministic_for_fixed_inputs() -> None:
    notes: list[MemoryNote] = []
    for i in range(3):
        _note(notes, scope_id="QR", title=f"t{i}", body=f"body {i}")
    moment = datetime(2026, 5, 9, 12, tzinfo=UTC)
    a = render_notes(notes, budget=4096, now=moment)
    b = render_notes(notes, budget=4096, now=moment)
    assert a.text == b.text
    assert a.included_ids == b.included_ids
    assert a.skipped_ids == b.skipped_ids
    assert a.tokens_used == b.tokens_used


def test_render_notes_max_entries_caps_count() -> None:
    notes: list[MemoryNote] = []
    for i in range(5):
        _note(notes, scope_id="QR", title=f"t{i}", body=f"body{i}")
    result = render_notes(notes, budget=4096, max_entries=2)
    assert len(result.included_ids) == 2
    # The remaining entries land in skipped.
    assert len(result.skipped_ids) == 3
    assert set(result.included_ids) | set(result.skipped_ids) == {n.id for n in notes}


def test_render_notes_excludes_superseded_by_default() -> None:
    notes: list[MemoryNote] = []
    rec = _note(notes, scope_id="QR", title="t", body="b", status=MemoryStatus.SUPERSEDED)
    result = render_notes(notes, budget=4096)
    assert rec.id not in result.included_ids
    assert rec.id not in result.skipped_ids


def test_render_notes_include_superseded_admits_them() -> None:
    notes: list[MemoryNote] = []
    rec = _note(notes, scope_id="QR", title="t", body="b", status=MemoryStatus.SUPERSEDED)
    result = render_notes(notes, budget=4096, include_superseded=True)
    assert rec.id in result.included_ids


def test_render_notes_pruned_never_admitted() -> None:
    """PRUNED is excluded even when include_superseded=True."""
    notes: list[MemoryNote] = []
    rec = _note(notes, scope_id="QR", title="t", body="b", status=MemoryStatus.PRUNED)
    result = render_notes(notes, budget=4096, include_superseded=True)
    assert rec.id not in result.included_ids
    assert rec.id not in result.skipped_ids
