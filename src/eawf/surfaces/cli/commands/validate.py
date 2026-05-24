"""``eawf validate`` — schema + invariant validation of state or envelope JSON.

Two modes, auto-detected from the file contents:

- **State mode** (default): the file has a top-level ``project`` or
  ``workspace`` key. The validator runs the full schema + invariant
  pipeline from :mod:`eawf.kernel.validate.strict`.
- **Envelope mode**: the file has a top-level ``header`` and ``body``.
  The validator runs the §15.1 strict-mode contracts (``needs_user`` →
  ``body.user_question``; ``blocked|failed`` → ``footer.repair_commands``).

Output modes:

- Human: ``"validate: ok"`` on success, otherwise one line per schema error
  (``"schema: ..."``), one line per invariant violation
  (``"<CODE> at <PATH>: <MESSAGE>"``), or one line per envelope contract
  error (``"contract: ..."``).
- ``--json``: a single JSON object with ``ok``, ``schema_errors``, and
  ``violations`` (state mode) or ``contract_errors`` (envelope mode).

Exit codes:

- ``0`` when the report is ok.
- ``4`` when there are schema errors, invariant violations, or contract
  errors (regardless of ``--strict``). ``--strict`` only adds optional-key
  violations to state-mode validation; envelope-mode contracts are
  always strict.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer

logger = logging.getLogger(__name__)


def _detect_mode(payload: dict[str, Any]) -> str:
    """Return ``"envelope"`` or ``"state"`` based on top-level keys.

    Envelope payloads have ``header`` AND ``body`` AND ``footer`` (the
    three OutputEnvelope keys). State payloads have ``project`` or
    ``workspace`` (the alternation-required state roots).

    Falls back to ``"state"`` for anything else so the existing CLI
    contract is preserved on malformed input — schema validation will
    surface the right error.
    """
    if {"header", "body", "footer"}.issubset(payload.keys()):
        return "envelope"
    return "state"


def validate(
    target_path: Annotated[
        Path,
        typer.Argument(
            help="Path to .ea/state.json or a skill output envelope JSON.",
        ),
    ],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help=(
                "State mode: treat absent optional keys as violations. "
                "Envelope mode: always strict; the flag is accepted for "
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
    from eawf.kernel.validate.strict import validate_envelope, validate_path

    raw = orjson.loads(Path(target_path).read_bytes())
    if not isinstance(raw, dict):
        typer.echo("schema: top-level value must be a JSON object")
        raise typer.Exit(code=4)
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
            raise typer.Exit(code=4)
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
        raise typer.Exit(code=4)
