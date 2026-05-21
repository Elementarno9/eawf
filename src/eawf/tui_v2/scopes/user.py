"""``UserScreen`` — user-scope portfolio screen.

The user screen composes three vertical sections — **attention**,
**effort**, **portfolio** — weighted ``3:2:5``, inside the
:class:`~eawf.tui_v2.scopes.ScopeScreen` shared chassis (Header + Footer
+ Heartbeat reused verbatim).

The dedicated section sub-widgets the brief names (``AttentionList`` —
cross-repo attention rows; ``EffortBars`` — 7-day EU bars per repo;
``PortfolioTable`` — the registry portfolio grid) land in a later wave of
this band. This wave composes the **available** W17 widget catalog in the
three-section arrangement so the screen renders live today:

* attention (weight 3) — :class:`~eawf.tui_v2.widgets.status_pane.StatusPane`
  surfacing the blocked / running counters that demand attention;
* effort (weight 2) — :class:`~eawf.tui_v2.widgets.eu_bar.EUBar` as the
  compact effort gauge the per-repo ``EffortBars`` expand on;
* portfolio (weight 5) — :class:`~eawf.tui_v2.widgets.backlog_table.BacklogTable`
  as the portfolio grid the registry ``PortfolioTable`` replaces.

This screen overrides **only** :meth:`compose_body` + its footer hints;
the entire chassis is inherited from
:class:`~eawf.tui_v2.scopes.ScopeScreen` (zero-duplication).
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from eawf.tui_v2.scopes import ScopeScreen
from eawf.tui_v2.widgets.backlog_table import BacklogTable
from eawf.tui_v2.widgets.eu_bar import EUBar
from eawf.tui_v2.widgets.status_pane import StatusPane

#: Footer hints tuned for the user portfolio screen.
_USER_HINTS: tuple[str, ...] = (
    "↑↓ move",
    "Enter open",
    "w/r/u scope",
    "F5 refresh",
    "/ palette",
    "? help",
    "q quit",
)


class UserScreen(ScopeScreen):
    """User-scope screen: attention / effort / portfolio sections.

    Composes :class:`StatusPane` (attention) · :class:`EUBar` (effort) ·
    :class:`BacklogTable` (portfolio) weighted ``3:2:5`` inside the
    shared chassis.
    """

    FOOTER_HINTS: ClassVar[tuple[str, ...]] = _USER_HINTS

    def compose_body(self) -> ComposeResult:
        """Yield the three weighted sections (attention/effort/portfolio)."""
        with Vertical(id="body"):
            with Vertical(classes="section", id="attention"):
                yield Static("ATTENTION", classes="section-title")
                yield StatusPane(id="attention-status")
            with Vertical(classes="section", id="effort"):
                yield Static("EFFORT 7d (EU)", classes="section-title")
                yield EUBar(id="effort-bar")
            with Vertical(classes="section", id="portfolio"):
                yield Static("PORTFOLIO", classes="section-title")
                yield BacklogTable(id="portfolio-table")


__all__ = ["UserScreen"]
