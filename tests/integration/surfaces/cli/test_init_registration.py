"""``eawf init`` registers what it bears, and its refusals stay operator-facing.

A plain init lists the repository and a workspace of the same code in the
user-scope registry, so ``eawf ui`` finds it without a manual
``repo register``. In a git repository with a commit it also admits the
tree's repository row, which a directory without a git head never gets.
An opt-in whose principal is invalid is refused before
it writes a backup nothing pins.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.identity import IdentityError
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.opt_in import declare_opt_in
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.kernel.store.compaction import read_document
from eawf.platform.install.repository_row import (
    RepositoryRowOutcome,
    admit_tree_repository,
    tree_repository_urn,
)
from eawf.platform.registry import (
    WORKSPACE_ALREADY_REGISTERED,
    Registry,
    RegistryRepoEntry,
    WorkspaceRecord,
    default_registry_path,
    read_registry,
)
from eawf.runtime.daemon.epoch2_transaction import (
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()


@pytest.fixture
def registry_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the registry at a per-test file that does not exist yet."""
    path = tmp_path / "home" / ".eawf" / "registry.json"
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(path))
    return path


@pytest.fixture
def backup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the backup home at a per-test directory."""
    home = tmp_path / "backup-home"
    monkeypatch.setenv("EAWF_HOME", str(home))
    return home


def _repo(tmp_path: Path, name: str = "demo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    (repo / "pyproject.toml").write_text(f"[project]\nname = '{name}'\n", encoding="utf-8")
    return repo


def _invoke(*argv: str) -> tuple[int, dict[str, Any]]:
    result = runner.invoke(app, ["--json", *argv])
    return result.exit_code, json.loads(result.output)


def _snapshots(home: Path) -> list[Path]:
    root = home / ".eawf" / "backups"
    return sorted(p for p in root.rglob("*") if p.is_dir()) if root.exists() else []


# ---- registration ------------------------------------------------------------


def test_quick_init_registers_the_repository_and_its_workspace(
    tmp_path: Path, registry_file: Path
) -> None:
    repo = _repo(tmp_path)
    code, envelope = _invoke("init", "--quick", "--target", str(repo))
    assert code == exit_codes.OK, envelope
    project_code = envelope["project_code"]
    assert envelope["registry"] == {
        "registry_path": str(registry_file),
        "repository": "registered",
        "workspace": "registered",
    }
    registry = read_registry(path=registry_file)
    assert registry.repos[project_code].path == str(repo.resolve())
    workspace = registry.workspaces[project_code]
    assert workspace.home_project_code == project_code
    assert workspace.member_project_codes == frozenset({project_code})

    code, listing = _invoke("workspace", "list")
    assert code == exit_codes.OK, listing
    assert listing["result"]["registry_path"] == str(registry_file)
    assert [row["key"] for row in listing["result"]["workspaces"]] == [project_code]


def test_init_registers_beside_rows_already_in_the_registry(
    tmp_path: Path, registry_file: Path
) -> None:
    other = RegistryRepoEntry(code="OTHER", path=str(tmp_path / "other"))
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text(Registry(repos={"OTHER": other}).model_dump_json(), encoding="utf-8")
    repo = _repo(tmp_path)
    code, envelope = _invoke("--no-input", "init", "--target", str(repo), "--project-code", "DEMO")
    assert code == exit_codes.OK, envelope
    registry = read_registry(path=registry_file)
    assert set(registry.repos) == {"OTHER", "DEMO"}
    assert set(registry.workspaces) == {"DEMO"}


def test_init_registers_in_an_explicit_registry_path(tmp_path: Path, registry_file: Path) -> None:
    explicit = tmp_path / "explicit" / "registry.json"
    repo = _repo(tmp_path)
    code, envelope = _invoke(
        "--no-input",
        "init",
        "--target",
        str(repo),
        "--project-code",
        "DEMO",
        "--registry-path",
        str(explicit),
    )
    assert code == exit_codes.OK, envelope
    assert envelope["registry"]["registry_path"] == str(explicit)
    assert "DEMO" in read_registry(path=explicit).repos
    assert not registry_file.exists()


def test_init_leaves_a_code_registered_elsewhere_alone(tmp_path: Path, registry_file: Path) -> None:
    elsewhere = str(tmp_path / "elsewhere")
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text(
        Registry(repos={"DEMO": RegistryRepoEntry(code="DEMO", path=elsewhere)}).model_dump_json(),
        encoding="utf-8",
    )
    repo = _repo(tmp_path)
    code, envelope = _invoke("--no-input", "init", "--target", str(repo), "--project-code", "DEMO")
    assert code == exit_codes.OK, envelope
    assert envelope["registry"]["repository"] == "code_taken"
    assert envelope["registry"]["workspace"] == "skipped"
    registry = read_registry(path=registry_file)
    assert registry.repos["DEMO"].path == elsewhere
    assert registry.workspaces == {}


def test_init_does_not_merge_into_an_existing_workspace(
    tmp_path: Path, registry_file: Path
) -> None:
    existing = WorkspaceRecord(
        key="DEMO",
        member_project_codes=frozenset({"DEMO", "PEER"}),
        home_project_code="PEER",
    )
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text(
        Registry(workspaces={"DEMO": existing}).model_dump_json(), encoding="utf-8"
    )
    repo = _repo(tmp_path)
    code, envelope = _invoke("--no-input", "init", "--target", str(repo), "--project-code", "DEMO")
    assert code == exit_codes.OK, envelope
    assert envelope["registry"]["repository"] == "registered"
    assert envelope["registry"]["workspace"] == WORKSPACE_ALREADY_REGISTERED
    assert read_registry(path=registry_file).workspaces["DEMO"] == existing


def test_init_refuses_an_unreadable_registry(tmp_path: Path, registry_file: Path) -> None:
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text("{not json", encoding="utf-8")
    repo = _repo(tmp_path)
    code, envelope = _invoke("--no-input", "init", "--target", str(repo), "--project-code", "DEMO")
    assert code == exit_codes.USER_ERROR
    assert "could not register it" in envelope["message"]
    assert "eawf repo register" in envelope["message"]
    assert registry_file.read_text(encoding="utf-8") == "{not json"


# ---- the tree's repository row --------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _git_repo(tmp_path: Path, *, commit: bool = True) -> Path:
    repo = _repo(tmp_path)
    _git(repo, "init", "-q", "-b", "main")
    if commit:
        _git(repo, "add", "pyproject.toml")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
             "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "start")  # fmt: skip
    return repo


def _tree_document(repo: Path) -> dict[str, Any]:
    authority = require_native_authority(repo / ".ea")
    assert authority.target is not None and authority.generation_id is not None
    path = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return read_document(path)


@pytest.fixture
def no_outer_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a hook's ``GIT_DIR`` from pointing git at the repository under test."""
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("registry_file", "no_outer_git")
def test_quick_init_in_a_git_repository_admits_its_repository_row(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    code, envelope = _invoke("init", "--quick", "--target", str(repo))
    assert code == exit_codes.OK, envelope
    project_code = envelope["project_code"]
    urn = f"eawf://{project_code}/{project_code}/{project_code}/repository/{project_code}"
    assert envelope["repository_row"] == {"outcome": "admitted", "urn": urn}
    document = _tree_document(repo)
    (row,) = document["repository"].values()
    assert row["key"] == project_code
    assert row["urn"] == urn
    assert row["head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert document["canonical_sequence"] == 1


@pytest.mark.usefixtures("registry_file", "no_outer_git")
def test_init_outside_git_admits_no_row_and_says_why(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    code, envelope = _invoke("--no-input", "init", "--target", str(repo), "--project-code", "DEMO")
    assert code == exit_codes.OK, envelope
    row = envelope["repository_row"]
    assert row["outcome"] == "not_a_git_repository"
    assert "not a git repository" in row["note"]
    assert "eawf repository create eawf://DEMO/DEMO/DEMO/repository/DEMO" in row["note"]
    assert _tree_document(repo).get("repository", {}) == {}


@pytest.mark.usefixtures("registry_file", "no_outer_git")
def test_init_outside_git_prints_the_note_in_text_mode(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = runner.invoke(
        app, ["--no-input", "init", "--target", str(repo), "--project-code", "DEMO"]
    )
    assert result.exit_code == exit_codes.OK, result.output
    assert "repository_row=not_a_git_repository" in result.output
    assert "note: the target is not a git repository" in result.output


@pytest.mark.usefixtures("registry_file", "no_outer_git")
def test_init_in_a_repository_with_no_commit_admits_no_row(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path, commit=False)
    code, envelope = _invoke("--no-input", "init", "--target", str(repo), "--project-code", "DEMO")
    assert code == exit_codes.OK, envelope
    assert envelope["repository_row"]["outcome"] == "no_commit"
    assert "no commit yet" in envelope["repository_row"]["note"]
    assert _tree_document(repo).get("repository", {}) == {}


@pytest.mark.usefixtures("registry_file", "no_outer_git")
def test_admitting_the_row_again_leaves_it_and_sends_nothing(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    code, envelope = _invoke("--no-input", "init", "--target", str(repo), "--project-code", "DEMO")
    assert code == exit_codes.OK, envelope
    before = _tree_document(repo)
    sent: list[object] = []

    again = admit_tree_repository(repo / ".ea", project_code="DEMO", submit=sent.append)

    assert again.outcome is RepositoryRowOutcome.ALREADY_ADMITTED
    assert sent == []
    assert _tree_document(repo) == before


@pytest.mark.usefixtures("registry_file", "no_outer_git")
def test_a_refused_row_exits_non_zero_naming_the_verb_that_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise TransactionRefusedError(
            code=TransactionRefusalCode.REVISION_CONFLICT,
            detail="the tree moved",
            entity_ref="eawf://DEMO/DEMO/DEMO/repository/DEMO",
            remediation="Retry.",
        )

    monkeypatch.setattr("eawf.platform.install.repository_row.admit_in_process", refuse)
    repo = _git_repo(tmp_path)
    code, envelope = _invoke("--no-input", "init", "--target", str(repo), "--project-code", "DEMO")
    assert code == exit_codes.STATE_CONFLICT, envelope
    assert "could not admit its repository row" in envelope["message"]
    assert "eawf repository create eawf://DEMO/DEMO/DEMO/repository/DEMO" in envelope["message"]


def test_the_row_urn_refuses_a_code_that_is_not_a_symbol() -> None:
    with pytest.raises(IdentityError):
        tree_repository_urn("demo")


def test_default_registry_path_honours_the_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "r.json"))
    assert default_registry_path() == tmp_path / "r.json"
    # An explicit home is the narrower seam, so it beats the environment.
    assert default_registry_path(home=tmp_path / "h") == tmp_path / "h" / ".eawf" / "registry.json"


def test_default_registry_path_falls_back_to_home_without_an_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    for unset in (None, ""):
        if unset is None:
            monkeypatch.delenv("EAWF_REGISTRY_PATH", raising=False)
        else:
            monkeypatch.setenv("EAWF_REGISTRY_PATH", unset)
        assert default_registry_path() == tmp_path / ".eawf" / "registry.json"


# ---- opt-in validates before it backs up ---------------------------------------


@pytest.fixture
def epoch1_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plain epoch-1 tree; returns its ``.ea`` directory."""
    ea = tmp_path / "repo" / ".ea"
    ea.mkdir(parents=True)
    (ea / "state.json").write_text(json.dumps({"schema_version": "1.0", "scope_kind": "repo"}))
    monkeypatch.setenv("EA_STATE", str(ea / "state.json"))
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "registry.json"))
    return ea


def test_opt_in_no_orphan_backup_for_an_empty_purpose(epoch1_tree: Path, backup_home: Path) -> None:
    from datetime import UTC, datetime

    with pytest.raises(ValidationError):
        declare_opt_in(
            epoch1_tree / "state.json",
            declared_by="alice",
            purpose="",
            note="n",
            when=datetime.now(UTC),
        )
    assert _snapshots(backup_home) == []
