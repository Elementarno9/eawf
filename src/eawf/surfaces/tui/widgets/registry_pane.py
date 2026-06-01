"""``RegistryPane`` — read-only listing of the explicit repo registry.

A :class:`~textual.widgets.Static` that lists the entries of the global
``~/.eawf/registry.json`` registry — one line per registered repo, with
the repo code, its human-readable title, its on-disk path, and
``(active)`` / ``(stale)`` chips. It is the workspace dashboard's
companion to the per-repo
:class:`~eawf.surfaces.tui.widgets.workspace_table.WorkspaceTable`: the table
renders each repo's live progress, while this pane surfaces the registry
itself (which repos are registered, which is active, which have gone
stale).

Read-only and explicit-registry-only. The pane reads ONLY the registry
file via :func:`~eawf.platform.registry.read_registry`; it never scans,
walks, or imports-from-discovery the filesystem to find repos. Per the
project's explicit-registry-only rule the registry grows solely through
``eawf init`` / ``eawf repo add`` / ``eawf workspace add-repo`` — those
mutators are the only growth path, and nothing here writes. A missing or
corrupt registry, or a registry with zero repos, renders an
honest-empty placeholder rather than crashing or fabricating rows.

The registry resolution + formatting live in pure module functions
(:func:`load_registry_rows`, :func:`format_registry_lines`) so the
rendered text is unit-testable by feeding a
:class:`~eawf.platform.registry.Registry` value directly, without
mounting the widget.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from textual.widgets import Static

from eawf.platform.registry import (
    Registry,
    RegistryReadError,
    is_stale,
    read_registry,
    registry_mtime,
)
from eawf.surfaces.tui.widgets.markup import escape_markup

logger = logging.getLogger(__name__)

#: Line rendered when the registry resolves zero repos (a fresh install,
#: or a registry that exists but has not had ``eawf init`` /
#: ``eawf repo add`` run yet). The substring ``no repos registered`` is
#: the honest-empty contract the host + tests assert on. The follow-on
#: directive names the explicit-growth surfaces so the operator knows the
#: registry never auto-discovers repos.
REGISTRY_EMPTY_CELL: str = "no repos registered"

#: Line rendered when the registry file is missing or fails to load /
#: validate. Distinct from :data:`REGISTRY_EMPTY_CELL` so a broken
#: registry is never mistaken for an empty-but-valid one.
REGISTRY_UNAVAILABLE_CELL: str = "registry unavailable (read failed)"

#: Directive line appended under the honest-empty placeholder so the
#: operator sees the supported (explicit-only) way to register a repo.
REGISTRY_HINT_LINE: str = "add a repo: eawf init / eawf repo add <path>"


def format_registry_lines(
    registry: Registry | None,
    *,
    is_stale_at: dict[str, bool],
) -> list[str]:
    """Render *registry* into the pane's ordered text lines.

    Pure helper so the rendered listing is unit-testable without mounting
    the widget. One ``CODE  title  path  [chips]`` line per registered
    repo, ordered by code so the listing is deterministic. The active
    repo carries an ``(active)`` chip; a stale entry carries a
    ``(stale)`` chip.

    Honest-empty: a ``None`` registry (unavailable) yields the
    :data:`REGISTRY_UNAVAILABLE_CELL` line, and a registry with zero
    repos yields the :data:`REGISTRY_EMPTY_CELL` placeholder plus the
    explicit-growth hint — never a fabricated row.

    Args:
        registry: The loaded registry, or ``None`` when unavailable.
        is_stale_at: Per-code staleness flags keyed by repo code (the
            output of :func:`~eawf.platform.registry.is_stale` per entry).

    Returns:
        The ordered plain-text lines for the pane (always at least one).
    """
    if registry is None:
        return [REGISTRY_UNAVAILABLE_CELL]
    if not registry.repos:
        return [REGISTRY_EMPTY_CELL, REGISTRY_HINT_LINE]
    lines: list[str] = []
    for code in sorted(registry.repos):
        entry = registry.repos[code]
        chips: list[str] = []
        if code == registry.active_code:
            chips.append("(active)")
        if is_stale_at.get(code):
            chips.append("(stale)")
        suffix = f"  {' '.join(chips)}" if chips else ""
        title = entry.title or entry.code
        lines.append(f"{code}  {title}  {entry.path}{suffix}")
    return lines


def load_registry_rows(
    *,
    registry_path: Path | None = None,
    home: Path | None = None,
    now: datetime | None = None,
) -> list[str]:
    """Resolve the registry read-only and render its pane lines.

    Reads ONLY ``~/.eawf/registry.json`` via
    :func:`~eawf.platform.registry.read_registry`; never scans, walks, or
    imports-from-discovery the filesystem to find repos (the
    explicit-registry-only rule). Computes per-repo staleness with the
    same :func:`~eawf.platform.registry.is_stale` boundary the offline
    workspace dashboard uses, so the ``(stale)`` chip stays consistent
    across surfaces. A missing or corrupt registry degrades to the
    unavailable placeholder rather than raising.

    Args:
        registry_path: Explicit registry path. When ``None``, falls back
            to ``~/.eawf/registry.json`` (resolved via *home*).
        home: Test seam for the default-path branch. Pass a ``tmp_path``
            root so tests never touch the operator's real registry.
            Ignored when *registry_path* is supplied directly.
        now: Override for the current timestamp threaded to the staleness
            predicate so freshness comparisons stay deterministic in
            tests; defaults to the wall clock.

    Returns:
        The pane's rendered text lines (always at least one).
    """
    registry: Registry | None
    try:
        registry = read_registry(path=registry_path, home=home)
        mtime = registry_mtime(path=registry_path, home=home)
    except RegistryReadError as exc:
        logger.info(f"load_registry_rows registry unavailable cause={exc!r}")
        return [REGISTRY_UNAVAILABLE_CELL]
    is_stale_at: dict[str, bool] = {
        code: is_stale(entry, registry_mtime_at=mtime, now=now)
        for code, entry in registry.repos.items()
    }
    return format_registry_lines(registry, is_stale_at=is_stale_at)


class RegistryPane(Static):
    """Read-only listing of the explicit ``~/.eawf/registry.json`` registry.

    Renders one line per registered repo (code · title · path · chips),
    read solely from the registry file — never a filesystem scan. The
    pane refreshes on mount and exposes :meth:`refresh_registry` so a
    host screen can re-read the registry on a force-refresh keypress.

    Set the registry source via :paramref:`registry_path` /
    :paramref:`home` (the test seams); both default to the operator's
    real ``~/.eawf/registry.json`` so production reads need no wiring.
    """

    DEFAULT_CSS: ClassVar[str] = """
    RegistryPane {
        height: auto;
        width: 1fr;
    }
    """

    def __init__(
        self,
        *,
        registry_path: Path | None = None,
        home: Path | None = None,
        **kwargs: object,
    ) -> None:
        """Construct the pane.

        Args:
            registry_path: Explicit registry path; ``None`` falls back to
                ``~/.eawf/registry.json`` (resolved via *home*).
            home: Test seam for the default-path branch; ignored when
                *registry_path* is supplied directly.
            **kwargs: Forwarded to :class:`textual.widgets.Static`.
        """
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._registry_path = registry_path
        self._home = home
        #: The last-rendered registry lines, exposed via :meth:`lines` /
        #: :meth:`rendered_text` so a host / test reads the listing without
        #: scraping the compositor.
        self._lines: list[str] = []

    def on_mount(self) -> None:
        """Render the registry listing on first paint."""
        self.refresh_registry()

    def refresh_registry(self) -> None:
        """Re-read the registry (read-only) and repaint the listing.

        Resolves the registry via :func:`load_registry_rows` (which reads
        only the registry file, never a scan) and updates the rendered
        text. Each line is markup-escaped so an on-disk path containing a
        ``[`` is rendered literally rather than parsed as a Textual style
        tag.
        """
        self._lines = load_registry_rows(registry_path=self._registry_path, home=self._home)
        self.update("\n".join(escape_markup(line) for line in self._lines))

    def lines(self) -> list[str]:
        """Return the last-rendered registry lines (a copy).

        The pure listing the pane painted, read without scraping the
        compositor. Empty until :meth:`refresh_registry` (or the
        mount-time first paint) has run.

        Returns:
            A copy of the rendered registry lines.
        """
        return list(self._lines)

    def rendered_text(self) -> str:
        """Return the last-rendered listing as a single newline-joined string."""
        return "\n".join(self._lines)


__all__ = [
    "REGISTRY_EMPTY_CELL",
    "REGISTRY_HINT_LINE",
    "REGISTRY_UNAVAILABLE_CELL",
    "RegistryPane",
    "format_registry_lines",
    "load_registry_rows",
]
