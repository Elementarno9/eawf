"""``eawf workspace`` — workspace registry records and the registry view.

The workspace-state pointer verbs (``init``, ``add-repo``, ``remove-repo``,
``validate``, ``status``) retired at the flag day: the packet's workspace
verbs below are canonical, and a workspace lives in the registry rather
than in a pointer document.

Subcommands (registry-reader path, P20-I01-W05):

- ``workspace registry-list`` — enumerate the repos in
  ``~/.eawf/registry.json``.
- ``workspace registry-status`` — render the workspace dashboard
  (top strip + active-repo W02 quadrant) as text.

Subcommands (workspace-record path):

- ``workspace add <KEY> --home <c> --member <c>`` — register a
  :class:`~eawf.platform.registry.WorkspaceRecord`.
- ``workspace show [KEY]`` — show one record, resolving from the
  current directory when no key is given.
- ``workspace list`` — enumerate the registered workspace records.
- ``workspace member add|remove <KEY> <CODE>`` — edit membership.
- ``workspace select <KEY>`` — session-local selection; writes nothing.

The ``registry-list`` / ``registry-status`` subcommands are STRICTLY
READ-ONLY — per the
``feedback_explicit_registry_only`` memory note the registry grows
only via explicit ``init`` / ``repo add`` writes. Neither subcommand
creates, mutates, or scans the registry; both fail gracefully when
the file is absent.

Exit-code mapping mirrors the rest of the CLI (see
:mod:`eawf.surfaces.cli.errors`): bad inputs map to ``UserError``
(``kind="InvalidInput"``, exit 3), missing state to ``UserError``
(``kind="NotFound"``, exit 2), and lock contention to ``StateConflict``
(``kind="LockConflict"``, exit 5).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.runtime.lock import portalock
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.platform.registry import Registry, WorkspaceRecord

logger = logging.getLogger(__name__)

workspace_app = typer.Typer(
    name="workspace",
    help="Workspace-scoped state and repo linkage.",
    no_args_is_help=True,
    add_completion=False,
)


# ---- workspace registry-list ----------------------------------


@workspace_app.command(name="registry-list")
def workspace_registry_list_cmd(
    ctx: typer.Context,
    registry_path: Annotated[
        Path | None,
        typer.Option(
            "--registry-path",
            help="Override the default ``~/.eawf/registry.json`` (mostly for tests).",
        ),
    ] = None,
) -> None:
    """Enumerate repos in ``~/.eawf/registry.json``.

    STRICTLY READ-ONLY — never grows the registry. When the file is
    missing the command exits 2 (``UserError``, ``kind="NotFound"``) with
    a hint pointing at ``eawf init`` as the explicit-growth path. When the
    file is present but malformed it exits 3 (``UserError``,
    ``kind="InvalidInput"``) with the Pydantic validation error so the
    operator can repair by hand.
    """
    from eawf.platform.registry import (
        RegistryReadError,
        is_stale,
        read_registry,
        registry_mtime,
    )

    flags: GlobalFlags = ctx.obj
    try:
        registry: Registry = read_registry(path=registry_path)
    except RegistryReadError as exc:
        msg = str(exc)
        if "not found" in msg:
            cli_errors.emit_error(
                cli_errors.UserError(
                    f"registry not found; run `eawf init` or `eawf repo add` first ({exc})",
                    kind="NotFound",
                ),
                flags=flags,
            )
        else:
            cli_errors.emit_error(cli_errors.UserError(msg, kind="InvalidInput"), flags=flags)
        return
    mtime = registry_mtime(path=registry_path)
    rows: list[dict[str, Any]] = []
    for entry in sorted(registry.repos.values(), key=lambda e: e.code):
        rows.append(
            {
                "code": entry.code,
                "path": entry.path,
                "title": entry.title or entry.code,
                "stale": is_stale(entry, registry_mtime_at=mtime),
                "active": entry.code == registry.active_code,
            }
        )
    text_lines = [
        f"registry: {len(rows)} repo(s), active={registry.active_code!r}, "
        f"version={registry.version!r}"
    ]
    for row in rows:
        stale_marker = " (stale)" if row["stale"] else ""
        active_marker = " (active)" if row["active"] else ""
        text_lines.append(
            f"  {row['code']:12s} {row['title']!r:30s} {row['path']}{active_marker}{stale_marker}"
        )
    emit_json_or_text(
        {
            "registry_version": registry.version,
            "active_code": registry.active_code,
            "count": len(rows),
            "repos": rows,
        },
        "\n".join(text_lines),
        flags=flags,
    )


# ---- workspace registry-status --------------------------------


@workspace_app.command(name="registry-status")
def workspace_registry_status_cmd(
    ctx: typer.Context,
    registry_path: Annotated[
        Path | None,
        typer.Option(
            "--registry-path",
            help="Override the default ``~/.eawf/registry.json`` (mostly for tests).",
        ),
    ] = None,
    width: Annotated[
        int,
        typer.Option(
            "--width",
            help="Console width passed to the offline renderer.",
        ),
    ] = 100,
) -> None:
    """Render the workspace dashboard as text (top strip + W02 quadrant).

    STRICTLY READ-ONLY over the registry. The active repo's quadrant
    pulls panes from the W02 layout helpers so this view stays
    byte-identical to the single-repo TUI when only one entry is
    registered.

    JSON mode emits the same envelope shape as ``registry-list`` plus
    a ``rendered`` field carrying the captured text frame, so a
    downstream consumer can ingest either the structured payload or
    the pre-rendered view.
    """
    from eawf.platform.registry import (
        RegistryReadError,
        is_stale,
        read_registry,
        registry_mtime,
    )
    from eawf.surfaces.tui.chassis.offline import offline_render

    flags: GlobalFlags = ctx.obj
    rendered = offline_render(registry_path=registry_path, width=width)
    if flags.json_output:
        try:
            registry: Registry = read_registry(path=registry_path)
            mtime = registry_mtime(path=registry_path)
        except RegistryReadError as exc:
            emit_json_or_text(
                {
                    "registry_available": False,
                    "error": str(exc),
                    "rendered": rendered,
                },
                rendered,
                flags=flags,
            )
            return
        rows: list[dict[str, Any]] = []
        for entry in sorted(registry.repos.values(), key=lambda e: e.code):
            rows.append(
                {
                    "code": entry.code,
                    "path": entry.path,
                    "title": entry.title or entry.code,
                    "stale": is_stale(entry, registry_mtime_at=mtime),
                    "active": entry.code == registry.active_code,
                }
            )
        emit_json_or_text(
            {
                "registry_available": True,
                "registry_version": registry.version,
                "active_code": registry.active_code,
                "count": len(rows),
                "repos": rows,
                "rendered": rendered,
            },
            rendered,
            flags=flags,
        )
        return
    emit_json_or_text({"rendered": rendered}, rendered, flags=flags)


# ---- workspace registry records ---------------------------------------------
# The packet verbs below operate on the WorkspaceRecord rows in
# ``~/.eawf/registry.json`` rather than on a workspace state document.
# Mutating verbs proxy to the daemon's ``registry.workspace.*`` RPCs (the
# canonical registry mutator) and fall back to an in-process locked write
# only on the daemonless carve-out or a daemon that predates the RPCs.


#: Environment variable carrying the session-local workspace selection.
#: ``workspace select`` sets it for the current process and prints the
#: export line; nothing about the selection is persisted.
SESSION_WORKSPACE_ENV = "EAWF_WORKSPACE_KEY"


workspace_member_app = typer.Typer(
    name="member",
    help="Edit the membership of a registered workspace.",
    no_args_is_help=True,
    add_completion=False,
)
workspace_app.add_typer(workspace_member_app, name="member")


def _registry_target(registry_path: Path | None) -> Path:
    """Return the registry file the verbs read and write."""
    from eawf.platform.registry import default_registry_path

    return registry_path if registry_path is not None else default_registry_path()


def _read_registry_or_empty(target: Path) -> Registry:
    """Load *target*, treating a missing file as an empty registry.

    Raises:
        UserError: When the file exists but does not parse or validate
            (``kind="InvalidInput"``), so a hand-edited registry is
            reported rather than silently replaced.
    """
    from eawf.platform.registry import Registry as _Registry
    from eawf.platform.registry import RegistryReadError, read_registry

    try:
        return read_registry(path=target)
    except RegistryReadError as exc:
        msg = str(exc)
        if "not found" in msg:
            return _Registry()
        raise cli_errors.UserError(msg, kind="InvalidInput") from exc


def _emit_workspace_error(exc: Exception, *, code: str, flags: GlobalFlags) -> None:
    """Emit a workspace refusal with its stable code attached.

    Every refusal here is operator-fixable, so all of them exit on the
    ``UserError`` code. The discriminator a script routes on is
    ``error.data.code`` - the library's stable
    ``workspace_not_registered`` / ``workspace_ambiguous`` vocabulary -
    with ``error.data.candidates`` carrying the competing keys when the
    operator has to pick one.

    Args:
        exc: The library refusal being surfaced.
        code: Its stable code.
        flags: Global flags driving the emission format.

    Raises:
        typer.Exit: Always, via :func:`emit_error`.
    """
    from eawf.platform.registry import WORKSPACE_NOT_REGISTERED

    kind = "NotFound" if code == WORKSPACE_NOT_REGISTERED else "InvalidInput"
    data: dict[str, Any] = {"code": code}
    candidates = getattr(exc, "candidates", ())
    if candidates:
        data["candidates"] = list(candidates)
    cli_errors.emit_error(
        cli_errors.UserError(str(exc), kind=kind),
        flags=flags,
        data=data,
    )


def _write_registry_locked(registry: Registry, target: Path) -> None:
    """Validate and atomically write *registry* to *target* under a lock.

    Raises:
        StateConflict: Lock contention (``kind="LockConflict"``).
    """
    from eawf.platform.registry import Registry as _Registry

    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _Registry.model_validate(registry.model_dump(mode="json")).model_dump(mode="json")
    try:
        with portalock.acquire(target, timeout=5.0):
            atomic_write_json_locked(target, payload)
    except portalock.LockTimeout as exc:
        raise cli_errors.StateConflict(str(exc), kind="LockConflict") from exc


def _mutate_via_daemon(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch a workspace mutation to the daemon.

    Returns the RPC result, or ``None`` when the daemon must not or
    cannot serve it (daemonless carve-out, daemon down, or a daemon
    that predates the ``registry.workspace.*`` namespace) and the
    caller should run the in-process arm instead.

    Raises:
        UserError: When the daemon refused the mutation
            (``kind="InvalidInput"``); the refusal is the answer, not
            a reason to retry locally.
    """
    import os

    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError
    from eawf.surfaces.cli._mutation import _daemon_reachable, _proxy_enabled

    if os.environ.get("EAWF_DAEMONLESS", "") == "1" or not _proxy_enabled(None):
        return None
    if not _daemon_reachable():
        return None
    try:
        with DaemonClient() as client:
            return client.call(method, params)
    except DaemonRpcError as exc:
        if exc.code == -32601:
            logger.debug(f"_mutate_via_daemon method-not-found method={method!r}")
            return None
        raise cli_errors.UserError(exc.message, kind="InvalidInput") from exc


def _apply_workspace_mutation(
    *,
    method: str,
    params: dict[str, Any],
    target: Path,
    local: Callable[[Registry], Registry],
    key: str,
) -> WorkspaceRecord:
    """Run a workspace mutation through the daemon or the local fallback.

    Both arms end at the same library helper, so the record the caller
    gets back is identical whichever arm ran.

    Args:
        method: ``registry.workspace.*`` RPC name.
        params: RPC params for the daemon arm.
        target: Registry file the local arm writes.
        local: The library mutation applied to the loaded registry.
        key: Workspace key to pick out of the mutated registry.

    Returns:
        The mutated :class:`WorkspaceRecord`.

    Raises:
        UserError: Daemon refusal or unreadable registry.
        StateConflict: Lock contention on the local arm.
        WorkspaceMutationError: Library refusal on the local arm.
    """
    from eawf.platform.registry import WorkspaceRecord as _WorkspaceRecord

    result = _mutate_via_daemon(method, params)
    if result is not None:
        return _WorkspaceRecord.model_validate(result["workspace"])
    updated = local(_read_registry_or_empty(target))
    _write_registry_locked(updated, target)
    return updated.workspaces[key]


# ---- workspace add ----------------------------------------------------------


@workspace_app.command(name="add")
def workspace_add_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Workspace key (project-code shape).")],
    home: Annotated[
        str,
        typer.Option("--home", help="Project code of the member repo that anchors the workspace."),
    ],
    member: Annotated[
        list[str] | None,
        typer.Option(
            "--member",
            help="Member project code; repeat the flag for each member.",
        ),
    ] = None,
    title: Annotated[
        str | None, typer.Option("--title", help="Human-readable workspace title.")
    ] = None,
    registry_path: Annotated[
        Path | None,
        typer.Option("--registry-path", help="Override ``~/.eawf/registry.json`` (tests)."),
    ] = None,
) -> None:
    """Register a workspace record with an explicit membership.

    Membership is declared, never discovered: every member is named on
    the command line. ``--home`` must be one of them.
    """
    from pydantic import ValidationError as PydValidationError

    from eawf.platform.registry import WorkspaceMutationError
    from eawf.platform.registry import WorkspaceRecord as _WorkspaceRecord
    from eawf.platform.registry import create_workspace as _create

    flags: GlobalFlags = ctx.obj
    members = frozenset(member or ()) | {home}
    target = _registry_target(registry_path)
    try:
        record = _WorkspaceRecord(
            key=key,
            title=title,
            member_project_codes=members,
            home_project_code=home,
        )
    except PydValidationError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
        return
    try:
        stored = _apply_workspace_mutation(
            method="registry.workspace.create",
            params={
                "record": record.model_dump(mode="json"),
                "registry_path": str(target),
            },
            target=target,
            local=lambda registry: _create(registry, record=record),
            key=key,
        )
    except WorkspaceMutationError as exc:
        _emit_workspace_error(exc, code=exc.code, flags=flags)
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    _emit_workspace(stored, target=target, flags=flags, verb="add")


# ---- workspace member add / remove ------------------------------------------


def _member_mutation(
    ctx: typer.Context,
    *,
    key: str,
    add: tuple[str, ...],
    remove: tuple[str, ...],
    registry_path: Path | None,
    verb: str,
) -> None:
    """Shared body for ``member add`` and ``member remove``."""
    from pydantic import ValidationError as PydValidationError

    from eawf.platform.registry import WorkspaceMutationError
    from eawf.platform.registry import update_membership as _update

    flags: GlobalFlags = ctx.obj
    target = _registry_target(registry_path)
    try:
        stored = _apply_workspace_mutation(
            method="registry.workspace.update_membership",
            params={
                "key": key,
                "add": list(add),
                "remove": list(remove),
                "registry_path": str(target),
            },
            target=target,
            local=lambda registry: _update(registry, key=key, add=add, remove=remove),
            key=key,
        )
    except WorkspaceMutationError as exc:
        _emit_workspace_error(exc, code=exc.code, flags=flags)
        return
    except PydValidationError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    _emit_workspace(stored, target=target, flags=flags, verb=verb)


@workspace_member_app.command(name="add")
def workspace_member_add_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Workspace key.")],
    code: Annotated[str, typer.Argument(help="Project code to include.")],
    registry_path: Annotated[
        Path | None,
        typer.Option("--registry-path", help="Override ``~/.eawf/registry.json`` (tests)."),
    ] = None,
) -> None:
    """Add one project code to a registered workspace's membership."""
    _member_mutation(
        ctx,
        key=key,
        add=(code,),
        remove=(),
        registry_path=registry_path,
        verb="member add",
    )


@workspace_member_app.command(name="remove")
def workspace_member_remove_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Workspace key.")],
    code: Annotated[str, typer.Argument(help="Project code to drop.")],
    registry_path: Annotated[
        Path | None,
        typer.Option("--registry-path", help="Override ``~/.eawf/registry.json`` (tests)."),
    ] = None,
) -> None:
    """Drop one project code from a registered workspace's membership.

    Dropping the home repo, or the last member, is refused by the
    record model rather than silently allowed.
    """
    _member_mutation(
        ctx,
        key=key,
        add=(),
        remove=(code,),
        registry_path=registry_path,
        verb="member remove",
    )


# ---- workspace show / list / select -----------------------------------------


def _workspace_payload(record: WorkspaceRecord) -> dict[str, Any]:
    """Render *record* as the JSON envelope body the verbs share."""
    return {
        "key": record.key,
        "title": record.title,
        "members": sorted(record.member_project_codes),
        "home": record.home_project_code,
        "revision": record.revision,
    }


def _emit_workspace(
    record: WorkspaceRecord,
    *,
    target: Path,
    flags: GlobalFlags,
    verb: str,
    source: str | None = None,
) -> None:
    """Emit one workspace record in JSON or text form."""
    payload = _workspace_payload(record)
    payload["registry_path"] = str(target)
    if source is not None:
        payload["source"] = source
    members = ", ".join(payload["members"])
    text = (
        f"workspace {verb} {record.key} home={record.home_project_code} "
        f"members=[{members}] revision={record.revision}"
    )
    emit_json_or_text(payload, text, flags=flags)


@workspace_app.command(name="show")
def workspace_show_cmd(
    ctx: typer.Context,
    key: Annotated[
        str | None,
        typer.Argument(help="Workspace key; omit to resolve from the current directory."),
    ] = None,
    registry_path: Annotated[
        Path | None,
        typer.Option("--registry-path", help="Override ``~/.eawf/registry.json`` (tests)."),
    ] = None,
    repo_root: Annotated[
        Path | None,
        typer.Option("--repo-root", help="Repository root to resolve from (defaults to cwd)."),
    ] = None,
) -> None:
    """Show one workspace record, resolving it when no key is given.

    Resolution never walks toward the filesystem root: an unregistered
    directory refuses with ``workspace_not_registered`` instead of
    inheriting its parent's workspace.
    """
    import os

    from eawf.platform.registry import WorkspaceResolutionError, resolve_workspace

    flags: GlobalFlags = ctx.obj
    target = _registry_target(registry_path)
    try:
        registry = _read_registry_or_empty(target)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    try:
        resolution = resolve_workspace(
            registry,
            explicit_key=key,
            env_key=os.environ.get(SESSION_WORKSPACE_ENV),
            repo_root=repo_root if repo_root is not None else Path.cwd(),
        )
    except WorkspaceResolutionError as exc:
        _emit_workspace_error(exc, code=exc.code, flags=flags)
        return
    _emit_workspace(
        resolution.record,
        target=target,
        flags=flags,
        verb="show",
        source=resolution.source.value,
    )


@workspace_app.command(name="list")
def workspace_list_cmd(
    ctx: typer.Context,
    registry_path: Annotated[
        Path | None,
        typer.Option("--registry-path", help="Override ``~/.eawf/registry.json`` (tests)."),
    ] = None,
) -> None:
    """List every registered workspace, ordered by key. Read-only."""
    from eawf.platform.registry import list_workspaces

    flags: GlobalFlags = ctx.obj
    target = _registry_target(registry_path)
    try:
        registry = _read_registry_or_empty(target)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    rows = [_workspace_payload(record) for record in list_workspaces(registry)]
    text_lines = [f"workspaces: {len(rows)} registered"]
    for row in rows:
        members = ", ".join(row["members"])
        text_lines.append(f"  {row['key']:12s} home={row['home']:12s} members=[{members}]")
    emit_json_or_text(
        {"count": len(rows), "workspaces": rows, "registry_path": str(target)},
        "\n".join(text_lines),
        flags=flags,
    )


@workspace_app.command(name="select")
def workspace_select_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="Workspace key to select for this session.")],
    registry_path: Annotated[
        Path | None,
        typer.Option("--registry-path", help="Override ``~/.eawf/registry.json`` (tests)."),
    ] = None,
) -> None:
    """Select a workspace for the current session only.

    The selection is session-local by construction: it sets
    ``EAWF_WORKSPACE_KEY`` in this process and prints the export line
    for the operator's shell. Nothing is written to the registry or to
    any state document, so a selection can never outlive the session
    that made it or race a concurrent one.
    """
    import os

    from eawf.platform.registry import WorkspaceResolutionError, resolve_workspace

    flags: GlobalFlags = ctx.obj
    target = _registry_target(registry_path)
    try:
        registry = _read_registry_or_empty(target)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    try:
        resolution = resolve_workspace(registry, explicit_key=key)
    except WorkspaceResolutionError as exc:
        _emit_workspace_error(exc, code=exc.code, flags=flags)
        return
    os.environ[SESSION_WORKSPACE_ENV] = resolution.key
    export_line = f"{SESSION_WORKSPACE_ENV}={resolution.key}"
    emit_json_or_text(
        {
            "key": resolution.key,
            "source": resolution.source.value,
            "export": export_line,
            "persisted": False,
        },
        f"workspace select {resolution.key} (session-local; export {export_line})",
        flags=flags,
    )


__all__ = [
    "SESSION_WORKSPACE_ENV",
    "workspace_app",
    "workspace_member_app",
]
