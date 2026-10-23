"""Tests for the ``state.read`` JSON-RPC handler.

The handler is driven through its module-level coroutine; JSON-RPC framing
is exercised in :mod:`tests.integration.runtime.daemon.test_scaffolding`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.runtime.daemon.methods.state import read
from tests.fixtures.migration.epoch1_daemon_tree import write_state
from tests.integration.runtime.daemon._method_context import build_ctx

pytestmark = pytest.mark.unit


def _read(ctx: Any, params: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(read(ctx, params))


def test_state_read_returns_state_and_version(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    write_state(state_path)

    result = _read(build_ctx(tmp_path, state_path), {})

    assert result["state"]["project"]["code"] == "ABC"
    assert isinstance(result["version"], str)
    assert len(result["version"]) == 16


def test_state_read_version_is_stable_across_reads(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    write_state(state_path)
    ctx = build_ctx(tmp_path, state_path)

    assert _read(ctx, {})["version"] == _read(ctx, {})["version"]


def test_state_read_honours_an_explicit_repo_root(tmp_path: Path) -> None:
    bound = tmp_path / "bound" / ".ea" / "state.json"
    write_state(bound)
    other_root = tmp_path / "other"
    write_state(other_root / ".ea" / "state.json")

    result = _read(build_ctx(tmp_path, bound), {"repo_root": str(other_root)})

    assert result["state"]["project"]["code"] == "ABC"


def test_state_read_rejects_unknown_field(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    write_state(state_path)

    with pytest.raises(ValidationError, match=r"(unexpected|extra)"):
        _read(build_ctx(tmp_path, state_path), {"unexpected": "field"})


def test_state_read_rejects_a_mistyped_repo_root(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    write_state(state_path)

    with pytest.raises(ValidationError):
        _read(build_ctx(tmp_path, state_path), {"repo_root": 7})


def test_state_read_refuses_a_missing_state_file(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"

    with pytest.raises(FileNotFoundError):
        _read(build_ctx(tmp_path, state_path), {})


def test_state_read_refuses_a_schema_invalid_state(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text('{"schema_version": "1.0"}', encoding="utf-8")

    with pytest.raises(ValueError, match="state schema invalid"):
        _read(build_ctx(tmp_path, state_path), {})
