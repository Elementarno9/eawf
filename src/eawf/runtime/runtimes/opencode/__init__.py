"""OpenCode runtime adapter for Eä.

Renders a native OpenCode plugin under ``.opencode/plugins/eawf.js``
(project scope) or ``$OPENCODE_CONFIG_DIR/plugins/eawf.js`` (user
scope; defaults to ``~/.config/opencode/plugins/eawf.js``). The
managed-bytes registry lives in a sidecar
``.eawf-managed.json`` next to the plugin file; ``opencode.json`` is
patched only in its ``mcp`` block.

Public re-exports:

    InstallResult, install_plugin, expected_paths, Scope, doctor_plugin,
    DoctorReport, OpenCodeUserPluginConflict, detect_user_install
"""

from __future__ import annotations

from eawf.runtime.runtimes.opencode.plugin_conflict import (
    OpenCodeUserPluginConflict,
    detect_user_install,
)
from eawf.runtime.runtimes.opencode.plugin_doctor import DoctorReport, doctor_plugin
from eawf.runtime.runtimes.opencode.plugin_install import (
    InstallResult,
    Scope,
    expected_paths,
    install_plugin,
)

__all__ = [
    "DoctorReport",
    "InstallResult",
    "OpenCodeUserPluginConflict",
    "Scope",
    "detect_user_install",
    "doctor_plugin",
    "expected_paths",
    "install_plugin",
]
