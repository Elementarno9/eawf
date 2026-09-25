"""Re-export shim: ``pilot_harness`` moved to the shared TUI chassis.

The implementation now lives at
:mod:`eawf.surfaces.tui.chassis.pilot_harness`. This module stays as a thin
alias so the epoch-1-internal call sites this wave did not retarget --
including ``eawf.surfaces.tui.snapshot.asciinema`` and
``eawf.surfaces.tui.snapshot.behaviour_probe`` in this same package -- keep
working without a repo-wide import sweep; new code should import the
chassis module directly rather than through this path.

The alias installs the chassis module object itself under this path's entry
in :data:`sys.modules`, rather than copying its names, so a monkeypatch
against a name on this path lands on the exact object the moved
implementation resolves that name against.
"""

from __future__ import annotations

import sys

from eawf.surfaces.tui.chassis import pilot_harness as _pilot_harness

sys.modules[__name__] = _pilot_harness
