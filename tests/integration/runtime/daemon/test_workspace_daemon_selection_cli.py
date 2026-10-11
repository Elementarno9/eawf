"""``--workspace`` selects the daemon of the named tree, from any working directory."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.runtime.daemon import spawn as spawn_mod
from eawf.runtime.daemon.runtime_dir import (
    TREES_DIRNAME,
    pinned_workspace,
    runtime_base_dir,
    tree_key,
)
from eawf.runtime.daemon.spawn import DaemonSpawnTimeoutError
from eawf.surfaces.cli import _daemon_client
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

TASK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0001"
TRACK_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-0001"


@pytest.fixture
def default_resolution(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drop every override, the tier's daemonless default too, so verbs dial a daemon."""
    for key in ("EAWF_RUNTIME_DIR", "XDG_RUNTIME_DIR", "EA_STATE", "EAWF_DAEMONLESS"):
        monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def dialled(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record the runtime dir each daemon attach or spawn targets, then fail it."""
    seen: list[Path] = []

    def spawn(runtime_dir: Path) -> int:
        seen.append(runtime_dir)
        raise DaemonSpawnTimeoutError("recorded")

    def ready(runtime_dir: Path) -> int | None:
        seen.append(runtime_dir)
        return None

    monkeypatch.setattr(spawn_mod, "auto_spawn_daemon", spawn)
    monkeypatch.setattr(_daemon_client, "auto_spawn_daemon", spawn)
    monkeypatch.setattr(_daemon_client, "daemon_pid_if_ready", ready)
    return seen


def _tree(root: Path) -> Path:
    (root / ".ea").mkdir(parents=True)
    (root / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    return root


def _marked_tree(root: Path) -> Path:
    """Return the tree at *root* carrying the epoch-2 marker, which admits writes."""
    marker = _tree(root) / ".ea" / GENERATIONS_DIRNAME / MARKER_FILENAME
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    return root


def _key_dir(root: Path) -> Path:
    return runtime_base_dir() / TREES_DIRNAME / tree_key(root / ".ea" / "state.json")


def test_a_read_verb_with_workspace_dials_the_named_tree(
    default_resolution: Path, dialled: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = _tree(tmp_path / "named")
    monkeypatch.chdir(_tree(tmp_path / "other"))

    result = CliRunner().invoke(
        app, ["--workspace", str(named), "task", "assess", TASK_URN, "--actor", "OP-0001"]
    )

    assert result.exit_code == 4, result.output
    assert dialled == [_key_dir(named)]


def test_a_mutating_verb_with_workspace_spawns_the_named_tree(
    default_resolution: Path, dialled: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = _marked_tree(tmp_path / "named")
    monkeypatch.chdir(_marked_tree(tmp_path / "other"))

    result = CliRunner().invoke(
        app,
        [
            "--workspace",
            str(named),
            "track",
            "retire",
            TRACK_URN,
            "--expected-revision",
            "1",
            "--idempotency-key",
            "retire-track-0001",
            "--actor",
            "OP-0001",
            "--yes",
        ],
    )

    assert result.exit_code == 4, result.output
    assert dialled
    assert set(dialled) == {_key_dir(named)}


def test_a_relative_workspace_names_the_same_tree(
    default_resolution: Path, dialled: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = _tree(tmp_path / "named")
    monkeypatch.chdir(_tree(tmp_path / "other"))

    CliRunner().invoke(app, ["-w", "../named", "task", "assess", TASK_URN, "--actor", "OP-0001"])

    assert dialled == [_key_dir(named)]


def test_without_workspace_the_cwd_tree_is_dialled(
    default_resolution: Path, dialled: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tree(tmp_path / "named")
    cwd = _tree(tmp_path / "other")
    monkeypatch.chdir(cwd)

    result = CliRunner().invoke(app, ["task", "assess", TASK_URN, "--actor", "OP-0001"])

    assert result.exit_code == 4, result.output
    assert dialled == [_key_dir(cwd)]


def test_a_nonexistent_workspace_is_refused_before_any_daemon(
    default_resolution: Path, dialled: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_tree(tmp_path / "other"))

    result = CliRunner().invoke(
        app,
        ["--workspace", "../absent", "task", "assess", TASK_URN, "--actor", "OP-0001"],
    )

    # The usage panel wraps its text to the terminal and frames each line, so
    # the refusal is read with the frame and the line breaks taken out. The
    # path is relative because a long tmp path can break mid-word.
    refusal = " ".join(click.unstyle(result.output).replace("\u2502", " ").split())
    assert result.exit_code == 2
    assert "Directory '../absent' does not exist." in refusal
    assert dialled == []


def test_a_file_workspace_is_refused(
    default_resolution: Path, dialled: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_tree(tmp_path / "other"))
    stray = tmp_path / "file.txt"
    stray.write_text("", encoding="utf-8")

    result = CliRunner().invoke(
        app, ["--workspace", str(stray), "task", "assess", TASK_URN, "--actor", "OP-0001"]
    )

    assert result.exit_code == 2
    assert dialled == []


def test_the_pin_is_released_when_the_command_ends(
    default_resolution: Path, dialled: list[Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = _tree(tmp_path / "named")
    monkeypatch.chdir(_tree(tmp_path / "other"))

    CliRunner().invoke(
        app, ["--workspace", str(named), "task", "assess", TASK_URN, "--actor", "OP-0001"]
    )

    assert pinned_workspace() is None
