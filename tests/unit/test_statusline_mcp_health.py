"""Tests for the ``mcp_health`` statusline module (Phase 4 W06)."""

from __future__ import annotations

from pathlib import Path

import orjson

from eawf.runtime.runtimes.claude.statusline_modules import mcp_health


def _seed_state(tmp_path: Path, payload: dict[str, object]) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(payload))
    return state_path


def test_no_state_path_returns_question() -> None:
    seg = mcp_health.build({}, None)
    assert seg.text == "mcp:?"
    assert seg.status == "missing"


def test_state_without_mcp_servers_returns_question(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path, {})
    seg = mcp_health.build({}, state_path)
    assert seg.text == "mcp:?"
    assert seg.status == "missing"


def test_all_servers_up_renders_ok(tmp_path: Path) -> None:
    state_path = _seed_state(
        tmp_path,
        {
            "mcp_servers": {
                "a": {"id": "a", "status": "up"},
                "b": {"id": "b", "status": "up"},
            }
        },
    )
    seg = mcp_health.build({}, state_path)
    assert seg.text == "mcp:2/2"
    assert seg.status == "ok"


def test_partial_up_renders_warn(tmp_path: Path) -> None:
    state_path = _seed_state(
        tmp_path,
        {
            "mcp_servers": {
                "a": {"id": "a", "status": "up"},
                "b": {"id": "b", "status": "down"},
            }
        },
    )
    seg = mcp_health.build({}, state_path)
    assert seg.text == "mcp:1/2"
    assert seg.status == "warn"


def test_all_down_renders_degraded(tmp_path: Path) -> None:
    state_path = _seed_state(
        tmp_path,
        {
            "mcp_servers": {
                "a": {"id": "a", "status": "down"},
                "b": {"id": "b", "status": "down"},
            }
        },
    )
    seg = mcp_health.build({}, state_path)
    assert seg.text == "mcp:0/2"
    assert seg.status == "degraded"


def test_malformed_state_returns_question(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.write_bytes(b"{invalid")
    seg = mcp_health.build({}, state_path)
    assert seg.text == "mcp:?"
    assert seg.status == "missing"
