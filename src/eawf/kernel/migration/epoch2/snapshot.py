"""The read barrier over the epoch-1 corpus and the digests it emits.

Every number the census reports has to be reproducible, which means the
whole corpus must be read once, at one revision, before any rule looks at
it. This module is that single read: it walks the declared surfaces, pins
each one to a sha256 over its own bytes, and hands downstream stages a
frozen in-memory snapshot they can never re-read from disk.

The barrier is taken over a staging root rather than over the live
project directory on purpose. A live tree has a writer -- the daemon --
so a barrier over it could only ever be advisory; a barrier over an
assembled copy is enforceable, and :meth:`SourceSnapshot.verify_unchanged`
proves after the fact that it held.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import (
    MigrationDuplicateKeyError,
    MigrationSourceMutatedError,
    MigrationSourceUnreadableError,
    MigrationStagingRefusedError,
)
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel, rule_digest
from eawf.kernel.store.commit_policy import CommitPolicy, classify_path

logger = logging.getLogger(__name__)


DOCUMENT_LOCATOR = "document.json"
REGISTRY_LOCATOR = "registry.json"
TELEMETRY_LOCATOR = "telemetry.json"
STORE_DIRECTORY = "store"
CONFIG_DIRECTORY = "config"

LEDGER_SUFFIX = ".jsonl"
CONFIG_SUFFIX = ".yaml"


class SourceSurface(StrEnum):
    """The five kinds of epoch-1 read surface the barrier covers."""

    DOCUMENT = "document"
    STORE = "store"
    REGISTRY = "registry"
    CONFIG = "config"
    TELEMETRY = "telemetry"


class SurfaceDigest(StrictMigrationModel):
    """One read surface, pinned to the bytes it held at barrier time.

    Attributes:
        surface: Which kind of surface the file belongs to.
        locator: The snapshot-root-relative POSIX path, which is what the
            digest set is keyed by. A root-relative locator keeps the
            identity free of machine-specific paths.
        byte_length: Size of the file in bytes.
        digest: Lowercase sha256 hex digest over the raw bytes.
    """

    surface: SourceSurface
    locator: Annotated[str, Field(min_length=1, max_length=256)]
    byte_length: Annotated[int, Field(ge=0)]
    digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SourceSnapshotIdentity(StrictMigrationModel):
    """The digest set that names one epoch-1 revision.

    Attributes:
        surfaces: Every read surface, in locator order.
        snapshot_digest: A digest over the whole surface set, so two
            snapshots can be compared with one string rather than by
            walking their surfaces.
    """

    surfaces: tuple[SurfaceDigest, ...]
    snapshot_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    def digest_for(self, locator: str) -> SurfaceDigest:
        """Return the surface digest recorded for ``locator``.

        Args:
            locator: A snapshot-root-relative POSIX path.

        Returns:
            The pinned surface digest.

        Raises:
            KeyError: When no surface was read at ``locator``.
        """
        for surface in self.surfaces:
            if surface.locator == locator:
                return surface
        raise KeyError(locator)

    def locators_for(self, surface: SourceSurface) -> tuple[str, ...]:
        """Return every locator read under ``surface``, in locator order."""
        return tuple(row.locator for row in self.surfaces if row.surface is surface)


def digest_bytes(data: bytes) -> str:
    """Return the lowercase sha256 hex digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a dict from ``pairs``, refusing a repeated key.

    Args:
        pairs: The key/value pairs the JSON parser decoded for one object.

    Returns:
        The object as a dict.

    Raises:
        MigrationDuplicateKeyError: When a key appears more than once.
    """
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MigrationDuplicateKeyError(f"duplicate key {key!r} in the source document")
        result[key] = value
    return result


def _read_bytes(path: Path, *, what: str) -> bytes:
    """Read ``path`` in binary, reporting a missing surface as a refusal.

    Args:
        path: The file to read.
        what: A root-relative description used in the failure message.

    Returns:
        The raw file bytes.

    Raises:
        MigrationSourceUnreadableError: When the file is missing or is
            not a readable regular file.
    """
    try:
        return path.read_bytes()
    except OSError as error:
        raise MigrationSourceUnreadableError(
            f"cannot read the {what!r} read surface: {error.__class__.__name__}"
        ) from error


def _require_directory(path: Path, *, what: str) -> None:
    """Raise when ``path`` is not an existing directory.

    Args:
        path: The directory to check.
        what: A root-relative description used in the failure message.

    Raises:
        MigrationSourceUnreadableError: When the directory is absent.
    """
    if not path.is_dir():
        raise MigrationSourceUnreadableError(f"the {what!r} read surface directory is missing")


def _parse_document(data: bytes, *, locator: str) -> dict[str, Any]:
    """Decode the epoch-1 document, refusing duplicates and non-objects.

    Args:
        data: The raw document bytes.
        locator: The root-relative path, used in failure messages.

    Returns:
        The decoded document.

    Raises:
        MigrationDuplicateKeyError: When any object repeats a key.
        MigrationSourceUnreadableError: When the bytes are not valid JSON
            or decode to something other than a JSON object.
    """
    try:
        decoded = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MigrationSourceUnreadableError(f"{locator} is not valid JSON: {error}") from error
    if not isinstance(decoded, dict):
        raise MigrationSourceUnreadableError(
            f"{locator} decodes to {type(decoded).__name__}, expected a JSON object"
        )
    return decoded


def _parse_ledger(data: bytes, *, locator: str) -> tuple[dict[str, Any], ...]:
    """Decode one JSONL ledger into its rows.

    Blank lines are ignored because a trailing newline is not a row. A
    line that is present but undecodable is a refusal rather than a skip.

    Args:
        data: The raw ledger bytes.
        locator: The root-relative path, used in failure messages.

    Returns:
        The decoded rows, in file order.

    Raises:
        MigrationDuplicateKeyError: When a row repeats an object key.
        MigrationSourceUnreadableError: When a line is not valid JSON or
            is not a JSON object.
    """
    rows: list[dict[str, Any]] = []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MigrationSourceUnreadableError(f"{locator} is not valid UTF-8: {error}") from error
    for index, line in enumerate(text.splitlines()):
        if not line.strip():
            continue
        try:
            decoded = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as error:
            raise MigrationSourceUnreadableError(
                f"{locator} line {index + 1} is not valid JSON: {error}"
            ) from error
        if not isinstance(decoded, dict):
            raise MigrationSourceUnreadableError(
                f"{locator} line {index + 1} decodes to {type(decoded).__name__}, "
                f"expected a JSON object"
            )
        rows.append(decoded)
    return tuple(rows)


def _sorted_children(directory: Path, *, suffix: str) -> Iterator[Path]:
    """Yield the files in ``directory`` with ``suffix``, in name order."""
    yield from sorted(
        child for child in directory.iterdir() if child.is_file() and child.name.endswith(suffix)
    )


class SourceSnapshot(StrictMigrationModel):
    """One epoch-1 corpus, read once and frozen.

    Attributes:
        root: The staging directory the barrier was taken over.
        identity: The digest set naming this revision of the corpus.
        document: The decoded epoch-1 state document.
        ledgers: Every store ledger's rows, keyed by file stem.
    """

    root: Path
    identity: SourceSnapshotIdentity
    document: dict[str, Any]
    ledgers: dict[str, tuple[dict[str, Any], ...]]

    @classmethod
    def read(cls, root: Path) -> SourceSnapshot:
        """Take the read barrier over the snapshot rooted at ``root``.

        The layout is fixed: ``document.json`` for the epoch-1 state
        document, ``store/*.jsonl`` for the ledgers, ``registry.json``,
        ``config/*.yaml`` for the layered configuration and
        ``telemetry.json`` for the telemetry metadata. The registry,
        config and telemetry surfaces are digested but not decoded: the
        census reports no facts about their contents, and pinning their
        bytes is what stops a later stage from reading a different
        revision than the one the census counted.

        Args:
            root: The staging directory holding the assembled corpus.

        Returns:
            The frozen snapshot.

        Raises:
            MigrationSourceUnreadableError: When a declared surface is
                missing, is not valid JSON, or holds a line that is not a
                JSON object.
            MigrationDuplicateKeyError: When any decoded object repeats a
                key.
        """
        digests: list[SurfaceDigest] = []

        def pin(path: Path, *, surface: SourceSurface, locator: str) -> bytes:
            data = _read_bytes(path, what=locator)
            digests.append(
                SurfaceDigest(
                    surface=surface,
                    locator=locator,
                    byte_length=len(data),
                    digest=digest_bytes(data),
                )
            )
            return data

        document_bytes = pin(
            root / DOCUMENT_LOCATOR, surface=SourceSurface.DOCUMENT, locator=DOCUMENT_LOCATOR
        )

        store_dir = root / STORE_DIRECTORY
        _require_directory(store_dir, what=STORE_DIRECTORY)
        ledgers: dict[str, tuple[dict[str, Any], ...]] = {}
        for ledger_path in _sorted_children(store_dir, suffix=LEDGER_SUFFIX):
            locator = f"{STORE_DIRECTORY}/{ledger_path.name}"
            data = pin(ledger_path, surface=SourceSurface.STORE, locator=locator)
            ledgers[ledger_path.name[: -len(LEDGER_SUFFIX)]] = _parse_ledger(data, locator=locator)

        pin(root / REGISTRY_LOCATOR, surface=SourceSurface.REGISTRY, locator=REGISTRY_LOCATOR)

        config_dir = root / CONFIG_DIRECTORY
        _require_directory(config_dir, what=CONFIG_DIRECTORY)
        for config_path in _sorted_children(config_dir, suffix=CONFIG_SUFFIX):
            pin(
                config_path,
                surface=SourceSurface.CONFIG,
                locator=f"{CONFIG_DIRECTORY}/{config_path.name}",
            )

        pin(root / TELEMETRY_LOCATOR, surface=SourceSurface.TELEMETRY, locator=TELEMETRY_LOCATOR)

        surfaces = tuple(sorted(digests, key=lambda row: row.locator))
        return cls(
            root=root,
            identity=SourceSnapshotIdentity(
                surfaces=surfaces,
                snapshot_digest=rule_digest([[row.locator, row.digest] for row in surfaces]),
            ),
            document=_parse_document(document_bytes, locator=DOCUMENT_LOCATOR),
            ledgers=ledgers,
        )

    def ledger(self, name: str) -> tuple[dict[str, Any], ...]:
        """Return the rows of the ``name`` ledger.

        Args:
            name: The ledger file stem, such as ``audit``.

        Returns:
            The ledger rows, in file order.

        Raises:
            MigrationSourceUnreadableError: When the snapshot holds no
                such ledger. A missing ledger is not an empty one, and
                counting it as zero rows would shrink the census.
        """
        try:
            return self.ledgers[name]
        except KeyError as error:
            raise MigrationSourceUnreadableError(
                f"the snapshot holds no {name!r} ledger under {STORE_DIRECTORY}/"
            ) from error

    def verify_unchanged(self) -> None:
        """Re-read every pinned surface and confirm the barrier held.

        Raises:
            MigrationSourceUnreadableError: When a pinned surface has
                since been removed.
            MigrationSourceMutatedError: When a surface's bytes no longer
                match the digest taken at barrier time.
        """
        for surface in self.identity.surfaces:
            data = _read_bytes(self.root / surface.locator, what=surface.locator)
            current = digest_bytes(data)
            if current != surface.digest:
                raise MigrationSourceMutatedError(
                    f"{surface.locator} changed under the read barrier: "
                    f"pinned {surface.digest}, found {current}"
                )


#: Where the committed corpus lives, relative to the repository root.
LIVE_STATE_LOCATOR: Final = ".ea/state.json"
LIVE_STORE_LOCATOR: Final = ".ea/store"
LIVE_CONFIG_LOCATOR: Final = ".ea/config.yaml"

#: The ``.ea`` directory the staging reads and must never write into.
LIVE_TREE_DIRNAME: Final = ".ea"

#: The file the committed layered config lands in inside the snapshot.
STAGED_CONFIG_FILENAME: Final = "base.yaml"

#: The one timestamp the synthesised registry carries. Fixed so two
#: stagings of one revision are byte-identical.
STAGED_REGISTRY_TIMESTAMP: Final = "2026-01-01T00:00:00Z"

#: The neutral telemetry body a staged snapshot carries. The live
#: ``.ea/telemetry.db`` is uncommitted because it embeds this machine's
#: absolute paths, so no clone could reproduce it.
STAGED_TELEMETRY_BODY: Final[dict[str, Any]] = {
    "row_counts": {"events": 0},
    "schema_version": "1.0",
    "tables": ["events", "spans"],
}

#: How long one git call may take before the staging gives up on it.
GIT_TIMEOUT_SECONDS: Final = 60


class StagedSource(StrictMigrationModel):
    """One committed file the snapshot is assembled from.

    Attributes:
        locator: The file's repo-relative path, which is the string the
            commit policy classifies.
        destination: Where the file lands inside the staged snapshot,
            relative to the snapshot root.
        object_id: The git blob the bytes are read from, so a staging
            can be reproduced from the revision alone.
    """

    locator: Annotated[str, Field(min_length=1)]
    destination: Annotated[str, Field(min_length=1)]
    object_id: Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]


class StagedCorpus(StrictMigrationModel):
    """The result of staging one revision's committed corpus.

    Attributes:
        root: The snapshot root the corpus was assembled in.
        revision: The commit every staged byte was read from.
        sources: The committed files copied, in staging order.
    """

    root: Path
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")]
    sources: tuple[StagedSource, ...]


def is_committed(locator: str) -> bool:
    """Report whether version control carries one path under ``.ea/``.

    Args:
        locator: A repo-relative, forward-slash path.

    Returns:
        ``True`` when the commit policy declares the path committed.

    Raises:
        UndeclaredPathError: When no policy row matches, which means the
            tree grew a file family nobody declared.
    """
    return classify_path(locator).policy is CommitPolicy.COMMITTED


def require_committed(locator: str) -> str:
    """Return a locator the snapshot layout needs, once the policy admits it.

    Args:
        locator: A repo-relative path the fixed part of the layout maps.

    Returns:
        The locator unchanged.

    Raises:
        ValueError: When the policy declares the path uncommitted. The
            layout and the policy then disagree about what a
            repository's reproducible state is, and that is a defect in
            one of the two rather than something to route around.
        UndeclaredPathError: When no policy row matches the path.
    """
    if not is_committed(locator):
        raise ValueError(f"{locator} is declared uncommitted, so the snapshot cannot stage it")
    return locator


def _git(repo_root: Path, *args: str) -> bytes:
    """Run one read-only git command and return its raw stdout.

    Args:
        repo_root: The directory to run in.
        *args: The git arguments, without the leading ``git``.

    Returns:
        The command's stdout, undecoded so blob bytes survive intact.

    Raises:
        MigrationSourceUnreadableError: git is missing, timed out, or
            exited non-zero, which means the committed corpus cannot be
            read at all.
    """
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            check=False,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise MigrationSourceUnreadableError(
            f"git {args[0]} could not run: {error.__class__.__name__}"
        ) from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise MigrationSourceUnreadableError(f"git {args[0]} failed: {detail}")
    return completed.stdout


def _tree_blobs(repo_root: Path, *, revision: str, pathspec: str) -> dict[str, str]:
    """Return every blob a revision tracks directly under ``pathspec``.

    Args:
        repo_root: The repository top level.
        revision: The commit to list.
        pathspec: A repo-relative file or ``dir/`` to list one level of.

    Returns:
        Blob object id keyed by repo-relative path. Trees and submodules
        are skipped: only a blob has bytes to stage.
    """
    listing = _git(repo_root, "ls-tree", "-z", "--full-tree", revision, "--", pathspec)
    blobs: dict[str, str] = {}
    for entry in listing.decode("utf-8").split("\0"):
        if not entry:
            continue
        header, path = entry.split("\t", 1)
        _mode, kind, object_id = header.split(" ")
        if kind == "blob":
            blobs[path] = object_id
    return blobs


def repository_revision(start: Path) -> tuple[Path, str]:
    """Return the repository top level containing ``start`` and its HEAD commit.

    Args:
        start: Any directory inside the repository.

    Returns:
        The top-level directory and the full commit id HEAD names.

    Raises:
        MigrationSourceUnreadableError: ``start`` is not inside a git
            repository, or the repository has no commit yet.
    """
    top = Path(_git(start, "rev-parse", "--show-toplevel").decode("utf-8").strip())
    revision = _git(top, "rev-parse", "--verify", "HEAD^{commit}").decode("utf-8").strip()
    return top, revision


def committed_sources(repo_root: Path, *, revision: str) -> tuple[StagedSource, ...]:
    """Return every committed file the snapshot stages at ``revision``.

    Selection is the intersection of two facts: git tracks the path at
    the revision, and the commit policy declares it committed. The
    document and the layered config sit at fixed destinations the
    snapshot layout requires, so their absence is raised rather than
    filtered: a corpus missing its document is not a smaller corpus, it
    is a broken one.

    Args:
        repo_root: The repository top level.
        revision: The commit to read.

    Returns:
        The document, the config, then each committed store ledger sorted
        by filename.

    Raises:
        MigrationSourceUnreadableError: The revision tracks no document
            or no config, or git cannot list the tree.
        ValueError: When a locator the layout requires is uncommitted.
    """
    fixed = _tree_blobs(repo_root, revision=revision, pathspec=LIVE_STATE_LOCATOR)
    fixed |= _tree_blobs(repo_root, revision=revision, pathspec=LIVE_CONFIG_LOCATOR)
    for locator in (LIVE_STATE_LOCATOR, LIVE_CONFIG_LOCATOR):
        if locator not in fixed:
            raise MigrationSourceUnreadableError(
                f"{locator} is not tracked at {revision}, so there is no committed corpus"
            )
    ledgers = _tree_blobs(repo_root, revision=revision, pathspec=f"{LIVE_STORE_LOCATOR}/")
    return (
        StagedSource(
            locator=require_committed(LIVE_STATE_LOCATOR),
            destination=DOCUMENT_LOCATOR,
            object_id=fixed[LIVE_STATE_LOCATOR],
        ),
        StagedSource(
            locator=require_committed(LIVE_CONFIG_LOCATOR),
            destination=f"{CONFIG_DIRECTORY}/{STAGED_CONFIG_FILENAME}",
            object_id=fixed[LIVE_CONFIG_LOCATOR],
        ),
        *(
            StagedSource(
                locator=locator,
                destination=f"{STORE_DIRECTORY}/{locator.rsplit('/', 1)[1]}",
                object_id=object_id,
            )
            for locator, object_id in sorted(ledgers.items())
            if locator.endswith(LEDGER_SUFFIX) and is_committed(locator)
        ),
    )


def staged_registry(*, workspace_key: str, project_key: str) -> dict[str, Any]:
    """Return the one-workspace registry a staged snapshot ships beside its corpus.

    The machine registry is not copied: it names this machine's
    repository paths, and no clone carries it. The snapshot instead holds
    the single workspace the corpus is imported under, which is all the
    importer resolves.

    Args:
        workspace_key: The addressing workspace, already validated.
        project_key: The project the workspace is rooted on.

    Returns:
        The registry document.
    """
    return {
        "active_code": None,
        "repos": {},
        "updated_at": STAGED_REGISTRY_TIMESTAMP,
        "version": "1",
        "workspaces": {
            workspace_key: {
                "home_project_code": project_key,
                "key": workspace_key,
                "member_project_codes": [project_key],
                "revision": 1,
                "title": f"the workspace the staged corpus imports under as {project_key}",
                "updated_at": STAGED_REGISTRY_TIMESTAMP,
            }
        },
    }


def _require_stageable(destination: Path, *, repo_root: Path) -> None:
    """Refuse a destination the staging cannot own outright.

    Args:
        destination: Where the snapshot is to be assembled.
        repo_root: The repository top level.

    Raises:
        MigrationStagingRefusedError: The destination is inside the live
            ``.ea`` tree, or already holds files.
    """
    live_tree = (repo_root / LIVE_TREE_DIRNAME).resolve()
    resolved = destination.resolve()
    if resolved == live_tree or live_tree in resolved.parents:
        raise MigrationStagingRefusedError(
            f"{destination} is inside the live {LIVE_TREE_DIRNAME} tree; stage outside it"
        )
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise MigrationStagingRefusedError(
            f"{destination} already holds files, which the read barrier would pin as corpus"
        )


def _write_staged_json(path: Path, payload: object) -> None:
    """Write ``payload`` as sorted, newline-terminated JSON."""
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def stage_committed_corpus(
    *, repo_root: Path, destination: Path, workspace_key: str, project_key: str
) -> StagedCorpus:
    """Assemble the committed ``.ea`` corpus at HEAD into a snapshot root.

    Every byte is read from git objects rather than from the working
    tree, so the result depends on the revision alone: an uncommitted
    daemon write, a gitignored firehose, or a live writer racing the copy
    cannot reach the snapshot. Nothing under the live ``.ea`` tree is
    opened, for reading or for writing.

    Args:
        repo_root: Any directory inside the repository to stage.
        destination: Where to assemble the snapshot. Created when absent;
            refused when it already holds files.
        workspace_key: The addressing workspace the synthesised registry
            declares, already validated.
        project_key: The project that workspace is rooted on.

    Returns:
        The staged corpus, naming the revision it was read at.

    Raises:
        MigrationSourceUnreadableError: The directory is not in a git
            repository, or HEAD tracks no committed corpus.
        MigrationStagingRefusedError: The destination is inside the live
            tree or is not empty.
        ValueError: A locator the layout requires is declared uncommitted.
    """
    top, revision = repository_revision(repo_root)
    _require_stageable(destination, repo_root=top)
    sources = committed_sources(top, revision=revision)
    (destination / STORE_DIRECTORY).mkdir(parents=True, exist_ok=True)
    (destination / CONFIG_DIRECTORY).mkdir(parents=True, exist_ok=True)
    for source in sources:
        blob = _git(top, "cat-file", "blob", source.object_id)
        (destination / source.destination).write_bytes(blob)
    _write_staged_json(
        destination / REGISTRY_LOCATOR,
        staged_registry(workspace_key=workspace_key, project_key=project_key),
    )
    _write_staged_json(destination / TELEMETRY_LOCATOR, STAGED_TELEMETRY_BODY)
    logger.debug(f"stage_committed_corpus revision={revision} sources={len(sources)}")
    return StagedCorpus(root=destination, revision=revision, sources=sources)


def staged_envelope(corpus: StagedCorpus) -> dict[str, Any]:
    """Return the envelope one staging is reported as.

    Args:
        corpus: The staged corpus.

    Returns:
        The snapshot root, the revision, and each staged locator.
    """
    return {
        "staged_to": str(corpus.root),
        "revision": corpus.revision,
        "sources": [source.locator for source in corpus.sources],
    }
