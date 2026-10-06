"""``eawf publish``: squash the branch's scratch commits into one commit per Batch.

The handler reads the generation's Task placements and staged bindings,
hands them to the publish library, and gives the library a binder that
sends the staged rows to the daemon once the Batch commits exist. It never
pushes; a push stays an operator action.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.workflow.integration.publish import PublishResult
    from eawf.workflow.integration.publish_binding import PublishedCommit

#: The daemon verb the binder sends, spelled here so the Typer tree builds
#: without the daemon method registry on the path.
PUBLISH_BIND_STAGED: Final = "runtime.publish.bind_staged"


def publish_cmd(ctx: typer.Context) -> None:
    """Squash the scratch commits past origin into one commit per Batch, then a state commit.

    Each Batch commit is made with git reset --soft and git commit, so the
    hooks run; the old tip is kept under refs/eawf/archive/publish/. The
    staged adoptions are bound to the new commits through the daemon. Run
    it again on a published branch and it changes nothing. It never pushes.
    """
    from eawf.kernel.state.epoch2.authority import NativeAuthorityRequiredError
    from eawf.surfaces.cli.commands.domain import _native_answer
    from eawf.workflow.integration.publish import PublishRefusedError, publish, read_membership

    flags: GlobalFlags = ctx.obj
    repo = (flags.workspace or Path.cwd()).resolve()

    def bind(branch: str, targets: Mapping[str, tuple[PublishedCommit, ...]]) -> int:
        answer = _native_answer(
            PUBLISH_BIND_STAGED,
            {
                "branch": branch,
                "candidates": [
                    {
                        "batch_ref": batch,
                        "commits": [item.model_dump(mode="json") for item in commits],
                    }
                    for batch, commits in targets.items()
                ],
            },
            flags=flags,
            verb_text="publish",
        )
        return len(answer["bound"])

    try:
        result = publish(
            repo, membership=read_membership(repo / ".ea"), bind=bind, now=datetime.now(UTC)
        )
    except NativeAuthorityRequiredError as exc:
        cli_errors.emit_error(cli_errors.MigrationRequired(str(exc)), flags=flags)
    except PublishRefusedError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind=exc.code), flags=flags)
    except DaemonRpcError as exc:
        cli_errors.emit_error(cli_errors.cli_error_for_rpc(exc.code, exc.message), flags=flags)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
    emit_json_or_text(result.model_dump(mode="json"), _text(result), flags=flags)


def _text(result: PublishResult) -> str:
    """Return the human rendering of one publish result."""
    if result.noop:
        return f"{result.branch} is already published at {result.new_tip[:12]}; nothing changed."
    lines = [f"Published {result.branch}: {result.old_tip[:12]} -> {result.new_tip[:12]}"]
    lines.extend(f"  {batch}  {sha[:12]}" for batch, sha in result.batch_commits.items())
    if result.state_commit is not None:
        lines.append(f"  state  {result.state_commit[:12]} ({result.bound} bindings)")
    if result.archive_ref is not None:
        lines.append(f"Old tip kept at {result.archive_ref}. Nothing was pushed.")
    return "\n".join(lines)


__all__ = ["PUBLISH_BIND_STAGED", "publish_cmd"]
