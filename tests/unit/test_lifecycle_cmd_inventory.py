"""Verb-inventory regression guard for the lifecycle command split (P27-W06).

The lifecycle handler module was split out of a single 2809-LOC
``cli/commands/lifecycle.py`` into a thin re-export shim plus four sibling
modules (``lifecycle_phase`` / ``lifecycle_iter`` / ``lifecycle_wave`` /
``lifecycle_wave_read``). These tests pin the exact verb set each lifecycle
Typer app carries so the split cannot silently drop a command, and assert
the public re-export surface (``phase_app`` / ``project_app`` /
``subproject_app`` / ``iter_app`` / ``wave_app`` / ``wave_budget_app`` plus
the ``_run_mutation`` / ``_load_state_readonly`` / ``_compute_iter_bump_hints``
helpers) still resolves from the shim module.
"""

from __future__ import annotations

import typer

from eawf.surfaces.cli.commands.lifecycle import (
    _compute_iter_bump_hints,
    _load_state_readonly,
    _run_mutation,
    iter_app,
    phase_app,
    project_app,
    subproject_app,
    wave_app,
    wave_budget_app,
)

# Lifecycle-owned verb set (the commands the split moved). External modules
# (``wave_ci`` / ``pr_review`` / ``worktree`` / ``wave_policy``) attach extra
# verbs to ``wave_app`` on import via ``app.py``; those are NOT owned by this
# wave, so the wave assertion checks containment (superset), not equality.
EXPECTED_PROJECT_VERBS = {"init"}
EXPECTED_SUBPROJECT_VERBS = {"add", "switch"}
EXPECTED_PHASE_VERBS = {"open", "close", "activate", "reopen", "prepare-close", "retro"}
EXPECTED_ITER_VERBS = {"activate", "open", "close", "plan"}
EXPECTED_WAVE_VERBS = {
    "plan",
    "claim",
    "close",
    "show",
    "fail",
    "update",
    "graph",
    "next-ready",
    "blocks-rebuild",
    "dispatch",
    "dispatch-batch",
    "verify-commits",
}
EXPECTED_WAVE_BUDGET_VERBS = {"set", "consume", "show"}


def _verb_names(app: typer.Typer) -> set[str]:
    """Return the set of registered command names on *app*."""
    return {cmd.name for cmd in app.registered_commands if cmd.name is not None}


def _group_names(app: typer.Typer) -> set[str]:
    """Return the set of registered sub-typer (group) names on *app*."""
    return {grp.name for grp in app.registered_groups if grp.name is not None}


def test_project_app_verb_inventory() -> None:
    assert _verb_names(project_app) == EXPECTED_PROJECT_VERBS


def test_subproject_app_verb_inventory() -> None:
    assert _verb_names(subproject_app) == EXPECTED_SUBPROJECT_VERBS


def test_phase_app_verb_inventory() -> None:
    assert _verb_names(phase_app) == EXPECTED_PHASE_VERBS


def test_iter_app_verb_inventory() -> None:
    assert _verb_names(iter_app) == EXPECTED_ITER_VERBS


def test_wave_app_owns_every_lifecycle_verb() -> None:
    # Containment, not equality: external modules add fix-ci / land / review
    # / etc. to ``wave_app`` via ``app.py``. The split must not drop any of
    # the lifecycle-owned wave verbs.
    assert _verb_names(wave_app) >= EXPECTED_WAVE_VERBS


def test_wave_budget_subapp_verb_inventory() -> None:
    assert _verb_names(wave_budget_app) == EXPECTED_WAVE_BUDGET_VERBS


def test_wave_app_registers_budget_subapp() -> None:
    assert "budget" in _group_names(wave_app)


def test_lifecycle_shim_reexports_helpers() -> None:
    # The siblings import these helpers from the shim; existing call sites
    # (``wave_ci`` / ``pr_review`` / tests) import them back from the shim.
    assert callable(_run_mutation)
    assert callable(_load_state_readonly)
    assert callable(_compute_iter_bump_hints)


def test_full_app_import_keeps_lifecycle_apps() -> None:
    # Importing the full CLI app triggers the external wave registrations.
    # The lifecycle apps must still be the same objects app.py wired in.
    from eawf.surfaces.cli import app as app_module
    from eawf.surfaces.cli.commands.lifecycle import wave_app as shim_wave_app

    assert app_module.app is not None
    # The external fix-ci / land / review verbs land on the SAME wave_app
    # the shim re-exports, proving the re-export shares one Typer instance.
    assert {"fix-ci", "land", "review"} <= _verb_names(shim_wave_app)
