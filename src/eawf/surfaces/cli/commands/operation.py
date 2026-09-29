"""``eawf follow``: stream one submitted operation until it is terminal.

A long verb answers with an operation reference, and exit zero on that
answer means the work was accepted, not that it finished. ``follow`` reads
the operation's trail from a cursor, printing each state as it is recorded:
one text line per state, or one compact JSON object per line under
``--json``. Every poll is a fresh connection, so a dropped one is simply
reconnected from the cursor; only a daemon that stays unreachable ends the
stream early.

``follow`` is a read. It never starts a daemon, and it exits with the typed
status of the state it ended on: ``0`` for succeeded, the refusal code for
failed, and the daemon-unreachable code for unknown -- the outcome of work
whose daemon stopped under it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Annotated, Any, Final

import orjson
import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.commands.domain import _native_answer
from eawf.surfaces.cli.flags import GlobalFlags

#: The daemon verb a long command submits through, and the one it is
#: followed by; spelled here so the Typer tree builds without the daemon.
OPERATION_SUBMIT: Final = "operation.submit"
OPERATION_FOLLOW: Final = "operation.follow"

#: Consecutive polls that may fail to reach the daemon before the stream
#: gives up; one dropped connection is a reconnect, not an outcome.
_RECONNECT_ATTEMPTS: Final = 3

#: The exit status each terminal state ends a follow with.
_TERMINAL_EXIT: Final[dict[str, int]] = {
    "succeeded": exit_codes.OK,
    "failed": exit_codes.STATE_CONFLICT,
    "unknown": exit_codes.DAEMON_UNREACHABLE,
}


def operation_answer(record: dict[str, Any]) -> dict[str, Any]:
    """Return the answer a terminal operation carries, as a direct call would.

    Args:
        record: The operation's terminal revision.

    Returns:
        The work's result, when it succeeded.

    Raises:
        DaemonRpcError: The work failed; carries the very code, message and
            data a direct call would have raised.
        DaemonUnreachable: The daemon running the work stopped before it
            recorded an outcome.
    """
    if record["state"] == "succeeded":
        result: dict[str, Any] = record["result"]
        return result
    if record["state"] == "failed":
        error = record["error"]
        raise DaemonRpcError(error["code"], error["message"], error.get("data"))
    raise cli_errors.DaemonUnreachable(
        f"operation {record['operation_id']} ended {record['state']}: the daemon running "
        "it stopped before recording an outcome; inspect the subject and resubmit"
    )


def follow_operation(
    reference: str,
    *,
    flags: GlobalFlags,
    emit: Callable[[dict[str, Any]], None],
    interval_seconds: float,
) -> dict[str, Any]:
    """Stream one operation's revisions to *emit* until it is terminal.

    Args:
        reference: The operation reference a submission answered with.
        flags: Resolved global flags (the workspace anchor).
        emit: Called once per revision, oldest first.
        interval_seconds: How long to wait between polls.

    Returns:
        The operation's terminal revision.

    Raises:
        DaemonRpcError: The daemon refused the reference.
        DaemonUnreachable: The daemon stayed unreachable for
            :data:`_RECONNECT_ATTEMPTS` consecutive polls.
    """
    cursor = 0
    failures = 0
    while True:
        try:
            answer = _native_answer(
                OPERATION_FOLLOW,
                {"operation_ref": reference, "cursor": cursor},
                flags=flags,
                verb_text="follow",
                read=True,
            )
        except cli_errors.DaemonUnreachable:
            failures += 1
            if failures >= _RECONNECT_ATTEMPTS:
                raise
            time.sleep(interval_seconds)
            continue
        failures = 0
        for record in answer["records"]:
            emit(record)
        cursor = answer["cursor"]
        if answer["terminal"]:
            operation: dict[str, Any] = answer["operation"]
            return operation
        time.sleep(interval_seconds)


def _record_text(record: dict[str, Any]) -> str:
    """Return the text line one revision prints as."""
    line = (
        f"{record['verb']} {record['subject']} {record['state']} "
        f"revision {record['revision']} at {record['updated_at']}"
    )
    if record.get("error") is not None:
        line += f"\n  {record['error']['message']}"
    return line


def follow_cmd(
    ctx: typer.Context,
    operation_ref: Annotated[
        str, typer.Argument(help="The operation reference a long verb answered with.")
    ],
    interval: Annotated[
        float, typer.Option("--interval", min=0.05, help="Polling interval in seconds.")
    ] = 0.5,
) -> None:
    """Stream an operation's states until it succeeds, fails or is lost."""
    flags: GlobalFlags = ctx.obj

    def emit(record: dict[str, Any]) -> None:
        if flags.json_output:
            typer.echo(orjson.dumps(record, option=orjson.OPT_SORT_KEYS).decode())
        else:
            typer.echo(_record_text(record))

    try:
        final = follow_operation(operation_ref, flags=flags, emit=emit, interval_seconds=interval)
    except DaemonRpcError as exc:
        cli_errors.emit_error(cli_errors.cli_error_for_rpc(exc.code, exc.message), flags=flags)
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    code = _TERMINAL_EXIT[final["state"]]
    if code != exit_codes.OK:
        raise typer.Exit(code)


__all__ = [
    "OPERATION_FOLLOW",
    "OPERATION_SUBMIT",
    "follow_cmd",
    "follow_operation",
    "operation_answer",
]
