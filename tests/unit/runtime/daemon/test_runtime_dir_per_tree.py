"""The default runtime dir is keyed by the tree a daemon would serve."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.kernel.state.resolve import REASON_PWD_UPWARD, resolve_with_reason
from eawf.runtime.daemon import runtime_dir as runtime_dir_mod
from eawf.runtime.daemon.runtime_dir import (
    TREES_DIRNAME,
    ensure_runtime_dir,
    runtime_base_dir,
    runtime_dir,
    socket_path,
    tree_key,
)

# macOS caps the AF_UNIX sun_path at 104 bytes.
_AF_UNIX_SUN_PATH_CAP = 104


@pytest.fixture
def default_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drop every override so ``runtime_dir`` takes the per-tree default."""
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


def test_two_trees_resolve_two_runtime_dirs(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_tree(tmp_path / "a"))
    first = runtime_dir()
    monkeypatch.chdir(_tree(tmp_path / "b"))
    second = runtime_dir()

    assert first != second
    assert first.parent == second.parent == default_resolution / ".eawfd" / TREES_DIRNAME


def test_subdirectory_of_a_tree_shares_its_runtime_dir(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "a")
    monkeypatch.chdir(root)
    at_root = runtime_dir()
    (root / "src" / "pkg").mkdir(parents=True)
    monkeypatch.chdir(root / "src" / "pkg")

    assert runtime_dir() == at_root


def test_ea_state_names_the_tree(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _tree(tmp_path / "a") / ".ea" / "state.json"
    monkeypatch.chdir(_tree(tmp_path / "b"))
    monkeypatch.setenv("EA_STATE", str(state))

    assert runtime_dir().name == tree_key(state)


def test_override_is_used_verbatim(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(tmp_path / "pinned"))

    assert runtime_dir() == tmp_path / "pinned"


def test_tree_key_ignores_path_spelling(tmp_path: Path) -> None:
    state = _tree(tmp_path / "a") / ".ea" / "state.json"
    spelled = tmp_path / "a" / "x" / ".." / ".ea" / "state.json"
    (tmp_path / "a" / "x").mkdir()

    assert tree_key(spelled) == tree_key(state)
    assert len(tree_key(state)) == 16


def test_socket_under_a_home_of_ordinary_length_fits_the_cap(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_tree(tmp_path / "a"))
    # A home of 33 bytes, longer than most real ones.
    monkeypatch.setenv("HOME", "/" + "h" * 32)

    assert len(str(socket_path()).encode()) < _AF_UNIX_SUN_PATH_CAP


def test_linux_base_follows_xdg_runtime_dir(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime_dir_mod.sys, "platform", "linux")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))

    assert runtime_base_dir() == tmp_path / "run" / "eawfd"


@pytest.mark.skipif(runtime_dir_mod.os.name == "nt", reason="POSIX modes")
def test_ensure_hardens_the_base_and_trees_dirs(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_tree(tmp_path / "a"))
    rt_dir = ensure_runtime_dir()

    for path in (rt_dir, rt_dir.parent, rt_dir.parent.parent):
        assert path.stat().st_mode & 0o777 == 0o700


def _marked_tree(root: Path) -> Path:
    """Return *root* holding only the epoch-2 marker, with no v1 ``state.json``."""
    marker = root / ".ea" / GENERATIONS_DIRNAME / MARKER_FILENAME
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    return root


def test_a_marked_tree_without_state_json_anchors_the_upward_walk(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _marked_tree(tmp_path / "a")
    (root / "src" / "pkg").mkdir(parents=True)
    monkeypatch.chdir(root / "src" / "pkg")

    path, reason = resolve_with_reason(workspace=None)

    assert (path, reason) == ((root / ".ea" / "state.json").resolve(), REASON_PWD_UPWARD)
    assert not path.exists()
    assert runtime_dir().name == tree_key(root / ".ea" / "state.json")


def test_a_generations_dir_without_the_marker_does_not_anchor_the_walk(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = _tree(tmp_path / "outer")
    inner = outer / "inner"
    (inner / ".ea" / GENERATIONS_DIRNAME).mkdir(parents=True)
    monkeypatch.chdir(inner)

    path, reason = resolve_with_reason(workspace=None)

    assert (path, reason) == ((outer / ".ea" / "state.json").resolve(), REASON_PWD_UPWARD)


def test_the_nearest_marked_tree_wins_over_an_outer_v1_tree(
    default_resolution: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer = _tree(tmp_path / "outer")
    inner = _marked_tree(outer / "inner")
    monkeypatch.chdir(inner)

    path, _reason = resolve_with_reason(workspace=None)

    assert path == (inner / ".ea" / "state.json").resolve()
