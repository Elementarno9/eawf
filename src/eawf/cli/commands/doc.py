"""``eawf doc`` Typer sub-app.

Houses the doc-drift linter (``eawf doc verify``). Sibling of ``eawf doctor``;
shares ``GlobalFlags`` + canonical JSON-envelope shape.

Exit codes:

- ``0`` — no drift, no cross-check violation (or drift detected without
  ``--strict``).
- ``2`` (``NOT_FOUND``) — no ``state.json`` resolvable.
- ``4`` (``VALIDATION_FAILED``) — drift or cross-check violation detected and
  ``--strict`` was set.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli import exit_codes
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.doctor.doc_verify import verify_docs
from eawf.state.models import State
from eawf.state.resolve import resolve_with_reason
from eawf.validate.strict import validate_state

logger = logging.getLogger(__name__)


doc_app = typer.Typer(
    name="doc",
    help="Read-only documentation drift + state-vs-doc cross-checks.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_state(state_path: Path) -> State:
    if not state_path.exists():
        raise cli_errors.NotFound(f"state file not found: {state_path}")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationFailed(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


@doc_app.command("verify")
def doc_verify(
    ctx: typer.Context,
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit 4 when any drift or cross-check violation is detected.",
        ),
    ] = False,
) -> None:
    """Verify that rendered docs match state.json + manifest hashes."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path, _reason = resolve_with_reason(flags.workspace)
        state = _load_state(state_path)
        repo_root = state_path.parent.parent
        report = verify_docs(state, repo_root)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload = {
        "status": report.status,
        "manifest_targets": report.manifest_targets,
        "manifest_entries": report.manifest_entries,
        "drift": [
            {
                "target": str(r.target),
                "id": r.id,
                "kind": r.kind,
                "on_disk_hash": r.on_disk_hash,
                "manifest_hash": r.manifest_hash,
            }
            for r in report.drift_reports
            if r.kind != "ok"
        ],
        "cross_check_violations": [
            {"code": v.code, "target": v.target, "message": v.message}
            for v in report.cross_check_violations
        ],
        "drift_count": report.extras.get("drift_count", 0),
        "cross_check_count": report.extras.get("cross_check_count", 0),
    }
    if report.status == "ok":
        text = (
            f"doc verify: ok ({report.manifest_targets} targets, {report.manifest_entries} regions)"
        )
    else:
        text = (
            f"doc verify: drift detected "
            f"(drift={payload['drift_count']}, cross_check={payload['cross_check_count']})"
        )
    emit_json_or_text(payload, text, flags=flags)
    if strict and report.status != "ok":
        raise typer.Exit(code=exit_codes.VALIDATION_FAILED)
