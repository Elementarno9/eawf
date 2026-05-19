"""``config.*`` JSON-RPC methods: read / set_layer_value / list_layers.

Daemon-side canonical writer for layered config YAML (authority map
rows 5-7). Wires P24-W10 sub-phase c of C02 §7.2: every layered-config
write that previously went through the in-process ``_save_value_to_layer``
helper now proxies through the ``config.set_layer_value`` RPC by default
(``daemon.proxy_enabled=True``).

Algorithm — mirrors the W09 ``state.mutate`` lifecycle for symmetry:

1. Idempotency-cache lookup keyed by ``params['idempotency_key']`` (when
   supplied). Re-emit the cached envelope verbatim with
   ``idempotent_replay=True`` flipped on.
2. ``portalock(target_path, timeout=5)`` — defense-in-depth (rule 4
   V1 retains portalocker inside the daemon mutator path).
3. Read + parse the YAML layer; deep-set the dotted key.
4. Atomic-rename write (tempfile → fsync → rename + parent dir fsync).
5. Build a canonical ``StoreKind.CONFIG_UPDATED`` envelope + publish on
   the subscription bus so TUI / watchers see the change.
6. Cache the result; return ``{layer, key_path, value, envelope}``.

This module owns the layered-config writer surface only; the registry
writer lives next door in :mod:`eawf.daemon.methods.registry`.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.config.layered import (
    global_config_path,
    local_config_path,
    repo_config_path,
    workspace_config_path,
)
from eawf.config.loader import load_yaml_layer
from eawf.daemon.methods import MethodContext, register
from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope

logger = logging.getLogger(__name__)


#: TTL for cached idempotency results (seconds). Mirrors
#: :data:`eawf.daemon.methods.state.IDEMPOTENCY_TTL_SECONDS` so the
#: replay window across the two mutator surfaces stays consistent.
IDEMPOTENCY_TTL_SECONDS: Final[float] = 60.0


# ---- Params + Result models ------------------------------------------------


class ReadParams(BaseModel):
    """Params for :func:`read`.

    Attributes:
        layer: Optional canonical layer label. When set, only the named
            layer's YAML is loaded and returned (raw). When ``None`` the
            handler defaults to the ``repo`` layer resolved against
            ``ctx.state_path``'s parent — the daemonless reader merges
            layers itself when a merge is needed.
    """

    model_config = ConfigDict(extra="forbid")
    layer: str | None = None


class ReadResult(BaseModel):
    """Result of :func:`read`."""

    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    layer_path: str


class SetLayerValueParams(BaseModel):
    """Params for :func:`set_layer_value`.

    Attributes:
        layer: Canonical layer label (``global`` | ``workspace`` |
            ``repo`` | ``local``). The ``built-in`` layer is read-only.
        key_path: Dotted-key as a list (e.g. ``["vcs", "auto_commit"]``).
            List form keeps the wire encoding unambiguous when any
            segment contains a literal ``.``.
        value: Typed value to set. Caller is responsible for type
            coercion before crossing the wire.
        idempotency_key: Optional caller-supplied retry key.
    """

    model_config = ConfigDict(extra="forbid")
    layer: str
    key_path: list[str] = Field(min_length=1)
    value: Any
    idempotency_key: str | None = None


class SetLayerValueResult(BaseModel):
    """Result of :func:`set_layer_value`."""

    model_config = ConfigDict(extra="forbid")
    layer: str
    layer_path: str
    key_path: list[str]
    value: Any
    envelope: dict[str, Any]
    idempotent_replay: bool = False


class ListLayersParams(BaseModel):
    """Params for :func:`list_layers` — empty by contract."""

    model_config = ConfigDict(extra="forbid")


class ListLayersResult(BaseModel):
    """Result of :func:`list_layers`."""

    model_config = ConfigDict(extra="forbid")
    layers: dict[str, str]


# ---- Idempotency cache ------------------------------------------------------


class _CachedConfigMutation(BaseModel):
    """One row in the daemon's config idempotency cache."""

    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any]
    cached_at: float = Field(ge=0.0)


def _idempotency_cache(ctx: MethodContext) -> dict[str, _CachedConfigMutation]:
    """Return the per-process config idempotency cache.

    The daemon shares :attr:`MethodContext.idempotency_cache` across
    ``state.mutate`` + ``config.set_layer_value`` + ``registry.update``
    because the idempotency-key namespace is caller-owned and a single
    cache simplifies eviction. State entries live under
    :class:`eawf.daemon.methods.state._CachedMutation`; config entries
    live under this class. Cross-pollination is harmless because the
    state cache lookups type-check before unboxing.
    """
    if isinstance(ctx.idempotency_cache, dict):
        return ctx.idempotency_cache
    fresh: dict[str, _CachedConfigMutation] = {}
    ctx.idempotency_cache = fresh
    return fresh


def _evict_expired(cache: dict[str, Any], *, now: float) -> None:
    """Drop entries whose age exceeds :data:`IDEMPOTENCY_TTL_SECONDS`."""
    expired = [
        k
        for k, v in cache.items()
        if hasattr(v, "cached_at") and now - v.cached_at > IDEMPOTENCY_TTL_SECONDS
    ]
    for k in expired:
        cache.pop(k, None)


# ---- Layer-path resolution --------------------------------------------------


def _resolve_layer_path(layer: str, *, state_path: Path | None) -> Path:
    """Return the YAML file path for *layer*.

    Resolves the four file layers (global / workspace / repo / local)
    relative to the daemon's project anchor. ``ctx.state_path``
    (``<repo>/.ea/state.json``) names the repo root via its parent of
    parent; tests can pass a different anchor by configuring the
    context's ``state_path`` field.

    Args:
        layer: Canonical writable-layer label.
        state_path: ``state.json`` path used to derive the repo root.

    Returns:
        The on-disk YAML path for *layer*.

    Raises:
        ValueError: When *layer* is not a writable file layer, or when
            ``state_path`` is missing for a repo-anchored layer.
    """
    if layer == "global":
        return global_config_path()
    if state_path is None:
        raise ValueError(f"state_path required to resolve layer {layer!r}")
    repo = state_path.parent.parent  # <repo>/.ea/state.json → <repo>
    if layer == "repo":
        return repo_config_path(repo)
    if layer == "local":
        return local_config_path(repo)
    if layer == "workspace":
        return workspace_config_path(repo)
    raise ValueError(f"unknown writable layer: {layer!r}")


def _set_dotted(payload: dict[str, Any], key_path: list[str], value: Any) -> None:
    """In-place deep-set ``payload[a][b][c] = value`` for ``[a, b, c]``."""
    cur: dict[str, Any] = payload
    for part in key_path[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[key_path[-1]] = value


def _atomic_write_yaml(target: Path, payload: dict[str, Any]) -> None:
    """Atomic YAML write — tempfile + fsync + rename + parent fsync.

    Mirrors :func:`eawf.cli.commands.config._atomic_write_yaml` byte-
    for-byte so a CLI fallback write and a daemon proxy write produce
    identical on-disk bytes for the same input.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = target.with_name(f"{target.name}.tmp.{suffix}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, sort_keys=True, default_flow_style=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
        parent_fd = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        logger.info(f"_atomic_write_yaml wrote {target}")
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink(missing_ok=True)


# ---- Envelope construction --------------------------------------------------


def _build_envelope(
    *,
    layer: str,
    layer_path: Path,
    key_path: list[str],
    value: Any,
) -> Envelope:
    """Build the canonical ``CONFIG_UPDATED`` envelope.

    Subscribers filter on ``kind=config_updated`` to react to config
    drift (TUI refresh, watcher reload). The payload carries enough to
    re-read the affected layer without re-walking the full merge.
    """
    now = datetime.now(UTC)
    dotted = ".".join(key_path)
    summary = f"config.set_layer_value layer={layer} key={dotted}"
    payload: dict[str, Any] = {
        "layer": layer,
        "layer_path": str(layer_path),
        "key_path": list(key_path),
        "value": value,
    }
    return Envelope(
        schema_version="1.0",
        id=f"CFG-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.CONFIG_UPDATED,
        scope_id=None,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )


# ---- Handlers ---------------------------------------------------------------


@register("config.read")
async def read(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the parsed YAML body for one config layer.

    Args:
        ctx: Server context — ``ctx.state_path`` must be configured
            for the repo / workspace / local layers (the ``global``
            layer is anchored at ``~/.config/eawf/``).
        params: JSON-RPC params per :class:`ReadParams`.

    Returns:
        Dict matching :class:`ReadResult` — parsed YAML body + layer
        path (the latter is repo-relative-like; tests can compare it
        against their fixture path).

    Raises:
        RuntimeError: When the daemon context is missing a state path
            needed to resolve the chosen layer.
        ValueError: When *layer* is unknown or non-writable.
    """
    args = ReadParams.model_validate(params)
    layer = args.layer or "repo"
    state_path = ctx.state_path if isinstance(ctx.state_path, Path) else None
    target = _resolve_layer_path(layer, state_path=state_path)
    body = load_yaml_layer(target)
    return ReadResult(config=body, layer_path=str(target)).model_dump(mode="json")


@register("config.set_layer_value")
async def set_layer_value(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Write one dotted-key value into a YAML layer.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`SetLayerValueParams`.

    Returns:
        Dict matching :class:`SetLayerValueResult`.

    Raises:
        RuntimeError: When the daemon context is missing fields the
            mutator depends on.
        ValueError: When the layer is unknown, the layer is read-only,
            or the params payload fails validation.
    """
    try:
        args = SetLayerValueParams.model_validate(params)
    except ValidationError as exc:
        raise ValueError(f"validation_failed: {exc}") from exc

    if args.layer == "built-in":
        raise ValueError("validation_failed: layer 'built-in' is read-only")

    state_path = ctx.state_path if isinstance(ctx.state_path, Path) else None
    target = _resolve_layer_path(args.layer, state_path=state_path)

    cache = _idempotency_cache(ctx)
    now_mono = time.monotonic()
    _evict_expired(cache, now=now_mono)
    if args.idempotency_key is not None:
        cached = cache.get(args.idempotency_key)
        if cached is not None and hasattr(cached, "result"):
            result = dict(cached.result)
            result["idempotent_replay"] = True
            logger.info(
                f"set_layer_value idempotent_replay layer={args.layer} "
                f"key_path={args.key_path} key={args.idempotency_key!r}"
            )
            return result

    # Daemon-internal portalock — defense in depth so a CLI fallback
    # write does not race with the daemon's mutator.
    from eawf.lock import portalock

    ctx.in_flight_mutations += 1
    try:
        with portalock.acquire(target, timeout=5.0):
            existing = load_yaml_layer(target)
            _set_dotted(existing, list(args.key_path), args.value)
            _atomic_write_yaml(target, existing)

            envelope = _build_envelope(
                layer=args.layer,
                layer_path=target,
                key_path=list(args.key_path),
                value=args.value,
            )
            if ctx.bus is not None and hasattr(ctx.bus, "publish"):
                ctx.bus.publish(envelope)
            ctx.last_event_id = envelope.id

            logger.info(
                f"set_layer_value ok layer={args.layer} key_path={args.key_path} "
                f"envelope_id={envelope.id!r}"
            )

            result = SetLayerValueResult(
                layer=args.layer,
                layer_path=str(target),
                key_path=list(args.key_path),
                value=args.value,
                envelope=envelope.model_dump(mode="json"),
                idempotent_replay=False,
            ).model_dump(mode="json")

            if args.idempotency_key is not None:
                cache[args.idempotency_key] = _CachedConfigMutation(
                    result=result,
                    cached_at=time.monotonic(),
                )
            return result
    finally:
        ctx.in_flight_mutations = max(0, ctx.in_flight_mutations - 1)


@register("config.list_layers")
async def list_layers(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the discovered on-disk paths for every writable layer.

    Args:
        ctx: Server context.
        params: JSON-RPC params; must be empty.

    Returns:
        Dict matching :class:`ListLayersResult` — keys are layer
        labels, values are absolute paths (whether the file exists or
        not — callers can ``Path(value).exists()`` themselves).
    """
    ListLayersParams.model_validate(params)
    state_path = ctx.state_path if isinstance(ctx.state_path, Path) else None
    layers: dict[str, str] = {
        "global": str(global_config_path()),
    }
    if state_path is not None:
        repo = state_path.parent.parent
        layers["workspace"] = str(workspace_config_path(repo))
        layers["repo"] = str(repo_config_path(repo))
        layers["local"] = str(local_config_path(repo))
    return ListLayersResult(layers=layers).model_dump(mode="json")


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "list_layers",
    "read",
    "set_layer_value",
]
