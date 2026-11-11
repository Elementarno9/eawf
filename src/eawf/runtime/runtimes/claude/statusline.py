"""Orchestrator for ``eawf cc statusline``.

Reads Claude's stdin JSON, builds the line-1 segments and the bar rows, and
writes them as powerline rows in the resolved theme. Modules
degrade independently: any exception inside a module is caught here and
becomes a ``status="failed"`` segment, which the renderer omits, so the whole
pipeline never crashes.

Design notes:

- Line 1 is built from a fixed list of builders, left to right: model,
  project, active time, cost, MCP, hooks, then the Run's scope and budget.
  The ``git``, ``memory`` and ``token_saving`` modules are not in the layout.
- Under line 1 come the ``ctx``, ``5h`` and ``7d`` bar rows: the context
  with its cache-read share, each limit window with its pace against an even
  spend, all read from the payload alone.
- Every segment carries a truth field naming its producer, freshness and
  measurement quality; a value a module cannot source is omitted from the
  rows, never drawn as a marker.
- Every redraw renders synchronously, so a line is never older than the
  payload it answers. The render runs on a daemon thread under a 250 ms
  budget; when it raises or overruns, the session's last line from
  :class:`~eawf.runtime.statusline_cache.StatuslineCache` is printed instead
  and a detached ``eawf cc statusline prewarm`` renders and rewrites it; a
  claim file keeps it to one prewarm in flight per session.
  Entries are keyed by the theme and glyphs the flag and the environment
  name, so a fallback never shows a line drawn in another style.
- Claude Code runs the statusline on every redraw, so this module imports
  nothing heavy at load time: the modules, the renderer, the state resolver
  and the counter sidecar load only when a render actually runs. The render
  key it does compute reads the environment alone.
- The runtime counter sidecar is written by every render, the redraw's own
  or a background one.

Public surface:

- :func:`render_pipeline` — full module call + theme apply.
- :func:`serve_line` — the budgeted render with its cached fallback, for the
  console entry point.
- :func:`run_with_cache` — :func:`serve_line` over stdin, used by the CLI.
- :func:`prewarm` — render, write the cache entry, exit silently.
- :func:`cache_path_for` — cache-path resolver, shared with the counter
  sidecar readers.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import orjson

from eawf.runtime.runtimes.claude.statusline_theme import render_key
from eawf.runtime.statusline_cache import StatuslineCache

if TYPE_CHECKING:
    import logging

    from eawf.surfaces.render.statusline import LineStyle, PowerlineBar, StatuslineSegment

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


def drop_cached_lines() -> int:
    """Delete every session's cached rows, so each next redraw renders afresh.

    A cached line would show state an operator just changed, such as
    acknowledged hook failures, until its refresh lands. Sidecars beside an
    entry (``<session>.<kind>.json``) are kept; a session id with a dot in
    it keeps its entry too and catches up on its next refresh.

    Best-effort: an entry that cannot be deleted is skipped and the rest are
    still dropped; it catches up on its session's next render.

    Returns:
        How many entries were deleted; ``0`` when the cache directory is absent.
    """
    root = _cache_root()
    if not root.is_dir():
        return 0
    dropped = 0
    for path in root.glob("*.json"):
        if "." in path.stem:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _logger().debug(f"drop_cached_lines unlink-failed path={path!r} error={exc}")
            continue
        dropped += 1
    return dropped


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


def _line_style(theme_name: str | None, state_path: Path | None) -> LineStyle:
    """Return the style this render draws in: flag and environment over the config.

    The config is read from the repository *state_path* lies in, or from the
    global layer alone outside one.
    """
    from eawf.runtime.runtimes.claude.statusline_theme import (
        glyphs_override,
        statusline_config,
        theme_override,
    )
    from eawf.surfaces.render.statusline import LineStyle, load_theme

    repo_root = state_path.parent.parent if state_path is not None else None
    config = statusline_config(repo_root)
    theme = theme_override(theme_name, os.environ) or config.theme
    return LineStyle(
        theme=None if theme == "none" else load_theme(theme, ink=config.ink),
        glyphs=glyphs_override(os.environ) or config.glyphs,
        bar_width=config.bar_width,
    )


def render_pipeline(
    claude_payload: dict[str, Any],
    *,
    workspace: Path | None,
    theme_name: str | None = None,
) -> str:
    """Run every module and draw the powerline rows, joined by newlines.

    No caching — the cold path, used for a cache miss and by :func:`prewarm`.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        workspace: The ``--workspace`` the state path is resolved from.
        theme_name: The ``--theme`` value; ``None`` leaves the theme to the
            environment and the config.

    Returns:
        The rows, top to bottom, without a trailing newline;
        :func:`_minimal_line` when no row, or an empty first row, could be
        drawn, since the host shows an empty output as a blank status line.
    """
    from eawf.surfaces.render.statusline import render_powerline

    state_path = _safe_resolve_state_path(workspace)
    segments = _build_segments(claude_payload, state_path)
    bars = _build_bars(claude_payload, datetime.now(UTC))
    style = _line_style(theme_name, state_path)
    rows = render_powerline(segments, bars, style, width=_columns())
    if not rows or not rows[0].strip():
        return _minimal_line(claude_payload)
    return "\n".join(rows)


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
    """Record the session's counters, then render its rows."""
    _write_runtime_counter_sidecar(session_id, claude_payload)
    return render_pipeline(claude_payload, workspace=workspace, theme_name=theme_name)


_REFRESH_EXPIRY_SECONDS: Final = 10.0
"""How long one session's refresh claim holds off another prewarm.

A prewarm releases the claim when it has written the entry; the expiry
covers one that crashed or hung, so it cannot block refreshes for good.
"""


def _refresh_claim_for(session_id: str) -> Path:
    """Return the claim file marking a prewarm in flight for *session_id*.

    Its suffix is not ``.json``, so :func:`drop_cached_lines` never takes it
    for an entry.
    """
    entry = cache_path_for(session_id)
    return entry.with_name(f"{entry.stem}.refresh")


def _claim_refresh(session_id: str, *, now: float) -> bool:
    """Claim the session's one refresh slot; ``True`` when a prewarm may start.

    The claim file is created exclusively, so of the redraws that overrun
    together only one spawns. A claim older than :data:`_REFRESH_EXPIRY_SECONDS`
    is taken over by re-stamping it. When no claim can be recorded at all the
    cache directory is unwritable, and a prewarm could not write its entry
    either, so none starts.
    """
    claim = _refresh_claim_for(session_id)
    try:
        claim.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        try:
            if now - claim.stat().st_mtime < _REFRESH_EXPIRY_SECONDS:
                return False
            os.utime(claim, (now, now))
        except OSError as exc:
            # Released by the prewarm between the two calls, or unwritable.
            _logger().debug(f"_claim_refresh claim-retake-failed error={exc}")
            return False
    except OSError as exc:
        _logger().debug(f"_claim_refresh claim-failed error={exc}")
        return False
    return True


def _release_refresh(session_id: str) -> None:
    """Remove the session's refresh claim, so the next overrun may refresh again."""
    try:
        _refresh_claim_for(session_id).unlink(missing_ok=True)
    except OSError as exc:
        _logger().debug(f"_release_refresh claim-unlink-failed error={exc}")


def _spawn_refresh(
    raw: str, *, workspace: Path | None, theme_name: str | None, stamp: float
) -> None:
    """Start a detached ``eawf cc statusline prewarm`` fed *raw* on stdin.

    The payload goes through an unlinked temporary file rather than a pipe
    so the caller never blocks on a child that is still importing. The
    child gets its own session so it outlives the redraw that started it,
    and the redraw's payload *stamp*, so its entry never replaces one
    answering a later payload. A failed spawn is logged at debug; the entry
    keeps its old line and the next redraw renders again.
    """
    import subprocess
    import tempfile

    from eawf.platform.subprocess_detach import no_window_kwargs

    argv = [sys.executable, "-m", "eawf"]
    if workspace is not None:
        argv += ["--workspace", str(workspace)]
    argv += ["cc", "statusline", "prewarm", "--payload-at", repr(stamp)]
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


_RENDER_BUDGET_SECONDS: Final = 0.25
"""How long a redraw waits for its render before printing the cached line.

The host debounces redraws at 300 ms, so a render still running past this
would leave the previous line up anyway; the cached line at least carries
the session's last known state.
"""

_NO_CACHE_WAIT_SECONDS: Final = 3.0
"""How long a redraw with no cached line waits for an overrunning render.

Nothing else describes the session then, so the render gets longer than
:data:`_RENDER_BUDGET_SECONDS`, but a hung one must not hold the host's
redraw: past this the minimal line is printed.
"""


@dataclass(frozen=True)
class ServedLine:
    """The rows one redraw prints.

    Attributes:
        text: The rows, top to bottom, without a trailing newline.
        render_running: ``True`` when *text* is a fallback printed while the
            redraw's own render still runs on its daemon thread; the caller
            should exit without waiting for it.
    """

    text: str
    render_running: bool = False


class _RenderAttempt:
    """One render on a daemon thread, with its line or its exception."""

    def __init__(self, render: Callable[[], str]) -> None:
        """Hold *render* until :meth:`start` runs it."""
        self._render = render
        self.done = threading.Event()
        self.line: str | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        """Start the render; :attr:`done` is set once it returns or raises."""
        threading.Thread(target=self._run, name="eawf-statusline-render", daemon=True).start()

    def _run(self) -> None:
        try:
            self.line = self._render()
        except Exception as exc:
            self.error = exc
        finally:
            self.done.set()


def _await_render(done: threading.Event, budget_seconds: float) -> bool:
    """Wait up to *budget_seconds* for *done*; ``True`` when the render finished."""
    return done.wait(budget_seconds)


def _minimal_line(claude_payload: dict[str, Any]) -> str:
    """Return the plain model name, the line printed when nothing better exists.

    Falls back to ``eawf`` when the payload names no model, so the host
    never shows an empty status line for a failed render.
    """
    model = claude_payload.get("model")
    if isinstance(model, dict):
        model = model.get("display_name") or model.get("id") or model.get("name")
    return model if isinstance(model, str) and model else "eawf"


def serve_line(raw: str, *, workspace: Path | None, theme_name: str | None) -> ServedLine:
    """Render the rows for the payload in *raw*, falling back to the cached line.

    The render runs synchronously under :data:`_RENDER_BUDGET_SECONDS`; one
    that finishes in time is printed and cached, never the older cached line.
    When it raises, or overruns while the session has a cached line, that
    line is returned and a detached prewarm renders the entry anew, unless
    the session's one refresh is already in flight. With no
    cached line an overrun is waited on for up to
    :data:`_NO_CACHE_WAIT_SECONDS`, since nothing else describes the session;
    a failed or hung render with none prints :func:`_minimal_line`. A payload
    without a session id has no cache and renders on the caller's time.

    Args:
        raw: Claude's stdin JSON, undecoded.
        workspace: The ``--workspace`` the state path is resolved from.
        theme_name: The ``--theme`` value, or ``None``.

    Returns:
        The rows to print, and whether the render still runs.
    """
    claude_payload = _decode_payload(raw)
    session_id = _session_id(claude_payload)
    if session_id is None:
        try:
            return ServedLine(
                render_pipeline(claude_payload, workspace=workspace, theme_name=theme_name)
            )
        except Exception as exc:
            _logger().warning(f"serve_line render-raised error={exc}")
            return ServedLine(_minimal_line(claude_payload))
    stamp = time.time()
    cache = StatuslineCache(cache_path_for(session_id), key=render_key(theme_name, os.environ))
    cached = cache.get()
    attempt = _RenderAttempt(
        lambda: _render_session(
            claude_payload, session_id, workspace=workspace, theme_name=theme_name
        )
    )
    attempt.start()
    if not _await_render(attempt.done, _RENDER_BUDGET_SECONDS) and cached is None:
        _await_render(attempt.done, _NO_CACHE_WAIT_SECONDS)
    if attempt.done.is_set() and attempt.line is not None:
        cache.write(attempt.line, stamp=stamp)
        return ServedLine(attempt.line)
    if attempt.error is not None:
        _logger().warning(f"serve_line render-raised error={attempt.error}")
    if _claim_refresh(session_id, now=time.time()):
        _spawn_refresh(raw, workspace=workspace, theme_name=theme_name, stamp=stamp)
    return ServedLine(
        cached if cached is not None else _minimal_line(claude_payload),
        render_running=not attempt.done.is_set(),
    )


def read_payload() -> str:
    """Return Claude's stdin as text, any byte that is not UTF-8 replaced.

    The bytes are read and decoded here rather than through the text stream,
    whose strict decoder raises on such a byte: a payload the line cannot use
    still has to leave the host a line, not a traceback.
    """
    return sys.stdin.buffer.read().decode("utf-8", errors="replace")


def run_with_cache(
    *,
    workspace: Path | None,
    theme_name: str | None,
) -> str:
    """Read Claude's stdin and return the rows :func:`serve_line` serves for it."""
    return serve_line(read_payload(), workspace=workspace, theme_name=theme_name).text


def prewarm(
    *,
    workspace: Path | None,
    theme_name: str | None,
    payload_at: float | None = None,
) -> str:
    """Render the rows from Claude stdin and write them to the session's cache.

    This is the background refresh worker :func:`serve_line` spawns, and
    a manual way to warm the cache. When ``session_id`` is missing or not a
    string, the rows are rendered but not cached. Rendered or not, the
    session's refresh claim is released, so the next overrun may refresh.

    Args:
        workspace: The ``--workspace`` the state path is resolved from.
        theme_name: The ``--theme`` value, or ``None``.
        payload_at: Unix seconds at which the spawning redraw received the
            payload; ``None`` for a manual run, stamped now. An entry
            answering a later payload is kept.

    Returns:
        The rendered rows.
    """
    stamp = time.time() if payload_at is None else payload_at
    claude_payload = _decode_payload(read_payload())
    session_id = _session_id(claude_payload)
    if session_id is None:
        return render_pipeline(claude_payload, workspace=workspace, theme_name=theme_name)
    try:
        line = _render_session(
            claude_payload, session_id, workspace=workspace, theme_name=theme_name
        )
        cache = StatuslineCache(cache_path_for(session_id), key=render_key(theme_name, os.environ))
        cache.write(line, stamp=stamp)
    finally:
        _release_refresh(session_id)
    return line


__all__ = [
    "ServedLine",
    "cache_path_for",
    "drop_cached_lines",
    "prewarm",
    "read_payload",
    "render_pipeline",
    "run_with_cache",
    "serve_line",
]
