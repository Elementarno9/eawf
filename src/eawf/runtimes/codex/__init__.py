"""Codex runtime adapter for Eä (P14-W06 / D12).

Mirrors the Claude Code adapter package layout but emits a Codex tree
under ``.codex/`` plus a TOML config (``.codex/config.toml``) for the
managed MCP namespace. Skill / agent / hook bodies are reused from
:mod:`eawf.render.*`; the renderer is idempotent and the install run
is byte-stable across re-runs.

Public re-exports:

    InstallResult, install_plugin, expected_paths, doctor_plugin
"""

from __future__ import annotations

from eawf.runtimes.codex.plugin_doctor import DoctorReport, doctor_plugin
from eawf.runtimes.codex.plugin_install import (
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
