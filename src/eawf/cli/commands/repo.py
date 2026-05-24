"""``eawf repo`` — repo-scoped init + workspace linkage + registry mutators.

This module is the facade for the ``repo`` command group (P27-I05-W09).
It owns the :data:`repo_app` Typer group, the shared registry-persist
core (``_persist_registry`` + its diff / read / daemon-proxy helpers),
the recognised-parent allowlist, and the thin ``repo init`` alias. The
concrete verb bodies live in two sibling modules:

- :mod:`eawf.cli.commands.repo_link` — ``repo link`` cross-link of a
  repo state and a workspace state.
- :mod:`eawf.cli.commands.repo_registry` — ``repo add`` / ``repo
  remove`` / ``repo prune`` user-scope registry mutators.

Each sibling imports the app + shared helpers from this module and
attaches its handlers via ``@repo_app.command(...)``. Importing this
module imports the siblings (at the bottom, after every shared symbol
is defined), so the decorators run and ``repo_app`` carries its full
verb set. Existing import sites (``registry.py`` mounting
``repo_app``; the ``spec`` docstring referencing
``_daemon_proxy_enabled_for_registry``) keep resolving from this module
unchanged.

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
  exits 2 (UserError, ``kind="NotFound"``) when the code is absent.
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
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.lock import portalock

if TYPE_CHECKING:
    from eawf.registry import Registry

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


# ---- registry persist core (shared by repo_registry siblings) --------------


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
    (validation failure, corrupted JSON) raise :class:`UserError`
    (``kind="InvalidInput"``) so the operator can repair by hand before
    mutating.

    Raises:
        UserError: When the file exists but cannot be parsed /
            validated (``kind="InvalidInput"``).
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


def _persist_registry_via_daemon(validated: Registry, registry_path: Path) -> bool:
    """Dispatch the registry diff to the daemon's ``registry.update`` RPC.

    Loads the on-disk before-image, diffs *validated* against it, and
    dispatches one RPC per add/remove op. Returns ``True`` when the
    daemon handled the write (the caller skips the in-process arm) and
    ``False`` when the daemon reported method-not-found (a pre-W10
    daemon — the caller falls through to the in-process write).

    Raises:
        StateConflict: Daemon required but unreachable
            (``kind="IntegrityViolation"``).
        UserError: On-disk registry exists but cannot be read
            (``kind="InvalidInput"``).
    """
    from eawf.cli._daemon_client import DaemonClient, DaemonRpcError
    from eawf.cli._mutation import _daemon_reachable
    from eawf.registry import Registry, RegistryReadError, read_registry

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
                        return False
                    raise
        return True
    finally:
        if previous is None:
            os.environ.pop("EAWF_REGISTRY_PATH", None)
        else:
            os.environ["EAWF_REGISTRY_PATH"] = previous


def _persist_registry(registry: Registry, registry_path: Path) -> None:
    """Write *registry* to *registry_path*.

    Since P24-W10 this helper is a thin dispatcher:

    * **Daemon-proxy arm (default).** When ``daemon.proxy_enabled``
      is ``True`` AND the daemon is reachable, diff *registry*
      against the on-disk state, then dispatch one or more
      ``registry.update`` RPCs (one per add/remove) via
      :func:`_persist_registry_via_daemon`. The daemon owns the
      portalock + atomic-rename + bus publish.
    * **In-process fallback arm.** Reached when ``proxy_enabled`` is
      ``False`` (V1 carve-out), ``EAWF_DAEMONLESS=1`` is set, OR the
      daemon is unreachable. The legacy validate + lock + atomic-
      write loop runs.

    Args:
        registry: Candidate registry to persist (already mutated).
        registry_path: Absolute path to ``~/.eawf/registry.json``
            (or a test override).

    Raises:
        StateConflict: Daemon required but unreachable
            (``kind="IntegrityViolation"``); or in-process arm could not
            acquire the lock (``kind="LockConflict"``).
        ValidationError: Candidate payload fails schema validation.
    """
    from pydantic import ValidationError as PydValidationError

    from eawf.registry import Registry

    try:
        validated = Registry.model_validate(registry.model_dump(mode="json"))
    except PydValidationError as exc:
        raise cli_errors.ValidationError(f"registry post-mutation payload invalid: {exc}") from exc
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload = validated.model_dump(mode="json")

    if _daemon_proxy_enabled_for_registry() and _persist_registry_via_daemon(
        validated, registry_path
    ):
        return

    # In-process fallback arm (V1 carve-out / EAWF_DAEMONLESS=1 / pre-W10 daemon).
    try:
        with portalock.acquire(registry_path, timeout=5.0):
            atomic_write_json_locked(registry_path, payload)
    except portalock.LockTimeout as exc:
        raise cli_errors.StateConflict(str(exc), kind="LockConflict") from exc


def _persist_registry_or_exit(updated: Registry, target: Path, *, flags: GlobalFlags) -> None:
    """Persist *updated* to *target*, exiting on a CLI error.

    Raises:
        typer.Exit: via :func:`emit_error` when the persist fails.
    """
    try:
        _persist_registry(updated, target)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


# ---- command registration ---------------------------------------------------
# Importing the sibling modules runs their ``@repo_app.command(...)``
# decorators so the app above carries its full verb set. The imports sit
# at the bottom, after every shared symbol is defined, so the siblings
# can import the app and helpers from this module without a circular-
# import failure.
from eawf.cli.commands import repo_link as _repo_link  # noqa: E402, F401
from eawf.cli.commands import repo_registry as _repo_registry  # noqa: E402, F401

__all__ = [
    "repo_app",
]
