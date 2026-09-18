"""``wal.*`` JSON-RPC method definitions for operator-driven WAL admin.

The daemon server imports this module, so the four admin verbs are live
on every daemon rather than only where a test imported them by hand.

A caller names the WAL it means in one of three ways, and the module
resolves exactly one of them. A raw ``wal_dir`` is the operator CLI's
path: it targets a local WAL directly, daemon or no daemon. A
``repo_root`` is the native path: the WAL of an epoch-2 root is a
namespace under the daemon's own, named by a digest of the tree, so the
caller names the tree and the root context answers with the directory.
Naming neither falls back to the daemon's WAL directory, which is the
epoch-1 surface the operator verbs have always addressed.

Naming both is refused rather than ranked. Two parameters pointing at
two different directories is a caller that does not know which WAL it is
administering, and a garbage-collect run against the wrong one is not
recoverable by asking again.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.epoch2.authority import NativeAuthorityRequiredError
from eawf.runtime.daemon import wal
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.native_guard import EA_DIRNAME

logger = logging.getLogger(__name__)


class _WalDirParams(BaseModel):
    """Shared params shape: every wal.* method names the WAL it administers.

    Attributes:
        wal_dir: A WAL directory addressed directly.
        repo_root: The repository root of an epoch-2 tree whose native WAL
            namespace the root context resolves.
    """

    model_config = ConfigDict(extra="forbid")
    wal_dir: str | None = Field(default=None, min_length=1)
    repo_root: str | None = Field(default=None, min_length=1)


class _WalPendingParams(_WalDirParams):
    """Params for :func:`list_pending`."""


class _WalPoisonedParams(_WalDirParams):
    """Params for :func:`list_poisoned`."""


class WalGcParams(_WalDirParams):
    """Params for :func:`gc`.

    Attributes:
        max_age_seconds: Drop ``.fsynced.json`` files older than this
            threshold. Defaults to the retention window of one hour.
    """

    max_age_seconds: int = Field(default=3600, ge=0, le=30 * 24 * 3600)


class WalInspectParams(_WalDirParams):
    """Params for :func:`inspect`.

    Attributes:
        record_id: WAL record id to inspect. Searches both the live
            statuses and the ``poisoned/`` subdirectory.
    """

    record_id: str = Field(min_length=1)


class WalListResult(BaseModel):
    """Result of ``wal.list_pending`` / ``wal.list_poisoned``.

    The result reports only **file paths** rather than full record
    bodies; operators inspect a specific record via ``wal.inspect``.
    Listing bodies inline would balloon the JSON-RPC envelope for the
    pathological "thousands of poisoned records" case.
    """

    model_config = ConfigDict(extra="forbid")
    count: int
    paths: list[str]


class WalGcResult(BaseModel):
    """Result of ``wal.gc``."""

    model_config = ConfigDict(extra="forbid")
    removed_count: int
    removed_paths: list[str]


class WalInspectResult(BaseModel):
    """Result of ``wal.inspect`` — the typed :class:`WalRecord` body."""

    model_config = ConfigDict(extra="forbid")
    record: dict[str, Any]
    path: str
    status: str


def resolve_admin_wal_dir(ctx: MethodContext, args: _WalDirParams) -> Path:
    """Return the one WAL directory a ``wal.*`` request addresses.

    Args:
        ctx: Server context, which owns the daemon's WAL directory and the
            per-root native contexts.
        args: The already-validated shared params.

    Returns:
        The directory the handler lists, collects or inspects under.

    Raises:
        DaemonValidationError: The request names both a WAL directory and a
            repository root, names neither on a daemon bound to no WAL, or
            names a repository that is not an epoch-2 tree and so has no
            native namespace. Every one is the caller's mistake, which is
            why none of them reaches the wire as an internal error.
        MigrationDualAuthorityError: The named tree's select is not whole.
        RuntimeError: The daemon has no WAL directory to namespace under.
    """
    if args.wal_dir is not None and args.repo_root is not None:
        raise DaemonValidationError(
            "validation_failed: name exactly one of wal_dir / repo_root; naming both leaves "
            "which WAL directory is being administered undecided"
        )
    if args.wal_dir is not None:
        return Path(args.wal_dir)
    if args.repo_root is not None:
        try:
            return ctx.native_root_context(Path(args.repo_root) / EA_DIRNAME).wal_dir
        except NativeAuthorityRequiredError as error:
            raise DaemonValidationError(f"validation_failed: {error.code}: {error}") from error
    if ctx.wal_dir is None:
        raise DaemonValidationError(
            "validation_failed: the request names no wal_dir or repo_root and the daemon is "
            "bound to no WAL directory, so there is nothing to administer"
        )
    return Path(ctx.wal_dir)


def _resolve_path(record_id: str, wal_dir: Path) -> tuple[Path, str] | None:
    """Search the WAL directory for a record by id; return ``(path, status)``."""
    for status in (
        wal.WalStatus.PENDING,
        wal.WalStatus.APPLIED,
        wal.WalStatus.FSYNCED,
    ):
        candidate = wal_dir / f"{record_id}.{status.value}.json"
        if candidate.exists():
            return candidate, status.value
    poisoned = wal_dir / "poisoned" / f"{record_id}.poisoned.json"
    if poisoned.exists():
        return poisoned, wal.WalStatus.POISONED.value
    return None


@register("wal.list_pending")
async def list_pending(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return paths of all ``.pending.json`` records under the addressed WAL."""
    args = _WalPendingParams.model_validate(params)
    wal_dir = resolve_admin_wal_dir(ctx, args)
    paths = wal.list_records(wal_dir, status=wal.WalStatus.PENDING)
    result = WalListResult(count=len(paths), paths=[str(p) for p in paths])
    return result.model_dump(mode="json")


@register("wal.list_poisoned")
async def list_poisoned(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return paths of all records under ``<wal_dir>/poisoned/``."""
    args = _WalPoisonedParams.model_validate(params)
    wal_dir = resolve_admin_wal_dir(ctx, args)
    paths = wal.list_poisoned(wal_dir)
    result = WalListResult(count=len(paths), paths=[str(p) for p in paths])
    return result.model_dump(mode="json")


@register("wal.gc")
async def gc(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Unlink ``.fsynced.json`` records older than ``max_age_seconds``."""
    args = WalGcParams.model_validate(params)
    wal_dir = resolve_admin_wal_dir(ctx, args)
    removed = wal.gc_done_records(wal_dir, max_age_seconds=args.max_age_seconds)
    result = WalGcResult(
        removed_count=len(removed),
        removed_paths=[str(p) for p in removed],
    )
    return result.model_dump(mode="json")


@register("wal.inspect")
async def inspect(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the typed :class:`WalRecord` body for a single record id.

    Raises:
        FileNotFoundError: When the record id is not found in any
            status (including ``poisoned/``).
        ValueError: When the record bytes fail schema validation.
    """
    args = WalInspectParams.model_validate(params)
    wal_dir = resolve_admin_wal_dir(ctx, args)
    located = _resolve_path(args.record_id, wal_dir)
    if located is None:
        raise FileNotFoundError(f"wal record not found: id={args.record_id!r}")
    path, status = located
    record = wal.read_record(path)
    result = WalInspectResult(
        record=record.model_dump(mode="json"),
        path=str(path),
        status=status,
    )
    return result.model_dump(mode="json")


__all__ = [
    "WalGcParams",
    "WalGcResult",
    "WalInspectParams",
    "WalInspectResult",
    "WalListResult",
    "gc",
    "inspect",
    "list_pending",
    "list_poisoned",
    "resolve_admin_wal_dir",
]
