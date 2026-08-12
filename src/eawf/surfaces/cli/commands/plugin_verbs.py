"""``eawf plugin`` verb handlers (install / update / doctor / package / sync).

Split out of :mod:`eawf.surfaces.cli.commands.plugin`. The
:data:`plugin_app` Typer group, the conflict gates, and the
scope / runtime validators live in the parent module; the JSON / text
renderers live in :mod:`eawf.surfaces.cli.commands.plugin_render`. This module
attaches the five command bodies via ``@plugin_app.command(...)`` and
keeps every ``eawf.runtime.runtimes.*`` import inside the handler bodies so the
command-tree build path stays off the import-budget heavy graph.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.commands.plugin import (
    _VALID_SCOPES,
    Scope,
    _default_package_target,
    _install_conflict_clear,
    _normalise_sync_runtimes,
    _resolve_target,
    _scope_tip_banner,
    _validate_plugin_root,
    _validate_runtime,
    _validate_scope,
    plugin_app,
)
from eawf.surfaces.cli.commands.plugin_render import (
    _codex_doctor_payload,
    _codex_doctor_text,
    _codex_install_payload,
    _codex_install_text,
    _codex_package_payload,
    _codex_package_text,
    _doctor_payload,
    _doctor_text,
    _install_payload,
    _install_text,
    _multi_kind_doctor_payload,
    _multi_kind_doctor_text,
    _opencode_doctor_payload,
    _opencode_doctor_text,
    _opencode_install_payload,
    _opencode_install_text,
    _package_payload,
    _package_text,
    _sync_payload,
    _sync_text,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.runtime.runtimes.claude.plugin_update import UpdateResult

logger = logging.getLogger(__name__)


def _install_codex(
    *,
    target: Path,
    scope: str,
    scope_lit: Scope,
    force: bool,
    dry_run: bool,
    plugin_root: Path | None,
    flags: GlobalFlags,
) -> None:
    """Run + report the codex ``plugin install`` arm.

    Raises:
        typer.Exit: via :func:`emit_error` on an integrity violation.
    """
    from eawf.runtime.runtimes.codex import install_plugin as codex_install_plugin
    from eawf.runtime.runtimes.codex.plugin_install import (
        IntegrityViolation as CodexIntegrityViolation,
    )

    try:
        codex_result = codex_install_plugin(
            target,
            scope=scope_lit,
            force=force,
            dry_run=dry_run,
            plugin_root=plugin_root,
        )
    except CodexIntegrityViolation as exc:
        cli_errors.emit_error(
            cli_errors.StateConflict(str(exc), kind="IntegrityViolation"),
            flags=flags,
        )
        return
    emit_json_or_text(
        _codex_install_payload(codex_result),
        _codex_install_text(codex_result),
        flags=flags,
    )
    codex_plugin_root = (
        codex_result.manifest.path.parents[1]
        if codex_result.manifest is not None
        else codex_result.target_dir
    )
    if not flags.no_input and not flags.json_output and not codex_result.dry_run:
        # Codex does not auto-load from <plugin_root>; marketplace
        # registration is required for discovery. Steer the operator
        # at `eawf plugin package codex`.
        print(
            "note: codex does not auto-load plugins from this path.\n"
            "      run `eawf plugin package codex` to emit a marketplace tree\n"
            "      then `codex plugin marketplace add <target>` and\n"
            "      `codex plugin add eawf@eawf` to register and install it."
        )
        banner = _scope_tip_banner(runtime="codex", scope=scope, result_path=codex_plugin_root)
        if banner:
            print(banner)


def _install_opencode(
    *, target: Path, scope: str, scope_lit: Scope, force: bool, dry_run: bool, flags: GlobalFlags
) -> None:
    """Run + report the opencode ``plugin install`` arm.

    Raises:
        typer.Exit: via :func:`emit_error` on an integrity violation or
            invalid input.
    """
    from eawf.runtime.runtimes.opencode import install_plugin as opencode_install_plugin
    from eawf.runtime.runtimes.opencode.plugin_install import (
        IntegrityViolation as OpencodeIntegrityViolation,
    )

    try:
        oc_result = opencode_install_plugin(target, scope=scope_lit, force=force, dry_run=dry_run)
    except OpencodeIntegrityViolation as exc:
        cli_errors.emit_error(
            cli_errors.StateConflict(str(exc), kind="IntegrityViolation"),
            flags=flags,
        )
        return
    except ValueError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(str(exc), kind="InvalidInput"),
            flags=flags,
        )
        return
    emit_json_or_text(
        _opencode_install_payload(oc_result),
        _opencode_install_text(oc_result),
        flags=flags,
    )
    oc_plugin_dir = (
        oc_result.plugin_js.path.parent if oc_result.plugin_js is not None else oc_result.target_dir
    )
    banner = _scope_tip_banner(runtime="opencode", scope=scope, result_path=oc_plugin_dir)
    if banner and not flags.no_input and not flags.json_output:
        print(banner)


@plugin_app.command(name="install")
def install_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str,
        typer.Argument(
            help="Runtime to install: 'claude', 'codex', or 'opencode'.",
        ),
    ],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Install location: 'project' (default) or 'user' (cross-project).",
        ),
    ] = "project",
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Overwrite hand-edited managed files. For runtime='claude', "
                "also bypasses the CC-marketplace-conflict gate; for "
                "codex/opencode, bypasses the user-scope-clash gate."
            ),
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Render the tree but do not write any bytes."),
    ] = False,
    plugin_root: Annotated[
        Path | None,
        typer.Option(
            "--plugin-root",
            help=(
                "Explicit Codex plugin root. Overrides scope-derived and "
                "marketplace-cache discovery."
            ),
        ),
    ] = None,
) -> None:
    """Render a runtime plugin tree."""
    from eawf.runtime.runtimes.claude.plugin_install import (
        IntegrityViolation,
        install_plugin,
    )

    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
        _validate_scope(scope, runtime=runtime)
        _validate_plugin_root(runtime, plugin_root)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    target = _resolve_target(flags)
    if not _install_conflict_clear(runtime=runtime, scope=scope, flags=flags, force=force):
        return
    scope_lit = cast(Scope, scope)
    if runtime == "codex":
        _install_codex(
            target=target,
            scope=scope,
            scope_lit=scope_lit,
            force=force,
            dry_run=dry_run,
            plugin_root=plugin_root,
            flags=flags,
        )
        return
    if runtime == "opencode":
        _install_opencode(
            target=target,
            scope=scope,
            scope_lit=scope_lit,
            force=force,
            dry_run=dry_run,
            flags=flags,
        )
        return
    try:
        result = install_plugin(target, force=force, dry_run=dry_run)
    except IntegrityViolation as exc:
        cli_errors.emit_error(
            cli_errors.StateConflict(str(exc), kind="IntegrityViolation"),
            flags=flags,
        )
        return
    except ValueError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(str(exc), kind="InvalidInput"),
            flags=flags,
        )
        return

    emit_json_or_text(_install_payload(result), _install_text(result), flags=flags)


@plugin_app.command(name="update")
def update_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str,
        typer.Argument(help="Runtime to update: 'claude', 'codex', or 'opencode'."),
    ],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Install location: 'project' (default) or 'user' (cross-project).",
        ),
    ] = "project",
    plugin_root: Annotated[
        Path | None,
        typer.Option(
            "--plugin-root",
            help=(
                "Explicit Codex plugin root. Overrides scope-derived and "
                "marketplace-cache discovery."
            ),
        ),
    ] = None,
) -> None:
    """Re-render a runtime plugin tree, aborting on hand-edits."""
    from eawf.runtime.runtimes.claude.plugin_install import IntegrityViolation
    from eawf.runtime.runtimes.claude.plugin_update import update_plugin
    from eawf.runtime.runtimes.codex import install_plugin as codex_install_plugin
    from eawf.runtime.runtimes.codex.plugin_install import (
        IntegrityViolation as CodexIntegrityViolation,
    )
    from eawf.runtime.runtimes.opencode import install_plugin as opencode_install_plugin
    from eawf.runtime.runtimes.opencode.plugin_install import (
        IntegrityViolation as OpencodeIntegrityViolation,
    )

    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
        _validate_scope(scope, runtime=runtime)
        _validate_plugin_root(runtime, plugin_root)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    target = _resolve_target(flags)
    scope_lit = cast(Scope, scope)
    if runtime == "codex":
        try:
            codex_result = codex_install_plugin(
                target,
                scope=scope_lit,
                force=False,
                plugin_root=plugin_root,
            )
        except CodexIntegrityViolation as exc:
            cli_errors.emit_error(
                cli_errors.StateConflict(str(exc), kind="IntegrityViolation"), flags=flags
            )
            return
        emit_json_or_text(
            _codex_install_payload(codex_result),
            _codex_install_text(codex_result),
            flags=flags,
        )
        return
    if runtime == "opencode":
        try:
            oc_result = opencode_install_plugin(target, scope=scope_lit, force=False)
        except OpencodeIntegrityViolation as exc:
            cli_errors.emit_error(
                cli_errors.StateConflict(str(exc), kind="IntegrityViolation"), flags=flags
            )
            return
        except ValueError as exc:
            cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
            return
        emit_json_or_text(
            _opencode_install_payload(oc_result),
            _opencode_install_text(oc_result),
            flags=flags,
        )
        return
    try:
        result: UpdateResult = update_plugin(target)
    except IntegrityViolation as exc:
        cli_errors.emit_error(
            cli_errors.StateConflict(str(exc), kind="IntegrityViolation"),
            flags=flags,
        )
        return

    emit_json_or_text(_install_payload(result), _install_text(result), flags=flags)


def _doctor_single_runtime(
    *,
    runtime: str,
    scope: str,
    target: Path,
    plugin_root: Path | None,
    flags: GlobalFlags,
) -> None:
    """Run the per-runtime drift sweep for a single named runtime.

    Validates *runtime* / *scope*, dispatches to the codex / opencode /
    claude doctor, and emits the JSON or text report.

    Raises:
        typer.Exit: ``STATE_CONFLICT`` when the runtime reports drift;
            or via :func:`emit_error` on invalid runtime / scope.
    """
    from eawf.runtime.runtimes.claude.plugin_doctor import doctor_plugin
    from eawf.runtime.runtimes.codex import doctor_plugin as codex_doctor_plugin
    from eawf.runtime.runtimes.codex.plugin_install import (
        IntegrityViolation as CodexIntegrityViolation,
    )
    from eawf.runtime.runtimes.opencode import doctor_plugin as opencode_doctor_plugin

    try:
        _validate_runtime(runtime)
        _validate_scope(scope, runtime=runtime)
        _validate_plugin_root(runtime, plugin_root)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    scope_lit = cast(Scope, scope)
    if runtime == "codex":
        try:
            codex_report = codex_doctor_plugin(
                target,
                scope=scope_lit,
                plugin_root=plugin_root,
            )
        except CodexIntegrityViolation as exc:
            cli_errors.emit_error(
                cli_errors.StateConflict(str(exc), kind="IntegrityViolation"),
                flags=flags,
            )
            return
        emit_json_or_text(
            _codex_doctor_payload(codex_report),
            _codex_doctor_text(codex_report),
            flags=flags,
        )
        if not codex_report.clean:
            raise typer.Exit(exit_codes.STATE_CONFLICT)
        return
    if runtime == "opencode":
        oc_report = opencode_doctor_plugin(target, scope=scope_lit)
        emit_json_or_text(
            _opencode_doctor_payload(oc_report),
            _opencode_doctor_text(oc_report),
            flags=flags,
        )
        if not oc_report.clean:
            raise typer.Exit(exit_codes.STATE_CONFLICT)
        return
    claude_report = doctor_plugin(target)
    emit_json_or_text(_doctor_payload(claude_report), _doctor_text(claude_report), flags=flags)
    if not claude_report.clean:
        raise typer.Exit(exit_codes.STATE_CONFLICT)


@plugin_app.command(name="doctor")
def doctor_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Runtime to inspect: 'claude', 'codex', or 'opencode'. "
                "Omit the argument to run the multi-runtime 4-drift-kind "
                "sweep (manifest-vs-disk, registry-vs-disk, "
                "capability-vs-probe, helper-LOC-overflow) per C07a §5.9."
            ),
        ),
    ] = None,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Install location to inspect: 'project' (default) or 'user'.",
        ),
    ] = "project",
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help=(
                "Run the Claude checksum sweep under the shared sync/doctor "
                "portalock so the drift gate reads the post-'plugin sync' "
                "checksum from the same lock-scope. CI uses this to fail a PR "
                "on a stale build/<runtime>-plugin/ tree without flaking."
            ),
        ),
    ] = False,
    plugin_root: Annotated[
        Path | None,
        typer.Option(
            "--plugin-root",
            help=(
                "Explicit Codex plugin root. Overrides scope-derived and "
                "marketplace-cache discovery."
            ),
        ),
    ] = None,
) -> None:
    """Report drift in an installed runtime plugin tree."""
    from eawf.runtime.runtimes.claude.plugin_doctor import doctor_plugin_strict
    from eawf.runtime.runtimes.plugin_doctor import run_doctor

    flags: GlobalFlags = ctx.obj
    target = _resolve_target(flags)
    try:
        _validate_plugin_root(runtime, plugin_root)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    if strict and runtime not in (None, "claude"):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--strict applies to the claude checksum sweep only; "
                f"drop --strict or pass 'claude' (got {runtime!r})",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    if strict:
        # ``--strict`` is the checksum-level drift gate: it runs the
        # Claude per-file sweep under the shared sync/doctor portalock so
        # the checksum read is the post-``plugin sync`` checksum from the
        # same lock-scope. It deliberately bypasses the 4-kind
        # multi-runtime sweep (which is the no-arg default) — the strict
        # gate is CI's "build/<runtime>-plugin/ is stale" check, which is
        # checksum-level by construction.
        strict_report = doctor_plugin_strict(target)
        emit_json_or_text(
            _doctor_payload(strict_report),
            _doctor_text(strict_report),
            flags=flags,
        )
        if not strict_report.clean:
            raise typer.Exit(exit_codes.STATE_CONFLICT)
        return
    if runtime is None:
        # Multi-kind sweep (no runtime arg) — enumerates the 4 drift
        # kinds across all three runtimes. ``capability-vs-probe`` is
        # skipped here because probe injection is the daemon's job; the
        # CLI surface only emits the manifest/registry/LOC kinds.
        report = run_doctor(target)
        emit_json_or_text(
            _multi_kind_doctor_payload(report),
            _multi_kind_doctor_text(report),
            flags=flags,
        )
        if not report.clean:
            raise typer.Exit(exit_codes.STATE_CONFLICT)
        return

    _doctor_single_runtime(
        runtime=runtime,
        scope=scope,
        target=target,
        plugin_root=plugin_root,
        flags=flags,
    )


@plugin_app.command(name="package")
def package_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str,
        typer.Argument(
            help=(
                "Runtime to package: 'claude' (Claude Code marketplace plugin) "
                "or 'codex' (Codex CLI local marketplace). OpenCode has no "
                "marketplace concept — use 'eawf plugin install opencode' instead."
            ),
        ),
    ],
    target: Annotated[
        Path | None,
        typer.Option(
            "--target",
            help=(
                "Output directory. Defaults to "
                "<workspace>/build/eawf-plugin/ for claude, "
                "<workspace>/build/eawf-codex-marketplace/ for codex."
            ),
        ),
    ] = None,
    include_marketplace: Annotated[
        bool,
        typer.Option(
            "--include-marketplace/--no-marketplace",
            help="Emit .claude-plugin/marketplace.json (claude only; default yes).",
        ),
    ] = True,
    include_readme: Annotated[
        bool,
        typer.Option(
            "--include-readme/--no-readme",
            help="Emit README.md (claude only; default yes).",
        ),
    ] = True,
    include_hooks: Annotated[
        bool,
        typer.Option(
            "--include-hooks/--no-hooks",
            help=(
                "Emit hooks.json + hooks/<event>.sh wrappers for the six "
                "session-level Claude Code events (claude only; default yes)."
            ),
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite a non-empty target that is not a previous eawf plugin output.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="List what would be written; write nothing.",
        ),
    ] = False,
) -> None:
    """Emit an installable runtime plugin tree."""
    from eawf.runtime.runtimes.claude.plugin_install import IntegrityViolation
    from eawf.runtime.runtimes.claude.plugin_package import package_plugin
    from eawf.runtime.runtimes.codex import package_plugin as codex_package_plugin

    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    if runtime == "opencode":
        cli_errors.emit_error(
            cli_errors.UserError(
                "opencode has no marketplace concept; use "
                "`eawf plugin install opencode` (project or user scope) instead",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    resolved_target = (target or _default_package_target(flags, runtime=runtime)).resolve()
    if runtime == "codex":
        try:
            codex_result = codex_package_plugin(resolved_target, force=force, dry_run=dry_run)
        except ValueError as exc:
            cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
            return
        emit_json_or_text(
            _codex_package_payload(codex_result),
            _codex_package_text(codex_result),
            flags=flags,
        )
        if not flags.no_input and not flags.json_output and not codex_result.dry_run:
            print(
                f"next: codex plugin marketplace add {codex_result.target}\n"
                "      codex plugin add eawf@eawf"
            )
        return
    try:
        result = package_plugin(
            resolved_target,
            include_marketplace=include_marketplace,
            include_readme=include_readme,
            include_hooks=include_hooks,
            force=force,
            dry_run=dry_run,
        )
    except IntegrityViolation as exc:
        cli_errors.emit_error(
            cli_errors.StateConflict(str(exc), kind="IntegrityViolation"),
            flags=flags,
        )
        return
    except ValueError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(str(exc), kind="InvalidInput"),
            flags=flags,
        )
        return

    emit_json_or_text(_package_payload(result), _package_text(result), flags=flags)


@plugin_app.command(name="sync")
def sync_cmd(
    ctx: typer.Context,
    runtimes: Annotated[
        list[str] | None,
        typer.Option(
            "--runtime",
            help=(
                "Restrict sync to one or more runtime ids. Repeat the flag "
                "to sync several (e.g. '--runtime claude --runtime codex'). "
                "Defaults to all three (claude-code + codex + opencode)."
            ),
        ),
    ] = None,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Install location: 'project' (default) or 'user' (cross-project).",
        ),
    ] = "project",
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite hand-edited managed files (passed through to each runtime).",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Compute deltas across runtimes but write no bytes.",
        ),
    ] = False,
) -> None:
    """Regenerate per-runtime plugin artifacts deterministically.

    The canonical multi-runtime regeneration verb. Each requested
    runtime is driven through its ``install_plugin`` renderer with
    shared inputs (frozen timestamp, pass-through force / dry_run); the
    result aggregates per-file deltas under a single envelope.
    """
    from eawf.runtime.runtimes.plugin_sync import PluginSyncIntegrityError, sync_plugins

    flags: GlobalFlags = ctx.obj
    if scope not in _VALID_SCOPES:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"invalid --scope {scope!r}; expected one of {list(_VALID_SCOPES)}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    try:
        canonical = _normalise_sync_runtimes(runtimes or [])
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    target = _resolve_target(flags)
    scope_lit = cast(Literal["project", "user"], scope)
    try:
        # The Sequence[RuntimeId] cast is structural — canonical is
        # built from the closed alias map above so the strings are
        # already one of the three canonical ids.
        from eawf.runtime.runtimes.manifest import RuntimeId

        typed_runtimes = cast(list[RuntimeId], canonical)
        result = sync_plugins(
            target,
            scope=scope_lit,
            runtimes=typed_runtimes or None,
            force=force,
            dry_run=dry_run,
        )
    except PluginSyncIntegrityError as exc:
        cli_errors.emit_error(
            cli_errors.StateConflict(str(exc), kind="IntegrityViolation"),
            flags=flags,
        )
        return

    emit_json_or_text(_sync_payload(result), _sync_text(result), flags=flags)
