"""Tests for :func:`eawf.state.resolve.resolve_with_reason`.

Covers the same precedence as ``cli/scope.py`` (``EA_STATE`` > ``-w`` > pwd)
but verifies the reason string surfaced to CLI users via ``eawf state resolve``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.state.resolve import (
    REASON_ENV,
    REASON_PWD_UPWARD,
    REASON_WORKSPACE_FLAG,
    resolve_with_reason,
)


@pytest.fixture(autouse=True)
def _isolate_ea_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("EA_STATE", raising=False)
    yield


def test_env_var_wins_over_workspace_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_target = tmp_path / "env_state.json"
    env_target.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "ws"
    (workspace / ".ea").mkdir(parents=True)
    monkeypatch.setenv("EA_STATE", str(env_target))
    path, reason = resolve_with_reason(workspace=workspace)
    assert path == env_target
    assert reason == REASON_ENV


def test_env_var_wins_over_pwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_target = tmp_path / "env_state.json"
    env_target.write_text("{}", encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(env_target))
    monkeypatch.chdir(repo)
    path, reason = resolve_with_reason(workspace=None)
    assert path == env_target
    assert reason == REASON_ENV


def test_workspace_flag_when_env_unset(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    path, reason = resolve_with_reason(workspace=workspace)
    assert path == workspace / ".ea" / "state.json"
    assert reason == REASON_WORKSPACE_FLAG


def test_workspace_flag_does_not_require_state_existence(tmp_path: Path) -> None:
    """Workspace branch returns the candidate even when state file is absent."""
    workspace = tmp_path / "fresh-ws"
    path, reason = resolve_with_reason(workspace=workspace)
    assert path == workspace / ".ea" / "state.json"
    assert reason == REASON_WORKSPACE_FLAG
    assert not path.exists()


def test_pwd_upward_finds_in_current_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    state = repo / ".ea" / "state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(repo)
    path, reason = resolve_with_reason(workspace=None)
    assert path == state.resolve()
    assert reason == REASON_PWD_UPWARD


def test_pwd_upward_walks_to_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    state = repo / ".ea" / "state.json"
    state.write_text("{}", encoding="utf-8")
    deep = repo / "src" / "pkg"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    path, reason = resolve_with_reason(workspace=None)
    assert path == state.resolve()
    assert reason == REASON_PWD_UPWARD


def test_pwd_upward_fallback_when_no_state_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no candidate exists, returns ``cwd / .ea / state.json`` with reason pwd_upward."""
    empty = tmp_path / "empty"
    empty.mkdir()
    walker = empty.resolve()
    for parent in [walker, *walker.parents]:
        if (parent / ".ea" / "state.json").exists():
            pytest.skip(f"host has a .ea/state.json at {parent}")
    monkeypatch.chdir(empty)
    path, reason = resolve_with_reason(workspace=None)
    assert path == empty.resolve() / ".ea" / "state.json"
    assert reason == REASON_PWD_UPWARD
    assert not path.exists()


def test_explicit_env_dict_overrides_real_environ(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A custom ``env`` mapping takes precedence over :data:`os.environ`."""
    custom = tmp_path / "custom_state.json"
    custom.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_STATE", "/should/not/be/used.json")
    fake_env: dict[str, str] = {"EA_STATE": str(custom)}
    path, reason = resolve_with_reason(workspace=None, env=fake_env)  # type: ignore[arg-type]
    assert path == custom
    assert reason == REASON_ENV


def test_empty_env_dict_falls_through_to_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    fake_env: dict[str, str] = {}
    path, reason = resolve_with_reason(workspace=workspace, env=fake_env)  # type: ignore[arg-type]
    assert path == workspace / ".ea" / "state.json"
    assert reason == REASON_WORKSPACE_FLAG


def test_relative_env_path_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EA_STATE", "relative/path/state.json")
    path, reason = resolve_with_reason(workspace=None)
    assert path == Path("relative/path/state.json")
    assert reason == REASON_ENV
