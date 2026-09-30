"""``eawf action``: the command-line twin of the console's Attention verbs.

The group carries every write the Attention route offers beside an answer, so a keyboard
without the console reaches each of them: a principal's own snooze of a pending action,
its assignment to another principal, a provider permission's approval or denial, and a
budget notice's snooze, acknowledgement or resolve. An answer itself is sealed through
``eawf milestone seal-approval`` or relayed through ``eawf question answer``.

Each verb prints its consequence block before it sends, at the revision it names;
``--dry-run`` prints only the block and sends nothing. Each is sent at the revision the
operator was shown, so a record that moved since is refused naming where it stands.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

import typer

from eawf.surfaces.cli.commands.domain_consequence import DryRun, Yes, preview
from eawf.surfaces.cli.commands.question_decision import forward_answer
from eawf.surfaces.cli.flags import GlobalFlags

#: The daemon verbs these commands forward to, spelled here so the Typer tree builds
#: without the daemon method registry on the path.
ACTION_SNOOZE: Final = "runtime.pending_action.snooze"
ACTION_ASSIGN: Final = "runtime.pending_action.assign"
PERMISSION_DECIDE: Final = "runtime.permission.decide"
NOTICE_DISPOSE: Final = "budget_notice.dispose"

#: The principal class a person deciding from the command line acts as, as on a console.
OPERATOR_CLASS: Final = "operator"

_REVISION_HELP: Final = "The revision the record was shown at."
_KEY_HELP: Final = "The name this request is filed under; a retry under it writes nothing."
_ACTOR_HELP: Final = "Principal key the write is attributed to."
_MINUTES_HELP: Final = "How long a snooze lasts, in minutes."

action_app = typer.Typer(
    name="action",
    help=(
        "What the Attention register asks a principal about (snooze, assign, "
        "decide-permission, notice)."
    ),
    no_args_is_help=True,
)


def _send(
    ctx: typer.Context,
    method: str,
    params: dict[str, Any],
    *,
    urn: str,
    revision: int,
    verb_text: str,
    dry_run: bool,
    yes: bool,
) -> None:
    """Print the consequence of one Attention write, then send it unless told not to."""
    flags: GlobalFlags = ctx.obj
    if preview(method, urn, revision, flags=flags, dry_run=dry_run, yes=yes):
        forward_answer(method, params, urn=urn, verb_text=verb_text, flags=flags)


def _until(minutes: int) -> str:
    """Return the instant a snooze of ``minutes`` minutes lapses, as the wire states it."""
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


@action_app.command("snooze")
def action_snooze_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help="The pending action to hide from yourself.")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    minutes: Annotated[int, typer.Option("--for-minutes", min=1, help=_MINUTES_HELP)] = 60,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Hide a waiting pending action from yourself alone for a while; it answers nothing."""
    params = {
        "urn": urn,
        "expected_revision": expected_revision,
        "idempotency_key": idempotency_key,
        "actor": actor,
        "snooze_until": _until(minutes),
    }
    _send(
        ctx,
        ACTION_SNOOZE,
        params,
        urn=urn,
        revision=expected_revision,
        verb_text="action snooze",
        dry_run=dry_run,
        yes=yes,
    )


@action_app.command("assign")
def action_assign_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help="The pending action to address.")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    to: Annotated[
        str | None,
        typer.Option("--to", help="The principal to address it to; omitted, everyone."),
    ] = None,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Address a waiting pending action to one principal; anyone eligible may still answer."""
    params = {
        "urn": urn,
        "expected_revision": expected_revision,
        "idempotency_key": idempotency_key,
        "actor": actor,
        "assignee": to,
    }
    _send(
        ctx,
        ACTION_ASSIGN,
        params,
        urn=urn,
        revision=expected_revision,
        verb_text="action assign",
        dry_run=dry_run,
        yes=yes,
    )


@action_app.command("decide-permission")
def action_decide_permission_cmd(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help="The provider permission to decide.")],
    verb: Annotated[Literal["approve", "deny"], typer.Option("--verb", help="Approve or deny it.")],
    expected_revision: Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Approve or deny a provider permission as the operator, before its deadline."""
    params = {
        "urn": urn,
        "verb": verb,
        "principal_class": OPERATOR_CLASS,
        "actor": actor,
        "expected_revision": expected_revision,
    }
    _send(
        ctx,
        PERMISSION_DECIDE,
        params,
        urn=urn,
        revision=expected_revision,
        verb_text="action decide-permission",
        dry_run=dry_run,
        yes=yes,
    )


@action_app.command("notice")
def action_notice_cmd(
    ctx: typer.Context,
    notice_key: Annotated[str, typer.Argument(help="The budget notice's key.")],
    disposition: Annotated[
        Literal["snooze", "acknowledge", "resolve"],
        typer.Option("--disposition", help="Snooze or acknowledge it for yourself, or resolve it."),
    ],
    expected_revision: Annotated[int, typer.Option("--expected-revision", help=_REVISION_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
    minutes: Annotated[int, typer.Option("--for-minutes", min=1, help=_MINUTES_HELP)] = 60,
    dry_run: DryRun = False,
    yes: Yes = False,
) -> None:
    """Snooze, acknowledge or resolve a budget notice; none of them touches the work."""
    params: dict[str, Any] = {
        "notice_key": notice_key,
        "principal": actor,
        "disposition": disposition,
        "expected_revision": expected_revision,
    }
    if disposition == "snooze":
        params["snooze_until"] = _until(minutes)
    _send(
        ctx,
        NOTICE_DISPOSE,
        params,
        urn=notice_key,
        revision=expected_revision,
        verb_text="action notice",
        dry_run=dry_run,
        yes=yes,
    )


__all__ = [
    "ACTION_ASSIGN",
    "ACTION_SNOOZE",
    "NOTICE_DISPOSE",
    "OPERATOR_CLASS",
    "PERMISSION_DECIDE",
    "action_app",
]
