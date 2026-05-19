"""``registry.*`` JSON-RPC methods: read / update.

Daemon-side canonical writer for ``~/.eawf/registry.json`` (authority
map row 8). Wires P24-W10 sub-phase c of C02 §7.2: every registry
mutation that previously went through the in-process
``_persist_registry`` helper now proxies through the ``registry.update``
RPC by default (``daemon.proxy_enabled=True``).

Algorithm — mirrors the W09 ``state.mutate`` lifecycle:

1. Idempotency-cache lookup keyed by ``params['idempotency_key']`` (when
   supplied).
2. ``portalock(registry_path, timeout=5)``.
3. Read + parse the JSON registry into a typed :class:`Registry`.
4. Apply the named operation (``add`` / ``remove`` / ``rename``);
   re-validate the candidate via Pydantic round-trip.
5. Atomic-write the new payload (via
   :func:`eawf.state.writer.atomic_write_json_locked`).
6. Build the canonical ``StoreKind.REGISTRY_UPDATED`` envelope +
   publish on the subscription bus.
7. Cache the result; return ``{operation, repo_id, envelope}``.

Per ``feedback_explicit_registry_only`` the registry grows only via
explicit ``add`` / ``remove`` / ``rename`` writes — there is no scan
or walk. The daemon mutator enforces this by accepting only the three
named operations; anything else fails with ``validation_failed``.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.daemon.methods import MethodContext, register
from eawf.registry import (
    Registry,
    RegistryReadError,
    RegistryRepoEntry,
    default_registry_path,
    read_registry,
)
from eawf.state.enums import StoreKind
from eawf.state.writer import atomic_write_json_locked
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


#: TTL for cached idempotency results (seconds).
IDEMPOTENCY_TTL_SECONDS: Final[float] = 60.0


# ---- Params + Result models ------------------------------------------------


class ReadParams(BaseModel):
    """Params for :func:`read`.

    Attributes:
        registry_path: Optional override for the registry file path
            so test harnesses + the ``--registry-path`` CLI flag can
            point the daemon at a non-default file.
    """

    model_config = ConfigDict(extra="forbid")
    registry_path: str | None = None


class ReadResult(BaseModel):
    """Result of :func:`read`."""

    model_config = ConfigDict(extra="forbid")
    registry: dict[str, Any]
    registry_path: str


class UpdateParams(BaseModel):
    """Params for :func:`update`.

    Attributes:
        operation: One of ``add`` / ``remove`` / ``rename``. Daemon
            rejects anything else with ``validation_failed`` so the
            mutator surface stays explicit.
        repo_id: Project-code-shape repo identifier the operation
            targets. ``add`` requires a fresh code; ``remove`` requires
            an existing one; ``rename`` requires both (``repo_id`` =
            existing code, ``fields['new_code']`` = target).
        fields: Operation-specific extras. ``add`` needs ``path`` +
            optional ``title`` + optional ``set_active``; ``remove``
            takes nothing extra; ``rename`` needs ``new_code``.
        idempotency_key: Optional caller-supplied retry key.
        registry_path: Optional override for the registry file path
            so test harnesses + the ``--registry-path`` CLI flag can
            point the daemon at a non-default file. Empty / missing
            falls back to ``EAWF_REGISTRY_PATH`` env or the user's
            ``~/.eawf/registry.json`` default.
    """

    model_config = ConfigDict(extra="forbid")
    operation: str
    repo_id: str = Field(min_length=1)
    fields: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    registry_path: str | None = None


class UpdateResult(BaseModel):
    """Result of :func:`update`."""

    model_config = ConfigDict(extra="forbid")
    operation: str
    repo_id: str
    registry_path: str
    envelope: dict[str, Any]
    idempotent_replay: bool = False


# ---- Idempotency cache ------------------------------------------------------


class _CachedRegistryMutation(BaseModel):
    """One row in the daemon's registry idempotency cache."""

    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any]
    cached_at: float = Field(ge=0.0)


def _idempotency_cache(ctx: MethodContext) -> dict[str, _CachedRegistryMutation]:
    """Return the per-process registry idempotency cache.

    Shares :attr:`MethodContext.idempotency_cache` with the state and
    config mutators — see
    :func:`eawf.daemon.methods.config._idempotency_cache` for the
    rationale.
    """
    if isinstance(ctx.idempotency_cache, dict):
        return ctx.idempotency_cache
    fresh: dict[str, _CachedRegistryMutation] = {}
    ctx.idempotency_cache = fresh
    return fresh


def _evict_expired(cache: dict[str, Any], *, now: float) -> None:
    """Drop entries older than :data:`IDEMPOTENCY_TTL_SECONDS`."""
    expired = [
        k
        for k, v in cache.items()
        if hasattr(v, "cached_at") and now - v.cached_at > IDEMPOTENCY_TTL_SECONDS
    ]
    for k in expired:
        cache.pop(k, None)


# ---- Operation appliers ----------------------------------------------------


def _resolve_registry_path(override: str | None = None) -> Path:
    """Resolve the registry path.

    Precedence (highest first):

    1. ``override`` argument (caller-supplied via RPC param, e.g.
       the CLI ``--registry-path`` flag).
    2. ``EAWF_REGISTRY_PATH`` environment variable (test seam for
       in-process unit tests of the daemon methods).
    3. :func:`default_registry_path` — ``~/.eawf/registry.json``.
    """
    if override:
        return Path(override)
    env_override = os.environ.get("EAWF_REGISTRY_PATH")
    if env_override:
        return Path(env_override)
    return default_registry_path()


def _load_registry(registry_path: Path) -> Registry:
    """Load *registry_path* into a typed :class:`Registry` for mutation.

    Returns a fresh empty :class:`Registry` when the file is missing
    (bootstrap path for the first ``add``). Other read errors raise
    :class:`ValueError` so the daemon maps them onto ``-32602``.
    """
    try:
        return read_registry(path=registry_path)
    except RegistryReadError as exc:
        msg = str(exc)
        if "not found" in msg:
            return Registry()
        raise ValueError(f"validation_failed: registry unreadable: {msg}") from exc


def _apply_add(registry: Registry, *, repo_id: str, fields: dict[str, Any]) -> Registry:
    """Apply an ``add`` operation; idempotent on same code + same path."""
    path = fields.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("validation_failed: 'path' field required for add")
    title = fields.get("title")
    set_active = bool(fields.get("set_active", False))

    existing = registry.repos.get(repo_id)
    if existing is not None and existing.path == path:
        # Idempotent re-add — preserve title; flip active only when requested.
        if set_active and registry.active_code != repo_id:
            return Registry(
                version=registry.version,
                updated_at=datetime.now(UTC),
                active_code=repo_id,
                repos=dict(registry.repos),
            )
        return registry
    if existing is not None and existing.path != path:
        raise ValueError(
            f"validation_failed: repo {repo_id!r} already registered at {existing.path}; "
            f"refusing to overwrite with {path}"
        )
    new_entry = RegistryRepoEntry(
        code=repo_id,
        path=path,
        title=title if isinstance(title, str) and title else None,
        last_seen=datetime.now(UTC),
    )
    new_repos = dict(registry.repos)
    new_repos[repo_id] = new_entry
    return Registry(
        version=registry.version,
        updated_at=datetime.now(UTC),
        active_code=(repo_id if set_active else registry.active_code),
        repos=new_repos,
    )


def _apply_remove(registry: Registry, *, repo_id: str) -> Registry:
    """Apply a ``remove`` operation; rejects when *repo_id* is absent."""
    if repo_id not in registry.repos:
        raise ValueError(f"validation_failed: repo {repo_id!r} not registered")
    new_repos = {k: v for k, v in registry.repos.items() if k != repo_id}
    new_active = None if registry.active_code == repo_id else registry.active_code
    return Registry(
        version=registry.version,
        updated_at=datetime.now(UTC),
        active_code=new_active,
        repos=new_repos,
    )


def _apply_rename(registry: Registry, *, repo_id: str, fields: dict[str, Any]) -> Registry:
    """Apply a ``rename`` operation — re-keys the entry under ``new_code``."""
    new_code = fields.get("new_code")
    if not isinstance(new_code, str) or not new_code:
        raise ValueError("validation_failed: 'new_code' field required for rename")
    if repo_id not in registry.repos:
        raise ValueError(f"validation_failed: repo {repo_id!r} not registered")
    if new_code in registry.repos:
        raise ValueError(f"validation_failed: target code {new_code!r} already registered")
    new_repos = dict(registry.repos)
    old_entry = new_repos.pop(repo_id)
    renamed = RegistryRepoEntry(
        code=new_code,
        path=old_entry.path,
        title=old_entry.title,
        last_seen=old_entry.last_seen,
    )
    new_repos[new_code] = renamed
    new_active = new_code if registry.active_code == repo_id else registry.active_code
    return Registry(
        version=registry.version,
        updated_at=datetime.now(UTC),
        active_code=new_active,
        repos=new_repos,
    )


_OPERATIONS = {"add", "remove", "rename"}


# ---- Envelope construction --------------------------------------------------


def _build_envelope(
    *,
    operation: str,
    repo_id: str,
    registry_path: Path,
    fields: dict[str, Any],
) -> Envelope:
    """Build the canonical ``REGISTRY_UPDATED`` envelope."""
    now = datetime.now(UTC)
    summary = f"registry.update operation={operation} repo_id={repo_id}"
    payload: dict[str, Any] = {
        "operation": operation,
        "repo_id": repo_id,
        "registry_path": str(registry_path),
        "fields": dict(fields),
    }
    return Envelope(
        schema_version="1.0",
        id=f"REG-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.REGISTRY_UPDATED,
        scope_id=None,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


# ---- Handlers ---------------------------------------------------------------


@register("registry.read")
async def read(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the parsed registry JSON.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`ReadParams`.

    Returns:
        Dict matching :class:`ReadResult` — full registry payload as a
        JSON-mode dict + the on-disk path.
    """
    args = ReadParams.model_validate(params)
    registry_path = _resolve_registry_path(args.registry_path)
    registry = _load_registry(registry_path)
    return ReadResult(
        registry=registry.model_dump(mode="json"),
        registry_path=str(registry_path),
    ).model_dump(mode="json")


@register("registry.update")
async def update(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Mutate the registry: add / remove / rename.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`UpdateParams`.

    Returns:
        Dict matching :class:`UpdateResult`.

    Raises:
        ValueError: When the params payload or the requested operation
            fails validation; mapped to ``-32602`` by the server.
    """
    try:
        args = UpdateParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    if args.operation not in _OPERATIONS:
        raise ValueError(
            f"validation_failed: unknown operation {args.operation!r}; "
            f"expected one of {sorted(_OPERATIONS)}"
        )

    cache = _idempotency_cache(ctx)
    now_mono = time.monotonic()
    _evict_expired(cache, now=now_mono)
    if args.idempotency_key is not None:
        cached = cache.get(args.idempotency_key)
        if cached is not None and hasattr(cached, "result"):
            result = dict(cached.result)
            result["idempotent_replay"] = True
            logger.info(
                f"update idempotent_replay operation={args.operation} "
                f"repo_id={args.repo_id!r} key={args.idempotency_key!r}"
            )
            return result

    registry_path = _resolve_registry_path(args.registry_path)
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    from eawf.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(registry_path, timeout=5.0):
            registry = _load_registry(registry_path)
            if args.operation == "add":
                updated = _apply_add(registry, repo_id=args.repo_id, fields=args.fields)
            elif args.operation == "remove":
                updated = _apply_remove(registry, repo_id=args.repo_id)
            else:  # rename
                updated = _apply_rename(registry, repo_id=args.repo_id, fields=args.fields)

            # Re-validate via round-trip — defends against future
            # schema drift in the appliers.
            payload = Registry.model_validate(updated.model_dump(mode="json")).model_dump(
                mode="json"
            )
            atomic_write_json_locked(registry_path, payload)

            envelope = _build_envelope(
                operation=args.operation,
                repo_id=args.repo_id,
                registry_path=registry_path,
                fields=args.fields,
            )
            if ctx.bus is not None and hasattr(ctx.bus, "publish"):
                ctx.bus.publish(envelope)
            ctx.last_event_id = envelope.id

            logger.info(
                f"update ok operation={args.operation} repo_id={args.repo_id!r} "
                f"envelope_id={envelope.id!r}"
            )

            result = UpdateResult(
                operation=args.operation,
                repo_id=args.repo_id,
                registry_path=str(registry_path),
                envelope=envelope.model_dump(mode="json"),
                idempotent_replay=False,
            ).model_dump(mode="json")

            if args.idempotency_key is not None:
                cache[args.idempotency_key] = _CachedRegistryMutation(
                    result=result,
                    cached_at=time.monotonic(),
                )
            return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "read",
    "update",
]
