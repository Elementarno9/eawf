"""``eawf plugin install/update/doctor {claude,codex,opencode}`` Typer commands.

Surface contract:

- ``eawf plugin install <runtime> [--scope project|user] [--force] [--dry-run]``
  renders the runtime plugin tree. ``--scope user`` writes under the
  runtime's user-scope config dir; ``--scope project`` (default)
  writes under the workspace. ``claude`` rejects ``--scope user``
  (use the CC marketplace export instead).
- ``eawf plugin update <runtime> [--scope ...]`` re-renders the tree,
  aborting on hand-edits (exit 8 / ``INTEGRITY_VIOLATION``).
- ``eawf plugin doctor <runtime> [--scope ...] [--strict]`` reports
  drift / missing files; clean exits 0, dirty exits 8. ``--strict``
  runs the Claude checksum sweep under the shared sync/doctor
  portalock so the drift gate reads the post-``plugin sync`` checksum
  from the same lock-scope (closes the sync-then-doctor race per the
  C09 F19 mitigation).

Idempotent: re-running ``install`` against an unchanged source tree
produces a byte-identical tree. Hand-edits abort with exit 8 unless
``--force`` is passed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, cast

import typer

from eawf.cli import errors as cli_errors
from eawf.cli import exit_codes
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text

if TYPE_CHECKING:
    # Annotation-only result/report types. The runtime values
    # (install/doctor/package/update/sync functions + integrity
    # exceptions) are imported lazily inside each command handler so
    # importing this module for completion does not pull eawf.runtimes
    # (and its jinja2/yaml transitive deps).
    from eawf.runtimes.claude.plugin_conflict import CCPluginConflict
    from eawf.runtimes.claude.plugin_doctor import DoctorReport
    from eawf.runtimes.claude.plugin_install import InstallResult
    from eawf.runtimes.claude.plugin_package import PackageResult
    from eawf.runtimes.claude.plugin_update import UpdateResult
    from eawf.runtimes.codex.plugin_conflict import CodexUserPluginConflict
    from eawf.runtimes.codex.plugin_doctor import DoctorReport as CodexDoctorReport
    from eawf.runtimes.codex.plugin_install import (
        InstallResult as CodexInstallResult,
    )
    from eawf.runtimes.codex.plugin_package import PackageResult as CodexPackageResult
    from eawf.runtimes.opencode.plugin_conflict import OpenCodeUserPluginConflict
    from eawf.runtimes.opencode.plugin_doctor import DoctorReport as OpencodeDoctorReport
    from eawf.runtimes.opencode.plugin_install import (
        InstallResult as OpencodeInstallResult,
    )
    from eawf.runtimes.plugin_doctor import PluginDoctorReport
    from eawf.runtimes.plugin_sync import SyncResult

logger = logging.getLogger(__name__)


# Module-level lazy wrappers for the three runtime conflict detectors.
# Tests monkeypatch these names (``eawf.cli.commands.plugin.<detector>``)
# to inject synthetic conflicts, so they must stay module-level
# attributes; the real implementations are imported lazily inside each
# wrapper so importing this module for shell completion does not pull
# ``eawf.runtimes.*`` (and its jinja2 transitive dep).
def detect_marketplace_install() -> CCPluginConflict | None:
    """Detect an existing CC-marketplace eawf install (lazy import)."""
    from eawf.runtimes.claude.plugin_conflict import (
        detect_marketplace_install as _impl,
    )

    return _impl()


def codex_detect_user_install() -> CodexUserPluginConflict | None:
    """Detect a user-scope codex eawf install (lazy import)."""
    from eawf.runtimes.codex import detect_user_install as _impl

    return _impl()


def opencode_detect_user_install() -> OpenCodeUserPluginConflict | None:
    """Detect a user-scope opencode eawf install (lazy import)."""
    from eawf.runtimes.opencode import detect_user_install as _impl

    return _impl()


Scope = Literal["project", "user"]


_SYNC_RUNTIME_IDS: dict[str, str] = {
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "opencode": "opencode",
}


plugin_app = typer.Typer(
    name="plugin",
    help=(
        "Install, update, or diagnose runtime plugins (claude, codex, opencode). "
        "Use 'install' for all three; 'package' is Claude-only (marketplace export)."
    ),
    no_args_is_help=True,
)


_SUPPORTED_RUNTIMES: tuple[str, ...] = ("claude", "codex", "opencode")
_VALID_SCOPES: tuple[str, ...] = ("project", "user")


def _validate_runtime(runtime: str) -> None:
    """Reject runtimes outside the v0.1 supported list."""
    if runtime not in _SUPPORTED_RUNTIMES:
        raise cli_errors.UserError(
            f"unknown runtime {runtime!r}; expected one of {list(_SUPPORTED_RUNTIMES)}",
            kind="InvalidInput",
        )


def _validate_scope(scope: str, *, runtime: str) -> None:
    """Reject invalid scope values, and ``user`` for claude.

    Raises:
        UserError: when *scope* is not ``project`` / ``user``, or
            when *runtime* is ``claude`` and *scope* is ``user``
            (``kind="InvalidInput"``).
    """
    if scope not in _VALID_SCOPES:
        raise cli_errors.UserError(
            f"invalid --scope {scope!r}; expected one of {list(_VALID_SCOPES)}", kind="InvalidInput"
        )
    if runtime == "claude" and scope == "user":
        raise cli_errors.UserError(
            "claude is project-scope only; use the CC marketplace for user-scope installs",
            kind="InvalidInput",
        )


def _claude_conflict_clear(*, flags: GlobalFlags, force: bool) -> bool:
    """Return ``True`` when no CC-marketplace conflict blocks ``install claude``.

    Detects an existing eawf install under ``~/.claude/plugins/``. When found:

    - ``--force`` overrides the gate (caller is acknowledging the duplicate
      render).
    - ``--no-input`` mode refuses the install with a :exc:`UserError`
      (``kind="InvalidInput"``) so the operator can pick a path before
      retrying.
    - Otherwise prompts via :mod:`questionary` for confirmation; a ``No``
      answer aborts cleanly.
    """
    conflict = detect_marketplace_install()
    if conflict is None:
        return True
    if force:
        logger.info(f"_claude_conflict_clear force-bypass plugin_dir={conflict.plugin_dir}")
        return True
    message = (
        f"detected CC marketplace plugin at {conflict.plugin_dir}; "
        f"installing the project-local .claude/ tree alongside it will cause "
        f"Claude Code to see every skill/agent/hook twice"
    )
    if flags.no_input:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"{message}. Rerun without --no-input to confirm, pass --force "
                "to acknowledge, or `/plugin uninstall eawf@eawf-local` inside "
                "Claude Code first.",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return False
    import questionary

    proceed = questionary.confirm(
        f"{message}.\nProceed with project-local install anyway?",
        default=False,
    ).ask()
    if not proceed:
        print("plugin install claude: aborted (conflict not acknowledged)")
        return False
    return True


def _codex_user_conflict_clear(*, flags: GlobalFlags, force: bool) -> bool:
    """Warn when a user-scope codex install of ``eawf`` exists during project install."""
    conflict = codex_detect_user_install()
    if conflict is None:
        return True
    if force:
        logger.info(f"_codex_user_conflict_clear force-bypass plugin_dir={conflict.plugin_dir}")
        return True
    message = (
        f"detected user-scope codex eawf install at {conflict.plugin_dir}; "
        f"running a project-scope install alongside it will cause Codex to "
        f"resolve two 'eawf' plugins with undefined precedence"
    )
    if flags.no_input:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"{message}. Rerun without --no-input to confirm, pass --force "
                "to acknowledge, or remove the user-scope install first.",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return False
    import questionary

    proceed = questionary.confirm(
        f"{message}.\nProceed with project-scope install anyway?",
        default=False,
    ).ask()
    if not proceed:
        print("plugin install codex: aborted (conflict not acknowledged)")
        return False
    return True


def _opencode_user_conflict_clear(*, flags: GlobalFlags, force: bool) -> bool:
    """Warn when a user-scope opencode install of ``eawf.js`` exists during project install."""
    conflict = opencode_detect_user_install()
    if conflict is None:
        return True
    if force:
        logger.info(
            f"_opencode_user_conflict_clear force-bypass plugin_file={conflict.plugin_file}"
        )
        return True
    message = (
        f"detected user-scope opencode eawf install at {conflict.plugin_file}; "
        f"running a project-scope install alongside it will cause OpenCode to "
        f"auto-load two 'eawf' plugins with undefined precedence"
    )
    if flags.no_input:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"{message}. Rerun without --no-input to confirm, pass --force "
                "to acknowledge, or remove the user-scope install first.",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return False
    import questionary

    proceed = questionary.confirm(
        f"{message}.\nProceed with project-scope install anyway?",
        default=False,
    ).ask()
    if not proceed:
        print("plugin install opencode: aborted (conflict not acknowledged)")
        return False
    return True


def _install_conflict_clear(*, runtime: str, scope: str, flags: GlobalFlags, force: bool) -> bool:
    """Dispatch conflict-gate detection to the runtime-specific helper.

    For ``claude``: always probe (claude is project-only). For
    ``codex`` / ``opencode``: probe only on a project-scope install,
    detecting a clashing user-scope install of the same plugin name.
    """
    if runtime == "claude":
        return _claude_conflict_clear(flags=flags, force=force)
    if scope != "project":
        return True
    if runtime == "codex":
        return _codex_user_conflict_clear(flags=flags, force=force)
    if runtime == "opencode":
        return _opencode_user_conflict_clear(flags=flags, force=force)
    return True


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


def _codex_install_payload(result: CodexInstallResult) -> dict[str, object]:
    """Render the Codex :class:`InstallResult` as the JSON envelope body."""
    return {
        "runtime": "codex",
        "scope": result.scope,
        "target_dir": str(result.target_dir),
        "dry_run": result.dry_run,
        "skills": [{"path": str(d.path), "action": d.action} for d in result.skills],
        "hooks": [{"path": str(d.path), "action": d.action} for d in result.hooks],
        "manifest": (
            {"path": str(result.manifest.path), "action": result.manifest.action}
            if result.manifest is not None
            else None
        ),
        "sidecar": (
            {"path": str(result.sidecar.path), "action": result.sidecar.action}
            if result.sidecar is not None
            else None
        ),
        "config": (
            {"path": str(result.config.path), "action": result.config.action}
            if result.config is not None
            else None
        ),
    }


def _codex_install_text(result: CodexInstallResult) -> str:
    verb = "dry-run" if result.dry_run else "wrote"
    # manifest path is <plugin_root>/.codex-plugin/plugin.json → parents[1] = plugin_root
    plugin_root = result.manifest.path.parents[1] if result.manifest else result.target_dir
    parts = [f"plugin install codex --scope {result.scope} ({verb}) → {plugin_root}"]
    parts.append(f"  skills:   {len(result.skills)} files")
    parts.append(f"  hooks:    {len(result.hooks)} files")
    parts.append(f"  manifest: {result.manifest.action if result.manifest else 'no-op'}")
    parts.append(f"  sidecar:  {result.sidecar.action if result.sidecar else 'no-op'}")
    if result.config is not None:
        parts.append(f"  config:   {result.config.action} ({result.config.path})")
    else:
        parts.append("  config:   no-op")
    return "\n".join(parts)


def _codex_doctor_payload(report: CodexDoctorReport) -> dict[str, object]:
    """Render the Codex :class:`DoctorReport` as the JSON envelope body."""
    return {
        "runtime": "codex",
        "scope": report.scope,
        "target_dir": str(report.target_dir),
        "plugin_root": str(report.plugin_root),
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
        "legacy_paths": [str(p) for p in report.legacy_paths],
    }


def _codex_doctor_text(report: CodexDoctorReport) -> str:
    parts = [f"plugin doctor codex --scope {report.scope} → {report.plugin_root}"]
    parts.append(
        f"  ok={len(report.ok)} drifted={len(report.drifted)} missing={len(report.missing)}"
    )
    if report.legacy_paths:
        parts.append("  legacy paths (delete manually):")
        for path in report.legacy_paths:
            parts.append(f"    - {path}")
    return "\n".join(parts)


def _opencode_install_payload(result: OpencodeInstallResult) -> dict[str, object]:
    """Render the OpenCode :class:`InstallResult` as the JSON envelope body."""
    return {
        "runtime": "opencode",
        "scope": result.scope,
        "target_dir": str(result.target_dir),
        "dry_run": result.dry_run,
        "plugin_js": (
            {"path": str(result.plugin_js.path), "action": result.plugin_js.action}
            if result.plugin_js is not None
            else None
        ),
        "sidecar": (
            {"path": str(result.sidecar.path), "action": result.sidecar.action}
            if result.sidecar is not None
            else None
        ),
        "config": (
            {"path": str(result.config.path), "action": result.config.action}
            if result.config is not None
            else None
        ),
        "agents": [{"path": str(d.path), "action": d.action} for d in result.agents],
        "commands": [{"path": str(d.path), "action": d.action} for d in result.commands],
    }


def _opencode_install_text(result: OpencodeInstallResult) -> str:
    verb = "dry-run" if result.dry_run else "wrote"
    # plugin_js path is <dir>/eawf.js → parent = plugins dir
    plugin_dir = result.plugin_js.path.parent if result.plugin_js else result.target_dir
    parts = [f"plugin install opencode --scope {result.scope} ({verb}) → {plugin_dir}"]
    plugin_js_action = result.plugin_js.action if result.plugin_js else "no-op"
    parts.append(f"  plugin.js: {plugin_js_action}")
    parts.append(f"  sidecar:   {result.sidecar.action if result.sidecar else 'no-op'}")
    parts.append(f"  agents:    {len(result.agents)} files")
    parts.append(f"  commands:  {len(result.commands)} files")
    if result.config is not None:
        parts.append(f"  config:    {result.config.action} ({result.config.path})")
    else:
        parts.append("  config:    no-op")
    return "\n".join(parts)


def _opencode_doctor_payload(report: OpencodeDoctorReport) -> dict[str, object]:
    return {
        "runtime": "opencode",
        "scope": report.scope,
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
        "legacy_paths": [str(p) for p in report.legacy_paths],
    }


def _opencode_doctor_text(report: OpencodeDoctorReport) -> str:
    sample = next(
        (e for e in report.ok + report.drifted + report.missing if e.kind == "plugin_js"),
        None,
    )
    plugin_dir: object = sample.path.parent if sample is not None else report.target_dir
    parts = [f"plugin doctor opencode --scope {report.scope} → {plugin_dir}"]
    parts.append(
        f"  ok={len(report.ok)} drifted={len(report.drifted)} missing={len(report.missing)}"
    )
    if report.legacy_paths:
        parts.append("  legacy paths (delete manually):")
        for path in report.legacy_paths:
            parts.append(f"    - {path}")
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


def _multi_kind_doctor_payload(report: PluginDoctorReport) -> dict[str, object]:
    """Render the multi-kind :class:`PluginDoctorReport` as JSON envelope body."""
    return {
        "target_dir": str(report.target_dir),
        "runtimes": list(report.runtimes),
        "clean": report.clean,
        "kinds": [
            {
                "kind": kind.kind,
                "clean": kind.clean,
                "skipped": kind.skipped,
                "findings": [
                    {
                        "runtime": f.runtime,
                        "location": f.location,
                        "detail": f.detail,
                    }
                    for f in kind.findings
                ],
            }
            for kind in report.kinds
        ],
    }


def _multi_kind_doctor_text(report: PluginDoctorReport) -> str:
    """Render the multi-kind :class:`PluginDoctorReport` as text."""
    parts = [f"plugin doctor (4 drift kinds) -> {report.target_dir}"]
    parts.append(f"  runtimes: {', '.join(report.runtimes)}")
    parts.append(f"  clean: {report.clean}")
    for kind in report.kinds:
        status = "skipped" if kind.skipped else ("clean" if kind.clean else "drift")
        parts.append(f"  [{kind.kind}] {status} ({len(kind.findings)} findings)")
        for finding in kind.findings:
            runtime_tag = finding.runtime or "-"
            parts.append(f"    - runtime={runtime_tag} location={finding.location}")
            parts.append(f"      detail: {finding.detail}")
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
        "wrote_hooks": result.wrote_hooks,
    }


def _package_text(result: PackageResult) -> str:
    """Render :class:`PackageResult` as a human-readable summary."""
    parts = [f"plugin package ({'dry-run' if result.dry_run else 'wrote'}) → {result.target}"]
    parts.append(f"  skills:      {len(result.skills)}")
    parts.append(f"  agents:      {len(result.agents)}")
    parts.append(f"  marketplace: {'yes' if result.wrote_marketplace else 'no'}")
    parts.append(f"  readme:      {'yes' if result.wrote_readme else 'no'}")
    parts.append(f"  hooks:       {'yes' if result.wrote_hooks else 'no'}")
    return "\n".join(parts)


def _default_package_target(flags: GlobalFlags, *, runtime: str) -> Path:
    """Default ``--target`` for ``plugin package``.

    Claude ships its plugin tree under ``<workspace>/build/eawf-plugin/``;
    Codex ships its marketplace tree under
    ``<workspace>/build/eawf-codex-marketplace/`` so the two outputs
    coexist when both are built side-by-side.
    """
    base = (flags.workspace or Path.cwd()).resolve()
    if runtime == "codex":
        return base / "build" / "eawf-codex-marketplace"
    return base / "build" / "eawf-plugin"


def _codex_package_payload(result: CodexPackageResult) -> dict[str, object]:
    """Render the Codex :class:`PackageResult` as the JSON envelope body."""
    return {
        "runtime": "codex",
        "target": str(result.target),
        "dry_run": result.dry_run,
        "skills": [{"path": str(d.path), "action": d.action} for d in result.skills],
        "hooks": [{"path": str(d.path), "action": d.action} for d in result.hooks],
        "manifest": (
            {"path": str(result.manifest.path), "action": result.manifest.action}
            if result.manifest is not None
            else None
        ),
        "marketplace": (
            {"path": str(result.marketplace.path), "action": result.marketplace.action}
            if result.marketplace is not None
            else None
        ),
    }


def _codex_package_text(result: CodexPackageResult) -> str:
    verb = "dry-run" if result.dry_run else "wrote"
    parts = [f"plugin package codex ({verb}) → {result.target}"]
    parts.append(f"  skills:      {len(result.skills)}")
    parts.append(f"  hooks:       {len(result.hooks)}")
    parts.append(f"  manifest:    {result.manifest.action if result.manifest else 'no-op'}")
    parts.append(f"  marketplace: {result.marketplace.action if result.marketplace else 'no-op'}")
    return "\n".join(parts)


def _scope_tip_banner(*, runtime: str, scope: str, result_path: Path) -> str | None:
    """Return the post-install tip banner (text-mode only) or ``None`` to suppress.

    Shown when *runtime* is codex/opencode and *scope* is project — points
    out the cross-project alternative. Suppressed for claude and for
    user-scope installs (already where the user asked for).
    """
    if runtime == "claude" or scope != "project":
        return None
    return f"tip: --scope user installs cross-project (alongside or in lieu of {result_path})"


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
) -> None:
    """Render a runtime plugin tree."""
    from eawf.runtimes.claude.plugin_install import (
        IntegrityViolation,
        install_plugin,
    )
    from eawf.runtimes.codex import install_plugin as codex_install_plugin
    from eawf.runtimes.codex.plugin_install import (
        IntegrityViolation as CodexIntegrityViolation,
    )
    from eawf.runtimes.opencode import install_plugin as opencode_install_plugin
    from eawf.runtimes.opencode.plugin_install import (
        IntegrityViolation as OpencodeIntegrityViolation,
    )

    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
        _validate_scope(scope, runtime=runtime)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    target = _resolve_target(flags)
    if not _install_conflict_clear(runtime=runtime, scope=scope, flags=flags, force=force):
        return
    scope_lit = cast(Scope, scope)
    if runtime == "codex":
        try:
            codex_result = codex_install_plugin(
                target, scope=scope_lit, force=force, dry_run=dry_run
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
                "      and `codex plugin marketplace add <target>` to register it."
            )
            banner = _scope_tip_banner(runtime=runtime, scope=scope, result_path=codex_plugin_root)
            if banner:
                print(banner)
        return
    if runtime == "opencode":
        try:
            oc_result = opencode_install_plugin(
                target, scope=scope_lit, force=force, dry_run=dry_run
            )
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
            oc_result.plugin_js.path.parent
            if oc_result.plugin_js is not None
            else oc_result.target_dir
        )
        banner = _scope_tip_banner(runtime=runtime, scope=scope, result_path=oc_plugin_dir)
        if banner and not flags.no_input and not flags.json_output:
            print(banner)
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
) -> None:
    """Re-render a runtime plugin tree, aborting on hand-edits."""
    from eawf.runtimes.claude.plugin_install import IntegrityViolation
    from eawf.runtimes.claude.plugin_update import update_plugin
    from eawf.runtimes.codex import install_plugin as codex_install_plugin
    from eawf.runtimes.codex.plugin_install import (
        IntegrityViolation as CodexIntegrityViolation,
    )
    from eawf.runtimes.opencode import install_plugin as opencode_install_plugin
    from eawf.runtimes.opencode.plugin_install import (
        IntegrityViolation as OpencodeIntegrityViolation,
    )

    flags: GlobalFlags = ctx.obj
    try:
        _validate_runtime(runtime)
        _validate_scope(scope, runtime=runtime)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    target = _resolve_target(flags)
    scope_lit = cast(Scope, scope)
    if runtime == "codex":
        try:
            codex_result = codex_install_plugin(target, scope=scope_lit, force=False)
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
) -> None:
    """Report drift in an installed runtime plugin tree."""
    from eawf.runtimes.claude.plugin_doctor import doctor_plugin, doctor_plugin_strict
    from eawf.runtimes.codex import doctor_plugin as codex_doctor_plugin
    from eawf.runtimes.opencode import doctor_plugin as opencode_doctor_plugin
    from eawf.runtimes.plugin_doctor import run_doctor

    flags: GlobalFlags = ctx.obj
    target = _resolve_target(flags)
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
            raise typer.Exit(exit_codes.INTEGRITY_VIOLATION)
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
            raise typer.Exit(exit_codes.INTEGRITY_VIOLATION)
        return

    try:
        _validate_runtime(runtime)
        _validate_scope(scope, runtime=runtime)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    scope_lit = cast(Scope, scope)
    if runtime == "codex":
        codex_report = codex_doctor_plugin(target, scope=scope_lit)
        emit_json_or_text(
            _codex_doctor_payload(codex_report),
            _codex_doctor_text(codex_report),
            flags=flags,
        )
        if not codex_report.clean:
            raise typer.Exit(exit_codes.INTEGRITY_VIOLATION)
        return
    if runtime == "opencode":
        oc_report = opencode_doctor_plugin(target, scope=scope_lit)
        emit_json_or_text(
            _opencode_doctor_payload(oc_report),
            _opencode_doctor_text(oc_report),
            flags=flags,
        )
        if not oc_report.clean:
            raise typer.Exit(exit_codes.INTEGRITY_VIOLATION)
        return
    claude_report = doctor_plugin(target)
    emit_json_or_text(_doctor_payload(claude_report), _doctor_text(claude_report), flags=flags)
    if not claude_report.clean:
        raise typer.Exit(exit_codes.INTEGRITY_VIOLATION)


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
    from eawf.runtimes.claude.plugin_install import IntegrityViolation
    from eawf.runtimes.claude.plugin_package import package_plugin
    from eawf.runtimes.codex import package_plugin as codex_package_plugin

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
                f"      (the [plugins.eawf] enabled=true block in ~/.codex/config.toml "
                f"activates the plugin; Codex has no separate 'plugin install' subcommand)"
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


def _sync_payload(result: SyncResult) -> dict[str, object]:
    """Render :class:`SyncResult` as the JSON envelope body."""
    return {
        "target_dir": str(result.target_dir),
        "scope": result.scope,
        "dry_run": result.dry_run,
        "skipped": list(result.skipped),
        "runtimes": [
            {
                "runtime": r.runtime,
                "deltas": [{"path": str(d.path), "action": d.action} for d in r.deltas],
            }
            for r in result.results
        ],
    }


def _sync_text(result: SyncResult) -> str:
    """Render :class:`SyncResult` as a human-readable summary."""
    verb = "dry-run" if result.dry_run else "wrote"
    parts = [f"plugin sync --scope {result.scope} ({verb}) → {result.target_dir}"]
    for runtime_result in result.results:
        parts.append(f"  {runtime_result.runtime}: {len(runtime_result.deltas)} files")
    if result.skipped:
        parts.append(f"  skipped: {', '.join(result.skipped)}")
    return "\n".join(parts)


def _normalise_sync_runtimes(values: list[str]) -> list[str]:
    """Map operator-facing aliases (``claude``) to canonical ids (``claude-code``).

    Raises:
        UserError: when a value is not a recognised alias (``kind="InvalidInput"``).
    """
    canonical: list[str] = []
    for value in values:
        canonical_id = _SYNC_RUNTIME_IDS.get(value)
        if canonical_id is None:
            raise cli_errors.UserError(
                f"unknown runtime {value!r}; expected one of "
                f"{sorted(set(_SYNC_RUNTIME_IDS.values()))}",
                kind="InvalidInput",
            )
        canonical.append(canonical_id)
    return canonical


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
    from eawf.runtimes.plugin_sync import PluginSyncIntegrityError, sync_plugins

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
        from eawf.runtimes.manifest import RuntimeId

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


__all__ = [
    "plugin_app",
]
