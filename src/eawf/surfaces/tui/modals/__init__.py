"""Evidence-surface modals for the Eä Textual TUI.

Modal overlays that drill into the Evidence mode's close-readiness ledger.
The first is the why-peek :class:`~eawf.surfaces.tui.modals.evidence_drill.EvidenceDrillModal`,
which renders one criterion's evidence chain -- each gate outcome plus the
joined evidence rows -- so the operator can see WHY a criterion landed at
its status without leaving the pane.

The render helpers live as pure module functions so the chain content is
unit-testable without mounting Textual, mirroring the widget-catalog
convention the scope screens and mode panes follow.
"""

from __future__ import annotations
