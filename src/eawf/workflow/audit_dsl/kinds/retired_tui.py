"""The TUI gate kinds whose surface was the retired epoch-1 application.

``affordance_parity``, ``tui_flow`` and the live-capture modes of
``mockup_golden_diff`` mounted the epoch-1 Textual application and drove its
key bindings. That application is deleted; the console proves its keys and
frames with its own replay harness (:mod:`eawf.surfaces.tui.console.harness`).
The kind names stay registered so gate rows already persisted with them still
load, and every run reports ``blocked``: a gate never passes on a surface that
no longer exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eawf.workflow.audit_dsl.models import CheckResult

if TYPE_CHECKING:
    from pathlib import Path

    from eawf.workflow.audit_dsl.models import CheckSpec

#: Why a retired TUI gate cannot run, and what proves the console instead.
RETIRED_TUI_DETAIL: str = (
    "the epoch-1 TUI app this gate drove is retired; gate the console with its replay"
    " harness (tests/snapshots/tui/console)"
)


def check_retired_tui(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Report a gate over the retired epoch-1 TUI as ``blocked``.

    Args:
        spec: The gate spec; its name and kind are echoed into the result.
        cwd: The checkout root every kind receives; nothing here reads it.

    Returns:
        A ``blocked`` result carrying :data:`RETIRED_TUI_DETAIL`.
    """
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=False,
        status="blocked",
        details=RETIRED_TUI_DETAIL,
    )
