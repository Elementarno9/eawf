"""Reusable widget catalog for the Eä Textual TUI (tui_v2).

These standalone widgets are the building blocks the per-scope screens
compose:

    Header        — shared chassis brand + breadcrumb + runtime + clock
    Footer        — shared chassis key hints + heartbeat (owns Heartbeat)
    Heartbeat     — pulsing liveness dot (accent / err on degrade)
    RoadmapTree   — phase → iter → wave tree with V12 status glyphs
    EUBar         — 5-cell colour-banded effort-unit progress bar
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

from eawf.tui_v2.widgets.backlog_table import BacklogTable
from eawf.tui_v2.widgets.eu_bar import EUBar
from eawf.tui_v2.widgets.footer import Footer, Heartbeat
from eawf.tui_v2.widgets.git_pane import GitPane
from eawf.tui_v2.widgets.header import Header
from eawf.tui_v2.widgets.roadmap_tree import RoadmapTree
from eawf.tui_v2.widgets.status_pane import StatusPane

__all__ = [
    "BacklogTable",
    "EUBar",
    "Footer",
    "GitPane",
    "Header",
    "Heartbeat",
    "RoadmapTree",
    "StatusPane",
]
