"""Tests for the ``hooks_plugins`` statusline module's hook count and plugin record."""

from __future__ import annotations

import json
from pathlib import Path

import orjson
import pytest

from eawf.runtime.runtimes.claude.statusline_modules import hooks_plugins


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``$HOME`` at this test's tmp dir so the host record is the test's own."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


def _seed_state(tmp_path: Path, payload: dict[str, object]) -> Path:
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(payload))
    return state_path


def _plugin_record(home: Path, plugins: dict[str, object]) -> None:
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 2, "plugins": plugins}), encoding="utf-8")


def test_no_state_path_names_why() -> None:
    seg = hooks_plugins.build({}, None)
    assert seg.text == "hooks:n/a(no-state)"
    assert seg.status == "missing"


def test_no_plugin_record_names_why_and_claims_no_health(tmp_path: Path) -> None:
    state_path = _seed_state(tmp_path, {})
    seg = hooks_plugins.build({}, state_path)
    assert seg.text == "hooks:0 plugins:n/a(no-plugin-record)"
    # The count is informational only (no hook exit code is read), so the
    # segment must not claim health it never measured.
    assert seg.status == "degraded"


def test_the_host_record_counts_plugins_not_the_document(tmp_path: Path, home: Path) -> None:
    state_path = _seed_state(tmp_path, {"plugins": {"a": {}, "b": {}, "c": {}}})
    _plugin_record(home, {"claude-core@m": [{"scope": "user"}], "research@m": [{"scope": "user"}]})
    seg = hooks_plugins.build({}, state_path)
    assert seg.text == "hooks:0 plugins:2"
    assert seg.status == "degraded"


def test_hooks_directory_count_added(tmp_path: Path, home: Path) -> None:
    state_path = _seed_state(tmp_path, {})
    _plugin_record(home, {"x@m": [{"scope": "user"}]})
    hooks_dir = tmp_path / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "pre_commit.sh").write_text("#!/bin/sh\n")
    (hooks_dir / "post_commit.sh").write_text("#!/bin/sh\n")
    seg = hooks_plugins.build({}, state_path)
    assert seg.text == "hooks:2 plugins:1"


def test_malformed_state_keeps_the_hook_count(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir()
    state_path.write_bytes(b"not-json")
    seg = hooks_plugins.build({}, state_path)
    assert seg.text == "hooks:0 plugins:n/a(no-plugin-record)"
    assert seg.status == "degraded"
