"""Modal overlays for the Eä TUI (tui_v2).

Each overlay is a Textual :class:`~textual.screen.ModalScreen` pushed on
top of a scope screen, capped at a stack depth of 3 by the App. The
catalog: :class:`DetailModal` (the row-drill-in card the widgets emit
selection messages into), :class:`ConfirmModal` (the yes/no
destructive-op confirmation), :class:`PlanPreviewModal` (the ``/prep``
wave-DAG preview), :class:`NeedsUserModal` (the ``status=needs_user``
AskUserQuestion surface), :class:`AuditRunningModal` (live audit
progress), :class:`AuditFailedModal` (the mutating repair menu),
:class:`MetricsModal` (the ``/metrics`` 3x2 dashboard),
:class:`PrListModal` (the ``/pr`` open-PRs list), and
:class:`EventsModal` (the ``/events`` last-50 ring buffer). The remaining
config overlays land in later waves and register here as they arrive.
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
from eawf.tui_v2.screens.overlays.config_modal import (
    ConfigModal,
    ConfigModalState,
    open_config,
)
from eawf.tui_v2.screens.overlays.confirm import ConfirmModal
from eawf.tui_v2.screens.overlays.detail import (
    DetailCard,
    DetailModal,
    resolve_detail,
)
from eawf.tui_v2.screens.overlays.edit_field import EditFieldModal, open_edit_field
from eawf.tui_v2.screens.overlays.events import (
    EventRow,
    EventsModal,
    load_recent_events,
    open_events,
)
from eawf.tui_v2.screens.overlays.metrics import (
    MetricsArgs,
    MetricsModal,
    open_metrics,
    parse_metrics_args,
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
from eawf.tui_v2.screens.overlays.pr_list import (
    PrListModal,
    PrRow,
    open_pr_list,
    parse_pr_rows,
)

__all__ = [
    "AuditFailedModal",
    "AuditProgress",
    "AuditRunningModal",
    "CheckRow",
    "CheckState",
    "ConfigModal",
    "ConfigModalState",
    "ConfirmModal",
    "DetailCard",
    "DetailModal",
    "EditFieldModal",
    "EventRow",
    "EventsModal",
    "MetricsArgs",
    "MetricsModal",
    "NeedsUserModal",
    "PlanIterRow",
    "PlanPreviewModal",
    "PlanTree",
    "PlanWaveRow",
    "PrListModal",
    "PrRow",
    "build_plan_tree",
    "format_dispatch_line",
    "load_recent_events",
    "open_audit_failed",
    "open_audit_running",
    "open_config",
    "open_edit_field",
    "open_events",
    "open_metrics",
    "open_needs_user",
    "open_plan_preview",
    "open_pr_list",
    "parse_metrics_args",
    "parse_pr_rows",
    "resolve_detail",
]
