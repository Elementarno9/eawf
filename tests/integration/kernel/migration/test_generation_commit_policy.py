"""The epoch-2 generation tree against the ``.ea/`` commit declaration.

An apply is the first writer that puts a generation into a tree git
tracks, so the declaration has to answer for every file one leaves behind
before a native write creates a live generation. Each case builds that
tree the way the cutover suites do -- a real apply into a declared canary,
or a real crash between the two staging builds -- inside a throwaway
repository carrying this repository's ``.gitignore``, and checks it
against real git: every file classifies, every file declared not
committed is ignored by its own generation entry rather than by accident,
and ``git add`` stages exactly the committed families. A staging file
force-added into the index then proves the census reds on the defect it
exists to catch.
"""

from __future__ import annotations

import inspect
import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from eawf.kernel.migration.epoch2 import canary
from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    GENERATIONS_DIRNAME,
    JOURNAL_FILENAME,
    MARKER_FILENAME,
    RESTORE_DIRNAME,
    RESTORE_MANIFEST_FILENAME,
    SELECTION_FILENAME,
)
from eawf.kernel.migration.epoch2.generation import (
    GENERATION_DOCUMENT,
    STAGING_PREFIX,
    generation_id_for,
)
from eawf.kernel.store.commit_census import run_census
from eawf.kernel.store.commit_policy import CensusFindingKind, CommitPolicy, classify_path
from eawf.kernel.store.paths import index_path, ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from tests.integration.kernel.migration._cutover_harness import (
    applied_tree,
    crash_points,
    crash_the_apply,
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GENERATIONS = f".ea/{GENERATIONS_DIRNAME}"
_CANARY = f".ea/{CANARY_DECLARATION_FILENAME}"
_SEEDED_STAGING_FILE = ".ea/generations/.staging-a/x"

#: The tree kinds a repository is built from: a finished apply, and the
#: wreckage of an apply killed with both staging builds on disk.
_APPLIED = "applied"
_CRASHED_STAGING = "after_staging_second"

#: The rows each tree kind must exercise, so a case that classified
#: nothing interesting cannot pass.
_ROWS_BY_TREE: dict[str, frozenset[str]] = {
    _APPLIED: frozenset(
        {
            ".ea/generations/selected.json",
            ".ea/generations/EPOCH2_ACTIVE.json",
            ".ea/generations/gen-*/state.json",
            ".ea/generations/gen-*/ledger/*.jsonl",
            ".ea/generations/gen-*/indexes/**",
            ".ea/generations/restore/**",
            ".ea/generations/journal.jsonl",
            ".ea/epoch2-disposable-canary.json",
        }
    ),
    _CRASHED_STAGING: frozenset(
        {
            ".ea/generations/.staging-*/**",
            ".ea/generations/restore/**",
            ".ea/generations/journal.jsonl",
            ".ea/epoch2-disposable-canary.json",
        }
    ),
}

#: The ``.gitignore`` entry each not-committed generation row relies on.
#: Pinning the entry rather than "some rule matched" keeps a broad glob
#: elsewhere from passing for a missing generation entry.
_IGNORE_ENTRY_BY_ROW: dict[str, str] = {
    ".ea/generations/gen-*/indexes/**": ".ea/generations/gen-*/indexes/",
    ".ea/generations/.staging-*/**": ".ea/generations/.staging-*/",
    ".ea/generations/restore/**": ".ea/generations/restore/",
    ".ea/generations/journal.jsonl": ".ea/generations/journal.jsonl",
    ".ea/epoch2-disposable-canary.json": ".ea/epoch2-disposable-canary.json",
}


@dataclass(frozen=True)
class GenerationRepo:
    """A throwaway repository holding one generation tree.

    Attributes:
        kind: Which tree it was built from.
        root: The repository root; the target tree is ``root/.ea``.
    """

    kind: str
    root: Path


@pytest.fixture(autouse=True)
def _isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the operator's git config, excludes file included, out of every probe."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)


def _git(repo: Path, *args: str) -> str:
    """Run one git command in ``repo`` and return its stdout."""
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout


def _build_repo(root: Path, kind: str) -> GenerationRepo:
    """Build one generation tree under ``root`` and make ``root`` a repository.

    Args:
        root: The directory to build in; it must not exist yet.
        kind: :data:`_APPLIED`, or the id of the crash point to stop at.

    Returns:
        The repository, with this repository's ``.gitignore`` and an
        ``AGENTS.md`` in place and nothing staged.
    """
    if kind == _APPLIED:
        applied_tree(root)
    else:
        point = next(row for row in crash_points() if row["id"] == kind)
        crash_the_apply(root=root, point=point)
    _git(root, "init", "--quiet")
    shutil.copyfile(_REPO_ROOT / ".gitignore", root / ".gitignore")
    (root / "AGENTS.md").write_text("# contract\n", encoding="utf-8")
    return GenerationRepo(kind=kind, root=root)


@pytest.fixture(params=[_APPLIED, _CRASHED_STAGING])
def generation_repo(request: pytest.FixtureRequest, tmp_path: Path) -> GenerationRepo:
    """Return a repository built from each generation tree kind."""
    return _build_repo(tmp_path / "tree", request.param)


def _tree_files(root: Path) -> list[str]:
    """Return every file under the generation tree, plus the canary declaration."""
    generations = root / _GENERATIONS
    files = [
        path.relative_to(root).as_posix()
        for path in sorted(generations.rglob("*"))
        if path.is_file()
    ]
    assert (root / _CANARY).is_file()
    return [*files, _CANARY]


def _ignore_entries(root: Path, paths: Sequence[str]) -> dict[str, str]:
    """Return the ignore entry git reports for each path.

    Args:
        root: The repository to ask.
        paths: Repo-relative paths to test.

    Returns:
        ``path -> pattern``, with an empty pattern for a path no rule
        matches. ``--no-index`` matches what the census asks, so the answer
        does not depend on what happens to be staged.
    """
    completed = subprocess.run(
        ["git", "check-ignore", "--no-index", "--verbose", "--non-matching", "-z", "--stdin"],
        cwd=root,
        input="\0".join(paths) + "\0",
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode in (0, 1), completed.stderr
    fields = completed.stdout.split("\0")[:-1]
    records = [fields[index : index + 4] for index in range(0, len(fields), 4)]
    return {path: pattern for _source, _line, pattern, path in records}


def test_generation_tree_every_file_classifies(generation_repo: GenerationRepo) -> None:
    files = _tree_files(generation_repo.root)
    governing = {classify_path(path).pattern for path in files}
    assert governing == _ROWS_BY_TREE[generation_repo.kind]


def test_generation_tree_ignored_files_match_the_generation_entries(
    generation_repo: GenerationRepo,
) -> None:
    files = _tree_files(generation_repo.root)
    entries = _ignore_entries(generation_repo.root, files)
    assert set(entries) == set(files)
    for path in files:
        row = classify_path(path)
        if row.policy is CommitPolicy.COMMITTED:
            assert entries[path] == "", f"{path} is committed but ignored by {entries[path]!r}"
        else:
            assert entries[path] == _IGNORE_ENTRY_BY_ROW[row.pattern], path


def test_generation_tree_git_add_stages_only_committed_files(
    generation_repo: GenerationRepo,
) -> None:
    root = generation_repo.root
    _git(root, "add", ".gitignore", "AGENTS.md", ".ea")
    staged = set(_git(root, "ls-files", "--", _GENERATIONS, _CANARY).splitlines())
    committed = {
        path for path in _tree_files(root) if classify_path(path).policy is CommitPolicy.COMMITTED
    }
    assert staged == committed
    findings = run_census(root)
    assert findings == (), "\n".join(finding.render() for finding in findings)


def test_generation_census_reds_on_a_tracked_staging_file(tmp_path: Path) -> None:
    root = _build_repo(tmp_path / "tree", _APPLIED).root
    seeded = root / _SEEDED_STAGING_FILE
    seeded.parent.mkdir(parents=True)
    seeded.write_text("half-built\n", encoding="utf-8")
    _git(root, "add", ".gitignore", "AGENTS.md", ".ea")
    assert _SEEDED_STAGING_FILE not in _git(root, "ls-files").splitlines()
    assert run_census(root) == ()

    _git(root, "add", "--force", _SEEDED_STAGING_FILE)
    findings = run_census(root)

    assert [(finding.kind, finding.path) for finding in findings] == [
        (CensusFindingKind.TRACKED_BUT_NOT_COMMITTED, _SEEDED_STAGING_FILE)
    ]


def test_generation_tree_file_constants_resolve_to_declared_rows() -> None:
    """A renamed file constant reds here rather than shipping an undeclared path."""
    target = Path(".ea")
    generations = target / GENERATIONS_DIRNAME
    document = generations / generation_id_for("0" * 64) / GENERATION_DOCUMENT
    expected = {
        generations / SELECTION_FILENAME: CommitPolicy.COMMITTED,
        generations / MARKER_FILENAME: CommitPolicy.COMMITTED,
        document: CommitPolicy.COMMITTED,
        ledger_path(document, Epoch2Collection.TASK): CommitPolicy.COMMITTED,
        index_path(document, Epoch2Collection.TASK): CommitPolicy.NOT_COMMITTED,
        generations / f"{STAGING_PREFIX}-a" / GENERATION_DOCUMENT: CommitPolicy.NOT_COMMITTED,
        generations / RESTORE_DIRNAME / RESTORE_MANIFEST_FILENAME: CommitPolicy.NOT_COMMITTED,
        generations / JOURNAL_FILENAME: CommitPolicy.NOT_COMMITTED,
        target / CANARY_DECLARATION_FILENAME: CommitPolicy.NOT_COMMITTED,
    }
    actual = {path: classify_path(path.as_posix()).policy for path in expected}
    assert actual == expected


def test_canary_generations_comment_claims_no_single_policy_row() -> None:
    """The tree is classified per file family, and the module must not say otherwise."""
    assert "single row" not in inspect.getsource(canary)
