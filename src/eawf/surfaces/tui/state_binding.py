"""Re-export shim: ``state_binding`` moved to the shared TUI chassis.

The implementation now lives at
:mod:`eawf.surfaces.tui.chassis.state_binding`, which both the epoch-1 app
and the epoch-2 console import directly. This module stays as a thin alias
so the epoch-1-internal call sites this wave did not retarget keep working
without a repo-wide import sweep; new code should import the chassis module
directly rather than through this path.

The alias installs the chassis module object itself under this path's entry
in :data:`sys.modules`, rather than copying its names, so a test that
monkeypatches a name on this path (``monkeypatch.setattr(state_binding,
"DaemonClient", ...)``) lands on the exact object
:class:`~eawf.surfaces.tui.chassis.state_binding.StateBinding` itself
resolves that name against, not a disconnected copy that the real
implementation never reads.
"""

from __future__ import annotations

import sys

from eawf.surfaces.tui.chassis import state_binding as _state_binding

sys.modules[__name__] = _state_binding
