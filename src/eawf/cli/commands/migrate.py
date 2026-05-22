"""``eawf migrate`` Typer sub-app — state-schema migration surface.

CLI dispatch only (AGENTS rule 1): the handlers parse args, resolve the
``state.json`` path, and delegate the chain machinery to
:mod:`eawf.migrations`. The write routes through the migration package's
:func:`eawf.migrations.write_canonical` — the daemon canonical-writer
primitive (``portalock`` + ``atomic_write_json_locked``), never the
lock-acquiring ``atomic_write_json`` bypass (AGENTS rule 4 / D-SUP-01).

Verbs:

- ``eawf migrate`` — auto-detect from + to; run the chain.
- ``eawf migrate --to 1.1`` — explicit target version.
- ``eawf migrate --dry-run`` — show what would change; write nothing.
- ``eawf migrate --no-backup`` — skip the backup write (testing only).
- ``eawf migrate status`` — show current ``schema_version`` + chain.

Exit codes:

- ``0`` — success (incl. ``no-op`` when already at target).
- ``1`` (``USER_ERROR``) — unknown target / missing state file.
- ``2`` (``VALIDATION_ERROR``) — a step's pre/post invariant failed.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.error_codes import ErrorCode
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.migrations import (
    DEFAULT_REGISTRY,
    MigrationError,
    MigrationStepError,
    build_migration_chain,
    current_target_version,
    guard_target_supported,
    run_chain,
)
from eawf.state.resolve import resolve_with_reason

logger = logging.getLogger(__name__)


migrate_app = typer.Typer(
    name="migrate",
    help="Migrate state.json across schema versions (v1.0 -> v1.1 chain).",
    no_args_is_help=False,
    invoke_without_command=True,
    add_completion=False,
)


def _read_raw_version(state_path: Path) -> str:
    """Return the on-disk ``schema_version`` from the raw state dict.

    Reads the dict *before* any Pydantic load so a state at a version the
    live model does not accept (the whole point of migration) is still
    readable.

    Raises:
        UserError: When *state_path* is missing or carries no
            ``schema_version`` key.
    """
    if not state_path.exists():
        raise cli_errors.NotFound(f"state file not found: {state_path}")
    payload = json.loads(state_path.read_bytes())
    version = payload.get("schema_version")
    if not isinstance(version, str):
        raise cli_errors.InvalidInput(f"state.json has no string schema_version: {state_path}")
    return version


@migrate_app.callback(invoke_without_command=True)
def migrate_cmd(
    ctx: typer.Context,
    to: Annotated[
        str | None,
        typer.Option("--to", help="Explicit target schema version (e.g. 1.1)."),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would change; write nothing."),
    ] = False,
    no_backup: Annotated[
        bool,
        typer.Option("--no-backup", help="Skip the backup write (testing only; NOT recommended)."),
    ] = False,
) -> None:
    """Run the migration chain from the on-disk version to the target.

    When the on-disk version already equals the target the chain is empty
    and the verb emits a ``no-op`` envelope. Otherwise it builds the
    ordered chain, runs each step's pre/post invariant, and (unless
    ``--dry-run``) persists the result through the daemon canonical
    writer after snapshotting a gitignored backup.
    """
    # Defer to the registered subcommand (``status``) when one was invoked.
    if ctx.invoked_subcommand is not None:
        return

    flags: GlobalFlags = ctx.obj
    state_path, _reason = resolve_with_reason(workspace=flags.workspace)

    try:
        from_version = _read_raw_version(state_path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    to_version = to or current_target_version()

    # Refuse a target the live State model cannot re-validate before any
    # chain build or write — migrating past the model-supported max writes a
    # payload every subsequent read rejects, bricking the repo.
    try:
        guard_target_supported(to_version)
    except MigrationError as exc:
        cli_errors.emit_error(
            cli_errors.InvalidInput(str(exc)),
            flags=flags,
            error_code=ErrorCode.MIGRATION_TARGET_UNKNOWN,
        )
        return

    try:
        chain = build_migration_chain(
            DEFAULT_REGISTRY, from_version=from_version, to_version=to_version
        )
    except MigrationError as exc:
        cli_errors.emit_error(
            cli_errors.InvalidInput(str(exc)),
            flags=flags,
            error_code=ErrorCode.MIGRATION_TARGET_UNKNOWN,
        )
        return

    if not chain:
        emit_json_or_text(
            {"status": "no-op", "version": from_version},
            f"migrate: already at v{from_version} (no-op)",
            flags=flags,
        )
        return

    try:
        result = run_chain(
            state_path,
            chain=chain,
            from_version=from_version,
            to_version=to_version,
            dry_run=dry_run,
            backup=not no_backup,
        )
    except MigrationStepError as exc:
        code = (
            ErrorCode.MIGRATION_POSTCONDITION_FAILED
            if exc.phase == "post"
            else ErrorCode.MIGRATION_STEP_FAILED
        )
        cli_errors.emit_error(
            cli_errors.ValidationFailed(str(exc)),
            flags=flags,
            error_code=code,
            data={"step": f"{exc.from_version}->{exc.to_version}", "phase": exc.phase},
        )
        return

    payload = {
        "status": "dry-run" if dry_run else "ok",
        "from": from_version,
        "to": to_version,
        "steps": len(chain),
        "result_version": result.get("schema_version"),
    }
    verb = "would migrate" if dry_run else "migrated"
    text = f"migrate: {verb} v{from_version} -> v{to_version} ({len(chain)} step(s))"
    emit_json_or_text(payload, text, flags=flags)


@migrate_app.command("status")
def migrate_status(ctx: typer.Context) -> None:
    """Show the current ``schema_version`` and available migration edges."""
    flags: GlobalFlags = ctx.obj
    state_path, _reason = resolve_with_reason(workspace=flags.workspace)
    try:
        from_version = _read_raw_version(state_path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    target = current_target_version()
    edges = sorted(f"{step.from_version}->{step.to_version}" for step in DEFAULT_REGISTRY.values())
    payload = {
        "version": from_version,
        "target": target,
        "edges": edges,
    }
    lines = [
        f"schema_version: {from_version}",
        f"default target: {target}",
        "available migrations:",
    ]
    lines += [f"  {edge}" for edge in edges]
    emit_json_or_text(payload, "\n".join(lines), flags=flags)
