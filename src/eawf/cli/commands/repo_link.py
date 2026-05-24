"""``repo link`` — cross-link a repo state and a workspace state.

Split out of :mod:`eawf.cli.commands.repo` (P27-I05-W09). The
:data:`repo_app` Typer group lives in the parent module; this module
attaches the ``repo link`` handler via ``@repo_app.command(...)`` and
owns the link-payload loaders / resolvers / attachers / validators.

Workspace pointer attachment: rather than introduce a new
``workspace_code`` field on the :class:`Project` model, the link is
recorded under ``state.indexes['workspace_code']`` — the model already
declares ``indexes: dict[str, Any]`` for exactly this kind of "soft"
linkage.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.commands.repo import repo_app
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.lock import portalock
from eawf.state.enums import ProjectStatus
from eawf.state.ids import is_project_code
from eawf.state.urn import build as build_urn
from eawf.state.writer import atomic_write_json_locked

logger = logging.getLogger(__name__)


def _load_workspace_payload(workspace_state_path: Path) -> dict[str, Any]:
    """Load + JSON-decode *workspace_state_path*.

    Raises:
        UserError: When the workspace state file is absent
            (``kind="NotFound"``).
        StateConflict: When the file content is not valid JSON
            (``kind="IntegrityViolation"``).
    """
    if not workspace_state_path.exists():
        raise cli_errors.UserError(
            f"workspace state file not found: {workspace_state_path}", kind="NotFound"
        )
    try:
        return orjson.loads(workspace_state_path.read_bytes())  # type: ignore[no-any-return]
    except orjson.JSONDecodeError as exc:
        raise cli_errors.StateConflict(
            f"corrupted workspace state at {workspace_state_path}: {exc}", kind="IntegrityViolation"
        ) from exc


def _load_repo_payload(repo_state_path: Path) -> dict[str, Any]:
    """Load + JSON-decode *repo_state_path*.

    Raises:
        UserError: When the repo state file is absent (``kind="NotFound"``).
        StateConflict: When the file content is not valid JSON
            (``kind="IntegrityViolation"``).
    """
    if not repo_state_path.exists():
        raise cli_errors.UserError(f"repo state file not found: {repo_state_path}", kind="NotFound")
    try:
        return orjson.loads(repo_state_path.read_bytes())  # type: ignore[no-any-return]
    except orjson.JSONDecodeError as exc:
        raise cli_errors.StateConflict(
            f"corrupted repo state at {repo_state_path}: {exc}", kind="IntegrityViolation"
        ) from exc


def _resolve_repo_project_code(project: object, *, repo_code: str) -> str:
    """Return the repo's project code, falling back to *repo_code*.

    Wizard-init repos may carry ``project=None`` or a blank code; in
    that case the repo code stands in.
    """
    code = project.get("code") if isinstance(project, dict) else None
    if isinstance(code, str) and code:
        return code
    return repo_code


def _resolve_repo_title(
    title: str | None,
    *,
    project: object,
    repo_payload: dict[str, Any],
    repo_code: str,
) -> str:
    """Resolve the repo display title via the fallback ladder.

    Precedence: explicit *title* arg, then the repo's project title,
    then ``indexes.project_title``, then the repo code as last resort.
    """
    if title is not None:
        return title
    if isinstance(project, dict):
        candidate = project.get("title")
        if isinstance(candidate, str) and candidate:
            return candidate
    indexes = repo_payload.get("indexes") or {}
    if isinstance(indexes, dict):
        candidate = indexes.get("project_title")
        if isinstance(candidate, str) and candidate:
            return candidate
    return repo_code


def _attach_repo_to_workspace_payload(
    ws_payload: dict[str, Any],
    *,
    ws_section: dict[str, Any],
    workspace_code: str,
    repo_code: str,
    target_dir: Path,
    repo_title: str,
    project_code_on_repo: str,
) -> None:
    """Append the new repo ref to *ws_payload*'s workspace section in place."""
    from eawf.state.models import WorkspaceIndex, WorkspaceRepoRef

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
    ws_payload["workspace"] = WorkspaceIndex(
        code=workspace_code,
        title=ws_section.get("title", workspace_code),
        repos={k: WorkspaceRepoRef.model_validate(v) for k, v in new_repos.items()},
        current_repo_code=ws_section.get("current_repo_code"),
    ).model_dump(mode="json")
    ws_payload["updated_at"] = datetime.now(UTC).isoformat()


def _stamp_workspace_code_on_repo_payload(
    repo_payload: dict[str, Any], *, workspace_code: str
) -> None:
    """Record the back-reference workspace code on *repo_payload* in place."""
    repo_indexes = repo_payload.get("indexes") or {}
    if not isinstance(repo_indexes, dict):
        repo_indexes = {}
    repo_indexes["workspace_code"] = workspace_code
    repo_payload["indexes"] = repo_indexes
    repo_payload["updated_at"] = datetime.now(UTC).isoformat()


def _validate_link_payload(payload: dict[str, Any], *, label: str, flags: GlobalFlags) -> None:
    """Validate one post-link state candidate, exiting on schema failure.

    Raises:
        typer.Exit: via :func:`emit_error` when the payload fails strict
            validation.
    """
    from eawf.validate.strict import validate_state

    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        cli_errors.emit_error(
            cli_errors.ValidationError(
                f"{label} post-link payload invalid: " + "; ".join(report.schema_errors[:3])
            ),
            flags=flags,
        )


def _persist_link_payloads(
    ws_payload: dict[str, Any],
    repo_payload: dict[str, Any],
    *,
    workspace_path: Path,
    repo_state_path: Path,
    flags: GlobalFlags,
) -> None:
    """Write both link payloads atomically under their sibling locks.

    Raises:
        typer.Exit: via :func:`emit_error` when either lock is contended.
    """
    try:
        with portalock.acquire(workspace_path, timeout=5.0):
            atomic_write_json_locked(workspace_path, ws_payload)
        with portalock.acquire(repo_state_path, timeout=5.0):
            atomic_write_json_locked(repo_state_path, repo_payload)
    except portalock.LockTimeout as exc:
        cli_errors.emit_error(cli_errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)


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

    Exits 3 (``UserError``, ``kind="InvalidInput"``) when codes fail the
    project-code regex, when the workspace code on disk disagrees with the
    *workspace_code* arg, or when *repo_code* is already linked.
    """

    flags: GlobalFlags = ctx.obj
    if not is_project_code(workspace_code):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"invalid workspace code: {workspace_code!r}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    if not is_project_code(repo_code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid repo code: {repo_code!r}", kind="InvalidInput"),
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
            cli_errors.UserError(
                f"state at {workspace_path} has no workspace section", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    if ws_section.get("code") != workspace_code:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"workspace code mismatch: arg={workspace_code!r} "
                f"vs disk={ws_section.get('code')!r}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    project = repo_payload.get("project") or {}
    project_code_on_repo = _resolve_repo_project_code(project, repo_code=repo_code)
    repo_title = _resolve_repo_title(
        title, project=project, repo_payload=repo_payload, repo_code=repo_code
    )

    if (ws_section.get("repos") or {}).get(repo_code):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"repo {repo_code!r} already linked to workspace {workspace_code!r}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    _attach_repo_to_workspace_payload(
        ws_payload,
        ws_section=ws_section,
        workspace_code=workspace_code,
        repo_code=repo_code,
        target_dir=target_dir,
        repo_title=repo_title,
        project_code_on_repo=project_code_on_repo,
    )
    _stamp_workspace_code_on_repo_payload(repo_payload, workspace_code=workspace_code)

    _validate_link_payload(ws_payload, label="workspace", flags=flags)
    _validate_link_payload(repo_payload, label="repo", flags=flags)
    _persist_link_payloads(
        ws_payload,
        repo_payload,
        workspace_path=workspace_path,
        repo_state_path=repo_state_path,
        flags=flags,
    )

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
