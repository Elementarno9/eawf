"""Tests for the ``hooks_plugins`` statusline module (Phase 4 W06)."""

from __future__ import annotations

from pathlib import Path

import orjson

from eawf.runtime.runtimes.claude.statusline_modules import hooks_plugins


def _seed_state(tmp_path: Path, payload: dict[str, object]) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(payload))
    return state_path


def test_no_state_path_returns_dash() -> None:
    seg = hooks_plugins.build({}, None)
    assert seg.text == "hooks:- plugins:-"
    assert seg.status == "missing"


def test_state_without_plugins_returns_zero(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path, {})
    seg = hooks_plugins.build({}, state_path)
    assert seg.text == "hooks:0 plugins:0"
    # The count is informational only (no hook exit code is read), so the
    # segment must not claim health it never measured.
    assert seg.status != "ok"
    assert seg.status == "degraded"


def test_state_counts_plugins(tmp_path: Path) -> None:
    state_path = _seed_state(
        tmp_path,
        {
            "plugins": {
                "claude-core": {"id": "claude-core"},
                "research": {"id": "research"},
            }
        },
    )
    seg = hooks_plugins.build({}, state_path)
    assert seg.text == "hooks:0 plugins:2"


def test_hooks_directory_count_added(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path, {"plugins": {"x": {}}})
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "pre_commit.sh").write_text("#!/bin/sh\n")
    (hooks_dir / "post_commit.sh").write_text("#!/bin/sh\n")
    seg = hooks_plugins.build({}, state_path)
    assert seg.text == "hooks:2 plugins:1"


def test_malformed_state_returns_dash(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.write_bytes(b"not-json")
    seg = hooks_plugins.build({}, state_path)
    assert seg.text == "hooks:- plugins:-"
    assert seg.status == "missing"
