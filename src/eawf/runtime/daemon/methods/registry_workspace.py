"""``registry.workspace.*`` JSON-RPC methods: create / get / list / membership.

The daemon is the sole canonical mutator of ``~/.eawf/registry.json``,
so the workspace records that qualify URNs are created and edited here
rather than in the CLI. The handlers are a thin transport shell: every
rule they enforce lives in :mod:`eawf.platform.registry.workspace`, which
the CLI's daemonless fallback calls with the same arguments, so the two
arms cannot drift.

Write lifecycle mirrors ``registry.update``: portalock the registry
file, load it into a typed :class:`~eawf.platform.registry.Registry`,
apply the pure library helper, re-validate by round-trip, atomic-write,
then publish a ``registry_updated`` envelope so dashboard subscribers
re-read. The envelope reuses the existing registry payload shape - its
``repo_id`` carries the workspace's home project code and ``fields``
carries the workspace detail - so no subscriber needs a new kind to
notice that the registry moved.

These handlers live in their own module rather than alongside
``registry.update`` because the workspace surface has its own params,
its own compare-and-set rule, and its own error vocabulary.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.store.envelope import Envelope
from eawf.platform.registry import (
    Registry,
    RegistryReadError,
    WorkspaceMutationError,
    WorkspaceRecord,
    create_workspace,
    default_registry_path,
    get_workspace,
    list_workspaces,
    read_registry,
    update_membership,
)
from eawf.runtime.daemon.methods import MethodContext, register

logger = logging.getLogger(__name__)


# ---- Params + Result models ------------------------------------------------


class WorkspaceReadParams(BaseModel):
    """Params shared by the read-only workspace handlers.

    Attributes:
        key: Workspace key to look up. Required by ``get``, ignored by
            ``list``.
        registry_path: Optional registry-file override so tests and the
            ``--registry-path`` flag never touch the real registry.
    """

    model_config = ConfigDict(extra="forbid")
    key: str | None = None
    registry_path: str | None = None


class WorkspaceCreateParams(BaseModel):
    """Params for :func:`create`.

    Attributes:
        record: The workspace payload, validated against
            :class:`~eawf.platform.registry.WorkspaceRecord` before any
            lock is taken.
        registry_path: Optional registry-file override.
    """

    model_config = ConfigDict(extra="forbid")
    record: WorkspaceRecord
    registry_path: str | None = None


class WorkspaceMembershipParams(BaseModel):
    """Params for :func:`update_membership_method`.

    Attributes:
        key: Workspace to edit.
        add: Project codes to include.
        remove: Project codes to drop.
        expected_revision: Revision the caller read, for the
            compare-and-set. Omit to force the write through.
        registry_path: Optional registry-file override.
    """

    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1)
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    expected_revision: int | None = None
    registry_path: str | None = None


class WorkspaceResult(BaseModel):
    """Result of the single-record handlers.

    Attributes:
        workspace: The record as a JSON-mode dict.
        registry_path: The on-disk registry the handler used.
        envelope: The published ``registry_updated`` envelope, or
            ``None`` for the read-only handlers.
    """

    model_config = ConfigDict(extra="forbid")
    workspace: dict[str, Any]
    registry_path: str
    envelope: dict[str, Any] | None = None


class WorkspaceListResult(BaseModel):
    """Result of :func:`list_method`."""

    model_config = ConfigDict(extra="forbid")
    workspaces: list[dict[str, Any]]
    registry_path: str


# ---- Shared internals -------------------------------------------------------
# The path resolver and the tolerant loader below repeat what
# ``registry.update`` does. The method package forbids reaching into a
# sibling module's private names, and hoisting them into a shared helper
# would restructure a module this change does not otherwise touch, so the
# two short functions are stated here instead.


def _resolve_registry_path(override: str | None = None) -> Path:
    """Resolve the registry path, highest-precedence source first.

    Order: the caller's *override*, then ``EAWF_REGISTRY_PATH`` (the
    in-process test seam), then the user default. Matches the
    precedence ``registry.update`` applies, so a caller cannot land on
    a different file depending on which RPC it used.
    """
    if override:
        return Path(override)
    env_override = os.environ.get("EAWF_REGISTRY_PATH")
    if env_override:
        return Path(env_override)
    return default_registry_path()


def _load_registry(registry_path: Path) -> Registry:
    """Load *registry_path*, treating a missing file as an empty registry.

    Raises:
        ValueError: When the file exists but cannot be read or
            validated; the server maps it onto ``-32602``.
    """
    try:
        return read_registry(path=registry_path)
    except RegistryReadError as exc:
        msg = str(exc)
        if "not found" in msg:
            return Registry()
        raise ValueError(f"validation_failed: registry unreadable: {msg}") from exc


def _as_value_error(exc: WorkspaceMutationError) -> ValueError:
    """Map a library refusal onto the daemon's ``validation_failed`` shape.

    The stable code is kept at the front of the message so a client can
    route on it without a structured error channel.
    """
    return ValueError(f"validation_failed: {exc.code}: {exc}")


def _build_envelope(
    *,
    operation: str,
    record: WorkspaceRecord,
    registry_path: Path,
    fields: dict[str, Any],
) -> Envelope:
    """Build the ``REGISTRY_UPDATED`` envelope for a workspace mutation."""
    return Envelope(
        schema_version="1.0",
        id=f"REG-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.REGISTRY_UPDATED,
        scope_id=None,
        created_at=datetime.now(UTC),
        updated_at=None,
        summary=f"registry.workspace.{operation} key={record.key}",
        payload={
            "operation": f"workspace_{operation}",
            "repo_id": record.home_project_code,
            "registry_path": str(registry_path),
            "fields": fields,
        },
        blob_refs=[],
        artifact_ids=[],
    )


def _publish(ctx: MethodContext, envelope: Envelope) -> None:
    """Publish *envelope* on the bus when one is wired."""
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id


def _commit(
    ctx: MethodContext,
    *,
    operation: str,
    registry_path: str | None,
    mutate: Any,
) -> dict[str, Any]:
    """Lock, mutate, re-validate, write, publish - the shared write path.

    Args:
        ctx: Server context.
        operation: Short operation name for the envelope summary.
        registry_path: Optional registry-file override.
        mutate: Callable taking the loaded :class:`Registry` and
            returning ``(updated_registry, record)``.

    Returns:
        Dict matching :class:`WorkspaceResult`.

    Raises:
        ValueError: When the library refuses the mutation; the server
            maps it onto ``-32602``.
    """
    from eawf.runtime.lock import portalock

    resolved = _resolve_registry_path(registry_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(resolved, timeout=5.0):
            registry = _load_registry(resolved)
            try:
                updated, record = mutate(registry)
            except WorkspaceMutationError as exc:
                raise _as_value_error(exc) from exc
            payload = Registry.model_validate(updated.model_dump(mode="json")).model_dump(
                mode="json"
            )
            atomic_write_json_locked(resolved, payload)
            envelope = _build_envelope(
                operation=operation,
                record=record,
                registry_path=resolved,
                fields={
                    "workspace_key": record.key,
                    "member_project_codes": sorted(record.member_project_codes),
                    "revision": record.revision,
                },
            )
            _publish(ctx, envelope)
            logger.info(
                f"registry_workspace ok operation={operation} key={record.key!r} "
                f"revision={record.revision} envelope_id={envelope.id!r}"
            )
            return WorkspaceResult(
                workspace=record.model_dump(mode="json"),
                registry_path=str(resolved),
                envelope=envelope.model_dump(mode="json"),
            ).model_dump(mode="json")
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


# ---- Handlers ---------------------------------------------------------------


@register("registry.workspace.create")
async def create(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Register a new workspace record.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`WorkspaceCreateParams`.

    Returns:
        Dict matching :class:`WorkspaceResult`.

    Raises:
        ValueError: On a malformed record or a key that already exists.
    """
    try:
        args = WorkspaceCreateParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    def _mutate(registry: Registry) -> tuple[Registry, WorkspaceRecord]:
        return create_workspace(registry, record=args.record), args.record

    return _commit(
        ctx,
        operation="create",
        registry_path=args.registry_path,
        mutate=_mutate,
    )


@register("registry.workspace.update_membership")
async def update_membership_method(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Add or drop members on a registered workspace.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`WorkspaceMembershipParams`.

    Returns:
        Dict matching :class:`WorkspaceResult` carrying the bumped
        revision.

    Raises:
        ValueError: On malformed params, an unregistered key, a lost
            compare-and-set, or a membership the record model rejects.
    """
    try:
        args = WorkspaceMembershipParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    def _mutate(registry: Registry) -> tuple[Registry, WorkspaceRecord]:
        try:
            updated = update_membership(
                registry,
                key=args.key,
                add=args.add,
                remove=args.remove,
                expected_revision=args.expected_revision,
            )
        except ValidationError as exc:
            raise ValueError(f"validation_failed: {exc}") from exc
        return updated, updated.workspaces[args.key]

    return _commit(
        ctx,
        operation="update_membership",
        registry_path=args.registry_path,
        mutate=_mutate,
    )


@register("registry.workspace.get")
async def get(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one registered workspace record.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`WorkspaceReadParams`;
            ``key`` is required here.

    Returns:
        Dict matching :class:`WorkspaceResult` with no envelope.

    Raises:
        ValueError: When ``key`` is missing or not registered.
    """
    try:
        args = WorkspaceReadParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    if not args.key:
        raise ValueError("validation_failed: 'key' field required for registry.workspace.get")
    resolved = _resolve_registry_path(args.registry_path)
    registry = _load_registry(resolved)
    try:
        record = get_workspace(registry, args.key)
    except WorkspaceMutationError as exc:
        raise _as_value_error(exc) from exc
    return WorkspaceResult(
        workspace=record.model_dump(mode="json"),
        registry_path=str(resolved),
    ).model_dump(mode="json")


@register("registry.workspace.list")
async def list_method(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return every registered workspace, ordered by key.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`WorkspaceReadParams`.

    Returns:
        Dict matching :class:`WorkspaceListResult`.

    Raises:
        ValueError: When the params payload is malformed.
    """
    try:
        args = WorkspaceReadParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc
    resolved = _resolve_registry_path(args.registry_path)
    registry = _load_registry(resolved)
    return WorkspaceListResult(
        workspaces=[record.model_dump(mode="json") for record in list_workspaces(registry)],
        registry_path=str(resolved),
    ).model_dump(mode="json")


__all__ = [
    "WorkspaceCreateParams",
    "WorkspaceListResult",
    "WorkspaceMembershipParams",
    "WorkspaceReadParams",
    "WorkspaceResult",
    "create",
    "get",
    "list_method",
    "update_membership_method",
]
