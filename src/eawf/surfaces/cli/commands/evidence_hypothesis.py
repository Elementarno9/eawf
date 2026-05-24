"""Goal / outcome / hypothesis command handlers.

Split out of :mod:`eawf.surfaces.cli.commands.evidence` (P27-W07). The
``goal_app`` / ``outcome_app`` / ``hypothesis_app`` Typer apps and the
shared helpers live in the parent module; this module attaches the
command bodies via ``@<app>.command(...)``.
"""

from __future__ import annotations

import logging
from typing import Annotated

import typer

from eawf.kernel.state.enums import (
    HypothesisStatus,
    HypothesisVerdict,
    OutcomeDirection,
    OutcomeStatus,
    StoreKind,
)
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.evidence import (
    _emit,
    _flags,
    _run_read,
    _state_path,
    goal_app,
    hypothesis_app,
    outcome_app,
)

logger = logging.getLogger(__name__)


# ---- goal ------------------------------------------------------------------


@goal_app.command("define")
def goal_define(
    ctx: typer.Context,
    goal_id: Annotated[str, typer.Argument(help="Goal id (e.g. G01)")],
    title: Annotated[str, typer.Option("--title", help="Human-readable title")],
    summary: Annotated[
        str,
        typer.Option("--summary", help="One-line summary of the goal"),
    ] = "",
    scope_id: Annotated[
        str | None,
        typer.Option(
            "--scope-id",
            help="Owning scope (project/subproject id). Defaults to project code.",
        ),
    ] = None,
) -> None:
    """Define a new goal under the current scope."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import goal as goal_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            resolved_scope = scope_id
            if resolved_scope is None:
                if state.project is None:
                    raise cli_errors.UserError(
                        "scope_id required when state.project is unset", kind="InvalidInput"
                    )
                resolved_scope = state.project.code
            event = goal_evi.define_goal(
                state,
                goal_id=goal_id,
                title=title,
                summary=summary or title,
                scope_id=resolved_scope,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {"goal_id": goal_id, "scope_id": resolved_scope, "status": "open"},
        f"goal {goal_id} defined",
        flags,
    )


# ---- outcome ---------------------------------------------------------------


@outcome_app.command("define")
def outcome_define(
    ctx: typer.Context,
    outcome_id: Annotated[str, typer.Argument(help="Outcome id")],
    scope_id: Annotated[str, typer.Option("--scope-id", help="Owning scope id")],
    metric: Annotated[str, typer.Option("--metric", help="Metric name")],
    threshold: Annotated[float, typer.Option("--threshold", help="Threshold value")],
    direction: Annotated[
        OutcomeDirection,
        typer.Option("--direction", help="Threshold direction"),
    ] = OutcomeDirection.MIN,
) -> None:
    """Define a new pending outcome."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import outcome as outcome_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            event = outcome_evi.define_outcome(
                state,
                outcome_id=outcome_id,
                scope_id=scope_id,
                metric=metric,
                threshold=threshold,
                direction=direction,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "outcome_id": outcome_id,
            "scope_id": scope_id,
            "metric": metric,
            "threshold": threshold,
            "direction": direction.value,
            "status": "pending",
        },
        f"outcome {outcome_id} defined",
        flags,
    )


@outcome_app.command("set")
def outcome_set(
    ctx: typer.Context,
    outcome_id: Annotated[str, typer.Argument(help="Outcome id")],
    value: Annotated[float, typer.Option("--value", help="Measured value")],
    status: Annotated[OutcomeStatus, typer.Option("--status", help="met/missed/waived")],
    audit: Annotated[
        str,
        typer.Option(
            "--audit",
            help="Audit id (must reference a complete audit)",
        ),
    ],
) -> None:
    """Record an outcome measurement; requires --audit of a complete audit."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import outcome as outcome_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            event = outcome_evi.set_outcome(
                state,
                outcome_id=outcome_id,
                value=value,
                status=status,
                audit_id=audit,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "outcome_id": outcome_id,
            "value": value,
            "status": status.value,
            "audit_id": audit,
        },
        f"outcome {outcome_id} set status={status.value}",
        flags,
    )


# ---- hypothesis ------------------------------------------------------------


@hypothesis_app.command("define")
def hypothesis_define(
    ctx: typer.Context,
    hypothesis_id: Annotated[str, typer.Argument(help="Hypothesis id (e.g. H03-12)")],
    scope_id: Annotated[str, typer.Option("--scope-id", help="Owning scope id")],
    text: Annotated[str, typer.Option("--text", help="Falsifiable claim text")],
    metric: Annotated[str, typer.Option("--metric", help="Metric name")],
    confirm: Annotated[str, typer.Option("--confirm", help="Confirmation criterion")],
    reject: Annotated[str, typer.Option("--reject", help="Rejection criterion")],
    source: Annotated[
        str | None,
        typer.Option("--source", help="Source artifact id"),
    ] = None,
) -> None:
    """Register a new pending hypothesis."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import hypothesis as hypothesis_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            event = hypothesis_evi.define_hypothesis(
                state,
                hypothesis_id=hypothesis_id,
                scope_id=scope_id,
                text=text,
                metric=metric,
                confirm=confirm,
                reject=reject,
                source_artifact_id=source,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "hypothesis_id": hypothesis_id,
            "scope_id": scope_id,
            "metric": metric,
            "status": "pending",
        },
        f"hypothesis {hypothesis_id} defined",
        flags,
    )


@hypothesis_app.command("verdict")
def hypothesis_verdict(
    ctx: typer.Context,
    hypothesis_id: Annotated[str, typer.Argument(help="Hypothesis id")],
    verdict: Annotated[
        HypothesisVerdict,
        typer.Option("--verdict", help="confirmed/rejected/inconclusive"),
    ],
    audit: Annotated[
        str,
        typer.Option(
            "--audit",
            help="Audit id (must reference a complete audit)",
        ),
    ],
) -> None:
    """Record a hypothesis verdict; requires --audit of a complete audit."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import hypothesis as hypothesis_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            event = hypothesis_evi.set_verdict(
                state,
                hypothesis_id=hypothesis_id,
                verdict=verdict,
                audit_id=audit,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "hypothesis_id": hypothesis_id,
            "verdict": verdict.value,
            "audit_id": audit,
        },
        f"hypothesis {hypothesis_id} verdict={verdict.value}",
        flags,
    )


@hypothesis_app.command("list")
def hypothesis_list(
    ctx: typer.Context,
    scope_id: Annotated[str | None, typer.Option("--scope-id", help="Filter by scope id")] = None,
    status: Annotated[
        HypothesisStatus | None,
        typer.Option("--status", help="Filter by status"),
    ] = None,
) -> None:
    """List hypotheses (read-only)."""
    from eawf.workflow.evidence import hypothesis as hypothesis_evi
    from eawf.workflow.evidence._io import load_state

    flags = _flags(ctx)
    state_path = _state_path(flags)

    state = _run_read(flags, load_state, state_path)
    items = hypothesis_evi.list_hypotheses(state, scope_id=scope_id, status=status)

    payload = {
        "hypotheses": [
            {
                "id": h.id,
                "scope_id": h.scope_id,
                "metric": h.metric,
                "status": h.status.value,
                "verdict": h.verdict.value if h.verdict else None,
                "audit_id": h.audit_id,
            }
            for h in items
        ]
    }
    text = "\n".join(f"{h.id}\t{h.status.value}\t{h.metric}" for h in items) or "(none)"
    _emit(payload, text, flags)
