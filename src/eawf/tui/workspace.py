"""Workspace-scope dashboard for the Eä Rich TUI (P20-I01-W05).

A horizontally-striped top strip enumerates every repo in
``~/.eawf/registry.json``; below it the W02 repo-scope quadrant
(roadmap / status / git / backlog) renders for the *active* repo.
Strip entries that hit any of the three staleness signals carry an
inline ``(stale)`` chip in muted style so the operator can spot
repos that have not been touched in :data:`eawf.registry.STALE_AFTER`
without drilling in.

Layout sketch::

    +----------------------------------------------------------+
    | Eä  EAWF / P20 / P20-I01                                 |  ← header
    +----------------------------------------------------------+
    | < [EAWF] (active)   DEMO (stale)   OTHER >               |  ← strip
    +-------------------------------+--------------------------+
    | roadmap                       | status                   |
    | ...                           | ...                      |
    +-------------------------------+--------------------------+   ← W02
    | git                           | backlog                  |     quadrant
    | ...                           | ...                      |     (active
    +-------------------------------+--------------------------+      repo)
    | ↑↓ select repo  Enter focus  Esc back  ...               |  ← footer
    +----------------------------------------------------------+

**Strict file scope** — this module does NOT redefine the four pane
builders or :func:`build_quadrant` / :func:`build_frame` from
:mod:`eawf.tui.layout`. It only assembles them around a new top-strip
panel that knows how to render the registry-read view.

**Read-only registry surface** — per the
``feedback_explicit_registry_only`` memory note the workspace
dashboard never grows the registry. It calls
:func:`eawf.registry.read_registry` and bails to an empty-strip
placeholder when the registry is missing or malformed.

Keymap (success criterion 1): ``←/→`` cycle the active repo
selection in the top strip; ``Enter`` confirms the focus shift and
rebuilds the quadrant under the newly-focused entry; ``Esc`` exits
back to single-repo mode (the caller decides what "single-repo"
means; the dashboard returns control without mutation).
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

from eawf.registry import (
    Registry,
    RegistryReadError,
    RegistryRepoEntry,
    is_stale,
    read_registry,
    read_repo_state,
    registry_mtime,
)
from eawf.tui.layout import (
    BRAND,
    BRAND_STYLE,
    BREADCRUMB_STYLE,
    build_breadcrumb,
    build_header_panel,
    build_quadrant,
    repo_quadrant_panes,
)

logger = logging.getLogger(__name__)


#: Footer keymap hint for the workspace dashboard. Lists the new
#: strip-navigation keys ahead of the wave-board open key (``b``)
#: so the operator sees workspace-scope navigation first.
WORKSPACE_FOOTER_KEYMAP: str = "←/→ strip  Enter focus  Esc back  b board  q quit  (vim: h l)"

#: Style applied to the chip text rendered alongside stale entries.
STALE_CHIP_STYLE: str = "dim"

#: Style applied to the chip text rendered alongside the active entry.
ACTIVE_CHIP_STYLE: str = "bold green"

#: Separator between strip entries — two spaces so adjacent codes
#: stay distinct at the lowest terminal width the TUI supports.
_STRIP_SEPARATOR: str = "  "

#: Marker placeholder for an empty registry. Rendered when the
#: registry file is missing or read-failed so the strip stays
#: structurally identical (panel + body) even with zero entries.
_EMPTY_STRIP_PLACEHOLDER: str = "no repos registered (run `eawf init`)"


# ---------------------------------------------------------------------------
# View state (Pydantic v2; strict, ephemeral)
# ---------------------------------------------------------------------------


class WorkspaceViewState(BaseModel):
    """Ephemeral view state for the workspace dashboard.

    Tracks the strip cursor and which repo's quadrant the body
    currently shows. Strict (``extra="forbid"``) so a typo at
    construction fails fast.

    The selected_index points into a *sorted* view of the registry
    repos (alphabetical by code) so the cursor binding stays stable
    across renders even if the underlying dict iteration order
    changes between Python versions.

    Attributes:
        selected_index: 0-based cursor into the sorted repo list.
        focused_code: Project code of the repo whose quadrant the
            body currently renders. ``None`` means "show the strip's
            currently-cursored entry" — operator has not committed
            a focus with ``Enter`` yet.
    """

    model_config = ConfigDict(extra="forbid")

    selected_index: int = Field(default=0, ge=0)
    focused_code: str | None = None


# ---------------------------------------------------------------------------
# Strip + chip helpers (pure, state-shape agnostic)
# ---------------------------------------------------------------------------


def sorted_entries(registry: Registry) -> list[RegistryRepoEntry]:
    """Return registry entries sorted alphabetically by ``code``.

    Stable cursor positioning depends on a deterministic iteration
    order across renders. Dict insertion order is not portable across
    re-loads (the operator may re-init the registry in a different
    order) so we sort here once and the rest of the module follows
    the returned list.
    """
    return [registry.repos[code] for code in sorted(registry.repos)]


def initial_view_for(registry: Registry) -> WorkspaceViewState:
    """Build a fresh :class:`WorkspaceViewState` pre-pointed at the active repo.

    The dashboard's first frame should land the cursor on the registry's
    declared ``active_code`` (when present and known) rather than the
    alphabetically-first entry. Without this helper the operator has
    to press ``→`` to move the cursor onto their actual current repo
    every time they open the workspace view.

    Falls back to ``selected_index=0`` when the registry has no
    ``active_code`` or the active code is not in ``repos``.
    """
    entries = sorted_entries(registry)
    if not entries:
        return WorkspaceViewState()
    if registry.active_code and registry.active_code in registry.repos:
        for idx, entry in enumerate(entries):
            if entry.code == registry.active_code:
                return WorkspaceViewState(selected_index=idx)
    return WorkspaceViewState()


def active_code(
    registry: Registry,
    view: WorkspaceViewState,
    entries: list[RegistryRepoEntry] | None = None,
) -> str | None:
    """Resolve the active repo code from view + registry state.

    Resolution order:

    1. ``view.focused_code`` when it points to a known entry.
    2. The sorted entry at ``view.selected_index`` when in-bounds.
    3. :attr:`Registry.active_code` when it points to a known entry.
    4. ``None`` (empty registry).

    Args:
        registry: Loaded registry document.
        view: Current dashboard view state.
        entries: Optional pre-computed sorted entries (avoids a second
            sort when the caller already needed it).

    Returns:
        Project code of the active entry, or ``None`` when no entry
        is resolvable.
    """
    entry_list = entries if entries is not None else sorted_entries(registry)
    if view.focused_code and view.focused_code in registry.repos:
        return view.focused_code
    if entry_list:
        idx = max(0, min(view.selected_index, len(entry_list) - 1))
        return entry_list[idx].code
    if registry.active_code and registry.active_code in registry.repos:
        return registry.active_code
    return None


def render_chip_for(
    entry: RegistryRepoEntry,
    *,
    is_active: bool,
    stale: bool,
) -> Text:
    """Render one strip entry as a styled :class:`~rich.text.Text`.

    Layout: ``CODE`` plain → optional ``(active)`` chip → optional
    ``(stale)`` chip. The two chips are independent; an entry can
    be both active and stale (e.g. an active repo whose state.json
    has not been touched in >14 days).

    Args:
        entry: Registry entry being rendered.
        is_active: When ``True`` the entry gets the bold-green
            ``(active)`` chip; the code itself is also bolded so the
            operator's eye lands on it.
        stale: When ``True`` the muted ``(stale)`` chip trails the
            code (and any active chip).

    Returns:
        Rich :class:`Text` with up to three styled segments.
    """
    text = Text()
    code_style = "bold cyan" if is_active else BREADCRUMB_STYLE
    text.append(f"[{entry.code}]" if is_active else entry.code, style=code_style)
    if is_active:
        text.append(" ")
        text.append("(active)", style=ACTIVE_CHIP_STYLE)
    if stale:
        text.append(" ")
        text.append("(stale)", style=STALE_CHIP_STYLE)
    return text


def build_strip_text(
    registry: Registry,
    view: WorkspaceViewState,
    *,
    now: datetime | None = None,
    registry_mtime_at: datetime | None = None,
    is_stale_evaluator: Callable[[RegistryRepoEntry], bool] | None = None,
) -> Text:
    """Compose the full top-strip line as a single styled Text.

    Joins :func:`render_chip_for` outputs with :data:`_STRIP_SEPARATOR`.
    Falls back to :data:`_EMPTY_STRIP_PLACEHOLDER` when the registry
    has no repos so the panel still renders something rather than a
    blank line (which would look like a render bug).

    Args:
        registry: Loaded registry.
        view: Current view state (used to mark the active entry).
        now: Override for the "current" timestamp passed to
            :func:`eawf.registry.is_stale`.
        registry_mtime_at: Pre-resolved registry filesystem mtime.
            Tests inject a fixed value; runtime callers pass the
            output of :func:`eawf.registry.registry_mtime`.
        is_stale_evaluator: Optional override for the per-entry stale
            decision. The default branches to
            :func:`eawf.registry.is_stale` with the supplied
            ``registry_mtime_at`` + ``now``. Goldens and offline
            renderers inject a deterministic predicate so the
            snapshot does not depend on filesystem state.

    Returns:
        Rich :class:`Text` ready to wrap in a panel.
    """
    entries = sorted_entries(registry)
    if not entries:
        return Text(_EMPTY_STRIP_PLACEHOLDER, style=BREADCRUMB_STYLE)
    active = active_code(registry, view, entries)
    evaluator = is_stale_evaluator or (
        lambda entry: is_stale(entry, registry_mtime_at=registry_mtime_at, now=now)
    )
    line = Text()
    for idx, entry in enumerate(entries):
        if idx > 0:
            line.append(_STRIP_SEPARATOR)
        line.append_text(
            render_chip_for(entry, is_active=(entry.code == active), stale=evaluator(entry))
        )
    return line


def build_strip_panel(
    registry: Registry,
    view: WorkspaceViewState,
    *,
    now: datetime | None = None,
    registry_mtime_at: datetime | None = None,
    is_stale_evaluator: Callable[[RegistryRepoEntry], bool] | None = None,
) -> Panel:
    """Wrap :func:`build_strip_text` in a Rich Panel for the strip slot."""
    return Panel(
        build_strip_text(
            registry,
            view,
            now=now,
            registry_mtime_at=registry_mtime_at,
            is_stale_evaluator=is_stale_evaluator,
        ),
        title="workspace",
        border_style="dim",
    )


def build_empty_strip_panel(message: str) -> Panel:
    """Render a placeholder strip panel when the registry can't be read.

    Used by :func:`build_workspace_frame` when
    :func:`eawf.registry.read_registry` raises so the dashboard still
    renders a structurally-valid frame (header + strip + quadrant +
    footer) and only the strip's body changes shape.
    """
    return Panel(
        Text(message, style="dim"),
        title="workspace",
        border_style="dim",
    )


def build_workspace_footer_panel() -> Panel:
    """Workspace-specific footer hint with strip-nav keys first."""
    return Panel(Text(WORKSPACE_FOOTER_KEYMAP), title=None, border_style="dim")


# ---------------------------------------------------------------------------
# Header chassis (reuses W02 layout helpers; brand stays outside-left)
# ---------------------------------------------------------------------------


def workspace_header_for(active_state: dict[str, Any]) -> Panel:
    """Build the header strip for the workspace view.

    Reuses :func:`eawf.tui.layout.build_header_panel` so the brand +
    breadcrumb chassis stays byte-identical to the W02 quadrant header.
    The active repo's state dict drives the breadcrumb so the header
    shifts with the focus selection.
    """
    return build_header_panel(active_state)


def build_workspace_breadcrumb_text(active_state: dict[str, Any]) -> Text:
    """Backwards-compatibility helper — manual brand+breadcrumb builder.

    Mirrors :func:`eawf.tui.layout.build_brand_text` so future surfaces
    that want a strip-only header (no panel border) can re-use the
    text composition without depending on the panel wrapper.
    """
    breadcrumb = build_breadcrumb(active_state)
    text = Text()
    text.append(f"{BRAND}  ", style=BRAND_STYLE)
    text.append(breadcrumb, style=BREADCRUMB_STYLE)
    return text


# ---------------------------------------------------------------------------
# Frame composition
# ---------------------------------------------------------------------------


def build_workspace_frame(
    registry: Registry | None,
    view: WorkspaceViewState,
    *,
    now: datetime | None = None,
    registry_mtime_at: datetime | None = None,
    repo_state_loader: Callable[[Path], dict[str, Any] | None] | None = None,
    is_stale_evaluator: Callable[[RegistryRepoEntry], bool] | None = None,
) -> Layout:
    """Assemble header + strip + body (W02 quadrant) + footer into a Layout.

    The body row reuses :func:`eawf.tui.layout.build_quadrant` so the
    repo-scope quadrant is not duplicated. When the registry is empty
    or the active repo's state cannot be loaded, the quadrant renders
    against an empty state dict — every pane in W02 already handles
    that case via deterministic placeholders.

    Args:
        registry: Loaded :class:`Registry` document, or ``None`` to
            render the dashboard against an empty strip (used when
            :func:`eawf.registry.read_registry` raised).
        view: Current dashboard view state.
        now: Override for the "current" timestamp; threaded through
            to :func:`is_stale` and :func:`build_strip_text`.
        registry_mtime_at: Pre-resolved registry mtime. ``None`` is
            treated as fresh (caller decides whether to compute it).
        repo_state_loader: Test seam for the active repo state load.
            Defaults to :func:`eawf.registry.read_repo_state`.

    Returns:
        Rich :class:`Layout` ready to feed
        :class:`~rich.live.Live` or :func:`render_workspace`.
    """
    loader = repo_state_loader or read_repo_state
    active_state: dict[str, Any] = {}
    if registry is not None:
        active = active_code(registry, view)
        if active is not None and active in registry.repos:
            payload = loader(Path(registry.repos[active].path))
            if payload is not None:
                active_state = payload

    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="strip", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )
    layout["header"].update(workspace_header_for(active_state))
    if registry is None:
        layout["strip"].update(build_empty_strip_panel("registry unavailable (read failed)"))
    else:
        layout["strip"].update(
            build_strip_panel(
                registry,
                view,
                now=now,
                registry_mtime_at=registry_mtime_at,
                is_stale_evaluator=is_stale_evaluator,
            )
        )
    layout["body"].update(build_quadrant(repo_quadrant_panes(active_state)))
    layout["footer"].update(build_workspace_footer_panel())
    return layout


def render_workspace(
    registry: Registry | None,
    *,
    view: WorkspaceViewState | None = None,
    now: datetime | None = None,
    registry_mtime_at: datetime | None = None,
    repo_state_loader: Callable[[Path], dict[str, Any] | None] | None = None,
    is_stale_evaluator: Callable[[RegistryRepoEntry], bool] | None = None,
    console: Console | None = None,
) -> str:
    """Render the workspace dashboard into a string buffer.

    Offline callers (golden snapshots, headless ``--plain`` mode)
    consume this so they never block on an interactive
    :class:`rich.live.Live` loop. Mirrors the
    :func:`eawf.tui.app.render_layout` semantics so the workspace
    dashboard's offline path matches the quadrant's offline path.

    Args:
        registry: Loaded registry (or ``None`` for an empty strip).
        view: Optional view state; defaults to
            :func:`initial_view_for` when the registry is non-None
            (so the cursor lands on the registry's active repo),
            or a fresh :class:`WorkspaceViewState` otherwise.
        now: Override for the current timestamp.
        registry_mtime_at: Pre-resolved registry mtime.
        repo_state_loader: Test seam for the active state loader.
        is_stale_evaluator: Optional override for the per-entry stale
            predicate; threaded through to :func:`build_strip_text`.
        console: Optional pre-built console; when supplied the helper
            writes into the caller's console and returns ``""``.

    Returns:
        Captured render output when ``console`` is ``None``,
        otherwise an empty string.
    """
    if view is None:
        view = initial_view_for(registry) if registry is not None else WorkspaceViewState()
    buf = io.StringIO()
    real_console = console or Console(file=buf, force_terminal=False, width=100, record=False)
    layout = build_workspace_frame(
        registry,
        view,
        now=now,
        registry_mtime_at=registry_mtime_at,
        repo_state_loader=repo_state_loader,
        is_stale_evaluator=is_stale_evaluator,
    )
    real_console.print(layout)
    return buf.getvalue() if console is None else ""


# ---------------------------------------------------------------------------
# Keypress / view-state transitions (pure)
# ---------------------------------------------------------------------------


#: Keys that move the strip cursor left (towards alphabetically-lower codes).
_LEFT_KEYS: frozenset[str] = frozenset({"\x1b[D", "h"})
#: Keys that move the strip cursor right.
_RIGHT_KEYS: frozenset[str] = frozenset({"\x1b[C", "l"})
#: Keys that commit the cursor selection as the new focused repo.
_FOCUS_KEYS: frozenset[str] = frozenset({"\r", "\n"})
#: Keys that drop the explicit focus (Esc returns to "cursor follows strip").
_UNFOCUS_KEYS: frozenset[str] = frozenset({"\x1b"})


def apply_strip_key(
    view: WorkspaceViewState,
    key: str,
    *,
    registry: Registry,
) -> WorkspaceViewState:
    """Apply *key* to *view* against the strip cursor model.

    Pure function — does not touch any live :class:`rich.live.Live`
    instance. Unknown keys return the view unchanged so the live-loop
    caller can treat the helper as a fallthrough.

    Transitions:

    - Left arrow / ``h``: decrement ``selected_index`` (clamped at 0).
    - Right arrow / ``l``: increment ``selected_index`` (clamped at
      ``len(entries) - 1``).
    - Enter / Return: copy the cursor's code into ``focused_code``.
    - Esc: clear ``focused_code`` so the body follows the cursor again.

    Args:
        view: Current view state.
        key: Single keystroke (or ESC-prefixed CSI sequence).
        registry: Loaded registry; needed so the cursor stays in
            bounds when the strip shrinks/grows between renders.

    Returns:
        Updated :class:`WorkspaceViewState`.
    """
    entries = sorted_entries(registry)
    count = len(entries)
    if key in _LEFT_KEYS:
        new_idx = max(0, view.selected_index - 1)
        return view.model_copy(update={"selected_index": new_idx})
    if key in _RIGHT_KEYS:
        upper = max(0, count - 1)
        new_idx = min(upper, view.selected_index + 1)
        return view.model_copy(update={"selected_index": new_idx})
    if key in _FOCUS_KEYS:
        if not entries:
            return view
        idx = max(0, min(view.selected_index, count - 1))
        return view.model_copy(update={"focused_code": entries[idx].code})
    if key in _UNFOCUS_KEYS:
        return view.model_copy(update={"focused_code": None})
    return view


# ---------------------------------------------------------------------------
# Offline entry-point used by --plain / non-TTY callers and golden tests
# ---------------------------------------------------------------------------


def offline_render(
    *,
    registry_path: Path | None = None,
    home: Path | None = None,
    now: datetime | None = None,
    repo_state_loader: Callable[[Path], dict[str, Any] | None] | None = None,
    is_stale_evaluator: Callable[[RegistryRepoEntry], bool] | None = None,
    width: int = 100,
) -> str:
    """Render one workspace-dashboard frame to a string.

    Wraps :func:`read_registry` + :func:`render_workspace` so a single
    helper resolves the registry, computes the registry mtime, and
    emits a frozen frame. Used by:

    - Golden snapshot tests under ``tests/golden/tui/workspace_*.txt``.
    - The CLI ``workspace registry-status`` subcommand when the
      operator asks for a TTY-fallback preview.

    Args:
        registry_path: Explicit registry path; ``None`` falls back
            to :func:`eawf.registry.default_registry_path`.
        home: Test seam for the default-path branch.
        now: Override for the current timestamp.
        repo_state_loader: Test seam for the active-repo state loader.
        is_stale_evaluator: Optional override for the per-entry stale
            predicate; threaded through to :func:`render_workspace`.
        width: Console width passed to the :class:`Console` (matches
            the W02 golden snapshot fixture so the layouts share
            their column convention).

    Returns:
        Rendered text frame. When the registry is missing or fails
        validation the helper still returns a frame — the strip
        carries the "registry unavailable" placeholder instead.
    """
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=width, record=False)
    try:
        registry: Registry | None = read_registry(path=registry_path, home=home)
        mtime = registry_mtime(path=registry_path, home=home)
    except RegistryReadError as exc:
        logger.info(f"offline_render registry unavailable: {exc!r}")
        registry = None
        mtime = None
    view = initial_view_for(registry) if registry is not None else WorkspaceViewState()
    layout = build_workspace_frame(
        registry,
        view,
        now=now,
        registry_mtime_at=mtime,
        repo_state_loader=repo_state_loader,
        is_stale_evaluator=is_stale_evaluator,
    )
    console.print(layout)
    return buf.getvalue()


__all__ = [
    "ACTIVE_CHIP_STYLE",
    "STALE_CHIP_STYLE",
    "WORKSPACE_FOOTER_KEYMAP",
    "WorkspaceViewState",
    "active_code",
    "apply_strip_key",
    "build_empty_strip_panel",
    "build_strip_panel",
    "build_strip_text",
    "build_workspace_breadcrumb_text",
    "build_workspace_footer_panel",
    "build_workspace_frame",
    "initial_view_for",
    "offline_render",
    "render_chip_for",
    "render_workspace",
    "sorted_entries",
    "workspace_header_for",
]
