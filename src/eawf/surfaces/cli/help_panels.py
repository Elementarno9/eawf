"""Registry-aligned ``rich_help_panel`` assignments for the ``eawf`` CLI.

The interactive ``eawf config`` menu groups operator-tunable keys by the
:data:`eawf.kernel.config.registry.CONFIG_REGISTRY` tab — alphabetical tabs and
alphabetical-by-key fields within each tab (P20-W10). This module extends the
same alphabetical convention to the top-level ``eawf --help`` listing so the
two surfaces share one canonical grouping order.

Public API:

- :data:`COMMAND_PANELS` — command name → panel name (panel name is one of
  the :data:`eawf.kernel.config.registry.CONFIG_REGISTRY` tabs).
- :data:`PANEL_ORDER` — alphabetical tuple of panel names; the
  :class:`RegistryOrderedTyperGroup` uses this to enforce panel ordering.
- :func:`panel_for` — resolve a command name to its panel.
- :class:`RegistryOrderedTyperGroup` — :class:`typer.core.TyperGroup`
  subclass that returns commands sorted by ``(panel, name)`` so Rich's
  panel-grouping rendering emits panels in alphabetical order.

Ordering policy (mirrors :mod:`eawf.kernel.config.registry`):

* Panels are rendered in alphabetical order of their panel name.
* Commands within a panel are rendered in alphabetical order of their
  command name (Click's default :meth:`~click.Group.list_commands` already
  sorts; the custom subclass re-sorts after partitioning by panel).

Hidden commands (e.g. ``scope-debug``) are not assigned a panel — Rich
filters them out of the rendered help anyway. The module asserts at import
time that every assigned panel name belongs to the registry tab set so a
future :data:`CONFIG_REGISTRY` rename forces a coordinated edit here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from typer.core import TyperGroup

from eawf.kernel.config.registry import tabs_sorted

if TYPE_CHECKING:
    import click

logger = logging.getLogger(__name__)


# Alphabetical tuple of panel names — sourced from the metadata registry so
# the panel set cannot drift from the config menu's tab set without an
# import-time assertion failure (see :func:`_assert_panels_match_registry`).
PANEL_ORDER: tuple[str, ...] = tabs_sorted()


# Command name → panel name. Every non-hidden command registered on the root
# ``eawf`` Typer app MUST appear here; the integration test
# ``test_cli_help_groups`` walks the registered command set and fails when a
# command is missing.
#
# Mapping rationale: each command is placed under the registry tab whose
# domain it most closely serves. The catch-all bucket is ``planning`` since
# the majority of eawf nouns drive a roadmap / wave / plan workflow.
COMMAND_PANELS: dict[str, str] = {
    # audit: validation, audit checks, doctor diagnostics, doc-drift checks.
    "audit": "audit",
    "backfill": "audit",
    "backup": "audit",
    "doc": "audit",
    "doctor": "audit",
    "evidence": "audit",
    "migrate": "audit",
    "schema": "audit",
    "snapshot": "audit",
    "validate": "audit",
    "vfl": "audit",
    # estimation: EU estimates / actuals, impact graph, rolling metrics,
    # perf bench harness, effort-bucket calibration.
    "actual": "estimation",
    "bench": "estimation",
    "calibrate": "estimation",
    "estimate": "estimation",
    "impact": "estimation",
    "metrics": "estimation",
    "telemetry": "estimation",
    # planning: roadmap / wave / iter / phase nouns + research + memory.
    "agent-report": "planning",
    "artifact": "planning",
    "backlog": "planning",
    "decision": "planning",
    "draft": "planning",
    "goal": "planning",
    "hypothesis": "planning",
    "incident": "planning",
    "iter": "planning",
    "memory": "planning",
    "operator": "planning",
    "outcome": "planning",
    "phase": "planning",
    "plan": "planning",
    "project": "planning",
    "research": "planning",
    "roadmap": "planning",
    "session": "planning",
    "spec": "planning",
    "subproject": "planning",
    "wave": "planning",
    # runtime: harness adapters, hooks, plugins, skills, MCP, profiles,
    # eawfd daemon.
    "cc": "runtime",
    "config": "runtime",
    "daemon": "runtime",
    "hook": "runtime",
    "mcp": "runtime",
    "plugin": "runtime",
    "profile": "runtime",
    "skill": "runtime",
    # ship: PR / wiki / release artifacts and the sync renderer.
    "pr": "ship",
    "release": "ship",
    "sync": "ship",
    "wiki": "ship",
    # ui: surfaces that drive the terminal display.
    "completion": "ui",
    "help": "ui",
    "render-output": "ui",
    "status": "ui",
    "tui": "ui",
    "version": "ui",
    # vcs: VCS-adjacent operations (co-author, repo + workspace state,
    # clone, init are the entry points into a tracked workspace).
    "clone-repo": "vcs",
    "coauthor": "vcs",
    "init": "vcs",
    "repo": "vcs",
    "state": "vcs",
    "store": "vcs",
    "wal": "vcs",
    "workspace": "vcs",
    # worktrees: per-wave worktree dispatch and the flow skill that drives
    # the parallel-wave loop, plus the headless dispatch pause/resume toggle.
    "flow": "worktrees",
    "worktree": "worktrees",
    "dispatch": "worktrees",
}


def _assert_panels_match_registry() -> None:
    """Module-load guard: every assigned panel must be a registered tab.

    Raises:
        AssertionError: When :data:`COMMAND_PANELS` references a panel name
            that no longer appears in :data:`PANEL_ORDER` — typically caused
            by a tab rename in :data:`eawf.kernel.config.registry.CONFIG_REGISTRY`
            without a matching edit here.
    """
    assigned = set(COMMAND_PANELS.values())
    registry = set(PANEL_ORDER)
    unknown = assigned - registry
    if unknown:
        raise AssertionError(
            f"unknown panel(s) in COMMAND_PANELS: {sorted(unknown)} "
            f"(registry tabs: {sorted(registry)})"
        )


_assert_panels_match_registry()


def panel_for(command_name: str) -> str | None:
    """Return the panel name for *command_name* or ``None`` if unmapped.

    Args:
        command_name: Root-level command name as registered on the
            ``eawf`` Typer app (e.g. ``"wave"``, ``"audit"``).

    Returns:
        The panel name (one of :data:`PANEL_ORDER`) for the command, or
        ``None`` when the command is hidden / unmapped. ``None`` callers
        should fall back to Typer's default ``"Commands"`` panel.
    """
    return COMMAND_PANELS.get(command_name)


class RegistryOrderedTyperGroup(TyperGroup):
    """Typer group that renders panels in alphabetical-by-panel-name order.

    Rich's panel renderer iterates ``panel_to_commands`` in dict-insertion
    order. The first command encountered for each panel determines panel
    order. Click's default :meth:`list_commands` already returns commands
    in alphabetical order, but that does NOT yield alphabetical-by-panel
    order — e.g. command ``"audit"`` (panel ``audit``) and command
    ``"actual"`` (panel ``estimation``) place ``estimation`` before
    ``audit`` because ``"actual" < "audit"``.

    The override re-sorts the returned command name list by
    ``(panel_for(name) or zzz, name)`` so panels emit alphabetical and
    unmapped/hidden commands fall through to the end under the default
    ``"Commands"`` panel.
    """

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = list(super().list_commands(ctx))
        # Unmapped commands sort after every mapped panel via a sentinel
        # that compares greater than any real panel name.
        return sorted(names, key=lambda n: (COMMAND_PANELS.get(n, "~zzz"), n))


__all__ = [
    "COMMAND_PANELS",
    "PANEL_ORDER",
    "RegistryOrderedTyperGroup",
    "panel_for",
]
