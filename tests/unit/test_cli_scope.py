"""Tests for :func:`eawf.cli.scope.resolve_state_path`.

Precedence per ``ea-proposal.md`` §17 / v0.1 plan §W00:

1. ``EA_STATE`` env var (wins over everything).
2. ``-w / --workspace`` flag (wins over pwd-upward).
3. Pwd-upward walk through parents, taking the first ``.ea/state.json`` that
   exists on disk.
4. Otherwise :class:`FileNotFoundError`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.cli.scope import resolve_state_path


@pytest.fixture(autouse=True)
def _isolate_ea_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Ensure no ambient ``EA_STATE`` leaks across tests."""
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
    (workspace / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("EA_STATE", str(env_target))
    resolved = resolve_state_path(workspace=workspace)
    assert resolved == env_target


def test_env_var_wins_over_pwd_upward(
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
    resolved = resolve_state_path(workspace=None)
    assert resolved == env_target


def test_workspace_flag_wins_over_pwd_upward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "ws"
    (workspace / ".ea").mkdir(parents=True)
    pwd_root = tmp_path / "elsewhere"
    (pwd_root / ".ea").mkdir(parents=True)
    (pwd_root / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(pwd_root)
    resolved = resolve_state_path(workspace=workspace)
    assert resolved == workspace / ".ea" / "state.json"


def test_workspace_flag_does_not_require_state_to_exist(tmp_path: Path) -> None:
    """The workspace branch returns the candidate even when the file is absent."""
    workspace = tmp_path / "fresh-ws"
    resolved = resolve_state_path(workspace=workspace)
    assert resolved == workspace / ".ea" / "state.json"
    assert not resolved.exists()


def test_pwd_upward_finds_state_in_current_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    state = repo / ".ea" / "state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.chdir(repo)
    resolved = resolve_state_path(workspace=None)
    assert resolved == state.resolve()


def test_pwd_upward_walks_up_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    state = repo / ".ea" / "state.json"
    state.write_text("{}", encoding="utf-8")
    deep = repo / "src" / "pkg" / "sub"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    resolved = resolve_state_path(workspace=None)
    assert resolved == state.resolve()


def test_missing_state_raises_filenotfounderror(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    # Using a real environment with no EA_STATE and no upward .ea/state.json:
    # depending on the host filesystem this could find a real .ea above tmp,
    # so we explicitly make sure to skip if so.
    walker = empty.resolve()
    for parent in [walker, *walker.parents]:
        if (parent / ".ea" / "state.json").exists():
            pytest.skip(
                f"host has a .ea/state.json at {parent}; cannot exercise the "
                "FileNotFoundError branch from here.",
            )
    with pytest.raises(FileNotFoundError):
        resolve_state_path(workspace=None)


def test_env_var_path_returned_as_is(monkeypatch: pytest.MonkeyPatch) -> None:
    """``EA_STATE`` is honoured verbatim — even relative paths."""
    monkeypatch.setenv("EA_STATE", "relative/path/state.json")
    resolved = resolve_state_path(workspace=None)
    assert resolved == Path("relative/path/state.json")


def test_env_var_takes_precedence_even_when_file_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EA_STATE", str(tmp_path / "not-there.json"))
    resolved = resolve_state_path(workspace=tmp_path)
    assert str(resolved).endswith("not-there.json")


def test_resolved_state_path_is_absolute_when_workspace_absolute(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    resolved = resolve_state_path(workspace=workspace)
    assert resolved.is_absolute()
    assert os.fspath(resolved).endswith(str(Path(".ea") / "state.json"))
