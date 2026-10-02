"""Gate: the managed block ignores every machine-local path the stores declare.

The storage tiers and the commit policy say which bytes are machine-local:
the status projection, the derived indexes, the firehose and the cutover's
own scratch (staging generations, restore copies, the journal and the
canary declaration). A path they declare and the shipped block misses shows
up as committable in the first ``git status`` after a cutover, so this gate
asks git itself, through ``git check-ignore``, about a repository whose only
ignore rules are the rendered block.

An older block is the other half: a tree initialised by an earlier release
keeps the block that release wrote until something rewrites it, so the
refresh is pinned here too -- in place, idempotent, and blind to the
operator's own lines.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.commit_policy import EA_PATH_CLASSES, CommitPolicy
from eawf.kernel.store.paths import (
    index_path,
    ledger_path,
    local_store_path,
    seed_ledger_path,
    status_projection_path,
    store_path,
)
from eawf.kernel.store.tiers import (
    LEDGER_COLLECTIONS,
    STATUS_PROJECTION_COLLECTIONS,
    Epoch2Collection,
)
from eawf.platform.install.gitignore_writer import (
    GITIGNORE_PATTERNS,
    plan_gitignore_block,
    refresh_gitignore_block,
    write_gitignore,
)
from eawf.platform.install.managed_block import ManagedBlockError

#: The block v0.6.8 shipped: no cutover scratch, no generation tree.
_OLD_BLOCK = (
    "# BEGIN EAWF:gitignore\n"
    "CLAUDE.md\n"
    ".claude/\n"
    ".ea/locks/\n"
    ".ea/local/\n"
    ".ea/indexes/\n"
    "/custom/state.json.lock\n"
    "# END EAWF:gitignore\n"
)
_USER_HEAD = "# operator notes\nbuild/\n\n"
_USER_TAIL = "\n# after the block\n!keep.db\n"


def _git(repo: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        input=stdin,
        check=False,
        capture_output=True,
        text=True,
    )


def _ignored(repo: Path, paths: list[str]) -> set[str]:
    """Return the subset of *paths* the repository's ignore rules match."""
    result = _git(repo, "check-ignore", "--no-index", "--stdin", stdin="\n".join(paths))
    assert result.returncode in (0, 1), result.stderr
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _block_only_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "--quiet").returncode == 0
    write_gitignore(repo)
    return repo


def _relative(repo: Path, paths: list[Path]) -> list[str]:
    return [path.relative_to(repo).as_posix() for path in paths]


def _machine_local_paths(repo: Path) -> list[str]:
    """Every path the tier and path declarations put outside version control."""
    generation = repo / ".ea" / "generations" / "gen-0123abcd" / "state.json"
    epoch1 = repo / ".ea" / "state.json"
    paths = [
        status_projection_path(generation),
        *(ledger_path(generation, item) for item in STATUS_PROJECTION_COLLECTIONS),
        *(index_path(generation, item) for item in LEDGER_COLLECTIONS),
        *(local_store_path(generation, kind) for kind in StoreKind),
        *(index_path(epoch1, item) for item in LEDGER_COLLECTIONS),
        *(local_store_path(epoch1, kind) for kind in StoreKind),
        store_path(epoch1, StoreKind.EVENT),
    ]
    probes = [row.probe for row in EA_PATH_CLASSES if row.policy is CommitPolicy.NOT_COMMITTED]
    return [*_relative(repo, paths), *probes]


def _committed_paths(repo: Path) -> list[str]:
    """Paths a clone needs, which the block must leave tracked."""
    generation = repo / ".ea" / "generations" / "gen-0123abcd" / "state.json"
    paths = [
        generation,
        ledger_path(generation, Epoch2Collection.TASK),
        *(seed_ledger_path(generation, item) for item in STATUS_PROJECTION_COLLECTIONS),
    ]
    probes = [row.probe for row in EA_PATH_CLASSES if row.policy is CommitPolicy.COMMITTED]
    return [*_relative(repo, paths), *probes]


def test_block_ignores_every_declared_machine_local_path(tmp_path: Path) -> None:
    repo = _block_only_repo(tmp_path)
    expected = _machine_local_paths(repo)

    missed = sorted(set(expected) - _ignored(repo, expected))

    assert missed == []


def test_block_ignores_the_cutover_scratch_a_v068_tree_reports(tmp_path: Path) -> None:
    repo = _block_only_repo(tmp_path)
    reported = [
        ".ea/generations/gen-0123abcd/local/status.json",
        ".ea/generations/gen-0123abcd/local/ledger/run.jsonl",
        ".ea/generations/gen-0123abcd/indexes/task.index.json",
        ".ea/generations/journal.jsonl",
        ".ea/generations/restore/store/event.jsonl",
        ".ea/generations/.staging-0123abcd/state.json",
        ".ea/epoch2-disposable-canary.json",
    ]

    assert _ignored(repo, reported) == set(reported)


def test_block_leaves_every_committed_path_tracked(tmp_path: Path) -> None:
    repo = _block_only_repo(tmp_path)

    assert _ignored(repo, _committed_paths(repo)) == set()


def test_refresh_rewrites_an_outdated_block_in_place(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(f"{_USER_HEAD}{_OLD_BLOCK}{_USER_TAIL}", encoding="utf-8")

    plan = refresh_gitignore_block(tmp_path)

    text = gitignore.read_text(encoding="utf-8")
    assert plan.stale is True
    assert text.startswith(_USER_HEAD)
    assert text.endswith(_USER_TAIL)
    assert ".ea/generations/journal.jsonl\n" in text
    assert "/custom/state.json.lock\n" in text
    assert ".ea/generations/journal.jsonl" in plan.added
    assert text.count("# BEGIN EAWF:gitignore") == 1


def test_refresh_is_idempotent(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(f"{_USER_HEAD}{_OLD_BLOCK}{_USER_TAIL}", encoding="utf-8")
    refresh_gitignore_block(tmp_path)
    first = gitignore.read_bytes()

    again = refresh_gitignore_block(tmp_path)

    assert again.stale is False
    assert again.added == ()
    assert gitignore.read_bytes() == first


def test_refresh_leaves_a_file_without_a_block_alone(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(_USER_HEAD, encoding="utf-8")

    plan = refresh_gitignore_block(tmp_path)

    assert plan.stale is False
    assert gitignore.read_text(encoding="utf-8") == _USER_HEAD


def test_refresh_creates_no_gitignore_where_none_exists(tmp_path: Path) -> None:
    plan = refresh_gitignore_block(tmp_path)

    assert plan.stale is False
    assert not (tmp_path / ".gitignore").exists()


def test_plan_reports_a_current_block_as_fresh(tmp_path: Path) -> None:
    write_gitignore(tmp_path)

    assert plan_gitignore_block(tmp_path).stale is False


def test_plan_reports_a_block_missing_one_shipped_pattern_as_stale(tmp_path: Path) -> None:
    write_gitignore(tmp_path)
    gitignore = tmp_path / ".gitignore"
    last = GITIGNORE_PATTERNS[-1]
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8").replace(f"{last}\n", ""), encoding="utf-8"
    )

    plan = plan_gitignore_block(tmp_path)

    assert plan.stale is True
    assert plan.added == (last,)


def test_refresh_refuses_unpaired_markers_and_writes_nothing(tmp_path: Path) -> None:
    gitignore = tmp_path / ".gitignore"
    broken = "# BEGIN EAWF:gitignore\nCLAUDE.md\n"
    gitignore.write_text(broken, encoding="utf-8")

    with pytest.raises(ManagedBlockError):
        refresh_gitignore_block(tmp_path)

    assert gitignore.read_text(encoding="utf-8") == broken
