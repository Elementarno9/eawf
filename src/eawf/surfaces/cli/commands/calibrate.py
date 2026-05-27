"""``eawf calibrate`` Typer sub-app — effort-bucket re-fit/apply surface.

CLI dispatch only (AGENTS rule 1): the handler resolves the state path,
reads the typed :class:`~eawf.kernel.state.models.State`, and routes the re-fit
into :mod:`eawf.workflow.estimation.buckets`. ``buckets`` is read-only;
``apply`` writes one explicit bucket override through the layered-config
writer after an operator confirmation gate.

Verbs:

- ``eawf calibrate buckets`` — re-fit the XS/S/M/L/XL effort-bucket
  centroids from the trailing 90-day actuals and emit a nudge for any
  bucket whose fitted value drifts more than 25 percent from the
  configured value.
- ``eawf calibrate apply --bucket M`` — write the fitted centroid for one
  bucket to the chosen layered-config scope after ``--yes`` or an
  interactive confirmation.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.kernel.state.enums import EffortBucket
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.workflow.estimation.buckets import BucketCalibration, CalibrationReport

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

    - :class:`~eawf.surfaces.cli.errors.UserError` (``kind="NotFound"``, ``exit=1``)
      when no ``.ea/state.json`` resolves from the cwd / ``-w`` /
      ``EA_STATE`` chain.
    - :class:`~eawf.surfaces.cli.errors.ValidationError` (``exit=2``) when the
      on-disk payload fails strict schema validation.
    """
    from eawf.workflow.estimation.buckets import calibrate_buckets
    from eawf.workflow.evidence._io import load_state

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return

    try:
        state = load_state(state_path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    report = calibrate_buckets(state)
    payload = report.model_dump(mode="json")
    emit_json_or_text(payload, _render_calibration(report), flags=flags)


@calibrate_app.command(name="apply")
def calibrate_apply_cmd(
    ctx: typer.Context,
    bucket: Annotated[
        str,
        typer.Option("--bucket", help="Effort bucket to apply (XS|S|M|L|XL)."),
    ],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Layer to write to (global | workspace | repo | local | branch).",
        ),
    ] = "repo",
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Apply the fitted centroid without an interactive prompt."),
    ] = False,
) -> None:
    """Apply one fitted bucket centroid to layered config after confirmation."""
    import yaml

    from eawf.surfaces.cli.commands.config import _save_value_to_layer
    from eawf.workflow.estimation.buckets import calibrate_buckets
    from eawf.workflow.evidence._io import load_state

    flags: GlobalFlags = ctx.obj
    try:
        parsed_bucket = _parse_bucket(bucket)
        state_path = resolve_state_path(flags.workspace)
        state = load_state(state_path)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    report = calibrate_buckets(state)
    row = _row_for_bucket(report, parsed_bucket)
    if row.fitted_eu is None:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"bucket {parsed_bucket.value!r} has no fitted centroid to apply",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    try:
        _confirm_apply(row, yes=yes, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    try:
        target_path, repo = _target_path_for_scope(scope, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    value = _override_value(row)
    key = f"estimation.buckets.overrides.{parsed_bucket.value}"
    try:
        _save_value_to_layer(target_path=target_path, key=key, value=value, repo_root=repo)
    except cli_errors.ValidationError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    except yaml.YAMLError as exc:
        cli_errors.emit_error(
            cli_errors.ValidationError(f"config layer is not valid YAML: {exc}"),
            flags=flags,
        )
        return
    except OSError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"cannot read or write {target_path}: {exc}", kind="InvalidInput"),
            flags=flags,
        )
        return

    payload = {
        "bucket": parsed_bucket.value,
        "key": key,
        "scope": scope,
        "path": str(target_path),
        "value": value,
        "sample_count": row.sample_count,
        "drift_pct": row.drift_pct,
    }
    text = (
        f"applied {parsed_bucket.value} bucket override "
        f"expected={value['expected_eu']:.2f} EU "
        f"pessimistic={value['pessimistic_eu']:.2f} EU "
        f"(scope: {scope}, path: {target_path})"
    )
    emit_json_or_text(payload, text, flags=flags)


def _target_path_for_scope(scope: str, *, flags: GlobalFlags) -> tuple[Path, Path]:
    """Resolve the writable config target for ``calibrate apply``."""
    from eawf.kernel.config.layered import WRITABLE_LAYERS, layer_path
    from eawf.surfaces.cli.commands.config import _resolve_anchors

    if scope == "built-in":
        raise cli_errors.UserError(
            "layer 'built-in' is read-only; choose global|workspace|repo|branch|local",
            kind="InvalidInput",
        )
    if scope not in WRITABLE_LAYERS:
        raise cli_errors.UserError(
            f"unknown or non-writable scope {scope!r}; choose from {list(WRITABLE_LAYERS)}",
            kind="InvalidInput",
        )
    repo, workspace = _resolve_anchors(flags)
    try:
        target_path = layer_path(scope, workspace=workspace, repo=repo)
    except ValueError as exc:
        raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
    return target_path, repo


def _parse_bucket(raw: str) -> EffortBucket:
    """Parse a CLI bucket label into :class:`EffortBucket`."""
    label = raw.strip().upper()
    try:
        return EffortBucket(label)
    except ValueError as exc:
        allowed = ", ".join(bucket.value for bucket in EffortBucket)
        raise cli_errors.UserError(
            f"unknown effort bucket: {raw!r}; choose one of {allowed}",
            kind="InvalidInput",
        ) from exc


def _row_for_bucket(report: CalibrationReport, bucket: EffortBucket) -> BucketCalibration:
    """Return the calibration row for *bucket* from *report*."""
    for row in report.buckets:
        if row.bucket == bucket:
            return row
    raise cli_errors.UserError(f"bucket {bucket.value!r} missing from calibration report")


def _override_value(row: BucketCalibration) -> dict[str, float]:
    """Return strict config value for one bucket override row."""
    if row.fitted_eu is None:
        raise cli_errors.UserError(
            f"bucket {row.bucket.value!r} has no fitted centroid to apply",
            kind="InvalidInput",
        )
    pessimistic = row.fitted_pessimistic_eu
    if pessimistic is None:
        pessimistic = row.fitted_eu
    return {
        "expected_eu": round(row.fitted_eu, 6),
        "pessimistic_eu": round(pessimistic, 6),
    }


def _confirm_apply(row: BucketCalibration, *, yes: bool, flags: GlobalFlags) -> None:
    """Gate a config write behind ``--yes`` or interactive confirmation.

    Raises:
        UserError: When non-interactive policy forbids prompting or the
            operator declines the write.
    """
    if yes:
        return
    if flags.no_input:
        raise cli_errors.UserError(
            "--no-input passed without --yes; refusing to apply bucket calibration",
            kind="UserDeclined",
        )
    if not sys.stdin.isatty():
        raise cli_errors.UserError(
            "stdin is not a TTY and --yes was not passed; refusing to apply bucket calibration",
            kind="UserDeclined",
        )
    import questionary

    fitted = row.fitted_eu if row.fitted_eu is not None else 0.0
    answer: Any = questionary.confirm(
        (f"Apply fitted {row.bucket.value} bucket centroid {fitted:.2f} EU to layered config?"),
        default=False,
    ).ask()
    if not answer:
        raise cli_errors.UserError("user declined bucket calibration apply", kind="UserDeclined")


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


__all__ = ["calibrate_app", "calibrate_apply_cmd", "calibrate_buckets_cmd"]
