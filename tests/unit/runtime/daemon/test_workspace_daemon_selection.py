"""A pinned workspace selects the runtime dir and spawn environment of the named tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.daemon import spawn as spawn_mod
from eawf.runtime.daemon.runtime_dir import (
    TREES_DIRNAME,
    pin_workspace,
    runtime_base_dir,
    runtime_dir,
    tree_key,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def default_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drop every override so the runtime dir takes the per-tree default."""
    for key in ("EAWF_RUNTIME_DIR", "XDG_RUNTIME_DIR", "EA_STATE"):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _tree(root: Path) -> Path:
    (root / ".ea").mkdir(parents=True)
    (root / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    return root


def _key_dir(root: Path) -> Path:
    return runtime_base_dir() / TREES_DIRNAME / tree_key(root / ".ea" / "state.json")


def test_pin_workspace_returns_the_previous_pin(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _tree(tmp_path / "first")
    second = _tree(tmp_path / "second")
    monkeypatch.chdir(_tree(tmp_path / "other"))

    assert pin_workspace(first) is None
    try:
        assert pin_workspace(second) == first.resolve()
        assert runtime_dir() == _key_dir(second)
    finally:
        pin_workspace(None)
    assert runtime_dir() == _key_dir(tmp_path / "other")


def test_ea_state_still_wins_over_a_pinned_workspace(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_tree = _tree(tmp_path / "env")
    monkeypatch.setenv("EA_STATE", str(env_tree / ".ea" / "state.json"))
    pin_workspace(_tree(tmp_path / "named"))
    try:
        assert runtime_dir() == _key_dir(env_tree)
    finally:
        pin_workspace(None)


def test_a_spawned_daemon_is_told_the_pinned_tree(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = _tree(tmp_path / "named")
    monkeypatch.chdir(_tree(tmp_path / "other"))
    pin_workspace(named)
    try:
        env = spawn_mod.daemon_environ(tmp_path / "rt")
    finally:
        pin_workspace(None)

    assert env["EAWF_RUNTIME_DIR"] == str(tmp_path / "rt")
    assert env["EA_STATE"] == str((named / ".ea" / "state.json").resolve())


def test_an_unpinned_spawn_leaves_the_daemon_to_its_cwd(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_tree(tmp_path / "other"))

    env = spawn_mod.daemon_environ(tmp_path / "rt")

    assert env["EAWF_RUNTIME_DIR"] == str(tmp_path / "rt")
    assert "EA_STATE" not in env
