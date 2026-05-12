"""Codex runtime adapter for Eä.

Renders a native Codex plugin tree under ``.codex/plugins/eawf/`` (project
scope) or ``~/.codex/plugins/eawf/`` (user scope) with the canonical
``.codex-plugin/plugin.json`` manifest. Skill / agent / hook bodies are
reused from :mod:`eawf.render.*`; the renderer is idempotent and the
install is byte-stable across re-runs.

``package_plugin`` emits a standalone marketplace tree the operator can
register via ``codex plugin marketplace add <path>`` — required because
Codex does not auto-load from the user-scope ``~/.codex/plugins/`` dir.

Public re-exports:

    InstallResult, install_plugin, expected_paths, Scope, doctor_plugin,
    DoctorReport, CodexUserPluginConflict, detect_user_install,
    PackageResult, package_plugin
"""

from __future__ import annotations

from eawf.runtimes.codex.plugin_conflict import CodexUserPluginConflict, detect_user_install
from eawf.runtimes.codex.plugin_doctor import DoctorReport, doctor_plugin
from eawf.runtimes.codex.plugin_install import (
    InstallResult,
    Scope,
    expected_paths,
    install_plugin,
)
from eawf.runtimes.codex.plugin_package import PackageResult, package_plugin

__all__ = [
    "CodexUserPluginConflict",
    "DoctorReport",
    "InstallResult",
    "PackageResult",
    "Scope",
    "detect_user_install",
    "doctor_plugin",
    "expected_paths",
    "install_plugin",
    "package_plugin",
]
