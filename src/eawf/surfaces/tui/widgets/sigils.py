"""Re-export shim: ``sigils`` moved to the shared TUI chassis.

The implementation now lives at :mod:`eawf.surfaces.tui.chassis.sigils`,
which the sigil-totality lint (:mod:`eawf.platform.lint.sigil_totality`)
imports directly. This module stays as a thin alias so the epoch-1-internal
call sites this wave did not retarget keep working without a repo-wide
import sweep; new code should import the chassis module directly rather
than through this path.

The alias installs the chassis module object itself under this path's entry
in :data:`sys.modules`, rather than copying its names: several epoch-1
callers (``modes.autopilot`` among them) reach past the public
:data:`__all__` surface into private module state such as ``_LIFECYCLE``,
and a copy would either drop those names or fork them from the state the
real implementation reads and mutates.
"""

from __future__ import annotations

import sys

from eawf.surfaces.tui.chassis import sigils as _sigils

sys.modules[__name__] = _sigils
