"""Shared TUI chassis: the modules the console draws on that are not console surfaces.

The daemon transport (:mod:`~eawf.surfaces.tui.chassis.state_binding`), palette
colours (:mod:`~eawf.surfaces.tui.chassis.theme`), the truth-token glyph resolver
(:mod:`~eawf.surfaces.tui.chassis.sigils`) and its status colours
(:mod:`~eawf.surfaces.tui.chassis.status_tint`), the headless workspace dashboard
(:mod:`~eawf.surfaces.tui.chassis.offline`), the plain-text screen capture
(:mod:`~eawf.surfaces.tui.chassis.pilot_harness`), the deterministic cast recorder
(:mod:`~eawf.surfaces.tui.chassis.asciinema`) and the colour-vision-deficiency
simulation that keeps no fact carried by colour alone
(:mod:`~eawf.surfaces.tui.chassis.cvd`).
"""

from __future__ import annotations
