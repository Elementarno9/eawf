"""``wal.*`` JSON-RPC method definitions for operator-driven WAL admin.

W03 ships the request/response shapes + handler implementations. The
main dispatcher does NOT import this module yet — W09 wires it in when
``state.mutate`` is delivered, so the admin surface ships together with
the mutator that produces records to inspect.

Importing this module manually (e.g. in tests) is the way to populate
the registry without disturbing W01's lean ``daemon.*`` surface.

The handlers operate directly on a WAL directory path passed via
``params.wal_dir`` so the same code path serves both the daemon (where
the path comes from :mod:`eawf.daemon.runtime_dir`) and the operator
CLI (which targets the local WAL directly when the daemon is down).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from eawf.daemon import wal
from eawf.daemon.methods import MethodContext, register

logger = logging.getLogger(__name__)


class _WalDirParams(BaseModel):
    """Shared params shape: every wal.* method takes the WAL directory."""

    model_config = ConfigDict(extra="forbid")
    wal_dir: str = Field(min_length=1)


class _WalPendingParams(_WalDirParams):
    """Params for :func:`list_pending`."""


class _WalPoisonedParams(_WalDirParams):
    """Params for :func:`list_poisoned`."""


class WalGcParams(_WalDirParams):
    """Params for :func:`gc`.

    Attributes:
        max_age_seconds: Drop ``.fsynced.json`` files older than this
            threshold. Defaults to the C02 §5.6 retention window of
            one hour.
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
async def list_pending(_ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return paths of all ``.pending.json`` records under ``params.wal_dir``."""
    args = _WalPendingParams.model_validate(params)
    wal_dir = Path(args.wal_dir)
    paths = wal.list_records(wal_dir, status=wal.WalStatus.PENDING)
    result = WalListResult(count=len(paths), paths=[str(p) for p in paths])
    return result.model_dump(mode="json")


@register("wal.list_poisoned")
async def list_poisoned(_ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return paths of all records under ``<wal_dir>/poisoned/``."""
    args = _WalPoisonedParams.model_validate(params)
    wal_dir = Path(args.wal_dir)
    paths = wal.list_poisoned(wal_dir)
    result = WalListResult(count=len(paths), paths=[str(p) for p in paths])
    return result.model_dump(mode="json")


@register("wal.gc")
async def gc(_ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Unlink ``.fsynced.json`` records older than ``max_age_seconds``."""
    args = WalGcParams.model_validate(params)
    wal_dir = Path(args.wal_dir)
    removed = wal.gc_done_records(wal_dir, max_age_seconds=args.max_age_seconds)
    result = WalGcResult(
        removed_count=len(removed),
        removed_paths=[str(p) for p in removed],
    )
    return result.model_dump(mode="json")


@register("wal.inspect")
async def inspect(_ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the typed :class:`WalRecord` body for a single record id.

    Raises:
        FileNotFoundError: When the record id is not found in any
            status (including ``poisoned/``).
        ValueError: When the record bytes fail schema validation.
    """
    args = WalInspectParams.model_validate(params)
    wal_dir = Path(args.wal_dir)
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
]
