"""Integration tests for ``eawf clone-repo``.

The happy-path tests stand up a local bare git repository in ``tmp_path`` so
the clone is fully offline — no network, no auth. The handler is then
invoked via :class:`typer.testing.CliRunner` and the resulting tree is
asserted to carry a freshly-initialised ``.ea/``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for clone-repo integration tests",
)


def _bootstrap_bare_repo(workdir: Path) -> Path:
    """Create a small commit and clone it into a bare repo. Returns the bare path."""
    src = workdir / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, check=True)
    subprocess.run(
        ["git", "config", "user.email", "ci@example.com"],
        cwd=src,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "ci"], cwd=src, check=True)
    (src / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=src, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=src, check=True)
    bare = workdir / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(bare)],
        check=True,
    )
    return bare


def test_cli_clone_repo_runs_init(tmp_path: Path) -> None:
    """A successful clone-repo lays down ``.ea/`` and ``AGENTS.md``."""
    bare = _bootstrap_bare_repo(tmp_path)
    target = tmp_path / "clone"

    res = runner.invoke(
        app,
        [
            "clone-repo",
            f"file://{bare}",
            "--target",
            str(target),
            "--project-code",
            "DEMO",
            "--profile",
            "core",
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert (target / "README.md").exists(), "git clone payload should be present"
    assert (target / ".ea" / "state.json").exists()
    assert (target / ".ea" / "config.yaml").exists()
    assert (target / "AGENTS.md").exists()
    assert (target / "CLAUDE.md").exists()


def test_cli_clone_repo_rejects_non_url(tmp_path: Path) -> None:
    """A non-URL argument exits 3 (InvalidInput) before touching disk."""
    target = tmp_path / "clone"
    res = runner.invoke(
        app,
        [
            "clone-repo",
            "not-a-url",
            "--target",
            str(target),
            "--project-code",
            "DEMO",
        ],
    )
    assert res.exit_code == 1, res.stdout
    assert "not a git URL" in res.stdout
    assert not target.exists()


def test_cli_clone_repo_propagates_branch_flag(tmp_path: Path) -> None:
    """``--branch`` is forwarded verbatim to the underlying ``git clone``."""
    target = tmp_path / "clone"
    captured: dict[str, list[str]] = {}

    class _FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(args: list[str], **_kwargs: object) -> _FakeResult:
        captured["args"] = list(args)
        target_dir = Path(args[-1])
        target_dir.mkdir(parents=True, exist_ok=True)
        # Touch a marker file so init's project-code derivation has
        # something to attach to.
        (target_dir / "marker").write_text("x", encoding="utf-8")
        return _FakeResult()

    with mock.patch(
        "eawf.cli.commands.clone_repo.subprocess.run",
        side_effect=_fake_run,
    ):
        res = runner.invoke(
            app,
            [
                "clone-repo",
                "file:///tmp/whatever.git",
                "--target",
                str(target),
                "--project-code",
                "DEMO",
                "--branch",
                "release/v1",
                "--depth",
                "1",
                "--profile",
                "core",
            ],
        )
    assert res.exit_code == 0, res.stdout
    assert captured["args"][:2] == ["git", "clone"]
    assert "--branch" in captured["args"]
    assert "release/v1" in captured["args"]
    assert "--depth" in captured["args"]
    assert "1" in captured["args"]


def test_cli_clone_repo_requires_project_code_when_basename_invalid(
    tmp_path: Path,
) -> None:
    """When the target basename starts with a digit, exit 3 with a hint."""
    target = tmp_path / "1bad-target"
    res = runner.invoke(
        app,
        ["clone-repo", "file:///tmp/whatever.git", "--target", str(target)],
    )
    assert res.exit_code == 1, res.stdout
    assert "pass --project-code" in res.stdout
    assert not target.exists()


def test_cli_clone_repo_translates_git_failure_to_exit_5(tmp_path: Path) -> None:
    """A git failure with a transient stderr maps to exit 5 (LockConflict)."""
    target = tmp_path / "clone"

    class _FakeResult:
        returncode = 128
        stdout = ""
        stderr = "fatal: unable to access 'https://example': Could not resolve host"

    with mock.patch(
        "eawf.cli.commands.clone_repo.subprocess.run",
        return_value=_FakeResult(),
    ):
        res = runner.invoke(
            app,
            [
                "clone-repo",
                "https://example.com/missing.git",
                "--target",
                str(target),
                "--project-code",
                "DEMO",
            ],
        )
    assert res.exit_code == 3, res.stdout
