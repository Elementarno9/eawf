"""The operator console: the product port of the replayed console chassis.

The package grows one chassis step at a time. It holds the ratified glyph tokens
(:mod:`~eawf.surfaces.tui.console.tokens`), the width oracle every pad and clip measures
through (:mod:`~eawf.surfaces.tui.console.width`), the route registry as a data table
(:mod:`~eawf.surfaces.tui.console.registry`) and the one session model with its single
reset (:mod:`~eawf.surfaces.tui.console.session`). The test-only chassis under
``tests/snapshots/tui/console`` keeps replaying the golden contract until the product
console replays it itself, so both copies exist side by side until then.
"""

from __future__ import annotations
