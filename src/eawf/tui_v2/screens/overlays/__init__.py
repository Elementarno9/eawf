"""Modal overlays for the C06 Eä TUI (tui_v2).

Each overlay is a Textual :class:`~textual.screen.ModalScreen` pushed on
top of a scope screen, capped at a stack depth of 3 by the App. This wave
(P26-W19) ships :class:`DetailModal` (the row-drill-in card the W17
widgets emit selection messages into) and :class:`ConfirmModal` (the
yes/no destructive-op confirmation). The audit / plan-preview /
needs-user / metrics / events / pr-list / config overlays the C06 brief
§5.7 enumerates land in later waves of this band and register here as they
arrive.
"""

from __future__ import annotations

from eawf.tui_v2.screens.overlays.confirm import ConfirmModal
from eawf.tui_v2.screens.overlays.detail import (
    DetailCard,
    DetailModal,
    resolve_detail,
)

__all__ = [
    "ConfirmModal",
    "DetailCard",
    "DetailModal",
    "resolve_detail",
]
