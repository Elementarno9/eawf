"""The ``worktrees`` and ``leftovers`` doctor checks."""

from __future__ import annotations

import os
import stat
import tempfile
import time
from pathlib import Path

import pytest

from eawf.kernel.state import resolve as state_resolve
from eawf.observability.doctor import checks_worktrees
from eawf.observability.doctor.checks_worktrees import (
    MAX_TREES,
    SNAPSHOT_NOTE,
    btrfs_exclusive_bytes,
    check_leftovers,
    check_worktrees,
)
from eawf.runtime.daemon.runtime_dir import tree_key
from tests._linked_worktree import git, repo_with_linked_worktree

_EIGHT_DAYS = 8 * 24 * 3600


def _resolver_0_7_1(cwd: Path) -> tuple[Path, bool]:
    """The 0.7.1 resolution: walk up from the cwd itself to the first ``.ea``."""
    start = cwd.resolve()
    for directory in [start, *start.parents]:
        if (directory / ".ea").is_dir():
            return directory / ".ea" / "state.json", True
    return start / ".ea" / "state.json", False


def _btrfs_stand_in(tmp_path: Path, body: str) -> Path:
    """Write an executable that answers ``btrfs filesystem du`` with *body*."""
    script = tmp_path / "bin" / "btrfs"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


_TABLE = "printf '     Total   Exclusive  Set shared  Filename\\n 100 40 60 /x\\n'"


@pytest.fixture(autouse=True)
def _isolated_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the temp directory, the runtime base and btrfs at the test's own paths."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    monkeypatch.setattr(checks_worktrees, "BTRFS", str(tmp_path / "no-btrfs"))
    return temp


def _add(main: Path, name: str) -> Path:
    path = main / ".ea" / "worktrees" / name
    git(main, "worktree", "add", "-q", "-b", f"task-{name}", str(path))
    return path.resolve()


def _age(main: Path, tree: Path) -> None:
    stamp = time.time() - _EIGHT_DAYS
    for name in ("index", "HEAD"):
        os.utime(main / ".git" / "worktrees" / tree.name / name, (stamp, stamp))


def test_worktrees_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    main, linked = repo_with_linked_worktree(tmp_path / "repo")

    healthy = check_worktrees(workspace=main)
    assert healthy.status == "ok", healthy.detail
    assert healthy.detail is not None
    assert "1 linked worktree(s)" in healthy.detail
    assert "btrfs exclusive n/a" in healthy.detail
    assert SNAPSHOT_NOTE in healthy.detail
    # Run from the linked worktree, the check judges the same repository.
    assert check_worktrees(workspace=linked).status == "ok"

    # Fire proof: the 0.7.1 resolver hands a linked worktree its own .ea.
    with monkeypatch.context() as patch:
        patch.setattr(state_resolve, "home_state", _resolver_0_7_1)
        leaked = check_worktrees(workspace=main)
    assert leaked.status == "fail"
    assert f"the resolver returns {linked / '.ea' / 'state.json'} from {linked}" in (
        leaked.detail or ""
    )

    # A dirty tree idle past seven days.
    dirty = _add(main, "dirty")
    (dirty / "wip.txt").write_text("wip\n", encoding="utf-8")
    _age(main, dirty)
    stale = check_worktrees(workspace=main)
    assert stale.status == "warn"
    assert f"{dirty} dirty for over 7 days" in (stale.detail or "")
    assert "eawf worktree gc --dry-run" in (stale.detail or "")

    # An unlanded tree idle past seven days.
    git(dirty, "add", "wip.txt")
    git(dirty, "commit", "-q", "-m", "own")
    _age(main, dirty)
    assert f"{dirty} unlanded for over 7 days" in (check_worktrees(workspace=main).detail or "")
    git(dirty, "reset", "-q", "--hard", "HEAD~1")

    # A runtime directory keyed to a worktree's own state.json.
    runtime = tmp_path / "xdg" / "eawfd" / "trees" / tree_key(linked / ".ea" / "state.json")
    runtime.mkdir(parents=True)
    keyed = check_worktrees(workspace=main)
    assert keyed.status == "warn"
    assert f"runtime directory {runtime} is keyed to {linked}" in (keyed.detail or "")
    runtime.rmdir()

    # More than twenty trees: two in place, the rest added up to one past the limit.
    assert check_worktrees(workspace=main).status == "ok"
    for index in range(MAX_TREES - 1):
        _add(main, f"t{index:02d}")
    piled = check_worktrees(workspace=main)
    assert piled.status == "warn"
    assert f"{MAX_TREES + 1} linked worktrees (more than {MAX_TREES})" in (piled.detail or "")


def test_worktrees_check_outside_git(tmp_path: Path) -> None:
    assert check_worktrees(workspace=None).status == "ok"
    outside = check_worktrees(workspace=tmp_path)
    assert outside.status == "ok"
    assert outside.detail == "not in a git repository"


def test_leftovers_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolated_host: Path
) -> None:
    main, _linked = repo_with_linked_worktree(tmp_path / "repo")
    assert check_leftovers(workspace=main).status == "ok"

    temp = _isolated_host
    (temp / "eawf-gate-a1").mkdir()
    (temp / "eawf-gate-a1" / "out.log").write_bytes(b"g" * 10)
    (temp / "eawf-proof-1").mkdir()
    (temp / "eawf-proof-2").mkdir()
    (temp / "unrelated-1").mkdir()
    trees = tmp_path / "xdg" / "eawfd" / "trees"
    for key, pid in (("nopid", None), ("badpid", "x"), ("deadpid", "999999999")):
        (trees / key).mkdir(parents=True)
        if pid is not None:
            (trees / key / "eawfd.pid").write_text(pid, encoding="utf-8")
    (trees / "live").mkdir()
    (trees / "live" / "eawfd.pid").write_text(str(os.getpid()), encoding="utf-8")
    clone = main / ".ea" / "local" / "release-pipeline" / "0.7.0" / "batch-suite-checkout"
    clone.mkdir(parents=True)
    (clone / "README").write_bytes(b"c" * 3)

    result = check_leftovers(workspace=main)
    assert result.status == "warn"
    detail = result.detail or ""
    assert "eawf-gate sandboxes: 1, 10 B apparent, btrfs exclusive n/a" in detail
    assert "eawf-proof shells: 2, 0 B apparent" in detail
    assert "runtime directories with no live eawfd.pid: 3, " in detail
    assert "batch-suite-checkout clones: 1, 3 B apparent" in detail
    assert detail.endswith(SNAPSHOT_NOTE)
    # Without a workspace the clone row has no repository to look in.
    assert "batch-suite-checkout" not in (check_leftovers(workspace=None).detail or "")

    monkeypatch.setattr(checks_worktrees, "BTRFS", str(_btrfs_stand_in(tmp_path, _TABLE)))
    measured = check_leftovers(workspace=main).detail or ""
    assert "eawf-gate sandboxes: 1, 10 B apparent, btrfs exclusive 40 B" in measured
    assert "btrfs exclusive n/a" not in measured


def test_btrfs_exclusive_bytes_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert btrfs_exclusive_bytes([]) == 0
    assert btrfs_exclusive_bytes([tmp_path]) is None  # tool missing
    cases = {
        _TABLE: 40,
        "printf '  Total Exclusive Set Filename\\n 1 2 3 /a\\n 4 5 6 /b c\\n'": 7,
        "echo 'ERROR: not a btrfs filesystem' >&2; exit 1": None,
        "printf '     Total   Exclusive  Set shared  Filename\\n'": None,
        "printf 'header\\n 1 lots 3 /a\\n'": None,
        "printf 'header\\n 1 2\\n'": None,
    }
    for body, expected in cases.items():
        monkeypatch.setattr(checks_worktrees, "BTRFS", str(_btrfs_stand_in(tmp_path, body)))
        assert btrfs_exclusive_bytes([tmp_path]) == expected, body
