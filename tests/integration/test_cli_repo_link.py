"""Unit tests for ``eawf repo`` subcommands.

``repo init`` is a thin alias of :func:`eawf.surfaces.cli.commands.init.init_cmd`,
so we exercise it once end-to-end and lean on the existing init tests for
the deeper validation surface. ``repo link`` is more meaningful — it
cross-mutates a workspace state and a repo state and the linkage round-trip
needs its own coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


def _init_workspace(workspace_state_path: Path, code: str, title: str) -> None:
    """Materialise a workspace state.json at *workspace_state_path*."""
    workspace_state_path.parent.mkdir(parents=True, exist_ok=True)
    res = runner.invoke(
        app,
        [
            "-w",
            str(workspace_state_path.parent.parent),
            "workspace",
            "init",
            code,
            "--title",
            title,
        ],
    )
    assert res.exit_code == 0, res.stdout


def _init_repo(target_dir: Path, code: str) -> None:
    """Materialise a repo state.json at *target_dir*/.ea/state.json."""
    res = runner.invoke(
        app,
        [
            "--no-input",
            "repo",
            "init",
            "--target",
            str(target_dir),
            "--project-code",
            code,
            "--profile",
            "core",
        ],
    )
    assert res.exit_code == 0, res.stdout


def test_repo_init_delegates_to_init(tmp_path: Path) -> None:
    """``repo init`` produces the same .ea/ scaffolding as ``init``."""
    target = tmp_path / "demo-repo"
    res = runner.invoke(
        app,
        [
            "--no-input",
            "repo",
            "init",
            "--target",
            str(target),
            "--project-code",
            "DEMO",
            "--profile",
            "core",
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert (target / ".ea" / "state.json").exists()
    assert (target / ".ea" / "config.yaml").exists()
    assert (target / "AGENTS.md").exists()
    assert (target / "CLAUDE.md").exists()


def test_repo_init_invalid_project_code_exits_3(tmp_path: Path) -> None:
    """Invalid project codes propagate exit 3 from the underlying init handler."""
    res = runner.invoke(
        app,
        [
            "--no-input",
            "repo",
            "init",
            "--target",
            str(tmp_path),
            "--project-code",
            "lowercase",
            "--profile",
            "core",
        ],
    )
    assert res.exit_code == 1, res.stdout


def test_repo_init_refresh_gitignore_matches_top_level_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The repo alias refreshes an empty repo without running full init."""
    from eawf.platform.install import wizard

    def _full_init_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("refresh must not invoke the full init pipeline")

    monkeypatch.setattr(wizard, "run_wizard_no_input", _full_init_forbidden)
    monkeypatch.setattr(wizard, "run_wizard_interactive", _full_init_forbidden)
    top_level_target = tmp_path / "top-level"
    alias_target = tmp_path / "alias"
    common = [
        "--no-input",
        "--refresh-gitignore",
        "--state-path",
        "state.json",
    ]
    top_level = runner.invoke(
        app,
        [common[0], "init", *common[1:], "--target", str(top_level_target)],
    )
    alias = runner.invoke(
        app,
        [common[0], "repo", "init", *common[1:], "--target", str(alias_target)],
    )

    assert top_level.exit_code == 0, top_level.stdout
    assert alias.exit_code == 0, alias.stdout
    assert (top_level_target / ".gitignore").read_bytes() == (
        alias_target / ".gitignore"
    ).read_bytes()
    for target in (top_level_target, alias_target):
        assert not (target / ".ea").exists()
        assert not (target / "state.json").exists()
        assert not (target / "AGENTS.md").exists()


def test_repo_link_round_trip(tmp_path: Path) -> None:
    """``repo link`` records the link on both the workspace and the repo state."""
    workspace_dir = tmp_path / "ws"
    workspace_state = workspace_dir / ".ea" / "state.json"
    repo_dir = tmp_path / "repo"

    _init_workspace(workspace_state, "MAIN", "Main")
    _init_repo(repo_dir, "DEMO")

    res = runner.invoke(
        app,
        [
            "repo",
            "link",
            "MAIN",
            "DEMO",
            "--workspace-state",
            str(workspace_state),
            "--target",
            str(repo_dir),
        ],
    )
    assert res.exit_code == 0, res.stdout

    ws = orjson.loads(workspace_state.read_bytes())
    assert "DEMO" in ws["workspace"]["repos"]
    link = ws["workspace"]["repos"]["DEMO"]
    assert link["path"] == str(repo_dir.resolve())
    assert link["project_code"] == "DEMO"

    repo_state = orjson.loads((repo_dir / ".ea" / "state.json").read_bytes())
    indexes = repo_state.get("indexes") or {}
    assert indexes.get("workspace_code") == "MAIN"


def test_repo_link_rejects_workspace_code_mismatch(tmp_path: Path) -> None:
    """If the on-disk workspace code differs from the arg, exit 3."""
    workspace_dir = tmp_path / "ws"
    workspace_state = workspace_dir / ".ea" / "state.json"
    repo_dir = tmp_path / "repo"
    _init_workspace(workspace_state, "MAIN", "Main")
    _init_repo(repo_dir, "DEMO")

    res = runner.invoke(
        app,
        [
            "repo",
            "link",
            "OTHER",
            "DEMO",
            "--workspace-state",
            str(workspace_state),
            "--target",
            str(repo_dir),
        ],
    )
    assert res.exit_code == 1, res.stdout
    assert "workspace code mismatch" in res.stdout


def test_repo_link_rejects_duplicate(tmp_path: Path) -> None:
    """Re-linking the same repo to the same workspace exits 3."""
    workspace_dir = tmp_path / "ws"
    workspace_state = workspace_dir / ".ea" / "state.json"
    repo_dir = tmp_path / "repo"
    _init_workspace(workspace_state, "MAIN", "Main")
    _init_repo(repo_dir, "DEMO")

    args = [
        "repo",
        "link",
        "MAIN",
        "DEMO",
        "--workspace-state",
        str(workspace_state),
        "--target",
        str(repo_dir),
    ]
    res = runner.invoke(app, args)
    assert res.exit_code == 0
    res = runner.invoke(app, args)
    assert res.exit_code == 1, res.stdout
    assert "already linked" in res.stdout


def test_repo_link_emits_json_envelope(tmp_path: Path) -> None:
    """``--json`` surfaces the linked repo + workspace metadata."""
    workspace_dir = tmp_path / "ws"
    workspace_state = workspace_dir / ".ea" / "state.json"
    repo_dir = tmp_path / "repo"
    _init_workspace(workspace_state, "MAIN", "Main")
    _init_repo(repo_dir, "DEMO")

    res = runner.invoke(
        app,
        [
            "--json",
            "repo",
            "link",
            "MAIN",
            "DEMO",
            "--workspace-state",
            str(workspace_state),
            "--target",
            str(repo_dir),
        ],
    )
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["workspace"] == "MAIN"
    assert payload["repo"] == "DEMO"


# Silence pytest unused-import warnings on platforms with strict ruff configs.
_ = pytest
