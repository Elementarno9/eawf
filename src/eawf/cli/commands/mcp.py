"""``eawf mcp ...`` Typer commands.

Surface (Phase 5 W04):

- ``add <id>`` — register an Eä-owned MCP entry in
  ``state.mcp_servers``.
- ``install <id>`` — write the entry into the runtime config
  (Claude only in v0.1). Prompts unless ``--no-input`` is set.
- ``update <id>`` — patch an existing Eä-owned entry. Warns when
  re-install is required.
- ``remove <id>`` — delete from state and (unless
  ``--keep-runtime-entry``) from the runtime config.
- ``list`` — read-only enumeration with owner annotation.

Discipline checklist:

- Every mutator uses :func:`state_transaction`. Direct ``state.json``
  writes from this file would violate AGENTS.md rule 4.
- Env-ref tokens stay literal end-to-end (rule 16). The installer
  never reads ``os.environ`` for env-ref names.
- User-owned ``mcpServers[*]`` entries in settings.json are byte-equal
  across the whole add/install/update/remove sequence (verified by
  ``tests/integration/test_mcp_install_existing_user_entry.py``).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast

import typer
from pydantic import ValidationError

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.lifecycle.allocator import allocate_grant_id
from eawf.mcp.installer import (
    InstallEntryResult,
    IntegrityViolation,
    RemoveEntryResult,
    install_runtime_entry,
    list_runtime_entries,
    remove_runtime_entry,
)
from eawf.state.enums import McpRisk, McpStatus
from eawf.state.models import (
    GRANT_SCOPE_KINDS,
    McpGrant,
    McpGrantScopeKind,
    McpServer,
)

logger = logging.getLogger(__name__)


mcp_app = typer.Typer(
    name="mcp",
    help="Manage MCP server entries (add/install/update/remove/list).",
    no_args_is_help=True,
)


_SUPPORTED_RUNTIMES: tuple[str, ...] = ("claude",)
_OWNER_FILTERS: tuple[str, ...] = ("eawf", "user", "all")


def _escape_tsv_field(value: str) -> str:
    """Escape ``\\t`` and ``\\n`` so a TSV row stays single-line.

    Without this a command field like ``"sh -c 'echo a\\nb'"`` shears
    when piped into ``cut -f`` because the embedded newline ends the
    record. We escape the backslash too so a real ``"\\\\n"`` doesn't
    round-trip back into a newline.
    """
    return value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def _resolve_target(flags: GlobalFlags) -> Path:
    """Resolve the workspace root the mcp commands operate against."""
    return (flags.workspace or Path.cwd()).resolve()


def _validate_runtime(runtime: str) -> None:
    if runtime not in _SUPPORTED_RUNTIMES:
        raise cli_errors.InvalidInput(
            f"unknown runtime {runtime!r}; expected one of {list(_SUPPORTED_RUNTIMES)}"
        )


def _resolve_risk(raw: str) -> McpRisk:
    try:
        return McpRisk(raw)
    except ValueError as exc:
        raise cli_errors.InvalidInput(
            f"--risk must be one of {[r.value for r in McpRisk]}; got {raw!r}"
        ) from exc


def _server_payload(server: McpServer) -> dict[str, object]:
    """Render *server* as a JSON-friendly dict for envelopes."""
    return {
        "id": server.id,
        "owner": server.owner,
        "command": server.command,
        "args": list(server.args),
        "env_refs": list(server.env_refs),
        "risk": server.risk.value,
        "write_capable": server.write_capable,
        "status": server.status.value,
        "installed_targets": list(server.installed_targets),
    }


def _grant_payload(grant: McpGrant) -> dict[str, object]:
    """Render *grant* as a JSON-friendly dict for envelopes."""
    return {
        "id": grant.id,
        "scope_kind": grant.scope_kind,
        "scope_id": grant.scope_id,
        "server_id": grant.server_id,
        "granted_at": grant.granted_at.isoformat(),
    }


def _confirm_install(
    *,
    server: McpServer,
    runtime: str,
    settings_path: Path,
    no_input: bool,
) -> None:
    """Implement the ask-before-install gate.

    Behaviour:

    - ``--no-input`` (``flags.no_input is True``) → skip prompt and
      proceed. The user opted in to non-interactive policy.
    - stdin is **not** a TTY → fail closed with
      :class:`UserDeclined`. We refuse to silently proceed — the
      caller must explicitly opt in via ``--no-input``.
    - Otherwise prompt; a "no" answer raises :class:`UserDeclined`.

    The text mirrors a security checklist: command, env-refs,
    runtime, target path. No env-ref *values* are emitted (and we
    never have them — the installer is on the literal-token side
    of the env barrier).
    """
    if no_input:
        return
    if not sys.stdin.isatty():
        raise cli_errors.UserDeclined(
            "stdin is not a TTY and --no-input was not passed; refusing to "
            "install MCP server without confirmation"
        )
    env_refs_repr = ", ".join(server.env_refs) if server.env_refs else "(none)"
    prompt = (
        f"Install MCP server {server.id} (command={server.command}, "
        f"env_refs={env_refs_repr}) into {runtime} at {settings_path}? [y/N] "
    )
    answer = input(prompt).strip().lower()
    if answer not in {"y", "yes"}:
        raise cli_errors.UserDeclined(f"user declined install of {server.id!r}")


@mcp_app.command(name="add")
def add_cmd(
    ctx: typer.Context,
    server_id: Annotated[
        str,
        typer.Argument(help="MCP server identifier (state.mcp_servers map key).", metavar="ID"),
    ],
    command: Annotated[
        str,
        typer.Option("--command", help="argv[0] for the MCP launcher."),
    ],
    arg: Annotated[
        list[str] | None,
        typer.Option(
            "--arg",
            help="argv[1:] in declared order; pass once per argument.",
        ),
    ] = None,
    env_ref: Annotated[
        list[str] | None,
        typer.Option(
            "--env-ref",
            help='Literal env-ref token, e.g. "${ENV:OPENAI_KEY}"; never expanded.',
        ),
    ] = None,
    risk: Annotated[
        str,
        typer.Option("--risk", help="One of read | read-write | admin."),
    ] = "read",
    write_capable: Annotated[
        bool,
        typer.Option(
            "--write-capable/--no-write-capable",
            help="Marks the server as capable of mutating user state.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing owner=eawf entry with the same id.",
        ),
    ] = False,
) -> None:
    """Register a new Eä-owned MCP entry in ``state.mcp_servers``."""
    flags: GlobalFlags = ctx.obj
    try:
        risk_enum = _resolve_risk(risk)
        state_path = resolve_state_path(flags.workspace)
        with state_transaction(state_path) as state:
            servers = state.mcp_servers if state.mcp_servers is not None else {}
            existing = servers.get(server_id)
            if existing is not None and existing.owner != "eawf":
                raise cli_errors.InvalidInput(
                    f"mcp id {server_id!r} exists with owner={existing.owner!r}; "
                    "refusing to overwrite"
                )
            if existing is not None and not force:
                raise cli_errors.InvalidInput(
                    f"mcp id {server_id!r} exists; pass --force to redefine"
                )
            try:
                server = McpServer(
                    id=server_id,
                    owner="eawf",
                    command=command,
                    args=list(arg or []),
                    env_refs=list(env_ref or []),
                    risk=risk_enum,
                    write_capable=write_capable,
                    status=McpStatus.CONFIGURED,
                    installed_targets=[],
                )
            except ValidationError as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc
            servers[server_id] = server
            state.mcp_servers = servers
            state.updated_at = datetime.now(UTC)
        emit_json_or_text(
            payload=_server_payload(server),
            text=(f"mcp added: {server.id} (owner=eawf, command={server.command})"),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@mcp_app.command(name="install")
def install_cmd(
    ctx: typer.Context,
    server_id: Annotated[str, typer.Argument(help="MCP id to install.", metavar="ID")],
    runtime: Annotated[
        str,
        typer.Option("--runtime", help="Target runtime; only `claude` in v0.1."),
    ] = "claude",
    target_dir: Annotated[
        Path | None,
        typer.Option("--target-dir", help="Workspace root for the runtime config."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite a pre-existing user-owned entry under the same id.",
        ),
    ] = False,
) -> None:
    """Materialise an Eä-owned MCP entry into the runtime config."""
    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
        state_path = resolve_state_path(flags.workspace)
        target = (target_dir or _resolve_target(flags)).resolve()
        timestamp = datetime.now(UTC).isoformat()
        result: InstallEntryResult | None = None
        with state_transaction(state_path) as state:
            servers = state.mcp_servers or {}
            server = servers.get(server_id)
            if server is None:
                raise cli_errors.NotFound(
                    f"mcp id {server_id!r} not registered; run `eawf mcp add` first"
                )
            if server.owner != "eawf":
                raise cli_errors.InvalidInput(
                    f"mcp id {server_id!r} is owner={server.owner!r}; "
                    "install only manages owner=eawf entries"
                )
            settings_path = (target / ".claude" / "settings.json").resolve()
            _confirm_install(
                server=server,
                runtime=runtime,
                settings_path=settings_path,
                no_input=flags.no_input,
            )
            try:
                result = install_runtime_entry(
                    server=server,
                    runtime=runtime,
                    target_dir=target,
                    force=force,
                    timestamp=timestamp,
                )
            except IntegrityViolation as exc:
                raise cli_errors.IntegrityViolation(str(exc)) from exc
            except ValueError as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc

            updated = server.model_copy(
                update={
                    "status": McpStatus.INSTALLED,
                    "installed_targets": (
                        [*server.installed_targets, runtime]
                        if runtime not in server.installed_targets
                        else list(server.installed_targets)
                    ),
                }
            )
            servers[server_id] = updated
            state.mcp_servers = servers
            state.updated_at = datetime.now(UTC)
        assert result is not None
        emit_json_or_text(
            payload={
                "id": server_id,
                "runtime": runtime,
                "target_path": str(result.target_path),
                "action": "installed",
                "fs_action": result.action,
                "user_entries_preserved": result.user_entries_preserved,
                "status": McpStatus.INSTALLED.value,
            },
            text=(
                f"mcp installed: {server_id} → {result.target_path} "
                f"({result.action}; preserved {len(result.user_entries_preserved)} user entries)"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@mcp_app.command(name="update")
def update_cmd(
    ctx: typer.Context,
    server_id: Annotated[str, typer.Argument(help="MCP id to update.", metavar="ID")],
    command: Annotated[
        str | None,
        typer.Option("--command", help="Replace argv[0] for the MCP launcher."),
    ] = None,
    arg: Annotated[
        list[str] | None,
        typer.Option("--arg", help="Replace the full argv[1:] list when provided."),
    ] = None,
    env_ref: Annotated[
        list[str] | None,
        typer.Option(
            "--env-ref",
            help="Replace the full env-ref list when provided.",
        ),
    ] = None,
    risk: Annotated[
        str | None,
        typer.Option("--risk", help="Replace the risk classification."),
    ] = None,
    write_capable: Annotated[
        bool | None,
        typer.Option(
            "--write-capable/--no-write-capable",
            help="Toggle the write-capable flag.",
        ),
    ] = None,
) -> None:
    """Patch an existing Eä-owned MCP entry in ``state.mcp_servers``."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        risk_enum = _resolve_risk(risk) if risk is not None else None
        with state_transaction(state_path) as state:
            servers = state.mcp_servers or {}
            server = servers.get(server_id)
            if server is None:
                raise cli_errors.NotFound(f"mcp id {server_id!r} not registered; nothing to update")
            if server.owner != "eawf":
                raise cli_errors.InvalidInput(
                    f"mcp id {server_id!r} is owner={server.owner!r}; "
                    "update only manages owner=eawf entries"
                )
            updates: dict[str, object] = {}
            if command is not None:
                updates["command"] = command
            if arg is not None:
                updates["args"] = list(arg)
            if env_ref is not None:
                updates["env_refs"] = list(env_ref)
            if risk_enum is not None:
                updates["risk"] = risk_enum
            if write_capable is not None:
                updates["write_capable"] = write_capable
            if not updates:
                raise cli_errors.InvalidInput(
                    "update requires at least one of --command, --arg, --env-ref, "
                    "--risk, --write-capable"
                )
            try:
                updated = server.model_copy(update=updates)
                # model_copy bypasses validators; round-trip through
                # model_validate so a malformed env-ref gets caught
                # before the transaction commits.
                updated = McpServer.model_validate(updated.model_dump())
            except ValidationError as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc
            servers[server_id] = updated
            state.mcp_servers = servers
            state.updated_at = datetime.now(UTC)
            installed_targets = list(updated.installed_targets)

        reinstall_required = bool(installed_targets)
        text_lines = [f"mcp updated: {server_id}"]
        if reinstall_required:
            text_lines.append(
                f"note: run `eawf mcp install {server_id}` to apply the change to "
                f"{', '.join(installed_targets)}"
            )
        payload = _server_payload(updated)
        payload["reinstall_required"] = reinstall_required
        emit_json_or_text(payload=payload, text="\n".join(text_lines), flags=flags)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@mcp_app.command(name="remove")
def remove_cmd(
    ctx: typer.Context,
    server_id: Annotated[str, typer.Argument(help="MCP id to remove.", metavar="ID")],
    runtime: Annotated[
        str | None,
        typer.Option(
            "--runtime",
            help="Restrict to one runtime; defaults to all installed_targets.",
        ),
    ] = None,
    target_dir: Annotated[
        Path | None,
        typer.Option("--target-dir", help="Workspace root for the runtime config."),
    ] = None,
    keep_runtime_entry: Annotated[
        bool,
        typer.Option(
            "--keep-runtime-entry",
            help="Drop only the state row; leave the runtime config unchanged.",
        ),
    ] = False,
) -> None:
    """Delete an Eä-owned MCP entry from state (and optionally runtime configs)."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        target = (target_dir or _resolve_target(flags)).resolve()
        if runtime is not None:
            _validate_runtime(runtime)
        runtime_results: list[RemoveEntryResult] = []
        with state_transaction(state_path) as state:
            servers = state.mcp_servers or {}
            server = servers.get(server_id)
            if server is None:
                raise cli_errors.NotFound(f"mcp id {server_id!r} not registered; nothing to remove")
            if server.owner != "eawf":
                raise cli_errors.InvalidInput(
                    f"mcp id {server_id!r} is owner={server.owner!r}; "
                    "remove only manages owner=eawf entries"
                )
            if not keep_runtime_entry:
                targets = [runtime] if runtime is not None else list(server.installed_targets)
                for rt in targets:
                    try:
                        result = remove_runtime_entry(
                            server_id=server_id,
                            runtime=rt,
                            target_dir=target,
                            force=False,
                        )
                    except IntegrityViolation as exc:
                        raise cli_errors.IntegrityViolation(str(exc)) from exc
                    except ValueError as exc:
                        raise cli_errors.InvalidInput(str(exc)) from exc
                    runtime_results.append(result)
            del servers[server_id]
            state.mcp_servers = servers if servers else None
            state.updated_at = datetime.now(UTC)

        emit_json_or_text(
            payload={
                "id": server_id,
                "removed_from_state": True,
                "runtime_actions": [
                    {
                        "target_path": str(r.target_path),
                        "action": r.action,
                        "user_entries_preserved": r.user_entries_preserved,
                    }
                    for r in runtime_results
                ],
                "kept_runtime_entry": keep_runtime_entry,
            },
            text=(
                f"mcp removed: {server_id} "
                f"(runtime updates={len(runtime_results)}; kept_runtime={keep_runtime_entry})"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@mcp_app.command(name="list")
def list_cmd(
    ctx: typer.Context,
    owner: Annotated[
        str,
        typer.Option(
            "--owner",
            help="Filter by ownership: eawf | user | all.",
        ),
    ] = "eawf",
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Runtime to inspect for user entries; only `claude` in v0.1.",
        ),
    ] = "claude",
    target_dir: Annotated[
        Path | None,
        typer.Option("--target-dir", help="Workspace root for the runtime config."),
    ] = None,
) -> None:
    """List MCP entries from state and/or runtime config."""
    flags: GlobalFlags = ctx.obj
    try:
        if owner not in _OWNER_FILTERS:
            raise cli_errors.InvalidInput(
                f"--owner must be one of {list(_OWNER_FILTERS)}; got {owner!r}"
            )
        _validate_runtime(runtime)
        target = (target_dir or _resolve_target(flags)).resolve()

        rows: list[dict[str, object]] = []
        notes: list[str] = []

        if owner in {"eawf", "all"}:
            try:
                state_path = resolve_state_path(flags.workspace)
                if state_path.exists():
                    payload = json.loads(state_path.read_text(encoding="utf-8"))
                    state_servers = payload.get("mcp_servers") or {}
                    for sid, body in sorted(state_servers.items()):
                        if not isinstance(body, dict):
                            continue
                        if body.get("owner") != "eawf":
                            continue
                        rows.append(
                            {
                                "id": sid,
                                "owner": "eawf",
                                "command": body.get("command", ""),
                                "risk": body.get("risk", ""),
                                "status": body.get("status", ""),
                                "installed_targets": list(body.get("installed_targets", [])),
                            }
                        )
            except FileNotFoundError:
                # No state.json yet — treat as empty for the list.
                pass

        if owner in {"user", "all"}:
            try:
                runtime_rows = list_runtime_entries(runtime=runtime, target_dir=target)
            except ValueError as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc
            settings_path = target / ".claude" / "settings.json"
            if not settings_path.exists():
                notes.append(f"runtime config absent at {settings_path}")
            for row in runtime_rows:
                if row.owner != "user":
                    continue
                rows.append(
                    {
                        "id": row.id,
                        "owner": "user",
                        "command": row.command,
                        "risk": "",
                        "status": "",
                        "installed_targets": [runtime],
                    }
                )

        text_lines: list[str] = []
        if rows:
            text_lines.append("ID\tOWNER\tCOMMAND\tRISK\tSTATUS\tTARGETS")
            for entry in rows:
                targets_raw = entry.get("installed_targets", [])
                targets = list(targets_raw) if isinstance(targets_raw, (list, tuple)) else []
                command_field = _escape_tsv_field(str(entry.get("command", "")))
                text_lines.append(
                    f"{entry['id']}\t{entry['owner']}\t{command_field}\t{entry['risk']}"
                    f"\t{entry['status']}\t{','.join(str(t) for t in targets)}"
                )
        else:
            text_lines.append("(no entries)")
        text_lines.extend(notes)
        emit_json_or_text(
            payload={"servers": rows, "count": len(rows), "notes": notes},
            text="\n".join(text_lines),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


@mcp_app.command(name="grant")
def grant_cmd(
    ctx: typer.Context,
    scope_kind: Annotated[
        str,
        typer.Argument(
            help="Scope shape: wave | profile | global.",
            metavar="SCOPE_KIND",
        ),
    ],
    scope_id: Annotated[
        str,
        typer.Argument(
            help=(
                "Scope identifier (e.g. wave id `P10-I01-W04`, profile name, or "
                "the literal `global`)."
            ),
            metavar="SCOPE_ID",
        ),
    ],
    server_id: Annotated[
        str,
        typer.Argument(help="MCP server id from state.mcp_servers.", metavar="SERVER_ID"),
    ],
    grant_id: Annotated[
        str | None,
        typer.Option(
            "--grant-id",
            help=(
                "Override the auto-generated grant id (default: `GRANT-<n>` "
                "with n = max existing + 1)."
            ),
        ),
    ] = None,
) -> None:
    """Bind an MCP server to a scope so dispatch can project allowed-tools.

    The grant body is persisted under ``state.mcp_grants[grant_id]``;
    ``state.updated_at`` is bumped by :func:`state_transaction`.

    The transaction validates referential integrity after mutation: if
    *server_id* is not registered in ``state.mcp_servers``, the
    ``INV.REF.MCP_GRANT_SERVER_MISSING`` invariant fires and the write is
    rolled back as :class:`ValidationFailed`.
    """
    flags: GlobalFlags = ctx.obj
    try:
        if scope_kind not in GRANT_SCOPE_KINDS:
            raise cli_errors.InvalidInput(
                f"scope_kind must be one of {list(GRANT_SCOPE_KINDS)}; got {scope_kind!r}"
            )
        scope_kind_narrowed = cast(McpGrantScopeKind, scope_kind)
        state_path = resolve_state_path(flags.workspace)
        with state_transaction(state_path) as state:
            grants = state.mcp_grants if state.mcp_grants is not None else {}
            resolved_grant_id = grant_id if grant_id is not None else allocate_grant_id(state)
            if resolved_grant_id in grants:
                raise cli_errors.InvalidInput(
                    f"mcp grant id {resolved_grant_id!r} already exists; "
                    "pick another or run `eawf mcp revoke` first"
                )
            try:
                grant = McpGrant(
                    id=resolved_grant_id,
                    scope_kind=scope_kind_narrowed,
                    scope_id=scope_id,
                    server_id=server_id,
                    granted_at=datetime.now(UTC),
                )
            except ValidationError as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc
            grants[resolved_grant_id] = grant
            state.mcp_grants = grants
            state.updated_at = datetime.now(UTC)
        emit_json_or_text(
            payload=_grant_payload(grant),
            text=(
                f"mcp granted: {grant.id} ({grant.scope_kind}={grant.scope_id} → {grant.server_id})"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@mcp_app.command(name="revoke")
def revoke_cmd(
    ctx: typer.Context,
    grant_id: Annotated[
        str,
        typer.Argument(help="Grant id from state.mcp_grants.", metavar="GRANT_ID"),
    ],
) -> None:
    """Remove an MCP grant from ``state.mcp_grants``.

    Bumps ``state.updated_at`` and clears the map slot. When the last
    grant is removed, ``mcp_grants`` is reset to ``None`` so the
    nullable-vs-empty distinction stays parallel to the ``mcp_servers``
    handling in :func:`remove_cmd`.
    """
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        removed: McpGrant | None = None
        with state_transaction(state_path) as state:
            grants = state.mcp_grants or {}
            grant = grants.get(grant_id)
            if grant is None:
                raise cli_errors.NotFound(
                    f"mcp grant id {grant_id!r} not registered; nothing to revoke"
                )
            del grants[grant_id]
            state.mcp_grants = grants if grants else None
            state.updated_at = datetime.now(UTC)
            removed = grant
        assert removed is not None
        emit_json_or_text(
            payload={
                "id": removed.id,
                "removed_from_state": True,
                "scope_kind": removed.scope_kind,
                "scope_id": removed.scope_id,
                "server_id": removed.server_id,
            },
            text=(
                f"mcp revoked: {removed.id} "
                f"({removed.scope_kind}={removed.scope_id} → {removed.server_id})"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


__all__ = ["mcp_app"]
