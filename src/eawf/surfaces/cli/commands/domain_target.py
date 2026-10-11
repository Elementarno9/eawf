"""The operator verbs that set one field of a record and move no status.

``milestone set-target`` sends ``domain.milestone.set_target`` and ``batch
set-title`` sends ``domain.batch.set_title``. Neither is one of the lifecycle
moves in :mod:`eawf.surfaces.cli.commands.domain`, but each is addressed and
answered exactly as they are: the URN, the revision the caller read, a retry
key and an actor, then the domain envelope printed as it arrived. The day is
parsed here so a malformed one costs no round trip; whether the Milestone is
still open, and whether the title fits, is the daemon's to decide.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any, Final

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.domain import (
    _ACTOR_HELP,
    _CORRELATION_HELP,
    _DATE_HELP,
    _KEY_HELP,
    _REVISION_HELP,
    _build_request,
    _call_envelope,
)
from eawf.surfaces.cli.commands.domain_consequence import DryRun, Yes, preview
from eawf.surfaces.cli.commands.lifecycle import batch_app, milestone_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.verb_contract import emit_envelope

#: The dotted JSON-RPC name the command forwards to.
MILESTONE_SET_TARGET: Final = "domain.milestone.set_target"

#: The dotted JSON-RPC name ``batch set-title`` forwards to.
BATCH_SET_TITLE: Final = "domain.batch.set_title"

_URN_HELP: Final = "URN of the Milestone to date."
_CLEAR_HELP: Final = "Clear the target date instead of setting one."
_BATCH_URN_HELP: Final = "URN of the Batch to title."
_TITLE_HELP: Final = "What the Batch delivers, as a short imperative noun phrase."

#: The one spelling a target date is accepted in. ``date.fromisoformat`` also
#: reads week dates and compact forms, which an operator never means here.
_DATE_SHAPE: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_target_date(text: str) -> date:
    """Return the calendar day *text* spells.

    Args:
        text: The operator's date, as ``YYYY-MM-DD``.

    Returns:
        The day.

    Raises:
        UserError: *text* is not ``YYYY-MM-DD`` or names no real day.
    """
    try:
        if _DATE_SHAPE.fullmatch(text) is None:
            raise ValueError(text)
        return date.fromisoformat(text)
    except ValueError as exc:
        raise cli_errors.UserError(
            f"the target date must be a real day spelled YYYY-MM-DD, got {text!r}",
            kind="InvalidInput",
        ) from exc


@milestone_app.command("set-target")
def milestone_set_target_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_URN_HELP)],
    expected_revision: Annotated[
        int,
        typer.Option("--expected-revision", "--expected-milestone-revision", help=_REVISION_HELP),
    ],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    target_date: Annotated[str | None, typer.Argument(help=_DATE_HELP)] = None,
    clear: Annotated[bool, typer.Option("--clear", help=_CLEAR_HELP)] = False,
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Set or clear the day an open Milestone is aimed at; its status does not move."""
    flags: GlobalFlags = ctx.obj
    try:
        if clear == (target_date is not None):
            raise cli_errors.UserError(
                "name exactly one of a YYYY-MM-DD date or --clear", kind="InvalidInput"
            )
        day = None if target_date is None else parse_target_date(target_date).isoformat()
        request = _build_request(
            method=MILESTONE_SET_TARGET,
            urn=urn,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor=actor,
            from_spec=None,
        )
        params: dict[str, Any] = {
            "urn": request.urn,
            "expected_revision": request.expected_revision,
            "idempotency_key": request.idempotency_key,
            "actor": request.actor,
            "target_date": day,
            "correlation_id": correlation_id,
        }
        if not preview(
            MILESTONE_SET_TARGET, urn, expected_revision, flags=flags, dry_run=dry_run, yes=yes
        ):
            return
        envelope = _call_envelope(MILESTONE_SET_TARGET, params, urn=urn, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    emit_envelope(envelope, urn=urn, flags=flags)


@batch_app.command("set-title")
def batch_set_title_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_BATCH_URN_HELP)],
    title: Annotated[str, typer.Option("--title", help=_TITLE_HELP)],
    expected_revision: Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Give a Batch, open or finished, the title shown beside its key; its status does not move."""
    flags: GlobalFlags = ctx.obj
    try:
        request = _build_request(
            method=BATCH_SET_TITLE,
            urn=urn,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor=actor,
            from_spec=None,
        )
        params: dict[str, Any] = {
            "urn": request.urn,
            "expected_revision": request.expected_revision,
            "idempotency_key": request.idempotency_key,
            "actor": request.actor,
            "title": title,
            "correlation_id": correlation_id,
        }
        if not preview(
            BATCH_SET_TITLE, urn, expected_revision, flags=flags, dry_run=dry_run, yes=yes
        ):
            return
        envelope = _call_envelope(BATCH_SET_TITLE, params, urn=urn, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    emit_envelope(envelope, urn=urn, flags=flags)


__all__ = [
    "BATCH_SET_TITLE",
    "MILESTONE_SET_TARGET",
    "batch_set_title_cmd",
    "milestone_set_target_cmd",
    "parse_target_date",
]
