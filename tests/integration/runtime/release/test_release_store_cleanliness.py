"""A dirty release store never reds the release tree check.

The release verbs append to ``.ea/store/release.jsonl`` and
``.ea/store/release_record.jsonl`` while a release is in flight, so a
tree check that counted them would red the very sweep whose verbs wrote
them. Both tree checks -- the ``tree_cleanliness`` probe and the
``release tag`` refusal -- set exactly those two stores aside, and any
other uncommitted path still reds both.

Each case drives a real git repository under ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalStatus,
)
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.release import _dirty_paths
from eawf.workflow.verify.release_probes import (
    TagPreflightInputs,
    build_tag_probes,
    release_tree_status,
)
from tests._release_helpers import dev1_config

pytestmark = pytest.mark.integration

RELEASE_STORES = (".ea/store/release.jsonl", ".ea/store/release_record.jsonl")

runner = CliRunner()


def _git(repo: Path, *args: str) -> str:
    """Run one git command inside *repo* and return its stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True, timeout=30
    ).stdout


def _write(repo: Path, relative: str, text: str) -> None:
    """Write *text* to *relative* under *repo*, creating its parents."""
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Return a clean repository that already tracks the record store."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "--quiet", "--initial-branch", "main")
    _git(path, "config", "user.name", "EAWF Test")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "commit.gpgSign", "false")
    _git(path, "config", "core.hooksPath", ".git/hooks")
    _write(path, "src/module.py", "VALUE = 1\n")
    _write(path, ".ea/store/release_record.jsonl", '{"row": 1}\n')
    _git(path, "add", "--all")
    _git(path, "commit", "--quiet", "--message", "init")
    return path


def dirty(repo: Path, relative: str) -> None:
    """Make *relative* show up in ``git status``: modified when tracked, else new."""
    path = repo / relative
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    _write(repo, relative, f'{existing}{{"row": 2}}\n')


def tree_row(repo: Path) -> ReleaseSignalStatus:
    """Return the ``tree_cleanliness`` verdict for *repo*."""
    probe = build_tag_probes(
        TagPreflightInputs(
            repo_root=repo,
            version="0.7.0.dev1",
            tag="v0.7.0.dev1",
            remote="origin",
            package_version="0.7.0.dev1",
        )
    )[ReleaseSignalName.TREE_CLEANLINESS]
    context = ReleaseSignalContext(dev1_config(), ReleaseSignalName.TREE_CLEANLINESS, None)
    return probe(context).status


# --- the probe --------------------------------------------------------------


@pytest.mark.parametrize("store", RELEASE_STORES)
def test_tree_cleanliness_passes_with_a_dirty_release_store(repo: Path, store: str) -> None:
    """An untracked ledger or a modified record store leaves the row green."""
    dirty(repo, store)
    assert _git(repo, "status", "--porcelain", "--untracked-files=all").strip()

    assert tree_row(repo) is ReleaseSignalStatus.PASS


def test_tree_cleanliness_passes_with_both_stores_dirty(repo: Path) -> None:
    """The two stores together are still nothing the release tree carries."""
    for store in RELEASE_STORES:
        dirty(repo, store)

    assert tree_row(repo) is ReleaseSignalStatus.PASS
    assert release_tree_status(repo).stdout == ""


@pytest.mark.parametrize("store", RELEASE_STORES)
def test_tree_cleanliness_reds_a_dirty_source_beside_a_dirty_store(repo: Path, store: str) -> None:
    """Setting the store aside never hides a real uncommitted change."""
    dirty(repo, store)
    _write(repo, "src/module.py", "VALUE = 2\n")

    assert tree_row(repo) is ReleaseSignalStatus.FAIL
    assert release_tree_status(repo).stdout.splitlines() == [" M src/module.py"]


@pytest.mark.parametrize(
    "relative",
    [".ea/store/audit.jsonl", "release.jsonl", ".ea/store/archive/release.jsonl"],
)
def test_tree_cleanliness_reds_a_store_the_release_verbs_do_not_own(
    repo: Path, relative: str
) -> None:
    """Only the release stores themselves are set aside."""
    dirty(repo, relative)

    assert tree_row(repo) is ReleaseSignalStatus.FAIL


def test_tree_cleanliness_reds_when_git_cannot_report(tmp_path: Path) -> None:
    """A directory git does not know is a failed check, not a clean one."""
    assert tree_row(tmp_path) is ReleaseSignalStatus.FAIL


# --- the CLI tree check -----------------------------------------------------


@pytest.mark.parametrize("store", RELEASE_STORES)
def test_dirty_paths_sets_the_release_store_aside(repo: Path, store: str) -> None:
    """The CLI check reads the same filtered status the probe does."""
    dirty(repo, store)

    assert _dirty_paths(repo) == ()


def test_dirty_paths_lists_each_untracked_file(repo: Path) -> None:
    """An untracked directory is listed file by file, never folded."""
    _write(repo, "notes/one.txt", "1\n")
    _write(repo, "notes/two.txt", "2\n")

    assert _dirty_paths(repo) == ("?? notes/one.txt", "?? notes/two.txt")


def test_dirty_paths_raises_when_git_cannot_report(tmp_path: Path) -> None:
    """A failing git query is surfaced, not read as a clean tree."""
    with pytest.raises(subprocess.CalledProcessError):
        _dirty_paths(tmp_path)


@pytest.mark.parametrize("store", RELEASE_STORES)
def test_release_tag_admits_a_dirty_release_store(
    repo: Path, store: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``release tag`` is not refused over a store the release verbs wrote."""
    dirty(repo, store)
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["release", "tag", "9.9.9"])

    assert result.exit_code == 0, result.stdout
    assert "v9.9.9" in _git(repo, "tag", "--list")


def test_release_tag_refuses_a_dirty_source_beside_a_dirty_store(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other dirty path still refuses the tag, and only it is counted."""
    for store in RELEASE_STORES:
        dirty(repo, store)
    _write(repo, "src/module.py", "VALUE = 2\n")
    monkeypatch.chdir(repo)

    result = runner.invoke(app, ["release", "tag", "9.9.9"])

    output = result.stdout + str(result.exception or "")
    assert result.exit_code != 0
    assert "dirty_release_tree: 1 uncommitted path(s)" in output
    assert _git(repo, "tag", "--list").strip() == ""
