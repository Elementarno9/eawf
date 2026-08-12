"""Workflow-health slice of the canonical doctor check set."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from eawf.observability.doctor.checks import (
    check_active_phase_without_iter,
    check_branch_currency,
    check_cli_daemon_version,
    check_iter_audit_links,
    check_parallel_cap_enforcement,
    check_recent_actuals,
    check_stale_session_count,
)
from eawf.observability.doctor.models import CheckResult

if TYPE_CHECKING:
    from collections.abc import Callable


def run_workflow_health_checks(
    *,
    workspace: Path | None,
    daemon_version_probe: Callable[[], str | None] | None = None,
) -> list[CheckResult]:
    """Run workflow integrity checks shared by CLI and TUI doctor surfaces."""
    return [
        check_active_phase_without_iter(workspace=workspace),
        check_stale_session_count(workspace=workspace),
        check_recent_actuals(workspace=workspace),
        check_iter_audit_links(workspace=workspace),
        check_cli_daemon_version(probe_version=daemon_version_probe),
        check_parallel_cap_enforcement(workspace=workspace),
        check_branch_currency(workspace=workspace),
    ]


__all__ = ["run_workflow_health_checks"]
