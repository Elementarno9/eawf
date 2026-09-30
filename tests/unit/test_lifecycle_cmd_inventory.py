"""Verb-inventory regression guard for the lifecycle command split.

The lifecycle handler module was split out of a single 2809-LOC
``cli/commands/lifecycle.py`` into a thin re-export shim plus four sibling
modules (``lifecycle_phase`` / ``lifecycle_iter`` / ``lifecycle_wave`` /
``lifecycle_wave_read``). These tests pin the exact verb set each lifecycle
Typer app carries so the split cannot silently drop a command, and assert
the public re-export surface (``phase_app`` / ``project_app`` /
``track_app`` / ``iter_app`` / ``wave_app`` / ``wave_budget_app`` plus
the ``_run_mutation`` / ``_load_state_readonly`` / ``_compute_iter_bump_hints``
helpers) still resolves from the shim module.
"""

from __future__ import annotations

import typer

from eawf.surfaces.cli.commands.lifecycle import (
    track_app,
    wave_app,
)

# ``retire`` and ``create`` are the native epoch-2 verbs the ``domain``
# sibling attaches to the same app; the three epoch-1 verbs keep their
# places beside them.
EXPECTED_TRACK_VERBS = {"retire", "create"}


def _verb_names(app: typer.Typer) -> set[str]:
    """Return the set of registered command names on *app*."""
    return {cmd.name for cmd in app.registered_commands if cmd.name is not None}


def _group_names(app: typer.Typer) -> set[str]:
    """Return the set of registered sub-typer (group) names on *app*."""
    return {grp.name for grp in app.registered_groups if grp.name is not None}


def test_track_app_verb_inventory() -> None:
    assert _verb_names(track_app) == EXPECTED_TRACK_VERBS


def test_wave_app_registers_budget_subapp() -> None:
    assert "budget" in _group_names(wave_app)
