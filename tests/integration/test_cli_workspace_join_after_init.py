"""A repository can join a shared workspace at or after ``eawf init``.

A plain init makes every repository anchor a workspace of its own code,
which then refuses a join of a shared workspace with
``cross_workspace_mutation_forbidden``. ``init --workspace`` joins at
birth, ``workspace rekey-repo`` moves an initialised repository over,
and ``workspace remove`` drops a workspace only one repository uses.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.platform.registry import (
    CROSS_WORKSPACE_MUTATION_FORBIDDEN,
    WORKSPACE_NOT_REGISTERED,
    Registry,
    RegistryRepoEntry,
    WorkspaceRecord,
    read_registry,
)
from eawf.platform.registry.workspace import WORKSPACE_HAS_OTHER_MEMBERS
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def registry_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the registry at a per-test file holding the ALLODS workspace."""
    path = tmp_path / "home" / ".eawf" / "registry.json"
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(path))
    anchor = tmp_path / "allods"
    anchor.mkdir()
    registry = Registry(
        repos={"ALLODS": RegistryRepoEntry(code="ALLODS", path=str(anchor))},
        workspaces={
            "ALLODS": WorkspaceRecord(
                key="ALLODS",
                member_project_codes=frozenset({"ALLODS"}),
                home_project_code="ALLODS",
            )
        },
    )
    path.parent.mkdir(parents=True)
    path.write_text(registry.model_dump_json(), encoding="utf-8")
    return path


def _repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "pyproject.toml").write_text(f"[project]\nname = '{name}'\n", encoding="utf-8")
    return repo


def _invoke(*argv: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--json", *argv])
    return result.exit_code, json.loads(result.output)


def _init(repo: Path, code: str, *extra: str) -> tuple[int, dict[str, Any]]:
    return _invoke("--no-input", "init", "--target", str(repo), "--project-code", code, *extra)


def test_init_joins_an_existing_workspace(tmp_path: Path, registry_file: Path) -> None:
    repo = _repo(tmp_path, "game")
    code, envelope = _init(repo, "GAME", "--workspace", "ALLODS")
    assert code == exit_codes.OK, envelope
    assert envelope["registry"]["workspace"] == "joined"
    registry = read_registry(path=registry_file)
    assert set(registry.workspaces) == {"ALLODS"}
    allods = registry.workspaces["ALLODS"]
    assert allods.member_project_codes == frozenset({"ALLODS", "GAME"})
    assert allods.home_project_code == "ALLODS"
    assert registry.repos["GAME"].path == str(repo.resolve())


def test_init_refuses_an_unregistered_workspace_before_birth(
    tmp_path: Path, registry_file: Path
) -> None:
    repo = _repo(tmp_path, "game")
    before = registry_file.read_bytes()
    code, envelope = _init(repo, "GAME", "--workspace", "NOPE")
    assert code == exit_codes.USER_ERROR, envelope
    assert envelope["data"]["code"] == WORKSPACE_NOT_REGISTERED
    assert not (repo / ".ea").exists()
    assert registry_file.read_bytes() == before


def test_workspace_remove_only_a_single_member_workspace(
    tmp_path: Path, registry_file: Path
) -> None:
    code, envelope = _invoke("workspace", "member", "add", "ALLODS", "GAME")
    assert code == exit_codes.OK, envelope
    code, envelope = _invoke("workspace", "add", "SOLO", "--home", "SOLO")
    assert code == exit_codes.OK, envelope

    before = registry_file.read_bytes()
    code, envelope = _invoke("workspace", "remove", "ALLODS")
    assert code == exit_codes.USER_ERROR, envelope
    assert envelope["data"]["code"] == WORKSPACE_HAS_OTHER_MEMBERS
    assert registry_file.read_bytes() == before

    code, envelope = _invoke("workspace", "remove", "SOLO")
    assert code == exit_codes.OK, envelope
    registry = read_registry(path=registry_file)
    assert set(registry.workspaces) == {"ALLODS"}
    assert set(registry.repos) == {"ALLODS"}


def test_workspace_remove_refuses_an_unregistered_key(registry_file: Path) -> None:
    before = registry_file.read_bytes()
    code, envelope = _invoke("workspace", "remove", "NOPE")
    assert code == exit_codes.USER_ERROR, envelope
    assert envelope["data"]["code"] == WORKSPACE_NOT_REGISTERED
    assert registry_file.read_bytes() == before


def test_rekey_repo_lets_member_add_succeed(tmp_path: Path, registry_file: Path) -> None:
    repo = _repo(tmp_path, "game")
    code, envelope = _init(repo, "GAME")
    assert code == exit_codes.OK, envelope
    assert set(read_registry(path=registry_file).workspaces) == {"ALLODS", "GAME"}
    code, envelope = _invoke("workspace", "member", "add", "ALLODS", "GAME")
    assert code == exit_codes.USER_ERROR, envelope
    assert envelope["data"]["code"] == CROSS_WORKSPACE_MUTATION_FORBIDDEN

    code, envelope = _invoke("workspace", "rekey-repo", "--to", "ALLODS", "--repo-root", str(repo))
    assert code == exit_codes.OK, envelope

    code, envelope = _invoke("workspace", "member", "add", "ALLODS", "GAME")
    assert code == exit_codes.OK, envelope
    registry = read_registry(path=registry_file)
    assert set(registry.workspaces) == {"ALLODS"}
    assert registry.workspaces["ALLODS"].member_project_codes == frozenset({"ALLODS", "GAME"})


def test_rekey_repo_refuses_an_unregistered_root(tmp_path: Path, registry_file: Path) -> None:
    before = registry_file.read_bytes()
    stray = tmp_path / "stray"
    stray.mkdir()
    code, envelope = _invoke("workspace", "rekey-repo", "--to", "ALLODS", "--repo-root", str(stray))
    assert code == exit_codes.USER_ERROR, envelope
    assert envelope["data"]["code"] == WORKSPACE_NOT_REGISTERED
    assert registry_file.read_bytes() == before


def test_rekey_repo_refuses_a_shared_home_workspace(tmp_path: Path, registry_file: Path) -> None:
    repo = _repo(tmp_path, "game")
    code, envelope = _init(repo, "GAME")
    assert code == exit_codes.OK, envelope
    code, envelope = _invoke("workspace", "member", "add", "GAME", "TOOLS")
    assert code == exit_codes.OK, envelope
    before = registry_file.read_bytes()
    code, envelope = _invoke("workspace", "rekey-repo", "--to", "ALLODS", "--repo-root", str(repo))
    assert code == exit_codes.USER_ERROR, envelope
    assert envelope["data"]["code"] == WORKSPACE_HAS_OTHER_MEMBERS
    assert registry_file.read_bytes() == before
