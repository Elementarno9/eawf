"""``config.*`` JSON-RPC methods: read / set_layer_value / list_layers
plus wave-layer overlay management (set_wave_value /
clear_wave_overlay / get_wave_overlay).

Daemon-side canonical writer for layered config YAML (authority map
rows 5-7): every layered-config write that previously went through the
in-process ``_save_value_to_layer`` helper now proxies through the
``config.set_layer_value`` RPC by default (``daemon.proxy_enabled=True``).

The writer also covers the ``branch`` layer (file-backed at
``<repo>/.ea/branches/<branch>.yaml``; subdirectory layout for
slash-bearing branch names) and the ``wave`` layer (transient daemon
RAM, keyed by ``Wave.id``, reset on wave close — see
``set_wave_value`` / ``clear_wave_overlay``).

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
writer lives next door in :mod:`eawf.runtime.daemon.methods.registry`.
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

from eawf.kernel.config.layered import (
    branch_config_path,
    global_config_path,
    local_config_path,
    repo_config_path,
    workspace_config_path,
)
from eawf.kernel.config.loader import load_yaml_layer
from eawf.kernel.config.registry import leaf_key_lookup
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.methods import MethodContext, register

logger = logging.getLogger(__name__)


#: Module-level one-shot flag for the back-compat warning emitted when a
#: caller omits the ``repo_root`` param. Mirrors the state-side flag in
#: :data:`eawf.runtime.daemon.methods.state._ANCHOR_FALLBACK_WARN_EMITTED` —
#: one warning per process per surface keeps the daemon log readable
#: under stale-CLI load.
_ANCHOR_FALLBACK_WARN_EMITTED: bool = False


#: TTL for cached idempotency results (seconds). Mirrors
#: :data:`eawf.runtime.daemon.methods.state.IDEMPOTENCY_TTL_SECONDS` so the
#: replay window across the two mutator surfaces stays consistent.
IDEMPOTENCY_TTL_SECONDS: Final[float] = 60.0


# ---- Params + Result models ------------------------------------------------


class ReadParams(BaseModel):
    """Params for :func:`read`.

    Attributes:
        layer: Optional canonical layer label. When set, only the named
            layer's YAML is loaded and returned (raw). When ``None`` the
            handler defaults to the ``repo`` layer resolved against the
            caller's repo root — the daemonless reader merges layers
            itself when a merge is needed.
        branch: Branch name (required when ``layer == "branch"``).
        repo_root: Optional absolute path of the repo whose layered
            config YAML the daemon should read. The CLI proxy forwards
            ``flags.workspace`` (or ``Path.cwd()``) here so the daemon
            — which is one per user, not one per repo — resolves the
            right anchor regardless of boot-time cwd. Omitting falls
            back to ``ctx.state_path`` with a one-shot
            ``daemon_anchor_fallback`` warning.
    """

    model_config = ConfigDict(extra="forbid")
    layer: str | None = None
    branch: str | None = None
    repo_root: str | None = None


class ReadResult(BaseModel):
    """Result of :func:`read`."""

    model_config = ConfigDict(extra="forbid")
    config: dict[str, Any]
    layer_path: str


class SetLayerValueParams(BaseModel):
    """Params for :func:`set_layer_value`.

    Attributes:
        layer: Canonical layer label (``global`` | ``workspace`` |
            ``repo`` | ``branch`` | ``local``). The ``built-in`` layer
            is read-only; ``env`` / ``cli`` / ``wave`` are not file-
            backed (use :func:`set_wave_value` for the wave overlay).
        key_path: Dotted-key as a list (e.g. ``["vcs", "auto_commit"]``).
            List form keeps the wire encoding unambiguous when any
            segment contains a literal ``.``.
        value: Typed value to set. Caller is responsible for type
            coercion before crossing the wire.
        branch: Branch name (required when ``layer == "branch"``).
            Subdirectory form is preserved (``feature/foo`` →
            ``.ea/branches/feature/foo.yaml``).
        idempotency_key: Optional caller-supplied retry key.
        repo_root: Optional absolute path of the repo whose layered
            config YAML the daemon should write. Same semantics as
            the field on :class:`ReadParams`.
    """

    model_config = ConfigDict(extra="forbid")
    layer: str
    key_path: list[str] = Field(min_length=1)
    value: Any
    branch: str | None = None
    idempotency_key: str | None = None
    repo_root: str | None = None


class SetWaveValueParams(BaseModel):
    """Params for :func:`set_wave_value`.

    Attributes:
        wave_id: ``Wave.id`` for the transient overlay (one map per
            wave). The overlay lives in daemon RAM only and is dropped
            on wave close / daemon shutdown.
        key_path: Dotted-key as a list.
        value: Typed value to set.
    """

    model_config = ConfigDict(extra="forbid")
    wave_id: str
    key_path: list[str] = Field(min_length=1)
    value: Any


class SetWaveValueResult(BaseModel):
    """Result of :func:`set_wave_value`."""

    model_config = ConfigDict(extra="forbid")
    wave_id: str
    key_path: list[str]
    value: Any
    envelope: dict[str, Any]


class WaveOverlayParams(BaseModel):
    """Params for :func:`get_wave_overlay` / :func:`clear_wave_overlay`."""

    model_config = ConfigDict(extra="forbid")
    wave_id: str


class WaveOverlayResult(BaseModel):
    """Result of :func:`get_wave_overlay`."""

    model_config = ConfigDict(extra="forbid")
    wave_id: str
    overlay: dict[str, Any]


class ClearWaveOverlayResult(BaseModel):
    """Result of :func:`clear_wave_overlay`."""

    model_config = ConfigDict(extra="forbid")
    wave_id: str
    cleared: bool


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
    """Params for :func:`list_layers`.

    Attributes:
        repo_root: Optional absolute path of the repo whose layer paths
            the daemon should enumerate. Same semantics as the field on
            :class:`ReadParams`.
    """

    model_config = ConfigDict(extra="forbid")
    repo_root: str | None = None


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
    :class:`eawf.runtime.daemon.methods.state._CachedMutation`; config entries
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


def _emit_anchor_fallback_warning(ctx: MethodContext) -> None:
    """Log the one-shot ``daemon_anchor_fallback`` deprecation warning.

    Stays a no-op after the first call for the lifetime of the daemon
    process — mirrors the
    :data:`eawf.kernel.config.layered._LEGACY_RUNTIME_WARN_EMITTED` pattern so
    a stale CLI client does not spam the daemon log.
    """
    global _ANCHOR_FALLBACK_WARN_EMITTED
    if _ANCHOR_FALLBACK_WARN_EMITTED:
        return
    logger.warning(
        f"daemon_anchor_fallback state_path={ctx.state_path!r}; "
        f"caller omitted repo_root, resolving against boot-time state_path. "
        f"Update the caller to pass repo_root explicitly - the boot-time "
        f"fallback will be removed in a future wave."
    )
    _ANCHOR_FALLBACK_WARN_EMITTED = True


def _resolve_state_anchor(*, repo_root: str | None, ctx: MethodContext) -> Path | None:
    """Return the effective ``state.json`` anchor for the caller's repo.

    The layered-config helpers all derive their target YAML paths from
    ``state_path.parent.parent`` (the repo root). This helper centralises
    the per-request override:

    1. Per-request *repo_root* param wins — ``<repo>/.ea/state.json``.
    2. Boot-time ``ctx.state_path`` (legacy fallback). Emits a one-shot
       ``daemon_anchor_fallback`` warning so stale callers surface in
       the daemon log.
    3. ``None`` only when both are absent — the caller decides whether
       that is a fatal error (writers) or a benign skip (global layer).
    """
    if repo_root:
        return Path(repo_root) / ".ea" / "state.json"
    if isinstance(ctx.state_path, Path):
        _emit_anchor_fallback_warning(ctx)
        return ctx.state_path
    return None


def _resolve_layer_path(
    layer: str,
    *,
    state_path: Path | None,
    branch: str | None = None,
) -> Path:
    """Return the YAML file path for *layer*.

    Resolves the five file layers (global / workspace / repo / branch /
    local) relative to the daemon's project anchor. ``ctx.state_path``
    (``<repo>/.ea/state.json``) names the repo root via its parent of
    parent; tests can pass a different anchor by configuring the
    context's ``state_path`` field.

    Args:
        layer: Canonical writable-layer label.
        state_path: ``state.json`` path used to derive the repo root.
        branch: Branch name (required when ``layer == "branch"``).

    Returns:
        The on-disk YAML path for *layer*.

    Raises:
        ValueError: When *layer* is not a writable file layer, when
            ``state_path`` is missing for a repo-anchored layer, or
            when ``layer == "branch"`` and *branch* is missing.
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
    if layer == "branch":
        if not branch:
            raise ValueError("branch name required for 'branch' layer")
        return branch_config_path(repo, branch)
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
        logger.info(f"_atomic_write_yaml wrote target={target}")
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
    state_path = _resolve_state_anchor(repo_root=args.repo_root, ctx=ctx)
    target = _resolve_layer_path(layer, state_path=state_path, branch=args.branch)
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
    if args.layer == "wave":
        raise ValueError(
            "validation_failed: layer 'wave' is daemon-RAM-only; "
            "use 'config.set_wave_value' instead"
        )

    # Leaf-key gate: refuse unknown keys with the canonical error
    # message. The catalog is the source of truth for which dotted
    # paths the daemon may persist; an unknown key is almost always a
    # typo or a stale CLI build.
    dotted = ".".join(args.key_path)
    _ = leaf_key_lookup(dotted)  # raises ValueError on unknown.

    state_path = _resolve_state_anchor(repo_root=args.repo_root, ctx=ctx)
    target = _resolve_layer_path(args.layer, state_path=state_path, branch=args.branch)

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
    from eawf.runtime.lock import portalock

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
        not — callers can ``Path(value).exists()`` themselves). The
        ``branch`` key points at the ``.ea/branches`` parent directory
        — callers walk the tree to enumerate branch files since each
        branch yields a separate yaml file.
    """
    args = ListLayersParams.model_validate(params)
    state_path = _resolve_state_anchor(repo_root=args.repo_root, ctx=ctx)
    layers: dict[str, str] = {
        "global": str(global_config_path()),
    }
    if state_path is not None:
        repo = state_path.parent.parent
        layers["workspace"] = str(workspace_config_path(repo))
        layers["repo"] = str(repo_config_path(repo))
        layers["branch"] = str(repo / ".ea" / "branches")
        layers["local"] = str(local_config_path(repo))
    return ListLayersResult(layers=layers).model_dump(mode="json")


# ---- Wave layer (transient, daemon-RAM) -------------------------------------


def _wave_overlay_map(ctx: MethodContext) -> dict[str, dict[str, Any]]:
    """Return the per-context wave-overlay map, creating it on first use.

    The map lives on ``ctx`` rather than at module level so each test's
    :class:`MethodContext` starts with a clean slate.
    """
    existing = getattr(ctx, "wave_config_overrides", None)
    if isinstance(existing, dict):
        return existing
    fresh: dict[str, dict[str, Any]] = {}
    ctx.wave_config_overrides = fresh  # type: ignore[attr-defined]
    return fresh


def _set_dotted_in_overlay(
    overlay: dict[str, Any],
    key_path: list[str],
    value: Any,
) -> None:
    """Deep-set ``overlay[a][b]... = value`` for ``key_path = [a, b, ...]``."""
    cur: dict[str, Any] = overlay
    for part in key_path[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[key_path[-1]] = value


@register("config.set_wave_value")
async def set_wave_value(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Set one dotted-key value in the wave-layer overlay for *wave_id*.

    The wave layer is daemon-RAM-only and resets on wave close. Use
    case: V5 reactive runtime fallback (daemon flips a wave from
    ``claude`` → ``codex`` mid-dispatch; subsequent envelopes for the
    same wave honour the override).

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`SetWaveValueParams`.

    Returns:
        Dict matching :class:`SetWaveValueResult`.

    Raises:
        ValueError: When *key_path* references a key absent from the
            leaf catalog (typo gate).
    """
    args = SetWaveValueParams.model_validate(params)
    dotted = ".".join(args.key_path)
    entry = leaf_key_lookup(dotted)  # raises ValueError on unknown.
    if "wave" not in entry.writable_layers:
        raise ValueError(f"validation_failed: leaf {dotted!r} is not writable from the wave layer")

    overlay_map = _wave_overlay_map(ctx)
    per_wave = overlay_map.setdefault(args.wave_id, {})
    _set_dotted_in_overlay(per_wave, list(args.key_path), args.value)

    envelope = _build_envelope(
        layer="wave",
        layer_path=Path(f"<wave:{args.wave_id}>"),
        key_path=list(args.key_path),
        value=args.value,
    )
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id

    logger.info(f"set_wave_value ok wave={args.wave_id!r} key_path={args.key_path}")
    return SetWaveValueResult(
        wave_id=args.wave_id,
        key_path=list(args.key_path),
        value=args.value,
        envelope=envelope.model_dump(mode="json"),
    ).model_dump(mode="json")


@register("config.get_wave_overlay")
async def get_wave_overlay(
    ctx: MethodContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return the current wave-layer overlay for *wave_id*.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`WaveOverlayParams`.

    Returns:
        Dict matching :class:`WaveOverlayResult`. Empty mapping when
        the wave has no overlay set.
    """
    args = WaveOverlayParams.model_validate(params)
    overlay_map = _wave_overlay_map(ctx)
    overlay = overlay_map.get(args.wave_id, {})
    return WaveOverlayResult(
        wave_id=args.wave_id,
        overlay=dict(overlay),
    ).model_dump(mode="json")


@register("config.clear_wave_overlay")
async def clear_wave_overlay(
    ctx: MethodContext,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Drop the wave-layer overlay for *wave_id*.

    Idempotent — clearing an absent overlay returns ``cleared=False``.

    Args:
        ctx: Server context.
        params: JSON-RPC params per :class:`WaveOverlayParams`.

    Returns:
        Dict matching :class:`ClearWaveOverlayResult`.
    """
    args = WaveOverlayParams.model_validate(params)
    overlay_map = _wave_overlay_map(ctx)
    cleared = args.wave_id in overlay_map
    overlay_map.pop(args.wave_id, None)
    logger.info(f"clear_wave_overlay wave={args.wave_id!r} cleared={cleared}")
    return ClearWaveOverlayResult(
        wave_id=args.wave_id,
        cleared=cleared,
    ).model_dump(mode="json")


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "clear_wave_overlay",
    "get_wave_overlay",
    "list_layers",
    "read",
    "set_layer_value",
    "set_wave_value",
]
