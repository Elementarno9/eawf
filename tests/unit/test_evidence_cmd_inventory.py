"""Verb-inventory regression guard for the evidence command split (P27-W07).

The evidence handlers were split out of a single 1482-LOC
``cli/commands/evidence.py`` into a thin re-export shim plus four sibling
modules (``evidence_hypothesis`` / ``evidence_backlog`` /
``evidence_incident`` / ``evidence_artifact``). These tests pin the exact
verb set each evidence Typer app carries so the split cannot silently
drop a command, and assert the public re-export surface
(``goal_app`` / ``outcome_app`` / ``hypothesis_app`` / ``audit_app`` /
``incident_app`` / ``decision_app`` / ``artifact_app`` / ``backlog_app``)
still resolves from the shim module.
"""

from __future__ import annotations

import typer

from eawf.cli.commands.evidence import (
    artifact_app,
    audit_app,
    backlog_app,
    decision_app,
    goal_app,
    hypothesis_app,
    incident_app,
    outcome_app,
)

EXPECTED_GOAL_VERBS = {"define"}
EXPECTED_OUTCOME_VERBS = {"define", "set"}
# ``audit`` / ``hypothesis`` / ``decision`` / ``incident`` each gain a
# ``promote`` verb via ``install_promote_command`` on the shim.
EXPECTED_HYPOTHESIS_VERBS = {"define", "verdict", "list", "promote"}
EXPECTED_AUDIT_VERBS = {
    "add",
    "run",
    "integrity",
    "set-verdict",
    "show",
    "list",
    "promote",
}
EXPECTED_INCIDENT_VERBS = {"open", "close", "view", "promote"}
EXPECTED_DECISION_VERBS = {"add", "list", "graph", "promote"}
EXPECTED_ARTIFACT_VERBS = {"add", "update", "show", "validate", "verify"}
EXPECTED_BACKLOG_VERBS = {"add", "set-priority", "close"}


def _verb_names(app: typer.Typer) -> set[str]:
    """Return the set of registered command names on *app*."""
    return {cmd.name for cmd in app.registered_commands if cmd.name is not None}


def test_goal_app_verb_inventory() -> None:
    assert _verb_names(goal_app) == EXPECTED_GOAL_VERBS


def test_outcome_app_verb_inventory() -> None:
    assert _verb_names(outcome_app) == EXPECTED_OUTCOME_VERBS


def test_hypothesis_app_verb_inventory() -> None:
    assert _verb_names(hypothesis_app) == EXPECTED_HYPOTHESIS_VERBS


def test_audit_app_verb_inventory() -> None:
    assert _verb_names(audit_app) == EXPECTED_AUDIT_VERBS


def test_incident_app_verb_inventory() -> None:
    assert _verb_names(incident_app) == EXPECTED_INCIDENT_VERBS


def test_decision_app_verb_inventory() -> None:
    assert _verb_names(decision_app) == EXPECTED_DECISION_VERBS


def test_artifact_app_verb_inventory() -> None:
    assert _verb_names(artifact_app) == EXPECTED_ARTIFACT_VERBS


def test_backlog_app_verb_inventory() -> None:
    assert _verb_names(backlog_app) == EXPECTED_BACKLOG_VERBS


def test_full_app_import_keeps_evidence_apps() -> None:
    # Importing the full CLI app must keep the evidence apps the shim
    # re-exports (proving app.py wires the same Typer instances).
    from eawf.cli import app as app_module
    from eawf.cli.commands.evidence import hypothesis_app as shim_hypothesis_app

    assert app_module.app is not None
    assert _verb_names(shim_hypothesis_app) == EXPECTED_HYPOTHESIS_VERBS
