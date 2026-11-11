"""Tidying hand-made entries and orphan files out of a repository's ``.ea`` tree.

``.ea/local`` holds two kinds of entry: the folders agents and skills write
(:class:`~eawf.kernel.layout.LocalKind`), and the files and folders eawf's own
0.7 writers create at its top level. Anything else there was made by hand. A
few paths outside ``.ea/local`` are orphans: nothing in eawf writes or reads
them any more.

Tidying moves each hand-made entry and each orphan into a dated archive folder,
``.ea/local/archive/<YYYY-MM-DD>-tidy/``, under its path relative to ``.ea``,
and appends one ``from<TAB>to<TAB>bytes`` line per move to the folder's
``MANIFEST.tsv``. Nothing is deleted. An entry is kept in place, and reported,
when git tracks a file at or below it, when its target already exists, or when
it is neither a file, a folder nor a link.

The reflection collection of earlier versions, ``.ea/local/reflect/``, is not
archived: its files move one by one into ``.ea/local/reports/reflect/``, where
the reflect verbs now read them, recorded in the same manifest. A SQLite
database there moves together with its ``-wal``, ``-shm`` and ``-journal``
files, and only while no eawf process holds its advisory lock. The folders the
migration empties are removed.

Each move is written to ``MANIFEST.pending`` before its rename and appended to
the manifest after it, so a tidy that stops between the two leaves a record the
next apply reconciles against the tree.

Planning and moving are separate so ``eawf local tidy --dry-run`` shows the exact
moves the apply step makes.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
from dataclasses import dataclass
from datetime import date
from itertools import groupby
from pathlib import Path
from typing import Final

from eawf.kernel.layout import EA_DIRNAME, LOCAL_DIRNAME, REFLECT_DIRNAME, LocalKind
from eawf.kernel.state.enums import StoreKind
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.lock import portalock, sibling
from eawf.runtime.workspace.worktree_gc import apparent_bytes

logger = logging.getLogger(__name__)

#: The manifest each archive folder carries, one ``from<TAB>to<TAB>bytes`` line per move.
MANIFEST_FILENAME: Final = "MANIFEST.tsv"

#: The one move under way, written before its rename and removed once the manifest holds it.
PENDING_FILENAME: Final = "MANIFEST.pending"

#: The folder at the top of ``.ea/local`` that held the reflection collection
#: before it moved under ``reports/``.
LEGACY_REFLECT_NAME: Final = REFLECT_DIRNAME

#: The suffix of a SQLite database file in the legacy reflection collection.
_SQLITE_SUFFIX: Final = ".sqlite"

#: The files SQLite keeps beside a database; the database is consistent only with them.
_SQLITE_COMPANIONS: Final = ("-wal", "-shm", "-journal")

#: How a manifest field spells the characters that would break its line.
_ESCAPES: Final = {"\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r"}
_UNESCAPES: Final = {code[1]: char for char, code in _ESCAPES.items()}

#: The suffix of the dated archive folder a tidy moves entries into.
TIDY_ARCHIVE_SUFFIX: Final = "tidy"

#: The suffix of a lock file kept beside the file or folder it guards.
_LOCK_SUFFIX: Final = ".lock"

#: The ``.ea/local`` top-level names eawf's 0.7 writers create. Each stays
#: until v0.8 moves it into ``runtime/`` or ``cache/``.
MACHINE_NAMES_07: Final = frozenset(
    {
        "budget_notices.json",
        "config-backups",
        "config.yaml",
        "epoch2",
        "epoch2-canary.json",
        "evidence",
        "gate-children",
        "gate-claims",
        "gate-diagnostics",
        "gate-progress",
        "gate-proofs",
        "hooks",
        "ledger",
        "proof-evidence",
        "release-facts",
        "release-pipeline",
        "rule-staleness-sessions.json",
        "staged",
        "status.json",
        "transcript-index.jsonl",
        *(f"{kind.value}.jsonl" for kind in StoreKind),
    }
)

#: Name prefixes of the folders the epoch-2 canary and the runtime
#: certifier's decoy allocate at the top of ``.ea/local``.
MACHINE_PREFIXES_07: Final = ("eawf-canary-", "canary-credential-")

_AGENT_FOLDERS: Final = frozenset(kind.value for kind in LocalKind)

#: The folders only eawf writes in the v0.8 layout; allowed ahead of it.
_EAWF_ONLY_FOLDERS: Final = frozenset({"runtime", "cache"})

#: Paths below ``.ea`` nothing writes or reads any more.
ORPHAN_PATHS: Final = (
    "specs",
    "locks/worktrees.lock.lock",
    "store/commit_repin.jsonl",
    "store/gold_label.jsonl",
)

#: The schema-migration backups of the frozen ``.ea/state.json``.
ORPHAN_GLOB: Final = "state.json.bak.*"


def is_known_local_name(name: str) -> bool:
    """Return whether *name* is a folder or file eawf expects at the top of ``.ea/local``.

    Args:
        name: One top-level entry name of ``.ea/local``.

    Returns:
        ``True`` for an agent folder, an eawf-only folder, a 0.7 machine name,
        a name with a 0.7 machine prefix, or the ``.lock`` sibling of any of
        them; ``False`` otherwise.
    """
    base = name.removesuffix(_LOCK_SUFFIX)
    return (
        base in _AGENT_FOLDERS
        or base in _EAWF_ONLY_FOLDERS
        or base in MACHINE_NAMES_07
        or base.startswith(MACHINE_PREFIXES_07)
    )


def unknown_local_entries(local_dir: Path) -> tuple[Path, ...]:
    """Return each top-level entry of *local_dir* that is not a known name.

    Args:
        local_dir: A repository's ``.ea/local`` directory; absent is empty.

    Returns:
        The unknown entries, sorted by name.
    """
    if not local_dir.is_dir():
        return ()
    return tuple(
        entry for entry in sorted(local_dir.iterdir()) if not is_known_local_name(entry.name)
    )


def _is_legacy_reflect(path: Path) -> bool:
    """Return whether *path* is a real folder; a link or a file there is archived instead."""
    return path.is_dir() and not path.is_symlink()


def legacy_reflect_notice(local_dir: Path) -> str | None:
    """Return the line saying a legacy reflection collection waits to be moved.

    Args:
        local_dir: A repository's ``.ea/local`` directory; absent is empty.

    Returns:
        One sentence naming the folder and ``eawf local tidy``, or ``None``
        when *local_dir* holds no ``reflect`` folder.
    """
    if not _is_legacy_reflect(local_dir / LEGACY_REFLECT_NAME):
        return None
    reports = f"{EA_DIRNAME}/{LOCAL_DIRNAME}/{LocalKind.REPORTS.value}/{REFLECT_DIRNAME}"
    return (
        f"{EA_DIRNAME}/{LOCAL_DIRNAME}/{LEGACY_REFLECT_NAME} holds reflect data of an earlier"
        f" version that nothing reads; run eawf local tidy to move it to {reports}"
    )


@dataclass(frozen=True, slots=True)
class TidyMove:
    """One entry a tidy moves.

    Attributes:
        source: The entry, absolute.
        target: Where it moves, absolute: inside the archive folder, or
            inside ``reports/reflect`` for a legacy reflection file.
        size: Its apparent bytes, summed over a folder without following links.
        guard: The SQLite database whose advisory lock must be free for this
            move, or ``None``; consecutive moves sharing a guard move together
            or not at all.
    """

    source: Path
    target: Path
    size: int
    guard: Path | None = None


@dataclass(frozen=True, slots=True)
class TidyKept:
    """One entry a tidy leaves in place.

    Attributes:
        path: The entry, absolute.
        reason: Why it stays.
    """

    path: Path
    reason: str


@dataclass(frozen=True, slots=True)
class LocalTidyPlan:
    """The moves one tidy of a repository would make.

    Attributes:
        repo: The repository's main checkout.
        archive_dir: The dated archive folder the moves land in.
        moves: Each entry to move, local entries first, then orphans.
        kept: Each candidate left in place, with its reason.
    """

    repo: Path
    archive_dir: Path
    moves: tuple[TidyMove, ...] = ()
    kept: tuple[TidyKept, ...] = ()


@dataclass(frozen=True, slots=True)
class LocalTidyResult:
    """What applying a plan did.

    Attributes:
        made: The moves made, in plan order.
        kept: Each planned move left in place, with its reason: its target
            appeared since planning, its database was held, or the
            filesystem refused the rename.
    """

    made: tuple[TidyMove, ...] = ()
    kept: tuple[TidyKept, ...] = ()


class LocalTidyError(RuntimeError):
    """git could not say which files it tracks, so nothing may be moved."""


def _orphans(tree_root: Path) -> list[Path]:
    """Return each orphan path that exists below *tree_root*, links included."""
    found = sorted(tree_root.glob(ORPHAN_GLOB))
    found += [tree_root / rel for rel in ORPHAN_PATHS if os.path.lexists(tree_root / rel)]
    return found


def _tracked(repo: Path, candidates: list[Path]) -> set[str]:
    """Return the repository-relative paths of the files git tracks at or below *candidates*.

    Raises:
        LocalTidyError: git exits non-zero.
    """
    if not candidates:
        return set()
    specs = [candidate.relative_to(repo).as_posix() for candidate in candidates]
    result = subprocess.run(
        ["git", "--literal-pathspecs", "-C", str(repo), "ls-files", "-z", "--", *specs],
        capture_output=True,
        text=True,
        check=False,
        **no_window_kwargs(),
    )
    if result.returncode != 0:
        raise LocalTidyError(f"git ls-files failed: {result.stderr.strip()}")
    return {path for path in result.stdout.split("\0") if path}


def _holds_tracked(rel: str, tracked: set[str]) -> bool:
    """Return whether a tracked file is *rel* or lies below it."""
    return any(path == rel or path.startswith(f"{rel}/") for path in tracked)


def plan_local_tidy(repo: Path, *, today: date) -> LocalTidyPlan:
    """Return the moves that would tidy the ``.ea`` tree of *repo*.

    Args:
        repo: The repository's main checkout.
        today: The date the archive folder is named for.

    Returns:
        The plan; empty when nothing is hand-made or orphaned.

    Raises:
        LocalTidyError: git cannot list the tracked files of *repo*.
    """
    tree_root = repo / EA_DIRNAME
    local_dir = tree_root / LOCAL_DIRNAME
    archive_dir = local_dir / LocalKind.ARCHIVE.value / f"{today.isoformat()}-{TIDY_ARCHIVE_SUFFIX}"
    legacy = local_dir / LEGACY_REFLECT_NAME
    migrating = _is_legacy_reflect(legacy)
    candidates = [
        entry for entry in unknown_local_entries(local_dir) if not (migrating and entry == legacy)
    ]
    candidates += _orphans(tree_root)
    tracked = _tracked(repo, [legacy, *candidates] if migrating else candidates)
    moves: list[TidyMove] = []
    kept: list[TidyKept] = []
    if migrating:
        reports = local_dir / LocalKind.REPORTS.value / REFLECT_DIRNAME
        for members, guard in _legacy_units(legacy):
            pairs = [(member, reports / member.relative_to(legacy)) for member in members]
            reason = next(
                (found for pair in pairs if (found := _kept_reason(repo, *pair, tracked))), None
            )
            if reason is None:
                moves += [TidyMove(src, dst, apparent_bytes(src), guard) for src, dst in pairs]
            else:
                kept += [TidyKept(member, reason) for member in members]
    for source in candidates:
        target = archive_dir / source.relative_to(tree_root)
        reason = _kept_reason(repo, source, target, tracked)
        if reason is None:
            moves.append(TidyMove(source, target, apparent_bytes(source)))
        else:
            kept.append(TidyKept(source, reason))
    return LocalTidyPlan(repo=repo, archive_dir=archive_dir, moves=tuple(moves), kept=tuple(kept))


def _kept_reason(repo: Path, source: Path, target: Path, tracked: set[str]) -> str | None:
    """Return why *source* stays in place, or ``None`` when it may move to *target*."""
    if _holds_tracked(source.relative_to(repo).as_posix(), tracked):
        return "git tracks a file in it"
    if not (source.is_symlink() or source.is_file() or source.is_dir()):
        return "it is not a file, a folder or a link"
    if os.path.lexists(target):
        return f"{target.relative_to(repo).as_posix()} already exists"
    return None


def _legacy_units(legacy: Path) -> list[tuple[tuple[Path, ...], Path | None]]:
    """Return the files of *legacy* in path order, each SQLite database with its companions.

    A link to a folder is one file: the walk does not follow it, and the link
    itself moves.

    Returns:
        One ``(members, guard)`` pair per unit: a database's members are the
        database and the companions present, guarded by the database; any
        other file stands alone, unguarded.
    """
    files: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(legacy):
        here = Path(dirpath)
        files.update(here / name for name in filenames)
        files.update(here / name for name in dirnames if (here / name).is_symlink())
    units: list[tuple[tuple[Path, ...], Path | None]] = []
    grouped: set[Path] = set()
    for path in sorted(files):
        if path.name.endswith(_SQLITE_SUFFIX) and path.is_file() and not path.is_symlink():
            companions = [path.with_name(f"{path.name}{suffix}") for suffix in _SQLITE_COMPANIONS]
            members = (path, *(companion for companion in companions if companion in files))
            units.append((members, path))
            grouped.update(members)
    units += [((path,), None) for path in sorted(files - grouped)]
    return sorted(units, key=lambda unit: unit[0][0])


def _escape(field: str) -> str:
    """Return *field* with each backslash, tab, newline and carriage return escaped."""
    return "".join(_ESCAPES.get(char, char) for char in field)


def _unescape(field: str) -> str:
    """Return the text :func:`_escape` turned into *field*.

    Raises:
        ValueError: *field* holds a backslash that starts no known escape.
    """
    chars = iter(field)
    out: list[str] = []
    for char in chars:
        if char != "\\":
            out.append(char)
            continue
        code = next(chars, "")
        if code not in _UNESCAPES:
            raise ValueError(f"manifest field {field!r} holds an unknown escape")
        out.append(_UNESCAPES[code])
    return "".join(out)


def manifest_line(plan: LocalTidyPlan, move: TidyMove) -> str:
    """Return the ``from<TAB>to<TAB>bytes`` manifest line of *move*, paths repository-relative.

    A backslash, tab, newline or carriage return in a path is written as
    ``\\\\``, ``\\t``, ``\\n`` or ``\\r``, so every move takes exactly one line
    and :func:`parse_manifest_line` reads the paths back unchanged.

    Args:
        plan: The plan *move* belongs to.
        move: One of its moves.

    Returns:
        The line, without its newline.
    """
    source = _escape(move.source.relative_to(plan.repo).as_posix())
    target = _escape(move.target.relative_to(plan.repo).as_posix())
    return f"{source}\t{target}\t{move.size}"


def parse_manifest_line(line: str) -> tuple[str, str, int]:
    """Return the source, the target and the bytes one manifest line records.

    Args:
        line: One line :func:`manifest_line` wrote, without its newline.

    Returns:
        The repository-relative source and target, unescaped, and the bytes.

    Raises:
        ValueError: The line does not hold three tab-separated fields, a field
            holds an unknown escape, or the bytes are not a decimal count.
    """
    fields = line.split("\t")
    if len(fields) != 3:
        raise ValueError(f"manifest line holds {len(fields)} fields, not 3: {line!r}")
    source, target, size = fields
    if not (size.isascii() and size.isdigit()):
        raise ValueError(f"manifest line holds no byte count: {line!r}")
    return _unescape(source), _unescape(target), int(size)


def _record(archive_dir: Path, line: str) -> None:
    """Append *line* to the manifest unless it is already the last line, then clear the pending."""
    manifest = archive_dir / MANIFEST_FILENAME
    last = ""
    if manifest.exists():
        last = manifest.read_text(encoding="utf-8").removesuffix("\n").rpartition("\n")[2]
    if last != line:
        with manifest.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
    (archive_dir / PENDING_FILENAME).unlink()


def _reconcile_pending(plan: LocalTidyPlan) -> None:
    """Settle each move an earlier tidy wrote as pending and did not record.

    The move happened when its source is gone and its target is present; it
    is then appended to its manifest. Otherwise the rename never ran, and the
    pending record is dropped.
    """
    archive_root = plan.archive_dir.parent
    for pending in sorted(archive_root.glob(f"*-{TIDY_ARCHIVE_SUFFIX}/{PENDING_FILENAME}")):
        line = pending.read_text(encoding="utf-8").removesuffix("\n")
        try:
            source, target, _size = parse_manifest_line(line)
        except ValueError:
            # a tidy stopped while writing the record, so before its rename
            logger.warning(f"_reconcile_pending dropped unreadable record={pending}")
            pending.unlink()
            continue
        if not os.path.lexists(plan.repo / source) and os.path.lexists(plan.repo / target):
            logger.info(f"_reconcile_pending recorded source={source} target={target}")
            _record(pending.parent, line)
        else:
            logger.info(f"_reconcile_pending dropped source={source}: it did not move")
            pending.unlink()


def _move(plan: LocalTidyPlan, move: TidyMove) -> str | None:
    """Make one move, recording it first as pending and then in the manifest.

    Returns:
        ``None`` once moved and recorded, or why the entry stays in place.

    Raises:
        OSError: The manifest could not be written after the rename; the
            pending record stays for the next apply to reconcile.
    """
    # The target may have appeared since planning; rename would replace it.
    if os.path.lexists(move.target):
        logger.warning(f"_move skipped source={move.source}: target exists")
        return f"{move.target.relative_to(plan.repo).as_posix()} already exists"
    line = manifest_line(plan, move)
    pending = plan.archive_dir / PENDING_FILENAME
    try:
        plan.archive_dir.mkdir(parents=True, exist_ok=True)
        pending.write_text(f"{line}\n", encoding="utf-8")
        move.target.parent.mkdir(parents=True, exist_ok=True)
        move.source.rename(move.target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            pending.unlink(missing_ok=True)
        logger.warning(f"_move kept source={move.source} error={exc}")
        return f"it could not be moved: {exc.strerror or exc}"
    _record(plan.archive_dir, line)
    logger.info(f"_move moved source={move.source} target={move.target}")
    return None


def _move_set(
    plan: LocalTidyPlan, guard: Path, moves: tuple[TidyMove, ...]
) -> tuple[tuple[TidyMove, ...], tuple[TidyKept, ...]]:
    """Move a SQLite database and its companions together, or none of them.

    Every eawf reader of the database holds its sibling advisory lock while
    the database is open, and creates the lock file to take it. No lock file
    means no eawf process has it open; an existing one is taken without
    waiting, and held while the set moves. A process outside eawf that opens
    the file cannot be seen.

    Returns:
        The moves made and the entries kept; one of the two is empty.
    """
    with contextlib.ExitStack() as held:
        if os.path.lexists(sibling.lock_path(guard)):
            try:
                held.enter_context(portalock.acquire(guard, timeout=0))
            except portalock.LockTimeout:
                reason = f"another process holds {guard.name} open"
                return (), tuple(TidyKept(move.source, reason) for move in moves)
            except OSError as exc:
                reason = f"cannot tell whether a process holds {guard.name}: {exc.strerror or exc}"
                return (), tuple(TidyKept(move.source, reason) for move in moves)
        made: list[TidyMove] = []
        for move in moves:
            failed = _move(plan, move)
            if failed is None:
                made.append(move)
                continue
            # put back what moved, so the database never stands apart from its journal
            for done in reversed(made):
                _move(plan, TidyMove(done.target, done.source, done.size))
            reason = f"{guard.name} moves with its companions, and {move.source.name}: {failed}"
            return (), tuple(TidyKept(each.source, reason) for each in moves)
    return tuple(made), ()


def _remove_empty_folders(root: Path) -> None:
    """Remove each folder below and including *root* that holds nothing, deepest first."""
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        with contextlib.suppress(OSError):
            Path(dirpath).rmdir()


def apply_local_tidy(plan: LocalTidyPlan) -> LocalTidyResult:
    """Make every move of *plan*, recording each in the archive's manifest.

    Each entry moves by one ``rename``, so a file or a folder is either still
    in place or wholly moved. Pending records an earlier tidy left are settled
    first. A move whose target appeared since planning, whose database is held,
    or whose rename the filesystem refuses is kept and reported; the moves
    before it stay made and recorded.

    Args:
        plan: A plan :func:`plan_local_tidy` returned.

    Returns:
        The moves made and the planned moves kept.

    Raises:
        OSError: The manifest could not be written after a rename.
    """
    _reconcile_pending(plan)
    made: list[TidyMove] = []
    kept: list[TidyKept] = []
    for guard, run in groupby(plan.moves, key=lambda move: move.guard):
        if guard is not None:
            set_made, set_kept = _move_set(plan, guard, tuple(run))
            made += set_made
            kept += set_kept
            continue
        for move in run:
            reason = _move(plan, move)
            if reason is None:
                made.append(move)
            else:
                kept.append(TidyKept(move.source, reason))
    legacy = plan.repo / EA_DIRNAME / LOCAL_DIRNAME / LEGACY_REFLECT_NAME
    if any(move.source.is_relative_to(legacy) for move in made):
        _remove_empty_folders(legacy)
    return LocalTidyResult(made=tuple(made), kept=tuple(kept))


__all__ = [
    "LEGACY_REFLECT_NAME",
    "MACHINE_NAMES_07",
    "MACHINE_PREFIXES_07",
    "MANIFEST_FILENAME",
    "ORPHAN_GLOB",
    "ORPHAN_PATHS",
    "PENDING_FILENAME",
    "TIDY_ARCHIVE_SUFFIX",
    "LocalTidyError",
    "LocalTidyPlan",
    "LocalTidyResult",
    "TidyKept",
    "TidyMove",
    "apply_local_tidy",
    "is_known_local_name",
    "legacy_reflect_notice",
    "manifest_line",
    "parse_manifest_line",
    "plan_local_tidy",
    "unknown_local_entries",
]
