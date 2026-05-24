"""``repo add`` / ``repo remove`` / ``repo prune`` registry mutators.

Split out of :mod:`eawf.cli.commands.repo` (P27-I05-W09). The
:data:`repo_app` Typer group and the shared registry-persist core
(``_persist_registry`` + ``_resolve_registry_path`` +
``_read_registry_for_write`` + ``_persist_registry_or_exit``) live in
the parent module; this module attaches the three user-scope registry
mutator handlers via ``@repo_app.command(...)`` and owns the
identity-derivation + TOFU-confirm + insert/idempotent helpers.

Registry-growth invariant: per the ``feedback_explicit_registry_only``
memory note the registry grows ONLY via explicit ``add`` / ``init``
writes. There is no scan, no walk, no import-from-discovery.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.commands.repo import (
    _RECOGNISED_PARENT_DIR_NAMES,
    _persist_registry,
    _persist_registry_or_exit,
    _read_registry_for_write,
    _resolve_registry_path,
    repo_app,
)
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.kernel.state.ids import is_project_code

if TYPE_CHECKING:
    from eawf.registry import Registry, RegistryRepoEntry

logger = logging.getLogger(__name__)


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
      :class:`UserError` (``kind="UserDeclined"``). CI / scripts must
      pass ``--yes``.
    - stdin is not a TTY → also fail closed.
    - Otherwise prompt; a "no" answer raises :class:`UserError`
      (``kind="UserDeclined"``).

    Raises:
        UserError: When the operator declines or the policy
            forbids silent confirmation (``kind="UserDeclined"``).
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
    - 2 (UserError, ``kind="NotFound"``) — *path* does not exist.
    - 3 (UserError, ``kind="InvalidInput"``) — missing/invalid code,
      registry corrupted.
    - 6 (UserError, ``kind="UserDeclined"``) — TOFU gate declined.
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
    (UserError, ``kind="NotFound"``) when the registry has no such entry
    so a typo cannot silently drop the wrong row.

    Exit codes:

    - 0 — success.
    - 2 (UserError, ``kind="NotFound"``) — registry missing OR no entry
      with *code*.
    - 3 (UserError, ``kind="InvalidInput"``) — invalid code shape,
      registry corrupted.
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


def _confirm_prune(dropped: list[dict[str, str]], *, yes: bool, no_input: bool) -> None:
    """Staged confirmation gate for ``repo prune``.

    No-ops when ``--yes`` was passed. Otherwise fails closed with
    :class:`UserError` (``kind="UserDeclined"``) in ``--no-input`` mode
    or when stdin is not a TTY; in an interactive TTY it prompts and
    raises on a "no" answer.

    Raises:
        UserError: When the operator declines or policy forbids silent
            confirmation (``kind="UserDeclined"``).
    """
    if yes:
        return
    plural = "y" if len(dropped) == 1 else "ies"
    if no_input:
        raise cli_errors.UserError(
            f"--no-input passed without --yes; refusing to prune {len(dropped)} entr{plural}",
            kind="UserDeclined",
        )
    if not sys.stdin.isatty():
        raise cli_errors.UserError(
            f"stdin is not a TTY and --yes was not passed; refusing to prune "
            f"{len(dropped)} entr{plural}",
            kind="UserDeclined",
        )
    codes = ", ".join(d["code"] for d in dropped)
    answer = (
        input(f"Prune {len(dropped)} entr{plural} ({codes}) whose paths are missing? [y/N] ")
        .strip()
        .lower()
    )
    if answer not in {"y", "yes"}:
        raise cli_errors.UserError("user declined prune", kind="UserDeclined")


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
    :class:`UserError` (``kind="UserDeclined"``) so the operator never
    deletes silently.

    When ``--yes`` is passed alongside ``--no-input`` the command
    completes silently (suitable for CI cleanup hooks).

    Exit codes:

    - 0 — success (zero or more entries pruned).
    - 2 (UserError, ``kind="NotFound"``) — registry file is missing.
    - 3 (UserError, ``kind="InvalidInput"``) — registry corrupted /
      invalid schema.
    - 6 (UserError, ``kind="UserDeclined"``) — confirmation gate declined.
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
    try:
        _confirm_prune(dropped, yes=yes, no_input=flags.no_input)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
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
