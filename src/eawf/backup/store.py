"""On-disk store for manual snapshot backups under the user-scope home.

The store owns the backup tree layout and the byte-faithful copy primitives.
Per the C10 §5.15.2 layout, snapshots live under a per-repo directory keyed by
``repo_sha = sha256(repo-absolute-path)[:12]`` so backups from distinct repos
never collide and never sit inside the (committed) repo tree::

    <home>/.eawf/backups/<repo_sha>/
    ├── 2026-05-17T12-00-00Z/
    │   ├── state.json
    │   ├── config.yaml      # only when the source file exists
    │   ├── profile.yaml     # only when the source file exists
    │   └── note.txt         # only when an operator note was supplied
    └── 2026-05-15T09-00-00Z/
        └── ...

The timestamp directory name is an ISO-8601 UTC instant with the colons
replaced by hyphens (filesystem-safe) — e.g. ``2026-05-17T12-00-00Z``. The
name sorts lexicographically in chronological order, which the list/prune
verbs rely on.

The user-scope home resolves via the ``EAWF_HOME`` env override first (the
test + CI seam), then ``platformdirs``-anchored :func:`pathlib.Path.home`, so
no machine-specific path is ever hardcoded.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eawf.lock import portalock

logger = logging.getLogger(__name__)

# Files copied into / restored from a snapshot, relative to the repo ``.ea/``
# directory. ``state.json`` is mandatory (its absence is a USER_ERROR at the
# service layer); ``config.yaml`` / ``profile.yaml`` are copied only when they
# exist on disk (a fresh workspace may not carry them yet).
SNAPSHOT_ARTIFACTS: tuple[str, ...] = ("state.json", "config.yaml", "profile.yaml")

# The operator note rides alongside the artifacts as a sidecar text file.
NOTE_FILENAME: str = "note.txt"

# Timestamp dir-name format: ISO-8601 UTC with ``:`` -> ``-`` for fs-safety.
_TS_FORMAT: str = "%Y-%m-%dT%H-%M-%SZ"
_TS_PATTERN: re.Pattern[str] = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\Z")

# A pre-restore safety copy of the live state.json lands at the repo-root of
# the per-repo backup dir, named so it sorts after the timestamp dirs.
PRE_RESTORE_PREFIX: str = "pre-restore-state.json."


@dataclass(frozen=True)
class Snapshot:
    """One snapshot directory in the backup tree.

    Attributes:
        ts: The snapshot timestamp directory name (filesystem-safe, e.g.
            ``2026-05-17T12-00-00Z``). Also the wire identifier the
            ``--ts`` restore flag accepts.
        path: Absolute path to the snapshot directory.
        artifacts: Sorted tuple of artifact filenames present in the
            snapshot (subset of :data:`SNAPSHOT_ARTIFACTS`).
        note: The operator-supplied note, or ``None`` when absent.
    """

    ts: str
    path: Path
    artifacts: tuple[str, ...]
    note: str | None


def user_home(*, home: Path | None = None) -> Path:
    """Return the user-scope home root for the backup tree.

    Resolution order (mirrors the established ``EAWF_*`` env seams):

    1. The explicit *home* kwarg (the in-process test seam).
    2. The ``EAWF_HOME`` env var (the CLI + CI seam).
    3. :func:`pathlib.Path.home`.

    Args:
        home: Explicit home root. When ``None`` the env / ``Path.home``
            ladder is consulted.

    Returns:
        The resolved home root (the ``.eawf`` segment is appended by
        :func:`backups_root`).
    """
    if home is not None:
        return home
    env_home = os.environ.get("EAWF_HOME")
    if env_home:
        return Path(env_home)
    return Path.home()


def repo_sha(repo_root: Path) -> str:
    """Return ``sha256(repo-absolute-path)[:12]`` for *repo_root*.

    The repo root's resolved absolute path is the hash input so the same
    repo always keys to the same backup directory regardless of the cwd the
    backup verb runs from. The 12-hex-char prefix matches the C02 repo-sha
    convention used elsewhere in the codebase.

    Args:
        repo_root: The repo root (the directory that holds ``.ea/``).

    Returns:
        The 12-character lowercase hex digest.
    """
    absolute = str(repo_root.resolve())
    return hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:12]


def backups_root(repo_root: Path, *, home: Path | None = None) -> Path:
    """Return the per-repo backup directory ``<home>/.eawf/backups/<sha>``.

    Args:
        repo_root: The repo root (the directory that holds ``.ea/``).
        home: Optional explicit home root (test seam); see
            :func:`user_home`.

    Returns:
        The absolute per-repo backup directory path (not created here).
    """
    return user_home(home=home) / ".eawf" / "backups" / repo_sha(repo_root)


def format_timestamp(when: datetime) -> str:
    """Render *when* as a filesystem-safe ISO-8601 UTC timestamp string.

    Args:
        when: The instant to format. Naive datetimes are treated as UTC;
            aware datetimes are converted to UTC first.

    Returns:
        The timestamp dir-name string (e.g. ``2026-05-17T12-00-00Z``).
    """
    aware = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime(_TS_FORMAT)


def _atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Write *payload* to *target* via tempfile + ``fsync`` + ``os.replace``.

    Lock-agnostic — the caller acquires the sibling portalock when one is
    needed. Mirrors :func:`eawf.state.writer._write_payload` (and the WAL's
    private byte-writer) rather than importing it, matching the codebase
    convention of keeping the byte-swap idiom co-located with its callers.

    The temp file lands in *target*'s own directory so the final
    :func:`os.replace` is a same-filesystem atomic rename. On any failure the
    half-written temp is removed and *target* is left untouched.

    Args:
        target: Destination path. Parent directories are created on demand.
        payload: Raw bytes to write verbatim (no re-serialisation).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = target.with_name(f"{target.name}.tmp.{suffix}")
    try:
        with tmp.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        parent_fd = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        tmp.unlink(missing_ok=True)


class BackupStore:
    """Reads, writes, lists, and prunes snapshots for a single repo.

    The store binds a repo root to its per-repo backup directory and exposes
    the byte-faithful copy primitives the service layer composes into the
    create/list/restore/prune verbs.
    """

    def __init__(self, repo_root: Path, *, home: Path | None = None) -> None:
        """Bind the store to *repo_root* under the resolved user home.

        Args:
            repo_root: The repo root (the directory that holds ``.ea/``).
            home: Optional explicit home root (test seam); see
                :func:`user_home`.
        """
        self.repo_root = repo_root
        self.ea_dir = repo_root / ".ea"
        self.root = backups_root(repo_root, home=home)

    def _read_snapshot(self, ts_dir: Path) -> Snapshot:
        """Build a :class:`Snapshot` from an on-disk timestamp directory."""
        artifacts = tuple(name for name in SNAPSHOT_ARTIFACTS if (ts_dir / name).is_file())
        note_path = ts_dir / NOTE_FILENAME
        note = note_path.read_text(encoding="utf-8") if note_path.is_file() else None
        return Snapshot(ts=ts_dir.name, path=ts_dir, artifacts=artifacts, note=note)

    def list_snapshots(self) -> list[Snapshot]:
        """Return all snapshots, most-recent first.

        Returns:
            Snapshots sorted by timestamp descending. An empty list when
            the per-repo backup directory does not yet exist.
        """
        if not self.root.is_dir():
            return []
        dirs = sorted(
            (p for p in self.root.iterdir() if p.is_dir() and _TS_PATTERN.match(p.name)),
            key=lambda p: p.name,
            reverse=True,
        )
        return [self._read_snapshot(p) for p in dirs]

    def get_snapshot(self, ts: str) -> Snapshot | None:
        """Return the snapshot named *ts*, or ``None`` when absent.

        *ts* is operator-supplied (the ``--ts`` restore flag), so it is
        validated against the snapshot-id timestamp pattern *before* the
        ``self.root / ts`` join. A ``..`` segment or an absolute path would
        otherwise let the join escape the per-repo backup tree (a
        path-traversal read); the pattern admits only the exact
        filesystem-safe ISO-8601 dir-name :meth:`write_snapshot` writes.

        A malformed *ts* is operator-fixable (a typo or a traversal attempt),
        so it raises the typed :class:`~eawf.backup.service.BackupError` the
        CLI already renders as a ``USER_ERROR`` envelope — not a bare
        ``ValueError`` that would surface as an internal traceback. The
        import is function-local because :mod:`eawf.backup.service` imports
        this module at load time; a top-level import would cycle.

        Args:
            ts: The snapshot timestamp dir-name (the ``--ts`` value).

        Returns:
            The matching :class:`Snapshot`, or ``None`` when no directory
            with that exact name exists.

        Raises:
            BackupError: When *ts* does not match the snapshot timestamp
                pattern (rejects ``..``, absolute paths, and any other
                non-timestamp input before the path-join).
        """
        if not _TS_PATTERN.match(ts):
            from eawf.backup.service import BackupError

            raise BackupError(f"invalid snapshot timestamp: {ts!r}")
        ts_dir = self.root / ts
        if not ts_dir.is_dir():
            return None
        return self._read_snapshot(ts_dir)

    def write_snapshot(self, ts: str, *, note: str | None) -> Snapshot:
        """Copy the repo's ``.ea/`` artifacts into a new ``ts`` snapshot.

        ``state.json`` is always copied; ``config.yaml`` / ``profile.yaml``
        are copied only when present. The copy uses :func:`shutil.copyfile`
        (byte-faithful content copy) so a later restore round-trips to
        byte-identical bytes.

        Args:
            ts: The snapshot timestamp dir-name to create.
            note: Optional operator note persisted as ``note.txt``.

        Returns:
            The written :class:`Snapshot`.
        """
        ts_dir = self.root / ts
        ts_dir.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for name in SNAPSHOT_ARTIFACTS:
            src = self.ea_dir / name
            if src.is_file():
                shutil.copyfile(src, ts_dir / name)
                copied.append(name)
        if note is not None:
            (ts_dir / NOTE_FILENAME).write_text(note, encoding="utf-8")
        logger.info(f"write_snapshot repo_sha={self.root.name} ts={ts!r} artifacts={copied}")
        return self._read_snapshot(ts_dir)

    def write_pre_restore(self, when: datetime) -> Path | None:
        """Snapshot the live ``state.json`` before a restore overwrites it.

        Args:
            when: The instant naming the pre-restore copy.

        Returns:
            The pre-restore copy path, or ``None`` when no live
            ``state.json`` exists to protect.
        """
        live = self.ea_dir / "state.json"
        if not live.is_file():
            return None
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{PRE_RESTORE_PREFIX}{format_timestamp(when)}"
        shutil.copyfile(live, target)
        logger.info(f"write_pre_restore repo_sha={self.root.name} target={target.name!r}")
        return target

    def _restore_state(self, src: Path, dest: Path) -> None:
        """Write the snapshot ``state.json`` over the live file atomically.

        ``state.json`` is the daemon's sole canonical mutable surface, so a
        torn or partial write on interrupt — or a clobbering race with a
        concurrent daemon write — would corrupt project state. The restore
        therefore holds the same sibling :func:`eawf.lock.portalock.acquire`
        the daemon uses and swaps the file via :func:`_atomic_write_bytes`
        (tempfile + ``fsync`` + :func:`os.replace`): the live file is only
        ever the fully-old document or the fully-new one, never an
        intermediate.

        The snapshot bytes are written verbatim (no re-serialisation) so the
        restore stays byte-faithful per C10 §5.15.3 — a snapshot of an
        operator-edited or older-schema ``state.json`` round-trips exactly.

        Args:
            src: The snapshot's ``state.json`` source path.
            dest: The live ``.ea/state.json`` destination path.

        Raises:
            portalock.LockTimeout: When the sibling lock cannot be acquired
                within the timeout (a concurrent daemon write is in flight).
        """
        payload = src.read_bytes()
        with portalock.acquire(dest, timeout=5.0):
            _atomic_write_bytes(dest, payload)

    def restore_snapshot(self, snapshot: Snapshot) -> list[str]:
        """Copy *snapshot*'s artifacts back into the repo's ``.ea/`` dir.

        Only ``state.json`` / ``config.yaml`` / ``profile.yaml`` are
        restored (per C10 §5.15.3) — never ``.ea/store/*.jsonl`` or
        ``.ea/local/``. The restore is byte-faithful.

        ``state.json`` is restored atomically under a sibling portalock (see
        :meth:`_restore_state`) so an interrupt cannot leave a torn file and a
        concurrent daemon write cannot corrupt the result. The ``config.yaml``
        / ``profile.yaml`` sidecars are plain content (not the daemon's
        canonical mutable file) and are restored with a byte-faithful copy.

        Args:
            snapshot: The snapshot to restore from.

        Returns:
            The list of artifact filenames written back, in canonical
            order.

        Raises:
            portalock.LockTimeout: When the sibling lock on ``state.json``
                cannot be acquired within the timeout.
        """
        self.ea_dir.mkdir(parents=True, exist_ok=True)
        restored: list[str] = []
        for name in SNAPSHOT_ARTIFACTS:
            src = snapshot.path / name
            if not src.is_file():
                continue
            dest = self.ea_dir / name
            if name == "state.json":
                self._restore_state(src, dest)
            else:
                shutil.copyfile(src, dest)
            restored.append(name)
        logger.info(f"restore_snapshot ts={snapshot.ts!r} artifacts={restored}")
        return restored

    def prune(self, keep: int) -> list[str]:
        """Delete all but the *keep* most-recent snapshots.

        Pre-restore safety copies (``pre-restore-state.json.*``) are never
        pruned — only the timestamp snapshot directories are subject to the
        keep window.

        Args:
            keep: Number of most-recent snapshots to retain (``>= 0``).

        Returns:
            The timestamp names removed, most-recent-removed first.
        """
        snapshots = self.list_snapshots()
        doomed = snapshots[keep:]
        removed: list[str] = []
        for snap in doomed:
            shutil.rmtree(snap.path)
            removed.append(snap.ts)
        logger.info(f"prune repo_sha={self.root.name} keep={keep} removed={removed}")
        return removed
