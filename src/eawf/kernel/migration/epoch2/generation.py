"""Building a generation, reading it back, and the two-file atomic select.

A generation is a complete epoch-2 tree -- document, ledgers and derived
indexes -- built beside the one in use rather than over it. Nothing the
cutover writes here can damage the tree that is currently being read,
which is what makes the whole apply abandonable right up to the select.

The select is two writes and their order is the contract. First the
selection pointer is replaced atomically, naming the generation the tree
should read from. Then, and only then, the epoch marker is written.
Readers consult the marker to decide which epoch they are in, so the
window between the two writes is a tree that has a complete new
generation ready and is still, correctly, epoch 1. A crash in that window
costs a re-run, not a repair.

The generation is built twice into two staging directories and compared
byte for byte before either is published. The import is supposed to be a
function of the pinned source; building it once proves the writer ran,
building it twice proves the writer is that function. Only then is one
staging directory renamed into place, which is a single atomic operation
rather than a file-by-file copy an interruption could leave half done.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import Field

from eawf.kernel.migration.epoch2.canary import DisposableTarget
from eawf.kernel.migration.epoch2.cutover import (
    require_document_holds_only_work_in_flight,
    stage_cutover,
)
from eawf.kernel.migration.epoch2.errors import (
    MigrationReadSmokeFailedError,
    MigrationValidationDivergedError,
)
from eawf.kernel.migration.epoch2.manifest import MigrationManifest
from eawf.kernel.migration.epoch2.plan_mode import MigrationPlan
from eawf.kernel.migration.epoch2.rules import StrictMigrationModel
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import LEDGER_COLLECTIONS, StorageTier

logger = logging.getLogger(__name__)


#: The shape of a generation identifier. Derived from the manifest digest
#: rather than from a clock or a counter, so two applies of one approved
#: plan address the same generation and the second can tell it is already
#: built.
GENERATION_ID_PATTERN: Final = r"^gen-[0-9a-f]{16}$"

#: How many hex characters of the manifest digest name the generation.
GENERATION_ID_WIDTH: Final = 16

#: The document file inside a generation directory.
GENERATION_DOCUMENT: Final = "state.json"

#: The prefix of the two staging directories a build uses. A leading dot
#: keeps them out of the generation namespace, so a crashed build can
#: never be mistaken for a generation.
STAGING_PREFIX: Final = ".staging"


class GenerationSelection(StrictMigrationModel):
    """The pointer naming the generation the tree reads from.

    Attributes:
        schema_version: Always ``"1"``.
        generation_id: The selected generation's directory name.
        manifest_digest: The content digest of the manifest that built
            it, which is what a second apply compares against to decide
            it has nothing to do.
        approval_digest: The plan digest the operator approved.
        placement_digest: The tier placement the build measured.
        selected_at: When the pointer was replaced.
    """

    schema_version: Literal["1"]
    generation_id: Annotated[str, Field(pattern=GENERATION_ID_PATTERN)]
    manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    approval_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    placement_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    selected_at: datetime


class EpochMarker(StrictMigrationModel):
    """The file whose presence means readers are in epoch 2.

    Attributes:
        schema_version: Always ``"1"``.
        epoch: Always ``2``.
        generation_id: The generation the marker was written for.
        manifest_digest: The manifest that built it.
        written_at: When the marker was written, which is the last
            durable act of the apply.
    """

    schema_version: Literal["1"]
    epoch: Literal[2]
    generation_id: Annotated[str, Field(pattern=GENERATION_ID_PATTERN)]
    manifest_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    written_at: datetime


def generation_id_for(manifest_digest: str) -> str:
    """Return the generation identifier one manifest digest addresses.

    Args:
        manifest_digest: The manifest's content digest.

    Returns:
        ``gen-`` plus the digest's first :data:`GENERATION_ID_WIDTH` hex
        characters.

    Raises:
        ValueError: The digest is shorter than the identifier needs, so
            the generation would be addressed by a truncated name.
    """
    if len(manifest_digest) < GENERATION_ID_WIDTH:
        raise ValueError(
            f"a manifest digest of {len(manifest_digest)} characters cannot name a "
            f"generation, which needs {GENERATION_ID_WIDTH}"
        )
    return f"gen-{manifest_digest[:GENERATION_ID_WIDTH]}"


def atomic_write_json(path: Path, payload: StrictMigrationModel) -> None:
    """Replace ``path`` with ``payload`` in one indivisible step.

    A temporary file in the same directory is written and fsynced, then
    renamed over the target. A reader therefore sees either the old file
    or the new one, never a partial write -- which is what makes the
    select and the marker atomic rather than merely quick.

    Args:
        path: The file to replace.
        payload: The model to serialise.

    Raises:
        OSError: The write or the rename failed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_selection(target: DisposableTarget) -> GenerationSelection | None:
    """Return the selection pointer, or ``None`` when nothing is selected.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The pointer, or ``None`` before the first select.

    Raises:
        ValidationError: The pointer exists but does not satisfy the
            contract, which is a tree nobody can say what it reads from.
        json.JSONDecodeError: The pointer is not JSON.
    """
    path = target.selection_path
    if not path.exists():
        return None
    return GenerationSelection.model_validate(json.loads(path.read_text("utf-8")))


def read_marker(target: DisposableTarget) -> EpochMarker | None:
    """Return the epoch marker, or ``None`` when the tree is still epoch 1.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The marker, or ``None`` when it has not been written.

    Raises:
        ValidationError: The marker exists but does not satisfy the
            contract.
        json.JSONDecodeError: The marker is not JSON.
    """
    path = target.marker_path
    if not path.exists():
        return None
    return EpochMarker.model_validate(json.loads(path.read_text("utf-8")))


def tree_digests(root: Path) -> dict[str, str]:
    """Return one digest per file under ``root``, keyed by relative path.

    Args:
        root: The directory to walk.

    Returns:
        A mapping from relative POSIX path to sha256 hex digest. A file
        that appears or disappears changes the key set, so comparing two
        of these catches a created file as well as an edited one.
    """
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _stage_once(
    *,
    plan: MigrationPlan,
    snapshot_root: Path,
    allowlist_path: Path,
    staging_root: Path,
    recorded_at: datetime,
) -> MigrationManifest:
    """Build one complete generation into an empty staging directory."""
    if staging_root.exists():
        shutil.rmtree(staging_root)
    return stage_cutover(
        plan=plan,
        snapshot_root=snapshot_root,
        allowlist_path=allowlist_path,
        state_path=staging_root / GENERATION_DOCUMENT,
        recorded_at=recorded_at,
    )


def build_generation(
    *,
    target: DisposableTarget,
    plan: MigrationPlan,
    snapshot_root: Path,
    allowlist_path: Path,
    recorded_at: datetime,
) -> tuple[str, MigrationManifest]:
    """Build the plan's generation twice, compare, and publish one copy.

    Args:
        target: The fence-cleared target tree.
        plan: The plan whose digest the operator approved.
        snapshot_root: The staging directory holding the epoch-1 corpus.
        allowlist_path: Location of the allowed-legacy-symbol allowlist.
        recorded_at: The timestamp every written row records.

    Returns:
        ``(generation_id, manifest)`` for the published generation, with
        the manifest at the ``staged`` rollback boundary.

    Raises:
        MigrationValidationDivergedError: The two builds over one pinned
            revision left different bytes, so the import is not a
            function of the source.
        MigrationSourceChangedError: The corpus moved since the plan.
        MigrationTerminalInDocumentError: The built document retains a
            record that is not work in flight.
        MigrationHomePathLeakError: The built tree carries a concrete
            home-directory path.
        OSError: A staging directory could not be written or renamed.
    """
    first_root = target.generations_dir / f"{STAGING_PREFIX}-a"
    second_root = target.generations_dir / f"{STAGING_PREFIX}-b"
    first = _stage_once(
        plan=plan,
        snapshot_root=snapshot_root,
        allowlist_path=allowlist_path,
        staging_root=first_root,
        recorded_at=recorded_at,
    )
    second = _stage_once(
        plan=plan,
        snapshot_root=snapshot_root,
        allowlist_path=allowlist_path,
        staging_root=second_root,
        recorded_at=recorded_at,
    )
    _require_builds_identical(first_root=first_root, second_root=second_root)
    if first.manifest_digest != second.manifest_digest:
        raise MigrationValidationDivergedError(
            f"the two staging imports sealed different manifests: "
            f"{first.manifest_digest} then {second.manifest_digest}"
        )

    generation_id = generation_id_for(first.manifest_digest)
    destination = target.generation_path(generation_id)
    if destination.exists():
        shutil.rmtree(destination)
    os.replace(first_root, destination)
    shutil.rmtree(second_root)
    logger.info(
        f"build_generation generation={generation_id} manifest_digest={first.manifest_digest[:12]}"
    )
    return generation_id, first


def _require_builds_identical(*, first_root: Path, second_root: Path) -> None:
    """Refuse two staging imports that did not leave the same bytes.

    Args:
        first_root: The first staging directory.
        second_root: The second.

    Raises:
        MigrationValidationDivergedError: The trees differ. The message
            names the locators that disagree, because a difference an
            operator cannot locate is one they cannot explain.
    """
    first = tree_digests(first_root)
    second = tree_digests(second_root)
    if first == second:
        return
    differing = sorted(
        locator for locator in set(first) | set(second) if first.get(locator) != second.get(locator)
    )
    raise MigrationValidationDivergedError(
        f"two staging imports over one pinned revision left {len(differing)} differing "
        f"files, so the import is not a function of the source: {', '.join(differing)}"
    )


def read_smoke(*, target: DisposableTarget, generation_id: str, manifest: MigrationManifest) -> int:
    """Read a built generation back through the public readers.

    Args:
        target: The fence-cleared target tree.
        generation_id: The generation to read.
        manifest: The manifest the build produced, whose tier placement
            the read is checked against.

    Returns:
        How many records were read across the document and the ledgers.

    Raises:
        MigrationReadSmokeFailedError: The generation holds no placement
            to check against, or the counts on disk disagree with the
            manifest.
        MigrationTerminalInDocumentError: The document retains a record
            that is not work in flight.
        LedgerTornTailError: A ledger ends mid-line.
    """
    placement = manifest.tier_placement
    if placement is None:
        raise MigrationReadSmokeFailedError(
            f"{generation_id} was built but its manifest reports no tier placement, so "
            "there is nothing to read the tree back against"
        )
    state_path = target.generation_path(generation_id) / GENERATION_DOCUMENT
    document = read_document(state_path)
    in_document = sum(len(document_rows(document, collection)) for collection in LEDGER_COLLECTIONS)
    in_ledgers = sum(
        len(read_ledger_records(ledger_path(state_path, collection)))
        for collection in placement.indexed_collections
    )
    _require_counts_agree(
        generation_id=generation_id,
        placement_document=placement.records_in(StorageTier.DOCUMENT),
        placement_ledger=placement.records_in(StorageTier.LEDGER),
        in_document=in_document,
        in_ledgers=in_ledgers,
    )
    require_document_holds_only_work_in_flight(state_path)
    logger.info(
        f"read_smoke generation={generation_id} document_records={in_document} "
        f"ledger_records={in_ledgers}"
    )
    return in_document + in_ledgers


def _require_counts_agree(
    *,
    generation_id: str,
    placement_document: int,
    placement_ledger: int,
    in_document: int,
    in_ledgers: int,
) -> None:
    """Refuse a generation whose readers disagree with its manifest.

    Args:
        generation_id: The generation being read.
        placement_document: Document records the manifest claims.
        placement_ledger: Ledger records the manifest claims.
        in_document: Document records the reader found.
        in_ledgers: Ledger records the reader found.

    Raises:
        MigrationReadSmokeFailedError: Either count disagrees. Both are
            reported, so one run names every discrepancy.
    """
    mismatches = [
        f"{name}: manifest {claimed}, read {found}"
        for name, claimed, found in (
            ("document", placement_document, in_document),
            ("ledger", placement_ledger, in_ledgers),
        )
        if claimed != found
    ]
    if mismatches:
        raise MigrationReadSmokeFailedError(
            f"{generation_id} does not read back as the manifest describes it: "
            f"{'; '.join(mismatches)}"
        )


def select_generation(
    *,
    target: DisposableTarget,
    generation_id: str,
    manifest: MigrationManifest,
    approval_digest: str,
    selected_at: datetime,
) -> GenerationSelection:
    """Point the tree at one built generation, atomically.

    Args:
        target: The fence-cleared target tree.
        generation_id: The generation to select.
        manifest: The manifest that built it, at the ``staged`` boundary.
        approval_digest: The plan digest the operator approved.
        selected_at: When the pointer is replaced.

    Returns:
        The pointer that was written.

    Raises:
        MigrationReadSmokeFailedError: The manifest reports no placement,
            so the pointer could not record which tree it names.
        OSError: The pointer could not be written or renamed.
    """
    placement = manifest.tier_placement
    if placement is None:
        raise MigrationReadSmokeFailedError(
            f"{generation_id} cannot be selected from a manifest with no tier placement"
        )
    selection = GenerationSelection(
        schema_version="1",
        generation_id=generation_id,
        manifest_digest=manifest.manifest_digest,
        approval_digest=approval_digest,
        placement_digest=placement.placement_digest,
        selected_at=selected_at,
    )
    atomic_write_json(target.selection_path, selection)
    logger.info(f"select_generation generation={generation_id}")
    return selection


def write_marker(
    *, target: DisposableTarget, generation_id: str, manifest_digest: str, written_at: datetime
) -> EpochMarker:
    """Write the epoch marker, which is the apply's last durable act.

    Args:
        target: The fence-cleared target tree.
        generation_id: The selected generation.
        manifest_digest: The manifest that built it.
        written_at: When the marker is written.

    Returns:
        The marker that was written.

    Raises:
        OSError: The marker could not be written or renamed.
    """
    marker = EpochMarker(
        schema_version="1",
        epoch=2,
        generation_id=generation_id,
        manifest_digest=manifest_digest,
        written_at=written_at,
    )
    atomic_write_json(target.marker_path, marker)
    logger.info(f"write_marker generation={generation_id}")
    return marker


def generation_ids(target: DisposableTarget) -> tuple[str, ...]:
    """Return every built generation's identifier, in name order.

    Args:
        target: The fence-cleared target tree.

    Returns:
        The directory names under ``generations/`` that name a
        generation. A crashed build's staging directory is excluded by
        its leading dot, so it is never mistaken for one.
    """
    directory = target.generations_dir
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            item.name
            for item in directory.iterdir()
            if item.is_dir() and not item.name.startswith(".")
        )
    )


__all__ = [
    "GENERATION_DOCUMENT",
    "GENERATION_ID_PATTERN",
    "STAGING_PREFIX",
    "EpochMarker",
    "GenerationSelection",
    "atomic_write_json",
    "build_generation",
    "generation_id_for",
    "generation_ids",
    "read_marker",
    "read_selection",
    "read_smoke",
    "select_generation",
    "tree_digests",
    "write_marker",
]
