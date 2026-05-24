"""Evidence-area Typer apps: shared helpers + app registry (W02 deliverable).

This module owns the eight evidence Typer apps
(``goal_app`` / ``outcome_app`` / ``hypothesis_app`` / ``audit_app`` /
``incident_app`` / ``decision_app`` / ``artifact_app`` / ``backlog_app``)
and the small shared helpers every evidence handler composes. The concrete
command bodies live in four sibling modules:

- :mod:`eawf.surfaces.cli.commands.evidence_hypothesis` — goal / outcome /
  hypothesis.
- :mod:`eawf.surfaces.cli.commands.evidence_backlog` — audit / backlog.
- :mod:`eawf.surfaces.cli.commands.evidence_incident` — incident / decision.
- :mod:`eawf.surfaces.cli.commands.evidence_artifact` — artifact (add / update /
  show / validate / verify).

Each sibling imports the apps and shared helpers from this module and
attaches its handlers via ``@<app>.command(...)``. Importing this module
imports the siblings (at the bottom, after every shared symbol is
defined), so the decorators run and the apps carry their full verb set.
Existing import sites (``app.py``) keep resolving
``from eawf.surfaces.cli.commands.evidence import hypothesis_app`` and the seven
other apps unchanged.

Every state-mutating handler runs inside
:func:`eawf.surfaces.cli._mutation.state_transaction`, which holds
``portalock(state.json)`` across the load + mutate + validate + write
cycle. Library mutators (``define_*`` / ``add_*`` / ``set_*`` /
``verdict_*`` / ``close_*``) take the typed :class:`State` and mutate
it in place, returning the JSONL envelope(s) for the handler to append
after the transaction body completes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.draft import install_promote_command
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)


# ---- Shared helpers --------------------------------------------------------


def _flags(ctx: typer.Context) -> GlobalFlags:
    """Return the resolved :class:`GlobalFlags` from the Typer context."""
    flags = ctx.obj
    if not isinstance(flags, GlobalFlags):
        flags = GlobalFlags()
    return flags


def _state_path(flags: GlobalFlags) -> Path:
    """Resolve the state path or raise :class:`UserError` (``kind="NotFound"``)."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        raise cli_errors.UserError(str(exc), kind="NotFound") from exc


def _emit(payload: dict[str, Any], text: str, flags: GlobalFlags) -> None:
    emit_json_or_text(payload, text, flags=flags)


def _run_read(
    flags: GlobalFlags,
    fn: Any,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a read-only *fn* and translate :class:`CliError` into an envelope."""
    try:
        return fn(*args, **kwargs)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)


# ---- Typer apps ------------------------------------------------------------

goal_app = typer.Typer(
    name="goal",
    help="Manage project goals (define).",
    no_args_is_help=True,
)

outcome_app = typer.Typer(
    name="outcome",
    help="Manage outcomes (define / set).",
    no_args_is_help=True,
)

hypothesis_app = typer.Typer(
    name="hypothesis",
    help="Manage hypotheses (define / verdict / list).",
    no_args_is_help=True,
)

audit_app = typer.Typer(
    name="audit",
    help="Manage audits (add / run / integrity / show / list).",
    no_args_is_help=True,
)

incident_app = typer.Typer(
    name="incident",
    help="Manage incidents (open / close / view).",
    no_args_is_help=True,
)

decision_app = typer.Typer(
    name="decision",
    help="Manage decisions (add / supersede / list / graph).",
    no_args_is_help=True,
)

artifact_app = typer.Typer(
    name="artifact",
    help="Manage artifacts (add / show / verify).",
    no_args_is_help=True,
)

backlog_app = typer.Typer(
    name="backlog",
    help="Manage backlog items (add / close).",
    no_args_is_help=True,
)


# ---- Command registration --------------------------------------------------
# Importing the sibling modules runs their ``@<app>.command(...)`` decorators
# so the apps above carry their full verb set. The imports sit at the bottom,
# after every shared symbol is defined, so the siblings can import the apps and
# helpers from this module without a circular-import failure.
from eawf.surfaces.cli.commands import evidence_artifact as _evidence_artifact  # noqa: E402, F401
from eawf.surfaces.cli.commands import evidence_backlog as _evidence_backlog  # noqa: E402, F401
from eawf.surfaces.cli.commands import (  # noqa: E402
    evidence_hypothesis as _evidence_hypothesis,  # noqa: F401
)
from eawf.surfaces.cli.commands import evidence_incident as _evidence_incident  # noqa: E402, F401

install_promote_command(audit_app, "audit")
install_promote_command(hypothesis_app, "hypothesis")
install_promote_command(decision_app, "decision")
install_promote_command(incident_app, "incident")


# ---- Re-exports ------------------------------------------------------------

__all__ = [
    "artifact_app",
    "audit_app",
    "backlog_app",
    "decision_app",
    "goal_app",
    "hypothesis_app",
    "incident_app",
    "outcome_app",
]
