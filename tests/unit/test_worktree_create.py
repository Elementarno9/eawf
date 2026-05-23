"""Unit tests for :func:`eawf.worktree.create.create_worktree`.

The tests stand up a small ``git init``-ed tmp repo, seed a minimal
:class:`State` with one CLAIMED wave, then exercise the create-time
guards (branch source, path computation, claim allocation, error paths).

Each test is self-contained: no shared fixture, no shared state.json on
disk — :func:`create_worktree` is a pure-functional mutator over the
in-memory state, so we can assert without round-tripping to disk.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.cli import errors as cli_errors
from eawf.state.enums import (
    PhaseStatus,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
)
from eawf.state.models import (
    CurrentPointers,
    Iter,
    Phase,
    Project,
    State,
    Wave,
)
from eawf.worktree.create import create_worktree

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is required for worktree integration tests",
)


_DT = datetime(2026, 5, 9, tzinfo=UTC)


def _make_repo(workdir: Path, *, branch: str = "feature/eawf-v0.1") -> Path:
    """Initialise a git repo with one commit and a feature branch.

    Returns the repo root. The repo starts on ``main`` with one commit
    and then creates+checks out *branch*. Caller can then point
    :func:`create_worktree` at the repo via ``repo_root``.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=workdir, check=True)
    (workdir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=workdir, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=workdir, check=True)
    return workdir


def _claimed_state(*, wave_id: str = "P05-I01-W01") -> State:
    """Build a minimal :class:`State` with one CLAIMED wave."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:DEMO",
            "updated_at": _DT.isoformat(),
            "project": Project(
                code="DEMO",
                slug="demo",
                title="Demo",
                description=None,
                domains=["test"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:DEMO",
            ).model_dump(mode="json"),
            "current": CurrentPointers().model_dump(mode="json"),
            "workspace": None,
            "phases": {
                "P05": Phase(
                    id="P05",
                    scope_id="DEMO",
                    title="Phase 5",
                    status=PhaseStatus.ACTIVE,
                    iter_ids=["P05-I01"],
                    outcome_ids=[],
                    opened_at=_DT,
                ).model_dump(mode="json"),
            },
            "iters": {
                "P05-I01": Iter(
                    id="P05-I01",
                    phase_id="P05",
                    title="Iter 1",
                    status="active",  # type: ignore[arg-type]
                    wave_ids=[wave_id],
                    opened_at=_DT,
                ).model_dump(mode="json"),
            },
            "waves": {
                wave_id: Wave(
                    id=wave_id,
                    iter_id="P05-I01",
                    title="W1",
                    status=WaveStatus.CLAIMED,
                    deps=[],
                    file_scopes=["src/eawf/worktree/"],
                    claim_session_id="SES-001",
                    opened_at=_DT,
                ).model_dump(mode="json"),
            },
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def test_create_default_path_under_dot_ea_worktrees(tmp_path: Path) -> None:
    """Default path resolves to ``<repo>/.ea/worktrees/<suffix>/``."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    assert record.path == ".ea/worktrees/p05-w01"
    assert record.branch == "feature/eawf-v0.1-p05-w01"
    assert record.base_branch == "feature/eawf-v0.1"
    assert record.status.value == "active"


def test_create_records_worktree_record(tmp_path: Path) -> None:
    """``state.worktrees`` is materialised with the new record."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    assert state.worktrees is not None
    assert record.id in state.worktrees
    assert state.worktrees[record.id].wave_id == "P05-I01-W01"


def test_create_stamps_wave_worktree_id(tmp_path: Path) -> None:
    """The wave's ``worktree_id`` field carries the new record id."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    assert state.waves["P05-I01-W01"].worktree_id == record.id


def test_create_refuses_main_branch(tmp_path: Path) -> None:
    """HEAD on ``main`` (the default_branch) is refused without --base."""
    workdir = tmp_path / "repo"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=workdir, check=True)
    (workdir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=workdir, check=True)
    # Stay on main — do NOT create a feature branch.
    state = _claimed_state()
    with pytest.raises(cli_errors.UserError) as exc_info:
        create_worktree(state, repo_root=workdir, wave_id="P05-I01-W01")
    assert "refuses to branch from" in str(exc_info.value)


def test_create_explicit_base_overrides_main_check(tmp_path: Path) -> None:
    """An explicit ``--base main`` (via ``explicit_base=True``) succeeds."""
    workdir = tmp_path / "repo"
    workdir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.email", "ci@example.com"], cwd=workdir, check=True)
    subprocess.run(["git", "config", "user.name", "ci"], cwd=workdir, check=True)
    (workdir / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=workdir, check=True)
    state = _claimed_state()
    record = create_worktree(
        state,
        repo_root=workdir,
        wave_id="P05-I01-W01",
        base="main",
        explicit_base=True,
    )
    assert record.base_branch == "main"


def test_create_refuses_detached_head(tmp_path: Path) -> None:
    """Detached HEAD -> :class:`InvalidInput`."""
    repo = _make_repo(tmp_path / "repo")
    # Detach by checking out the commit sha directly.
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", sha], check=True)
    state = _claimed_state()
    with pytest.raises(cli_errors.UserError) as exc_info:
        create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    assert "non-detached HEAD" in str(exc_info.value)


def test_create_refuses_path_outside_repo(tmp_path: Path) -> None:
    """A ``--path`` outside the repo root -> :class:`InvalidInput`."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    bogus = tmp_path / "elsewhere" / "wt"
    with pytest.raises(cli_errors.UserError) as exc_info:
        create_worktree(state, repo_root=repo, wave_id="P05-I01-W01", path=bogus)
    assert "outside repo root" in str(exc_info.value)


def test_create_refuses_existing_branch(tmp_path: Path) -> None:
    """A branch that already exists locally -> :class:`InvalidInput`."""
    repo = _make_repo(tmp_path / "repo")
    # Pre-create the would-be default branch.
    subprocess.run(
        ["git", "-C", str(repo), "branch", "feature/eawf-v0.1-p05-w01"],
        check=True,
    )
    state = _claimed_state()
    with pytest.raises(cli_errors.UserError) as exc_info:
        create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    assert "already exists locally" in str(exc_info.value)


def test_create_missing_wave_raises_not_found(tmp_path: Path) -> None:
    """Unknown wave id -> :class:`NotFound`."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    with pytest.raises(cli_errors.UserError):
        create_worktree(state, repo_root=repo, wave_id="P99-I99-W99")


def test_create_force_overwrites_empty_dir(tmp_path: Path) -> None:
    """Pre-existing empty target dir + ``force=True`` succeeds."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    pre = repo / ".ea" / "worktrees" / "p05-w01"
    pre.mkdir(parents=True)
    record = create_worktree(state, repo_root=repo, wave_id="P05-I01-W01", force=True)
    assert record.path == ".ea/worktrees/p05-w01"


def test_create_refuses_invalid_wave_id_regex(tmp_path: Path) -> None:
    """Wave id failing the regex -> :class:`InvalidInput`."""
    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    with pytest.raises(cli_errors.UserError):
        create_worktree(state, repo_root=repo, wave_id="not-a-wave")


def test_list_worktrees_git_present_cross_check_post_relative_path(
    tmp_path: Path,
) -> None:
    """``list_worktrees`` must combine ``record.path`` (repo-relative) with
    ``repo_root`` before comparing against ``git worktree list --porcelain``
    output (which emits absolute paths). Regression for the
    path-relativization audit fix in P08."""
    from eawf.worktree import list_worktrees

    repo = _make_repo(tmp_path / "repo")
    state = _claimed_state()
    create_worktree(state, repo_root=repo, wave_id="P05-I01-W01")
    rows = list(list_worktrees(state, repo_root=repo))
    assert len(rows) == 1
    assert rows[0].git_present is True
