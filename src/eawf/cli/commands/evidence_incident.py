"""Incident / decision command handlers.

Split out of :mod:`eawf.cli.commands.evidence` (P27-W07). The
``incident_app`` / ``decision_app`` Typer apps and the shared helpers
live in the parent module; this module attaches the command bodies via
``@<app>.command(...)``.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Annotated

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.commands.evidence import (
    _emit,
    _flags,
    _run_read,
    _state_path,
    decision_app,
    incident_app,
)
from eawf.state.enums import (
    IncidentSeverity,
    StoreKind,
)

logger = logging.getLogger(__name__)


# ---- incident --------------------------------------------------------------


@incident_app.command("open")
def incident_open(
    ctx: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident id")],
    severity: Annotated[
        IncidentSeverity,
        typer.Option("--severity", help="low / medium / high / critical"),
    ],
    title: Annotated[str, typer.Option("--title", help="Short title")],
    scope_id: Annotated[
        str | None,
        typer.Option(
            "--scope-id",
            help="Owning scope id. Defaults to project code.",
        ),
    ] = None,
) -> None:
    """Open a new incident."""
    from eawf.cli._mutation import state_transaction
    from eawf.evidence import incident as incident_evi
    from eawf.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            resolved_scope = scope_id
            if resolved_scope is None:
                if state.project is None:
                    raise cli_errors.InvalidInput("scope_id required when state.project is unset")
                resolved_scope = state.project.code
            record, event = incident_evi.open_incident(
                state,
                incident_id=incident_id,
                scope_id=resolved_scope,
                severity=severity,
                title=title,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.INCIDENT], record)
            append_jsonl(paths[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "incident_id": incident_id,
            "severity": severity.value,
            "title": title,
            "status": "open",
        },
        f"incident {incident_id} opened severity={severity.value}",
        flags,
    )


@incident_app.command("close")
def incident_close(
    ctx: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident id")],
    root_cause: Annotated[str, typer.Option("--root-cause", help="Identified root cause")],
    audit: Annotated[
        str,
        typer.Option(
            "--audit",
            help="Audit id (must reference a complete audit)",
        ),
    ],
    corrective_action: Annotated[
        list[str] | None,
        typer.Option(
            "--corrective-action",
            help="Corrective-action id (repeatable)",
        ),
    ] = None,
) -> None:
    """Close an incident; requires --audit of a complete audit."""
    from eawf.cli._mutation import state_transaction
    from eawf.evidence import incident as incident_evi
    from eawf.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)
    actions = list(corrective_action or [])

    try:
        with state_transaction(state_path) as state:
            record, event = incident_evi.close_incident(
                state,
                incident_id=incident_id,
                root_cause=root_cause,
                corrective_action_ids=actions,
                audit_id=audit,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.INCIDENT], record)
            append_jsonl(paths[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "incident_id": incident_id,
            "root_cause": root_cause,
            "corrective_action_ids": actions,
            "audit_id": audit,
            "status": "resolved",
        },
        f"incident {incident_id} closed",
        flags,
    )


@incident_app.command("view")
def incident_view(
    ctx: typer.Context,
    incident_id: Annotated[str, typer.Argument(help="Incident id")],
) -> None:
    """View incident metadata + linked artifact ids."""
    from eawf.evidence import incident as incident_evi
    from eawf.evidence._io import load_state

    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    incident = _run_read(flags, incident_evi.view_incident, state, incident_id)
    payload = json.loads(incident.model_dump_json())
    _emit(
        payload,
        (
            f"incident {incident.id} severity={incident.severity.value} "
            f"status={incident.status.value}"
        ),
        flags,
    )


# ---- decision --------------------------------------------------------------


@decision_app.command("add")
def decision_add(
    ctx: typer.Context,
    decision_id: Annotated[str, typer.Argument(help="Decision id (e.g. D012)")],
    scope_id: Annotated[str, typer.Option("--scope-id", help="Owning scope id")],
    summary: Annotated[str, typer.Option("--summary", help="One-line summary")],
    rationale: Annotated[str, typer.Option("--rationale", help="Why this decision")],
    alternative: Annotated[
        list[str] | None,
        typer.Option("--alternative", help="Alternative considered (repeatable)"),
    ] = None,
    supersedes: Annotated[
        str | None,
        typer.Option(
            "--supersedes",
            help="ID of the ACTIVE decision this row supersedes; flips parent "
            "status to SUPERSEDED and sets parent.superseded_by atomically.",
        ),
    ] = None,
) -> None:
    """Record a durable decision."""
    from eawf.cli._mutation import state_transaction
    from eawf.evidence import decision as decision_evi
    from eawf.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            record, event = decision_evi.add_decision(
                state,
                decision_id=decision_id,
                scope_id=scope_id,
                summary=summary,
                rationale=rationale,
                alternatives=list(alternative or []),
                supersedes=supersedes,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.DECISION], record)
            append_jsonl(paths[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload: dict[str, str] = {
        "decision_id": decision_id,
        "scope_id": scope_id,
        "summary": summary,
        "status": "active",
    }
    if supersedes is not None:
        payload["supersedes"] = supersedes
    text = f"decision {decision_id} added" + (f" (supersedes {supersedes})" if supersedes else "")
    _emit(payload, text, flags)


@decision_app.command("supersede")
def decision_supersede(
    ctx: typer.Context,
    old_id: Annotated[str, typer.Argument(help="ID of the ACTIVE decision to retire")],
    new_id: Annotated[
        str,
        typer.Option("--by", help="ID of the decision that supersedes <old_id>"),
    ],
) -> None:
    """Supersede an existing decision by another existing decision."""
    from eawf.cli._mutation import state_transaction
    from eawf.evidence import decision as decision_evi
    from eawf.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            record, event = decision_evi.supersede_decision(
                state,
                old_id=old_id,
                new_id=new_id,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.DECISION], record)
            append_jsonl(paths[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "decision_id": old_id,
            "superseded_by": new_id,
            "status": "superseded",
        },
        f"decision {old_id} superseded by {new_id}",
        flags,
    )


@decision_app.command("list")
def decision_list(
    ctx: typer.Context,
    scope_id: Annotated[str | None, typer.Option("--scope-id", help="Filter by scope")] = None,
) -> None:
    """List decisions filtered by scope."""
    from eawf.evidence import decision as decision_evi
    from eawf.evidence._io import load_state

    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    items = decision_evi.list_decisions(state, scope_id=scope_id)

    payload = {
        "decisions": [
            {
                "id": d.id,
                "scope_id": d.scope_id,
                "summary": d.title,
                "status": d.status.value,
            }
            for d in items
        ]
    }
    text = "\n".join(f"{d.id}\t{d.status.value}\t{d.title}" for d in items) or "(none)"
    _emit(payload, text, flags)


class _DecisionGraphFormat(StrEnum):
    """Output format selector for ``decision graph --format``."""

    TEXT = "text"
    DOT = "dot"
    MERMAID = "mermaid"


@decision_app.command("graph")
def decision_graph(
    ctx: typer.Context,
    fmt: Annotated[
        _DecisionGraphFormat,
        typer.Option(
            "--format",
            help="Output format (text/dot/mermaid).",
        ),
    ] = _DecisionGraphFormat.TEXT,
) -> None:
    """Render the decision graph (text, Graphviz DOT, or Mermaid)."""
    from eawf.evidence._io import load_state
    from eawf.render.decision_graph import (
        build_decision_graph,
        render_dot,
        render_mermaid,
        render_text,
    )

    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    graph = build_decision_graph(state)
    if fmt is _DecisionGraphFormat.TEXT:
        body = render_text(graph)
    elif fmt is _DecisionGraphFormat.DOT:
        body = render_dot(graph)
    else:
        body = render_mermaid(graph)
    payload = {
        "format": fmt.value,
        "nodes": [n.model_dump() for n in graph.nodes],
        "edges": [e.model_dump() for e in graph.edges],
        "body": body,
    }
    _emit(payload, body, flags)
