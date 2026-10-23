"""Lifecycle nouns: the Typer app registry.

This module owns the lifecycle Typer apps: ``project_app``, ``track_app``,
``phase_app``, ``iter_app``, ``wave_app`` (plus its ``budget`` sub-app),
and the epoch-2 nouns ``milestone_app``, ``batch_app``, ``task_app``,
``run_app`` and ``repository_app``. It holds no command body: the native
verbs live in :mod:`eawf.surfaces.cli.commands.domain` and its siblings,
which forward to the daemon's per-entity lifecycle RPCs and attach their
handlers to these apps via ``@<app>.command(...)``. Importing this module
imports :mod:`~eawf.surfaces.cli.commands.domain` at the bottom, after
every app is defined, so the decorators run and the apps carry their full
verb set.
"""

from __future__ import annotations

import logging

import typer

logger = logging.getLogger(__name__)


# ---- Typer apps -------------------------------------------------------------

project_app = typer.Typer(
    name="project",
    help="Project-level lifecycle (init).",
    no_args_is_help=True,
)
track_app = typer.Typer(
    name="track",
    help="Track lifecycle (create, retire).",
    no_args_is_help=True,
)
phase_app = typer.Typer(
    name="phase",
    help="Phase lifecycle (open, close, reopen).",
    no_args_is_help=True,
)
iter_app = typer.Typer(
    name="iter",
    help="Iteration lifecycle (open, close).",
    no_args_is_help=True,
)
wave_app = typer.Typer(
    name="wave",
    help="Wave lifecycle (plan, claim, close, fail, graph, next-ready).",
    no_args_is_help=True,
)

wave_budget_app = typer.Typer(
    name="budget",
    help="Per-wave token-budget cap (set, consume, show).",
    no_args_is_help=True,
)
wave_app.add_typer(wave_budget_app, name="budget")

# The epoch-2 lifecycle nouns. They carry no epoch-1 verbs at all: each
# one is a native per-entity move, and the handlers live in the sibling
# ``domain`` module beside the Track verb that joins this app's own.
milestone_app = typer.Typer(
    name="milestone",
    help="Milestone lifecycle (activate, open-review, open-approval, accept, cancel, set-target).",
    no_args_is_help=True,
)
batch_app = typer.Typer(
    name="batch",
    help=(
        "Delivery-batch lifecycle (activate, integrate, adopt-landed, ready, merge, reconcile, "
        "observe-merge, complete, set-title)."
    ),
    no_args_is_help=True,
)
task_app = typer.Typer(
    name="task",
    help="Task lifecycle (promote, claim, start, submit, seal, prove, assess, ready, complete).",
    no_args_is_help=True,
)
run_app = typer.Typer(
    name="run",
    help="Run lifecycle (create, start, finish, fail).",
    no_args_is_help=True,
)
repository_app = typer.Typer(
    name="repository",
    help="Repository rows a plan binds its head to (create).",
    no_args_is_help=True,
)


# ---- Command registration ---------------------------------------------------
# Importing the domain module runs its ``@<app>.command(...)`` decorators, and
# its own sibling registrations, so the apps above carry their full verb set.
# The import sits at the bottom, after every shared symbol is defined, so the
# domain modules can import the apps from this module without a circular-import
# failure.
from eawf.surfaces.cli.commands import domain as _domain  # noqa: E402, F401

# ---- Re-exports -------------------------------------------------------------

__all__ = [
    "batch_app",
    "iter_app",
    "milestone_app",
    "phase_app",
    "project_app",
    "run_app",
    "task_app",
    "track_app",
    "wave_app",
    "wave_budget_app",
]
