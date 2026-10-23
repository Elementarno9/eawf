"""Unit tests for the epoch-1 memory store readers and summary text."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.enums import Confidence, StoreKind
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.memory import MemoryPayload
from eawf.platform.memory.store import content_hash, read_envelopes, summary_text


def test_read_envelopes_empty_file(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.jsonl"
    memory_path.write_text("", encoding="utf-8")
    assert read_envelopes(memory_path) == []


def test_read_envelopes_skips_blank_lines(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.jsonl"
    append_envelope(
        memory_path,
        Envelope(
            id="MEM-20260508-01",
            kind=StoreKind.MEMORY,
            scope_id="QR",
            created_at=datetime(2026, 5, 8, tzinfo=UTC),
            summary=summary_text("t", "b"),
            payload=MemoryPayload(body="b", confidence=Confidence.MEDIUM).model_dump(mode="json"),
        ),
    )
    text = memory_path.read_text(encoding="utf-8")
    memory_path.write_text(text + "\n\n\n", encoding="utf-8")
    assert len(read_envelopes(memory_path)) == 1


def test_content_hash_is_stable() -> None:
    a = content_hash("QR", "title", "body")
    b = content_hash("QR", "title", "body")
    c = content_hash("QR", "title", "body different")
    assert a == b
    assert a != c


def test_summary_text_joins_title_and_first_body_line() -> None:
    assert summary_text("title", "first\nsecond") == "title: first"
    assert summary_text("title", "   ") == "title"


def test_summary_text_truncates_past_480_chars() -> None:
    text = summary_text("t", "x" * 1000)
    assert len(text) == 480
    assert text.endswith("...")
