"""``eawf validate`` — validation of a generation document or a skill output envelope.

Two modes, auto-detected from the file contents:

- **Envelope mode**: the file has a top-level ``header`` and ``body``.
  The validator runs the §15.1 strict-mode contracts (``needs_user`` →
  ``body.user_question``; ``blocked|failed`` → ``footer.repair_commands``).
- **Epoch-2 mode**: the file is a generation document (its
  ``schema_version`` is the epoch-2 one). Each imported row and each
  native row of a modelled collection is checked against its model and
  the key it is filed under, and each Batch and Task against the record
  it is filed under (:mod:`eawf.kernel.validate.epoch2`); violation paths
  read ``<collection>/<key>``. A reference a copied document cannot
  resolve for want of a ledger is printed as a warning.

Any other object is refused: a 0.6 ``state.json`` is migrated under
0.7.0rc4 before this release reads it.

Output modes:

- Human: ``"validate: ok"`` on success, otherwise one line per schema error
  (``"schema: ..."``), one line per violation
  (``"<CODE> at <PATH>: <MESSAGE>"``), or one line per envelope contract
  error (``"contract: ..."``).
- ``--json`` (the command's own flag or the global one): a single JSON
  object with ``ok``, ``schema_errors``, and ``contract_errors`` (envelope
  mode) or ``mode`` and ``violations`` (epoch-2 mode).

Exit codes:

- ``0`` when the report is ok.
- ``2`` (``VALIDATION_ERROR``) when there are schema errors, violations,
  contract errors, or the document is neither mode.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags

logger = logging.getLogger(__name__)


def _detect_mode(payload: dict[str, Any]) -> str | None:
    """Return ``"envelope"`` or ``"epoch2"`` from the top-level keys, else ``None``.

    Envelope payloads have ``header`` AND ``body`` AND ``footer`` (the
    three OutputEnvelope keys). A generation document carries the epoch-2
    ``schema_version``.
    """
    from eawf.kernel.validate.epoch2 import is_generation_document

    if {"header", "body", "footer"}.issubset(payload.keys()):
        return "envelope"
    if is_generation_document(payload):
        return "epoch2"
    return None


def validate(
    ctx: typer.Context,
    target_path: Annotated[
        Path,
        typer.Argument(
            help="Path to a generation's state.json or a skill output envelope JSON.",
        ),
    ],
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable JSON report."),
    ] = False,
) -> None:
    """Validate a generation document or an output envelope.

    The mode is auto-detected from the top-level keys of *target_path*.
    """
    from eawf.kernel.validate.epoch2 import validate_generation
    from eawf.kernel.validate.strict import validate_envelope
    from eawf.surfaces.cli.upgrade_refusal import UPGRADE_STEPS

    flags: GlobalFlags = ctx.obj
    output_json = output_json or flags.json_output
    raw = orjson.loads(Path(target_path).read_bytes())
    if not isinstance(raw, dict):
        typer.echo("schema: top-level value must be a JSON object")
        raise typer.Exit(code=exit_codes.VALIDATION_ERROR)
    mode = _detect_mode(raw)
    if mode is None:
        typer.echo(
            "schema: neither a generation document nor an output envelope; "
            f"a 0.6 state.json is read only after migration: {UPGRADE_STEPS}"
        )
        raise typer.Exit(code=exit_codes.VALIDATION_ERROR)

    if mode == "envelope":
        env_report = validate_envelope(raw)
        if output_json:
            payload = {
                "ok": env_report.ok,
                "schema_errors": env_report.schema_errors,
                "contract_errors": env_report.contract_errors,
            }
            typer.echo(json.dumps(payload))
        else:
            if env_report.ok:
                typer.echo("validate: ok")
            else:
                for err in env_report.schema_errors:
                    typer.echo(f"schema: {err}")
                for err in env_report.contract_errors:
                    typer.echo(f"contract: {err}")
        if not env_report.ok:
            raise typer.Exit(code=exit_codes.VALIDATION_ERROR)
        return

    if mode == "epoch2":
        gen_report = validate_generation(Path(target_path))
        if output_json:
            payload = {
                "ok": gen_report.ok,
                "mode": mode,
                "schema_errors": gen_report.schema_errors,
                "violations": [
                    {"code": row.code, "path": row.path, "message": row.message}
                    for row in gen_report.violations
                ],
                "warnings": [
                    {"code": row.code, "path": row.path, "message": row.message}
                    for row in gen_report.warnings
                ],
            }
            typer.echo(json.dumps(payload))
        else:
            for row in gen_report.warnings:
                typer.echo(f"warning: {row.code} at {row.path}: {row.message}")
            if gen_report.ok:
                typer.echo("validate: ok")
            else:
                for err in gen_report.schema_errors:
                    typer.echo(f"schema: {err}")
                for row in gen_report.violations:
                    typer.echo(f"{row.code} at {row.path}: {row.message}")
        if not gen_report.ok:
            raise typer.Exit(code=exit_codes.VALIDATION_ERROR)
