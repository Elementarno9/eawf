"""Doctor checks over linked worktrees and the leftovers eawf runs strew around.

Kept out of :mod:`eawf.observability.doctor.checks` so that module stays
under the EAWF010 line budget; ``checks.run_all`` imports and registers both.

``worktrees`` fails when the state resolver, started in a linked worktree,
returns that worktree's frozen ``.ea`` copy instead of the main checkout's,
and warns when trees pile up, sit dirty or unlanded, or own a runtime
directory. ``leftovers`` only warns: gate sandboxes and proof shells in the
temp directory, runtime directories of dead daemons and release suite clones
are safe to remove by hand once listed.

On btrfs, hourly snapshots share most of those bytes, so apparent size is
not what removal frees; both checks add the exclusive bytes ``btrfs
filesystem du`` reports, or ``n/a`` where it cannot.
"""

from __future__ import annotations

import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from eawf.kernel.state import resolve as state_resolve
from eawf.observability.doctor.models import CheckResult
from eawf.platform.process_liveness import pid_alive
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.daemon.runtime_dir import TREES_DIRNAME, runtime_base_dir, tree_key
from eawf.runtime.vcs.branch_name import BareRepositoryError, repository_home
from eawf.runtime.workspace.worktree_gc import (
    NotARepositoryError,
    WorktreeRow,
    apparent_bytes,
    integration_branch,
    scan_worktrees,
)

WORKTREES_CHECK: Final = "worktrees"
LEFTOVERS_CHECK: Final = "leftovers"

#: More linked trees than this means landed ones are not being removed.
MAX_TREES: Final = 20

#: How long a tree may sit dirty or unlanded before it is called abandoned.
STALE_AFTER: Final = timedelta(days=7)

#: The btrfs tool; a module constant so a test can point it at a stand-in.
BTRFS: Final = "btrfs"

#: ``btrfs filesystem du`` walks every extent; a large tree takes seconds.
_BTRFS_TIMEOUT_SECONDS: Final = 120.0

SNAPSHOT_NOTE: Final = "apparent size is not reclaimable while snapshots hold it"


def btrfs_exclusive_bytes(paths: list[Path]) -> int | None:
    """Return the btrfs exclusive bytes of *paths*, summed.

    Args:
        paths: Files or directories on one filesystem.

    Returns:
        The summed ``Exclusive`` column of ``btrfs filesystem du -s --raw``;
        0 for no paths; ``None`` when the tool is missing, refuses (not btrfs,
        no permission), times out or prints a table it cannot read.
    """
    if not paths:
        return 0
    try:
        proc = subprocess.run(
            [BTRFS, "filesystem", "du", "-s", "--raw", *(str(path) for path in paths)],
            capture_output=True,
            text=True,
            timeout=_BTRFS_TIMEOUT_SECONDS,
            check=False,
            **no_window_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    total = 0
    rows = proc.stdout.splitlines()[1:]
    for row in rows:
        cells = row.split(None, 3)
        if len(cells) < 4 or not cells[1].isdigit():
            return None
        total += int(cells[1])
    return total if rows else None


def _size_clause(paths: list[Path], apparent: int) -> str:
    """Return *apparent* bytes and the btrfs exclusive bytes of *paths* as one clause."""
    exclusive = btrfs_exclusive_bytes(paths)
    shown = "n/a" if exclusive is None else f"{exclusive:,} B"
    return f"{apparent:,} B apparent, btrfs exclusive {shown}"


def _main_root(workspace: Path | None) -> Path | None:
    """Return the main checkout of the repository holding *workspace*, if any."""
    if workspace is None:
        return None
    try:
        home = repository_home(workspace)
    except BareRepositoryError:
        return None
    return None if home is None else home.main_root


def _row_warnings(row: WorktreeRow, now: datetime, trees_dir: Path) -> list[str]:
    """Return why one present tree deserves a warning: stale, or owning a runtime directory."""
    warnings: list[str] = []
    flags = [flag for flag, on in (("dirty", row.dirty), ("unlanded", not row.landed)) if on]
    if flags and now - row.last_activity > STALE_AFTER:
        warnings.append(f"{row.path} {', '.join(flags)} for over {STALE_AFTER.days} days")
    runtime = trees_dir / tree_key(row.path / ".ea" / "state.json")
    if runtime.exists():
        warnings.append(f"runtime directory {runtime} is keyed to {row.path}")
    return warnings


def check_worktrees(*, workspace: Path | None) -> CheckResult:
    """Judge the linked worktrees of the repository holding *workspace*.

    Args:
        workspace: The workspace anchor, already resolved by the caller;
            ``None`` when no anchor exists.

    Returns:
        ``fail`` naming each linked worktree from which the state resolver
        returns the tree's own ``.ea``; else ``warn`` on more than
        :data:`MAX_TREES` trees, a tree dirty or unlanded past
        :data:`STALE_AFTER`, or a daemon runtime directory keyed to a tree's
        own ``state.json``; else ``ok``. Outside git there is nothing to judge.
    """
    main_root = _main_root(workspace)
    if main_root is None:
        return CheckResult(name=WORKTREES_CHECK, status="ok", detail="not in a git repository")
    try:
        rows = scan_worktrees(main_root, integration=integration_branch(main_root))
    except NotARepositoryError as exc:
        return CheckResult(name=WORKTREES_CHECK, status="warn", detail=str(exc))
    leaks: list[str] = []
    warnings: list[str] = []
    if len(rows) > MAX_TREES:
        warnings.append(f"{len(rows)} linked worktrees (more than {MAX_TREES})")
    now = datetime.now(UTC)
    trees_dir = runtime_base_dir() / TREES_DIRNAME
    for row in rows:
        if row.prunable:
            continue
        resolved, _found = state_resolve.home_state(row.path)
        if resolved.is_relative_to(row.path):
            leaks.append(f"the resolver returns {resolved} from {row.path}")
        warnings.extend(_row_warnings(row, now, trees_dir))
    paths = [row.path for row in rows if not row.prunable]
    apparent = sum(row.size_bytes for row in rows)
    summary = f"{len(rows)} linked worktree(s), {_size_clause(paths, apparent)}; {SNAPSHOT_NOTE}"
    if leaks:
        return CheckResult(
            name=WORKTREES_CHECK,
            status="fail",
            detail=f"{'; '.join(leaks)}: state must resolve to the main checkout; {summary}",
        )
    if warnings:
        return CheckResult(
            name=WORKTREES_CHECK,
            status="warn",
            detail=f"{'; '.join(warnings)}; run `eawf worktree gc --dry-run`; {summary}",
        )
    return CheckResult(name=WORKTREES_CHECK, status="ok", detail=summary)


def _dead_runtime_dirs() -> list[Path]:
    """Return the per-tree runtime directories whose ``eawfd.pid`` names no live daemon."""
    trees_dir = runtime_base_dir() / TREES_DIRNAME
    if not trees_dir.is_dir():
        return []
    dead: list[Path] = []
    for directory in sorted(trees_dir.iterdir()):
        if not directory.is_dir():
            continue
        try:
            pid = int((directory / "eawfd.pid").read_text(encoding="utf-8").strip())
        except OSError, ValueError:
            dead.append(directory)
            continue
        if not pid_alive(pid):
            dead.append(directory)
    return dead


def check_leftovers(*, workspace: Path | None) -> CheckResult:
    """Count the leftovers eawf runs leave behind, with their apparent bytes.

    Args:
        workspace: The workspace anchor, already resolved by the caller;
            ``None`` when no anchor exists, which skips the clone row.

    Returns:
        ``warn`` with a count and bytes per row when any leftover exists,
        else ``ok``; never ``fail``, since each is safe to keep.
    """
    temp = Path(tempfile.gettempdir())
    main_root = _main_root(workspace) or workspace
    clones = (
        sorted((main_root / ".ea" / "local" / "release-pipeline").glob("*/batch-suite-checkout"))
        if main_root is not None
        else []
    )
    groups = (
        ("eawf-gate sandboxes", sorted(temp.glob("eawf-gate-*"))),
        ("eawf-proof shells", sorted(temp.glob("eawf-proof-*"))),
        ("runtime directories with no live eawfd.pid", _dead_runtime_dirs()),
        ("batch-suite-checkout clones", clones),
    )
    if not any(paths for _label, paths in groups):
        return CheckResult(name=LEFTOVERS_CHECK, status="ok", detail="no leftovers")
    # One btrfs call per row: the temp directory is often tmpfs, and a
    # single call over a mixed list fails as a whole.
    rows = [
        f"{label}: {len(paths)}, {_size_clause(paths, sum(apparent_bytes(p) for p in paths))}"
        for label, paths in groups
        if paths
    ]
    return CheckResult(
        name=LEFTOVERS_CHECK,
        status="warn",
        detail=f"{'; '.join(rows)}; {SNAPSHOT_NOTE}",
    )


__all__ = [
    "BTRFS",
    "LEFTOVERS_CHECK",
    "MAX_TREES",
    "SNAPSHOT_NOTE",
    "STALE_AFTER",
    "WORKTREES_CHECK",
    "btrfs_exclusive_bytes",
    "check_leftovers",
    "check_worktrees",
]
