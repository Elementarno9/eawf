"""``eawf repo`` — repo-scoped init + workspace linkage + registry mutators.

Daemon-internal note (P24-W10): :func:`_persist_registry` is the sole
CLI-side mutator for ``~/.eawf/registry.json``; since W10 it
dispatches through the daemon's ``registry.update`` RPC when
``daemon.proxy_enabled=True`` (the new default). The in-process arm
is retained as the V1 carve-out fallback (CI / read-only one-shot /
recovery shell / ``EAWF_DAEMONLESS=1``). After v0.5 the in-process
arm migrates under ``daemon/_internal/`` and stops being importable
from user code.


Subcommands:

- ``repo init`` — for v0.1 this is a thin alias of :func:`eawf.cli.commands.init.init_cmd`.
  The "real divergence" between repo init and the top-level ``eawf init``
  lands in Phase 5 W06 (``eawf init`` self-apply); for v0.1 the surface is
  identical so existing callers can opt into the noun-based UX without
  behaviour change.
- ``repo link <workspace-code> <repo-code>`` — append the current (or
  ``--target``) repo to a workspace state document, optionally recording
  the workspace code on the repo's ``state.indexes.workspace_code`` so a
  future ``workspace validate`` can cross-check.
- ``repo add <path>`` (P20-I01-W06) — explicitly add a repo to the
  user-scope registry (``~/.eawf/registry.json``). Idempotent on the
  ``(code, path)`` pair; TOFU prompt when the operator passes a path
  whose parent dir is not a recognised "Workspace" / "Repos" parent
  (mockup-style ``~/Repos``, ``~/Workspaces``, ``/repos``, ...). The
  TOFU gate confirms intent so the user does not register an unrelated
  directory by typo. Honours ``--no-input`` + ``--yes``.
- ``repo remove <code>`` (P20-I01-W06) — drop the entry whose
  ``code == <code>`` from the registry. Explicit-only: never auto-prunes;
  exits 2 (NotFound) when the code is absent.
- ``repo prune`` (P20-I01-W06) — drop registry entries whose on-disk
  paths no longer exist. Requires ``--yes`` (or ``--no-input``) so the
  pruner never deletes silently. Reports each dropped entry in the
  envelope so a wrapping audit picks up the trail.

Registry-growth invariant: per the ``feedback_explicit_registry_only``
memory note the registry grows ONLY via explicit ``add`` / ``init``
writes. There is no scan, no walk, no import-from-discovery. Each
mutator below validates the path argument was supplied explicitly by
the operator (Typer enforces this — no defaults expand from cwd) and
the body never enumerates parent dirs to bulk-register.

Workspace pointer attachment (legacy from ``repo link``): rather than
introduce a new ``workspace_code`` field on the :class:`Project` model
(which would alter the schema for every repo-scoped state, including
ones that are not part of any workspace), the link is recorded under
``state.indexes['workspace_code']`` — the model already declares
``indexes: dict[str, Any]`` for exactly this kind of "soft" linkage.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.lock import portalock
from eawf.state.enums import ProjectStatus
from eawf.state.ids import is_project_code
from eawf.state.urn import build as build_urn
from eawf.state.writer import atomic_write_json_locked

if TYPE_CHECKING:
    from eawf.registry import Registry, RegistryRepoEntry

logger = logging.getLogger(__name__)


#: Parent-dir names that are recognised "workspace homes" for the
#: TOFU prompt on ``repo add``. When the operator passes a path
#: whose parent dir name is in this set we treat it as expected and
#: skip the confirm. The values are intentionally generic names
#: rather than absolute paths so the comparison stays machine-
#: agnostic. Operators with non-standard layouts can bypass the
#: confirm via ``--yes`` / ``--no-input``.
_RECOGNISED_PARENT_DIR_NAMES: frozenset[str] = frozenset(
    {
        "Repos",
        "Workspaces",
        "repos",
        "workspaces",
        "Workspace",
        "workspace",
        "Code",
        "code",
        "src",
        "Source",
        "Projects",
        "projects",
    }
)

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
    from eawf.cli.commands.init import init_cmd as _init_cmd

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
    """Load + JSON-decode *workspace_state_path*.

    Raises:
        NotFound: When the workspace state file is absent.
        IntegrityViolation: When the file content is not valid JSON.
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
        NotFound: When the repo state file is absent.
        IntegrityViolation: When the file content is not valid JSON.
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

    Exits 3 (``InvalidInput``) when codes fail the project-code regex, when
    the workspace code on disk disagrees with the *workspace_code* arg, or
    when *repo_code* is already linked.
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


# ---- registry mutators (P20-I01-W06) ---------------------------------------


def _resolve_registry_path(registry_path: Path | None) -> Path:
    """Return the registry path, falling back to the user default.

    Tests pass an explicit ``tmp_path``-rooted ``--registry-path``;
    runtime callers leave it unset and pick up
    :func:`eawf.registry.default_registry_path`.
    """
    from eawf.registry import default_registry_path

    return registry_path if registry_path is not None else default_registry_path()


def _read_registry_for_write(registry_path: Path) -> Registry:
    """Load *registry_path* into a typed Registry for mutation.

    When the file is missing returns a fresh :class:`Registry` with
    version 1 and an empty repos mapping. Other read errors
    (validation failure, corrupted JSON) raise :class:`InvalidInput`
    so the operator can repair by hand before mutating.

    Raises:
        InvalidInput: When the file exists but cannot be parsed /
            validated.
    """
    from eawf.registry import Registry, RegistryReadError, read_registry

    try:
        return read_registry(path=registry_path)
    except RegistryReadError as exc:
        msg = str(exc)
        if "not found" in msg:
            # Bootstrap path: first ``repo add`` creates the registry.
            return Registry()
        raise cli_errors.UserError(msg, kind="InvalidInput") from exc


def _daemon_proxy_enabled_for_registry() -> bool:
    """Return True when daemon-proxy mode is on for registry mutations.

    Mirrors :func:`eawf.cli.commands.config._daemon_proxy_enabled` —
    the daemon-proxy gate honours both ``daemon.proxy_enabled`` in
    the merged config AND the ``EAWF_DAEMONLESS=1`` env-var override
    so callers can opt out of the proxy for one process.
    """
    import os

    if os.environ.get("EAWF_DAEMONLESS", "") == "1":
        return False
    from eawf.cli._mutation import _proxy_enabled

    return _proxy_enabled(None)


def _diff_registries_to_ops(
    before: Registry,
    after: Registry,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Derive one or more ``registry.update`` ops from the before/after diff.

    Returns a list of ``(operation, repo_id, fields)`` tuples that, when
    applied in order, transform *before* into *after*. The supported
    diff vocabulary is:

    * ``add`` — a code present in *after* but absent in *before*.
    * ``remove`` — a code present in *before* but absent in *after*.
    * ``add`` (idempotent ``set_active`` flip) — when only
      ``active_code`` changes, emit an idempotent add for the new
      active code that flags ``set_active=True``.

    The current call sites only exercise these three shapes; the
    daemon API supports ``rename`` too but the CLI never round-trips a
    rename through ``_persist_registry`` (rename is a same-code path
    swap in v0.3, not its own verb).
    """
    ops: list[tuple[str, str, dict[str, Any]]] = []
    before_codes = set(before.repos.keys())
    after_codes = set(after.repos.keys())

    added = after_codes - before_codes
    removed = before_codes - after_codes

    for code in sorted(added):
        entry = after.repos[code]
        fields: dict[str, Any] = {"path": entry.path}
        if entry.title is not None:
            fields["title"] = entry.title
        if after.active_code == code:
            fields["set_active"] = True
        ops.append(("add", code, fields))

    for code in sorted(removed):
        ops.append(("remove", code, {}))

    if (
        not added
        and not removed
        and before.active_code != after.active_code
        and after.active_code is not None
    ):
        entry = after.repos[after.active_code]
        ops.append(
            (
                "add",
                after.active_code,
                {
                    "path": entry.path,
                    "set_active": True,
                    **({"title": entry.title} if entry.title else {}),
                },
            ),
        )

    return ops


def _persist_registry(registry: Registry, registry_path: Path) -> None:
    """Write *registry* to *registry_path*.

    Since P24-W10 this helper is a thin dispatcher:

    * **Daemon-proxy arm (default).** When ``daemon.proxy_enabled``
      is ``True`` AND the daemon is reachable, diff *registry*
      against the on-disk state, then dispatch one or more
      ``registry.update`` RPCs (one per add/remove). The daemon owns
      the portalock + atomic-rename + bus publish.
    * **In-process fallback arm.** Reached when ``proxy_enabled`` is
      ``False`` (V1 carve-out), ``EAWF_DAEMONLESS=1`` is set, OR the
      daemon is unreachable. The legacy validate + lock + atomic-
      write loop runs.

    Args:
        registry: Candidate registry to persist (already mutated).
        registry_path: Absolute path to ``~/.eawf/registry.json``
            (or a test override).

    Raises:
        IntegrityViolation: Daemon required but unreachable.
        ValidationFailed: Candidate payload fails schema validation.
        LockConflict: In-process arm could not acquire the lock.
    """
    from pydantic import ValidationError as PydValidationError

    from eawf.registry import Registry, RegistryReadError, read_registry

    try:
        validated = Registry.model_validate(registry.model_dump(mode="json"))
    except PydValidationError as exc:
        raise cli_errors.ValidationError(f"registry post-mutation payload invalid: {exc}") from exc
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload = validated.model_dump(mode="json")

    if _daemon_proxy_enabled_for_registry():
        from eawf.cli._daemon_client import DaemonClient, DaemonRpcError
        from eawf.cli._mutation import _daemon_reachable

        if not _daemon_reachable():
            raise cli_errors.StateConflict(
                "daemon_required: daemon.proxy_enabled=true but the daemon is unreachable; "
                "run `eawf daemon start` or set EAWF_DAEMONLESS=1 for the V1 carve-out",
                kind="IntegrityViolation",
            )
        # Load the on-disk before-image so we can derive operations.
        try:
            on_disk = read_registry(path=registry_path)
        except RegistryReadError as exc:
            msg = str(exc)
            if "not found" in msg:
                on_disk = Registry()
            else:
                raise cli_errors.UserError(msg, kind="InvalidInput") from exc
        ops = _diff_registries_to_ops(on_disk, validated)
        # Point the daemon at the right registry file for tests; the
        # production daemon ignores the env when its own resolver
        # finds the default path. Setting the env here is process-
        # local; the with-block on the CLI side restores it on exit.
        previous = os.environ.get("EAWF_REGISTRY_PATH")
        os.environ["EAWF_REGISTRY_PATH"] = str(registry_path)
        try:
            with DaemonClient() as client:
                for operation, repo_id, fields in ops:
                    try:
                        client.registry_update(
                            operation=operation,
                            repo_id=repo_id,
                            fields=fields,
                        )
                    except DaemonRpcError as exc:
                        if exc.code == -32601:
                            # Pre-W10 daemon — drop through to in-process arm.
                            logger.debug(
                                "_persist_registry daemon-rpc method-not-found; "
                                "falling back to in-process write"
                            )
                            break
                        raise
                else:
                    return
        finally:
            if previous is None:
                os.environ.pop("EAWF_REGISTRY_PATH", None)
            else:
                os.environ["EAWF_REGISTRY_PATH"] = previous

    # In-process fallback arm (V1 carve-out / EAWF_DAEMONLESS=1 / pre-W10 daemon).
    try:
        with portalock.acquire(registry_path, timeout=5.0):
            atomic_write_json_locked(registry_path, payload)
    except portalock.LockTimeout as exc:
        raise cli_errors.StateConflict(str(exc), kind="LockConflict") from exc


def _derive_code_from_state(repo_path: Path) -> str | None:
    """Return the project code from ``<repo_path>/.ea/state.json``.

    Best-effort — a missing / unreadable / mis-shaped state file
    yields ``None`` so the caller can fall through to the
    ``--code`` operator override.
    """
    candidate = repo_path / ".ea" / "state.json"
    if not candidate.is_file():
        return None
    try:
        payload: dict[str, Any] = orjson.loads(candidate.read_bytes())
    except (orjson.JSONDecodeError, OSError) as exc:
        logger.debug(f"_derive_code_from_state unreadable path={candidate!r} err={exc!r}")
        return None
    project = payload.get("project")
    if not isinstance(project, dict):
        return None
    code = project.get("code")
    return code if isinstance(code, str) and code else None


def _derive_title_from_state(repo_path: Path) -> str | None:
    """Return the project title from ``<repo_path>/.ea/state.json`` if present."""
    candidate = repo_path / ".ea" / "state.json"
    if not candidate.is_file():
        return None
    try:
        payload: dict[str, Any] = orjson.loads(candidate.read_bytes())
    except orjson.JSONDecodeError, OSError:
        return None
    project = payload.get("project")
    if isinstance(project, dict):
        title = project.get("title")
        if isinstance(title, str) and title:
            return title
    indexes = payload.get("indexes")
    if isinstance(indexes, dict):
        title = indexes.get("project_title")
        if isinstance(title, str) and title:
            return title
    return None


def _parent_dir_is_recognised(repo_path: Path) -> bool:
    """Return ``True`` when *repo_path*'s parent dir name is in the
    recognised-workspace allowlist.

    The TOFU prompt fires only when the parent dir name is NOT in
    :data:`_RECOGNISED_PARENT_DIR_NAMES`. Operators who keep their
    repos in non-standard layouts can always opt out of the
    prompt via ``--yes`` / ``--no-input``.
    """
    parent = repo_path.parent
    if parent == repo_path:
        # Root path — no recognisable parent.
        return False
    return parent.name in _RECOGNISED_PARENT_DIR_NAMES


def _confirm_unrecognised_parent(
    *,
    repo_path: Path,
    no_input: bool,
    yes: bool,
) -> None:
    """TOFU gate for ``repo add`` when the parent dir is unrecognised.

    Behaviour:

    - ``--yes`` → skip prompt and proceed. Operator explicit opt-in.
    - ``--no-input`` (and no ``--yes``) → fail closed with
      :class:`UserDeclined`. CI / scripts must pass ``--yes``.
    - stdin is not a TTY → also fail closed.
    - Otherwise prompt; a "no" answer raises :class:`UserDeclined`.

    Raises:
        UserDeclined: When the operator declines or the policy
            forbids silent confirmation.
    """
    if yes:
        return
    if no_input:
        raise cli_errors.UserError(
            f"parent dir of {repo_path} is not a recognised workspace home "
            "and --no-input was passed without --yes; refusing to register",
            kind="UserDeclined",
        )
    if not sys.stdin.isatty():
        raise cli_errors.UserError(
            f"parent dir of {repo_path} is not a recognised workspace home "
            "and stdin is not a TTY; pass --yes to confirm",
            kind="UserDeclined",
        )
    prompt = (
        f"Path parent {repo_path.parent} is not a recognised workspace home "
        f"(expected one of {sorted(_RECOGNISED_PARENT_DIR_NAMES)}). "
        f"Add {repo_path} to the registry anyway? [y/N] "
    )
    answer = input(prompt).strip().lower()
    if answer not in {"y", "yes"}:
        raise cli_errors.UserError(
            f"user declined to register path {repo_path}", kind="UserDeclined"
        )


def _resolve_add_identity(resolved_path: Path, *, code: str | None, flags: GlobalFlags) -> str:
    """Validate the add path and resolve the repo code, exiting on failure.

    Returns:
        The validated repo code (explicit ``--code`` or derived from the
        target's ``state.json``).

    Raises:
        typer.Exit: via :func:`emit_error` when the path is absent / not a
            directory, the code cannot be derived, or the code fails the
            project-code regex.
    """
    if not resolved_path.exists():
        cli_errors.emit_error(
            cli_errors.UserError(f"path does not exist: {resolved_path}", kind="NotFound"),
            flags=flags,
        )
    if not resolved_path.is_dir():
        cli_errors.emit_error(
            cli_errors.UserError(f"path is not a directory: {resolved_path}", kind="InvalidInput"),
            flags=flags,
        )
    derived_code = code or _derive_code_from_state(resolved_path)
    if not derived_code:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"cannot derive repo code from {resolved_path}; "
                "pass --code explicitly or run `eawf init` in the target dir",
                kind="InvalidInput",
            ),
            flags=flags,
        )
    if not is_project_code(derived_code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid repo code: {derived_code!r}", kind="InvalidInput"),
            flags=flags,
        )
    return derived_code


def _persist_registry_or_exit(updated: Registry, target: Path, *, flags: GlobalFlags) -> None:
    """Persist *updated* to *target*, exiting on a CLI error.

    Raises:
        typer.Exit: via :func:`emit_error` when the persist fails.
    """
    try:
        _persist_registry(updated, target)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


def _handle_idempotent_readd(
    registry: Registry,
    existing: RegistryRepoEntry,
    *,
    derived_code: str,
    resolved_path: Path,
    target: Path,
    set_active: bool,
    flags: GlobalFlags,
) -> None:
    """Handle a same-code/same-path re-add: optionally re-activate, then report.

    Writes only when ``--set-active`` flips the active pointer; otherwise
    the call is a pure no-op that still emits the idempotent envelope.
    """
    from eawf.registry import Registry

    if set_active and registry.active_code != derived_code:
        updated = Registry(
            version=registry.version,
            updated_at=datetime.now(UTC),
            active_code=derived_code,
            repos=dict(registry.repos),
        )
        _persist_registry_or_exit(updated, target, flags=flags)
    emit_json_or_text(
        {
            "code": derived_code,
            "path": str(resolved_path),
            "title": existing.title or derived_code,
            "registry_path": str(target),
            "added": False,
            "active": (set_active or registry.active_code == derived_code),
        },
        f"repo add {derived_code} (idempotent — already registered at {resolved_path})",
        flags=flags,
    )


def _insert_new_repo_entry(
    registry: Registry,
    *,
    derived_code: str,
    resolved_path: Path,
    effective_title: str,
    target: Path,
    set_active: bool,
    flags: GlobalFlags,
) -> None:
    """Insert a fresh registry entry, persist, and emit the success envelope."""
    from eawf.registry import Registry, RegistryRepoEntry

    new_entry = RegistryRepoEntry(
        code=derived_code,
        path=str(resolved_path),
        title=effective_title,
        last_seen=datetime.now(UTC),
    )
    new_repos = dict(registry.repos)
    new_repos[derived_code] = new_entry
    updated = Registry(
        version=registry.version,
        updated_at=datetime.now(UTC),
        active_code=derived_code if set_active else registry.active_code,
        repos=new_repos,
    )
    _persist_registry_or_exit(updated, target, flags=flags)
    emit_json_or_text(
        {
            "code": derived_code,
            "path": str(resolved_path),
            "title": effective_title,
            "registry_path": str(target),
            "added": True,
            "active": (set_active or registry.active_code == derived_code),
        },
        f"repo add {derived_code} path={resolved_path} title={effective_title!r}",
        flags=flags,
    )


@repo_app.command(name="add")
def repo_add_cmd(
    ctx: typer.Context,
    path: Annotated[
        Path,
        typer.Argument(
            help="Absolute path to the repo's working tree (must exist).",
        ),
    ],
    code: Annotated[
        str | None,
        typer.Option(
            "--code",
            help=(
                "Repo code override. Defaults to the project code in "
                "<path>/.ea/state.json; required when the state file is absent."
            ),
        ),
    ] = None,
    title: Annotated[
        str | None,
        typer.Option(
            "--title",
            help="Display title override. Defaults to the project title or code.",
        ),
    ] = None,
    set_active: Annotated[
        bool,
        typer.Option(
            "--set-active/--no-set-active",
            help="Mark the added repo as the registry's active entry.",
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Skip the TOFU prompt for unrecognised parent dirs.",
        ),
    ] = False,
    registry_path: Annotated[
        Path | None,
        typer.Option(
            "--registry-path",
            help="Override the default ``~/.eawf/registry.json`` (mostly for tests).",
        ),
    ] = None,
) -> None:
    """Explicitly add a repo to the user-scope registry.

    Behaviour:

    1. Resolve *path* (must exist as a directory).
    2. Derive the repo code from ``<path>/.ea/state.json`` when
       ``--code`` is not supplied.
    3. TOFU confirm when the parent dir name is not in the
       recognised-workspace allowlist (unless ``--yes`` /
       ``--no-input`` opt out).
    4. Idempotent insert: if ``(code, path)`` already matches a
       registry entry, succeed without writing.
    5. Persist atomically via :func:`_persist_registry`.

    Exit codes:

    - 0 — success (idempotent re-add included).
    - 2 (NotFound) — *path* does not exist.
    - 3 (InvalidInput) — missing/invalid code, registry corrupted.
    - 6 (UserDeclined) — TOFU gate declined.
    """
    flags: GlobalFlags = ctx.obj
    resolved_path = path.resolve()
    derived_code = _resolve_add_identity(resolved_path, code=code, flags=flags)
    effective_title = title or _derive_title_from_state(resolved_path) or derived_code

    try:
        if not _parent_dir_is_recognised(resolved_path):
            _confirm_unrecognised_parent(
                repo_path=resolved_path,
                no_input=flags.no_input,
                yes=yes,
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)

    target = _resolve_registry_path(registry_path)
    try:
        registry = _read_registry_for_write(target)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)

    existing = registry.repos.get(derived_code)
    if existing is not None and existing.path == str(resolved_path):
        _handle_idempotent_readd(
            registry,
            existing,
            derived_code=derived_code,
            resolved_path=resolved_path,
            target=target,
            set_active=set_active,
            flags=flags,
        )
        return
    if existing is not None and existing.path != str(resolved_path):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"repo code {derived_code!r} already registered at {existing.path}; "
                f"refusing to overwrite with {resolved_path}",
                kind="InvalidInput",
            ),
            flags=flags,
        )

    _insert_new_repo_entry(
        registry,
        derived_code=derived_code,
        resolved_path=resolved_path,
        effective_title=effective_title,
        target=target,
        set_active=set_active,
        flags=flags,
    )


@repo_app.command(name="remove")
def repo_remove_cmd(
    ctx: typer.Context,
    code: Annotated[
        str,
        typer.Argument(
            help="Repo code to drop from the registry (explicit; no implicit auto-resolve).",
        ),
    ],
    registry_path: Annotated[
        Path | None,
        typer.Option(
            "--registry-path",
            help="Override the default ``~/.eawf/registry.json`` (mostly for tests).",
        ),
    ] = None,
) -> None:
    """Drop the entry whose ``code == <code>`` from the registry.

    Strictly explicit: this command never resolves *code* from the
    current working directory and never bulk-removes. Exits 2
    (NotFound) when the registry has no such entry so a typo cannot
    silently drop the wrong row.

    Exit codes:

    - 0 — success.
    - 2 (NotFound) — registry missing OR no entry with *code*.
    - 3 (InvalidInput) — invalid code shape, registry corrupted.
    """
    from eawf.registry import Registry, RegistryReadError, read_registry

    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid repo code: {code!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    target = _resolve_registry_path(registry_path)
    try:
        registry = read_registry(path=target)
    except RegistryReadError as exc:
        msg = str(exc)
        if "not found" in msg:
            cli_errors.emit_error(
                cli_errors.UserError(
                    f"registry not found at {target}; nothing to remove", kind="NotFound"
                ),
                flags=flags,
            )
        else:
            cli_errors.emit_error(cli_errors.UserError(msg, kind="InvalidInput"), flags=flags)
        return
    if code not in registry.repos:
        cli_errors.emit_error(
            cli_errors.UserError(f"repo {code!r} not registered in {target}", kind="NotFound"),
            flags=flags,
        )
        return
    dropped = registry.repos[code]
    new_repos = {k: v for k, v in registry.repos.items() if k != code}
    new_active = None if registry.active_code == code else registry.active_code
    updated = Registry(
        version=registry.version,
        updated_at=datetime.now(UTC),
        active_code=new_active,
        repos=new_repos,
    )
    try:
        _persist_registry(updated, target)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        {
            "code": code,
            "removed": True,
            "path": dropped.path,
            "registry_path": str(target),
            "remaining": len(new_repos),
        },
        f"repo remove {code} (was at {dropped.path})",
        flags=flags,
    )


@repo_app.command(name="prune")
def repo_prune_cmd(
    ctx: typer.Context,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Confirm the prune. Required when stdin is a TTY without --no-input.",
        ),
    ] = False,
    registry_path: Annotated[
        Path | None,
        typer.Option(
            "--registry-path",
            help="Override the default ``~/.eawf/registry.json`` (mostly for tests).",
        ),
    ] = None,
) -> None:
    """Drop registry entries whose on-disk paths no longer exist.

    Walks ``registry.repos`` and reports each entry whose ``path``
    fails ``Path.exists()``. The prune is staged: nothing writes
    until either ``--yes`` or ``--no-input`` confirms the intent.
    The TTY-without-confirm branch fails closed with
    :class:`UserDeclined` so the operator never deletes silently.

    When ``--yes`` is passed alongside ``--no-input`` the command
    completes silently (suitable for CI cleanup hooks).

    Exit codes:

    - 0 — success (zero or more entries pruned).
    - 2 (NotFound) — registry file is missing.
    - 3 (InvalidInput) — registry corrupted / invalid schema.
    - 6 (UserDeclined) — confirmation gate declined.
    """
    from eawf.registry import Registry, RegistryReadError, read_registry

    flags: GlobalFlags = ctx.obj
    target = _resolve_registry_path(registry_path)
    try:
        registry = read_registry(path=target)
    except RegistryReadError as exc:
        msg = str(exc)
        if "not found" in msg:
            cli_errors.emit_error(
                cli_errors.UserError(
                    f"registry not found at {target}; nothing to prune", kind="NotFound"
                ),
                flags=flags,
            )
        else:
            cli_errors.emit_error(cli_errors.UserError(msg, kind="InvalidInput"), flags=flags)
        return
    dropped: list[dict[str, str]] = []
    survivors: dict[str, RegistryRepoEntry] = {}
    for entry_code in sorted(registry.repos):
        entry = registry.repos[entry_code]
        if Path(entry.path).exists():
            survivors[entry_code] = entry
        else:
            dropped.append({"code": entry_code, "path": entry.path})
    if not dropped:
        emit_json_or_text(
            {
                "pruned": [],
                "count": 0,
                "registry_path": str(target),
                "remaining": len(survivors),
            },
            f"repo prune: no missing paths (registry has {len(survivors)} entries)",
            flags=flags,
        )
        return
    if not yes:
        if flags.no_input:
            cli_errors.emit_error(
                cli_errors.UserError(
                    f"--no-input passed without --yes; refusing to prune "
                    f"{len(dropped)} entr{'y' if len(dropped) == 1 else 'ies'}",
                    kind="UserDeclined",
                ),
                flags=flags,
            )
            return
        if not sys.stdin.isatty():
            cli_errors.emit_error(
                cli_errors.UserError(
                    f"stdin is not a TTY and --yes was not passed; refusing to prune "
                    f"{len(dropped)} entr{'y' if len(dropped) == 1 else 'ies'}",
                    kind="UserDeclined",
                ),
                flags=flags,
            )
            return
        codes = ", ".join(d["code"] for d in dropped)
        answer = (
            input(
                f"Prune {len(dropped)} entr{'y' if len(dropped) == 1 else 'ies'} "
                f"({codes}) whose paths are missing? [y/N] "
            )
            .strip()
            .lower()
        )
        if answer not in {"y", "yes"}:
            cli_errors.emit_error(
                cli_errors.UserError("user declined prune", kind="UserDeclined"),
                flags=flags,
            )
            return
    new_active = registry.active_code if registry.active_code in survivors else None
    updated = Registry(
        version=registry.version,
        updated_at=datetime.now(UTC),
        active_code=new_active,
        repos=survivors,
    )
    try:
        _persist_registry(updated, target)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        {
            "pruned": dropped,
            "count": len(dropped),
            "registry_path": str(target),
            "remaining": len(survivors),
        },
        (
            f"repo prune: dropped {len(dropped)} entr{'y' if len(dropped) == 1 else 'ies'} "
            f"({', '.join(d['code'] for d in dropped)}); "
            f"{len(survivors)} remaining"
        ),
        flags=flags,
    )


__all__ = [
    "repo_app",
]
