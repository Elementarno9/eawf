"""``eawf validate`` — schema + invariant validation of state or envelope JSON.

Three modes, auto-detected from the file contents:

- **State mode** (default): the file has a top-level ``project`` or
  ``workspace`` key. The validator runs the full schema + invariant
  pipeline from :mod:`eawf.kernel.validate.strict`.
- **Envelope mode**: the file has a top-level ``header`` and ``body``.
  The validator runs the §15.1 strict-mode contracts (``needs_user`` →
  ``body.user_question``; ``blocked|failed`` → ``footer.repair_commands``).
- **Epoch-2 mode**: the file is a generation document (its
  ``schema_version`` is the epoch-2 one). Each native row is checked
  against its own model and each Batch and Task against the record it is
  filed under (:mod:`eawf.kernel.validate.epoch2`); violation paths read
  ``<collection>/<key>``.

Output modes:

- Human: ``"validate: ok"`` on success, otherwise one line per schema error
  (``"schema: ..."``), one line per invariant violation
  (``"<CODE> at <PATH>: <MESSAGE>"``), or one line per envelope contract
  error (``"contract: ..."``).
- ``--json`` (the command's own flag or the global one): a single JSON
  object with ``ok``, ``schema_errors``, and ``violations`` (state mode),
  ``contract_errors`` (envelope mode), or ``mode`` and ``violations``
  (epoch-2 mode).

Exit codes:

- ``0`` when the report is ok.
- ``2`` (``VALIDATION_ERROR``) when there are schema errors, invariant violations, or contract
  errors (regardless of ``--strict``). ``--strict`` only adds optional-key
  violations to state-mode validation; envelope-mode contracts are
  always strict, and epoch-2 mode has no optional keys to flag.
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


def _detect_mode(payload: dict[str, Any]) -> str:
    """Return ``"envelope"``, ``"epoch2"`` or ``"state"`` based on top-level keys.

    Envelope payloads have ``header`` AND ``body`` AND ``footer`` (the
    three OutputEnvelope keys). A generation document carries the epoch-2
    ``schema_version``. State payloads have ``project`` or ``workspace``
    (the alternation-required state roots).

    Falls back to ``"state"`` for anything else so the existing CLI
    contract is preserved on malformed input — schema validation will
    surface the right error.
    """
    from eawf.kernel.validate.epoch2 import is_generation_document

    if {"header", "body", "footer"}.issubset(payload.keys()):
        return "envelope"
    if is_generation_document(payload):
        return "epoch2"
    return "state"


def validate(
    ctx: typer.Context,
    target_path: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path to .ea/state.json, a generation's state.json or a skill output envelope JSON."
            ),
        ),
    ],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help=(
                "State mode: treat absent optional keys as violations. "
                "Envelope and epoch-2 modes: the flag is accepted for "
                "surface symmetry but does not change behaviour."
            ),
        ),
    ] = False,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable JSON report."),
    ] = False,
) -> None:
    """Validate a state or envelope document.

    The mode is auto-detected from the top-level keys of *target_path*.
    """
    from eawf.kernel.validate.epoch2 import validate_generation
    from eawf.kernel.validate.strict import validate_envelope, validate_path

    flags: GlobalFlags = ctx.obj
    output_json = output_json or flags.json_output
    raw = orjson.loads(Path(target_path).read_bytes())
    if not isinstance(raw, dict):
        typer.echo("schema: top-level value must be a JSON object")
        raise typer.Exit(code=exit_codes.VALIDATION_ERROR)
    mode = _detect_mode(raw)

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
            }
            typer.echo(json.dumps(payload))
        else:
            if gen_report.ok:
                typer.echo("validate: ok")
            else:
                for err in gen_report.schema_errors:
                    typer.echo(f"schema: {err}")
                for row in gen_report.violations:
                    typer.echo(f"{row.code} at {row.path}: {row.message}")
        if not gen_report.ok:
            raise typer.Exit(code=exit_codes.VALIDATION_ERROR)
        return

    report = validate_path(target_path, strict_optional=strict)
    if output_json:
        payload = {
            "ok": report.ok,
            "schema_errors": report.schema_errors,
            "violations": [
                {"code": v.code, "path": v.path, "message": v.message} for v in report.violations
            ],
        }
        typer.echo(json.dumps(payload))
    else:
        if report.ok:
            typer.echo("validate: ok")
        else:
            for err in report.schema_errors:
                typer.echo(f"schema: {err}")
            for v in report.violations:
                typer.echo(f"{v.code} at {v.path}: {v.message}")
    if not report.ok:
        raise typer.Exit(code=exit_codes.VALIDATION_ERROR)
