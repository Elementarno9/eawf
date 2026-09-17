"""The operator console: the product port of the replayed console chassis.

The package holds the ratified glyph tokens
(:mod:`~eawf.surfaces.tui.console.tokens`), the width oracle every pad and clip measures
through (:mod:`~eawf.surfaces.tui.console.width`), the route registry as a data table
(:mod:`~eawf.surfaces.tui.console.registry`), the one session model with its single reset
(:mod:`~eawf.surfaces.tui.console.session`), the renderers and overlays each route draws
through, and the app that composes them into one frame
(:mod:`~eawf.surfaces.tui.console.app`).

It also owns the golden harness (:mod:`~eawf.surfaces.tui.console.harness`) and the
pack-to-port normalisation map (:mod:`~eawf.surfaces.tui.console.normalisation`) that
replay the tracked contract against this console, and the plain-mode renderer
(:mod:`~eawf.surfaces.tui.console.plain`) that writes the same frame in the ASCII
allocation. The test-only chassis that carried the contract before the port is gone.
"""

from __future__ import annotations
