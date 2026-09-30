"""``mcp.*`` native verbs: register MCP servers and grant them to scopes.

A server is a ``capability`` row and a grant a ``tool_authority`` row of
the selected generation's document. Every verb reads the rows under the
project's lock, decides the one row it writes, and commits it through the
row-write transaction, so each change is one WAL intent, one document
write and one firehose row. A verb that changes an existing row names the
revision it read; a stale one is refused before anything is written.

Writing the host's own MCP configuration stays with the caller: it is a
file the host reads, not a record of the tree, and the caller writes it
before recording the install or removal it reports.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.state.enums import McpRisk, McpStatus
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import StrictPositiveInt
from eawf.kernel.state.models import McpGrant, McpGrantScopeKind, McpServer
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.epoch2_root import Epoch2RootContext, RootSession
from eawf.runtime.daemon.epoch2_transaction import commit_row_write
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery import keyed_call
from eawf.runtime.daemon.methods.memory import project_subject
from eawf.runtime.daemon.native_guard import native_mutator, native_params
from eawf.runtime.mcp.book import (
    StandingGrant,
    StandingServer,
    capability_row,
    next_grant_id,
    read_grants,
    read_servers,
    tool_authority_row,
)

logger = logging.getLogger(__name__)

MCP_ADD_METHOD: Final = "mcp.add"
MCP_INSTALL_METHOD: Final = "mcp.install"
MCP_UPDATE_METHOD: Final = "mcp.update"
MCP_REMOVE_METHOD: Final = "mcp.remove"
MCP_GRANT_METHOD: Final = "mcp.grant"
MCP_REVOKE_METHOD: Final = "mcp.revoke"

#: The owner every server the verbs register carries.
EAWF_OWNER: Final = "eawf"

_Text = Annotated[str, Field(min_length=1)]
_Key = Annotated[str, Field(min_length=1, max_length=128)]
_EnvRef = Annotated[str, Field(pattern=r"^\$\{ENV:[A-Z_][A-Z0-9_]*\}$")]
McpRuntime = Literal["claude", "codex", "opencode"]


class McpAdd(BaseModel):
    """What ``mcp.add`` is asked for: a server to register, or with ``force`` redefine."""

    model_config = ConfigDict(extra="forbid")

    id: _Text
    command: _Text
    args: tuple[str, ...] = ()
    env_refs: tuple[_EnvRef, ...] = ()
    risk: McpRisk = McpRisk.READ
    write_capable: bool = False
    force: bool = False
    idempotency_key: _Key


class McpInstall(BaseModel):
    """What ``mcp.install`` is asked for: the runtime a server was written into."""

    model_config = ConfigDict(extra="forbid")

    id: _Text
    runtime: McpRuntime
    expected_revision: StrictPositiveInt
    idempotency_key: _Key


class McpUpdate(BaseModel):
    """What ``mcp.update`` is asked for: the fields to replace, at least one."""

    model_config = ConfigDict(extra="forbid")

    id: _Text
    command: _Text | None = None
    args: tuple[str, ...] | None = None
    env_refs: tuple[_EnvRef, ...] | None = None
    risk: McpRisk | None = None
    write_capable: bool | None = None
    expected_revision: StrictPositiveInt
    idempotency_key: _Key


class McpRemove(BaseModel):
    """What ``mcp.remove`` is asked for."""

    model_config = ConfigDict(extra="forbid")

    id: _Text
    expected_revision: StrictPositiveInt
    idempotency_key: _Key


class McpGrantParams(BaseModel):
    """What ``mcp.grant`` is asked for; ``grant_id`` is allocated when omitted."""

    model_config = ConfigDict(extra="forbid")

    scope_kind: McpGrantScopeKind
    scope_id: _Text
    server_id: _Text
    grant_id: _Text | None = None
    idempotency_key: _Key


class McpRevoke(BaseModel):
    """What ``mcp.revoke`` is asked for."""

    model_config = ConfigDict(extra="forbid")

    grant_id: _Text
    expected_revision: StrictPositiveInt
    idempotency_key: _Key


class McpServerAnswer(BaseModel):
    """A server as one verb left it, with the revision its row now stands at."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server: McpServer
    revision: int
    removed: bool = False


class McpGrantAnswer(BaseModel):
    """A grant as one verb left it, with the revision its row now stands at."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant: McpGrant
    revision: int
    revoked: bool = False


def _refusal(code: str, message: str) -> DaemonValidationError:
    """Return the validation refusal the CLI routes on by *code*."""
    return DaemonValidationError(f"validation_failed: {code}: {message}")


def _standing_server(session: RootSession, server_id: str) -> tuple[StandingServer | None, int]:
    """Return the row *server_id* names, if any, beside the revision it stands at."""
    standing = read_servers(session.read_document()).get(server_id)
    return standing, 0 if standing is None else standing.revision


def _anchored_server(session: RootSession, server_id: str, expected: int) -> tuple[McpServer, int]:
    """Return the registered server *server_id* names, at the revision the caller read.

    Raises:
        DaemonValidationError: No server stands under the id, or its row
            stands at another revision.
    """
    standing, revision = _standing_server(session, server_id)
    if standing is None or standing.server is None:
        raise _refusal("mcp_not_found", f"mcp id {server_id!r} is not registered")
    if revision != expected:
        raise _refusal(
            "revision_conflict",
            f"mcp id {server_id!r} is at revision {revision} but the request expects {expected}",
        )
    return standing.server, revision


def _write_server(
    session: RootSession,
    server: McpServer,
    *,
    revision: int,
    removed: bool,
    event: str,
    now: datetime,
) -> McpServerAnswer:
    """Commit *server*'s row at *revision* and return the answer describing it."""
    commit_row_write(
        session,
        collection=Epoch2Collection.CAPABILITY,
        record_key=server.id,
        row=capability_row(
            server, status="removed" if removed else "registered", revision=revision, at=now
        ),
        event_name=event,
        event_fields={"revision": revision},
        compaction=None,
        now=now,
    )
    return McpServerAnswer(server=server, revision=revision, removed=removed)


def _validated(fields: dict[str, Any]) -> McpServer:
    """Return the server *fields* describe.

    Raises:
        DaemonValidationError: The fields do not make a valid server.
    """
    try:
        return McpServer.model_validate(fields)
    except ValidationError as error:
        paths = sorted({".".join(str(part) for part in row["loc"]) for row in error.errors()})
        raise _refusal("schema_validation_failed", f"check {', '.join(paths)}") from error


def add_server(context: Epoch2RootContext, args: McpAdd, *, now: datetime) -> McpServerAnswer:
    """Register a server, or redefine a registered one when ``force`` is set.

    Raises:
        DaemonValidationError: The id is registered and ``force`` is not set.
    """
    with context.session([project_subject(context)]) as session:
        standing, revision = _standing_server(session, args.id)
        if standing is not None and standing.server is not None and not args.force:
            raise _refusal("mcp_exists", f"mcp id {args.id!r} exists; pass --force to redefine")
        server = _validated(
            {
                "id": args.id,
                "owner": EAWF_OWNER,
                "command": args.command,
                "args": list(args.args),
                "env_refs": list(args.env_refs),
                "risk": args.risk,
                "write_capable": args.write_capable,
                "status": McpStatus.CONFIGURED,
                "installed_targets": [],
            }
        )
        answer = _write_server(
            session, server, revision=revision + 1, removed=False, event=MCP_ADD_METHOD, now=now
        )
    logger.info(f"add_server id={args.id} revision={answer.revision}")
    return answer


def install_server(
    context: Epoch2RootContext, args: McpInstall, *, now: datetime
) -> McpServerAnswer:
    """Record that a server was written into one runtime's configuration."""
    with context.session([project_subject(context)]) as session:
        server, revision = _anchored_server(session, args.id, args.expected_revision)
        targets = list(server.installed_targets)
        if args.runtime not in targets:
            targets.append(args.runtime)
        installed = server.model_copy(
            update={"status": McpStatus.INSTALLED, "installed_targets": targets}
        )
        answer = _write_server(
            session,
            installed,
            revision=revision + 1,
            removed=False,
            event=MCP_INSTALL_METHOD,
            now=now,
        )
    logger.info(f"install_server id={args.id} runtime={args.runtime} revision={answer.revision}")
    return answer


def update_server(context: Epoch2RootContext, args: McpUpdate, *, now: datetime) -> McpServerAnswer:
    """Replace the named fields of a registered server.

    Raises:
        DaemonValidationError: No field was named, or the result is not a
            valid server.
    """
    updates = {
        name: value
        for name, value in (
            ("command", args.command),
            ("args", None if args.args is None else list(args.args)),
            ("env_refs", None if args.env_refs is None else list(args.env_refs)),
            ("risk", args.risk),
            ("write_capable", args.write_capable),
        )
        if value is not None
    }
    if not updates:
        raise _refusal(
            "mcp_update_empty",
            "update requires at least one of command, args, env_refs, risk, write_capable",
        )
    with context.session([project_subject(context)]) as session:
        server, revision = _anchored_server(session, args.id, args.expected_revision)
        updated = _validated({**server.model_dump(mode="json"), **updates})
        answer = _write_server(
            session, updated, revision=revision + 1, removed=False, event=MCP_UPDATE_METHOD, now=now
        )
    logger.info(f"update_server id={args.id} fields={sorted(updates)} revision={answer.revision}")
    return answer


def remove_server(context: Epoch2RootContext, args: McpRemove, *, now: datetime) -> McpServerAnswer:
    """Retire a registered server; its row stays under its id, removed."""
    with context.session([project_subject(context)]) as session:
        server, revision = _anchored_server(session, args.id, args.expected_revision)
        answer = _write_server(
            session, server, revision=revision + 1, removed=True, event=MCP_REMOVE_METHOD, now=now
        )
    logger.info(f"remove_server id={args.id} revision={answer.revision}")
    return answer


def _write_grant(
    session: RootSession,
    grant: McpGrant,
    *,
    revision: int,
    revoked: bool,
    event: str,
    now: datetime,
) -> McpGrantAnswer:
    """Commit *grant*'s row at *revision* and return the answer describing it."""
    commit_row_write(
        session,
        collection=Epoch2Collection.TOOL_AUTHORITY,
        record_key=grant.id,
        row=tool_authority_row(
            grant, status="revoked" if revoked else "granted", revision=revision, at=now
        ),
        event_name=event,
        event_fields={"revision": revision, "server_id": grant.server_id},
        compaction=None,
        now=now,
    )
    return McpGrantAnswer(grant=grant, revision=revision, revoked=revoked)


def grant_server(
    context: Epoch2RootContext, args: McpGrantParams, *, now: datetime
) -> McpGrantAnswer:
    """Grant a registered server to a scope.

    Raises:
        DaemonValidationError: The server is not registered, or the named
            grant id already stands.
    """
    with context.session([project_subject(context)]) as session:
        document = session.read_document()
        server = read_servers(document).get(args.server_id)
        if server is None or server.server is None:
            raise _refusal(
                "mcp_server_not_found", f"mcp server {args.server_id!r} is not registered"
            )
        grants = read_grants(document)
        grant_id = args.grant_id or next_grant_id(grants)
        prior: StandingGrant | None = grants.get(grant_id)
        if prior is not None and prior.grant is not None:
            raise _refusal(
                "mcp_grant_exists",
                f"mcp grant id {grant_id!r} already exists; pick another or revoke it first",
            )
        grant = McpGrant(
            id=grant_id,
            scope_kind=args.scope_kind,
            scope_id=args.scope_id,
            server_id=args.server_id,
            granted_at=now,
        )
        answer = _write_grant(
            session,
            grant,
            revision=1 if prior is None else prior.revision + 1,
            revoked=False,
            event=MCP_GRANT_METHOD,
            now=now,
        )
    logger.info(f"grant_server grant={grant_id} server={args.server_id} scope={args.scope_kind}")
    return answer


def revoke_grant(context: Epoch2RootContext, args: McpRevoke, *, now: datetime) -> McpGrantAnswer:
    """Revoke a standing grant; its row stays under its id, revoked.

    Raises:
        DaemonValidationError: No grant stands under the id, or its row
            stands at another revision.
    """
    with context.session([project_subject(context)]) as session:
        standing = read_grants(session.read_document()).get(args.grant_id)
        if standing is None or standing.grant is None:
            raise _refusal("mcp_grant_not_found", f"mcp grant id {args.grant_id!r} is not granted")
        if standing.revision != args.expected_revision:
            raise _refusal(
                "revision_conflict",
                f"mcp grant {args.grant_id!r} is at revision {standing.revision} but the "
                f"request expects {args.expected_revision}",
            )
        answer = _write_grant(
            session,
            standing.grant,
            revision=standing.revision + 1,
            revoked=True,
            event=MCP_REVOKE_METHOD,
            now=now,
        )
    logger.info(f"revoke_grant grant={args.grant_id} revision={answer.revision}")
    return answer


async def _keyed(
    ctx: MethodContext,
    authority: RootAuthority,
    *,
    method: str,
    args: McpAdd | McpInstall | McpUpdate | McpRemove | McpGrantParams | McpRevoke,
    verb: Any,
) -> dict[str, Any]:
    """Run *verb* once per idempotency key and replay its answer on a retry."""
    context = ctx.native_root_context(authority.root)
    now = datetime.now(UTC)
    return await asyncio.to_thread(
        keyed_call,
        context,
        method=method,
        key=args.idempotency_key,
        params=args.model_dump(mode="json"),
        call=partial(verb, context, args, now=now),
        at=now,
    )


@native_mutator(MCP_ADD_METHOD)
async def _add(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Register an MCP server."""
    args = native_params(McpAdd, params)
    return await _keyed(ctx, authority, method=MCP_ADD_METHOD, args=args, verb=add_server)


@native_mutator(MCP_INSTALL_METHOD)
async def _install(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Record an MCP server's install into one runtime."""
    args = native_params(McpInstall, params)
    return await _keyed(ctx, authority, method=MCP_INSTALL_METHOD, args=args, verb=install_server)


@native_mutator(MCP_UPDATE_METHOD)
async def _update(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Replace fields of an MCP server."""
    args = native_params(McpUpdate, params)
    return await _keyed(ctx, authority, method=MCP_UPDATE_METHOD, args=args, verb=update_server)


@native_mutator(MCP_REMOVE_METHOD)
async def _remove(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Retire an MCP server."""
    args = native_params(McpRemove, params)
    return await _keyed(ctx, authority, method=MCP_REMOVE_METHOD, args=args, verb=remove_server)


@native_mutator(MCP_GRANT_METHOD)
async def _grant(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Grant an MCP server to a scope."""
    args = native_params(McpGrantParams, params)
    return await _keyed(ctx, authority, method=MCP_GRANT_METHOD, args=args, verb=grant_server)


@native_mutator(MCP_REVOKE_METHOD)
async def _revoke(
    ctx: MethodContext, params: dict[str, Any], authority: RootAuthority
) -> dict[str, Any]:
    """Revoke an MCP grant."""
    args = native_params(McpRevoke, params)
    return await _keyed(ctx, authority, method=MCP_REVOKE_METHOD, args=args, verb=revoke_grant)


__all__ = [
    "MCP_ADD_METHOD",
    "MCP_GRANT_METHOD",
    "MCP_INSTALL_METHOD",
    "MCP_REMOVE_METHOD",
    "MCP_REVOKE_METHOD",
    "MCP_UPDATE_METHOD",
    "McpAdd",
    "McpGrantAnswer",
    "McpGrantParams",
    "McpInstall",
    "McpRemove",
    "McpRevoke",
    "McpServerAnswer",
    "McpUpdate",
    "add_server",
    "grant_server",
    "install_server",
    "remove_server",
    "revoke_grant",
    "update_server",
]
