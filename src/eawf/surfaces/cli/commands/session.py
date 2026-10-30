"""``eawf session bind``: re-scope a host session to the Task it serves.

A host session started on a Task worktree branch is bound to that Task by its start
hook; this verb binds one to another Task, or binds one the branch did not name. The
binding is a machine-local row that supersedes the session's earlier one, so
``eawf run transcript <TASK>`` lists the session's transcript from then on. No
record moves.

JSON answer (``--json``): ``{"task_key", "vendor_session_digest", "source",
"recorded_at"}``.

Exit codes:

- ``0`` when the binding row was appended.
- ``1`` (``USER_ERROR``) with ``data.kind`` ``NotFound`` when there is no tree, the
  tree holds no native generation, no Task has the key, or no session is named and
  the command runs in no host session; ``InvalidInput`` when the key or session id
  is malformed.

Run inside a linked worktree, the binding is kept in the main checkout's tree.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

session_app = typer.Typer(
    name="session",
    help="Host sessions: bind one to the Task it serves.",
    no_args_is_help=True,
)


@session_app.command("bind")
def session_bind_cmd(
    ctx: typer.Context,
    task_key: Annotated[str, typer.Argument(help="The Task the session serves, e.g. EAWF-0649.")],
    session: Annotated[
        str | None,
        typer.Option(
            "--session",
            help="The host session id; defaults to the session this command runs in.",
        ),
    ] = None,
) -> None:
    """Bind a host session to a Task, superseding its earlier binding."""
    from pydantic import ValidationError

    from eawf.runtime.session.task_binding import current_session_id, rebind_session
    from eawf.runtime.vcs.branch_name import main_checkout

    flags: GlobalFlags = ctx.obj
    try:
        state_dir = resolve_state_path(flags.workspace).parent
        tree_root = main_checkout(state_dir.parent) / state_dir.name
        session_id = session if session is not None else current_session_id(os.environ)
        row = rebind_session(
            tree_root, task_key=task_key, session_id=session_id, now=datetime.now(UTC)
        )
    except (FileNotFoundError, LookupError) as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
    except ValidationError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
    emit_json_or_text(
        row.model_dump(mode="json"),
        f"bound session {row.vendor_session_digest} to {row.task_key}",
        flags=flags,
    )


__all__ = ["session_app", "session_bind_cmd"]
