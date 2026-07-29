"""Unit tests for the D14 ``runtime.kind`` → ``runtime.adapters`` shim."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from eawf.kernel.config import layered
from eawf.kernel.config.layered import _normalise_runtime_adapters, merge_config


@pytest.fixture(autouse=True)
def _reset_warn_flag() -> Any:
    # Reset module-level deprecation flag so each test asserts the warning
    # path independently.
    layered._LEGACY_RUNTIME_WARN_EMITTED = False
    yield
    layered._LEGACY_RUNTIME_WARN_EMITTED = False


def _write_yaml(path: Path, body: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body, sort_keys=True))


def test_legacy_kind_synthesises_adapters(caplog: pytest.LogCaptureFixture) -> None:
    merged: dict[str, Any] = {"runtime": {"kind": "claude-code"}}
    with caplog.at_level(logging.WARNING):
        _normalise_runtime_adapters(merged)
    assert merged["runtime"]["adapters"] == ["claude-code"]
    assert any("deprecated_runtime_kind" in rec.message for rec in caplog.records)


def test_warning_emits_once(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        _normalise_runtime_adapters({"runtime": {"kind": "claude-code"}})
        _normalise_runtime_adapters({"runtime": {"kind": "codex"}})
    deprecated = [r for r in caplog.records if "deprecated_runtime_kind" in r.message]
    assert len(deprecated) == 1


def test_explicit_adapters_wins() -> None:
    merged: dict[str, Any] = {"runtime": {"adapters": ["codex"], "kind": "claude-code"}}
    _normalise_runtime_adapters(merged)
    assert merged["runtime"]["adapters"] == ["codex"]


def test_no_runtime_block_is_noop() -> None:
    merged: dict[str, Any] = {"unrelated": 1}
    _normalise_runtime_adapters(merged)
    assert merged == {"unrelated": 1}


def test_merge_applies_shim_through_overlay(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    repo_dir = tmp_path / "repo"
    _write_yaml(
        workspace / ".ea" / "config.yaml",
        {"runtime": {"kind": "codex"}},
    )
    merged, _sources = merge_config(workspace=workspace, repo=repo_dir, env={})
    assert merged["runtime"]["adapters"] == ["codex"]


def test_merge_v11_overlay_wins(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    repo_dir = tmp_path / "repo"
    _write_yaml(
        workspace / ".ea" / "config.yaml",
        {
            "schema_version": "1.1",
            "runtime": {"adapters": ["opencode"], "kind": "opencode"},
        },
    )
    merged, _sources = merge_config(workspace=workspace, repo=repo_dir, env={})
    assert merged["runtime"]["adapters"] == ["opencode"]
    assert merged["schema_version"] == "1.0"
