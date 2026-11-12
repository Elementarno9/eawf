"""``eawf git`` Typer sub-app: git commands that hold the state document lock.

CLI dispatch only: the handler resolves the main checkout, hands git's
arguments to :func:`eawf.runtime.vcs.document_hold.run_git_holding_document_lock`
and exits with git's status.

Verbs:

- ``eawf git commit -- <git args>`` runs ``git commit <git args>``.
- ``eawf git cherry-pick -- <git args>`` runs ``git cherry-pick <git args>``.
- ``eawf git push -- <git args>`` runs ``git push <git args>``.
"""

from __future__ import annotations

from typing import Annotated

import typer

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.runtime.lock.portalock import LockTimeout
from eawf.runtime.vcs.branch_name import BareRepositoryError
from eawf.runtime.vcs.document_hold import run_git_holding_document_lock
from eawf.surfaces.cli.errors import StateConflict, UserError, emit_error
from eawf.surfaces.cli.flags import GlobalFlags

git_app = typer.Typer(
    name="git",
    help=(
        "Run git commit, git cherry-pick or git push while holding the state document "
        "lock, so a daemon write is not lost while commit or pre-push hooks stash files."
    ),
    no_args_is_help=True,
    add_completion=False,
)

_GitArgs = Annotated[
    list[str] | None,
    typer.Argument(help="The arguments for git, after --.", show_default=False),
]


def _run(ctx: typer.Context, subcommand: str, git_args: list[str] | None) -> None:
    """Run ``git <subcommand> <git_args>`` under the lock and exit with its status."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path, _reason = resolve_with_reason(workspace=flags.workspace)
        status = run_git_holding_document_lock(
            state_path.parent.parent, [subcommand, *(git_args or [])]
        )
    except (FileNotFoundError, BareRepositoryError) as exc:
        emit_error(UserError(str(exc), kind="NotFound"), flags=flags)
    except LockTimeout as exc:
        emit_error(StateConflict(str(exc), kind="LockTimeout"), flags=flags)
    raise typer.Exit(code=status)


@git_app.command("commit")
def git_commit(ctx: typer.Context, git_args: _GitArgs = None) -> None:
    """Run git commit with the arguments after --, holding the document lock."""
    _run(ctx, "commit", git_args)


@git_app.command("cherry-pick")
def git_cherry_pick(ctx: typer.Context, git_args: _GitArgs = None) -> None:
    """Run git cherry-pick with the arguments after --, holding the document lock."""
    _run(ctx, "cherry-pick", git_args)


@git_app.command("push")
def git_push(ctx: typer.Context, git_args: _GitArgs = None) -> None:
    """Run git push with the arguments after --, holding the document lock."""
    _run(ctx, "push", git_args)


__all__ = ["git_app"]
