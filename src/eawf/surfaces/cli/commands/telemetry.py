"""``eawf telemetry`` Typer sub-app — observability subsystem surface.

CLI dispatch only (AGENTS rule 1): every handler parses args, calls into
:mod:`eawf.telemetry`, and routes output through
:func:`eawf.surfaces.cli.output.emit_json_or_text`. The pricing snapshot, drift
detection, and (later waves) the projection all live in the library.

Verbs:

- ``eawf telemetry pricing-currency-check`` — validate the embedded
  ``PRICING`` snapshot's shape + internal currency and emit a typed
  :class:`~eawf.telemetry.pricing.PricingDriftReport`. With ``--strict``
  the verb exits ``2`` (``VALIDATION_ERROR``) on detected drift so the
  weekly CI gate can open an auto-refresh PR.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated

import typer

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.telemetry.pricing import PricingDriftReport

logger = logging.getLogger(__name__)


telemetry_app = typer.Typer(
    name="telemetry",
    help="Telemetry / observability subsystem — pricing currency, projection.",
    no_args_is_help=True,
    add_completion=False,
)


@telemetry_app.command(name="pricing-currency-check")
def pricing_currency_check(
    ctx: typer.Context,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit non-zero (2) when the embedded pricing snapshot drifts.",
        ),
    ] = False,
) -> None:
    """Validate the embedded pricing snapshot and emit a drift report.

    The check is offline — it confirms the embedded ``PRICING`` snapshot is
    internally consistent (every row carries the embedded ``pricing_version``
    and each cache rate equals the Anthropic-stated multiplier on that row's
    base input). It does not hit the network.

    Exit codes:
        - ``0`` — snapshot is current (or drift found without ``--strict``).
        - ``2`` (``VALIDATION_ERROR``) — drift found and ``--strict`` set.
    """
    from eawf.telemetry.pricing import check_pricing_currency

    flags: GlobalFlags = ctx.obj
    report = check_pricing_currency()
    payload = report.model_dump(mode="json")
    emit_json_or_text(payload, _render_report(report), flags=flags)
    if strict and not report.is_current:
        raise typer.Exit(code=exit_codes.VALIDATION_ERROR)


def _render_report(report: PricingDriftReport) -> str:
    """Render a :class:`PricingDriftReport` as a human-readable summary."""
    head = (
        f"pricing snapshot {report.pricing_version} "
        f"({report.model_count} models): "
        f"{'CURRENT' if report.is_current else 'DRIFT'}"
    )
    if report.is_current:
        return head
    lines = [head]
    for finding in report.findings:
        lines.append(
            f"  {finding.model_id} {finding.field}: "
            f"expected={finding.expected} actual={finding.actual} "
            f"({finding.detail})"
        )
    return "\n".join(lines)


__all__ = ["pricing_currency_check", "telemetry_app"]
