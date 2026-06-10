"""Orchestrator for ``eawf cc statusline`` (Phase 4 W06).

Reads Claude's stdin JSON, calls each statusline module in fixed order,
applies the configured theme, writes one line to stdout. Modules degrade
independently — any exception inside a module is caught here and rendered
as a ``status="failed"`` segment so the whole pipeline never crashes.

Design notes:

- Module order is fixed (left-to-right): ``state``, ``git``,
  ``model_session_cwd``, ``context_tokens``, ``mcp_health``,
  ``hooks_plugins``, ``memory``, ``token_saving``.
- Every module signature is uniform: ``build(claude_payload, state_path) ->
  StatuslineSegment``. The orchestrator iterates the call list to keep the
  cold path tight (no introspection, no plugin loop).
- Cache hit path: when a per-session cache file exists at
  ``~/.claude/statusline-cache/<session-id>.json`` and is fresh, the
  orchestrator returns its cached line directly. The prewarm worker
  (:func:`prewarm`) writes that file in the background so subsequent
  ``statusline`` invocations skip the full module call list.
- Cache freshness: a cache entry is considered fresh for the session id
  for which it was written; staleness checks beyond that are reserved for
  Phase 5.

Public surface:

- :func:`render_pipeline` — full module call + theme apply (always cold).
- :func:`run_with_cache` — cache-aware entry point used by the CLI.
- :func:`prewarm` — write the rendered line to the cache, exit silently.
- :func:`cache_path_for` — cache-path resolver (exposed so the CLI can
  echo it under ``--json``).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import orjson

from eawf.runtime.runtime_counter_sidecar import (
    RuntimeCounterSidecar,
    sidecar_path_for_statusline_cache,
)
from eawf.runtime.runtimes.claude.runtime_counters import parse_runtime_counters
from eawf.runtime.runtimes.claude.statusline_modules import (
    context_tokens,
    git,
    hooks_plugins,
    mcp_health,
    memory,
    model_session_cwd,
    state,
    token_saving,
)
from eawf.surfaces.cli.scope import resolve_state_path
from eawf.surfaces.render.statusline import (
    StatuslineSegment,
    StatuslineTheme,
    load_themes,
    render_segments,
    resolve_theme,
)

logger = logging.getLogger(__name__)


_MODULE_ORDER: list[Any] = [
    state,
    git,
    model_session_cwd,
    context_tokens,
    mcp_health,
    hooks_plugins,
    memory,
    token_saving,
]
"""Ordered list of statusline modules. Each must expose a ``build``
function with the signature
``build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment``.
"""


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


def _read_payload_from_stdin() -> dict[str, Any]:
    """Decode Claude's stdin JSON into a dict.

    Empty stdin → empty dict. Decode errors → empty dict (logged at debug
    level). The orchestrator must never raise here — a bad upstream
    payload is a degradation, not a crash.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        decoded: Any = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        logger.debug(f"_read_payload_from_stdin stdin-json-decode-failed error={exc}")
        return {}
    if not isinstance(decoded, dict):
        logger.debug(f"_read_payload_from_stdin stdin-not-mapping got={type(decoded).__name__}")
        return {}
    return decoded


def _safe_resolve_state_path(workspace: Path | None) -> Path | None:
    """Best-effort wrapper around :func:`resolve_state_path` returning ``None``."""
    try:
        return resolve_state_path(workspace)
    except FileNotFoundError:
        return None


def _build_segments(
    claude_payload: dict[str, Any],
    state_path: Path | None,
) -> list[StatuslineSegment]:
    """Call every module, wrapping exceptions into a ``failed`` segment."""
    segments: list[StatuslineSegment] = []
    for module in _MODULE_ORDER:
        module_name = module.__name__.rsplit(".", 1)[-1]
        try:
            segment = module.build(claude_payload, state_path)
        except Exception as exc:
            # Orchestrator must never crash — every module exception
            # collapses to a ``failed`` segment so the theme can decide
            # whether to skip or render ``<module>:!``.
            logger.warning(f"_build_segments module-raised module={module_name!r} error={exc}")
            segments.append(
                StatuslineSegment(module=module_name, text=f"{module_name}:!", status="failed")
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

    No caching — the cold path. Used directly by :func:`prewarm` and as the
    fallback inside :func:`run_with_cache`.
    """
    state_path = _safe_resolve_state_path(workspace)
    segments = _build_segments(claude_payload, state_path)
    theme = _resolve_active_theme(theme_name)
    return render_segments(segments, theme)


def _resolve_active_theme(theme_name: str | None) -> StatuslineTheme:
    """Resolve the active theme honoring CLI flag + env var."""
    name = theme_name
    if not name:
        env_name = os.environ.get("EAWF_STATUSLINE_THEME")
        if env_name:
            name = env_name
    themes = load_themes()
    return resolve_theme(name, themes)


def _read_cached_line(session_id: str) -> str | None:
    """Return the cached statusline for *session_id*, or ``None`` on miss.

    Cache miss reasons (any of which collapses to ``None``): file doesn't
    exist, file is empty, file fails JSON decode, the JSON is not a
    mapping, or the mapping has no ``line`` field.
    """
    if not session_id:
        return None
    path = cache_path_for(session_id)
    if not path.exists():
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        logger.debug(f"_read_cached_line cache-read-failed session={session_id!r} error={exc}")
        return None
    if not raw.strip():
        return None
    try:
        decoded: Any = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        logger.debug(
            f"_read_cached_line cache-json-decode-failed session={session_id!r} error={exc}"
        )
        return None
    if not isinstance(decoded, dict):
        return None
    line = decoded.get("line")
    if not isinstance(line, str):
        return None
    return line


def _write_cached_line(session_id: str, line: str) -> None:
    """Write *line* to the cache file for *session_id* (best-effort)."""
    if not session_id:
        return
    path = cache_path_for(session_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = orjson.dumps({"session_id": session_id, "line": line})
        path.write_bytes(body)
    except OSError as exc:
        logger.debug(f"_write_cached_line cache-write-failed session={session_id!r} error={exc}")


def _write_runtime_counter_sidecar(session_id: str, claude_payload: dict[str, Any]) -> None:
    """Persist parsed runtime counters beside the statusline cache, best-effort."""
    if not session_id:
        return
    counters = parse_runtime_counters(claude_payload)
    if counters is None:
        return
    path = sidecar_path_for_statusline_cache(cache_path_for(session_id))
    RuntimeCounterSidecar(path).write(counters)


def run_with_cache(
    *,
    workspace: Path | None,
    theme_name: str | None,
) -> str:
    """Cache-aware entry point: read Claude stdin → return one statusline.

    Strategy:

    1. Decode Claude stdin (empty/garbage → empty dict; never raises).
    2. If the payload carries a ``session_id`` and the cache hit succeeds,
       return the cached line verbatim.
    3. Otherwise run the full :func:`render_pipeline` and return its output.
       (We do **not** populate the cache from the cold path — prewarm is
       the only writer per the W06 acceptance contract.)
    """
    claude_payload = _read_payload_from_stdin()
    session_id = claude_payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        _write_runtime_counter_sidecar(session_id, claude_payload)
        cached = _read_cached_line(session_id)
        if cached is not None:
            return cached
    return render_pipeline(claude_payload, workspace=workspace, theme_name=theme_name)


def prewarm(
    *,
    workspace: Path | None,
    theme_name: str | None,
) -> str:
    """Run the full render pipeline and write the line to the cache.

    Reads Claude stdin (the same shape ``run_with_cache`` consumes),
    renders, and writes ``{"session_id": <id>, "line": <statusline>}`` to
    ``cache_path_for(session_id)``. Returns the rendered line so the CLI
    can echo it on demand. When ``session_id`` is missing or not a
    string, the line is rendered but not cached.
    """
    claude_payload = _read_payload_from_stdin()
    line = render_pipeline(claude_payload, workspace=workspace, theme_name=theme_name)
    session_id = claude_payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        _write_runtime_counter_sidecar(session_id, claude_payload)
        _write_cached_line(session_id, line)
    return line


__all__ = [
    "cache_path_for",
    "prewarm",
    "render_pipeline",
    "run_with_cache",
]
