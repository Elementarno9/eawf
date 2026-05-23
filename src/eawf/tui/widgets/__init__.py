"""Reusable widget catalog for the Eä Textual TUI (tui).

These standalone widgets are the building blocks the per-scope screens
compose:

    Header        — shared chassis brand + breadcrumb + runtime + clock
    Footer        — shared chassis key hints + heartbeat (owns Heartbeat)
    Heartbeat     — pulsing liveness dot (accent / err on degrade)
    RoadmapTree   — phase → iter → wave tree with V12 status glyphs
    EUBar         — 5-cell colour-banded effort-unit progress bar
    VarianceTile  — colour-banded M26 estimate-actual variance gauge
    StatusPane    — current-scope lifecycle status summary
    GitPane       — live git branch / status / ahead-behind context
    BacklogTable  — sortable / filterable backlog grid

The Header / Footer / Heartbeat trio is the shared chassis: every
per-scope screen reuses the same three widgets with no per-scope
duplication. Each widget is driven by the App's reactive ``state``
(read-only) and is unit-testable standalone via the Textual Pilot
harness.
"""

from __future__ import annotations

from eawf.tui.widgets.backlog_table import BacklogTable
from eawf.tui.widgets.eu_bar import EUBar
from eawf.tui.widgets.footer import Footer, Heartbeat
from eawf.tui.widgets.git_pane import GitPane
from eawf.tui.widgets.header import Header
from eawf.tui.widgets.roadmap_tree import RoadmapTree
from eawf.tui.widgets.status_pane import StatusPane
from eawf.tui.widgets.variance_tile import VarianceTile

__all__ = [
    "BacklogTable",
    "EUBar",
    "Footer",
    "GitPane",
    "Header",
    "Heartbeat",
    "RoadmapTree",
    "StatusPane",
    "VarianceTile",
]
