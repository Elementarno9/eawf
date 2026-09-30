"""``eawf mcp ...`` Typer commands.

Surface:

- ``add <id>`` — register an Eä-owned MCP server.
- ``install <id>`` — write the server into a runtime's MCP configuration
  and record the install. Prompts unless ``--no-input`` is set.
- ``update <id>`` — replace fields of a registered server. Warns when a
  re-install is required.
- ``remove <id>`` — retire a server and (unless ``--keep-runtime-entry``)
  drop it from the runtime configurations it was installed into.
- ``list`` — read-only enumeration with owner annotation.
- ``grant <scope_kind> <scope_id> <server_id>`` — bind a server to a scope
  so dispatch can project allowed-tools.
- ``revoke <grant_id>`` — drop a grant.
- ``serve`` / ``run-config`` — the per-Run semantic tool server.

Discipline checklist:

- The registry is the selected generation's ``capability`` and
  ``tool_authority`` rows; every mutator sends one native ``mcp.*`` verb
  and the daemon writes the row. Nothing here writes a tree record.
- A verb that changes a standing row takes the ``--expected-revision``
  the operator read from ``mcp list`` (or a grant's answer) and an
  ``--idempotency-key``; a create takes the key alone.
- Env-ref tokens stay literal end-to-end. The installer never reads
  ``os.environ`` for env-ref names.
- User-owned ``mcpServers[*]`` entries in the runtime configuration are
  byte-equal across the whole add/install/update/remove sequence.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import typer

from eawf.kernel.state.enums import McpRisk
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.kernel.state.models import McpServer
    from eawf.runtime.mcp.book import StandingGrant, StandingServer

logger = logging.getLogger(__name__)


mcp_app = typer.Typer(
    name="mcp",
    help="Manage MCP server entries (add/install/update/remove/list/grant/revoke).",
    no_args_is_help=True,
)

#: The daemon verbs the handlers send. Spelled here so the Typer tree
#: builds without the daemon method registry on the path.
MCP_ADD: Final = "mcp.add"
MCP_INSTALL: Final = "mcp.install"
MCP_UPDATE: Final = "mcp.update"
MCP_REMOVE: Final = "mcp.remove"
MCP_GRANT: Final = "mcp.grant"
MCP_REVOKE: Final = "mcp.revoke"

_SUPPORTED_RUNTIMES: tuple[str, ...] = ("claude", "codex", "opencode")
_OWNER_FILTERS: tuple[str, ...] = ("eawf", "user", "all")
_REVISION_HELP: Final = "Revision of the row as last read (see `eawf mcp list --json`)."
_KEY_HELP: Final = "Caller's name for this request; a retry replays its receipt."

ExpectedRevision = Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)]
IdempotencyKey = Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)]


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
        raise cli_errors.UserError(
            f"unknown runtime {runtime!r}; expected one of {list(_SUPPORTED_RUNTIMES)}",
            kind="InvalidInput",
        )


def _resolve_risk(raw: str) -> McpRisk:
    try:
        return McpRisk(raw)
    except ValueError as exc:
        raise cli_errors.UserError(
            f"--risk must be one of {[r.value for r in McpRisk]}; got {raw!r}", kind="InvalidInput"
        ) from exc


def _server_payload(server: McpServer, revision: int) -> dict[str, object]:
    """Render *server* at *revision* as a JSON-friendly dict for envelopes."""
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
        "revision": revision,
    }


def _mcp_rpc(
    method: str, params: dict[str, object], *, flags: GlobalFlags, verb_text: str
) -> dict[str, object]:
    """Send one native ``mcp.*`` verb for the addressed tree.

    Raises:
        UserError: The daemon refused the request; a missing server or
            grant is ``NotFound``, a stale revision ``StateConflict`` and
            every other refusal ``InvalidInput``, with the daemon's message.
        CliError: The daemon was unreachable or failed outside the
            refusal vocabulary.
    """
    from eawf.surfaces.cli._daemon_client import DaemonRpcError
    from eawf.surfaces.cli.commands.domain import _native_answer

    repo_root = resolve_state_path(flags.workspace).parent.parent
    wire = {"repo_root": str(repo_root), **params}
    try:
        return _native_answer(method, wire, flags=flags, verb_text=verb_text)
    except DaemonRpcError as exc:
        if exc.code != cli_errors.RPC_VALIDATION_FAILED:
            raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
        message = exc.message.removeprefix("validation_failed: ")
        code = message.split(":", 1)[0]
        if code == "revision_conflict":
            raise cli_errors.StateConflict(message, kind="RevisionConflict") from exc
        kind = "NotFound" if code.endswith("_not_found") else "InvalidInput"
        raise cli_errors.UserError(message, kind=kind) from exc


def _generation_document(flags: GlobalFlags) -> Path | None:
    """Return the selected generation's document, or ``None`` on an epoch-1 tree."""
    from eawf.runtime.mcp.book import generation_document

    return generation_document(resolve_state_path(flags.workspace).parent)


def _book(flags: GlobalFlags) -> tuple[dict[str, StandingServer], dict[str, StandingGrant]]:
    """Return the tree's server and grant rows.

    Raises:
        UserError: The tree answers in epoch 1, whose MCP registry is
            frozen until the cutover imports it.
    """
    from eawf.kernel.store.compaction import read_document
    from eawf.runtime.mcp.book import read_grants, read_servers

    document_path = _generation_document(flags)
    if document_path is None:
        raise cli_errors.UserError(
            "this tree answers in epoch 1, whose MCP registry is frozen; run "
            "`eawf migrate` to import it before changing it",
            kind="InvalidInput",
        )
    document = read_document(document_path)
    return read_servers(document), read_grants(document)


def _anchored(flags: GlobalFlags, server_id: str, expected_revision: int) -> McpServer:
    """Return the registered server *server_id* at the revision the operator read.

    The daemon re-checks the anchor under its lock; this read only keeps a
    stale request from touching a runtime configuration first.

    Raises:
        UserError: No server is registered under the id.
        StateConflict: The row stands at another revision.
    """
    servers, _ = _book(flags)
    standing = servers.get(server_id)
    if standing is None or standing.server is None:
        raise cli_errors.UserError(f"mcp id {server_id!r} is not registered", kind="NotFound")
    if standing.revision != expected_revision:
        raise cli_errors.StateConflict(
            f"mcp id {server_id!r} is at revision {standing.revision}; "
            f"--expected-revision {expected_revision} is stale",
            kind="RevisionConflict",
        )
    return standing.server


def _answered_server(answer: dict[str, object]) -> tuple[McpServer, int]:
    """Return the server and revision one ``mcp.*`` server answer carries."""
    from eawf.kernel.state.models import McpServer

    revision = answer["revision"]
    assert isinstance(revision, int)
    return McpServer.model_validate(answer["server"]), revision


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
      :class:`UserError` (``kind="UserDeclined"``). We refuse to silently
      proceed — the caller must explicitly opt in via ``--no-input``.
    - Otherwise prompt; a "no" answer raises :class:`UserError`
      (``kind="UserDeclined"``).

    The text mirrors a security checklist: command, env-refs,
    runtime, target path. No env-ref *values* are emitted (and we
    never have them — the installer is on the literal-token side
    of the env barrier).
    """
    if no_input:
        return
    if not sys.stdin.isatty():
        raise cli_errors.UserError(
            "stdin is not a TTY and --no-input was not passed; refusing to "
            "install MCP server without confirmation",
            kind="UserDeclined",
        )
    env_refs_repr = ", ".join(server.env_refs) if server.env_refs else "(none)"
    prompt = (
        f"Install MCP server {server.id} (command={server.command}, "
        f"env_refs={env_refs_repr}) into {runtime} at {settings_path}? [y/N] "
    )
    answer = input(prompt).strip().lower()
    if answer not in {"y", "yes"}:
        raise cli_errors.UserError(f"user declined install of {server.id!r}", kind="UserDeclined")


@mcp_app.command(name="add")
def add_cmd(
    ctx: typer.Context,
    server_id: Annotated[
        str,
        typer.Argument(help="MCP server identifier.", metavar="ID"),
    ],
    command: Annotated[
        str,
        typer.Option("--command", help="argv[0] for the MCP launcher."),
    ],
    idempotency_key: IdempotencyKey,
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
            help="Redefine a registered entry with the same id.",
        ),
    ] = False,
) -> None:
    """Register a new Eä-owned MCP server."""
    flags: GlobalFlags = ctx.obj
    try:
        answer = _mcp_rpc(
            MCP_ADD,
            {
                "id": server_id,
                "command": command,
                "args": list(arg or []),
                "env_refs": list(env_ref or []),
                "risk": _resolve_risk(risk).value,
                "write_capable": write_capable,
                "force": force,
                "idempotency_key": idempotency_key,
            },
            flags=flags,
            verb_text="mcp add",
        )
        server, revision = _answered_server(answer)
        emit_json_or_text(
            payload=_server_payload(server, revision),
            text=f"mcp added: {server.id} (owner=eawf, command={server.command})",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


@mcp_app.command(name="install")
def install_cmd(
    ctx: typer.Context,
    server_id: Annotated[str, typer.Argument(help="MCP id to install.", metavar="ID")],
    expected_revision: ExpectedRevision,
    idempotency_key: IdempotencyKey,
    runtime: Annotated[
        str,
        typer.Option("--runtime", help="Target runtime: claude | codex | opencode."),
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
    """Write a registered MCP server into a runtime config and record the install."""
    from datetime import UTC, datetime

    from eawf.runtime.mcp.installer import (
        IntegrityViolation,
        VerifyFailure,
        install_runtime_entry,
        runtime_config_path,
    )

    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
        target = (target_dir or _resolve_target(flags)).resolve()
        server = _anchored(flags, server_id, expected_revision)
        settings_path = runtime_config_path(runtime, target).resolve()
        _confirm_install(
            server=server, runtime=runtime, settings_path=settings_path, no_input=flags.no_input
        )
        try:
            result = install_runtime_entry(
                server=server,
                runtime=runtime,
                target_dir=target,
                force=force,
                timestamp=datetime.now(UTC).isoformat(),
            )
        except IntegrityViolation as exc:
            raise cli_errors.StateConflict(str(exc), kind="IntegrityViolation") from exc
        except VerifyFailure as exc:
            raise cli_errors.StateConflict(str(exc), kind="VerifyFailure") from exc
        except ValueError as exc:
            raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
        answer = _mcp_rpc(
            MCP_INSTALL,
            {
                "id": server_id,
                "runtime": runtime,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
            flags=flags,
            verb_text="mcp install",
        )
        installed, revision = _answered_server(answer)
        emit_json_or_text(
            payload={
                "id": server_id,
                "runtime": runtime,
                "target_path": str(result.target_path),
                "action": "installed",
                "fs_action": result.action,
                "user_entries_preserved": result.user_entries_preserved,
                "status": installed.status.value,
                "revision": revision,
            },
            text=(
                f"mcp installed: {server_id} → {result.target_path} "
                f"({result.action}; preserved {len(result.user_entries_preserved)} user entries)"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


@mcp_app.command(name="update")
def update_cmd(
    ctx: typer.Context,
    server_id: Annotated[str, typer.Argument(help="MCP id to update.", metavar="ID")],
    expected_revision: ExpectedRevision,
    idempotency_key: IdempotencyKey,
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
    """Replace fields of a registered Eä-owned MCP server."""
    flags: GlobalFlags = ctx.obj
    try:
        params: dict[str, object] = {
            "id": server_id,
            "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
        }
        if command is not None:
            params["command"] = command
        if arg is not None:
            params["args"] = list(arg)
        if env_ref is not None:
            params["env_refs"] = list(env_ref)
        if risk is not None:
            params["risk"] = _resolve_risk(risk).value
        if write_capable is not None:
            params["write_capable"] = write_capable
        answer = _mcp_rpc(MCP_UPDATE, params, flags=flags, verb_text="mcp update")
        updated, revision = _answered_server(answer)
        installed_targets = list(updated.installed_targets)
        text_lines = [f"mcp updated: {server_id}"]
        if installed_targets:
            text_lines.append(
                f"note: run `eawf mcp install {server_id}` to apply the change to "
                f"{', '.join(installed_targets)}"
            )
        payload = _server_payload(updated, revision)
        payload["reinstall_required"] = bool(installed_targets)
        emit_json_or_text(payload=payload, text="\n".join(text_lines), flags=flags)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


@mcp_app.command(name="remove")
def remove_cmd(
    ctx: typer.Context,
    server_id: Annotated[str, typer.Argument(help="MCP id to remove.", metavar="ID")],
    expected_revision: ExpectedRevision,
    idempotency_key: IdempotencyKey,
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
            help="Retire only the registry row; leave the runtime config unchanged.",
        ),
    ] = False,
) -> None:
    """Retire an Eä-owned MCP server (and optionally its runtime config entries)."""
    from eawf.runtime.mcp.installer import IntegrityViolation, remove_runtime_entry

    flags: GlobalFlags = ctx.obj
    try:
        target = (target_dir or _resolve_target(flags)).resolve()
        if runtime is not None:
            _validate_runtime(runtime)
        server = _anchored(flags, server_id, expected_revision)
        runtime_actions: list[dict[str, object]] = []
        if not keep_runtime_entry:
            targets = [runtime] if runtime is not None else list(server.installed_targets)
            for rt in targets:
                try:
                    result = remove_runtime_entry(
                        server_id=server_id, runtime=rt, target_dir=target, force=False
                    )
                except IntegrityViolation as exc:
                    raise cli_errors.StateConflict(str(exc), kind="IntegrityViolation") from exc
                except ValueError as exc:
                    raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
                runtime_actions.append(
                    {
                        "target_path": str(result.target_path),
                        "action": result.action,
                        "user_entries_preserved": result.user_entries_preserved,
                    }
                )
        answer = _mcp_rpc(
            MCP_REMOVE,
            {
                "id": server_id,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
            flags=flags,
            verb_text="mcp remove",
        )
        emit_json_or_text(
            payload={
                "id": server_id,
                "removed_from_state": True,
                "revision": answer["revision"],
                "runtime_actions": runtime_actions,
                "kept_runtime_entry": keep_runtime_entry,
            },
            text=(
                f"mcp removed: {server_id} "
                f"(runtime updates={len(runtime_actions)}; kept_runtime={keep_runtime_entry})"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


# ---- command registration ---------------------------------------------------
# Importing the sibling modules runs their ``@mcp_app.command(...)``
# decorators so the app above carries its full verb set. The imports sit
# at the bottom, after every shared symbol is defined, so the siblings can
# import the app and helpers from this module without a circular import.
from eawf.surfaces.cli.commands import mcp_grants as _mcp_grants  # noqa: E402, F401
from eawf.surfaces.cli.commands import mcp_run as _mcp_run  # noqa: E402, F401

__all__ = ["mcp_app"]
