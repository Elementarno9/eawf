"""Opaque session-log handle registry for the daemon.

:class:`~eawf.kernel.state.models.SessionAttempt.session_log_handle` is an
**opaque** string — never an absolute filesystem path. The path
itself stays out of ``state.json`` + ``event.jsonl`` to satisfy AGENTS
rule 16 (secrets / PII hygiene); the daemon's in-process map below is
the **only** place real paths live.

The registry round-trip:

1. The dispatcher resolves a runtime-managed session-log path on
   spawn (e.g. ``~/.claude/projects/<scope>/<session-id>.json``).
2. :func:`register_session_log` stores that path in
   :data:`_REGISTRY` keyed by a generated handle
   (``urn:eawf:v1:session-log:<runtime>:<uuid>``) and returns the
   handle.
3. The handle goes into ``state.json`` /
   :class:`~eawf.kernel.state.models.SessionAttempt` /
   ``event.jsonl`` — never the path.
4. Consumers that need the real path (TUI tail, log inspector) call
   :func:`resolve_session_log` against the handle. Unknown handles
   raise :class:`KeyError` — fail fast, surface the gap.
5. :func:`prune_handles_for_wave` evicts wave-scoped handles whose
   parent wave's TTL has elapsed (see
   :mod:`eawf.daemon.session_ttl`).

The registry is a **process-local** in-memory map; it is rebuilt on
daemon restart from ``state.waves[*].sessions[*].session_log_handle``
plus the runtime adapter's path resolver. The TTL sweep
(:mod:`eawf.daemon.session_ttl`) keeps the registry bounded.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


#: URN prefix for the opaque session-log handle.
HANDLE_PREFIX: str = "urn:eawf:v1:session-log:"


@dataclass
class _Entry:
    """One row in the in-process handle map.

    Attributes:
        runtime: Runtime adapter id that owns the session log
            (``"claude-code"`` / ``"codex"`` / ...).
        raw_path: Real filesystem path; never serialised outside the
            daemon process.
        wave_id: Owning wave id; populated when the handle was
            allocated through the dispatcher path so the TTL sweep
            can scope evictions by wave. ``None`` for synthetic /
            test-only registrations.
    """

    runtime: str
    raw_path: Path
    wave_id: str | None = None


# In-process map. Module-global because the daemon runs as a single
# process per OS user; a class-level instance would only add ceremony
# for the same end-state.
_REGISTRY: dict[str, _Entry] = {}
_LOCK = threading.Lock()


def _new_handle(runtime: str) -> str:
    """Mint a fresh handle for *runtime*.

    Args:
        runtime: Runtime adapter id; embedded in the URN body so the
            handle is self-describing on the wire.

    Returns:
        A new opaque handle string matching
        ``urn:eawf:v1:session-log:<runtime>:<uuid4-hex>``.
    """
    return f"{HANDLE_PREFIX}{runtime}:{uuid.uuid4().hex}"


def register_session_log(
    runtime: str,
    raw_path: Path,
    *,
    wave_id: str | None = None,
) -> str:
    """Store *raw_path* under a freshly-minted opaque handle.

    Args:
        runtime: Runtime adapter id (``"claude-code"`` / ``"codex"`` / ...).
        raw_path: Absolute path to the runtime-managed session log.
            The path is held only in-process; it is never serialised
            into ``state.json``, ``event.jsonl``, or any error
            envelope (AGENTS rule 16 — secrets / PII hygiene).
        wave_id: Owning wave id when the registration is wave-scoped;
            pass ``None`` for test-only or synthetic registrations.

    Returns:
        The opaque handle to embed in
        :class:`~eawf.kernel.state.models.SessionAttempt.session_log_handle`.

    Raises:
        ValueError: When *runtime* is empty.
    """
    if not runtime:
        raise ValueError("runtime must be non-empty")
    handle = _new_handle(runtime)
    with _LOCK:
        _REGISTRY[handle] = _Entry(runtime=runtime, raw_path=raw_path, wave_id=wave_id)
    logger.debug(f"register_session_log runtime={runtime!r} wave={wave_id!r} handle={handle!r}")
    return handle


def resolve_session_log(handle: str) -> Path:
    """Return the real filesystem path for *handle*.

    Args:
        handle: Opaque handle previously returned by
            :func:`register_session_log`.

    Returns:
        The absolute :class:`Path` originally registered.

    Raises:
        KeyError: When *handle* is not present in the registry —
            either it was never registered or it has been pruned.
    """
    with _LOCK:
        try:
            entry = _REGISTRY[handle]
        except KeyError as exc:
            raise KeyError(f"unknown session-log handle: {handle!r}") from exc
    return entry.raw_path


def prune_handles_for_wave(wave_id: str) -> int:
    """Evict every registry entry that belongs to *wave_id*.

    Called by the TTL sweep when the parent wave's TTL has elapsed
    (default 86_400 s, configurable via
    ``daemon.session_handle_ttl_seconds``). Idempotent — pruning an
    unknown wave id is a no-op.

    Args:
        wave_id: Wave id whose handles should be dropped.

    Returns:
        Count of evicted handles.
    """
    with _LOCK:
        targets = [h for h, entry in _REGISTRY.items() if entry.wave_id == wave_id]
        for handle in targets:
            del _REGISTRY[handle]
    if targets:
        logger.info(f"prune_handles_for_wave wave={wave_id!r} dropped={len(targets)}")
    return len(targets)


def known_handles() -> Iterable[str]:
    """Return a snapshot of registered handles.

    Test-only helper; production callers route through
    :func:`resolve_session_log` instead.

    Returns:
        Iterable of handle strings currently held in the registry.
    """
    with _LOCK:
        return tuple(_REGISTRY)


def reset_registry() -> None:
    """Drop every registry entry. Test-only helper."""
    with _LOCK:
        _REGISTRY.clear()
