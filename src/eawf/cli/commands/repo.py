"""``eawf repo`` — repo-scoped init + workspace linkage.

Two subcommands:

- ``repo init`` — for v0.1 this is a thin alias of :func:`eawf.cli.commands.init.init_cmd`.
  The "real divergence" between repo init and the top-level ``eawf init``
  lands in Phase 5 W06 (``eawf init`` self-apply); for v0.1 the surface is
  identical so existing callers can opt into the noun-based UX without
  behaviour change.
- ``repo link <workspace-code> <repo-code>`` — append the current (or
  ``--target``) repo to a workspace state document, optionally recording
  the workspace code on the repo's ``state.indexes.workspace_code`` so a
  future ``workspace validate`` can cross-check.

Workspace pointer attachment: rather than introduce a new
``workspace_code`` field on the :class:`Project` model (which would alter
the schema for every repo-scoped state, including ones that are not part
of any workspace), the link is recorded under
``state.indexes['workspace_code']`` — the model already declares
``indexes: dict[str, Any]`` for exactly this kind of "soft" linkage.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.commands.init import init_cmd as _init_cmd
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.lock import portalock
from eawf.state.enums import ProjectStatus
from eawf.state.ids import is_project_code
from eawf.state.models import WorkspaceIndex, WorkspaceRepoRef
from eawf.state.urn import build as build_urn
from eawf.state.writer import atomic_write_json_locked
from eawf.validate.strict import validate_state

logger = logging.getLogger(__name__)

repo_app = typer.Typer(
    name="repo",
    help="Repo-scoped init + workspace linkage.",
    no_args_is_help=True,
    add_completion=False,
)


# ---- repo init --------------------------------------------------------------


@repo_app.command(name="init")
def repo_init_cmd(
    ctx: typer.Context,
    target: Annotated[
        Path | None,
        typer.Option(
            "--target",
            help="Target directory (defaults to the current working directory).",
        ),
    ] = None,
    state_path: Annotated[
        Path,
        typer.Option(
            "--state-path",
            help="Path of the state file relative to the target dir (or absolute).",
        ),
    ] = Path(".ea/state.json"),
    project_code: Annotated[
        str | None,
        typer.Option(
            "--project-code",
            help="Project code (uppercase, alnum/dash, 2-16 chars).",
        ),
    ] = None,
    project_title: Annotated[
        str | None,
        typer.Option("--project-title", help="Free-form project title."),
    ] = None,
    profile: Annotated[
        list[str] | None,
        typer.Option(
            "--profile",
            help="Profiles to enable (repeatable; defaults to 'core').",
        ),
    ] = None,
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Default runtime (claude-code|opencode|generic).",
        ),
    ] = "claude-code",
    lifecycle_depth: Annotated[
        str,
        typer.Option(
            "--lifecycle-depth",
            help="Default lifecycle depth (phase|iter|wave).",
        ),
    ] = "phase",
    plugin: Annotated[
        list[str] | None,
        typer.Option("--plugin", help="Optional plugins (repeatable)."),
    ] = None,
    mcp: Annotated[
        list[str] | None,
        typer.Option("--mcp", help="Optional MCP servers (repeatable)."),
    ] = None,
    acceptance_tests: Annotated[
        bool,
        typer.Option(
            "--acceptance-tests/--no-acceptance-tests",
            help="Require tests as an acceptance gate.",
        ),
    ] = True,
    acceptance_lint: Annotated[
        bool,
        typer.Option(
            "--acceptance-lint/--no-acceptance-lint",
            help="Require lint as an acceptance gate.",
        ),
    ] = True,
    acceptance_typecheck: Annotated[
        bool,
        typer.Option(
            "--acceptance-typecheck/--no-acceptance-typecheck",
            help="Require typecheck as an acceptance gate.",
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite existing .ea/ canonical files.",
        ),
    ] = False,
) -> None:
    """Initialise a repo-scoped workspace at *target*.

    For v0.1 this delegates verbatim to :func:`eawf.cli.commands.init.init_cmd`.
    The Phase 5 W06 self-apply will fork the body so ``repo init`` records
    the optional ``--workspace`` linkage in one shot.
    """
    _init_cmd(
        ctx,
        target=target,
        state_path=state_path,
        project_code=project_code,
        project_title=project_title,
        profile=profile,
        runtime=runtime,
        lifecycle_depth=lifecycle_depth,
        plugin=plugin,
        mcp=mcp,
        acceptance_tests=acceptance_tests,
        acceptance_lint=acceptance_lint,
        acceptance_typecheck=acceptance_typecheck,
        force=force,
    )


# ---- repo link --------------------------------------------------------------


def _load_workspace_payload(workspace_state_path: Path) -> dict[str, Any]:
    """Load + JSON-decode *workspace_state_path*. Raises :class:`NotFound`."""
    if not workspace_state_path.exists():
        raise cli_errors.NotFound(f"workspace state file not found: {workspace_state_path}")
    try:
        return orjson.loads(workspace_state_path.read_bytes())  # type: ignore[no-any-return]
    except orjson.JSONDecodeError as exc:
        raise cli_errors.IntegrityViolation(
            f"corrupted workspace state at {workspace_state_path}: {exc}"
        ) from exc


def _load_repo_payload(repo_state_path: Path) -> dict[str, Any]:
    """Load + JSON-decode *repo_state_path*. Raises :class:`NotFound`."""
    if not repo_state_path.exists():
        raise cli_errors.NotFound(f"repo state file not found: {repo_state_path}")
    try:
        return orjson.loads(repo_state_path.read_bytes())  # type: ignore[no-any-return]
    except orjson.JSONDecodeError as exc:
        raise cli_errors.IntegrityViolation(
            f"corrupted repo state at {repo_state_path}: {exc}"
        ) from exc


@repo_app.command(name="link")
def repo_link_cmd(
    ctx: typer.Context,
    workspace_code: Annotated[str, typer.Argument(help="Workspace code (parent).")],
    repo_code: Annotated[str, typer.Argument(help="Repo code (child).")],
    workspace_state: Annotated[
        Path,
        typer.Option(
            "--workspace-state",
            help="Path to the workspace state.json being linked into.",
        ),
    ],
    target: Annotated[
        Path | None,
        typer.Option(
            "--target",
            help="Repo target directory (defaults to cwd).",
        ),
    ] = None,
    title: Annotated[
        str | None,
        typer.Option(
            "--title",
            help="Repo title (defaults to the repo's project title).",
        ),
    ] = None,
) -> None:
    """Cross-link a repo state and a workspace state.

    Procedure:

    1. Load the workspace state at ``--workspace-state`` and the repo state
       at ``<target>/.ea/state.json``.
    2. Append a :class:`WorkspaceRepoRef` to the workspace's
       ``repos`` mapping under *repo_code*.
    3. Stamp ``state.indexes['workspace_code']`` on the repo state so a
       later ``workspace validate`` can confirm the back-reference.
    4. Write both state files atomically under their own sibling locks.

    Exits 3 (``InvalidInput``) when codes fail the project-code regex, when
    the workspace code on disk disagrees with the *workspace_code* arg, or
    when *repo_code* is already linked.
    """
    flags: GlobalFlags = ctx.obj
    if not is_project_code(workspace_code):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid workspace code: {workspace_code!r}"),
            flags=flags,
        )
        return
    if not is_project_code(repo_code):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid repo code: {repo_code!r}"),
            flags=flags,
        )
        return

    workspace_path = workspace_state.resolve()
    target_dir = (target or Path.cwd()).resolve()
    repo_state_path = (target_dir / ".ea" / "state.json").resolve()

    try:
        ws_payload = _load_workspace_payload(workspace_path)
        repo_payload = _load_repo_payload(repo_state_path)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    ws_section = ws_payload.get("workspace")
    if not isinstance(ws_section, dict):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"state at {workspace_path} has no workspace section"),
            flags=flags,
        )
        return
    if ws_section.get("code") != workspace_code:
        cli_errors.emit_error(
            cli_errors.InvalidInput(
                f"workspace code mismatch: arg={workspace_code!r} "
                f"vs disk={ws_section.get('code')!r}"
            ),
            flags=flags,
        )
        return

    project = repo_payload.get("project") or {}
    project_code_on_repo = project.get("code") if isinstance(project, dict) else None
    if not isinstance(project_code_on_repo, str) or not project_code_on_repo:
        # Wizard-init repos may have project=None; fall back to repo_code.
        project_code_on_repo = repo_code
    repo_title = title
    if repo_title is None and isinstance(project, dict):
        candidate_title = project.get("title")
        if isinstance(candidate_title, str) and candidate_title:
            repo_title = candidate_title
    if repo_title is None:
        indexes = repo_payload.get("indexes") or {}
        if isinstance(indexes, dict):
            candidate_title = indexes.get("project_title")
            if isinstance(candidate_title, str) and candidate_title:
                repo_title = candidate_title
    if repo_title is None:
        repo_title = repo_code

    if (ws_section.get("repos") or {}).get(repo_code):
        cli_errors.emit_error(
            cli_errors.InvalidInput(
                f"repo {repo_code!r} already linked to workspace {workspace_code!r}"
            ),
            flags=flags,
        )
        return

    new_ref = WorkspaceRepoRef(
        code=repo_code,
        path=str(target_dir),
        state_urn=build_urn("state", owner=project_code_on_repo),
        project_code=project_code_on_repo,
        title=repo_title,
        status=ProjectStatus.ACTIVE,
    )
    new_repos = dict(ws_section.get("repos") or {})
    new_repos[repo_code] = new_ref.model_dump(mode="json")
    new_ws_section = WorkspaceIndex(
        code=workspace_code,
        title=ws_section.get("title", workspace_code),
        repos={k: WorkspaceRepoRef.model_validate(v) for k, v in new_repos.items()},
        current_repo_code=ws_section.get("current_repo_code"),
    ).model_dump(mode="json")
    ws_payload["workspace"] = new_ws_section
    ws_payload["updated_at"] = datetime.now(UTC).isoformat()

    repo_indexes = repo_payload.get("indexes") or {}
    if not isinstance(repo_indexes, dict):
        repo_indexes = {}
    repo_indexes["workspace_code"] = workspace_code
    repo_payload["indexes"] = repo_indexes
    repo_payload["updated_at"] = datetime.now(UTC).isoformat()

    # Validate both candidates before persisting either.
    ws_report = validate_state(ws_payload, strict_optional=False)
    if ws_report.state is None:
        cli_errors.emit_error(
            cli_errors.ValidationFailed(
                "workspace post-link payload invalid: " + "; ".join(ws_report.schema_errors[:3])
            ),
            flags=flags,
        )
        return
    repo_report = validate_state(repo_payload, strict_optional=False)
    if repo_report.state is None:
        cli_errors.emit_error(
            cli_errors.ValidationFailed(
                "repo post-link payload invalid: " + "; ".join(repo_report.schema_errors[:3])
            ),
            flags=flags,
        )
        return

    try:
        with portalock.acquire(workspace_path, timeout=5.0):
            atomic_write_json_locked(workspace_path, ws_payload)
        with portalock.acquire(repo_state_path, timeout=5.0):
            atomic_write_json_locked(repo_state_path, repo_payload)
    except portalock.LockTimeout as exc:
        cli_errors.emit_error(cli_errors.LockConflict(str(exc)), flags=flags)
        return

    emit_json_or_text(
        {
            "workspace": workspace_code,
            "repo": repo_code,
            "workspace_state": str(workspace_path),
            "repo_state": str(repo_state_path),
            "title": repo_title,
            "project_code": project_code_on_repo,
        },
        f"repo link {repo_code} -> workspace {workspace_code} "
        f"(workspace_state={workspace_path}, repo_state={repo_state_path})",
        flags=flags,
    )


__all__ = [
    "repo_app",
]
