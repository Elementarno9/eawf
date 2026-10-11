"""``eawf batch squash`` and ``eawf batch rebind``: one commit per Batch before a push.

``batch squash`` reads the generation's Task placements and hands them to the
squash library, which writes the branch's commits again as one commit per Batch
plus a trailing ``state:`` commit on a new branch, never moving the source
branch, and writes the map from each source commit to the commit now carrying
it. ``batch rebind`` sends that map to the daemon, which supersedes every
ledger row still naming a source commit. Neither pushes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final

import orjson
import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import batch_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.workflow.integration.squash import SquashResult

_ONTO_HELP: Final = "The published commit the squashed history starts after, e.g. origin/main."
_SOURCE_HELP: Final = "The tip to squash; its branch is never moved."
_INTO_HELP: Final = "New branch to create at the squashed tip; omit to only plan."
_BRANCH_HELP: Final = "The branch the squashed history is to replace; defaults to HEAD's."
_ON_CONFLICT_HELP: Final = (
    "On a Batch whose work does not replay apart: 'merge' joins the two Batches into "
    "one commit; 'carry' keeps them apart and lets the earlier commit carry the "
    "conflicted paths at their source content."
)
_TIDY_HELP: Final = (
    "Command run on each Batch commit's tree but the last before it is committed, "
    "keeping its edits to tracked files, e.g. an import sorter (repeatable)."
)
_CHECK_HELP: Final = "Command run in an export of each rewritten commit (repeatable)."
_MAP_OUT_HELP: Final = "Write the source-to-squashed commit map here, for batch rebind."
_MAP_FILE_HELP: Final = "The map batch squash wrote; the branch must point at its squashed tip."
_KEY_HELP: Final = "Caller's name for this request; a retry replays its receipt."
_ACTOR_HELP: Final = "Principal key the request is attributed to."

#: The daemon verb ``batch rebind`` sends, spelled here so the Typer tree builds
#: without the daemon method registry on the path.
DELIVERY_REBIND_SQUASHED: Final = "runtime.delivery.rebind_squashed"


@batch_app.command("squash")
def batch_squash_cmd(
    ctx: typer.Context,
    onto: Annotated[str, typer.Option("--onto", help=_ONTO_HELP)],
    on_conflict: Annotated[str, typer.Option("--on-conflict", help=_ON_CONFLICT_HELP)],
    source: Annotated[str, typer.Option("--source", help=_SOURCE_HELP)] = "HEAD",
    into: Annotated[str | None, typer.Option("--into", help=_INTO_HELP)] = None,
    branch: Annotated[str | None, typer.Option("--branch", help=_BRANCH_HELP)] = None,
    tidy: Annotated[list[str] | None, typer.Option("--tidy", help=_TIDY_HELP)] = None,
    check: Annotated[list[str] | None, typer.Option("--check", help=_CHECK_HELP)] = None,
    map_out: Annotated[Path | None, typer.Option("--map-out", help=_MAP_OUT_HELP)] = None,
) -> None:
    """Write the branch again as one commit per Batch and a trailing state commit.

    The squashed tip's tree is the source tip's, byte for byte; the answer says
    whether it is. The source branch is never moved and nothing is pushed.
    """
    from eawf.kernel.state.epoch2.authority import NativeAuthorityRequiredError
    from eawf.workflow.integration.publish import PublishRefusedError, read_membership
    from eawf.workflow.integration.squash import squash

    flags: GlobalFlags = ctx.obj
    if on_conflict not in ("merge", "carry"):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--on-conflict must be 'merge' or 'carry', not {on_conflict!r}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    repo = (flags.workspace or Path.cwd()).resolve()
    try:
        result = squash(
            repo,
            base=onto,
            source=source,
            branch=branch,
            task_batches=read_membership(repo / ".ea").task_batches,
            into=into,
            on_conflict="merge" if on_conflict == "merge" else "carry",
            tidy=tuple(tidy or ()),
            checks=tuple(check or ()),
        )
    except NativeAuthorityRequiredError as exc:
        cli_errors.emit_error(cli_errors.MigrationRequired(str(exc)), flags=flags)
        return
    except PublishRefusedError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind=exc.code), flags=flags)
        return
    if map_out is not None:
        map_out.write_bytes(
            orjson.dumps(result.map.model_dump(mode="json"), option=orjson.OPT_INDENT_2)
        )
    emit_json_or_text(result.model_dump(mode="json"), _squash_text(result), flags=flags)


def _squash_text(result: SquashResult) -> str:
    """Return the human rendering of one squash."""
    lines = [
        f"{'Squashed into ' + result.into if result.into else 'Planned'}: "
        f"{len(result.map.commits)} source commits -> {len(result.commits)} commits, "
        f"tree {'matches' if result.tree_equal else 'DIFFERS FROM'} the source tip"
    ]
    for commit in result.commits:
        batches = ",".join(commit.batch_keys) or "state"
        lines.append(
            f"  {commit.sha[:12]} {batches} {len(commit.task_keys)} Tasks "
            f"{commit.source_commits} source  {commit.subject}"
        )
        lines.extend(f"      folded {item}" for item in commit.folded)
        if commit.carried:
            lines.append(f"      carries {len(commit.carried)} paths at source content")
        lines.extend(f"      check failed: {item}" for item in commit.check_failures)
    lines.extend(f"  merged {', '.join(keys)}" for keys in result.merged)
    lines.append("Nothing was pushed; the source branch did not move.")
    return "\n".join(lines)


@batch_app.command("rebind")
def batch_rebind_cmd(
    ctx: typer.Context,
    map_file: Annotated[Path, typer.Option("--map-file", help=_MAP_FILE_HELP)],
    idempotency_key: Annotated[str, typer.Option("--idempotency-key", help=_KEY_HELP)],
    actor: Annotated[str, typer.Option("--actor", help=_ACTOR_HELP)],
) -> None:
    """Supersede every ledger row naming a commit the squash replaced.

    Run it once the branch points at the squashed tip. The daemon confirms the
    rewritten commits are on the branch and carry their Batch and Task
    trailers, then appends one superseding row per line naming a source commit.
    """
    from eawf.surfaces.cli.commands.domain_integration import _answer, _document, _send

    document = _document(ctx, map_file)
    if document is None:
        return
    subject = str(document.get("branch", map_file))
    answer = _send(
        ctx,
        DELIVERY_REBIND_SQUASHED,
        {"actor": actor, "idempotency_key": idempotency_key, "squash_map": document},
        urn=subject,
        verb_text="batch rebind",
        key=idempotency_key,
    )
    if answer is not None:
        _answer(ctx, answer, operation=DELIVERY_REBIND_SQUASHED, urn=subject, revision=None)
