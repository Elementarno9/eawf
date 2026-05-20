"""Modal overlays for the C06 Eä TUI (tui_v2).

Each overlay is a Textual :class:`~textual.screen.ModalScreen` pushed on
top of a scope screen, capped at a stack depth of 3 by the App. W19 shipped
:class:`DetailModal` (the row-drill-in card the W17 widgets emit selection
messages into) and :class:`ConfirmModal` (the yes/no destructive-op
confirmation). This wave (P26-W20) adds the plan-mode + needs_user + audit
overlays: :class:`PlanPreviewModal` (the ``/prep`` wave-DAG preview),
:class:`NeedsUserModal` (the ``status=needs_user`` AskUserQuestion surface),
:class:`AuditRunningModal` (live audit progress), and
:class:`AuditFailedModal` (the D17 mutating repair menu). The remaining
metrics / events / pr-list / config overlays the C06 brief §5.7 enumerates
land in later waves of this band and register here as they arrive.
"""

from __future__ import annotations

from eawf.tui_v2.screens.overlays.audit_failed import (
    AuditFailedModal,
    format_dispatch_line,
    open_audit_failed,
)
from eawf.tui_v2.screens.overlays.audit_running import (
    AuditProgress,
    AuditRunningModal,
    CheckRow,
    CheckState,
    open_audit_running,
)
from eawf.tui_v2.screens.overlays.confirm import ConfirmModal
from eawf.tui_v2.screens.overlays.detail import (
    DetailCard,
    DetailModal,
    resolve_detail,
)
from eawf.tui_v2.screens.overlays.needs_user import NeedsUserModal, open_needs_user
from eawf.tui_v2.screens.overlays.plan_preview import (
    PlanIterRow,
    PlanPreviewModal,
    PlanTree,
    PlanWaveRow,
    build_plan_tree,
    open_plan_preview,
)

__all__ = [
    "AuditFailedModal",
    "AuditProgress",
    "AuditRunningModal",
    "CheckRow",
    "CheckState",
    "ConfirmModal",
    "DetailCard",
    "DetailModal",
    "NeedsUserModal",
    "PlanIterRow",
    "PlanPreviewModal",
    "PlanTree",
    "PlanWaveRow",
    "build_plan_tree",
    "format_dispatch_line",
    "open_audit_failed",
    "open_audit_running",
    "open_needs_user",
    "open_plan_preview",
    "resolve_detail",
]
