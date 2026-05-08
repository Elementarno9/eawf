"""``eawf validate`` — schema + invariant validation of ``.ea/state.json``.

Output modes:

- Human: ``"validate: ok"`` on success, otherwise one line per schema error
  (``"schema: ..."``) and one line per invariant violation
  (``"<CODE> at <PATH>: <MESSAGE>"``).
- ``--json``: a single JSON object with ``ok``, ``schema_errors``, and
  ``violations`` keys.

Exit codes:

- ``0`` when the report is ok.
- ``4`` when there are schema errors or invariant violations (regardless of
  ``--strict``). Schema and invariant errors are always fatal; ``--strict``
  only adds optional-key violations on top.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer

from eawf.validate.strict import validate_path

logger = logging.getLogger(__name__)


def validate(
    state_path: Annotated[Path, typer.Argument(help="Path to .ea/state.json")],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Treat absent optional keys as violations.",
        ),
    ] = False,
    output_json: Annotated[
        bool,
        typer.Option("--json", help="Emit a machine-readable JSON report."),
    ] = False,
) -> None:
    """Validate a state document against schema and cross-entity invariants."""
    report = validate_path(state_path, strict_optional=strict)
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
