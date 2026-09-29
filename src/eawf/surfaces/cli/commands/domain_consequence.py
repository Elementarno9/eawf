"""The consequence a lifecycle command prints before it sends anything.

The command line keeps the console's contract: nothing mutates on a path that has not
printed its consequence. Every per-entity lifecycle command prints the consequence block
-- the target at the exact revision it names, the effects, the non-effects and the reload
rule -- before it sends. ``--dry-run`` prints only the block and sends nothing, so no
canonical or external effect is recorded; ``--yes`` sends without asking; an interactive
terminal is asked to confirm, and declining sends nothing.

The block is derived from the same transition table the console card reads, through
:func:`~eawf.kernel.state.epoch2.consequence.consequence`. The command has not read the
record, so the block states every edge the verb may take and leaves the choice, and every
guard, to the daemon.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any

import typer

from eawf.kernel.state.epoch2.consequence import MUTATIONS_BY_METHOD, consequence
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


def consequence_block(method: str, urn: str, revision: int) -> dict[str, Any]:
    """Return the consequence of ``method`` on ``urn`` at ``revision``, as data.

    Args:
        method: The per-entity daemon verb.
        urn: The record the command names.
        revision: The revision the command names.

    Returns:
        The target, effects, non-effects and reload rule.

    Raises:
        KeyError: ``method`` is not a lifecycle verb.
        ValueError: ``revision`` is not positive.
    """
    mutation = MUTATIONS_BY_METHOD[method]
    stated = consequence(mutation, key=urn.rsplit("/", 1)[-1], revision=revision, status=None)
    return {
        "operation": method,
        "target": urn,
        "revision": revision,
        "precision": "exact",
        "effects": list(stated.effects),
        "not": list(stated.not_effects),
        "if_stale": stated.if_stale,
    }


def block_text(block: dict[str, Any]) -> str:
    """Return the plain-text consequence block, one fact per line."""
    return "\n".join(
        [
            f"consequence: {block['operation']} {block['target']} at revision "
            f"{block['revision']} · {block['precision']}",
            *(f"  effect: {line}" for line in block["effects"]),
            *(f"  not: {line}" for line in block["not"]),
            f"  if stale: {block['if_stale']}",
        ]
    )


def preview(
    method: str, urn: str, revision: int, *, flags: GlobalFlags, dry_run: bool, yes: bool
) -> bool:
    """Print the consequence, then say whether the move may be sent.

    A dry run prints the block as the command's answer and sends nothing. Otherwise the
    block is printed on stderr, so a caller parsing stdout still reads only the daemon's
    envelope, and an interactive terminal without ``--yes`` is asked to confirm.

    Args:
        method: The per-entity daemon verb.
        urn: The record the command names.
        revision: The revision the command names.
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


__all__ = ["DECLINED_EXIT", "DryRun", "Yes", "block_text", "consequence_block", "preview"]
