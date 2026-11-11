"""``eawf worktree gc --dry-run`` lists reap candidates and never reaps."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.main import get_command

from eawf.runtime.workspace.worktree_gc import (
    FALLBACK_INTEGRATION,
    REBUILDABLE,
    NotARepositoryError,
    apparent_bytes,
    integration_branch,
    plan_worktree_gc,
    scan_worktrees,
)
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from tests._linked_worktree import git, repo_with_linked_worktree

_TWO_DAYS = 2 * 24 * 3600


def _gc(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    """Run ``eawf worktree gc <argv>`` in process; return exit code, stdout and stderr."""
    capsys.readouterr()
    code = get_command(app).main(["worktree", "gc", *argv], prog_name="eawf", standalone_mode=False)
    captured = capsys.readouterr()
    return (code or 0), captured.out, captured.err


def _add(main: Path, name: str, *, detach: bool = False) -> Path:
    """Add a linked worktree named *name* under the main checkout's worktrees root."""
    path = main / ".ea" / "worktrees" / name
    branch = ["--detach"] if detach else ["-b", f"task-{name}"]
    git(main, "worktree", "add", "-q", *branch, str(path))
    return path.resolve()


def _commit(tree: Path, name: str, *, task: str | None = None) -> str:
    """Commit one new file in *tree*; return the commit id."""
    (tree / f"{name}.txt").write_text(f"{name}\n", encoding="utf-8")
    git(tree, "add", f"{name}.txt")
    message = ["-m", f"add {name}"] + (["-m", f"Task: {task}"] if task else [])
    git(tree, "commit", "-q", *message)
    return git(tree, "rev-parse", "HEAD").strip()


def _age(main: Path, tree: Path, seconds: int) -> None:
    """Push the last activity of *tree* *seconds* into the past."""
    admin = main / ".git" / "worktrees" / tree.name
    stamp = time.time() - seconds
    for name in ("index", "HEAD"):
        os.utime(admin / name, (stamp, stamp))


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Path]]:
    """A repository whose worktrees root holds one entry of every kind."""
    main, _outside = repo_with_linked_worktree(tmp_path / "repo")
    trees = {
        name: _add(main, name)
        for name in (
            "picked",
            "trailer",
            "partial",
            "empty",
            "dirty",
            "unlanded",
            "ignored",
            "gone",
        )
    }
    trees["scratch-old"] = _add(main, "scratch-old", detach=True)
    trees["scratch-new"] = _add(main, "scratch-new", detach=True)
    # Landed by an equivalent patch: the cherry-pick drops no trailer to match.
    git(main, "cherry-pick", _commit(trees["picked"], "picked"))
    # Only its Task trailer is upstream, on a commit with other content: a hint, not proof.
    _commit(trees["trailer"], "trailer", task="QR-0003")
    _commit(main, "rewritten", task="QR-0003")
    # One commit cherry-picked, a later one of the same Task never landed.
    git(main, "cherry-pick", _commit(trees["partial"], "partial-1", task="QR-0005"))
    _commit(trees["partial"], "partial-2", task="QR-0005")
    _commit(trees["unlanded"], "unlanded", task="QR-0004")
    (trees["dirty"] / "scratch.txt").write_text("wip\n", encoding="utf-8")
    # Ignored local work a removal would delete; rebuildable caches do not count.
    (main / ".git" / "info" / "exclude").write_text(
        "*.local\n.venv/\n__pycache__/\n", encoding="utf-8"
    )
    (trees["ignored"] / "notes.local").write_text("z" * 11, encoding="utf-8")
    for name in ("empty", "ignored"):
        (trees[name] / ".venv").mkdir()
        (trees[name] / ".venv" / "python").write_text("v", encoding="utf-8")
        (trees[name] / "pkg" / "__pycache__").mkdir(parents=True)
        (trees[name] / "pkg" / "__pycache__" / "m.pyc").write_text("c", encoding="utf-8")
    shutil.rmtree(trees["gone"])
    root = main / ".ea" / "worktrees"
    (root / "hand-made").mkdir()
    (root / "hand-made" / "notes.txt").write_text("x" * 7, encoding="utf-8")
    (root / "stray.py").write_text("y" * 5, encoding="utf-8")
    _age(main, trees["scratch-old"], _TWO_DAYS)
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(main)
    return main, trees


def _snapshot(main: Path) -> tuple[str, dict[str, tuple[int, int]]]:
    """Return git's worktree list and every entry's size and mtime under the trees."""
    listing = git(main, "worktree", "list", "--porcelain")
    entries: dict[str, tuple[int, int]] = {}
    for base in (main / ".ea" / "worktrees", main / ".git" / "worktrees"):
        for path in sorted(base.rglob("*")):
            stat = path.lstat()
            entries[str(path)] = (stat.st_size, stat.st_mtime_ns)
    return listing, entries


def test_dry_run_lists_landed(
    fleet: tuple[Path, dict[str, Path]], capsys: pytest.CaptureFixture[str]
) -> None:
    main, trees = fleet
    code, out, _err = _gc(["--dry-run", "--json"], capsys)
    assert code == exit_codes.OK
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["integration"] == "main"
    reasons = {Path(row["path"]).name: row["reason"] for row in payload["candidates"]}
    assert reasons == {
        "picked": "landed",
        "empty": "landed",
        "scratch-old": "expired",
        "gone": "prunable",
        "hand-made": "unregistered",
        "stray.py": "leftover",
    }
    sizes = {Path(row["path"]).name: row["bytes"] for row in payload["candidates"]}
    assert sizes["hand-made"] == 7
    assert sizes["stray.py"] == 5
    assert sizes["gone"] == 0
    assert sizes["picked"] > 0
    kept = {Path(row["path"]).name: row["flags"] for row in payload["kept"]}
    assert kept == {
        "dirty": ["dirty"],
        "unlanded": ["unlanded"],
        "trailer": ["unlanded", "trailer-landed"],
        "partial": ["unlanded", "trailer-landed"],
        "ignored": ["ignored"],
        "scratch-new": ["scratch"],
    }
    ignored = {Path(row["path"]).name: row for row in payload["kept"]}["ignored"]
    assert (ignored["ignored_files"], ignored["ignored_bytes"]) == (1, 11)
    # The tree outside the worktrees root is never a candidate.
    assert all(
        Path(row["path"]).parent == main / ".ea" / "worktrees" for row in payload["candidates"]
    )
    assert trees["picked"].is_dir()


def test_dry_run_prints_remove_line_and_writes_nothing(
    fleet: tuple[Path, dict[str, Path]], capsys: pytest.CaptureFixture[str]
) -> None:
    main, trees = fleet
    before = _snapshot(main)
    code, out, _err = _gc(["--dry-run"], capsys)
    assert code == exit_codes.OK
    lines = out.splitlines()
    for name in ("picked", "empty"):
        assert f"git worktree remove {trees[name]}" in lines
    removes = [line for line in lines if line.startswith("git worktree remove ")]
    assert len(removes) == 2
    assert f"  kept (ignored): {trees['ignored']} (1 ignored, 11 B)" in lines
    assert _snapshot(main) == before


@pytest.mark.parametrize("name", ["with space", "$(touch pwned)", "it's"])
def test_dry_run_remove_line_is_shell_quoted(tmp_path: Path, name: str) -> None:
    main, _outside = repo_with_linked_worktree(tmp_path / "repo")
    tree = main / ".ea" / "worktrees" / name
    git(main, "worktree", "add", "-q", "-b", "quoted", str(tree))
    (candidate,) = plan_worktree_gc(main, now=datetime.now(UTC)).candidates
    assert candidate.remove_line == f"git worktree remove {shlex.quote(str(tree))}"
    assert shlex.split(candidate.remove_line or "") == ["git", "worktree", "remove", str(tree)]


def test_landed_needs_every_own_commit_upstream(tmp_path: Path) -> None:
    main, _outside = repo_with_linked_worktree(tmp_path / "repo")
    tree = _add(main, "t")
    # Move the integration branch on first, so the cherry-pick is a new commit.
    _commit(main, "other")
    git(main, "cherry-pick", _commit(tree, "one", task="QR-0009"))
    (row,) = [r for r in scan_worktrees(main, integration="main") if r.path == tree]
    assert (row.landed, row.trailer_seen) == (True, False)
    _commit(tree, "two", task="QR-0009")
    (row,) = [r for r in scan_worktrees(main, integration="main") if r.path == tree]
    assert (row.landed, row.trailer_seen) == (False, True)


def test_ignored_counts_only_non_rebuildable_entries(tmp_path: Path) -> None:
    main, _outside = repo_with_linked_worktree(tmp_path / "repo")
    tree = _add(main, "t")
    (main / ".git" / "info" / "exclude").write_text("*.keep\n.ruff_cache/\n", encoding="utf-8")
    (tree / ".ruff_cache").mkdir()
    (tree / ".ruff_cache" / "x").write_text("rr", encoding="utf-8")
    (row,) = [r for r in scan_worktrees(main, integration="main") if r.path == tree]
    assert (row.ignored_files, row.ignored_bytes) == (0, 0)
    (tree / "a.keep").write_text("aaa", encoding="utf-8")
    (tree / "sub").mkdir()
    (tree / "sub" / "b.keep").write_text("b", encoding="utf-8")
    (row,) = [r for r in scan_worktrees(main, integration="main") if r.path == tree]
    assert (row.ignored_files, row.ignored_bytes) == (2, 4)
    assert {".venv", "__pycache__"} <= REBUILDABLE


def test_gc_without_dry_run_refused(
    fleet: tuple[Path, dict[str, Path]], capsys: pytest.CaptureFixture[str]
) -> None:
    main, _trees = fleet
    before = _snapshot(main)
    code, out, _err = _gc([], capsys)
    assert code == exit_codes.USER_ERROR == 1
    assert "not available before 0.8" in out
    assert _snapshot(main) == before


def test_gc_outside_a_repository_is_a_user_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(tmp_path)
    code, out, _err = _gc(["--dry-run"], capsys)
    assert code == exit_codes.USER_ERROR
    assert "not a git repository" in out


def test_plan_without_worktrees_root_is_empty(tmp_path: Path) -> None:
    main, outside = repo_with_linked_worktree(tmp_path / "repo")
    plan = plan_worktree_gc(main, now=datetime.now(UTC))
    assert plan.candidates == []
    assert plan.kept == []
    assert [row.path for row in scan_worktrees(main, integration="main")] == [outside]


def test_plan_outside_a_repository_raises(tmp_path: Path) -> None:
    with pytest.raises(NotARepositoryError, match="not a git repository"):
        plan_worktree_gc(tmp_path, now=datetime.now(UTC))


def test_scan_without_integration_lands_nothing(tmp_path: Path) -> None:
    main, _outside = repo_with_linked_worktree(tmp_path / "repo")
    (rows,) = scan_worktrees(main, integration=None)
    assert rows.landed is False
    (rows,) = scan_worktrees(main, integration="no-such-branch")
    assert rows.landed is False


def test_integration_branch_falls_back_to_dev(tmp_path: Path) -> None:
    main, _outside = repo_with_linked_worktree(tmp_path / "repo")
    assert integration_branch(main) == "main"
    git(main, "checkout", "-q", "--detach")
    assert integration_branch(main) is None
    git(main, "branch", FALLBACK_INTEGRATION)
    assert integration_branch(main) == FALLBACK_INTEGRATION


def test_apparent_bytes_boundaries(tmp_path: Path) -> None:
    assert apparent_bytes(tmp_path / "missing") == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert apparent_bytes(empty) == 0
    one = tmp_path / "one.txt"
    one.write_bytes(b"a")
    assert apparent_bytes(one) == 1
    tree = tmp_path / "tree"
    (tree / "sub").mkdir(parents=True)
    (tree / "sub" / "big.bin").write_bytes(b"b" * 4096)
    (tree / "link").symlink_to(tmp_path / "one.txt")
    assert apparent_bytes(tree) == 4096 + (tree / "link").lstat().st_size
