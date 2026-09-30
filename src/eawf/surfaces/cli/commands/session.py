"""Typer sub-app for ``eawf session ...``.

Sub-commands:

- ``start``      — open a new ``ACTIVE`` session for ``(scope, runtime)``.
- ``checkpoint`` — append a ``session.checkpoint`` event for an existing session.
- ``close``      — terminate a session with ``closed | stale | failed``.
- ``recover``    — mark heartbeat-aged sessions ``stale``; default age 30 m.

``close`` and ``recover`` write through the daemon's ``state.session_close``
and ``state.session_recover``; the in-process writer runs only under the
explicit daemonless carve-out.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from eawf.kernel.state.enums import AgentSessionStatus, StoreKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)

#: Mirrors :data:`eawf.runtime.session.recovery.DEFAULT_AGE_MINUTES`; inlined as a
#: literal so the ``session recover --age`` default does not import the heavy
#: session/recovery subtree at command-tree build time.
_DEFAULT_AGE_MINUTES: int = 30

session_app = typer.Typer(
    name="session",
    help="Manage AI/human work sessions.",
    no_args_is_help=True,
)


def _events_path_for(state_path: Path) -> Path:
    """Return the canonical events-store JSONL path next to ``state.json``."""
    from eawf.kernel.store.paths import store_path

    return store_path(state_path, StoreKind.EVENT)


def _resolve_close_status(raw: str) -> AgentSessionStatus:
    """Resolve close status — only closed/stale/failed accepted."""
    try:
        status = AgentSessionStatus(raw.strip().lower())
    except ValueError as exc:
        raise cli_errors.UserError(
            f"--status must be one of closed/stale/failed; got {raw!r}", kind="InvalidInput"
        ) from exc
    if status not in {
        AgentSessionStatus.CLOSED,
        AgentSessionStatus.STALE,
        AgentSessionStatus.FAILED,
    }:
        raise cli_errors.UserError(
            f"--status must be one of closed/stale/failed; got {raw!r}", kind="InvalidInput"
        )
    return status


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
    from eawf.surfaces.cli import _dispatch

    flags: GlobalFlags = ctx.obj
    try:
        status_enum = _resolve_close_status(status)
        state_path = resolve_state_path(flags.workspace)
        answer = _dispatch.call_upkeep(
            "state.session_close",
            {
                "repo_root": str(state_path.parent.parent),
                "session_id": session_id,
                "status": status_enum.value,
                "summary": summary,
            },
            flags=flags,
            verb="session close",
            local=lambda: _close_locally(state_path, session_id, status_enum, summary),
        )
        emit_json_or_text(
            payload=answer,
            text=f"session closed: {answer['id']} ({status_enum.value})",
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


def _close_locally(
    state_path: Path, session_id: str, status: AgentSessionStatus, summary: str | None
) -> dict[str, object]:
    """Close one session in process, under the daemonless carve-out."""
    from eawf.runtime.session.store import SessionNotFound, close_session
    from eawf.surfaces.cli._mutation import state_transaction

    with state_transaction(state_path) as state:
        try:
            result = close_session(
                state=state,
                events_path=_events_path_for(state_path),
                session_id=session_id,
                status=status,
                summary=summary,
            )
        except SessionNotFound as exc:
            raise cli_errors.UserError(str(exc), kind="NotFound") from exc
    ended = result.session.ended_at
    return {
        "id": result.session.id,
        "status": result.session.status.value,
        "ended_at": ended.isoformat() if ended is not None else None,
    }


@session_app.command("recover")
def session_recover_cmd(
    ctx: typer.Context,
    age: Annotated[
        int,
        typer.Option(
            "--age",
            help=f"Heartbeat age threshold in minutes. Default {_DEFAULT_AGE_MINUTES}.",
        ),
    ] = _DEFAULT_AGE_MINUTES,
) -> None:
    """Mark every active/checkpointed session whose heartbeat is older than ``--age`` as stale."""
    from eawf.surfaces.cli import _dispatch

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        answer = _dispatch.call_upkeep(
            "state.session_recover",
            {"repo_root": str(state_path.parent.parent), "age_minutes": age},
            flags=flags,
            verb="session recover",
            local=lambda: _recover_locally(state_path, age),
        )
        marked = [str(sid) for sid in answer["marked_session_ids"]]
        emit_json_or_text(
            payload=answer,
            text=(
                f"sessions marked stale: {len(marked)}\n" + "\n".join(marked)
                if marked
                else "no stale sessions found"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)


def _recover_locally(state_path: Path, age: int) -> dict[str, object]:
    """Run one recovery sweep in process, under the daemonless carve-out."""
    from eawf.runtime.session.recovery import (
        record_recovery_summary,
        recover_sessions,
        recovery_payload,
    )
    from eawf.surfaces.cli._mutation import state_transaction

    events_path = _events_path_for(state_path)
    with state_transaction(state_path) as state:
        report = recover_sessions(state=state, events_path=events_path, age_minutes=age)
    record_recovery_summary(events_path=events_path, report=report, now=datetime.now(UTC))
    return recovery_payload(report)
