"""OpenCode runtime adapter for Eä (P14-W07 / D12 + D13).

Emits an OpenCode-shaped plugin tree under the workspace root:

- ``opencode.json`` with an ``mcp`` block + ``managed`` namespace
- ``plugin.js`` — the untyped JS bridge that forwards hook events to
  ``eawf hook`` over stdio (no TypeScript, no build step per D13)

Public re-exports:

    InstallResult, install_plugin, expected_paths, doctor_plugin
"""

from __future__ import annotations

from eawf.runtimes.opencode.plugin_doctor import DoctorReport, doctor_plugin
from eawf.runtimes.opencode.plugin_install import (
    InstallResult,
    expected_paths,
    install_plugin,
)

__all__ = [
    "DoctorReport",
    "InstallResult",
    "doctor_plugin",
    "expected_paths",
    "install_plugin",
]
