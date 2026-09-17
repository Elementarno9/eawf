"""Provisioning and tearing down a disposable epoch-2 canary repository.

A canary is where native epoch-2 writes are allowed to happen before any
production tree is trusted with them. It is born at epoch 2 rather than
migrated there: provisioning writes the owner's disposable declaration,
an empty generation, the selection pointer and then the epoch marker --
the same four artifacts, in the same order, that a completed cutover
leaves behind -- so the authority resolver grants it epoch 2 for exactly
the reason it would grant a migrated tree.

Three things keep a canary isolated from the operator's real work.

Its tree is fresh. Provisioning refuses a directory that already holds
anything, so a canary can never be an existing repository relabelled,
and tearing one down may remove the whole directory it created.

Its daemon runtime directory is fresh. Provisioning allocates a new one
and records it, so a daemon serving the canary never shares a socket,
lock or write-ahead log with the daemon serving production.

Its registry row is explicit and removable. The row is added under the
canary's own project code, refused if that code already names a
repository, and removed by teardown together with any other row that
still points at the canary's tree -- a teardown that left one would leak
the canary into the explicit registry.

Nothing here writes the registry file. The functions return the updated
:class:`~eawf.platform.registry.models.Registry`, and the caller persists
it through the canonical registry mutator.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.identity import EntityKind, IdentityError, QualifiedUrn
from eawf.kernel.migration.epoch2.canary import (
    CanaryDeclaration,
    DisposableTarget,
    declaration_path,
)
from eawf.kernel.migration.epoch2.cutover import DOCUMENT_SCHEMA_KEY
from eawf.kernel.migration.epoch2.errors import MigrationTargetNotDisposableError
from eawf.kernel.migration.epoch2.generation import (
    GENERATION_DOCUMENT,
    GENERATION_ID_PATTERN,
    GenerationSelection,
    atomic_write_json,
    generation_digest,
    generation_id_for,
    verify_selected_generation,
    write_marker,
)
from eawf.kernel.migration.epoch2.manifest import MANIFEST_SCHEMA_VERSION
from eawf.kernel.migration.epoch2.manifest_rows import TierPlacement
from eawf.kernel.migration.epoch2.rules import rule_digest
from eawf.kernel.state.epoch2.authority import CanaryRepositoryRef, resolve_authority
from eawf.kernel.state.ids import is_project_code
from eawf.kernel.state.writer import atomic_write_json as write_json_record
from eawf.kernel.store.compaction import write_document
from eawf.platform.registry.models import Registry, RegistryRepoEntry
from eawf.runtime.daemon.runtime_dir import pid_path, socket_path

logger = logging.getLogger(__name__)


#: The directory under a repository root that holds the fenced tree.
EA_DIRNAME: Final = ".ea"

#: The project code a canary is provisioned under when none is given.
DEFAULT_CANARY_CODE: Final = "CANARY"

#: Who a provisioned canary's declaration says declared it.
CANARY_DECLARED_BY: Final = "eawf-init"

#: What a provisioned canary's declaration says it is for.
CANARY_PURPOSE: Final = (
    "native epoch-2 canary provisioned by eawf init; the whole repository is throwaway"
)

#: Where the provisioning record lives inside the fenced tree. Under
#: ``local/`` because the runtime directory it names is a fact about one
#: machine, and a clone must not inherit another machine's canary.
PROVISION_RECORD_LOCATOR: Final = "local/epoch2-canary.json"

#: The name prefix of every canary runtime directory. Teardown removes a
#: recorded runtime directory only when its name carries this prefix, so a
#: hand-edited record cannot point the removal at an arbitrary directory.
RUNTIME_DIR_PREFIX: Final = "eawf-canary-"

#: The slot prefixes the canary's URN keys are spelled with. Each key must
#: still fit the bounded symbol grammar, which caps the project code.
WORKSPACE_KEY_PREFIX: Final = "WSP-"
PROJECT_KEY_PREFIX: Final = "PRJ-"
REPOSITORY_KEY_PREFIX: Final = "REP-"


class CanaryProvisionError(ValueError):
    """A canary provisioning or teardown was refused before it wrote anything."""


class CanaryProvision(BaseModel):
    """What one provisioning created, and what teardown must remove.

    Attributes:
        schema_version: Always ``"1"``.
        ref: The typed reference a checkpoint configuration declares the
            canary by.
        root: The canary repository's root directory.
        runtime_dir: The fresh daemon runtime directory allocated for it.
        generation_id: The born-native generation the canary reads from.
        provisioned_at: When the canary was provisioned.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    ref: CanaryRepositoryRef
    root: Path
    runtime_dir: Path
    generation_id: Annotated[str, Field(pattern=GENERATION_ID_PATTERN)]
    provisioned_at: datetime


class CanaryTeardown(BaseModel):
    """What one teardown removed.

    Attributes:
        ref: The canary that was torn down.
        removed_registry_codes: The registry rows removed, in code order.
        runtime_dir_removed: Whether the recorded runtime directory was
            still on disk and was removed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: CanaryRepositoryRef
    removed_registry_codes: tuple[str, ...]
    runtime_dir_removed: bool


def canary_ref(project_code: str) -> CanaryRepositoryRef:
    """Return the reference a canary under ``project_code`` is declared by.

    Args:
        project_code: The canary's project code.

    Returns:
        The reference, whose repository URN addresses the canary inside
        its own workspace and project.

    Raises:
        CanaryProvisionError: The code is not a project code, or is too
            long for the prefixed workspace, project and repository keys to
            fit the symbol grammar.
    """
    if not is_project_code(project_code):
        raise CanaryProvisionError(f"{project_code!r} is not a project code")
    try:
        repository = QualifiedUrn(
            workspace_key=f"{WORKSPACE_KEY_PREFIX}{project_code}",
            project_key=f"{PROJECT_KEY_PREFIX}{project_code}",
            repository_key=f"{REPOSITORY_KEY_PREFIX}{project_code}",
            kind=EntityKind.REPOSITORY,
            entity_key=f"{REPOSITORY_KEY_PREFIX}{project_code}",
        )
    except IdentityError as error:
        raise CanaryProvisionError(
            f"canary code {project_code!r} cannot address a repository once its keys are "
            f"prefixed; use a code of at most 12 characters ({error})"
        ) from error
    return CanaryRepositoryRef(repository=repository, project_code=project_code)


def require_fresh_root(repo_root: Path) -> None:
    """Refuse a canary root that already holds anything.

    Args:
        repo_root: Where the canary repository would be created.

    Raises:
        CanaryProvisionError: The path exists and is not an empty
            directory. A canary is never an existing tree relabelled.
    """
    if not repo_root.exists():
        return
    if not repo_root.is_dir() or any(repo_root.iterdir()):
        raise CanaryProvisionError(
            f"{repo_root.name} already exists and is not an empty directory; a canary is "
            "provisioned only into a fresh directory"
        )


def _write_born_generation(
    target: DisposableTarget, *, ref: CanaryRepositoryRef
) -> tuple[str, str]:
    """Write the canary's empty generation.

    The generation is named by a digest over what it was born from, the
    same way a migrated generation is named by its manifest digest, so
    two canaries under one code are born into the same identifier.

    Returns:
        ``(generation_id, birth_digest)``.
    """
    document = {DOCUMENT_SCHEMA_KEY: MANIFEST_SCHEMA_VERSION}
    birth_digest = rule_digest({"canary": ref.model_dump(mode="json"), "document": document})
    generation_id = generation_id_for(birth_digest)
    state_path = target.generation_path(generation_id) / GENERATION_DOCUMENT
    state_path.parent.mkdir(parents=True, exist_ok=True)
    write_document(state_path, document)
    return generation_id, birth_digest


def _activate(
    target: DisposableTarget, *, generation_id: str, birth_digest: str, activated_at: datetime
) -> None:
    """Select the born generation, then write the marker, in that order.

    Nothing was planned, so the birth digest stands in both the manifest
    and the approval slot: the provisioning invocation approved exactly
    this generation and nothing else.
    """
    state_path = target.generation_path(generation_id) / GENERATION_DOCUMENT
    placement = TierPlacement.of(
        records_by_tier={},
        document_record_count=0,
        document_byte_length=state_path.stat().st_size,
        ledger_byte_length=0,
        indexed_collections=(),
    )
    atomic_write_json(
        target.selection_path,
        GenerationSelection(
            schema_version="1",
            generation_id=generation_id,
            manifest_digest=birth_digest,
            approval_digest=birth_digest,
            placement_digest=placement.placement_digest,
            selected_at=activated_at,
        ),
    )
    write_marker(
        target=target,
        generation_id=generation_id,
        manifest_digest=birth_digest,
        published_digest=generation_digest(target, generation_id=generation_id),
        written_at=activated_at,
    )


def provision_canary(
    *, repo_root: Path, ref: CanaryRepositoryRef, provisioned_at: datetime
) -> CanaryProvision:
    """Make ``repo_root`` a declared, activated epoch-2 canary.

    Args:
        repo_root: The canary repository's root. Its ``.ea`` tree must not
            already carry a declaration; the caller has either just created
            it or laid down the epoch-1 skeleton in it.
        ref: The reference the canary is declared by.
        provisioned_at: When the canary is provisioned.

    Returns:
        The provisioning record, also written into the tree.

    Raises:
        CanaryProvisionError: The tree already carries a declaration.
        MigrationReadSmokeFailedError: The born generation does not read
            back through the public readers.
        RuntimeError: The finished tree does not resolve to epoch 2,
            which would make every claim this function returns false.
        OSError: A file or the runtime directory could not be written.
    """
    root = repo_root.resolve()
    tree_root = root / EA_DIRNAME
    if declaration_path(tree_root).exists():
        raise CanaryProvisionError(f"{root.name} already declares a disposable canary")
    declaration = CanaryDeclaration(
        disposable=True, declared_by=CANARY_DECLARED_BY, purpose=CANARY_PURPOSE
    )
    atomic_write_json(declaration_path(tree_root), declaration)
    target = DisposableTarget.require(tree_root)
    generation_id, birth_digest = _write_born_generation(target, ref=ref)
    _activate(
        target,
        generation_id=generation_id,
        birth_digest=birth_digest,
        activated_at=provisioned_at,
    )
    verify_selected_generation(target, generation_id=generation_id)
    if resolve_authority(tree_root).epoch != 2:
        raise RuntimeError(f"{root.name} was provisioned but does not resolve to epoch 2")

    provision = CanaryProvision(
        ref=ref,
        root=root,
        runtime_dir=Path(tempfile.mkdtemp(prefix=RUNTIME_DIR_PREFIX)),
        generation_id=generation_id,
        provisioned_at=provisioned_at,
    )
    try:
        write_json_record(tree_root / PROVISION_RECORD_LOCATOR, provision.model_dump(mode="json"))
    except OSError:
        shutil.rmtree(provision.runtime_dir, ignore_errors=True)
        raise
    logger.info(
        f"provision_canary code={ref.project_code} generation={generation_id} "
        f"runtime_dir={provision.runtime_dir.name}"
    )
    return provision


def read_provision(repo_root: Path) -> CanaryProvision:
    """Return the provisioning record of the canary at ``repo_root``.

    Args:
        repo_root: The canary repository's root.

    Returns:
        The record.

    Raises:
        CanaryProvisionError: The tree is not a declared canary, carries
            no readable record, or carries a record naming another root.
            Each is a tree teardown must not remove.
    """
    root = repo_root.resolve()
    tree_root = root / EA_DIRNAME
    try:
        DisposableTarget.require(tree_root)
    except MigrationTargetNotDisposableError as error:
        raise CanaryProvisionError(f"{root.name} is not a disposable canary: {error}") from error
    record_path = tree_root / PROVISION_RECORD_LOCATOR
    try:
        provision = CanaryProvision.model_validate_json(record_path.read_bytes())
    except (OSError, ValidationError) as error:
        raise CanaryProvisionError(
            f"{root.name} carries no readable {PROVISION_RECORD_LOCATOR}, so it was not "
            f"provisioned by eawf init ({error.__class__.__name__})"
        ) from error
    if provision.root != root:
        raise CanaryProvisionError(
            f"{root.name}/{PROVISION_RECORD_LOCATOR} names a different root, so this tree "
            "is a copy and is not the canary that record provisioned"
        )
    return provision


def require_code_unregistered(registry: Registry, ref: CanaryRepositoryRef) -> None:
    """Refuse a canary whose code already names a registered repository.

    Args:
        registry: The registry as it is on disk.
        ref: The canary about to be provisioned.

    Raises:
        CanaryProvisionError: The code is taken. A canary never overwrites
            another tree's row, so the check runs before anything is written.
    """
    if ref.project_code in registry.repos:
        raise CanaryProvisionError(
            f"registry code {ref.project_code!r} already names a repository; pick another "
            "canary code"
        )


def register_canary(registry: Registry, provision: CanaryProvision) -> Registry:
    """Return ``registry`` with the canary's repository row added.

    Args:
        registry: The registry as it is on disk.
        provision: The canary to register.

    Returns:
        The updated registry, not yet persisted.

    Raises:
        CanaryProvisionError: The canary's code already names a registered
            repository.
    """
    require_code_unregistered(registry, provision.ref)
    code = provision.ref.project_code
    row = RegistryRepoEntry(
        code=code,
        path=str(provision.root),
        title=f"epoch-2 canary {code}",
        last_seen=provision.provisioned_at,
    )
    return registry.model_copy(update={"repos": {**registry.repos, code: row}})


def unregister_canary(
    registry: Registry, provision: CanaryProvision
) -> tuple[Registry, tuple[str, ...]]:
    """Return ``registry`` without any row that points at the canary.

    Args:
        registry: The registry as it is on disk.
        provision: The canary being torn down.

    Returns:
        ``(registry, removed_codes)``. Every row whose path resolves to the
        canary's root is removed whatever its code, and a row under the
        canary's code that points elsewhere is kept: it belongs to a tree
        registered after the canary took that code.
    """
    removed = tuple(
        sorted(
            code
            for code, row in registry.repos.items()
            if Path(row.path).resolve() == provision.root
        )
    )
    repos = {code: row for code, row in registry.repos.items() if code not in removed}
    active = None if registry.active_code in removed else registry.active_code
    return registry.model_copy(update={"repos": repos, "active_code": active}), removed


def require_no_live_daemon(provision: CanaryProvision) -> None:
    """Refuse a teardown while a daemon may still be serving the canary.

    Args:
        provision: The canary about to be torn down.

    Raises:
        CanaryProvisionError: The canary's runtime directory still holds a
            daemon PID file or socket. Removing the directory under a live
            daemon would orphan the process, so teardown checks this before
            it touches the registry.
    """
    runtime_dir = provision.runtime_dir
    live = [name for name in (pid_path().name, socket_path().name) if (runtime_dir / name).exists()]
    if live:
        raise CanaryProvisionError(
            f"the canary runtime directory still holds {', '.join(live)}; stop its daemon "
            "with EAWF_RUNTIME_DIR set to that directory before tearing the canary down"
        )


def discard_canary(
    provision: CanaryProvision, *, removed_registry_codes: tuple[str, ...]
) -> CanaryTeardown:
    """Remove the canary's tree and runtime directory.

    Call it after the registry rows are persisted as removed, so a failure
    here leaves a tree a second teardown can still find and finish.

    Args:
        provision: The record :func:`read_provision` returned.
        removed_registry_codes: The rows the caller removed, reported back.

    Returns:
        What was removed.

    Raises:
        CanaryProvisionError: A daemon may still be serving the canary.
        OSError: A directory could not be removed.
    """
    require_no_live_daemon(provision)
    runtime_dir = provision.runtime_dir
    owned_runtime = runtime_dir.name.startswith(RUNTIME_DIR_PREFIX) and runtime_dir.is_dir()
    shutil.rmtree(provision.root)
    if owned_runtime:
        shutil.rmtree(runtime_dir)
    logger.info(
        f"discard_canary code={provision.ref.project_code} "
        f"registry_rows={len(removed_registry_codes)} runtime_dir_removed={owned_runtime}"
    )
    return CanaryTeardown(
        ref=provision.ref,
        removed_registry_codes=removed_registry_codes,
        runtime_dir_removed=owned_runtime,
    )


__all__ = [
    "CANARY_DECLARED_BY",
    "CANARY_PURPOSE",
    "DEFAULT_CANARY_CODE",
    "PROVISION_RECORD_LOCATOR",
    "RUNTIME_DIR_PREFIX",
    "CanaryProvision",
    "CanaryProvisionError",
    "CanaryTeardown",
    "canary_ref",
    "discard_canary",
    "provision_canary",
    "read_provision",
    "register_canary",
    "require_code_unregistered",
    "require_fresh_root",
    "require_no_live_daemon",
    "unregister_canary",
]
