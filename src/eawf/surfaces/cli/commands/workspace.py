"""``eawf workspace`` — workspace-scoped state init + repo linkage + registry view.

A workspace state is a parent-of-repos state document with
``scope_kind == "workspace"``, no embedded ``project``, and a
:class:`~eawf.kernel.state.models.WorkspaceIndex` populated under ``workspace``.
Each entry in ``workspace.repos`` is a
:class:`~eawf.kernel.state.models.WorkspaceRepoRef` recording the on-disk path and
canonical URN of a repo-scoped state that the workspace tracks.

Subcommands (state-file path):

- ``workspace init <code> --title <t>`` — create a workspace state document.
- ``workspace add-repo <code> --path <p>`` — append a repo link.
- ``workspace remove-repo <code>`` — drop a repo link.
- ``workspace validate`` — verify each linked repo path resolves.
- ``workspace status`` — print workspace + linked-repo summary.

Subcommands (registry-reader path, P20-I01-W05):

- ``workspace registry-list`` — enumerate the repos in
  ``~/.eawf/registry.json``.
- ``workspace registry-status`` — render the workspace dashboard
  (top strip + active-repo W02 quadrant) as text.

The registry subcommands are STRICTLY READ-ONLY — per the
``feedback_explicit_registry_only`` memory note the registry grows
only via explicit ``init`` / ``add-repo`` writes. Neither subcommand
creates, mutates, or scans the registry; both fail gracefully when
the file is absent.

Exit-code mapping mirrors the rest of the CLI (see
:mod:`eawf.surfaces.cli.errors`): bad inputs map to ``UserError``
(``kind="InvalidInput"``, exit 3), missing state to ``UserError``
(``kind="NotFound"``, exit 2), and lock contention to ``StateConflict``
(``kind="LockConflict"``, exit 5).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer

from eawf.kernel.state.enums import ProjectStatus, ScopeKind
from eawf.kernel.state.ids import is_project_code
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.runtime.lock import portalock
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)

workspace_app = typer.Typer(
    name="workspace",
    help="Workspace-scoped state and repo linkage.",
    no_args_is_help=True,
    add_completion=False,
)


# ---- internal helpers -------------------------------------------------------


def _empty_workspace_state(*, code: str, title: str) -> dict[str, Any]:
    """Return a minimal-but-valid ``state.json`` payload for a workspace.

    Mirrors the repo-scoped builder in :mod:`eawf.surfaces.cli.commands.lifecycle`
    but with ``scope_kind == "workspace"``, ``project = None`` and a populated
    :class:`WorkspaceIndex`.
    """
    return {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.WORKSPACE.value,
        "urn": build_urn("workspace", owner=code),
        "updated_at": datetime.now(UTC).isoformat(),
        "project": None,
        "current": {
            "project_code": None,
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": {
            "code": code,
            "title": title,
            "repos": {},
            "current_repo_code": None,
        },
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _read_repo_state_defaults(repo_path: Path) -> tuple[str | None, str | None]:
    """Best-effort read of ``<repo_path>/.ea/state.json`` for default fields.

    Returns ``(project_code, title)`` — any field not derivable returns
    ``None`` so the caller can fall back to CLI-provided values. Never
    raises; a malformed or missing repo state simply yields ``(None, None)``.
    """
    candidate = repo_path / ".ea" / "state.json"
    if not candidate.exists():
        return None, None
    try:
        payload: dict[str, Any] = orjson.loads(candidate.read_bytes())
    except (orjson.JSONDecodeError, OSError) as exc:
        logger.debug(f"_read_repo_state_defaults ignoring path={candidate!r} err={exc!r}")
        return None, None
    project = payload.get("project")
    indexes = payload.get("indexes") or {}
    project_code: str | None = None
    title: str | None = None
    if isinstance(project, dict):
        project_code = project.get("code")
        title = project.get("title")
    if not title and isinstance(indexes, dict):
        # ``project_title`` is the wizard's fallback when no Project record exists.
        candidate_title = indexes.get("project_title")
        if isinstance(candidate_title, str) and candidate_title:
            title = candidate_title
    return (
        project_code if isinstance(project_code, str) and project_code else None,
        title if isinstance(title, str) and title else None,
    )


# ---- workspace init ---------------------------------------------------------


@workspace_app.command(name="init")
def workspace_init_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="Workspace code (uppercase, alnum/dash).")],
    title: Annotated[str, typer.Option("--title", help="Human-readable workspace title.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing workspace state at the resolved path.",
        ),
    ] = False,
) -> None:
    """Create a workspace state document at the resolved state path.

    When ``state.json`` already exists with ``scope_kind == "workspace"`` and
    a different ``code``, the operator must pass ``--force`` to overwrite.
    """
    from pydantic import ValidationError as PydValidationError

    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid workspace code: {code!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"could not resolve state path; pass -w/--workspace or set EA_STATE: {exc}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    try:
        with portalock.acquire(state_path, timeout=5.0):
            if state_path.exists() and not force:
                cli_errors.emit_error(
                    cli_errors.UserError(
                        f"state already exists at {state_path}; pass --force to overwrite",
                        kind="InvalidInput",
                    ),
                    flags=flags,
                )
                return
            payload = _empty_workspace_state(code=code, title=title)
            try:
                # Round-trip through the model to enforce schema invariants
                # before we touch disk.
                from eawf.kernel.validate.strict import validate_state as _validate_state

                report = _validate_state(payload, strict_optional=False)
                if report.state is None:
                    raise cli_errors.ValidationError(
                        "workspace init payload invalid: " + "; ".join(report.schema_errors[:3])
                    )
            except PydValidationError as exc:
                raise cli_errors.ValidationError(str(exc)) from exc
            atomic_write_json_locked(state_path, payload)
    except portalock.LockTimeout as exc:
        cli_errors.emit_error(cli_errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        {"workspace": code, "title": title, "state_path": str(state_path)},
        f"workspace init {code} title={title!r} state={state_path}",
        flags=flags,
    )


# ---- workspace add-repo -----------------------------------------------------


@workspace_app.command(name="add-repo")
def workspace_add_repo_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="Repo code (project-code shape).")],
    path: Annotated[Path, typer.Option("--path", help="On-disk path to the repo.")],
    title: Annotated[
        str | None,
        typer.Option(
            "--title",
            help="Repo title (defaults to the linked repo's project title).",
        ),
    ] = None,
    project_code: Annotated[
        str | None,
        typer.Option(
            "--project-code",
            help="Project code recorded on the link (defaults to the linked repo's).",
        ),
    ] = None,
) -> None:
    """Append a :class:`WorkspaceRepoRef` to the workspace index.

    If ``--path/.ea/state.json`` exists and is well-formed, its project_code
    and title populate the link defaults. Otherwise the operator must
    supply both via ``--project-code`` / ``--title``.
    """
    from eawf.kernel.state.models import WorkspaceIndex, WorkspaceRepoRef
    from eawf.surfaces.cli._mutation import state_transaction

    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid repo code: {code!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return

    repo_path = Path(path).resolve()
    derived_project_code, derived_title = _read_repo_state_defaults(repo_path)
    effective_project_code = project_code or derived_project_code or code
    effective_title = title or derived_title or code
    if not is_project_code(effective_project_code):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"invalid project code on link: {effective_project_code!r}; pass --project-code",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    try:
        with state_transaction(state_path) as state:
            if state.workspace is None:
                raise cli_errors.UserError(
                    f"state at {state_path} has no workspace section; "
                    "run `eawf workspace init` first",
                    kind="InvalidInput",
                )
            if code in state.workspace.repos:
                raise cli_errors.UserError(
                    f"repo {code!r} already linked to workspace {state.workspace.code!r}",
                    kind="InvalidInput",
                )
            ref = WorkspaceRepoRef(
                code=code,
                path=str(repo_path),
                state_urn=build_urn("state", owner=effective_project_code),
                project_code=effective_project_code,
                title=effective_title,
                status=ProjectStatus.ACTIVE,
            )
            new_repos = dict(state.workspace.repos)
            new_repos[code] = ref
            state.workspace = WorkspaceIndex(
                code=state.workspace.code,
                title=state.workspace.title,
                repos=new_repos,
                current_repo_code=state.workspace.current_repo_code,
            )
            state.updated_at = datetime.now(UTC)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        {
            "workspace": _peek_workspace_code(state_path),
            "repo": code,
            "path": str(repo_path),
            "title": effective_title,
            "project_code": effective_project_code,
        },
        f"workspace add-repo {code} path={repo_path} title={effective_title!r}",
        flags=flags,
    )


# ---- workspace remove-repo --------------------------------------------------


@workspace_app.command(name="remove-repo")
def workspace_remove_repo_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="Repo code to drop from the index.")],
) -> None:
    """Drop a :class:`WorkspaceRepoRef` from the workspace index.

    For v0.1 the entry is removed outright (rather than mutated to a
    "removed" status — :class:`ProjectStatus` does not have a clean
    "unlinked" value). The audit trail of the removal lives in
    ``store/event.jsonl`` once this command lands inside ``state_transaction``.
    """
    from eawf.kernel.state.models import WorkspaceIndex
    from eawf.surfaces.cli._mutation import state_transaction

    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid repo code: {code!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    try:
        with state_transaction(state_path) as state:
            if state.workspace is None:
                raise cli_errors.UserError(
                    f"state at {state_path} has no workspace section", kind="InvalidInput"
                )
            if code not in state.workspace.repos:
                raise cli_errors.UserError(
                    f"repo {code!r} not linked to workspace {state.workspace.code!r}",
                    kind="NotFound",
                )
            new_repos = {k: v for k, v in state.workspace.repos.items() if k != code}
            new_current = (
                None
                if state.workspace.current_repo_code == code
                else state.workspace.current_repo_code
            )
            state.workspace = WorkspaceIndex(
                code=state.workspace.code,
                title=state.workspace.title,
                repos=new_repos,
                current_repo_code=new_current,
            )
            state.updated_at = datetime.now(UTC)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        {"repo": code, "removed": True},
        f"workspace remove-repo {code}",
        flags=flags,
    )


# ---- workspace validate -----------------------------------------------------


@workspace_app.command(name="validate")
def workspace_validate_cmd(ctx: typer.Context) -> None:
    """Check that every linked repo path exists and contains ``.ea/state.json``.

    Each link is reported as ``ok`` (path resolves to a directory containing
    ``.ea/state.json``), ``missing_path`` (path is absent), or
    ``missing_state`` (path exists but has no ``.ea/state.json``). The
    overall command returns exit 0; the envelope reports per-link findings
    so a wrapping script can route on them.
    """
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound"),
            flags=flags,
        )
        return
    payload = orjson.loads(state_path.read_bytes())
    workspace = payload.get("workspace")
    if not isinstance(workspace, dict):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"state at {state_path} has no workspace section", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    repos = workspace.get("repos") or {}
    findings: list[dict[str, str]] = []
    ok_count = 0
    for repo_code, ref in repos.items():
        repo_path = Path(ref["path"])
        if not repo_path.exists() or not repo_path.is_dir():
            findings.append({"repo": repo_code, "status": "missing_path", "path": str(repo_path)})
            continue
        if not (repo_path / ".ea" / "state.json").exists():
            findings.append({"repo": repo_code, "status": "missing_state", "path": str(repo_path)})
            continue
        findings.append({"repo": repo_code, "status": "ok", "path": str(repo_path)})
        ok_count += 1
    text_lines = [f"workspace validate: {workspace.get('code')!r} ({ok_count}/{len(repos)} ok)"]
    for finding in findings:
        text_lines.append(f"  {finding['repo']:8s} {finding['status']:14s} {finding['path']}")
    emit_json_or_text(
        {
            "workspace": workspace.get("code"),
            "ok": ok_count,
            "total": len(repos),
            "findings": findings,
        },
        "\n".join(text_lines),
        flags=flags,
    )


# ---- workspace status -------------------------------------------------------


@workspace_app.command(name="status")
def workspace_status_cmd(ctx: typer.Context) -> None:
    """Print the workspace metadata + linked-repos summary."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound"),
            flags=flags,
        )
        return
    payload = orjson.loads(state_path.read_bytes())
    workspace = payload.get("workspace")
    if not isinstance(workspace, dict):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"state at {state_path} has no workspace section", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    repos = workspace.get("repos") or {}
    repo_rows: list[dict[str, str]] = []
    for repo_code, ref in repos.items():
        repo_rows.append(
            {
                "code": repo_code,
                "path": ref.get("path", ""),
                "title": ref.get("title", ""),
                "project_code": ref.get("project_code", ""),
                "status": ref.get("status", ""),
            }
        )
    text_lines = [
        f"workspace {workspace.get('code')!r}: {workspace.get('title')!r}",
        f"  state_path: {state_path}",
        f"  current_repo_code: {workspace.get('current_repo_code')}",
        f"  repos ({len(repo_rows)}):",
    ]
    for row in repo_rows:
        text_lines.append(
            f"    {row['code']:8s} {row['status']:9s} {row['title']!r:30s} {row['path']}"
        )
    emit_json_or_text(
        {
            "workspace": workspace.get("code"),
            "title": workspace.get("title"),
            "state_path": str(state_path),
            "current_repo_code": workspace.get("current_repo_code"),
            "repos": repo_rows,
        },
        "\n".join(text_lines),
        flags=flags,
    )


# ---- shared helpers --------------------------------------------------------


def _peek_workspace_code(state_path: Path) -> str | None:
    """Read just the workspace code from *state_path*.

    Used after ``state_transaction`` exits so the JSON envelope can carry
    the workspace code without re-validating the entire payload. Returns
    ``None`` if the state has no workspace section.
    """
    try:
        payload = orjson.loads(state_path.read_bytes())
    except orjson.JSONDecodeError, OSError:
        return None
    workspace = payload.get("workspace")
    if not isinstance(workspace, dict):
        return None
    code = workspace.get("code")
    return code if isinstance(code, str) else None


# ---- workspace registry-list ----------------------------------


@workspace_app.command(name="registry-list")
def workspace_registry_list_cmd(
    ctx: typer.Context,
    registry_path: Annotated[
        Path | None,
        typer.Option(
            "--registry-path",
            help="Override the default ``~/.eawf/registry.json`` (mostly for tests).",
        ),
    ] = None,
) -> None:
    """Enumerate repos in ``~/.eawf/registry.json``.

    STRICTLY READ-ONLY — never grows the registry. When the file is
    missing the command exits 2 (``UserError``, ``kind="NotFound"``) with
    a hint pointing at ``eawf init`` as the explicit-growth path. When the
    file is present but malformed it exits 3 (``UserError``,
    ``kind="InvalidInput"``) with the Pydantic validation error so the
    operator can repair by hand.
    """
    from eawf.platform.registry import (
        Registry,
        RegistryReadError,
        is_stale,
        read_registry,
        registry_mtime,
    )

    flags: GlobalFlags = ctx.obj
    try:
        registry: Registry = read_registry(path=registry_path)
    except RegistryReadError as exc:
        msg = str(exc)
        if "not found" in msg:
            cli_errors.emit_error(
                cli_errors.UserError(
                    "registry not found; run `eawf init` or "
                    f"`eawf workspace add-repo` first ({exc})",
                    kind="NotFound",
                ),
                flags=flags,
            )
        else:
            cli_errors.emit_error(cli_errors.UserError(msg, kind="InvalidInput"), flags=flags)
        return
    mtime = registry_mtime(path=registry_path)
    rows: list[dict[str, Any]] = []
    for entry in sorted(registry.repos.values(), key=lambda e: e.code):
        rows.append(
            {
                "code": entry.code,
                "path": entry.path,
                "title": entry.title or entry.code,
                "stale": is_stale(entry, registry_mtime_at=mtime),
                "active": entry.code == registry.active_code,
            }
        )
    text_lines = [
        f"registry: {len(rows)} repo(s), active={registry.active_code!r}, "
        f"version={registry.version!r}"
    ]
    for row in rows:
        stale_marker = " (stale)" if row["stale"] else ""
        active_marker = " (active)" if row["active"] else ""
        text_lines.append(
            f"  {row['code']:12s} {row['title']!r:30s} {row['path']}{active_marker}{stale_marker}"
        )
    emit_json_or_text(
        {
            "registry_version": registry.version,
            "active_code": registry.active_code,
            "count": len(rows),
            "repos": rows,
        },
        "\n".join(text_lines),
        flags=flags,
    )


# ---- workspace registry-status --------------------------------


@workspace_app.command(name="registry-status")
def workspace_registry_status_cmd(
    ctx: typer.Context,
    registry_path: Annotated[
        Path | None,
        typer.Option(
            "--registry-path",
            help="Override the default ``~/.eawf/registry.json`` (mostly for tests).",
        ),
    ] = None,
    width: Annotated[
        int,
        typer.Option(
            "--width",
            help="Console width passed to the offline renderer.",
        ),
    ] = 100,
) -> None:
    """Render the workspace dashboard as text (top strip + W02 quadrant).

    STRICTLY READ-ONLY over the registry. The active repo's quadrant
    pulls panes from the W02 layout helpers so this view stays
    byte-identical to the single-repo TUI when only one entry is
    registered.

    JSON mode emits the same envelope shape as ``registry-list`` plus
    a ``rendered`` field carrying the captured text frame, so a
    downstream consumer can ingest either the structured payload or
    the pre-rendered view.
    """
    from eawf.platform.registry import (
        Registry,
        RegistryReadError,
        is_stale,
        read_registry,
        registry_mtime,
    )
    from eawf.surfaces.tui.offline import offline_render

    flags: GlobalFlags = ctx.obj
    rendered = offline_render(registry_path=registry_path, width=width)
    if flags.json_output:
        try:
            registry: Registry = read_registry(path=registry_path)
            mtime = registry_mtime(path=registry_path)
        except RegistryReadError as exc:
            emit_json_or_text(
                {
                    "registry_available": False,
                    "error": str(exc),
                    "rendered": rendered,
                },
                rendered,
                flags=flags,
            )
            return
        rows: list[dict[str, Any]] = []
        for entry in sorted(registry.repos.values(), key=lambda e: e.code):
            rows.append(
                {
                    "code": entry.code,
                    "path": entry.path,
                    "title": entry.title or entry.code,
                    "stale": is_stale(entry, registry_mtime_at=mtime),
                    "active": entry.code == registry.active_code,
                }
            )
        emit_json_or_text(
            {
                "registry_available": True,
                "registry_version": registry.version,
                "active_code": registry.active_code,
                "count": len(rows),
                "repos": rows,
                "rendered": rendered,
            },
            rendered,
            flags=flags,
        )
        return
    emit_json_or_text({"rendered": rendered}, rendered, flags=flags)


__all__ = [
    "workspace_app",
]
