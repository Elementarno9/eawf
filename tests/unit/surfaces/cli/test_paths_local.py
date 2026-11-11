"""``eawf paths local`` prints an agent-writable ``.ea/local`` folder, or refuses.

Exit 0 prints the folder of a kind, or the dated file a slug names in it; exit 2
refuses ``runtime``, ``cache``, an unknown kind or a malformed slug; exit 3
refuses a directory no eawf-managed repository holds.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the contract is the verb's exit status

from eawf.kernel.layout import ONLY_EAWF_WRITES_REFUSAL, SLUG_MAX_LENGTH
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.scope import NOT_MANAGED_MESSAGE
from tests._linked_worktree import git, repo_with_linked_worktree

runner = CliRunner()


def _git_init(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "-q")


@pytest.fixture
def managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from the root of a fixture repository eawf manages."""
    repo = tmp_path / "repo"
    _git_init(repo)
    (repo / ".ea").mkdir()
    (repo / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return repo.resolve()


def _local(*args: str) -> tuple[int, str, str]:
    result = runner.invoke(app, ["paths", "local", *args])
    return result.exit_code, result.stdout, result.stderr


def test_prints_research_root(managed: Path) -> None:
    code, out, _ = _local("--kind", "research")
    assert code == 0
    assert out == f"{managed / '.ea' / 'local' / 'research'}\n"

    before = date.today()
    code, out, _ = _local("--kind", "research", "--slug", "pick-a-cache")
    after = date.today()
    assert code == 0
    expected = {
        f"{managed / '.ea' / 'local' / 'research' / f'{day.isoformat()}-pick-a-cache.md'}\n"
        for day in (before, after)
    }
    assert out in expected
    assert not (managed / ".ea" / "local").exists()


@pytest.mark.parametrize("kind", ["spikes", "release", "reports", "archive"])
def test_prints_every_agent_kind(managed: Path, kind: str) -> None:
    code, out, _ = _local("--kind", kind)
    assert (code, out) == (0, f"{managed / '.ea' / 'local' / kind}\n")


def test_a_subfolder_prints_the_repository_root(managed: Path) -> None:
    sub = managed / "a" / "b"
    sub.mkdir(parents=True)
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(sub)
        code, out, _ = _local("--kind", "spikes")
    assert (code, out) == (0, f"{managed / '.ea' / 'local' / 'spikes'}\n")


def test_a_linked_worktree_prints_the_main_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.delenv("EA_STATE", raising=False)
    main, linked = repo_with_linked_worktree(tmp_path)
    monkeypatch.chdir(linked)
    code, out, _ = _local("--kind", "research")
    assert (code, out) == (0, f"{main / '.ea' / 'local' / 'research'}\n")


def test_refusals(managed: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for kind in ("runtime", "cache"):
        code, out, err = _local("--kind", kind)
        assert (code, out) == (2, "")
        assert ONLY_EAWF_WRITES_REFUSAL == "runtime and cache are written by eawf only"
        assert ONLY_EAWF_WRITES_REFUSAL in err

    code, out, err = _local("--kind", "dispatch")
    assert (code, out) == (2, "")
    assert "unknown local kind 'dispatch'" in err

    scratch = tmp_path / "scratch"
    _git_init(scratch)
    monkeypatch.chdir(scratch)
    code, out, err = _local("--kind", "research")
    assert (code, out) == (3, "")
    assert NOT_MANAGED_MESSAGE in err


def test_refuses_outside_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loose = tmp_path / "loose"
    loose.mkdir()
    monkeypatch.chdir(loose)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    code, out, _ = _local("--kind", "research")
    assert (code, out) == (3, "")


@pytest.mark.parametrize(
    "slug",
    ["", "Upper", "two--hyphens", "-lead", "trail-", "a/b", "x" * (SLUG_MAX_LENGTH + 1)],
    ids=["empty", "upper", "double-hyphen", "leading", "trailing", "slash", "too-long"],
)
def test_refuses_a_malformed_slug(managed: Path, slug: str) -> None:
    code, out, err = _local("--kind", "research", "--slug", slug)
    assert (code, out) == (2, "")
    assert "slug" in err


def test_a_missing_kind_is_a_usage_error(managed: Path) -> None:
    code, out, _ = _local()
    assert (code, out) == (2, "")
