"""``eawf release bind-regime`` and ``eawf release discharge-debt``: fast-path debt.

A Milestone or Batch is bound to a delivery regime at plan time. A fast
binding defers gates, and each deferred gate is owed as a verification
debt that blocks stable release approval until the gate runs and passes.
These verbs are the operator's spelling of the two daemon verbs that bind
and discharge; the regime table, the fast quota and window, and the
refusal of a deferred safety gate are all decided daemon-side.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.release import _answer, _read_json_document, release_app
from eawf.surfaces.cli.flags import GlobalFlags

#: The daemon verbs these commands forward to, spelled here so the Typer
#: tree builds without the daemon method registry on the path.
REGIME_BIND: Final = "runtime.regime.bind"
REGIME_DISCHARGE_DEBT: Final = "runtime.regime.discharge_debt"


@release_app.command("bind-regime")
def release_bind_regime(
    ctx: typer.Context,
    from_spec: Annotated[
        Path,
        typer.Option(
            "--from-spec",
            help=(
                "JSON file carrying regime, scope_ref, policy_revision and, as the regime "
                "needs, incident_ref, expires_at, hypothesis_ref, deferred_gates and succeeds."
            ),
        ),
    ],
) -> None:
    """Bind a Milestone or Batch to the steady, fast or experimental regime.

    A fast binding needs an Incident and an expiry inside the fast window,
    counts against the fast quota, and mints one verification debt per
    deferred gate; a safety gate the fast regime keeps cannot be deferred.
    """
    flags: GlobalFlags = ctx.obj
    try:
        spec = _read_json_document(from_spec, label="regime binding")
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    _answer(ctx, REGIME_BIND, spec, subject=str(spec.get("scope_ref", "")))


@release_app.command("discharge-debt")
def release_discharge_debt(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Verification debt key, e.g. VDT-0001.")],
    head: Annotated[str, typer.Option("--head", help="Commit the deferred gate passed at.")],
    evidence_ref: Annotated[
        str, typer.Option("--evidence-ref", help="Evidence URN of the passing gate run.")
    ],
) -> None:
    """Discharge a verification debt once its deferred gate has passed."""
    _answer(
        ctx,
        REGIME_DISCHARGE_DEBT,
        {"key": key, "head": head, "evidence_ref": evidence_ref},
        subject=key,
    )
