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

This package holds no eager re-exports: every caller in the tree imports
a widget from its own submodule (``eawf.surfaces.tui.widgets.header``
and siblings), and several of those submodules (:mod:`~.backlog_table`
among them) import :mod:`eawf.surfaces.tui.chassis.sigils`, which itself
imports :mod:`~.status_tint`. An eager ``from .backlog_table import
BacklogTable`` here would force this package's ``__init__`` to finish
before that chain can, which is exactly backwards when something reaches
``chassis.sigils`` first: importing ``status_tint`` from inside it would
need this package's own init already complete. Keeping this file import-free
breaks that cycle without touching either side of it.
"""

from __future__ import annotations
