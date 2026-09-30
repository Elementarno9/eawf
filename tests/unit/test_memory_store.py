"""Unit tests for the epoch-1 memory store readers and summary text."""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.state.models import State
from eawf.platform.memory.store import content_hash, read_envelopes, summary_text
from tests._memory_epoch1 import add_epoch1_note


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


def test_read_envelopes_empty_file(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.jsonl"
    memory_path.write_text("", encoding="utf-8")
    assert read_envelopes(memory_path) == []


def test_read_envelopes_skips_blank_lines(tmp_path: Path) -> None:
    state = _make_state()
    memory_path = tmp_path / "memory.jsonl"
    add_epoch1_note(state=state, memory_path=memory_path, scope_id="QR", title="t", body="b")
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
