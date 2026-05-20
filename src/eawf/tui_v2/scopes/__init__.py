"""C06 per-scope screens for the Eä Textual TUI (tui_v2).

The three scope screens (``RepoScreen`` / ``WorkspaceScreen`` /
``UserScreen``) compose the W17 widget catalog
(:mod:`eawf.tui_v2.widgets`) inside a **shared chassis** — one
:class:`~eawf.tui_v2.widgets.header.Header` + one
:class:`~eawf.tui_v2.widgets.footer.Footer` (which owns the
:class:`~eawf.tui_v2.widgets.footer.Heartbeat`) reused verbatim by every
screen with **no per-scope duplication** (Decision D3 / G3
[2:99-101]).

The shared chassis lives on :class:`ScopeScreen`: it yields the Header,
then a per-scope body produced by the subclass's :meth:`ScopeScreen.compose_body`
hook, then the Footer. Each concrete screen overrides **only** the body
hook (and its scope-specific footer hints) — the brand, breadcrumb,
runtime cell, clock, heartbeat, and quit/help/palette key bindings are
declared once on the base. That is the literal D3 trim: the salvageable
chassis LOC drops from the P20 ``~5300`` duplicate-per-scope baseline to
``~2500`` shared [2:122-126].

Per-scope body layouts follow C06 §5.5:

* ``RepoScreen`` — 2x2 quadrant (roadmap · status / git · backlog).
* ``WorkspaceScreen`` — top strip + active-repo quadrant (the
  ``WorkspaceTopStrip`` / ``RepoQuadrant`` sub-widgets land in a later
  wave; this screen composes the available widget catalog in the
  strip+zoom arrangement).
* ``UserScreen`` — three weighted sections (attention · effort ·
  portfolio); the ``AttentionList`` / ``EffortBars`` / ``PortfolioTable``
  sub-widgets land in a later wave.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.screen import Screen

from eawf.tui_v2.widgets.footer import DEFAULT_HINTS, Footer
from eawf.tui_v2.widgets.header import Header


class ScopeScreen(Screen[None]):
    """Shared-chassis base for every per-scope screen (D3).

    Owns the Header + Footer (+ Heartbeat) chrome and the chrome key
    bindings; subclasses override **only** :meth:`compose_body` (and
    optionally :attr:`FOOTER_HINTS`). This is the single source of the
    chassis composition — no scope re-declares the header or footer.
    """

    #: Chrome key bindings shared by every scope screen (full key names
    #: per D11; arrows are primary, vim aliases live app-wide on
    #: :class:`~eawf.tui_v2.app.EaApp`). Scope-specific bindings (e.g.
    #: workspace ``z`` zoom) are appended by the subclass.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("slash", "open_palette", "palette", show=False),
        Binding("question_mark", "open_help", "help", show=False),
        Binding("r", "force_refresh", "refresh", show=False),
        Binding("q", "quit", "quit"),
        Binding("escape", "quit", "quit", show=False),
    ]

    #: Footer key hints for this scope (full key names). Overridable per
    #: subclass; defaults to the shared base hints.
    FOOTER_HINTS: ClassVar[tuple[str, ...]] = DEFAULT_HINTS

    def compose(self) -> ComposeResult:
        """Yield the shared chassis around the per-scope body.

        Header (top) → subclass body → Footer (bottom). The body is the
        only part a concrete screen customises; everything else is the
        D3 shared chassis.
        """
        yield Header()
        yield from self.compose_body()
        yield Footer()

    def compose_body(self) -> ComposeResult:
        """Yield the per-scope body widgets.

        Overridden by every concrete scope screen; the base raises so a
        screen that forgets to compose a body fails fast rather than
        rendering empty chrome.

        Raises:
            NotImplementedError: Always — concrete screens must override.
        """
        raise NotImplementedError("scope screens must override compose_body")
        yield  # pragma: no cover — unreachable; keeps the generator typed

    def on_mount(self) -> None:
        """Apply the scope-specific footer hints once the chassis mounts."""
        if self.FOOTER_HINTS != DEFAULT_HINTS:
            self.query_one(Footer).set_hints(self.FOOTER_HINTS)


from eawf.tui_v2.scopes.repo import RepoScreen  # noqa: E402  (after base def)
from eawf.tui_v2.scopes.user import UserScreen  # noqa: E402
from eawf.tui_v2.scopes.workspace import WorkspaceScreen  # noqa: E402

__all__ = [
    "RepoScreen",
    "ScopeScreen",
    "UserScreen",
    "WorkspaceScreen",
]
