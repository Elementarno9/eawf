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

The pane paints in the Eae cosmic-terminal language: each repo line leads
with a lifecycle sigil drawn from the shared I02 sigils source
(:func:`registry_line_sigil` + :mod:`eawf.surfaces.tui.widgets.sigils`) --
the RUNNING diamond for the active repo, the ABANDONED circled-slash for a
stale one, the CLOSED circle otherwise -- and the ``(active)`` / ``(stale)``
chips render in the green accent palette (:data:`CHIP_ACCENT`). The
honest-empty + unavailable lines stay plain so a fresh / broken registry
reads exactly as before. The plain (:func:`format_registry_lines`) and
content-markup (:func:`format_registry_markup_lines`) formatters are both
pure module functions so the reskin is unit-testable without mounting the
widget.
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
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE, RenderMode
from eawf.surfaces.tui.widgets.markup import escape_markup
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph, tint

logger = logging.getLogger(__name__)

#: The content-markup style the ``(active)`` / ``(stale)`` chips wear. The
#: theme's ``$accent`` palette var is the green-rotated accent of the Eae
#: cosmic-terminal reskin (see :mod:`eawf.surfaces.tui.theme`), so a chip
#: tinted with it reads in the same green as every other reskinned pane's
#: accent rather than inventing a local colour.
CHIP_ACCENT: str = "$accent"

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


def registry_line_sigil(*, is_active: bool, is_stale: bool) -> Sigil:
    """Map a registry entry's lifecycle onto its leading :class:`Sigil`.

    Each registry line leads with a lifecycle sigil drawn from the I02
    sigils source so the listing reads in the Eae cosmic-terminal language
    rather than as a flat code/path table. The three lifecycle states a
    registry entry can be in map onto the shared lifecycle alphabet:

    * the active repo wears the RUNNING diamond (the in-flight focus);
    * a stale entry wears the ABANDONED circled-slash (it has gone stale
      and recedes rather than alarms), shape-distinct from a not-yet-run
      ring;
    * every other registered-and-fresh entry wears the CLOSED filled
      circle (registered and healthy).

    The active flag wins over the stale flag when both are set, since the
    operator's current repo reads as in-flight even when its on-disk state
    has drifted stale.

    Args:
        is_active: Whether this entry is the registry's active repo.
        is_stale: Whether the staleness predicate flagged this entry.

    Returns:
        The lifecycle :class:`Sigil` the line leads with.
    """
    if is_active:
        return Sigil.RUNNING
    if is_stale:
        return Sigil.ABANDONED
    return Sigil.CLOSED


def _sigil_markup(sigil: Sigil, *, mode: RenderMode) -> str:
    """Return *sigil*'s glyph tinted by its lifecycle status, as markup.

    Composes the SHAPE (:func:`~eawf.surfaces.tui.widgets.sigils.glyph`) and
    the COLOUR (:func:`~eawf.surfaces.tui.widgets.sigils.tint`) from the
    shared sigils helper so a registry line leads with a tinted lifecycle
    mark; a sigil whose mapped status carries no tint falls back to the
    muted span so the mark still renders.

    Args:
        sigil: The lifecycle mark to render.
        mode: The App's resolved render-mode label -- selects the glyph's
            ASCII / unicode column.

    Returns:
        A content-markup span: the tinted (or muted) lifecycle glyph.
    """
    mark = escape_markup(glyph(sigil, mode=mode))
    hue = tint(sigil)
    if hue is None:
        return f"[$muted]{mark}[/]"
    return f"[{hue}]{mark}[/]"


def _chip_markup(text: str) -> str:
    """Return *text* wrapped in the green-accent chip span.

    Args:
        text: The literal chip text (``(active)`` / ``(stale)``).

    Returns:
        A content-markup span tinting *text* with :data:`CHIP_ACCENT`.
    """
    return f"[{CHIP_ACCENT}]{escape_markup(text)}[/]"


def format_registry_markup_lines(
    registry: Registry | None,
    *,
    is_stale_at: dict[str, bool],
    mode: RenderMode = DEFAULT_RENDER_MODE,
) -> list[str]:
    """Render *registry* into the pane's content-markup lines.

    The reskin twin of :func:`format_registry_lines`: each repo line leads
    with a tinted lifecycle sigil (via :func:`registry_line_sigil` +
    :func:`_sigil_markup`) and tints the ``(active)`` / ``(stale)`` chips in
    the green accent palette (:data:`CHIP_ACCENT`). The ``CODE  title  path``
    body is markup-escaped so a path containing a ``[`` renders literally.

    The honest-empty and unavailable lines are returned escaped-plain --
    byte-identical to :func:`format_registry_lines` once escaped -- so a
    fresh / broken registry reads exactly as before, without a sigil or a
    chip implying a state that is not there.

    Args:
        registry: The loaded registry, or ``None`` when unavailable.
        is_stale_at: Per-code staleness flags keyed by repo code.
        mode: The App's resolved render-mode label -- selects each sigil's
            ASCII / unicode column.

    Returns:
        The ordered content-markup lines for the pane (always at least one).
    """
    if registry is None:
        return [escape_markup(REGISTRY_UNAVAILABLE_CELL)]
    if not registry.repos:
        return [escape_markup(REGISTRY_EMPTY_CELL), escape_markup(REGISTRY_HINT_LINE)]
    lines: list[str] = []
    for code in sorted(registry.repos):
        entry = registry.repos[code]
        is_active = code == registry.active_code
        stale = bool(is_stale_at.get(code))
        chips: list[str] = []
        if is_active:
            chips.append(_chip_markup("(active)"))
        if stale:
            chips.append(_chip_markup("(stale)"))
        suffix = f"  {' '.join(chips)}" if chips else ""
        title = entry.title or entry.code
        sigil = _sigil_markup(registry_line_sigil(is_active=is_active, is_stale=stale), mode=mode)
        body = escape_markup(f"{code}  {title}  {entry.path}")
        lines.append(f"{sigil} {body}{suffix}")
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


def load_registry_markup_rows(
    *,
    registry_path: Path | None = None,
    home: Path | None = None,
    now: datetime | None = None,
    mode: RenderMode = DEFAULT_RENDER_MODE,
) -> list[str]:
    """Resolve the registry read-only and render its content-markup lines.

    The reskin twin of :func:`load_registry_rows`: it reads the same
    explicit registry the same way (never a scan / walk) and computes the
    same staleness, but renders each line through
    :func:`format_registry_markup_lines` so the listing leads with a tinted
    lifecycle sigil and tints the chips green. A missing or corrupt registry
    degrades to the escaped unavailable placeholder rather than raising.

    Args:
        registry_path: Explicit registry path. When ``None``, falls back to
            ``~/.eawf/registry.json`` (resolved via *home*).
        home: Test seam for the default-path branch. Pass a ``tmp_path``
            root so tests never touch the operator's real registry. Ignored
            when *registry_path* is supplied directly.
        now: Override for the current timestamp threaded to the staleness
            predicate so freshness comparisons stay deterministic in tests;
            defaults to the wall clock.
        mode: The App's resolved render-mode label -- selects each sigil's
            ASCII / unicode column.

    Returns:
        The pane's content-markup lines (always at least one).
    """
    registry: Registry | None
    try:
        registry = read_registry(path=registry_path, home=home)
        mtime = registry_mtime(path=registry_path, home=home)
    except RegistryReadError as exc:
        logger.info(f"load_registry_markup_rows registry unavailable cause={exc!r}")
        return [escape_markup(REGISTRY_UNAVAILABLE_CELL)]
    is_stale_at: dict[str, bool] = {
        code: is_stale(entry, registry_mtime_at=mtime, now=now)
        for code, entry in registry.repos.items()
    }
    return format_registry_markup_lines(registry, is_stale_at=is_stale_at, mode=mode)


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

    def _render_mode(self) -> RenderMode:
        """Resolve the active render-mode label from the host app.

        Threads :attr:`eawf.surfaces.tui.app.EaApp.render_mode` into the
        sigil helpers so an ``ascii`` flip swaps every lifecycle glyph to
        its ASCII column; falls back to :data:`DEFAULT_RENDER_MODE` under a
        bare test harness whose host App carries no ``render_mode``.

        Returns:
            The render-mode label (``"ascii"`` or a unicode label).
        """
        return getattr(self.app, "render_mode", DEFAULT_RENDER_MODE)

    def refresh_registry(self) -> None:
        """Re-read the registry (read-only) and repaint the listing.

        Resolves the registry twice off the same read: the plain lines (via
        :func:`load_registry_rows`) back :meth:`lines` / :meth:`rendered_text`
        so a host / test reads the listing without the markup spans, and the
        content-markup lines (via :func:`load_registry_markup_rows`) paint
        the pane -- each repo line leading with a tinted lifecycle sigil and
        green-tinted ``(active)`` / ``(stale)`` chips, with the path body
        markup-escaped so a ``[`` in a path renders literally. The
        honest-empty / unavailable lines stay byte-identical to the plain
        listing.
        """
        self._lines = load_registry_rows(registry_path=self._registry_path, home=self._home)
        markup = load_registry_markup_rows(
            registry_path=self._registry_path, home=self._home, mode=self._render_mode()
        )
        self.update("\n".join(markup))

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
    "CHIP_ACCENT",
    "REGISTRY_EMPTY_CELL",
    "REGISTRY_HINT_LINE",
    "REGISTRY_UNAVAILABLE_CELL",
    "RegistryPane",
    "format_registry_lines",
    "format_registry_markup_lines",
    "load_registry_markup_rows",
    "load_registry_rows",
    "registry_line_sigil",
]
