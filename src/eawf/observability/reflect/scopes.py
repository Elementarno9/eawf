"""Which local sessions a reflection scope covers, and where their rows may land.

A session row carries the digest of the working directory it ran in, never the
path, so a scope is a set of digests: ``project`` covers the repository root and
each of its git worktrees, ``workspace`` every project in the machine-local eawf
registry, and ``user`` every indexed session.

The history index and the analysis hold every indexed session whatever the scope,
so rows of other projects reach every file a run writes. Those rows stay on this
machine: an output directory that git would commit refuses them before the first
byte is written. The ignore check asks git itself, so every ignore source git
honours counts, not just one ``.gitignore``.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from eawf.kernel.store.paths import LOCAL_DIRNAME
from eawf.observability.reflect.report import ReportScope
from eawf.observability.telemetry.sources.session_history import working_dir_digest
from eawf.platform.registry import Registry, RegistryReadError, read_registry
from eawf.platform.subprocess_detach import no_window_kwargs

logger = logging.getLogger(__name__)


class ForeignRowsRefusedError(ValueError):
    """An output directory git would commit was asked to hold other projects' rows.

    Attributes:
        directory: The refused output directory.
        foreign_rows: How many session rows of other projects the write carried.
    """

    def __init__(self, directory: Path, foreign_rows: int) -> None:
        self.directory = directory
        self.foreign_rows = foreign_rows
        super().__init__(
            f"refusing to write {foreign_rows} session rows of other projects into"
            f" {directory}, which git would commit; name an --out path that is"
            " git-ignored or outside the repository, or drop --out to write under"
            f" {LOCAL_DIRNAME}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionScope:
    """The working-directory digests one scope covers.

    Attributes:
        own: The digests of this project: its root and each of its worktrees.
        covered: The digests the scope covers, or ``None`` for every session.
        labels: A project name for each digest the scope can name.
        label: What the covered set is called on the pages.
    """

    own: frozenset[str]
    covered: frozenset[str] | None
    labels: Mapping[str, str]
    label: str

    def covers(self, cwd_digest: str | None) -> bool:
        """Return whether a session that ran in *cwd_digest* is inside the scope."""
        if self.covered is None:
            return True
        return cwd_digest is not None and cwd_digest in self.covered

    def foreign_rows(self, cwd_digests: Iterable[str | None]) -> int:
        """Return how many of *cwd_digests* belong to a project other than this one."""
        return sum(1 for digest in cwd_digests if digest is None or digest not in self.own)


def _common_dir(checkout: Path) -> Path | None:
    """Return the git directory every worktree of *checkout*'s repository shares.

    The worktrees are read from git's on-disk layout rather than from ``git worktree
    list``, so selecting sessions spawns no process; ``--local-only`` runs stay
    spawn-free unless an output directory sits inside a work tree.
    """
    marker = checkout / ".git"
    if marker.is_dir():
        return marker
    if not marker.is_file():
        return None
    gitdir = Path(marker.read_text(encoding="utf-8").strip().removeprefix("gitdir: "))
    gitdir = gitdir if gitdir.is_absolute() else checkout / gitdir
    common = gitdir / "commondir"
    if not common.is_file():
        return gitdir
    shared = Path(common.read_text(encoding="utf-8").strip())
    return (shared if shared.is_absolute() else gitdir / shared).resolve()


def _checkouts(project_root: Path) -> frozenset[Path]:
    """Return *project_root* and every git worktree of its repository.

    Args:
        project_root: A repository root; a directory git does not track, or one
            that no longer exists, stands for itself alone.

    Returns:
        The checkout directories, *project_root* always among them.
    """
    common = _common_dir(project_root)
    if common is None:
        return frozenset({project_root})
    linked = (
        Path(entry.read_text(encoding="utf-8").strip()).parent
        for entry in (common / "worktrees").glob("*/gitdir")
    )
    return frozenset({project_root, common.parent, *linked})


def _digests(project_root: Path) -> frozenset[str]:
    """Return the working-directory digests of *project_root*'s checkouts."""
    return frozenset(working_dir_digest(path) for path in _checkouts(project_root))


def _registry() -> Registry | None:
    """Return the machine-local registry, or ``None`` when there is none to read."""
    try:
        return read_registry()
    except RegistryReadError as exc:
        logger.debug(f"_registry absent={exc!r}")
        return None


def _registered_labels(registry: Registry | None) -> dict[str, str]:
    """Return a project code for each checkout digest of every registered project."""
    if registry is None:
        return {}
    return {
        digest: entry.code
        for entry in registry.repos.values()
        for digest in _digests(Path(entry.path).expanduser())
    }


def resolve_session_scope(scope: ReportScope, *, project_root: Path, label: str) -> SessionScope:
    """Return the sessions *scope* covers on this machine.

    ``milestone`` and ``track`` select Runs, not sessions, so their sessions are
    the project's own.

    Args:
        scope: The parsed ``--scope``.
        project_root: This tree's repository root.
        label: This project's name.

    Returns:
        The scope. ``workspace`` with no readable registry covers this project alone.
    """
    own = _digests(project_root)
    if scope.kind in ("project", "milestone", "track"):
        return SessionScope(own=own, covered=own, labels=dict.fromkeys(own, label), label=label)
    labels = _registered_labels(_registry()) | dict.fromkeys(own, label)
    covered = None if scope.kind == "user" else frozenset(labels)
    logger.debug(f"resolve_session_scope scope={scope.text()} own={len(own)} labels={len(labels)}")
    return SessionScope(own=own, covered=covered, labels=labels, label=scope.text())


def _work_tree(directory: Path) -> Path | None:
    """Return the top of the git work tree holding *directory*, as git reports it.

    An entry named ``.git`` only says git may be worth asking: an empty or
    invalid one is no repository, and git's own discovery passes it. With no
    such entry above *directory*, git is not spawned at all.

    Args:
        directory: An absolute directory; it need not exist yet.

    Returns:
        The work tree top, or ``None`` when git finds no work tree or the
        probe fails.
    """
    chain = (directory, *directory.parents)
    if not any((parent / ".git").exists() for parent in chain):
        return None
    probe = next(parent for parent in chain if parent.is_dir())
    found = subprocess.run(
        ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    top = found.stdout.strip()
    if found.returncode != 0 or not top:
        logger.debug(f"_work_tree none probe_status={found.returncode}")
        return None
    return Path(top).resolve()


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputArea:
    """The directory a run writes into, and whether git would commit it.

    Attributes:
        directory: The output directory.
        committed: ``True`` when the directory lies in a git work tree, outside the
            tree's local directory, and git does not ignore it.
    """

    directory: Path
    committed: bool

    @classmethod
    def of(cls, directory: Path, *, tree_root: Path) -> OutputArea:
        """Return the area of *directory*, asking git whether it would be committed.

        Args:
            directory: The output directory; it need not exist yet.
            tree_root: The tree's ``.ea`` directory, whose local directory is
                machine-local by construction.

        Returns:
            The area.
        """
        resolved = directory.resolve()
        if resolved.is_relative_to(tree_root.resolve() / LOCAL_DIRNAME):
            return cls(directory=resolved, committed=False)
        work_tree = _work_tree(resolved)
        if work_tree is None or not resolved.is_relative_to(work_tree):
            return cls(directory=resolved, committed=False)
        relative = resolved.relative_to(work_tree).as_posix()
        # The trailing slash asks about the directory itself, so a pattern such as
        # ``out/`` matches before the directory exists; exit 0 means ignored.
        ignored = subprocess.run(
            ["git", "-C", str(work_tree), "check-ignore", "-q", "--", f"{relative}/"],
            capture_output=True,
            check=False,
            **no_window_kwargs(),
        )
        return cls(directory=resolved, committed=ignored.returncode != 0)

    @classmethod
    def created(cls, directory: Path) -> OutputArea:
        """Return the area of a directory this run created for itself.

        No repository's ignore rules govern a fresh private directory, so git is
        not asked about it.

        Args:
            directory: The directory the run created.

        Returns:
            The area, never committed.
        """
        return cls(directory=directory.resolve(), committed=False)

    def admit(self, foreign_rows: int) -> None:
        """Refuse a write that would carry rows of other projects into a committed area.

        Args:
            foreign_rows: How many session rows of other projects the write carries.

        Raises:
            ForeignRowsRefusedError: The area is committed and *foreign_rows* is
                above zero.
        """
        if self.committed and foreign_rows > 0:
            raise ForeignRowsRefusedError(self.directory, foreign_rows)


__all__ = [
    "ForeignRowsRefusedError",
    "OutputArea",
    "SessionScope",
    "resolve_session_scope",
]
