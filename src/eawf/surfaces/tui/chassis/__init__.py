"""Shared TUI chassis: modules the epoch-1 app and the epoch-2 console both need.

The epoch-1 :class:`~eawf.surfaces.tui.app.EaApp` and the epoch-2
:class:`~eawf.surfaces.tui.console.app.ConsoleApp` are two different Textual
apps over two different render models, but they share a handful of
concerns that belong to neither: the daemon transport
(:mod:`~eawf.surfaces.tui.chassis.state_binding`), palette colours
(:mod:`~eawf.surfaces.tui.chassis.theme`), the truth-token glyph resolver
(:mod:`~eawf.surfaces.tui.chassis.sigils`), the unified completion bar
(:mod:`~eawf.surfaces.tui.chassis.progress`), the headless status emitter
(:mod:`~eawf.surfaces.tui.chassis.offline`) and the Pilot capture harness
(:mod:`~eawf.surfaces.tui.chassis.pilot_harness`). Before this package
existed the console imported these straight out of the epoch-1 tree, so
retiring that tree (the P35 flag day) would have taken the console's own
transport and glyphs down with it. This package is neutral ground: neither
app owns it, and the epoch-1 tree's eventual deletion cannot reach it.

Modules that still import these six from their old ``eawf.surfaces.tui.*``
locations keep working through the thin re-export shims left at those
paths; new code should import the chassis module directly.
"""

from __future__ import annotations
