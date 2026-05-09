"""``eawf doctor`` Typer command.

Surface contract:

- ``eawf doctor`` runs the W01 check set (tools, state presence, config
  merge), prints a Rich-formatted table, and exits ``0`` when every check
  is ``ok``/``warn`` and ``6`` when a hard probe fails (via
  :class:`eawf.cli.errors.InstrumentMissing`).
- ``eawf doctor --reprobe`` deletes the on-disk probe cache before re-running
  the probes (forces a fresh ``shutil.which`` round-trip per requirement).
- ``eawf --json doctor`` switches to the canonical JSON envelope:

  .. code-block:: json

      {
        "ok": true,
        "status": "ok",
        "checks": [
          {"name": "tools_available", "status": "ok", "detail": "3 probes ok"},
          ...
        ]
      }

Exit codes:

- ``0`` — every check passed (warnings allowed).
- ``6`` — at least one ``hard`` requirement is missing (probe raised
  :class:`eawf.cli.errors.InstrumentMissing`).
- ``1`` — any other ``fail`` status (forward-compat; the W01 surface only
  has the probe path that maps to ``6``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from eawf.cli.errors import InstrumentMissing, emit_error
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.doctor import checks as doctor_checks
from eawf.doctor.report import overall_status, to_payload, to_text
from eawf.install.instrument_probe import resolve_cache_path

logger = logging.getLogger(__name__)


doctor_app = typer.Typer(
    name="doctor",
    help="Run install-readiness checks (tools, state, config).",
    no_args_is_help=False,
    invoke_without_command=True,
)


def _maybe_clear_cache(workspace: Path | None) -> None:
    """Delete the probe cache if it exists, honouring ``EA_INSTRUMENT_PROBE``."""
    anchor = workspace if workspace is not None else Path.cwd()
    candidate = anchor / ".ea" / "instrument-probe.json"
    target = resolve_cache_path(candidate)
    if target.exists():
        try:
            target.unlink()
            logger.info(f"_maybe_clear_cache: removed {target}")
        except OSError as exc:
            logger.warning(f"_maybe_clear_cache: cannot remove {target}: {exc}")


@doctor_app.callback(invoke_without_command=True)
def doctor(
    ctx: typer.Context,
    reprobe: Annotated[
        bool,
        typer.Option(
            "--reprobe",
            help="Invalidate the cached probe results and re-run every check.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a machine-readable JSON envelope (overrides root --json).",
        ),
    ] = False,
) -> None:
    """Run install-readiness checks."""
    flags: GlobalFlags = ctx.obj
    effective_flags = GlobalFlags(
        json_output=flags.json_output or json_output,
        plain_output=flags.plain_output,
        no_input=flags.no_input,
        workspace=flags.workspace,
    )
    if reprobe:
        _maybe_clear_cache(effective_flags.workspace)

    try:
        results = doctor_checks.run_all(
            workspace=effective_flags.workspace,
            reprobe=reprobe,
        )
    except InstrumentMissing as exc:
        emit_error(exc, flags=effective_flags)

    payload = to_payload(results)
    text = to_text(results, plain=effective_flags.plain_output)
    emit_json_or_text(payload, text, flags=effective_flags)
    if overall_status(results) == "fail":
        # Defence in depth: ``run_all`` already converts hard probe failures
        # into InstrumentMissing. A residual ``fail`` here means a future
        # check produced one without raising — we still want a non-zero exit.
        raise typer.Exit(code=1)
