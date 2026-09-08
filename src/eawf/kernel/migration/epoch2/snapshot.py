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
from collections.abc import Iterator
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from eawf.kernel.migration.epoch2.errors import (
    MigrationDuplicateKeyError,
    MigrationSourceMutatedError,
    MigrationSourceUnreadableError,
)
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel, rule_digest

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
