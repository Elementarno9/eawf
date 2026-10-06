"""Orchestrator for ``eawf cc statusline``.

Reads Claude's stdin JSON, calls each statusline module in fixed order,
applies the configured theme, writes one line to stdout. Modules degrade
independently — any exception inside a module is caught here and rendered
as a ``status="failed"`` segment so the whole pipeline never crashes.

Design notes:

- Module order is fixed (left-to-right): ``scope``, ``budget``, ``git``,
  ``model_session_cwd``, ``context_tokens``, ``rate_window``, ``cost``,
  ``mcp_health``, ``hooks_plugins``, ``memory``, ``token_saving``.
- Every segment carries a truth field naming its producer, freshness and
  measurement quality; a value a module cannot source renders
  ``<label>:n/a(<reason>)``, never a bare ``?`` or ``-``.
- Every module signature is uniform: ``build(claude_payload, state_path) ->
  StatuslineSegment``.
- Caching is stale-while-revalidate through
  :class:`~eawf.runtime.statusline_cache.StatuslineCache`, one entry for
  each Claude session id. A hit (fresh or stale) is served without rendering; a
  stale hit also starts a detached ``eawf cc statusline prewarm`` that
  renders and rewrites the entry. Only a miss renders on the caller's time.
- Claude Code runs the statusline on every redraw, so this module imports
  nothing heavy at load time: the modules, the theme renderer, the state
  resolver and the counter sidecar load only when a render actually runs.
  The console entry point serves cache hits through
  :func:`serve_cached_line` before the CLI app is imported at all.
- The runtime counter sidecar is written whenever a render runs (a miss or
  a background refresh), so it trails the live payload by at most one TTL.

Public surface:

- :func:`render_pipeline` — full module call + theme apply (always cold).
- :func:`serve_cached_line` — hit-only path for the console entry point.
- :func:`run_with_cache` — cache-aware entry point used by the CLI.
- :func:`prewarm` — render, write the cache entry, exit silently.
- :func:`cache_path_for` — cache-path resolver, shared with the counter
  sidecar readers.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson

from eawf.runtime.statusline_cache import StatuslineCache

if TYPE_CHECKING:
    import logging
    from types import ModuleType

    from eawf.surfaces.render.statusline import StatuslineSegment, StatuslineTheme


def _logger() -> logging.Logger:
    """Return this module's logger, importing :mod:`logging` on first use.

    ``logging`` costs about a third of a cached statusline redraw, and a
    cache hit logs nothing, so the import waits for the first log line.
    """
    import logging

    return logging.getLogger(__name__)


def _module_order() -> list[ModuleType]:
    """Return the statusline modules in render order.

    Each exposes ``build(claude_payload: dict[str, Any], state_path: Path |
    None) -> StatuslineSegment``. Imported here, not at module load, so a
    cache hit never pays for them.
    """
    from eawf.runtime.runtimes.claude.statusline_modules import (
        budget,
        context_tokens,
        cost,
        git,
        hooks_plugins,
        mcp_health,
        memory,
        model_session_cwd,
        rate_window,
        scope,
        token_saving,
    )

    return [
        scope,
        budget,
        git,
        model_session_cwd,
        context_tokens,
        rate_window,
        cost,
        mcp_health,
        hooks_plugins,
        memory,
        token_saving,
    ]


_CACHE_DIR_ENV: str = "EAWF_STATUSLINE_CACHE"
"""Optional override for the cache directory, used by tests to redirect
cache writes to a tmp dir without touching the real ``~/.claude/`` tree.
"""


def _cache_root() -> Path:
    """Return the cache directory, honouring :data:`_CACHE_DIR_ENV`."""
    override = os.environ.get(_CACHE_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".claude" / "statusline-cache"


def cache_path_for(session_id: str) -> Path:
    """Return the cache path for *session_id*.

    The basename is ``<session-id>.json`` after sanitization. Claude's
    stdin is untrusted, so any character that could escape the cache
    root via path traversal (``/``, ``\\``, ``..``, NUL) is replaced
    with ``_`` and the result is truncated to 128 characters. Empty,
    purely-traversal, or all-disallowed session ids collapse to
    ``unknown``.
    """
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", session_id)[:128] if session_id else ""
    if not safe or safe in {".", ".."} or set(safe) == {"."}:
        safe = "unknown"
    return _cache_root() / f"{safe}.json"


def _decode_payload(raw: str) -> dict[str, Any]:
    """Decode Claude's stdin JSON into a dict.

    Empty input → empty dict. Decode errors → empty dict (logged at debug
    level). The orchestrator must never raise here — a bad upstream
    payload is a degradation, not a crash.
    """
    if not raw.strip():
        return {}
    try:
        decoded: Any = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        _logger().debug(f"_decode_payload stdin-json-decode-failed error={exc}")
        return {}
    if not isinstance(decoded, dict):
        _logger().debug(f"_decode_payload stdin-not-mapping got={type(decoded).__name__}")
        return {}
    return decoded


def _session_id(claude_payload: dict[str, Any]) -> str | None:
    """Return the payload's non-empty string ``session_id``, else ``None``."""
    session_id = claude_payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    return None


def _safe_resolve_state_path(workspace: Path | None) -> Path | None:
    """Best-effort wrapper around ``resolve_state_path`` returning ``None``."""
    from eawf.surfaces.cli.scope import resolve_state_path

    try:
        return resolve_state_path(workspace)
    except FileNotFoundError:
        return None


def _build_segments(
    claude_payload: dict[str, Any],
    state_path: Path | None,
) -> list[StatuslineSegment]:
    """Call every module, wrapping exceptions into a ``failed`` segment."""
    from eawf.surfaces.render.statusline import SegmentSource, unavailable_segment

    segments: list[StatuslineSegment] = []
    for module in _module_order():
        module_name = module.__name__.rsplit(".", 1)[-1]
        try:
            segment = module.build(claude_payload, state_path)
        except Exception as exc:
            # Orchestrator must never crash — every module exception
            # collapses to a ``failed`` segment so the theme can decide
            # whether to skip or render its ``n/a(render-failed)`` marker.
            _logger().warning(f"_build_segments module-raised module={module_name!r} error={exc}")
            source = SegmentSource(producer="eawf.statusline", provenance=module.__name__)
            segments.append(
                unavailable_segment(
                    module_name, module_name, "render-failed", source, status="failed"
                )
            )
            continue
        segments.append(segment)
    return segments


def render_pipeline(
    claude_payload: dict[str, Any],
    *,
    workspace: Path | None,
    theme_name: str | None,
) -> str:
    """Run every module + apply the theme; return the joined statusline string.

    No caching — the cold path, used for a cache miss and by :func:`prewarm`.
    """
    from eawf.surfaces.render.statusline import render_segments

    state_path = _safe_resolve_state_path(workspace)
    segments = _build_segments(claude_payload, state_path)
    theme = _resolve_active_theme(theme_name)
    return render_segments(segments, theme)


def _resolve_active_theme(theme_name: str | None) -> StatuslineTheme:
    """Resolve the active theme honoring CLI flag + env var."""
    from eawf.surfaces.render.statusline import load_themes, resolve_theme

    name = theme_name
    if not name:
        env_name = os.environ.get("EAWF_STATUSLINE_THEME")
        if env_name:
            name = env_name
    themes = load_themes()
    return resolve_theme(name, themes)


def _write_runtime_counter_sidecar(session_id: str, claude_payload: dict[str, Any]) -> None:
    """Persist parsed runtime counters beside the statusline cache, best-effort."""
    from eawf.runtime.runtime_counter_sidecar import (
        RuntimeCounterSidecar,
        sidecar_path_for_statusline_cache,
    )
    from eawf.runtime.runtimes.claude.runtime_counters import parse_runtime_counters

    counters = parse_runtime_counters(claude_payload)
    if counters is None:
        return
    path = sidecar_path_for_statusline_cache(cache_path_for(session_id))
    RuntimeCounterSidecar(path).write(counters)


def _render_session(
    claude_payload: dict[str, Any],
    session_id: str,
    *,
    workspace: Path | None,
    theme_name: str | None,
) -> str:
    """Record the session's counters, then render its line."""
    _write_runtime_counter_sidecar(session_id, claude_payload)
    return render_pipeline(claude_payload, workspace=workspace, theme_name=theme_name)


def _spawn_refresh(raw: str, *, workspace: Path | None, theme_name: str | None) -> None:
    """Start a detached ``eawf cc statusline prewarm`` fed *raw* on stdin.

    The payload goes through an unlinked temporary file rather than a pipe
    so the caller never blocks on a child that is still importing. The
    child gets its own session so it outlives the redraw that started it.
    A failed spawn is logged at debug; the entry stays stale and the next
    redraw past the TTL tries again.
    """
    import subprocess
    import tempfile

    from eawf.platform.subprocess_detach import no_window_kwargs

    argv = [sys.executable, "-m", "eawf"]
    if workspace is not None:
        argv += ["--workspace", str(workspace)]
    argv += ["cc", "statusline", "prewarm"]
    if theme_name:
        argv += ["--theme", theme_name]
    try:
        with tempfile.TemporaryFile() as payload:
            payload.write(raw.encode("utf-8"))
            payload.seek(0)
            subprocess.Popen(
                argv,
                stdin=payload,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                **no_window_kwargs(),
            )
    except OSError as exc:
        _logger().debug(f"_spawn_refresh spawn-failed error={exc}")


def serve_cached_line(raw: str, *, theme_name: str | None) -> str | None:
    """Return the cached line for the payload in *raw*, or ``None`` to render.

    The hit-only half of :func:`run_with_cache`, for the console entry
    point: it renders nothing, so it loads none of the render stack. A
    stale hit starts the background refresh. ``None`` means the caller must
    take the full render path (no session id, or no usable entry).
    """
    session_id = _session_id(_decode_payload(raw))
    if session_id is None:
        return None
    cache = StatuslineCache(cache_path_for(session_id))
    return cache.serve_cached(lambda: _spawn_refresh(raw, workspace=None, theme_name=theme_name))


def run_with_cache(
    *,
    workspace: Path | None,
    theme_name: str | None,
) -> str:
    """Cache-aware entry point: read Claude stdin → return one statusline.

    A payload without a session id renders uncached. Otherwise the session's
    entry is served stale-while-revalidate: a hit returns the cached line
    (a stale one also starts the background refresh) and a miss renders
    synchronously and writes the entry.
    """
    raw = sys.stdin.read()
    claude_payload = _decode_payload(raw)
    session_id = _session_id(claude_payload)
    if session_id is None:
        return render_pipeline(claude_payload, workspace=workspace, theme_name=theme_name)
    cache = StatuslineCache(cache_path_for(session_id))
    return cache.serve(
        lambda: _render_session(
            claude_payload, session_id, workspace=workspace, theme_name=theme_name
        ),
        refresh=lambda: _spawn_refresh(raw, workspace=workspace, theme_name=theme_name),
    )


def prewarm(
    *,
    workspace: Path | None,
    theme_name: str | None,
) -> str:
    """Render the line from Claude stdin and write it to the session's cache.

    This is the background refresh worker :func:`run_with_cache` spawns, and
    a manual way to warm the cache. Returns the rendered line. When
    ``session_id`` is missing or not a string, the line is rendered but not
    cached.
    """
    claude_payload = _decode_payload(sys.stdin.read())
    session_id = _session_id(claude_payload)
    if session_id is None:
        return render_pipeline(claude_payload, workspace=workspace, theme_name=theme_name)
    line = _render_session(claude_payload, session_id, workspace=workspace, theme_name=theme_name)
    StatuslineCache(cache_path_for(session_id)).write(line)
    return line


__all__ = [
    "cache_path_for",
    "prewarm",
    "render_pipeline",
    "run_with_cache",
    "serve_cached_line",
]
