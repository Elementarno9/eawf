"""``eawf plugin install/update/doctor claude`` Typer commands.

Surface contract per Phase 4 W05 acceptance:

- ``eawf plugin install claude [--force] [--dry-run]`` renders the
  Claude Code plugin tree under the workspace root. Idempotent: re-run
  produces a byte-identical hash. Hand-edits abort with exit 8 unless
  ``--force`` is passed.
- ``eawf plugin update claude`` re-renders the tree, asserting that no
  managed file has been hand-edited (exit 8 on drift).
- ``eawf plugin doctor claude`` reports drift / missing files; clean
  exits 0, dirty exits 8.

The runtime argument is a positional Typer argument so a future
``opencode`` adapter (W06+) can register under the same root command
without a flag rename.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from eawf.cli import errors as cli_errors
from eawf.cli import exit_codes
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.runtimes.claude.plugin_doctor import DoctorReport, doctor_plugin
from eawf.runtimes.claude.plugin_install import (
    InstallResult,
    IntegrityViolation,
    install_plugin,
)
from eawf.runtimes.claude.plugin_package import PackageResult, package_plugin
from eawf.runtimes.claude.plugin_update import UpdateResult, update_plugin

logger = logging.getLogger(__name__)


plugin_app = typer.Typer(
    name="plugin",
    help="Install, update, or diagnose runtime plugins (claude, opencode, ...).",
    no_args_is_help=True,
)


_SUPPORTED_RUNTIMES: tuple[str, ...] = ("claude",)


def _validate_runtime(runtime: str) -> None:
    """Reject runtimes outside the v0.1 supported list."""
    if runtime not in _SUPPORTED_RUNTIMES:
        raise cli_errors.InvalidInput(
            f"unknown runtime {runtime!r}; expected one of {list(_SUPPORTED_RUNTIMES)}"
        )


def _resolve_target(flags: GlobalFlags) -> Path:
    """Resolve the workspace root the plugin commands operate against."""
    return (flags.workspace or Path.cwd()).resolve()


def _install_payload(result: InstallResult) -> dict[str, object]:
    """Render :class:`InstallResult` as the JSON envelope body."""
    return {
        "target_dir": str(result.target_dir),
        "dry_run": result.dry_run,
        "skills": [{"path": str(d.path), "action": d.action} for d in result.skills],
        "agents": [{"path": str(d.path), "action": d.action} for d in result.agents],
        "hooks": [{"path": str(d.path), "action": d.action} for d in result.hooks],
        "settings": (
            {
                "path": str(result.settings.path),
                "action": result.settings.action,
            }
            if result.settings is not None
            else None
        ),
    }


def _doctor_payload(report: DoctorReport) -> dict[str, object]:
    """Render :class:`DoctorReport` as the JSON envelope body."""
    return {
        "target_dir": str(report.target_dir),
        "clean": report.clean,
        "ok": [{"region_id": e.region_id, "path": str(e.path), "kind": e.kind} for e in report.ok],
        "drifted": [
            {
                "region_id": e.region_id,
                "path": str(e.path),
                "kind": e.kind,
                "on_disk_hash": e.on_disk_hash,
                "expected_hash": e.expected_hash,
            }
            for e in report.drifted
        ],
        "missing": [
            {"region_id": e.region_id, "path": str(e.path), "kind": e.kind} for e in report.missing
        ],
    }


def _install_text(result: InstallResult) -> str:
    """Render :class:`InstallResult` as a human-readable summary."""
    parts = [f"plugin install ({'dry-run' if result.dry_run else 'wrote'}) → {result.target_dir}"]
    parts.append(f"  skills:   {len(result.skills)} files")
    parts.append(f"  agents:   {len(result.agents)} files")
    parts.append(f"  hooks:    {len(result.hooks)} files")
    parts.append(f"  settings: {result.settings.action if result.settings else 'no-op'}")
    return "\n".join(parts)


def _doctor_text(report: DoctorReport) -> str:
    """Render :class:`DoctorReport` as a human-readable summary."""
    parts = [f"plugin doctor → {report.target_dir}"]
    parts.append(
        f"  ok={len(report.ok)} drifted={len(report.drifted)} missing={len(report.missing)}"
    )
    if report.drifted:
        parts.append("  drifted files:")
        for entry in report.drifted:
            parts.append(f"    - {entry.path} (on-disk={entry.on_disk_hash})")
    if report.missing:
        parts.append("  missing files:")
        for entry in report.missing:
            parts.append(f"    - {entry.path}")
    return "\n".join(parts)


def _package_payload(result: PackageResult) -> dict[str, object]:
    """Render :class:`PackageResult` as the JSON envelope body."""
    return {
        "target": str(result.target),
        "dry_run": result.dry_run,
        "skills": list(result.skills),
        "agents": list(result.agents),
        "wrote_marketplace": result.wrote_marketplace,
        "wrote_readme": result.wrote_readme,
    }


def _package_text(result: PackageResult) -> str:
    """Render :class:`PackageResult` as a human-readable summary."""
    parts = [f"plugin package ({'dry-run' if result.dry_run else 'wrote'}) → {result.target}"]
    parts.append(f"  skills:      {len(result.skills)}")
    parts.append(f"  agents:      {len(result.agents)}")
    parts.append(f"  marketplace: {'yes' if result.wrote_marketplace else 'no'}")
    parts.append(f"  readme:      {'yes' if result.wrote_readme else 'no'}")
    return "\n".join(parts)


def _default_package_target(flags: GlobalFlags) -> Path:
    """Default ``--target`` for ``plugin package`` — ``<workspace>/build/eawf-plugin/``."""
    base = (flags.workspace or Path.cwd()).resolve()
    return base / "build" / "eawf-plugin"


@plugin_app.command(name="install")
def install_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str,
        typer.Argument(help="Runtime to install (currently only `claude`)."),
    ],
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite hand-edited managed files (use with care)."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Render the tree but do not write any bytes."),
    ] = False,
) -> None:
    """Render a runtime plugin tree."""
    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    target = _resolve_target(flags)
    try:
        result = install_plugin(target, force=force, dry_run=dry_run)
    except IntegrityViolation as exc:
        cli_errors.emit_error(
            cli_errors.IntegrityViolation(str(exc)),
            flags=flags,
        )
        return
    except ValueError as exc:
        cli_errors.emit_error(
            cli_errors.InvalidInput(str(exc)),
            flags=flags,
        )
        return

    emit_json_or_text(_install_payload(result), _install_text(result), flags=flags)


@plugin_app.command(name="update")
def update_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str,
        typer.Argument(help="Runtime to update (currently only `claude`)."),
    ],
) -> None:
    """Re-render a runtime plugin tree, aborting on hand-edits."""
    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    target = _resolve_target(flags)
    try:
        result: UpdateResult = update_plugin(target)
    except IntegrityViolation as exc:
        cli_errors.emit_error(
            cli_errors.IntegrityViolation(str(exc)),
            flags=flags,
        )
        return

    emit_json_or_text(_install_payload(result), _install_text(result), flags=flags)


@plugin_app.command(name="doctor")
def doctor_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str,
        typer.Argument(help="Runtime to inspect (currently only `claude`)."),
    ],
) -> None:
    """Report drift in an installed runtime plugin tree."""
    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    target = _resolve_target(flags)
    report = doctor_plugin(target)
    emit_json_or_text(_doctor_payload(report), _doctor_text(report), flags=flags)
    if not report.clean:
        raise typer.Exit(exit_codes.INTEGRITY_VIOLATION)


@plugin_app.command(name="package")
def package_cmd(
    ctx: typer.Context,
    runtime: Annotated[
        str,
        typer.Argument(help="Runtime to package (currently only `claude`)."),
    ],
    target: Annotated[
        Path | None,
        typer.Option(
            "--target",
            help=(
                "Output directory. Defaults to <workspace>/build/eawf-plugin/ "
                "(or ./build/eawf-plugin/ when no workspace anchor)."
            ),
        ),
    ] = None,
    include_marketplace: Annotated[
        bool,
        typer.Option(
            "--include-marketplace/--no-marketplace",
            help="Emit .claude-plugin/marketplace.json (default: yes).",
        ),
    ] = True,
    include_readme: Annotated[
        bool,
        typer.Option(
            "--include-readme/--no-readme",
            help="Emit README.md (default: yes).",
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
    """Emit an installable Claude Code plugin tree."""
    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    resolved_target = (target or _default_package_target(flags)).resolve()
    try:
        result = package_plugin(
            resolved_target,
            include_marketplace=include_marketplace,
            include_readme=include_readme,
            force=force,
            dry_run=dry_run,
        )
    except IntegrityViolation as exc:
        cli_errors.emit_error(
            cli_errors.IntegrityViolation(str(exc)),
            flags=flags,
        )
        return
    except ValueError as exc:
        cli_errors.emit_error(
            cli_errors.InvalidInput(str(exc)),
            flags=flags,
        )
        return

    emit_json_or_text(_package_payload(result), _package_text(result), flags=flags)


__all__ = [
    "plugin_app",
]
