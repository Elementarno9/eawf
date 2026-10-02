"""The ``eawf mcp`` registry reads and grants: ``list``, ``grant`` and ``revoke``.

Split out of :mod:`eawf.surfaces.cli.commands.mcp`, whose :data:`mcp_app`
and helpers these commands attach to. ``grant`` and ``revoke`` send one
native ``mcp.*`` verb each; ``list`` reads the selected generation, or the
frozen document of a tree still in epoch 1.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.mcp import (
    _OWNER_FILTERS,
    MCP_GRANT,
    MCP_REVOKE,
    ExpectedRevision,
    IdempotencyKey,
    _escape_tsv_field,
    _mcp_rpc,
    _resolve_target,
    _server_payload,
    _validate_runtime,
    mcp_app,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)


def _registry_rows(flags: GlobalFlags) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Return the Eä-owned server rows and the grant rows the tree holds, as envelope dicts."""
    from eawf.runtime.mcp.book import owned_registry

    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError:
        return [], []
    servers, grants = owned_registry(state_path)
    return (
        [_server_payload(server, revision) for server, revision in servers],
        [{**grant.model_dump(mode="json"), "revision": revision} for grant, revision in grants],
    )


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
            help="Runtime to inspect for user entries: claude | codex.",
        ),
    ] = "claude",
    target_dir: Annotated[
        Path | None,
        typer.Option("--target-dir", help="Workspace root for the runtime config."),
    ] = None,
) -> None:
    """List MCP entries from the registry and/or the runtime config."""
    from eawf.runtime.mcp.installer import list_runtime_entries, runtime_config_path

    flags: GlobalFlags = ctx.obj
    try:
        if owner not in _OWNER_FILTERS:
            raise cli_errors.UserError(
                f"--owner must be one of {list(_OWNER_FILTERS)}; got {owner!r}", kind="InvalidInput"
            )
        _validate_runtime(runtime)
        target = (target_dir or _resolve_target(flags)).resolve()

        rows: list[dict[str, object]] = []
        grants: list[dict[str, object]] = []
        notes: list[str] = []

        if owner in {"eawf", "all"}:
            rows, grants = _registry_rows(flags)

        if owner in {"user", "all"}:
            try:
                runtime_rows = list_runtime_entries(runtime=runtime, target_dir=target)
            except ValueError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            settings_path = runtime_config_path(runtime, target)
            if not settings_path.exists():
                notes.append(f"runtime config absent at {settings_path}")
            rows.extend(
                {
                    "id": row.id,
                    "owner": "user",
                    "command": row.command,
                    "risk": "",
                    "status": "",
                    "installed_targets": [runtime],
                    "revision": None,
                }
                for row in runtime_rows
                if row.owner == "user"
            )

        text_lines: list[str] = []
        if rows:
            text_lines.append("ID\tOWNER\tCOMMAND\tRISK\tSTATUS\tTARGETS\tREVISION")
            for entry in rows:
                targets_raw = entry.get("installed_targets", [])
                targets = list(targets_raw) if isinstance(targets_raw, (list, tuple)) else []
                command_field = _escape_tsv_field(str(entry.get("command", "")))
                revision = entry.get("revision")
                text_lines.append(
                    f"{entry['id']}\t{entry['owner']}\t{command_field}\t{entry.get('risk', '')}"
                    f"\t{entry.get('status', '')}\t{','.join(str(t) for t in targets)}"
                    f"\t{'' if revision is None else revision}"
                )
        else:
            text_lines.append("(no entries)")
        text_lines.extend(notes)
        emit_json_or_text(
            payload={"servers": rows, "count": len(rows), "grants": grants, "notes": notes},
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
        typer.Argument(help="Registered MCP server id.", metavar="SERVER_ID"),
    ],
    idempotency_key: IdempotencyKey,
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

    The daemon refuses a grant naming a server that is not registered, so
    no grant ever authorises a tool nobody registered.
    """
    from eawf.kernel.state.models import GRANT_SCOPE_KINDS

    flags: GlobalFlags = ctx.obj
    try:
        if scope_kind not in GRANT_SCOPE_KINDS:
            raise cli_errors.UserError(
                f"unknown scope_kind {scope_kind!r}; expected one of {list(GRANT_SCOPE_KINDS)}",
                kind="InvalidInput",
            )
        answer = _mcp_rpc(
            MCP_GRANT,
            {
                "scope_kind": scope_kind,
                "scope_id": scope_id,
                "server_id": server_id,
                "grant_id": grant_id,
                "idempotency_key": idempotency_key,
            },
            flags=flags,
            verb_text="mcp grant",
        )
        grant = answer["grant"]
        assert isinstance(grant, dict)
        emit_json_or_text(
            payload={**grant, "revision": answer["revision"]},
            text=(
                f"mcp granted: {grant['id']} "
                f"({grant['scope_kind']}={grant['scope_id']} → {grant['server_id']})"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


@mcp_app.command(name="revoke")
def revoke_cmd(
    ctx: typer.Context,
    grant_id: Annotated[
        str,
        typer.Argument(help="Grant id to revoke.", metavar="GRANT_ID"),
    ],
    expected_revision: ExpectedRevision,
    idempotency_key: IdempotencyKey,
) -> None:
    """Revoke an MCP grant; its row stays under its id so the id is never reused."""
    flags: GlobalFlags = ctx.obj
    try:
        answer = _mcp_rpc(
            MCP_REVOKE,
            {
                "grant_id": grant_id,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
            flags=flags,
            verb_text="mcp revoke",
        )
        removed = answer["grant"]
        assert isinstance(removed, dict)
        emit_json_or_text(
            payload={
                "id": removed["id"],
                "removed_from_state": True,
                "scope_kind": removed["scope_kind"],
                "scope_id": removed["scope_id"],
                "server_id": removed["server_id"],
                "revision": answer["revision"],
            },
            text=(
                f"mcp revoked: {removed['id']} "
                f"({removed['scope_kind']}={removed['scope_id']} → {removed['server_id']})"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
