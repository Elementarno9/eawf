"""Unit tests for ``eawf workspace`` subcommands.

The state.json round-trip is covered end-to-end here because the workspace
sub-app is a thin layer over the existing :func:`state_transaction` and
:func:`atomic_write_json_locked` helpers — there is little to test in
isolation. Each test sets ``EA_STATE`` to a tmp path so the resolver
returns a deterministic location.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


@pytest.fixture
def workspace_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a temp dir with EA_STATE pointing inside it (file does not yet exist)."""
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    yield tmp_path


def _read_state(workspace: Path) -> dict[str, object]:
    state_path = workspace / ".ea" / "state.json"
    return orjson.loads(state_path.read_bytes())  # type: ignore[no-any-return]


def _init_workspace(workspace: Path, code: str = "MAIN", title: str = "Main") -> None:
    res = runner.invoke(app, ["workspace", "init", code, "--title", title])
    assert res.exit_code == 0, res.stdout


# ---- workspace init ---------------------------------------------------------


def test_workspace_init_creates_workspace_state_section(workspace_state: Path) -> None:
    """``workspace init`` writes a workspace-scoped state.json."""
    res = runner.invoke(
        app,
        ["--json", "workspace", "init", "MAIN", "--title", "Main Workspace"],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["workspace"] == "MAIN"
    state = _read_state(workspace_state)
    assert state["scope_kind"] == "workspace"
    assert state["project"] is None
    assert isinstance(state["workspace"], dict)
    assert state["workspace"]["code"] == "MAIN"  # type: ignore[index]
    assert state["workspace"]["title"] == "Main Workspace"  # type: ignore[index]
    assert state["workspace"]["repos"] == {}  # type: ignore[index]
    assert state["urn"] == "urn:eawf:v1:workspace:MAIN"


def test_workspace_init_refuses_overwrite_without_force(workspace_state: Path) -> None:
    """A second ``workspace init`` against the same path requires ``--force``."""
    _init_workspace(workspace_state)
    res = runner.invoke(app, ["workspace", "init", "OTHER", "--title", "Other"])
    assert res.exit_code == 3, res.stdout
    assert "already exists" in res.stdout
    state = _read_state(workspace_state)
    # State unchanged.
    assert state["workspace"]["code"] == "MAIN"  # type: ignore[index]

    # With --force, the workspace is replaced.
    res = runner.invoke(app, ["workspace", "init", "OTHER", "--title", "Other", "--force"])
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace_state)
    assert state["workspace"]["code"] == "OTHER"  # type: ignore[index]


def test_workspace_init_invalid_code_exits_3(workspace_state: Path) -> None:
    """Lowercase or invalid codes fail the regex with exit 3."""
    res = runner.invoke(app, ["workspace", "init", "lower", "--title", "x"])
    assert res.exit_code == 3, res.stdout
    assert "invalid workspace code" in res.stdout


# ---- workspace add-repo -----------------------------------------------------


def test_workspace_add_repo_appends_link(workspace_state: Path, tmp_path: Path) -> None:
    """``workspace add-repo`` appends a ``WorkspaceRepoRef`` to the index."""
    _init_workspace(workspace_state)
    repo_dir = tmp_path / "repo-A"
    repo_dir.mkdir()

    res = runner.invoke(
        app,
        [
            "workspace",
            "add-repo",
            "REPO_A",
            "--path",
            str(repo_dir),
            "--project-code",
            "REPO_A",
            "--title",
            "Repo A",
        ],
    )
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace_state)
    repos = state["workspace"]["repos"]  # type: ignore[index]
    assert "REPO_A" in repos
    assert repos["REPO_A"]["path"] == str(repo_dir.resolve())
    assert repos["REPO_A"]["title"] == "Repo A"
    assert repos["REPO_A"]["project_code"] == "REPO_A"
    assert repos["REPO_A"]["status"] == "active"


def test_workspace_add_repo_rejects_duplicate(workspace_state: Path, tmp_path: Path) -> None:
    """Re-adding the same code under the same workspace exits 3."""
    _init_workspace(workspace_state)
    repo_dir = tmp_path / "repo-B"
    repo_dir.mkdir()
    args = [
        "workspace",
        "add-repo",
        "REPO_B",
        "--path",
        str(repo_dir),
        "--project-code",
        "REPO_B",
        "--title",
        "B",
    ]
    res = runner.invoke(app, args)
    assert res.exit_code == 0
    res = runner.invoke(app, args)
    assert res.exit_code == 3, res.stdout
    assert "already linked" in res.stdout


# ---- workspace remove-repo --------------------------------------------------


def test_workspace_remove_repo_drops_entry(workspace_state: Path, tmp_path: Path) -> None:
    """``workspace remove-repo`` deletes the link from the index."""
    _init_workspace(workspace_state)
    repo_dir = tmp_path / "repo-C"
    repo_dir.mkdir()
    res = runner.invoke(
        app,
        [
            "workspace",
            "add-repo",
            "REPO_C",
            "--path",
            str(repo_dir),
            "--project-code",
            "REPO_C",
            "--title",
            "C",
        ],
    )
    assert res.exit_code == 0
    res = runner.invoke(app, ["workspace", "remove-repo", "REPO_C"])
    assert res.exit_code == 0, res.stdout
    state = _read_state(workspace_state)
    assert "REPO_C" not in state["workspace"]["repos"]  # type: ignore[index]


def test_workspace_remove_repo_unknown_exits_2(
    workspace_state: Path,
) -> None:
    """Removing a non-existent code yields ``NotFound`` (exit 2)."""
    _init_workspace(workspace_state)
    res = runner.invoke(app, ["workspace", "remove-repo", "MISSING"])
    assert res.exit_code == 2, res.stdout
    assert "not linked" in res.stdout


# ---- workspace validate -----------------------------------------------------


def test_workspace_validate_flags_missing_repo_path(workspace_state: Path, tmp_path: Path) -> None:
    """``validate`` reports ``missing_path`` when the linked dir vanished."""
    _init_workspace(workspace_state)
    repo_dir = tmp_path / "repo-D"
    repo_dir.mkdir()
    res = runner.invoke(
        app,
        [
            "workspace",
            "add-repo",
            "REPO_D",
            "--path",
            str(repo_dir),
            "--project-code",
            "REPO_D",
            "--title",
            "D",
        ],
    )
    assert res.exit_code == 0

    # Now remove the repo dir to trigger the missing_path branch.
    repo_dir.rmdir()
    res = runner.invoke(app, ["--json", "workspace", "validate"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["total"] == 1
    assert payload["ok"] == 0
    findings = payload["findings"]
    assert len(findings) == 1
    assert findings[0]["repo"] == "REPO_D"
    assert findings[0]["status"] == "missing_path"


def test_workspace_validate_flags_missing_state(workspace_state: Path, tmp_path: Path) -> None:
    """``validate`` reports ``missing_state`` when path lacks ``.ea/state.json``."""
    _init_workspace(workspace_state)
    repo_dir = tmp_path / "repo-E"
    repo_dir.mkdir()
    runner.invoke(
        app,
        [
            "workspace",
            "add-repo",
            "REPO_E",
            "--path",
            str(repo_dir),
            "--project-code",
            "REPO_E",
            "--title",
            "E",
        ],
    )
    res = runner.invoke(app, ["--json", "workspace", "validate"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["findings"][0]["status"] == "missing_state"


# ---- workspace status -------------------------------------------------------


def test_workspace_status_lists_links(workspace_state: Path, tmp_path: Path) -> None:
    """``status`` enumerates each link in the JSON envelope."""
    _init_workspace(workspace_state, code="MULTI", title="Multi-repo")
    for code in ("R1", "R2"):
        repo_dir = tmp_path / f"repo-{code}"
        repo_dir.mkdir()
        runner.invoke(
            app,
            [
                "workspace",
                "add-repo",
                code,
                "--path",
                str(repo_dir),
                "--project-code",
                code,
                "--title",
                f"Repo {code}",
            ],
        )
    res = runner.invoke(app, ["--json", "workspace", "status"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["workspace"] == "MULTI"
    assert payload["title"] == "Multi-repo"
    assert {row["code"] for row in payload["repos"]} == {"R1", "R2"}
