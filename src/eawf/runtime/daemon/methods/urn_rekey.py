"""Daemon-served URN re-key of one epoch-2 tree onto its single project key.

The plan is computed by :mod:`eawf.kernel.migration.epoch2.urn_rekey`; this
module is the side that writes it. Three surfaces change together: the
generation document, every ledger that gains superseding rows, and the
registry row that files the repository under the project key. They change
under one set of locks -- the root's document lock and the registry lock,
held across the planning read and every write -- so no other writer can
land between the read the plan was taken from and the bytes it appends.

Each superseding row is committed through the ledger-append path and the
document through the rewrite path, so every write is journalled, ordered
by the canonical sequence and announced on the firehose like any other
native commit. The writes run ledgers first, document second, registry
last. A crash between them leaves a tree a second apply finishes: an old
line whose superseding row already landed is no longer effective, so the
re-plan names only what is still outstanding.

The registry row is part of the transaction because the single key is the
whole address: two repositories filed under one project key would mint the
same URN for different records, so a key already held by another
repository is refused before anything is written.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.identity.errors import IdentityError
from eawf.kernel.identity.keys import EntityKind
from eawf.kernel.identity.urn import URN_SCHEME
from eawf.kernel.migration.epoch2.errors import MigrationRuleError
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.migration.epoch2.urn_rekey import UrnRekeyPlan, plan_urn_rekey
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.store.compaction import settle_status_ledgers
from eawf.platform.registry import Registry, WorkspaceRecord
from eawf.runtime.daemon.epoch2_rewrite import commit_document_rewrite
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext
from eawf.runtime.daemon.epoch2_transaction import commit_ledger_append
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.registry import (
    apply_rename,
    load_registry,
    resolve_registry_path,
    write_registry,
)
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)

#: The JSON-RPC method that plans, or with ``apply`` writes, the re-key.
URN_REKEY_METHOD: Final = "migration.urn_rekey"

#: The firehose event the document rewrite of an applied re-key carries.
REKEY_EVENT_NAME: Final = "migration.urn_rekey.applied"


class UrnRekeyRepositoryNotRegisteredError(MigrationRuleError):
    """The tree's repository has no registry row to file under the key."""

    code: ClassVar[str] = "urn_rekey_repository_not_registered"


class UrnRekeyDuplicateProjectKeyError(MigrationRuleError):
    """The project key is already filed for another repository."""

    code: ClassVar[str] = "urn_rekey_duplicate_project_key"


class UrnRekeyParams(BaseModel):
    """The request of one re-key, less the fenced ``repo_root``.

    Attributes:
        project_key: The single key every three-slot URN collapses onto.
        apply: Write the plan; when false the call only reads.
        registry_path: The registry the repository is filed in, or
            ``None`` for the ``EAWF_REGISTRY_PATH`` override or the
            machine registry.
    """

    model_config = ConfigDict(extra="forbid")

    project_key: str
    apply: bool = False
    registry_path: str | None = None


def _same_repository(entry_path: str, repo_root: Path) -> bool:
    """Whether a registry row's path names the repository at *repo_root*."""
    return Path(entry_path).expanduser().resolve() == repo_root.resolve()


def _rekeyed_registry(
    registry: Registry, *, repo_root: Path, project_key: str
) -> tuple[Registry, str] | None:
    """Return the registry with the repository filed under *project_key*.

    Args:
        registry: The registry as read under its lock.
        repo_root: The working tree whose ``.ea`` is being re-keyed.
        project_key: The validated single key.

    Returns:
        ``(registry, previous_code)`` when the row has to move, ``None``
        when the repository is already filed under the key.

    Raises:
        UrnRekeyDuplicateProjectKeyError: Another repository holds the key.
        UrnRekeyRepositoryNotRegisteredError: No row names the repository.
    """
    holder = registry.repos.get(project_key)
    if holder is not None:
        if _same_repository(holder.path, repo_root):
            return None
        raise UrnRekeyDuplicateProjectKeyError(
            f"project key {project_key!r} is already registered for another repository; "
            "choose a key no other registered repository holds"
        )
    codes = sorted(
        code for code, entry in registry.repos.items() if _same_repository(entry.path, repo_root)
    )
    if not codes:
        raise UrnRekeyRepositoryNotRegisteredError(
            f"{repo_root.name} has no registry row to file under {project_key!r}; "
            "register it with eawf repo add first"
        )
    previous = codes[0]
    renamed = apply_rename(registry, repo_id=previous, fields={"new_code": project_key})
    workspaces = {
        key: _renamed_member(record, previous=previous, project_key=project_key)
        for key, record in renamed.workspaces.items()
    }
    return renamed.model_copy(update={"workspaces": workspaces}), previous


def _renamed_member(record: WorkspaceRecord, *, previous: str, project_key: str) -> WorkspaceRecord:
    """Return *record* naming *project_key* wherever it named *previous*."""
    if previous not in record.member_project_codes:
        return record
    members = (record.member_project_codes - {previous}) | {project_key}
    home = project_key if record.home_project_code == previous else record.home_project_code
    return WorkspaceRecord(
        key=record.key,
        title=record.title,
        member_project_codes=members,
        home_project_code=home,
        revision=record.revision + 1,
        updated_at=datetime.now(UTC),
    )


def _envelope(
    plan: UrnRekeyPlan,
    *,
    tree_root: Path,
    registry_move: tuple[Registry, str] | None,
    applied: bool,
) -> dict[str, Any]:
    """Return the answer: what the plan names, and whether it was written."""
    ledgers: dict[str, int] = {}
    for row in plan.rows:
        name = row.ledger.relative_to(tree_root).as_posix()
        ledgers[name] = ledgers.get(name, 0) + 1
    writes = len(plan.rows) + int(plan.document is not None) + int(registry_move is not None)
    return {
        "project_key": plan.project_key,
        "applied": applied,
        "pending": 0 if applied else writes,
        "rows": len(plan.rows),
        "ledgers": ledgers,
        "document": plan.document is not None,
        "registry": None
        if registry_move is None
        else {"from": registry_move[1], "to": plan.project_key},
    }


def run_urn_rekey(
    context: Epoch2RootContext, params: UrnRekeyParams, *, at: datetime
) -> dict[str, Any]:
    """Plan the re-key of *context*'s tree and, when asked, write it.

    Args:
        context: The native context of the tree to re-key.
        params: The validated request.
        at: The instant every superseding row records.

    Returns:
        The answer: ``pending`` counts the writes still outstanding,
        ``rows`` the ledger rows planned or appended, ``document`` and
        ``registry`` whether those two surfaces change.

    Raises:
        IdentityError: The project key is not a valid symbol key.
        UrnRekeyDuplicateProjectKeyError: Another repository holds the key;
            nothing was written.
        UrnRekeyRepositoryNotRegisteredError: The repository has no
            registry row; nothing was written.
        TransactionRefusedError: A superseding row or the rewritten
            document carries a leak shape.
        NativeAuthorityRequiredError: The tree is not an epoch-2 tree.
    """
    tree_root = context.identity.tree_root
    registry_path = resolve_registry_path(params.registry_path)
    if not params.apply:
        document_path = _document_path(context.require_selected_generation())
        plan = plan_urn_rekey(document_path, recorded_at=at, project_key=params.project_key)
        move = _rekeyed_registry(
            load_registry(registry_path), repo_root=tree_root.parent, project_key=plan.project_key
        )
        return _envelope(plan, tree_root=tree_root, registry_move=move, applied=False)
    project_urn = (
        f"{URN_SCHEME}{params.project_key}/{EntityKind.PROJECT.value}/{params.project_key}"
    )
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with context.session([project_urn]) as session, portalock.acquire(registry_path):
        # The local Run ledger is seeded from the committed one on first
        # touch; planning before it exists would miss every Run row.
        settle_status_ledgers(session.document_path)
        plan = plan_urn_rekey(session.document_path, recorded_at=at, project_key=params.project_key)
        move = _rekeyed_registry(
            load_registry(registry_path), repo_root=tree_root.parent, project_key=plan.project_key
        )
        for row in plan.rows:
            commit_ledger_append(session, row.record)
        if plan.document is not None:
            commit_document_rewrite(
                session,
                new_document=plan.document,
                event_name=REKEY_EVENT_NAME,
                event_fields={"project_key": plan.project_key, "rows": len(plan.rows)},
                now=at,
            )
        if move is not None:
            write_registry(registry_path, move[0])
    logger.info(
        f"run_urn_rekey root={context.identity.root_id} rows={len(plan.rows)} "
        f"document={plan.document is not None} registry_moved={move is not None}"
    )
    return _envelope(plan, tree_root=tree_root, registry_move=move, applied=True)


def _document_path(authority: RootAuthority) -> Path:
    """Return the selected generation's document of *authority*."""
    assert authority.target is not None, "an epoch-2 answer always carries its target"
    assert authority.generation_id is not None, "an epoch-2 answer always names a generation"
    return authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT


@native_mutator(URN_REKEY_METHOD)
async def urn_rekey(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Serve one URN re-key plan, or its apply, for the fenced tree.

    Args:
        ctx: Server context, whose native root context serialises the
            apply with every other writer on the tree.
        params: The request, validated through :class:`UrnRekeyParams`.
        authority: The epoch-2 answer of the addressed tree.

    Returns:
        The answer of :func:`run_urn_rekey`.

    Raises:
        DaemonValidationError: The request does not parse, the key is not
            a symbol key, or the re-key is refused; a refusal carries its
            stable code.
    """
    args = native_params(UrnRekeyParams, params)
    context = ctx.native_root_context(authority.root)
    try:
        return await asyncio.to_thread(run_urn_rekey, context, args, at=datetime.now(UTC))
    except (MigrationRuleError, IdentityError) as error:
        raise DaemonValidationError(f"validation_failed: {error.code}: {error}") from error


__all__ = [
    "URN_REKEY_METHOD",
    "UrnRekeyDuplicateProjectKeyError",
    "UrnRekeyParams",
    "UrnRekeyRepositoryNotRegisteredError",
    "run_urn_rekey",
    "urn_rekey",
]
