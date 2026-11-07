"""Reflection scopes cover local sessions by working directory, and keep rows local.

- ``project`` covers the repository root and each git worktree, ``workspace`` adds every
  project in the machine-local registry, and ``user`` (spelled ``all_local`` too)
  covers every indexed session; ``milestone`` and ``track`` cover the project's own.
- An output directory git would commit refuses other projects' rows; the tree's local
  directory, a git-ignored directory and one outside every repository take them.

The repository is laid out by hand as git stores it, so no test here spawns git;
the command-line runs over a repository git built live in the contract tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from eawf.observability.reflect.report import ReportScope, parse_scope
from eawf.observability.reflect.scopes import (
    ForeignRowsRefusedError,
    OutputArea,
    SessionScope,
    resolve_session_scope,
)
from eawf.observability.telemetry.sources.session_history import working_dir_digest
from eawf.platform.registry import Registry, RegistryRepoEntry

_DIRECTORIES = ("repo", "worktree", "sibling", "elsewhere")


@dataclass(frozen=True)
class Layout:
    """A repository with one linked worktree, a registered sibling and a stray directory."""

    repo: Path
    worktree: Path
    sibling: Path
    elsewhere: Path

    @property
    def tree(self) -> Path:
        return self.repo / ".ea"

    def digest(self, name: str) -> str:
        return working_dir_digest(getattr(self, name))


def _write_registry(path: Path, repos: dict[str, Path]) -> None:
    entries = {code: RegistryRepoEntry(code=code, path=str(root)) for code, root in repos.items()}
    path.write_text(Registry(repos=entries).model_dump_json(), encoding="utf-8")


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Layout:
    """Return the layout, its repository ignoring ``ignored/`` and registered with the sibling."""
    base = tmp_path.resolve()
    repo = base / "repo"
    git = repo / ".git"
    for folder in ("objects", "refs", "worktrees/wt"):
        (git / folder).mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    worktree = base / "repo-wt"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {git / 'worktrees' / 'wt'}\n", encoding="utf-8")
    (git / "worktrees" / "wt" / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (git / "worktrees" / "wt" / "commondir").write_text("../..\n", encoding="utf-8")
    sibling = base / "sibling"
    sibling.mkdir()
    registry = base / "registry.json"
    _write_registry(registry, {"RFL": repo, "SIB": sibling})
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(registry))
    return Layout(repo=repo, worktree=worktree, sibling=sibling, elsewhere=base / "elsewhere")


_PROJECT = ("repo", "worktree")
_WORKSPACE = ("repo", "worktree", "sibling")


@pytest.mark.parametrize(
    ("value", "kind", "covered"),
    [
        ("project", "project", _PROJECT),
        ("workspace", "workspace", _WORKSPACE),
        ("user", "user", _DIRECTORIES),
        ("all_local", "user", _DIRECTORIES),
        ("milestone:MLS-0104", "milestone", _PROJECT),
        ("track:TRK-EAWF-CORE", "track", _PROJECT),
    ],
)
def test_scope_values(layout: Layout, value: str, kind: str, covered: tuple[str, ...]) -> None:
    """Each scope parses and covers exactly its directories, worktree labelled as its root."""
    scope = resolve_session_scope(parse_scope(value), project_root=layout.repo, label="RFL")

    assert parse_scope(value).kind == kind
    assert {name for name in _DIRECTORIES if scope.covers(layout.digest(name))} == set(covered)
    assert scope.own == {layout.digest(name) for name in _PROJECT}
    assert scope.labels[layout.digest("worktree")] == "RFL"
    if kind == "workspace":
        assert scope.labels[layout.digest("sibling")] == "SIB"
    assert scope.label == ("RFL" if kind in ("project", "milestone", "track") else kind)


def test_foreign_rows_stay_local(layout: Layout, tmp_path: Path) -> None:
    """The two foreign rows pass local, ignored and outside areas; a committed one refuses."""
    scope = resolve_session_scope(ReportScope(kind="user"), project_root=layout.repo, label="RFL")
    foreign = scope.foreign_rows(layout.digest(name) for name in _DIRECTORIES)
    assert foreign == 2

    for directory in (
        layout.tree / "local" / "reflect",
        layout.repo / "ignored" / "deep",
        tmp_path / "outside",
    ):
        area = OutputArea.of(directory, tree_root=layout.tree)
        assert not area.committed, directory
        area.admit(foreign)

    committed = OutputArea.of(layout.repo / "reports", tree_root=layout.tree)
    with pytest.raises(ForeignRowsRefusedError, match="refusing to write 2 session rows"):
        committed.admit(foreign)
    assert not (layout.repo / "reports").exists()


def test_a_committed_area_admits_a_write_with_no_foreign_rows(layout: Layout) -> None:
    """Boundary: zero foreign rows pass a committed area; one is refused and named."""
    area = OutputArea.of(layout.repo / "reports", tree_root=layout.tree)
    assert area.committed
    area.admit(0)
    with pytest.raises(ForeignRowsRefusedError, match="1 session rows") as caught:
        area.admit(1)
    assert caught.value.foreign_rows == 1


@pytest.mark.parametrize(
    ("relative", "committed"),
    [
        ("repo", True),
        ("repo/a/b/c", True),
        ("repo/.ea/local/reflect", False),
        ("repo/ignored", False),
        ("repo/ignored/not/yet/made", False),
        ("repo-wt/reports", True),
        ("outside", False),
    ],
    ids=["root", "nested", "local", "ignored", "ignored-nested", "worktree", "outside"],
)
def test_output_area_asks_git_whether_a_directory_is_committed(
    layout: Layout, relative: str, committed: bool
) -> None:
    """Boundary: the area is committed only inside a work tree, unignored, outside .ea/local."""
    area = OutputArea.of(layout.repo.parent / relative, tree_root=layout.tree)
    assert area.committed is committed
    area.admit(0 if committed else 5)


def test_foreign_rows_count_unknown_directories_and_skip_own() -> None:
    """Boundary: empty counts zero; a session with no directory counts as foreign."""
    scope = SessionScope(own=frozenset({"cwd-a"}), covered=None, labels={}, label="user")
    assert scope.foreign_rows([]) == 0
    assert scope.foreign_rows(["cwd-a"]) == 0
    assert scope.foreign_rows(["cwd-a", "cwd-b", None]) == 2
    assert scope.covers(None)


def test_a_project_scope_never_covers_an_unknown_directory(layout: Layout) -> None:
    """Error path: a session with no recorded directory is outside every named project."""
    scope = resolve_session_scope(ReportScope(), project_root=layout.repo, label="RFL")
    assert not scope.covers(None)
    assert not scope.covers(layout.digest("sibling"))


def test_a_directory_git_does_not_track_stands_for_itself(tmp_path: Path) -> None:
    """Boundary: a project root with no .git covers that root alone."""
    scope = resolve_session_scope(ReportScope(), project_root=tmp_path, label="RFL")
    assert scope.own == {working_dir_digest(tmp_path)}


def test_scopes_from_a_worktree_cover_the_main_checkout(layout: Layout) -> None:
    """Boundary: run from the linked worktree, project still covers the main checkout."""
    scope = resolve_session_scope(ReportScope(), project_root=layout.worktree, label="RFL")
    assert scope.own == {layout.digest(name) for name in _PROJECT}


def test_workspace_without_a_registry_covers_this_project_alone(
    layout: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a missing registry leaves the workspace scope this project's own."""
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "absent.json"))
    scope = resolve_session_scope(
        ReportScope(kind="workspace"), project_root=layout.repo, label="RFL"
    )
    assert scope.covered == frozenset(layout.digest(name) for name in _PROJECT)
    assert scope.label == "workspace"


def test_a_registered_project_that_is_gone_stands_for_its_path(
    layout: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a registered path that no longer exists still names its sessions."""
    gone = tmp_path / "gone"
    registry = tmp_path / "gone-registry.json"
    _write_registry(registry, {"GON": gone})
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(registry))
    scope = resolve_session_scope(
        ReportScope(kind="workspace"), project_root=layout.repo, label="RFL"
    )
    assert scope.labels[working_dir_digest(gone)] == "GON"


@pytest.mark.parametrize(
    "value", ["workspace:X", "user:", "ALL_LOCAL", "users", "all-local"], ids=str
)
def test_a_keyed_or_misspelled_unkeyed_scope_is_refused(value: str) -> None:
    """Error path: the unkeyed scopes take no key and parse only as spelled."""
    with pytest.raises(ValueError, match="workspace, user"):
        parse_scope(value)
