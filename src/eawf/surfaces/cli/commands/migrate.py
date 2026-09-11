"""``eawf migrate`` Typer sub-app — state-schema migration surface.

CLI dispatch only (AGENTS rule 1): the handlers parse args, resolve the
``state.json`` path, and delegate the chain machinery to
:mod:`eawf.kernel.migrations`. The write routes through the migration package's
:func:`eawf.kernel.migrations.write_canonical` — the daemon canonical-writer
primitive (``portalock`` + ``atomic_write_json_locked``), never the
lock-acquiring ``atomic_write_json`` bypass (AGENTS rule 4 / D-SUP-01).

The ``epoch2`` sub-app is a different kind of migration and is nested
rather than folded in: the chain steps one state document forward a
version at a time, while the epoch-2 cutover imports a whole corpus into
a new tree. Nesting keeps the chain verbs untouched — an operator's
``eawf migrate`` and ``eawf migrate status`` mean exactly what they did.

Verbs:

- ``eawf migrate`` — auto-detect from + to; run the chain.
- ``eawf migrate --to 1.1`` — explicit target version.
- ``eawf migrate --dry-run`` — show what would change; write nothing.
- ``eawf migrate --no-backup`` — skip the backup write (testing only).
- ``eawf migrate status`` — show current ``schema_version`` + chain.
- ``eawf migrate epoch2 --plan`` — read-only epoch-2 cutover plan.

Exit codes:

- ``0`` — success (incl. ``no-op`` when already at target).
- ``1`` (``USER_ERROR``) — unknown target / missing state file.
- ``2`` (``VALIDATION_ERROR``) — a step's pre/post invariant failed, or
  an importer rule refused the epoch-2 corpus.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError as PydanticValidationError

from eawf.kernel.migration.epoch2.errors import MigrationRuleError
from eawf.kernel.migration.epoch2.plan_mode import (
    EPOCH2_PLAN_METHOD,
    Epoch2PlanRequest,
    plan_cutover,
    plan_envelope,
)
from eawf.kernel.migrations import (
    DEFAULT_REGISTRY,
    MigrationError,
    MigrationStepError,
    build_migration_chain,
    current_target_version,
    guard_target_supported,
    run_chain,
)
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.error_codes import ErrorCode
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

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
        raise cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound")
    payload = json.loads(state_path.read_bytes())
    version = payload.get("schema_version")
    if not isinstance(version, str):
        raise cli_errors.UserError(
            f"state.json has no string schema_version: {state_path}", kind="InvalidInput"
        )
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
            cli_errors.UserError(str(exc), kind="InvalidInput"),
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
            cli_errors.UserError(str(exc), kind="InvalidInput"),
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
            cli_errors.ValidationError(str(exc)),
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


epoch2_app = typer.Typer(
    name="epoch2",
    help="Plan the one-shot epoch-1 to epoch-2 corpus cutover.",
    no_args_is_help=True,
    invoke_without_command=True,
    add_completion=False,
)
migrate_app.add_typer(epoch2_app)


def _epoch2_request(
    *,
    snapshot_root: Path,
    allowlist: Path,
    workspace_key: str,
    project_key: str,
    repository_key: str,
    sealed_by: str,
) -> Epoch2PlanRequest:
    """Parse the plan request, failing at the CLI boundary on a bad field.

    Raises:
        UserError: When a field violates the wire contract. The library
            takes an already-validated request, so the rejection happens
            here rather than three frames in.
    """
    try:
        return Epoch2PlanRequest(
            snapshot_root=str(snapshot_root),
            allowlist_path=str(allowlist),
            workspace_key=workspace_key,
            project_key=project_key,
            repository_key=repository_key,
            sealed_by=sealed_by,
        )
    except PydanticValidationError as exc:
        raise cli_errors.UserError(
            f"invalid epoch2 plan request: {exc}", kind="InvalidInput"
        ) from exc


def _epoch2_plan_payload(request: Epoch2PlanRequest) -> dict[str, Any]:
    """Return the cutover plan, preferring the daemon's own computation.

    Plan mode is a read, so the in-process arm is not a carve-out from
    the canonical-mutator rule — it is the same read run locally. The
    daemon is still preferred because the approval digest it computes is
    the one its later apply will verify.

    Raises:
        ValidationError: When an importer rule refuses the corpus.
        CliError: When the daemon answers with any other failure.
    """
    if _epoch2_daemon_enabled():
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient() as client:
                return client.call(EPOCH2_PLAN_METHOD, request.model_dump(mode="json"))
        except DaemonRpcError as exc:
            if exc.code in (-32602, cli_errors.RPC_VALIDATION_FAILED):
                raise cli_errors.ValidationError(exc.message) from exc
            if exc.code != -32601:
                raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
            logger.debug("_epoch2_plan_payload daemon-rpc method-not-found; fallback")
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise cli_errors.DaemonUnreachable(
                f"daemon unavailable for {EPOCH2_PLAN_METHOD}: {exc}"
            ) from exc

    try:
        plan = plan_cutover(request, sealed_at=datetime.now(UTC))
    except MigrationRuleError as exc:
        raise cli_errors.ValidationError(f"{exc.code}: {exc}") from exc
    return plan_envelope(plan)


def _epoch2_daemon_enabled() -> bool:
    """Whether this process routes the plan through the daemon.

    Mirrors the repo-wide proxy gate: ``EAWF_DAEMONLESS=1`` opts one
    process out, as do the CI and recovery-shell carve-outs the merged
    config expresses.
    """
    if os.environ.get("EAWF_DAEMONLESS", "") == "1":
        return False
    from eawf.surfaces.cli._mutation import _proxy_enabled

    return _proxy_enabled(None)


@epoch2_app.callback(invoke_without_command=True)
def epoch2_cmd(
    ctx: typer.Context,
    snapshot_root: Annotated[
        Path,
        typer.Option("--snapshot-root", help="Staging directory holding the epoch-1 corpus."),
    ],
    allowlist: Annotated[
        Path,
        typer.Option("--allowlist", help="Path to the allowed-legacy-symbol allowlist."),
    ],
    workspace_key: Annotated[
        str,
        typer.Option("--workspace-key", help="Addressing workspace for the imported corpus."),
    ],
    project_key: Annotated[
        str,
        typer.Option("--project-key", help="Addressing project for the imported corpus."),
    ],
    repository_key: Annotated[
        str,
        typer.Option("--repository-key", help="Addressing repository for the imported corpus."),
    ],
    plan: Annotated[
        bool,
        typer.Option("--plan", help="Required acknowledgement that this run writes nothing."),
    ] = False,
    sealed_by: Annotated[
        str,
        typer.Option("--sealed-by", help="Principal recorded in the manifest seal."),
    ] = "operator",
) -> None:
    """Emit the read-only epoch-2 cutover plan for a staged corpus.

    The verb writes nothing: no canonical document, no registry, no
    staging tree. It reports every mapping, every dropped collection with
    its proof, every Track an operator still has to assign and every row
    the cutover cannot place — which is the whole point of having it
    before the apply exists.
    """
    flags: GlobalFlags = ctx.obj
    if not plan:
        cli_errors.emit_error(
            cli_errors.UserError(
                "pass --plan: epoch2 has no default mode, so a run is never implicitly "
                "read-only or implicitly a write",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    try:
        request = _epoch2_request(
            snapshot_root=snapshot_root,
            allowlist=allowlist,
            workspace_key=workspace_key,
            project_key=project_key,
            repository_key=repository_key,
            sealed_by=sealed_by,
        )
        payload = _epoch2_plan_payload(request)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    emit_json_or_text(payload, _epoch2_plan_text(payload), flags=flags)


def _epoch2_plan_text(payload: Mapping[str, Any]) -> str:
    """Render one plan envelope for a terminal.

    Args:
        payload: The plan envelope.

    Returns:
        One header line naming the digests, then one line per step.
    """
    lines = [
        f"epoch2 plan: {payload['target_rows']} target rows, "
        f"{payload['required_operator_assignment_count']} operator assignment(s), "
        f"{payload['unresolved_row_count']} unresolved row(s)",
        f"  source digest:   {payload['source_digest']}",
        f"  manifest digest: {payload['manifest_digest']}",
        f"  approval digest: {payload['approval_digest']}",
        f"  applicable:      {payload['applicable']}",
    ]
    lines += [f"  {row['order']}. {row['step']}: {row['summary']}" for row in payload["steps"]]
    return "\n".join(lines)
