"""Calibration-table render helpers for the metrics TUI."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from eawf.kernel.state.enums import AgentSessionRole, EffortBucket
from eawf.workflow.estimation.buckets import BucketCalibration, CalibrationReport

_NO_DATA = "[$text-muted]no per-role calibration data[/]"
_TILE_ROLE_WIDTH = 10
_DRILL_ROLE_WIDTH = 17
_CELL_WIDTH = 6


class RoleCalibrationRow(Protocol):
    """Projection row carrying a role-specific CalibrationReport."""

    agent_role: AgentSessionRole
    report: CalibrationReport


def render_role_calibration_tile(rows: Sequence[RoleCalibrationRow]) -> str:
    """Render the compact per-role fit grid for the sixth metrics tile."""
    if not rows:
        return _NO_DATA
    lines = _fit_grid(rows, role_width=_TILE_ROLE_WIDTH, max_roles=3)
    if len(rows) > 3:
        lines.append(f"+{len(rows) - 3} roles")
    return "\n".join(lines)


def render_role_calibration_drilldown(rows: Sequence[RoleCalibrationRow]) -> str:
    """Render the full per-role calibration drilldown body."""
    if not rows:
        return _NO_DATA
    first_report = rows[0].report
    lines = [
        "fit EU by role and bucket",
        *_fit_grid(rows, role_width=_DRILL_ROLE_WIDTH, max_roles=None),
        "",
        f"window {first_report.window_days}d | nudge threshold "
        f"{first_report.drift_threshold_pct:.0f}% | ! = nudge",
        "",
        "details",
    ]
    for role_row in rows:
        lines.append(role_row.agent_role.value)
        lines.extend(_detail_lines(role_row.report))
    return "\n".join(lines)


def _fit_grid(
    rows: Sequence[RoleCalibrationRow],
    *,
    role_width: int,
    max_roles: int | None,
) -> list[str]:
    """Return fixed-width fit-grid lines."""
    visible = rows if max_roles is None else rows[:max_roles]
    header_cells = " ".join(f"{bucket.value:>{_CELL_WIDTH}}" for bucket in EffortBucket)
    lines = [f"{'role':<{role_width}} {header_cells}"]
    for role_row in visible:
        cells = " ".join(
            f"{_fit_cell(_bucket_row(role_row.report, bucket)):>{_CELL_WIDTH}}"
            for bucket in EffortBucket
        )
        lines.append(f"{_role_label(role_row.agent_role, role_width)} {cells}")
    return lines


def _detail_lines(report: CalibrationReport) -> list[str]:
    """Return bucket detail rows from one CalibrationReport."""
    lines: list[str] = []
    for bucket in EffortBucket:
        row = _bucket_row(report, bucket)
        if row is None:
            lines.append(f"  {bucket.value:<2} configured=none fitted=none n=0")
            continue
        if row.fitted_eu is None:
            lines.append(f"  {bucket.value:<2} configured={row.configured_eu:.2f} fitted=none n=0")
            continue
        drift = row.drift_pct if row.drift_pct is not None else 0.0
        suffix = " NUDGE" if row.nudge else ""
        lines.append(
            f"  {bucket.value:<2} configured={row.configured_eu:.2f} "
            f"fitted={row.fitted_eu:.2f} drift={drift:.1f}% "
            f"n={row.sample_count}{suffix}"
        )
    return lines


def _bucket_row(report: CalibrationReport, bucket: EffortBucket) -> BucketCalibration | None:
    """Return the calibration row for *bucket*, if present."""
    for row in report.buckets:
        if row.bucket == bucket:
            return row
    return None


def _fit_cell(row: BucketCalibration | None) -> str:
    """Format one grid cell as fitted EU plus nudge marker."""
    if row is None or row.fitted_eu is None or row.sample_count == 0:
        return "-"
    suffix = "!" if row.nudge else ""
    return f"{row.fitted_eu:.1f}{suffix}"


def _role_label(role: AgentSessionRole, width: int) -> str:
    """Return fixed-width role label, truncated only for compact tile rows."""
    return role.value[:width].ljust(width)


__all__ = [
    "RoleCalibrationRow",
    "render_role_calibration_drilldown",
    "render_role_calibration_tile",
]
