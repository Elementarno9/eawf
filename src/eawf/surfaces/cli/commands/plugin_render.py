"""JSON-envelope + human-text renderers for ``eawf plugin ...`` results.

Split out of :mod:`eawf.surfaces.cli.commands.plugin` (P27-I05-W09). These are
pure presentation helpers — each takes a runtime install / doctor /
package / sync result (or report) and returns either the JSON envelope
body (``dict``) or the human-readable text summary (``str``). They hold
no state and call nothing in ``eawf.runtime.runtimes`` at runtime; the result
types are annotation-only imports under ``if TYPE_CHECKING:`` so this
module stays off the import-budget heavy path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from eawf.runtime.runtimes.claude.plugin_doctor import DoctorReport
    from eawf.runtime.runtimes.claude.plugin_install import InstallResult
    from eawf.runtime.runtimes.claude.plugin_package import PackageResult
    from eawf.runtime.runtimes.codex.plugin_doctor import DoctorReport as CodexDoctorReport
    from eawf.runtime.runtimes.codex.plugin_install import (
        InstallResult as CodexInstallResult,
    )
    from eawf.runtime.runtimes.codex.plugin_package import PackageResult as CodexPackageResult
    from eawf.runtime.runtimes.opencode.plugin_doctor import DoctorReport as OpencodeDoctorReport
    from eawf.runtime.runtimes.opencode.plugin_install import (
        InstallResult as OpencodeInstallResult,
    )
    from eawf.runtime.runtimes.plugin_doctor import PluginDoctorReport
    from eawf.runtime.runtimes.plugin_sync import SyncResult


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
