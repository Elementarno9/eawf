"""Shared mutator plumbing for the ``spec.*`` daemon method family.

The spec verbs outgrew one module and now live in three: the
``init`` / ``validate`` / ``promote`` / ``archive`` / ``sync`` facade in
:mod:`eawf.runtime.daemon.methods.spec`, the conversion verbs in
:mod:`eawf.runtime.daemon.methods.spec_convert`, and the gate repoint in
:mod:`eawf.runtime.daemon.methods.spec_repoint`. All three ride the same
four steps around their own bounded write -- replay an idempotency key,
re-validate the post-mutation payload, cache the result, publish the
envelope -- so those steps belong to no one module in particular.

This module is their shared, non-private home: the facade owns the verbs,
this collaborator owns the plumbing, and every cross-module name in the
package stays public. It mirrors
:mod:`eawf.runtime.daemon.methods.state_context`, which plays the same
role for the ``state.*`` family, and it deliberately imports nothing from
its three callers so the package import graph stays acyclic.
"""

from __future__ import annotations

import time
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.store.envelope import Envelope
from eawf.kernel.validate.strict import validate_state
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state_context import state_version

#: TTL for cached idempotency results (seconds).
IDEMPOTENCY_TTL_SECONDS: Final[float] = 60.0


class CachedSpecMutation(BaseModel):
    """One row in the daemon's spec idempotency cache.

    Attributes:
        result: The JSON-mode result dict replayed verbatim on a repeat
            call carrying the same idempotency key.
        cached_at: Monotonic timestamp of the write, used for eviction.
    """

    model_config = ConfigDict(extra="forbid")
    result: dict[str, Any]
    cached_at: float = Field(ge=0.0)


def idempotency_cache(ctx: MethodContext) -> dict[str, CachedSpecMutation]:
    """Return the per-process spec idempotency cache.

    Shares :attr:`MethodContext.idempotency_cache` with the state /
    config / registry mutators -- one dict per daemon process; the
    namespace separator is the idempotency key shape, not the
    handler.

    Args:
        ctx: Server context whose cache slot is read (and lazily filled).

    Returns:
        The mutable cache dict attached to *ctx*.
    """
    if isinstance(ctx.idempotency_cache, dict):
        return ctx.idempotency_cache
    fresh: dict[str, CachedSpecMutation] = {}
    ctx.idempotency_cache = fresh
    return fresh


def evict_expired(cache: dict[str, Any], *, now: float) -> None:
    """Drop entries older than :data:`IDEMPOTENCY_TTL_SECONDS`.

    Args:
        cache: The idempotency cache to prune in place.
        now: Current monotonic clock reading the ages are measured against.
    """
    expired = [
        k
        for k, v in cache.items()
        if hasattr(v, "cached_at") and now - v.cached_at > IDEMPOTENCY_TTL_SECONDS
    ]
    for k in expired:
        cache.pop(k, None)


def idempotent_replay(
    ctx: MethodContext,
    idempotency_key: str | None,
) -> dict[str, Any] | None:
    """Return the cached result for *idempotency_key*, or ``None``.

    Args:
        ctx: Server context carrying the idempotency cache.
        idempotency_key: The caller-supplied key, or ``None`` when the
            call opted out of replay.

    Returns:
        A copy of the cached result with ``idempotent_replay`` set, or
        ``None`` when there is nothing to replay.
    """
    cache = idempotency_cache(ctx)
    evict_expired(cache, now=time.monotonic())
    if idempotency_key is None:
        return None
    cached = cache.get(idempotency_key)
    if cached is None or not hasattr(cached, "result"):
        return None
    result = dict(cached.result)
    result["idempotent_replay"] = True
    return result


def cache_replay(
    ctx: MethodContext,
    *,
    idempotency_key: str | None,
    result: dict[str, Any],
) -> None:
    """Store *result* under *idempotency_key* for replay (when supplied).

    Args:
        ctx: Server context carrying the idempotency cache.
        idempotency_key: The caller-supplied key; ``None`` is a no-op.
        result: The JSON-mode result dict to replay on a repeat call.
    """
    if idempotency_key is None:
        return
    cache = idempotency_cache(ctx)
    cache[idempotency_key] = CachedSpecMutation(
        result=result,
        cached_at=time.monotonic(),
    )


def publish_envelope(ctx: MethodContext, envelope: Envelope) -> None:
    """Publish *envelope* on the subscription bus if one is attached.

    Args:
        ctx: Server context carrying the optional bus and the last-event
            cursor updated here.
        envelope: The canonical envelope to publish.
    """
    if ctx.bus is not None and hasattr(ctx.bus, "publish"):
        ctx.bus.publish(envelope)
    ctx.last_event_id = envelope.id


def validate_post_sync(new_payload: dict[str, Any]) -> str:
    """Re-validate the post-mutation state payload and return its version.

    Args:
        new_payload: The candidate ``state.json`` payload after the spec
            mutation applied.

    Returns:
        The post-mutation state version digest.

    Raises:
        DaemonValidationError: When the payload fails schema validation or
            trips an invariant (mapped to ``-32002``).
    """
    post = validate_state(new_payload, strict_optional=False)
    if post.state is None:
        raise DaemonValidationError(
            "validation_failed: post-mutation schema invalid: " + "; ".join(post.schema_errors[:3])
        )
    if post.violations:
        codes = ",".join(v.code for v in post.violations)
        raise DaemonValidationError(
            f"validation_failed: post-mutation invariants violated: {codes}"
        )
    return state_version(new_payload)


__all__ = [
    "IDEMPOTENCY_TTL_SECONDS",
    "CachedSpecMutation",
    "cache_replay",
    "evict_expired",
    "idempotency_cache",
    "idempotent_replay",
    "publish_envelope",
    "validate_post_sync",
]
