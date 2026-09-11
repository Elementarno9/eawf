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
- ``eawf migrate epoch2 --export`` — read-only epoch-1 collection export.
- ``eawf migrate epoch2 --apply --plan-digest <d>`` — build and select a
  new generation in a tree that has declared itself disposable.
- ``eawf migrate epoch2 --recover [--manifest <p>]`` — finish or undo an
  interrupted cutover, whichever the tree's boundary allows.
- ``eawf migrate epoch2 --rollback [--manifest <p>]`` — put a tree back to
  the surfaces its restore point pinned.

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
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Final

import typer
from pydantic import ValidationError as PydanticValidationError

from eawf.kernel.migration.epoch2.errors import MigrationRuleError
from eawf.kernel.migration.epoch2.export import (
    EPOCH2_EXPORT_METHOD,
    Epoch2ExportRequest,
    export_epoch1,
    export_text,
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

if TYPE_CHECKING:
    from eawf.kernel.migration.epoch2.apply import Epoch2ApplyRequest
    from eawf.kernel.migration.epoch2.plan_mode import Epoch2PlanRequest
    from eawf.kernel.migration.epoch2.recovery import Epoch2RecoverRequest

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
    help="Plan, apply or export the one-shot epoch-1 to epoch-2 cutover.",
    no_args_is_help=True,
    invoke_without_command=True,
    add_completion=False,
)
migrate_app.add_typer(epoch2_app)


class Epoch2Mode(StrEnum):
    """The five things ``eawf migrate epoch2`` can be asked to do."""

    PLAN = "plan"
    APPLY = "apply"
    EXPORT = "export"
    RECOVER = "recover"
    ROLLBACK = "rollback"


#: The modes that read a corpus. The two recovery modes do not: they repair
#: a tree from the restore point and the journal inside it, so asking for a
#: snapshot root would be asking for a corpus nothing reads.
_CORPUS_MODES: Final[tuple[Epoch2Mode, ...]] = (
    Epoch2Mode.PLAN,
    Epoch2Mode.APPLY,
    Epoch2Mode.EXPORT,
)


def _epoch2_mode(
    *, plan: bool, apply_: bool, export: bool, recover: bool, rollback: bool
) -> Epoch2Mode:
    """Return the one mode the flags select.

    Args:
        plan: Whether ``--plan`` was passed.
        apply_: Whether ``--apply`` was passed.
        export: Whether ``--export`` was passed.
        recover: Whether ``--recover`` was passed.
        rollback: Whether ``--rollback`` was passed.

    Returns:
        The selected mode.

    Raises:
        UserError: No mode or more than one was selected. There is no
            default: a run of this verb is never implicitly read-only and
            never implicitly a write.
    """
    selected = [
        mode
        for mode, chosen in (
            (Epoch2Mode.PLAN, plan),
            (Epoch2Mode.APPLY, apply_),
            (Epoch2Mode.EXPORT, export),
            (Epoch2Mode.RECOVER, recover),
            (Epoch2Mode.ROLLBACK, rollback),
        )
        if chosen
    ]
    if len(selected) == 1:
        return selected[0]
    named = ", ".join(f"--{mode.value}" for mode in Epoch2Mode)
    raise cli_errors.UserError(
        f"pass exactly one of {named}: epoch2 has no default mode, so a run is never "
        f"implicitly read-only or implicitly a write (got {len(selected)})",
        kind="InvalidInput",
    )


def _required[T](value: T | None, *, option: str, mode: Epoch2Mode) -> T:
    """Return ``value``, refusing the mode when the option was omitted.

    Args:
        value: The parsed option value.
        option: The option's spelling, for the message.
        mode: The mode that requires it.

    Returns:
        The value.

    Raises:
        UserError: The option was omitted. Every addressing slot is
            required rather than defaulted, because a default here is a
            guess about where a whole corpus lands.
    """
    if value is None:
        raise cli_errors.UserError(f"--{mode.value} requires {option}", kind="InvalidInput")
    return value


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
    from eawf.kernel.migration.epoch2.plan_mode import Epoch2PlanRequest

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


def _epoch2_apply_request(
    *,
    plan_request: Epoch2PlanRequest,
    target_root: Path,
    registry_path: Path,
    plan_digest: str,
    accept_unresolved: list[str],
) -> Epoch2ApplyRequest:
    """Parse the apply request, failing at the CLI boundary on a bad field.

    Raises:
        UserError: When a field violates the wire contract.
    """
    from eawf.kernel.migration.epoch2.apply import Epoch2ApplyRequest

    try:
        return Epoch2ApplyRequest(
            plan_request=plan_request,
            target_root=str(target_root),
            registry_path=str(registry_path),
            plan_digest=plan_digest,
            accepted_unresolved_rows=tuple(accept_unresolved),
        )
    except PydanticValidationError as exc:
        raise cli_errors.UserError(
            f"invalid epoch2 apply request: {exc}", kind="InvalidInput"
        ) from exc


def _epoch2_recover_request(
    *, mode: Epoch2Mode, target_root: Path, manifest: Path | None
) -> Epoch2RecoverRequest:
    """Parse the recovery request, failing at the CLI boundary on a bad field.

    Args:
        mode: Which recovery mode was selected.
        target_root: The tree to recover.
        manifest: The restore manifest to write back, or ``None`` to use
            the one the apply left inside the tree.

    Returns:
        The validated request.

    Raises:
        UserError: When a field violates the wire contract.
    """
    from eawf.kernel.migration.epoch2.recovery import Epoch2RecoverRequest, RecoveryAction

    action = RecoveryAction.RECOVER if mode is Epoch2Mode.RECOVER else RecoveryAction.ROLLBACK
    try:
        return Epoch2RecoverRequest(
            target_root=str(target_root),
            action=action,
            manifest_path=None if manifest is None else str(manifest),
        )
    except PydanticValidationError as exc:
        raise cli_errors.UserError(
            f"invalid epoch2 recovery request: {exc}", kind="InvalidInput"
        ) from exc


def _epoch2_payload(
    *, method: str, params: Mapping[str, Any], local: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Return one epoch-2 envelope, preferring the daemon's own computation.

    The daemon is preferred for all three modes, for two different
    reasons. For the reads it is the side of the wire that computes the
    approval digest a later apply verifies. For the apply it is the
    canonical mutator. The in-process arm is the documented fallback: the
    apply takes the same ``portalocker`` authority locks itself, so a
    daemonless run is the direct-write path rather than an unguarded one.

    Args:
        method: The JSON-RPC method to call.
        params: The already-validated request, as JSON.
        local: The in-process computation to fall back to.

    Returns:
        The envelope.

    Raises:
        ValidationError: When an importer rule refuses the corpus.
        CliError: When the daemon answers with any other failure.
    """
    if _epoch2_daemon_enabled():
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        try:
            with DaemonClient() as client:
                return client.call(method, dict(params))
        except DaemonRpcError as exc:
            if exc.code in (-32602, cli_errors.RPC_VALIDATION_FAILED):
                raise cli_errors.ValidationError(exc.message) from exc
            if exc.code != -32601:
                raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
            logger.debug(f"_epoch2_payload daemon-rpc method-not-found method={method}; fallback")
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise cli_errors.DaemonUnreachable(f"daemon unavailable for {method}: {exc}") from exc

    try:
        return local()
    except MigrationRuleError as exc:
        raise cli_errors.ValidationError(f"{exc.code}: {exc}") from exc


def _epoch2_daemon_enabled() -> bool:
    """Whether this process routes the epoch-2 verbs through the daemon.

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
        Path | None,
        typer.Option("--snapshot-root", help="Staging directory holding the epoch-1 corpus."),
    ] = None,
    allowlist: Annotated[
        Path | None,
        typer.Option("--allowlist", help="Path to the allowed-legacy-symbol allowlist."),
    ] = None,
    workspace_key: Annotated[
        str | None,
        typer.Option("--workspace-key", help="Addressing workspace for the imported corpus."),
    ] = None,
    project_key: Annotated[
        str | None,
        typer.Option("--project-key", help="Addressing project for the imported corpus."),
    ] = None,
    repository_key: Annotated[
        str | None,
        typer.Option("--repository-key", help="Addressing repository for the imported corpus."),
    ] = None,
    plan: Annotated[
        bool,
        typer.Option("--plan", help="Read-only plan: report what the cutover would do."),
    ] = False,
    apply_: Annotated[
        bool,
        typer.Option("--apply", help="Build and select a generation (needs --plan-digest)."),
    ] = False,
    export: Annotated[
        bool,
        typer.Option("--export", help="Read-only export of every declared epoch-1 collection."),
    ] = False,
    recover: Annotated[
        bool,
        typer.Option(
            "--recover", help="Finish or undo an interrupted cutover (needs --target-root)."
        ),
    ] = False,
    rollback: Annotated[
        bool,
        typer.Option("--rollback", help="Put a tree back to its pre-cutover surfaces."),
    ] = False,
    manifest: Annotated[
        Path | None,
        typer.Option(
            "--manifest", help="Restore manifest to write back; defaults to the in-tree one."
        ),
    ] = None,
    plan_digest: Annotated[
        str | None,
        typer.Option("--plan-digest", help="The approval digest of the plan being applied."),
    ] = None,
    target_root: Annotated[
        Path | None,
        typer.Option("--target-root", help="Tree the new generation is built in."),
    ] = None,
    registry_path: Annotated[
        Path | None,
        typer.Option("--registry-path", help="Workspace registry the addressing key resolves in."),
    ] = None,
    accept_unresolved: Annotated[
        list[str] | None,
        typer.Option(
            "--accept-unresolved",
            help="Address of one unresolved row the apply accepts; repeat per row.",
        ),
    ] = None,
    sealed_by: Annotated[
        str,
        typer.Option("--sealed-by", help="Principal recorded in the manifest seal."),
    ] = "operator",
) -> None:
    """Plan, apply, recover, roll back or export the epoch-1 to epoch-2 cutover.

    ``--plan`` and ``--export`` write nothing: no canonical document, no
    registry, no staging tree. ``--apply`` is the only write that builds,
    and it refuses on any tree that has not declared itself a disposable
    canary, on any plan digest that is not the one the corpus now plans to,
    and on any unresolved row the operator has not named.

    ``--recover`` and ``--rollback`` repair a tree whose apply stopped part
    way: they read the restore point and the journal inside the tree rather
    than a corpus, so they take ``--target-root`` and no snapshot. A
    rollback asked for after the new generation has accepted a mutation
    refuses with ``rollback_boundary_crossed`` and writes nothing.
    """
    flags: GlobalFlags = ctx.obj
    try:
        mode = _epoch2_mode(
            plan=plan, apply_=apply_, export=export, recover=recover, rollback=rollback
        )
        payload = _epoch2_dispatch(
            mode=mode,
            snapshot_root=snapshot_root,
            allowlist=allowlist,
            workspace_key=workspace_key,
            project_key=project_key,
            repository_key=repository_key,
            sealed_by=sealed_by,
            plan_digest=plan_digest,
            target_root=target_root,
            registry_path=registry_path,
            manifest=manifest,
            accept_unresolved=accept_unresolved or [],
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    emit_json_or_text(payload, _epoch2_text(mode, payload), flags=flags)


def _epoch2_dispatch(
    *,
    mode: Epoch2Mode,
    snapshot_root: Path | None,
    allowlist: Path | None,
    workspace_key: str | None,
    project_key: str | None,
    repository_key: str | None,
    sealed_by: str,
    plan_digest: str | None,
    target_root: Path | None,
    registry_path: Path | None,
    manifest: Path | None,
    accept_unresolved: list[str],
) -> dict[str, Any]:
    """Assemble the request one mode needs and return its envelope.

    Args:
        mode: Which mode was selected.
        snapshot_root: The staging directory holding the epoch-1 corpus,
            required by every mode that reads one.
        allowlist: The allowed-legacy-symbol allowlist, required by the
            two modes that import.
        workspace_key: The addressing workspace.
        project_key: The addressing project.
        repository_key: The addressing repository.
        sealed_by: The principal recorded in the seal.
        plan_digest: The approved plan digest, required by ``--apply``.
        target_root: The tree the generation lands in, required by
            ``--apply`` and by both recovery modes.
        registry_path: The workspace registry, required by ``--apply``.
        manifest: The restore manifest the recovery writes back.
        accept_unresolved: Addresses of unresolved rows the apply accepts.

    Returns:
        The envelope the selected mode produced.

    Raises:
        UserError: A required option for the mode was omitted.
        ValidationError: An importer rule refused the corpus.
        CliError: The daemon answered with any other failure.
    """
    if mode not in _CORPUS_MODES:
        from eawf.kernel.migration.epoch2.recovery import (
            EPOCH2_RECOVER_METHOD,
            recover_cutover,
            recovery_envelope,
        )

        recover_request = _epoch2_recover_request(
            mode=mode,
            target_root=_required(target_root, option="--target-root", mode=mode),
            manifest=manifest,
        )
        return _epoch2_payload(
            method=EPOCH2_RECOVER_METHOD,
            params=recover_request.model_dump(mode="json"),
            local=lambda: recovery_envelope(
                recover_cutover(recover_request, recovered_at=datetime.now(UTC))
            ),
        )

    corpus = _required(snapshot_root, option="--snapshot-root", mode=mode)
    if mode is Epoch2Mode.EXPORT:
        export_request = Epoch2ExportRequest(snapshot_root=str(corpus))
        return _epoch2_payload(
            method=EPOCH2_EXPORT_METHOD,
            params=export_request.model_dump(mode="json"),
            local=lambda: export_epoch1(export_request),
        )

    plan_request = _epoch2_request(
        snapshot_root=corpus,
        allowlist=_required(allowlist, option="--allowlist", mode=mode),
        workspace_key=_required(workspace_key, option="--workspace-key", mode=mode),
        project_key=_required(project_key, option="--project-key", mode=mode),
        repository_key=_required(repository_key, option="--repository-key", mode=mode),
        sealed_by=sealed_by,
    )
    if mode is Epoch2Mode.PLAN:
        from eawf.kernel.migration.epoch2.plan_mode import (
            EPOCH2_PLAN_METHOD,
            plan_cutover,
            plan_envelope,
        )

        return _epoch2_payload(
            method=EPOCH2_PLAN_METHOD,
            params=plan_request.model_dump(mode="json"),
            local=lambda: plan_envelope(plan_cutover(plan_request, sealed_at=datetime.now(UTC))),
        )

    from eawf.kernel.migration.epoch2.apply import (
        EPOCH2_APPLY_METHOD,
        apply_cutover,
        apply_envelope,
    )

    apply_request = _epoch2_apply_request(
        plan_request=plan_request,
        target_root=_required(target_root, option="--target-root", mode=mode),
        registry_path=_required(registry_path, option="--registry-path", mode=mode),
        plan_digest=_required(plan_digest, option="--plan-digest", mode=mode),
        accept_unresolved=accept_unresolved,
    )
    return _epoch2_payload(
        method=EPOCH2_APPLY_METHOD,
        params=apply_request.model_dump(mode="json"),
        local=lambda: apply_envelope(apply_cutover(apply_request, applied_at=datetime.now(UTC))),
    )


def _epoch2_text(mode: Epoch2Mode, payload: Mapping[str, Any]) -> str:
    """Render one epoch-2 envelope for a terminal.

    Args:
        mode: Which mode produced the envelope.
        payload: The envelope.

    Returns:
        The rendered text.
    """
    if mode is Epoch2Mode.EXPORT:
        return export_text(dict(payload))
    if mode is Epoch2Mode.PLAN:
        return _epoch2_plan_text(payload)
    if mode is Epoch2Mode.APPLY:
        return _epoch2_apply_text(payload)
    return _epoch2_recovery_text(payload)


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


def _epoch2_apply_text(payload: Mapping[str, Any]) -> str:
    """Render one apply envelope for a terminal.

    Args:
        payload: The apply envelope.

    Returns:
        One header line naming the generation, then the digests and the
        rollback boundary the cutover reached.
    """
    verb = "applied" if payload["applied"] else "already selected"
    return "\n".join(
        [
            f"epoch2 apply: {verb} generation {payload['generation_id']} "
            f"over {payload['target_rows']} target rows",
            f"  manifest digest:     {payload['manifest_digest']}",
            f"  approval digest:     {payload['approval_digest']}",
            f"  rollback boundary:   {payload['rollback_boundary']}",
            f"  journal rows:        {payload['journal_rows']}",
            f"  generations on disk: {payload['generation_count']}",
            f"  accepted unresolved: {len(payload['accepted_unresolved_rows'])}",
        ]
    )


def _epoch2_recovery_text(payload: Mapping[str, Any]) -> str:
    """Render one recovery envelope for a terminal.

    Args:
        payload: The recovery envelope.

    Returns:
        One header line naming what was done, then the boundary it was done
        from and the single authority the tree reads from afterwards.
    """
    authority = payload["authority"]
    generation = authority["generation_id"] or "none"
    return "\n".join(
        [
            f"epoch2 {payload['action']}: {payload['outcome'].replace('_', ' ')} "
            f"from the {payload['boundary'].replace('_', ' ')} boundary",
            f"  reads from:          epoch {authority['epoch']} / {generation}",
            f"  generations on disk: {authority['generation_count']}",
            f"  surfaces restored:   {len(payload['restored_locators'])}",
            f"  discarded:           {len(payload['discarded'])}",
            f"  journal rows:        {payload['journal_rows']}",
        ]
    )
