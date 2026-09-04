"""Per-request anchoring and payload plumbing for the state methods.

The daemon owns one socket per user and serves many repositories, so
every state-method path resolves its ``state.json`` / ``event.jsonl`` /
WAL anchors from the caller's ``repo_root`` before touching disk. This
module owns that resolution, the state read/validate/digest helpers, and
the in-memory idempotency cache the mutator replays from.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import orjson

from eawf.kernel.state.enums import (
    StoreKind,
)
from eawf.kernel.state.models import (
    State,
)
from eawf.kernel.state.mutations import (
    Mutation,
)
from eawf.kernel.store.paths import store_path
from eawf.kernel.validate.strict import validate_state
from eawf.runtime.daemon.methods import (
    MethodContext,
    note_cross_root_serve,
)

if TYPE_CHECKING:
    pass
from eawf.runtime.daemon.methods.state_models import CachedMutation

logger = logging.getLogger(__name__)


#: Module-level one-shot flag for the back-compat warning emitted when a
#: caller omits the ``repo_root`` param. Flipped True on the first emit;
#: never reset for the lifetime of the daemon process. The companion
#: helper :func:`_resolve_anchor` reads + writes this directly.
_ANCHOR_FALLBACK_WARN_EMITTED: bool = False

#: TTL for cached idempotency results (seconds). A repeat
#: ``state.mutate`` with the same ``idempotency_key`` inside this
#: window replays the cached envelope verbatim. Outside the window
#: the daemon treats the call as new (the WAL record carries the
#: durable replay guarantee).
IDEMPOTENCY_TTL_SECONDS: Final[float] = 60.0


def idempotency_cache(ctx: MethodContext) -> dict[str, CachedMutation]:
    """Return the in-memory idempotency cache attached to *ctx*.

    The cache is stored on the :class:`MethodContext` dataclass field
    set up by :mod:`eawf.runtime.daemon.main`; legacy contexts (unit tests,
    daemonless paths) get a fresh dict installed lazily. The cache
    lives only for the lifetime of the daemon process — restart wipes
    it; the WAL carries the durable replay guarantee.
    """
    if isinstance(ctx.idempotency_cache, dict):
        return ctx.idempotency_cache
    fresh: dict[str, CachedMutation] = {}
    ctx.idempotency_cache = fresh
    return fresh


def evict_expired(cache: dict[str, CachedMutation], *, now: float) -> None:
    """Drop entries whose age exceeds :data:`IDEMPOTENCY_TTL_SECONDS`."""
    expired = [k for k, v in cache.items() if now - v.cached_at > IDEMPOTENCY_TTL_SECONDS]
    for k in expired:
        cache.pop(k, None)


# ---- Per-request repo anchor resolution -----------------------------------


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
        f"caller omitted 'repo_root' param, resolving against the boot-time "
        f"state_path — update the caller to pass repo_root explicitly "
        f"(the boot-time fallback will be removed in a future wave)"
    )
    _ANCHOR_FALLBACK_WARN_EMITTED = True


def resolve_state_path(*, repo_root: str | None, ctx: MethodContext) -> Path:
    """Return ``<repo>/.ea/state.json`` for the caller's repo.

    The daemon process owns one per-user UDS / named pipe and serves
    many repos. Path joins against ``<repo>/.ea/...`` MUST honour the
    caller's repo root, not the daemon's boot-time cwd — otherwise a
    daemon spawned from one directory will resolve a different repo's
    ``state.json`` against its own anchor and (on a read-only-root host)
    blow up with ``[Errno 30] Read-only file system: '/.ea'``.

    Precedence:

    1. Per-request *repo_root* param (the canonical, post-W03 callsite).
    2. Boot-time ``ctx.state_path`` (legacy fallback for callers that
       have not yet been rewired). Emits a one-shot
       ``daemon_anchor_fallback`` warning per process so stale clients
       surface in the daemon log without breaking CI.

    Raises:
        RuntimeError: When *repo_root* is ``None`` AND ``ctx.state_path``
            is also unset.
    """
    if repo_root:
        return Path(repo_root) / ".ea" / "state.json"
    if ctx.state_path is None:
        raise RuntimeError("state_path not configured on daemon context")
    _emit_anchor_fallback_warning(ctx)
    return Path(ctx.state_path)


def resolve_mutator_paths(
    *,
    repo_root: str | None,
    ctx: MethodContext,
) -> tuple[Path, Path, Path]:
    """Return ``(state_path, event_path, wal_dir)`` for the mutator path.

    Same precedence as :func:`resolve_state_path` for *state_path*;
    *event_path* is always derived from the resolved *state_path* via
    :func:`eawf.kernel.store.paths.store_path` so a per-request ``repo_root``
    routes the event-jsonl append to the correct repo too. *wal_dir*
    stays daemon-process-local (one WAL per daemon).

    Raises:
        RuntimeError: When the state path cannot be resolved or
            ``ctx.wal_dir`` is unset.
    """
    note_cross_root_serve(ctx, repo_root=repo_root, command="state mutation")
    state_path = resolve_state_path(repo_root=repo_root, ctx=ctx)
    if repo_root:
        event_path = store_path(state_path, StoreKind.EVENT)
    else:
        event_path = (
            Path(ctx.event_path)
            if ctx.event_path is not None
            else store_path(state_path, StoreKind.EVENT)
        )
    if not isinstance(ctx.wal_dir, Path):
        raise RuntimeError("wal_dir not configured on daemon context")
    return state_path, event_path, ctx.wal_dir


def bus_for_root(ctx: MethodContext, state_path: Path) -> Any | None:
    """Return the publishable bus iff *state_path* is the daemon's boot root.

    Live subscribers attach to the boot root's stream only; a mutation
    served for another repo root appends to that repo's own event log but
    must not leak its envelopes onto the boot root's bus. Returns ``None``
    when the bus is absent, has no ``publish``, or the target root differs
    from the bound root.
    """
    bus = ctx.bus
    if bus is None or not hasattr(bus, "publish"):
        return None
    if ctx.state_path is not None and Path(state_path).resolve() != Path(ctx.state_path).resolve():
        return None
    return bus


# ---- State payload helpers --------------------------------------------------


def state_version(payload: dict[str, Any]) -> str:
    """Stable 16-hex-char digest of a state payload.

    Mirrors :func:`eawf.surfaces.cli.commands.lifecycle._state_version` so the
    before/after-version strings stay comparable across the in-process
    and daemon-proxy paths.
    """
    raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()[:16]


def read_state(state_path: Path) -> tuple[State, dict[str, Any]]:
    """Load + validate ``state.json``; return ``(typed_state, payload)``.

    Raises:
        FileNotFoundError: when *state_path* does not exist.
        ValueError: when the on-disk payload fails schema validation.
            This is on-disk corruption (not a mutation rejection), so it
            stays a bare ``ValueError`` that the server maps to
            ``-32602 invalid_params`` — distinct from the typed
            :class:`~eawf.runtime.daemon.methods.DaemonValidationError` the
            mutator raises for a *rejected* mutation (``-32002``).
    """
    if not state_path.exists():
        raise FileNotFoundError(f"state file not found: {state_path!r}")
    raw = state_path.read_bytes()
    payload = orjson.loads(raw)
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise ValueError("state schema invalid: " + "; ".join(report.schema_errors[:3]))
    return report.state, payload


def args_hash(mutation: Mutation) -> str:
    """Stable 16-hex-char digest of the mutation params."""
    raw = orjson.dumps(mutation.params, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()[:16]


def config_root_for_state_path(state_path: Path) -> Path:
    """Return the root that owns ``.ea/config.yaml`` for *state_path*."""
    return state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent


def event_store_path_for(state_path: Path) -> Path:
    """Return the ``event.jsonl`` path that pairs with *state_path*.

    Thin wrapper around :func:`eawf.kernel.store.paths.store_path` so callers
    in :mod:`eawf.runtime.daemon.main` keep a single import surface for the
    canonical pairing.
    """
    return store_path(state_path, StoreKind.EVENT)
