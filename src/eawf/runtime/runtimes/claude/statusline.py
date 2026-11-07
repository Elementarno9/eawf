"""Orchestrator for ``eawf cc statusline``.

Reads Claude's stdin JSON, builds the line-1 segments and the bar rows, and
writes them as powerline rows in the light or the dark palette. Modules
degrade independently: any exception inside a module is caught here and
becomes a ``status="failed"`` segment, which the renderer omits, so the whole
pipeline never crashes.

Design notes:

- Line 1 is built from a fixed list of builders, left to right: model,
  project, active time, cost, MCP, hooks, then the Run's scope and budget.
  The ``git``, ``memory`` and ``token_saving`` modules are not in the layout.
- Under line 1 come the ``ctx``, ``5h`` and ``7d`` bar rows; each carries the
  trend of its used percentage since the session's previous render, read
  from a small per-session sidecar beside the cache entry.
- Every segment carries a truth field naming its producer, freshness and
  measurement quality; a value a module cannot source is omitted from the
  rows, never drawn as a marker.
- Caching is stale-while-revalidate through
  :class:`~eawf.runtime.statusline_cache.StatuslineCache`, one entry for
  each Claude session id, keyed by the resolved palette so a theme switch is
  a miss. A hit (fresh or stale) is served without rendering; a stale hit
  also starts a detached ``eawf cc statusline prewarm`` that renders and
  rewrites the entry. Only a miss renders on the caller's time.
- Claude Code runs the statusline on every redraw, so this module imports
  nothing heavy at load time: the modules, the renderer, the state resolver
  and the counter sidecar load only when a render actually runs. The palette
  resolver it does import reads settings files with ``orjson`` alone.
- The runtime counter sidecar is written whenever a render runs (a miss or
  a background refresh), so it trails the live payload by at most one TTL.

Public surface:

- :func:`render_pipeline` — full module call + palette apply (always cold).
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
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import orjson

from eawf.runtime.runtimes.claude.statusline_theme import PaletteName, resolve_palette
from eawf.runtime.statusline_cache import StatuslineCache

if TYPE_CHECKING:
    import logging

    from eawf.surfaces.render.statusline import PowerlineBar, StatuslineSegment

type SegmentBuilder = Callable[[dict[str, Any], Path | None], StatuslineSegment]


def _logger() -> logging.Logger:
    """Return this module's logger, importing :mod:`logging` on first use.

    ``logging`` costs about a third of a cached statusline redraw, and a
    cache hit logs nothing, so the import waits for the first log line.
    """
    import logging

    return logging.getLogger(__name__)


def _segment_builders() -> list[tuple[str, SegmentBuilder]]:
    """Return the line-1 builders in display order, each with its module id.

    Each takes ``(claude_payload, state_path)`` and returns a
    :class:`~eawf.surfaces.render.statusline.StatuslineSegment`. Imported
    here, not at module load, so a cache hit never pays for them.
    """
    from eawf.runtime.runtimes.claude.statusline_modules import (
        budget,
        cost,
        hooks_plugins,
        mcp_health,
        model_session_cwd,
        scope,
    )

    return [
        ("model", model_session_cwd.build_model),
        ("project", model_session_cwd.build_project),
        ("active_time", cost.build_active),
        ("cost", cost.build),
        ("mcp_health", mcp_health.build),
        ("hooks_plugins", hooks_plugins.build),
        ("scope", scope.build),
        ("budget", budget.build),
    ]


_CACHE_DIR_ENV: str = "EAWF_STATUSLINE_CACHE"
"""Optional override for the cache directory, used by tests to redirect
cache writes to a tmp dir without touching the real ``~/.claude/`` tree.
"""

_COLUMNS_ENV: Final = "COLUMNS"


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


def _levels_path_for(session_id: str) -> Path:
    """Return the trend sidecar beside the session's cache entry."""
    cache_path = cache_path_for(session_id)
    return cache_path.with_name(f"{cache_path.stem}.levels.json")


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
    """Call every line-1 builder, wrapping exceptions into a ``failed`` segment."""
    from eawf.runtime.runtimes.claude.statusline_modules._spine import one_read_per_render
    from eawf.surfaces.render.statusline import SegmentSource, unavailable_segment

    segments: list[StatuslineSegment] = []
    with one_read_per_render():
        for module_name, builder in _segment_builders():
            try:
                segments.append(builder(claude_payload, state_path))
            except Exception as exc:
                # Orchestrator must never crash: a builder exception becomes a
                # ``failed`` segment, which the renderer omits.
                _logger().warning(
                    f"_build_segments module-raised module={module_name!r} error={exc}"
                )
                source = SegmentSource(producer="eawf.statusline", provenance=builder.__module__)
                segments.append(
                    unavailable_segment(
                        module_name, module_name, "render-failed", source, status="failed"
                    )
                )
    return segments


def _build_bars(claude_payload: dict[str, Any], now: datetime) -> list[PowerlineBar]:
    """Return the ``ctx``, ``5h`` and ``7d`` rows the payload states, in that order.

    A producer that raises yields no rows of its own rather than failing the
    render, and never takes the other producer's rows with it.
    """
    from eawf.runtime.runtimes.claude.statusline_modules import context_tokens, rate_window

    bars: list[PowerlineBar] = []
    try:
        context = context_tokens.bar_row(claude_payload)
        if context is not None:
            bars.append(context)
    except Exception as exc:
        _logger().warning(f"_build_bars context-raised error={exc}")
    try:
        bars.extend(rate_window.bar_rows(claude_payload, now))
    except Exception as exc:
        _logger().warning(f"_build_bars rate-windows-raised error={exc}")
    return bars


def _read_levels(path: Path | None) -> dict[str, int]:
    """Return the used percentages the session's previous render recorded, by row."""
    if path is None:
        return {}
    try:
        decoded: Any = orjson.loads(path.read_bytes())
    except OSError, orjson.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        str(label): level
        for label, level in decoded.items()
        if isinstance(level, int) and not isinstance(level, bool)
    }


def _write_levels(path: Path | None, levels: Mapping[str, int]) -> None:
    """Record this render's used percentages for the next render's trend, best-effort."""
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(orjson.dumps(dict(levels)))
    except OSError as exc:
        _logger().debug(f"_write_levels levels-write-failed error={exc}")


def _with_trends(bars: list[PowerlineBar], previous: Mapping[str, int]) -> list[PowerlineBar]:
    """Return *bars*, each carrying its change in points since *previous* when known."""
    from dataclasses import replace

    return [
        replace(bar, trend=bar.percent - previous[bar.label]) if bar.label in previous else bar
        for bar in bars
    ]


def _columns() -> int | None:
    """Return the terminal width ``COLUMNS`` names, or ``None`` when it names none.

    Without a positive integer there is no width to fit against, and a
    guessed width would cut rows the terminal could show, so they render whole.
    """
    raw = os.environ.get(_COLUMNS_ENV, "").strip()
    if not raw.isdigit():
        return None
    columns = int(raw)
    return columns if columns > 0 else None


def render_pipeline(
    claude_payload: dict[str, Any],
    *,
    workspace: Path | None,
    palette: PaletteName,
    levels_path: Path | None = None,
) -> str:
    """Run every module and draw the powerline rows, joined by newlines.

    No caching — the cold path, used for a cache miss and by :func:`prewarm`.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        workspace: The ``--workspace`` the state path is resolved from.
        palette: The palette to draw in, resolved once by the caller so the
            rows and the cache key they are stored under always agree.
        levels_path: The session's trend sidecar, read for the previous
            render's percentages and rewritten with this render's; ``None``
            renders without trends.

    Returns:
        The rows, top to bottom, without a trailing newline.
    """
    from eawf.surfaces.render.statusline import load_palette, render_powerline

    state_path = _safe_resolve_state_path(workspace)
    segments = _build_segments(claude_payload, state_path)
    bars = _with_trends(_build_bars(claude_payload, datetime.now(UTC)), _read_levels(levels_path))
    _write_levels(levels_path, {bar.label: bar.percent for bar in bars})
    colours = load_palette(palette)
    return "\n".join(render_powerline(segments, bars, colours, width=_columns()))


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
    palette: PaletteName,
) -> str:
    """Record the session's counters, then render its rows with their trends."""
    _write_runtime_counter_sidecar(session_id, claude_payload)
    return render_pipeline(
        claude_payload,
        workspace=workspace,
        palette=palette,
        levels_path=_levels_path_for(session_id),
    )


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
    """Return the cached rows for the payload in *raw*, or ``None`` to render.

    The hit-only half of :func:`run_with_cache`, for the console entry
    point: it renders nothing, so it loads none of the render stack. A
    stale hit starts the background refresh. ``None`` means the caller must
    take the full render path (no session id, or no entry for this palette).
    """
    claude_payload = _decode_payload(raw)
    session_id = _session_id(claude_payload)
    if session_id is None:
        return None
    palette = resolve_palette(claude_payload, override=theme_name)
    cache = StatuslineCache(cache_path_for(session_id), key=palette)
    return cache.serve_cached(lambda: _spawn_refresh(raw, workspace=None, theme_name=theme_name))


def run_with_cache(
    *,
    workspace: Path | None,
    theme_name: str | None,
) -> str:
    """Cache-aware entry point: read Claude stdin → return the statusline rows.

    A payload without a session id renders uncached. Otherwise the session's
    entry for the resolved palette is served stale-while-revalidate: a hit
    returns the cached rows (a stale one also starts the background refresh)
    and a miss renders synchronously and writes the entry.
    """
    raw = sys.stdin.read()
    claude_payload = _decode_payload(raw)
    palette = resolve_palette(claude_payload, override=theme_name)
    session_id = _session_id(claude_payload)
    if session_id is None:
        return render_pipeline(claude_payload, workspace=workspace, palette=palette)
    cache = StatuslineCache(cache_path_for(session_id), key=palette)
    return cache.serve(
        lambda: _render_session(claude_payload, session_id, workspace=workspace, palette=palette),
        refresh=lambda: _spawn_refresh(raw, workspace=workspace, theme_name=theme_name),
    )


def prewarm(
    *,
    workspace: Path | None,
    theme_name: str | None,
) -> str:
    """Render the rows from Claude stdin and write them to the session's cache.

    This is the background refresh worker :func:`run_with_cache` spawns, and
    a manual way to warm the cache. Returns the rendered rows. When
    ``session_id`` is missing or not a string, the rows are rendered but not
    cached.
    """
    claude_payload = _decode_payload(sys.stdin.read())
    palette = resolve_palette(claude_payload, override=theme_name)
    session_id = _session_id(claude_payload)
    if session_id is None:
        return render_pipeline(claude_payload, workspace=workspace, palette=palette)
    line = _render_session(claude_payload, session_id, workspace=workspace, palette=palette)
    StatuslineCache(cache_path_for(session_id), key=palette).write(line)
    return line


__all__ = [
    "cache_path_for",
    "prewarm",
    "render_pipeline",
    "run_with_cache",
    "serve_cached_line",
]
