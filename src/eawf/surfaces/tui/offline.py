"""Deterministic, non-interactive text renderers for the ``tui`` surface.

The interactive Textual app (:mod:`eawf.surfaces.tui.app`) only runs at a TTY.
Headless callers — piped ``eawf`` / ``eawf tui``, ``--plain`` /
``--no-input``, CI scrapes, and ``eawf workspace registry-status`` — need a
deterministic single-frame text emission that never opens a Textual screen.
This module owns both:

* :func:`build_status_text` + :func:`emit_status` — the repo/workspace
  *status frame* the bare-``eawf`` / ``eawf tui`` non-TTY fallback prints
  (``Eä  <breadcrumb>`` header + a one-line lifecycle-count summary +
  a ``keymap:`` line). Exit code is always ``0``; no Textual paint.
* :func:`offline_render` — the *workspace registry dashboard* rendered to a
  plain-text frame for ``workspace registry-status`` (and its JSON
  envelope's ``rendered`` field). Strictly read-only over the registry.

Both renderers reuse the canonical brand literal (:data:`eawf.surfaces.render.brand.BRAND_LITERAL`)
and the typed-state breadcrumb (:func:`eawf.surfaces.tui.widgets.header.build_breadcrumb`)
so the headless surface stays byte-consistent with the interactive header.
``width`` is honoured via :func:`textwrap.fill` so narrow callers wrap.

This module replaces the two live consumers of the deleted legacy
``src/eawf/surfaces/tui/`` tree (its ``run_tui`` offline mode + its workspace
``offline_render``); the legacy Rich-Layout renderers are gone and
``tui`` owns these paths.
"""

from __future__ import annotations

import logging
import textwrap
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.surfaces.render.brand import BRAND_LITERAL
from eawf.surfaces.tui.state_binding import load_state
from eawf.surfaces.tui.widgets.footer import DEFAULT_HINTS, format_hints
from eawf.surfaces.tui.widgets.header import DEFAULT_PROJECT_CODE, build_breadcrumb

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.platform.registry import Registry

logger = logging.getLogger(__name__)

#: Two-space gap between the brand literal and the first breadcrumb
#: segment, matching :func:`eawf.surfaces.render.brand.render_breadcrumb_head`.
_BRAND_GAP: str = "  "

#: Placeholder rendered in the workspace strip when the registry file is
#: missing or fails to load. The substring ``registry unavailable`` is
#: part of the ``workspace registry-status`` text contract.
_REGISTRY_UNAVAILABLE: str = "registry unavailable (read failed)"


def _status_counts(state: State | None) -> dict[str, int]:
    """Count active phases, active/closed iters, pending waves, and audits.

    All counts are zero for ``None`` state (fresh workspace / daemon
    cold-spawn) so the frame stays deterministic before any roadmap
    activity.

    Args:
        state: The loaded typed state, or ``None``.

    Returns:
        A mapping of count name to value.
    """
    if state is None:
        return {
            "phases_open": 0,
            "iters_open": 0,
            "iters_closed": 0,
            "waves_pending": 0,
            "audits": 0,
        }
    from eawf.kernel.state.enums import IterStatus, PhaseStatus, WaveStatus

    phases_open = sum(1 for p in state.phases.values() if p.status is PhaseStatus.ACTIVE)
    iters_open = sum(1 for it in state.iters.values() if it.status is IterStatus.ACTIVE)
    iters_closed = sum(1 for it in state.iters.values() if it.status is IterStatus.CLOSED)
    waves_pending = sum(1 for w in state.waves.values() if w.status is WaveStatus.PENDING)
    audits = len(state.audits or {})
    return {
        "phases_open": phases_open,
        "iters_open": iters_open,
        "iters_closed": iters_closed,
        "waves_pending": waves_pending,
        "audits": audits,
    }


def build_status_text(state: State | None) -> str:
    """Build the deterministic single-frame status text from typed *state*.

    The three-line frame is the non-TTY fallback contract for bare
    ``eawf`` / ``eawf tui``:

    1. ``Eä  <breadcrumb>`` — brand outside-left of the scope breadcrumb.
    2. ``  project=<code> phases_open=N iters_open=N ...`` — a one-line
       lifecycle-count summary.
    3. ``keymap: <hints>`` — the shared footer key hints.

    Args:
        state: The loaded typed state, or ``None`` for a fresh workspace.

    Returns:
        The rendered status frame (no trailing newline).
    """
    breadcrumb = build_breadcrumb(state)
    counts = _status_counts(state)
    code = DEFAULT_PROJECT_CODE
    if state is not None and state.project is not None:
        code = state.project.code
    return (
        f"{BRAND_LITERAL}{_BRAND_GAP}{breadcrumb}\n"
        f"  project={code} "
        f"phases_open={counts['phases_open']} "
        f"iters_open={counts['iters_open']} "
        f"iters_closed={counts['iters_closed']} "
        f"waves_pending={counts['waves_pending']} "
        f"audits={counts['audits']}\n"
        f"keymap: {format_hints(DEFAULT_HINTS)}"
    )


def emit_status(
    *,
    workspace: Path | None = None,
    no_input: bool = False,
    plain: bool = False,
) -> int:
    """Print the deterministic status frame and return a clean exit code.

    The non-TTY / ``--plain`` / ``--no-input`` fallback for the bare
    ``eawf`` / ``eawf tui`` dispatch. Loads ``<workspace>/.ea/state.json``
    read-only (best effort — a missing or corrupt file degrades to the
    fresh-workspace placeholder frame) and prints :func:`build_status_text`.

    Args:
        workspace: Workspace root containing ``.ea/state.json``; defaults
            to the current working directory.
        no_input: Accepted for call-site parity with the interactive
            launcher; the status frame is identical regardless.
        plain: Accepted for call-site parity; the status frame carries no
            colour or markup, so plain mode is the only mode here.

    Returns:
        ``0`` — the status emission never fails.
    """
    base = workspace if workspace is not None else Path.cwd()
    state = load_state(base / ".ea" / "state.json")
    print(build_status_text(state))
    return 0


def _workspace_breadcrumb(registry: Registry | None) -> str:
    """Build the workspace dashboard breadcrumb head from the registry.

    Uses the active repo's code when the registry resolves one, else the
    :data:`DEFAULT_PROJECT_CODE` placeholder so the header stays
    informative for an empty or unavailable registry.

    Args:
        registry: The loaded registry, or ``None`` when unavailable.

    Returns:
        The breadcrumb string (without the brand prefix).
    """
    if registry is None:
        return DEFAULT_PROJECT_CODE
    if registry.active_code is not None:
        return registry.active_code
    if registry.repos:
        return sorted(registry.repos)[0]
    return DEFAULT_PROJECT_CODE


def _strip_line(registry: Registry | None, *, is_stale_at: dict[str, bool]) -> str:
    """Build the one-line repo strip ``CODE [chips]  CODE [chips]`` row.

    Each repo renders as its code, an ``(active)`` chip on the active
    entry, and a ``(stale)`` chip when the staleness predicate fired.

    Args:
        registry: The loaded registry, or ``None`` when unavailable.
        is_stale_at: Per-code staleness flags keyed by repo code.

    Returns:
        The strip row text, or the unavailable placeholder.
    """
    if registry is None or not registry.repos:
        return _REGISTRY_UNAVAILABLE
    cells: list[str] = []
    for code in sorted(registry.repos):
        chips: list[str] = []
        if code == registry.active_code:
            chips.append("(active)")
        if is_stale_at.get(code):
            chips.append("(stale)")
        suffix = f" {' '.join(chips)}" if chips else ""
        cells.append(f"{code}{suffix}")
    return "  ".join(cells)


def offline_render(
    *,
    registry_path: Path | None = None,
    home: Path | None = None,
    now: datetime | None = None,
    width: int = 100,
) -> str:
    """Render one workspace-dashboard frame to a plain-text string.

    Strictly read-only over ``~/.eawf/registry.json``: resolves the
    registry, computes per-repo staleness, and emits a labelled-section
    text frame (``workspace`` strip · ``roadmap`` / ``status`` /
    ``git`` / ``backlog`` sections). When the registry is missing or
    fails validation the frame still renders, with the strip carrying the
    ``registry unavailable`` placeholder instead of repo cells.

    Used by ``workspace registry-status`` (and its JSON envelope's
    ``rendered`` field). Replaces the deleted legacy workspace
    ``offline_render`` Rich-Layout renderer.

    Args:
        registry_path: Explicit registry path; ``None`` falls back to
            :func:`eawf.platform.registry.default_registry_path`.
        home: Test seam for the default-path branch.
        now: Override for the current timestamp threaded to the staleness
            predicate so freshness comparisons stay deterministic.
        width: Column width for line wrapping; narrow widths wrap the
            strip + section lines so the output differs from a wide
            render.

    Returns:
        The rendered text frame (terminated by a trailing newline).
    """
    from eawf.platform.registry import (
        RegistryReadError,
        is_stale,
        read_registry,
        registry_mtime,
    )

    registry: Registry | None
    try:
        registry = read_registry(path=registry_path, home=home)
        mtime = registry_mtime(path=registry_path, home=home)
    except RegistryReadError as exc:
        logger.info(f"offline_render registry unavailable cause={exc!r}")
        registry = None
        mtime = None

    is_stale_at: dict[str, bool] = {}
    if registry is not None:
        for code, entry in registry.repos.items():
            is_stale_at[code] = is_stale(entry, registry_mtime_at=mtime, now=now)

    breadcrumb = _workspace_breadcrumb(registry)
    code = breadcrumb if registry is not None else DEFAULT_PROJECT_CODE
    repo_count = len(registry.repos) if registry is not None else 0
    active = registry.active_code if registry is not None else None

    lines: list[str] = [
        f"{BRAND_LITERAL}{_BRAND_GAP}{breadcrumb}",
        "",
        "workspace",
        _strip_line(registry, is_stale_at=is_stale_at),
        "",
        "roadmap",
        f"  repos: {repo_count}",
        f"  active: {active if active is not None else '-'}",
        "",
        "status",
        f"  project: {code}",
        f"  registry: {'available' if registry is not None else 'unavailable'}",
        "",
        "git",
        "  branch: -",
        "",
        "backlog",
        f"  repos tracked: {repo_count}",
        "",
        f"keymap: {format_hints(DEFAULT_HINTS)}",
    ]

    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        wrapped.append(
            textwrap.fill(
                line,
                width=max(1, width),
                subsequent_indent="  ",
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return "\n".join(wrapped) + "\n"


__all__ = [
    "build_status_text",
    "emit_status",
    "offline_render",
]
