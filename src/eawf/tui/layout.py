"""Layout helpers for the Eä Rich TUI (P20-I01-W02).

This module owns the structural primitives used by :mod:`eawf.tui.app`
and any future TUI surface. It deliberately stays state-shape-agnostic
so future panes can reuse the chassis without coupling to a particular
``state.json`` schema:

* :func:`build_brand_text` — bold-accent ``Eä`` outside-left of the
  scope breadcrumb per ``feedback_tui_branding``.
* :func:`build_breadcrumb` — ``project / phase / iter`` from a state
  dict.
* :func:`build_header_panel`, :func:`build_footer_panel` —
  Panel-wrapped strips for the top and bottom of the frame.
* :func:`build_quadrant` — composes four named Layouts into a 2x2
  grid via :meth:`rich.layout.Layout.split_row` /
  :meth:`~rich.layout.Layout.split_column`.
* :func:`build_frame` — assembles header + body + footer rows, with
  the body slot pre-populated by a quadrant.

The repo-scope quadrant exposed to the app layer is roadmap (top
left), status (top right), git (bottom left), backlog (bottom right);
:func:`repo_quadrant_panes` is the canonical wiring callers use.

Keymap conventions follow ``feedback_tui_keymap_conventions``: arrow
keys + PageUp/PageDown/Home/End/Enter/Esc are the primary surface,
vim aliases (``h/j/k/l/g/G``) appear in parentheses as secondary
shorthand.
"""

from __future__ import annotations

import logging
from typing import Any

from rich.layout import Layout
from rich.panel import Panel
from rich.text import Text

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Brand / breadcrumb / keymap constants
# ---------------------------------------------------------------------------

#: Literal ``Eä`` (U+0045 + U+00E4) brand string. Outside-left of the
#: scope breadcrumb in the header strip per the branding memory note.
BRAND: str = "Eä"

#: Bold-accent style applied to the brand text. The remainder of the
#: header uses ``cyan`` so the brand stays visually outside-left.
BRAND_STYLE: str = "bold white"

#: Style applied to the breadcrumb (project / phase / iter chain).
BREADCRUMB_STYLE: str = "cyan"

#: Default project code used when ``state.json`` is missing or unreadable.
DEFAULT_PROJECT_CODE: str = "EAWF"

#: Footer keymap hint. Arrows lead; vim aliases trail. ``b`` opens
#: the wave-board view (P20-I01-W03); Esc returns from the board.
FOOTER_KEYMAP: str = (
    "↑↓←→ navigate  PageUp/PageDown page  Home/End jump  Enter  b board  Esc/q  (vim: h j k l g G)"
)

#: Pane order in the 2x2 quadrant — top-left, top-right, bottom-left,
#: bottom-right. Callers building panes for the quadrant MUST emit four
#: panels in this order; :func:`build_quadrant` rejects any other count.
QUADRANT_PANE_NAMES: tuple[str, str, str, str] = (
    "roadmap",
    "status",
    "git",
    "backlog",
)


# ---------------------------------------------------------------------------
# Brand + breadcrumb helpers
# ---------------------------------------------------------------------------


def build_brand_text(breadcrumb: str) -> Text:
    """Return the bold-accent ``Eä`` brand followed by the breadcrumb.

    The brand sits outside-left of the breadcrumb with two spaces of
    padding so the visual hierarchy stays consistent across terminal
    widths.

    Args:
        breadcrumb: ``project / phase / iter`` string built by
            :func:`build_breadcrumb`.

    Returns:
        Rich :class:`~rich.text.Text` with two styled segments.
    """
    text = Text()
    text.append(f"{BRAND}  ", style=BRAND_STYLE)
    text.append(breadcrumb, style=BREADCRUMB_STYLE)
    return text


def build_breadcrumb(state: dict[str, Any]) -> str:
    """Build the ``project / phase / iter`` breadcrumb from state.

    Falls back to :data:`DEFAULT_PROJECT_CODE` when the state dict is
    empty or missing the ``project.code`` field so the header stays
    informative for a fresh workspace.
    """
    project = (state.get("project") or {}).get("code") or DEFAULT_PROJECT_CODE
    current = state.get("current") or {}
    phase = current.get("phase_id")
    iter_id = current.get("iter_id")
    parts: list[str] = [project]
    if phase:
        parts.append(phase)
    if iter_id:
        parts.append(iter_id)
    return " / ".join(parts)


def build_header_panel(state: dict[str, Any]) -> Panel:
    """Build the top-strip header Panel with brand + breadcrumb."""
    breadcrumb = build_breadcrumb(state)
    return Panel(build_brand_text(breadcrumb), title=None, border_style="dim")


def build_footer_panel() -> Panel:
    """Build the bottom-strip footer Panel with the keymap hint."""
    return Panel(Text(FOOTER_KEYMAP), title=None, border_style="dim")


# ---------------------------------------------------------------------------
# Pane summary helpers (state-shape agnostic counters)
# ---------------------------------------------------------------------------


def summary_counts(state: dict[str, Any]) -> dict[str, int]:
    """Count phases, iters, waves, audits visible in state.

    All keys are zero when the state dict is empty so panes can render
    a deterministic frame even before any roadmap activity.
    """
    return {
        "phases_open": sum(
            1 for p in (state.get("phases") or {}).values() if p.get("status") == "active"
        ),
        "iters_open": sum(
            1 for it in (state.get("iters") or {}).values() if it.get("status") == "active"
        ),
        "waves_pending": sum(
            1 for w in (state.get("waves") or {}).values() if w.get("status") == "pending"
        ),
        "waves_in_progress": sum(
            1 for w in (state.get("waves") or {}).values() if w.get("status") == "in_progress"
        ),
        "audits": len(state.get("audits") or {}),
    }


def backlog_counts(state: dict[str, Any]) -> dict[str, int]:
    """Tally backlog items by status for the backlog pane.

    Treats missing keys as zero. Returns ``open`` / ``closed`` /
    ``total`` so the pane can show ``open/total`` ratios without the
    caller re-iterating the state.
    """
    items = (state.get("backlog") or {}).values()
    open_count = 0
    closed_count = 0
    total = 0
    for item in items:
        total += 1
        if item.get("status") == "open":
            open_count += 1
        elif item.get("status") == "closed":
            closed_count += 1
    return {"open": open_count, "closed": closed_count, "total": total}


# ---------------------------------------------------------------------------
# Pane builders (the four quadrant panels)
# ---------------------------------------------------------------------------


def build_roadmap_pane(state: dict[str, Any]) -> Panel:
    """Top-left pane: phases / iters / waves overview."""
    counts = summary_counts(state)
    body = Text(
        "\n".join(
            [
                f"phases (active):  {counts['phases_open']}",
                f"iters  (active):  {counts['iters_open']}",
                f"waves  (pending): {counts['waves_pending']}",
                f"waves  (in-prog): {counts['waves_in_progress']}",
            ]
        )
    )
    return Panel(body, title="roadmap", border_style="cyan")


def build_status_pane(state: dict[str, Any]) -> Panel:
    """Top-right pane: current scope + audit count."""
    current = state.get("current") or {}
    counts = summary_counts(state)
    project = (state.get("project") or {}).get("code") or DEFAULT_PROJECT_CODE
    lines = [
        f"project: {project}",
        f"phase:   {current.get('phase_id') or '-'}",
        f"iter:    {current.get('iter_id') or '-'}",
        f"audits:  {counts['audits']}",
    ]
    return Panel(Text("\n".join(lines)), title="status", border_style="cyan")


def build_git_pane(state: dict[str, Any]) -> Panel:
    """Bottom-left pane: git context from the state-resident snapshot.

    The TUI does not shell out to git from inside the layout helpers
    (panes are pure functions of the passed state). The wave-08 thread
    owns live git polling; this pane reads whatever ``state['git']``
    snapshot is present and falls back to a placeholder otherwise.
    """
    git = state.get("git") or {}
    branch = git.get("branch") or "-"
    dirty = git.get("dirty")
    head = git.get("head") or "-"
    dirty_str = ("dirty" if dirty else "clean") if isinstance(dirty, bool) else "-"
    lines = [
        f"branch: {branch}",
        f"head:   {head}",
        f"status: {dirty_str}",
    ]
    return Panel(Text("\n".join(lines)), title="git", border_style="cyan")


def build_backlog_pane(state: dict[str, Any]) -> Panel:
    """Bottom-right pane: backlog open/closed/total counters."""
    counts = backlog_counts(state)
    lines = [
        f"open:   {counts['open']}",
        f"closed: {counts['closed']}",
        f"total:  {counts['total']}",
    ]
    return Panel(Text("\n".join(lines)), title="backlog", border_style="cyan")


def repo_quadrant_panes(state: dict[str, Any]) -> tuple[Panel, Panel, Panel, Panel]:
    """Build the four panes for the repo-scope quadrant in canonical order.

    Order matches :data:`QUADRANT_PANE_NAMES`: roadmap (top-left),
    status (top-right), git (bottom-left), backlog (bottom-right).
    """
    return (
        build_roadmap_pane(state),
        build_status_pane(state),
        build_git_pane(state),
        build_backlog_pane(state),
    )


# ---------------------------------------------------------------------------
# Quadrant + frame composition
# ---------------------------------------------------------------------------


def build_quadrant(panes: tuple[Panel, Panel, Panel, Panel]) -> Layout:
    """Compose four Panels into a 2x2 grid via row/column splits.

    Args:
        panes: Four-tuple of panels in canonical
            (top-left, top-right, bottom-left, bottom-right) order.

    Returns:
        A :class:`~rich.layout.Layout` named ``quadrant`` containing
        two named sub-rows ``top`` and ``bottom``, each split into two
        columns whose names come from :data:`QUADRANT_PANE_NAMES`.

    Raises:
        ValueError: panes is not exactly four panels.
    """
    if len(panes) != 4:
        raise ValueError(f"quadrant requires exactly 4 panes, got {len(panes)!r}")
    top_left, top_right, bottom_left, bottom_right = panes
    quadrant = Layout(name="quadrant")
    top = Layout(name="top")
    bottom = Layout(name="bottom")
    top.split_row(
        Layout(top_left, name=QUADRANT_PANE_NAMES[0], ratio=1),
        Layout(top_right, name=QUADRANT_PANE_NAMES[1], ratio=1),
    )
    bottom.split_row(
        Layout(bottom_left, name=QUADRANT_PANE_NAMES[2], ratio=1),
        Layout(bottom_right, name=QUADRANT_PANE_NAMES[3], ratio=1),
    )
    quadrant.split_column(top, bottom)
    return quadrant


def build_frame(state: dict[str, Any]) -> Layout:
    """Assemble header + body (quadrant) + footer into one Layout.

    The header row carries the ``Eä`` brand + scope breadcrumb; the
    body row holds the 2x2 quadrant produced by
    :func:`repo_quadrant_panes` and :func:`build_quadrant`; the
    footer row carries :data:`FOOTER_KEYMAP`.
    """
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body", ratio=1),
        Layout(name="footer", size=3),
    )
    layout["header"].update(build_header_panel(state))
    layout["body"].update(build_quadrant(repo_quadrant_panes(state)))
    layout["footer"].update(build_footer_panel())
    return layout


__all__ = [
    "BRAND",
    "BRAND_STYLE",
    "BREADCRUMB_STYLE",
    "DEFAULT_PROJECT_CODE",
    "FOOTER_KEYMAP",
    "QUADRANT_PANE_NAMES",
    "backlog_counts",
    "build_backlog_pane",
    "build_brand_text",
    "build_breadcrumb",
    "build_footer_panel",
    "build_frame",
    "build_git_pane",
    "build_header_panel",
    "build_quadrant",
    "build_roadmap_pane",
    "build_status_pane",
    "repo_quadrant_panes",
    "summary_counts",
]
