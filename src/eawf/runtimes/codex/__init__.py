"""Codex runtime adapter for Eä.

Renders a native Codex plugin tree under ``.codex/plugins/eawf/`` (project
scope) or ``~/.codex/plugins/eawf/`` (user scope) with the canonical
``.codex-plugin/plugin.json`` manifest. Skill / agent / hook bodies are
reused from :mod:`eawf.render.*`; the renderer is idempotent and the
install is byte-stable across re-runs.

Public re-exports:

    InstallResult, install_plugin, expected_paths, Scope, doctor_plugin,
    DoctorReport, CodexUserPluginConflict, detect_user_install
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

__all__ = [
    "CodexUserPluginConflict",
    "DoctorReport",
    "InstallResult",
    "Scope",
    "detect_user_install",
    "doctor_plugin",
    "expected_paths",
    "install_plugin",
]
