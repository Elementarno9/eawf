"""``eawf profile`` Typer sub-app.

Two verbs:

- ``profile new <name> [--inherit <parent>] [--force]`` scaffolds
  ``.ea/profiles/<name>.yaml`` with ``extends: <parent>`` (when given).
- ``profile validate [<name>|--all]`` runs the layered loader pipeline
  and surfaces trust ledger + schema status.

Both commands respect ``--no-input``: an untrusted overlay profile or a
hash-drift event short-circuits to exit 4 (``VALIDATION_FAILED``) under
``--no-input``, matching the v0.1 contract for non-interactive surfaces.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli import exit_codes
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)


profile_app = typer.Typer(
    name="profile",
    help="Profile body scaffolding + trust ledger management.",
    no_args_is_help=True,
    add_completion=False,
)


def _resolve_workspace(flags: GlobalFlags) -> Path:
    """Return the workspace root (the dir holding ``.ea/``).

    The CLI handler routes through ``--workspace`` or pwd-upward; if no
    workspace is resolvable, ``profile new`` writes into the current
    directory and ``profile validate --all`` falls back to user + builtin
    overlays only.
    """
    if flags.workspace is not None:
        return Path(flags.workspace).resolve()
    return Path.cwd().resolve()


def _config_path(workspace: Path) -> Path:
    return workspace / ".ea" / "config.yaml"


@profile_app.command("new")
def profile_new_cmd(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Profile name (YAML stem).")],
    inherit: Annotated[
        str | None,
        typer.Option(
            "--inherit",
            help="Parent profile id to record as the body's ``extends`` field.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing workspace profile of the same name.",
        ),
    ] = False,
    description: Annotated[
        str,
        typer.Option("--description", help="Profile description string."),
    ] = "",
) -> None:
    """Scaffold a workspace profile at ``.ea/profiles/<name>.yaml``."""
    import yaml

    from eawf.profiles import discovery as profiles_discovery
    from eawf.profiles import trust as profiles_trust

    flags: GlobalFlags = ctx.obj
    workspace = _resolve_workspace(flags)
    if profiles_trust.is_bundled(name) and not force:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"name {name!r} collides with a bundled profile; pass --force to override",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    if inherit is not None and inherit not in profiles_discovery.list_profiles_all(
        workspace=workspace
    ):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"unknown parent profile {inherit!r}; "
                f"choose from {list(profiles_discovery.list_profiles_all(workspace=workspace))}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    target = profiles_discovery.workspace_profiles_dir(workspace) / f"{name}.yaml"
    if target.exists() and not force:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"profile already exists at {target}; pass --force to overwrite",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    body: dict[str, Any] = {
        "name": name,
        "version": "1.0",
        "description": description,
    }
    if inherit is not None:
        body["extends"] = inherit
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(body, sort_keys=True), encoding="utf-8")
    payload = {
        "name": name,
        "path": str(target),
        "extends": inherit,
    }
    emit_json_or_text(
        payload,
        f"profile new {name} path={target} extends={inherit!r}",
        flags=flags,
    )


@profile_app.command("validate")
def profile_validate_cmd(
    ctx: typer.Context,
    name: Annotated[
        str | None,
        typer.Argument(help="Profile id to validate; omit with --all."),
    ] = None,
    validate_all: Annotated[
        bool,
        typer.Option("--all", help="Validate every discoverable profile."),
    ] = False,
) -> None:
    """Validate a profile (or every profile) against the layered loader."""
    from eawf.profiles import discovery as profiles_discovery
    from eawf.profiles import trust as profiles_trust

    flags: GlobalFlags = ctx.obj
    if (name is None) == (not validate_all):
        cli_errors.emit_error(
            cli_errors.UserError(
                "exactly one of <name> or --all must be provided", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    workspace = _resolve_workspace(flags)
    ledger = profiles_trust.load_trust_ledger(_config_path(workspace))
    ids: list[str]
    if validate_all:
        ids = list(profiles_discovery.list_profiles_all(workspace=workspace))
    else:
        assert name is not None
        ids = [name]
    failures: list[dict[str, str]] = []
    for pid in ids:
        try:
            loc = profiles_discovery.discover_profile(pid, workspace=workspace)
        except cli_errors.UserError as exc:
            failures.append({"profile": pid, "code": "unknown", "message": str(exc)})
            continue
        try:
            profiles_discovery.load_profile_with_discovery(pid, workspace=workspace)
        except cli_errors.ValidationError as exc:
            failures.append({"profile": pid, "code": "schema_rejected", "message": str(exc)})
            continue
        try:
            profiles_trust.verify_trust(
                pid,
                path=loc.path,
                ledger=ledger,
                no_input=flags.no_input,
            )
        except profiles_trust.UntrustedProfileError as exc:
            failures.append({"profile": pid, "code": "untrusted", "message": str(exc)})
        except profiles_trust.TrustDriftError as exc:
            failures.append({"profile": pid, "code": "trust_drift", "message": str(exc)})
    payload = {
        "validated": list(ids),
        "failures": failures,
        "ok": not failures,
    }
    text = (
        f"profile validate {'--all' if validate_all else name} "
        f"checked={len(ids)} failures={len(failures)}"
    )
    emit_json_or_text(payload, text, flags=flags)
    if failures:
        raise typer.Exit(code=exit_codes.VALIDATION_FAILED)
