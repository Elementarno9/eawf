"""Unit tests for the epoch-1 memory store readers and summary text."""

from __future__ import annotations

from eawf.platform.memory.store import content_hash, summary_text


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
