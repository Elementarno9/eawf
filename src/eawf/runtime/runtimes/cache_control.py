"""Per-runtime cache-control marker injection.

Cache-control marker injection lives at the **adapter layer**: a skill
body never sees cache markers; the adapter injects them per its
runtime's convention. This module owns the *pure* injection policy —
the typed marker model + the per-runtime decision of whether a
caller-side marker is materialised — so each adapter's ``open_session``
routes its ``cache_prefix`` through one shared gate instead of carrying
a parallel hard-coded table.

The decision is driven by the YAML capability matrix
(``capabilities.yaml`` ``cache_control`` row) via
:func:`eawf.runtime.runtimes.selector.runtime_supports`, keeping the matrix the
single source of truth. Only ``claude-code`` exposes a caller-side
marker (``<cache_control type="ephemeral" />``); ``codex`` (OpenAI
prompt caching is automatic at the ≥1024-token threshold) and
``opencode`` (the ``@ai-sdk/anthropic`` provider injects internally +
the OAuth path strips the marker per upstream ``#17910``) are **no-op
paths** — the prompt is returned unchanged.

Boundaries
----------

* :class:`CacheControlMarker` — typed caller-side marker (the Claude
  ``cache_control`` breakpoint). Frozen + ``extra="forbid"``.
* :func:`runtime_accepts_marker` — boolean view over the matrix
  ``cache_control`` cell for one runtime id.
* :func:`inject_cache_control` — the adapter-boundary injection. Returns
  the (possibly marker-augmented) cache prefix; a no-op for runtimes
  whose matrix cell is ``unsupported``.
"""

from __future__ import annotations

import logging
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from eawf.runtime.runtimes.selector import runtime_supports

logger = logging.getLogger(__name__)

#: The matrix capability row this module gates on.
CACHE_CONTROL_CAPABILITY: Final[str] = "cache_control"

#: Caller-side marker types supported in v0.3-v0.5. Anthropic exposes a
#: single ``ephemeral`` breakpoint today; the closed literal forces an
#: explicit edit when a new type lands rather than a silent default.
MarkerType = Literal["ephemeral"]

#: The literal Claude cache-control breakpoint token appended to a cache
#: prefix when the runtime accepts a caller-side marker. Matches the
#: documented ``capabilities.yaml`` ``claude-code`` cell comment.
_EPHEMERAL_MARKER: Final[str] = '<cache_control type="ephemeral" />'


class CacheControlMarker(BaseModel):
    """A caller-side cache-control breakpoint (Claude ``cache_control``).

    Only Claude accepts a caller-side marker; the marker is appended to
    the dispatch *cache prefix* (the stable prompt head the runtime
    caches). The model is frozen so a single shared default instance is
    safe to reuse across adapters.

    Attributes:
        marker_type: Breakpoint type. ``"ephemeral"`` is the only
            Anthropic-exposed type in v0.3-v0.5.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    marker_type: MarkerType = "ephemeral"

    def render(self) -> str:
        """Render the marker into its runtime wire token.

        Returns:
            The literal ``<cache_control type="ephemeral" />`` token the
            Claude adapter appends to the cache prefix.
        """
        return _EPHEMERAL_MARKER


#: Shared default marker — frozen, so reuse across adapters is safe.
DEFAULT_MARKER: Final[CacheControlMarker] = CacheControlMarker()


def runtime_accepts_marker(runtime_id: str) -> bool:
    """Return whether ``runtime_id`` accepts a caller-side cache marker.

    Reads the ``cache_control`` cell from the YAML capability matrix via
    :func:`eawf.runtime.runtimes.selector.runtime_supports` (no parallel
    hard-coded table). ``claude-code`` is ``supported``; ``codex`` and
    ``opencode`` are ``unsupported`` (§5.6) and therefore return
    ``False``.

    Args:
        runtime_id: Canonical runtime id (``claude-code`` / ``codex`` /
            ``opencode``).

    Returns:
        ``True`` when the runtime exposes a caller-side cache-control
        marker, else ``False``.

    Raises:
        ValueError: ``runtime_id`` is not one of the three v0.3-v0.5
            ids (propagated from
            :func:`~eawf.runtime.runtimes.selector.runtime_supports`).
    """
    return runtime_supports(runtime_id, CACHE_CONTROL_CAPABILITY)


def inject_cache_control(
    *,
    runtime_id: str,
    cache_prefix: str | None,
    marker: CacheControlMarker = DEFAULT_MARKER,
) -> str | None:
    """Inject a cache-control marker into ``cache_prefix`` at the adapter boundary.

    The single injection gate every adapter routes its ``cache_prefix``
    through. For a runtime whose matrix ``cache_control`` cell is
    ``supported`` (``claude-code``) the marker token is appended to the
    prefix; for an ``unsupported`` runtime (``codex`` / ``opencode``)
    this is a **no-op** and ``cache_prefix`` is returned unchanged — the
    runtime caches via prefix-stability + session resume, with no
    caller-side knob.

    A ``None`` ``cache_prefix`` (no caller-supplied prefix) is returned
    as ``None`` regardless of runtime: there is nothing to mark.

    Args:
        runtime_id: Canonical runtime id of the dispatch target.
        cache_prefix: The stable prompt head the runtime caches.
            ``None`` when the caller supplies no prefix.
        marker: The marker to inject. Defaults to the shared
            ``ephemeral`` breakpoint.

    Returns:
        The cache prefix with the marker appended (Claude), or the
        prefix unchanged (no-op runtimes), or ``None`` when no prefix
        was supplied.

    Raises:
        ValueError: ``runtime_id`` is not a canonical runtime id.
    """
    if cache_prefix is None:
        return None
    if not runtime_accepts_marker(runtime_id):
        logger.debug(f"inject_cache_control runtime={runtime_id!r} decision=no_op")
        return cache_prefix
    injected = f"{cache_prefix}{marker.render()}"
    logger.debug(f"inject_cache_control runtime={runtime_id!r} decision=injected")
    return injected


__all__ = [
    "CACHE_CONTROL_CAPABILITY",
    "DEFAULT_MARKER",
    "CacheControlMarker",
    "MarkerType",
    "inject_cache_control",
    "runtime_accepts_marker",
]
