"""Typer sub-app for ``eawf session ...``.

Sub-commands:

- ``start``      — open a new ``ACTIVE`` session for ``(scope, runtime)``.
- ``checkpoint`` — append a ``session.checkpoint`` event for an existing session.
- ``close``      — terminate a session with ``closed | stale | failed``.
- ``recover``    — mark heartbeat-aged sessions ``stale``; default age 30 m.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.session.recovery import DEFAULT_AGE_MINUTES, recover_sessions
from eawf.session.store import (
    SessionConflict,
    SessionNotFound,
    append_event,
    close_session,
    start_session,
)
from eawf.session.store import (
    checkpoint as checkpoint_session,
)
from eawf.state.enums import AgentSessionRole, AgentSessionStatus, StoreKind
from eawf.store.paths import store_path

logger = logging.getLogger(__name__)

session_app = typer.Typer(
    name="session",
    help="Manage AI/human work sessions.",
    no_args_is_help=True,
)


def _events_path_for(state_path: Path) -> Path:
    """Return the canonical events-store JSONL path next to ``state.json``."""
    return store_path(state_path, StoreKind.EVENT)


def _resolve_role(raw: str) -> AgentSessionRole:
    try:
        return AgentSessionRole(raw.strip().lower())
    except ValueError as exc:
        raise cli_errors.InvalidInput(
            f"--role must be one of {[r.value for r in AgentSessionRole]}; got {raw!r}",
        ) from exc


def _resolve_close_status(raw: str) -> AgentSessionStatus:
    """Resolve close status — only closed/stale/failed accepted."""
    try:
        status = AgentSessionStatus(raw.strip().lower())
    except ValueError as exc:
        raise cli_errors.InvalidInput(
            f"--status must be one of closed/stale/failed; got {raw!r}",
        ) from exc
    if status not in {
        AgentSessionStatus.CLOSED,
        AgentSessionStatus.STALE,
        AgentSessionStatus.FAILED,
    }:
        raise cli_errors.InvalidInput(
            f"--status must be one of closed/stale/failed; got {raw!r}",
        )
    return status


_VALID_RUNTIMES: frozenset[str] = frozenset({"claude", "opencode", "generic"})


def _validate_runtime(runtime: str) -> str:
    if runtime not in _VALID_RUNTIMES:
        raise cli_errors.InvalidInput(
            f"--runtime must be one of {sorted(_VALID_RUNTIMES)}; got {runtime!r}",
        )
    return runtime


def _args_hash(args: dict[str, object]) -> str:
    return hashlib.sha256(orjson.dumps(args, option=orjson.OPT_SORT_KEYS)).hexdigest()


@session_app.command("start")
def session_start_cmd(
    ctx: typer.Context,
    role: Annotated[str, typer.Option("--role", help="Session role.")],
    scope: Annotated[str, typer.Option("--scope", help="Scope ID anchor.")],
    runtime: Annotated[str, typer.Option("--runtime", help="One of claude / opencode / generic.")],
) -> None:
    """Start a new agent session; rejects (scope, runtime) collisions."""
    flags: GlobalFlags = ctx.obj
    try:
        role_enum = _resolve_role(role)
        runtime = _validate_runtime(runtime)
        state_path = resolve_state_path(flags.workspace)
        events_path = _events_path_for(state_path)
        with state_transaction(state_path) as state:
            try:
                result = start_session(
                    state=state,
                    events_path=events_path,
                    role=role_enum,
                    scope_id=scope,
                    runtime=runtime,
                )
            except SessionConflict as exc:
                raise cli_errors.ValidationFailed(str(exc)) from exc
        emit_json_or_text(
            payload={
                "id": result.session.id,
                "role": result.session.role.value,
                "scope_id": result.session.scope_id,
                "runtime": result.session.runtime,
                "status": result.session.status.value,
            },
            text=f"session started: {result.session.id}",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@session_app.command("checkpoint")
def session_checkpoint_cmd(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Session ID, e.g. SES-...")],
    artifacts: Annotated[
        list[str] | None,
        typer.Option(
            "--artifact",
            help="Artifact ID to attach (repeatable).",
        ),
    ] = None,
    files: Annotated[
        list[str] | None,
        typer.Option("--files", help="File-glob string (repeatable)."),
    ] = None,
) -> None:
    """Append a checkpoint event for an existing session."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        events_path = _events_path_for(state_path)
        with state_transaction(state_path) as state:
            try:
                result = checkpoint_session(
                    state=state,
                    events_path=events_path,
                    session_id=session_id,
                    artifact_ids=list(artifacts or []),
                    file_globs=list(files or []),
                )
            except SessionNotFound as exc:
                raise cli_errors.NotFound(str(exc)) from exc
        emit_json_or_text(
            payload={
                "id": result.session.id,
                "status": result.session.status.value,
                "artifact_ids": list(result.session.artifact_ids),
                "files": list(files or []),
                "event_id": result.event.id,
            },
            text=f"session checkpointed: {result.session.id} (event {result.event.id})",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@session_app.command("close")
def session_close_cmd(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Argument(help="Session ID, e.g. SES-...")],
    status: Annotated[
        str,
        typer.Option("--status", help="One of closed / stale / failed."),
    ] = "closed",
    summary: Annotated[
        str | None,
        typer.Option("--summary", help="Summary text written to the session row."),
    ] = None,
) -> None:
    """Close a session; required to reach the ``closed/stale/failed`` set."""
    flags: GlobalFlags = ctx.obj
    try:
        status_enum = _resolve_close_status(status)
        state_path = resolve_state_path(flags.workspace)
        events_path = _events_path_for(state_path)
        with state_transaction(state_path) as state:
            try:
                result = close_session(
                    state=state,
                    events_path=events_path,
                    session_id=session_id,
                    status=status_enum,
                    summary=summary,
                )
            except SessionNotFound as exc:
                raise cli_errors.NotFound(str(exc)) from exc
        emit_json_or_text(
            payload={
                "id": result.session.id,
                "status": result.session.status.value,
                "ended_at": (
                    result.session.ended_at.isoformat()
                    if result.session.ended_at is not None
                    else None
                ),
            },
            text=f"session closed: {result.session.id} ({status_enum.value})",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@session_app.command("recover")
def session_recover_cmd(
    ctx: typer.Context,
    age: Annotated[
        int,
        typer.Option(
            "--age",
            help=f"Heartbeat age threshold in minutes. Default {DEFAULT_AGE_MINUTES}.",
        ),
    ] = DEFAULT_AGE_MINUTES,
) -> None:
    """Mark every active/checkpointed session whose heartbeat is older than ``--age`` as stale."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        events_path = _events_path_for(state_path)
        with state_transaction(state_path) as state:
            report = recover_sessions(
                state=state,
                events_path=events_path,
                age_minutes=age,
            )
        append_event(
            events_path=events_path,
            event_id=f"session-recover-summary-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}",
            event_type="session.recover.summary",
            actor="cli",
            command="session recover",
            args_hash=_args_hash({"age": age}),
            status="ok",
            message=(
                f"recovered {len(report.marked_session_ids)} session(s); "
                f"skipped {len(report.skipped_session_ids)}"
            ),
            scope_id=None,
            occurred_at=datetime.now(UTC),
        )
        emit_json_or_text(
            payload={
                "marked_session_ids": report.marked_session_ids,
                "skipped_session_ids": report.skipped_session_ids,
                "age_minutes": report.age_minutes,
            },
            text=(
                f"sessions marked stale: {len(report.marked_session_ids)}\n"
                + "\n".join(report.marked_session_ids)
                if report.marked_session_ids
                else "no stale sessions found"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


__all__ = ["session_app"]
