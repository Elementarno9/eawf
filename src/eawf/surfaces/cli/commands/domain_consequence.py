"""The consequence a writing command prints before it sends anything.

The command line keeps the console's contract: nothing mutates on a path that has not
printed its consequence. Every per-entity lifecycle command, every create command and
every attention write -- a pending action's snooze or assignment, a provider permission's
decision, a budget notice's disposition and a Run control -- prints the consequence block
-- the target at the exact revision it names, the effects, the non-effects and the reload
rule -- before it sends. ``--dry-run`` prints only the block and sends nothing, so no
canonical or external effect is recorded; ``--yes`` sends without asking; an interactive
terminal is asked to confirm, and declining sends nothing.

A lifecycle block is derived from the same transition table the console card reads,
through :func:`~eawf.kernel.state.epoch2.consequence.consequence`. The command has not read
the record, so the block states every edge the verb may take and leaves the choice, and
every guard, to the daemon. Every other write states its fixed effects from
:data:`WRITE_CONSEQUENCES`, the same sentences its console card draws.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Final

import typer

from eawf.kernel.state.epoch2.consequence import MUTATIONS_BY_METHOD, consequence, if_stale
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

#: The flag that prints the consequence and sends nothing.
DryRun = Annotated[
    bool, typer.Option("--dry-run", help="Print the consequence only; send nothing.")
]
#: The flag that sends after printing the consequence, without asking.
Yes = Annotated[bool, typer.Option("--yes", help="Send after printing the consequence.")]

#: The exit status of a move the operator declined at the prompt; nothing was sent.
DECLINED_EXIT = exit_codes.USER_ERROR

_CREATED: Final = ("no existing record moves", "nothing is dispatched or started by it")

#: What each write that is not a lifecycle move does, and what it does not, by daemon verb.
WRITE_CONSEQUENCES: Final[Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]]] = MappingProxyType(
    {
        **{
            f"domain.{entity}.create": (
                (f"a new {entity} record is admitted from the create document",),
                _CREATED,
            )
            for entity in ("track", "milestone", "batch", "task", "run", "repository")
        },
        "domain.milestone.set_target": (
            ("the milestone's target date is set or cleared, one revision on",),
            ("its status does not move", "no batch, task or release is rescheduled by it"),
        ),
        "runtime.evidence.claim.file": (
            (
                "a claim is filed under the next CLM key, its spans digested as they read now",
                "its four evidence rungs are scored as it lands",
            ),
            ("the subject's status does not move", "no cited evidence or receipt changes"),
        ),
        "runtime.pending_action.snooze": (
            ("hidden for you only until the deadline — other principals still see it",),
            ("it is not answered", "its revision does not move"),
        ),
        "runtime.pending_action.assign": (
            ("addressed to the named principal, one revision on",),
            ("no authority moves: anyone eligible may still answer",),
        ),
        "runtime.permission.decide": (
            ("the provider permission is recorded decided in your name, as the operator",),
            ("no pending action on the same run is answered", "the provider's deadline runs"),
        ),
        "budget_notice.dispose": (
            ("the notice is disposed in your name at the revision you name",),
            ("no run is stopped, extended or restarted by it",),
        ),
        "runtime.question.answer": (
            ("the open question is answered in your name, by option or in your words",),
            ("the asking run resumes only when it reads the answer",),
        ),
        "runtime.run.control.request": (
            ("a control request is recorded against the run",),
            ("the run does not move until it answers the request",),
        ),
        "runtime.dispatch.control.request": (
            ("the dispatch request is recorded in your name; a pause or drain holds admission",),
            (
                "no claimed run is stopped or cancelled",
                "a drain is refused while a release publishes",
            ),
        ),
    }
)


def consequence_block(method: str, urn: str, revision: int | None) -> dict[str, Any]:
    """Return the consequence of ``method`` on ``urn`` at ``revision``, as data.

    Args:
        method: The daemon verb the command sends.
        urn: The record the command names.
        revision: The revision the command names; ``None`` for a write its route anchors
            on no revision, whose block says so rather than naming one.

    Returns:
        The target, effects, non-effects and reload rule.

    Raises:
        KeyError: ``method`` is neither a lifecycle verb nor a write
            :data:`WRITE_CONSEQUENCES` states.
        ValueError: ``revision`` is not positive for a lifecycle verb, or is negative.
    """
    if method in MUTATIONS_BY_METHOD and revision is not None:
        mutation = MUTATIONS_BY_METHOD[method]
        stated = consequence(mutation, key=urn.rsplit("/", 1)[-1], revision=revision, status=None)
        effects, not_effects, stale = stated.effects, stated.not_effects, stated.if_stale
    else:
        effects, not_effects = WRITE_CONSEQUENCES[method]
        # a create names the tree's cursor, which an empty tree holds at zero
        if revision is not None and revision < 0:
            raise ValueError(f"revision must not be negative, got {revision}")
        stale = (
            if_stale(revision)
            if revision is not None
            else "the route takes no revision, so the daemon answers against what it holds"
        )
    return {
        "operation": method,
        "target": urn,
        "revision": revision,
        "precision": "exact" if revision is not None else "unanchored",
        "effects": list(effects),
        "not": list(not_effects),
        "if_stale": stale,
    }


def block_text(block: dict[str, Any]) -> str:
    """Return the plain-text consequence block, one fact per line."""
    at = f"revision {block['revision']}" if block["revision"] is not None else "no revision"
    return "\n".join(
        [
            f"consequence: {block['operation']} {block['target']} at {at} · {block['precision']}",
            *(f"  effect: {line}" for line in block["effects"]),
            *(f"  not: {line}" for line in block["not"]),
            f"  if stale: {block['if_stale']}",
        ]
    )


def preview(
    method: str, urn: str, revision: int | None, *, flags: GlobalFlags, dry_run: bool, yes: bool
) -> bool:
    """Print the consequence, then say whether the move may be sent.

    A dry run prints the block as the command's answer and sends nothing. Otherwise the
    block is printed on stderr, so a caller parsing stdout still reads only the daemon's
    envelope, and an interactive terminal without ``--yes`` is asked to confirm.

    Args:
        method: The daemon verb the command sends.
        urn: The record the command names.
        revision: The revision the command names; ``None`` for an unanchored route.
        flags: The resolved global flags.
        dry_run: Whether to print only.
        yes: Whether to send without asking.

    Returns:
        Whether to send.

    Raises:
        typer.Exit: With :data:`DECLINED_EXIT` when the operator declined; nothing was sent.
    """
    block = consequence_block(method, urn, revision)
    if dry_run:
        emit_json_or_text(
            {"dry_run": True, "sent": False, "consequence": block},
            f"{block_text(block)}\ndry run · nothing was sent",
            flags=flags,
        )
        return False
    typer.echo(block_text(block), err=True)
    if yes or flags.no_input or not sys.stdin.isatty():
        return True
    if typer.confirm("send it?", default=False, err=True):
        return True
    typer.echo("declined · nothing was sent", err=True)
    raise typer.Exit(DECLINED_EXIT)


__all__ = [
    "DECLINED_EXIT",
    "WRITE_CONSEQUENCES",
    "DryRun",
    "Yes",
    "block_text",
    "consequence_block",
    "preview",
]
