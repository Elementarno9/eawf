"""The operator verbs that maintain a Milestone or a Track without a lifecycle move.

``milestone edit`` sends ``domain.milestone.edit``, ``milestone purge`` sends
``domain.milestone.purge``, ``track rename`` sends ``domain.track.rename`` and
``track set-policy`` sends ``domain.track.set_policy``. Each is addressed and
answered exactly as a lifecycle move is: the URN, the revision the caller
read, a retry key and an actor, then the domain envelope printed as it
arrived. Whether the record may be edited or purged is the daemon's to decide.

``milestone edit`` takes the short fields as flags and the structured ones --
the appetite, exclusions and acceptance journey -- from a ``--from-spec`` JSON
object; a flag overrides the same field in the file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.domain import (
    _ACTOR_HELP,
    _CORRELATION_HELP,
    _KEY_HELP,
    _REVISION_HELP,
    _build_request,
    _call_envelope,
)
from eawf.surfaces.cli.commands.domain_consequence import DryRun, Yes, preview
from eawf.surfaces.cli.commands.lifecycle import milestone_app, track_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.verb_contract import emit_envelope, read_spec_document

#: The dotted JSON-RPC name each command forwards to.
MILESTONE_EDIT: Final = "domain.milestone.edit"
MILESTONE_PURGE: Final = "domain.milestone.purge"
TRACK_RENAME: Final = "domain.track.rename"
TRACK_SET_POLICY: Final = "domain.track.set_policy"

_MILESTONE_URN_HELP: Final = "URN of the Milestone."
_TRACK_URN_HELP: Final = "URN of the Track."
_TITLE_HELP: Final = "The new title, as a short imperative noun phrase."
_DESCRIPTION_HELP: Final = "The new long-form description."
_OUTCOME_HELP: Final = "The new outcome statement."
_EDIT_SPEC_HELP: Final = (
    "JSON object carrying any of title, description, outcome, appetite, exclusions and "
    "acceptance_journey."
)
_ACTIVE_MILESTONES_HELP: Final = "How many ACTIVE Milestones the Track admits at most."


def _send(
    ctx: typer.Context,
    *,
    method: str,
    urn: str,
    expected_revision: int,
    idempotency_key: str,
    actor: str,
    fields: dict[str, Any],
    correlation_id: str | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Send one maintenance request and print the envelope it is answered with.

    Args:
        ctx: Typer context carrying the resolved global flags.
        method: The dotted JSON-RPC name to send.
        urn: The record the request addresses.
        expected_revision: The compare-and-swap token.
        idempotency_key: The caller's name for this request.
        actor: The principal the request is attributed to.
        fields: The verb's own wire fields.
        correlation_id: The caller's thread of related requests.
        dry_run: Print the consequence only and send nothing.
        yes: Send after printing the consequence, without asking.
    """
    flags: GlobalFlags = ctx.obj
    try:
        request = _build_request(
            method=method,
            urn=urn,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor=actor,
            from_spec=None,
        )
        # the addressing goes last, so a spec file cannot readdress the request
        params: dict[str, Any] = {
            **fields,
            "urn": request.urn,
            "expected_revision": request.expected_revision,
            "idempotency_key": request.idempotency_key,
            "actor": request.actor,
            "correlation_id": correlation_id,
        }
        if not preview(method, urn, expected_revision, flags=flags, dry_run=dry_run, yes=yes):
            return
        envelope = _call_envelope(method, params, urn=urn, flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    emit_envelope(envelope, urn=urn, flags=flags)


@milestone_app.command("edit")
def milestone_edit_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_MILESTONE_URN_HELP)],
    expected_revision: Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    title: Annotated[str | None, typer.Option("--title", help=_TITLE_HELP)] = None,
    description: Annotated[
        str | None, typer.Option("--description", help=_DESCRIPTION_HELP)
    ] = None,
    outcome: Annotated[str | None, typer.Option("--outcome", help=_OUTCOME_HELP)] = None,
    from_spec: Annotated[Path | None, typer.Option("--from-spec", help=_EDIT_SPEC_HELP)] = None,
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Rewrite the contract of a PLANNED Milestone; its status does not move."""
    flags: GlobalFlags = ctx.obj
    try:
        fields = {} if from_spec is None else read_spec_document(from_spec)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return  # pragma: no cover  emit_error raises Exit
    flagged = {"title": title, "description": description, "outcome": outcome}
    fields.update({name: value for name, value in flagged.items() if value is not None})
    _send(
        ctx,
        method=MILESTONE_EDIT,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        fields=fields,
        correlation_id=correlation_id,
        dry_run=dry_run,
        yes=yes,
    )


@milestone_app.command("purge")
def milestone_purge_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_MILESTONE_URN_HELP)],
    expected_revision: Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Remove a CANCELLED Milestone that no pushed commit carries."""
    _send(
        ctx,
        method=MILESTONE_PURGE,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        fields={},
        correlation_id=correlation_id,
        dry_run=dry_run,
        yes=yes,
    )


@track_app.command("rename")
def track_rename_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_TRACK_URN_HELP)],
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
    """Give an ACTIVE Track a new title; its status does not move."""
    _send(
        ctx,
        method=TRACK_RENAME,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        fields={"title": title},
        correlation_id=correlation_id,
        dry_run=dry_run,
        yes=yes,
    )


@track_app.command("set-policy")
def track_set_policy_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help=_TRACK_URN_HELP)],
    active_milestones: Annotated[
        int, typer.Option("--active-milestones", min=0, help=_ACTIVE_MILESTONES_HELP)
    ],
    expected_revision: Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    correlation_id: Annotated[
        str | None, typer.Option("--correlation-id", help=_CORRELATION_HELP)
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Set how many ACTIVE Milestones an ACTIVE Track admits at most."""
    _send(
        ctx,
        method=TRACK_SET_POLICY,
        urn=urn,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        actor=actor,
        fields={"active_milestones": active_milestones},
        correlation_id=correlation_id,
        dry_run=dry_run,
        yes=yes,
    )


__all__ = [
    "MILESTONE_EDIT",
    "MILESTONE_PURGE",
    "TRACK_RENAME",
    "TRACK_SET_POLICY",
    "milestone_edit_cmd",
    "milestone_purge_cmd",
    "track_rename_cmd",
    "track_set_policy_cmd",
]
