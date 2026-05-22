"""``eawf calibrate`` Typer sub-app — effort-bucket re-fit surface.

CLI dispatch only (AGENTS rule 1): the handler resolves the state path,
reads the typed :class:`~eawf.state.models.State`, and routes the re-fit
into :mod:`eawf.estimation.buckets`. The handler is read-only — it surfaces
a calibration verdict + nudges; it never mutates the configured centroids
or ``state.json`` (applying a nudge is an explicit follow-up operator
action, not a side effect of the report).

Verbs:

- ``eawf calibrate buckets`` — re-fit the XS/S/M/L/XL effort-bucket
  centroids from the trailing 90-day actuals and emit a nudge for any
  bucket whose fitted value drifts more than 25 percent from the
  configured value.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.estimation.buckets import CalibrationReport

logger = logging.getLogger(__name__)


calibrate_app = typer.Typer(
    name="calibrate",
    help="Re-fit estimation parameters from recorded actuals.",
    no_args_is_help=True,
    add_completion=False,
)


@calibrate_app.command(name="buckets")
def calibrate_buckets_cmd(ctx: typer.Context) -> None:
    """Re-fit the XS..XL effort buckets from 90-day actuals and nudge on drift.

    Read-only: computes the per-bucket fitted centroid from the trailing
    90-day actuals and reports the relative drift versus the configured
    centroid. Any bucket whose drift exceeds 25 percent fires a nudge so the
    operator can re-cadence it. Failures map to the canonical CLI exit
    codes:

    - :class:`~eawf.cli.errors.NotFound` (``exit=1``) when no
      ``.ea/state.json`` resolves from the cwd / ``-w`` / ``EA_STATE`` chain.
    - :class:`~eawf.cli.errors.ValidationFailed` (``exit=2``) when the
      on-disk payload fails strict schema validation.
    """
    from eawf.estimation.buckets import calibrate_buckets
    from eawf.evidence._io import load_state

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return

    try:
        state = load_state(state_path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    report = calibrate_buckets(state)
    payload = report.model_dump(mode="json")
    emit_json_or_text(payload, _render_calibration(report), flags=flags)


def _render_calibration(report: CalibrationReport) -> str:
    """Render a :class:`CalibrationReport` as a human-readable summary."""
    head = (
        f"bucket calibration · window {report.window_days}d · "
        f"nudge threshold {report.drift_threshold_pct:.0f}%"
    )
    lines = [head]
    for row in report.buckets:
        if row.fitted_eu is None:
            lines.append(
                f"  {row.bucket.value:<2} configured={row.configured_eu:.2f} EU  "
                f"fitted=— (no in-window samples)"
            )
            continue
        flag = "  NUDGE" if row.nudge else ""
        drift = row.drift_pct if row.drift_pct is not None else 0.0
        lines.append(
            f"  {row.bucket.value:<2} configured={row.configured_eu:.2f} EU  "
            f"fitted={row.fitted_eu:.2f} EU  drift={drift:.1f}%  "
            f"n={row.sample_count}{flag}"
        )
    nudged = report.nudged_buckets
    if nudged:
        labels = ", ".join(bucket.value for bucket in nudged)
        lines.append(f"re-cadence suggested for: {labels}")
    return "\n".join(lines)


__all__ = ["calibrate_app", "calibrate_buckets_cmd"]
