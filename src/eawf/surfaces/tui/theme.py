"""Re-export shim: ``theme`` moved to the shared TUI chassis.

The implementation now lives at :mod:`eawf.surfaces.tui.chassis.theme`. This
module stays as a thin alias so the epoch-1-internal call sites this wave did
not retarget keep working without a repo-wide import sweep; new code should
import the chassis module directly rather than through this path. The
``theme.tcss`` stylesheet next to this file is unaffected -- it stays until
the epoch-1 app it styles is retired.

The alias installs the chassis module object itself under this path's entry
in :data:`sys.modules`, rather than copying its names, so a monkeypatch
against a name on this path lands on the exact object the moved
implementation resolves that name against.
"""

from __future__ import annotations

import sys

from eawf.surfaces.tui.chassis import theme as _theme

sys.modules[__name__] = _theme
