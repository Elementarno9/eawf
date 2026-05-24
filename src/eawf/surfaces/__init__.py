"""Surfaces layer: operator-facing CLI, TUI, and document rendering.

The surfaces super-package groups the packages that present eawf to the
operator on top of the kernel, workflow, and runtime layers:
:mod:`~eawf.surfaces.cli` (the Typer dispatch + command handlers),
:mod:`~eawf.surfaces.tui` (the Textual operator surface), and
:mod:`~eawf.surfaces.render` (managed-region markers, manifest, drift
detection, and doc rendering).
"""

from __future__ import annotations
