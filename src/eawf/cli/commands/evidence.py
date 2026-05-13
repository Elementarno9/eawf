"""Typer sub-apps for the evidence-area commands (W02 deliverable).

Every state-mutating handler runs inside
:func:`eawf.cli._mutation.state_transaction`, which holds
``portalock(state.json)`` across the load + mutate + validate + write
cycle. Library mutators (``define_*`` / ``add_*`` / ``set_*`` /
``verdict_*`` / ``close_*``) take the typed :class:`State` and mutate
it in place, returning the JSONL envelope(s) for the handler to append
after the transaction body completes.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.artifacts.validation import validate_markdown_artifact
from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.cli.commands.draft import install_promote_command
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.evidence import (
    artifact as artifact_evi,
)
from eawf.evidence import (
    audit as audit_evi,
)
from eawf.evidence import (
    backlog as backlog_evi,
)
from eawf.evidence import (
    decision as decision_evi,
)
from eawf.evidence import (
    goal as goal_evi,
)
from eawf.evidence import (
    hypothesis as hypothesis_evi,
)
from eawf.evidence import (
    incident as incident_evi,
)
from eawf.evidence import (
    outcome as outcome_evi,
)
from eawf.evidence._io import append_jsonl, load_state, store_paths
from eawf.render.audit_report import render_audit_markdown
from eawf.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    BacklogPriority,
    HypothesisStatus,
    HypothesisVerdict,
    IncidentSeverity,
    OutcomeDirection,
    OutcomeStatus,
    StoreKind,
)

logger = logging.getLogger(__name__)


# ---- Helpers ---------------------------------------------------------------


def _flags(ctx: typer.Context) -> GlobalFlags:
    """Return the resolved :class:`GlobalFlags` from the Typer context."""
    flags = ctx.obj
    if not isinstance(flags, GlobalFlags):
        flags = GlobalFlags()
    return flags


def _state_path(flags: GlobalFlags) -> Path:
    """Resolve the state path or raise :class:`NotFound`."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        raise cli_errors.NotFound(str(exc)) from exc


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


# ---- goal ------------------------------------------------------------------

goal_app = typer.Typer(
    name="goal",
    help="Manage project goals (define).",
    no_args_is_help=True,
)


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
    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            resolved_scope = scope_id
            if resolved_scope is None:
                if state.project is None:
                    raise cli_errors.InvalidInput("scope_id required when state.project is unset")
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

outcome_app = typer.Typer(
    name="outcome",
    help="Manage outcomes (define / set).",
    no_args_is_help=True,
)


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

hypothesis_app = typer.Typer(
    name="hypothesis",
    help="Manage hypotheses (define / verdict / list).",
    no_args_is_help=True,
)


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


# ---- audit -----------------------------------------------------------------

audit_app = typer.Typer(
    name="audit",
    help="Manage audits (add / run / integrity / show / list).",
    no_args_is_help=True,
)


@audit_app.command("add")
def audit_add(
    ctx: typer.Context,
    audit_id: Annotated[str, typer.Argument(help="Audit id")],
    scope_id: Annotated[str, typer.Option("--scope-id", help="Owning scope id")],
    kind: Annotated[
        AuditKind,
        typer.Option("--kind", help="evaluation / ship-gate / incident / review"),
    ] = AuditKind.EVALUATION,
    report: Annotated[
        str | None,
        typer.Option("--report", help="Report artifact id"),
    ] = None,
    verdict: Annotated[
        AuditVerdict | None,
        typer.Option("--verdict", help="pass / minor / major"),
    ] = None,
) -> None:
    """Register an audit; report-bearing audits land status=complete."""
    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            record, event = audit_evi.add_audit(
                state,
                audit_id=audit_id,
                scope_id=scope_id,
                kind=kind,
                report_artifact_id=report,
                verdict=verdict,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.AUDIT], record)
            append_jsonl(paths[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    status = "complete" if report is not None else "pending"
    _emit(
        {
            "audit_id": audit_id,
            "scope_id": scope_id,
            "kind": kind.value,
            "status": status,
            "verdict": verdict.value if verdict else None,
        },
        f"audit {audit_id} added status={status}",
        flags,
    )


@audit_app.command("run")
def audit_run(
    ctx: typer.Context,
    audit_id: Annotated[str, typer.Argument(help="Audit id")],
    scope_id: Annotated[str, typer.Option("--scope-id", help="Owning scope id")],
    kind: Annotated[
        AuditKind,
        typer.Option("--kind", help="evaluation / ship-gate / incident / review"),
    ] = AuditKind.EVALUATION,
    fixture: Annotated[
        Path | None,
        typer.Option(
            "--fixture",
            help="JSON fixture of check_results (legacy Phase 2 escape hatch).",
        ),
    ] = None,
    checks: Annotated[
        Path | None,
        typer.Option(
            "--checks",
            help="YAML spec for the audit-check DSL (v0.2; B019).",
        ),
    ] = None,
) -> None:
    """Run an audit. ``--checks`` drives the DSL runner; ``--fixture`` is the
    legacy JSON escape hatch. Pass at most one of the two.
    """
    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        if checks is not None and fixture is not None:
            raise cli_errors.InvalidInput("audit run accepts --fixture OR --checks, not both")

        check_results_payload: list[dict[str, Any]] | None = None
        if checks is not None:
            from eawf.audit_dsl.runner import load_spec, run_checks

            specs = load_spec(checks)
            results = run_checks(specs, cwd=state_path.parent.parent)
            check_results_payload = [
                {"name": r.name, "passed": r.passed, "details": r.details} for r in results
            ]

        with state_transaction(state_path) as state:
            record, event = audit_evi.run_audit(
                state,
                audit_id=audit_id,
                scope_id=scope_id,
                kind=kind,
                fixture_path=fixture,
                check_results=check_results_payload,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.AUDIT], record)
            append_jsonl(paths[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "audit_id": audit_id,
            "scope_id": scope_id,
            "kind": kind.value,
            "status": "complete",
            "fixture_path": str(fixture) if fixture else None,
            "checks_path": str(checks) if checks else None,
        },
        f"audit {audit_id} run complete",
        flags,
    )


@audit_app.command("integrity")
def audit_integrity(
    ctx: typer.Context,
    audit_id: Annotated[str, typer.Argument(help="Audit id")],
    check: Annotated[str, typer.Option("--check", help="Integrity check name")],
    status: Annotated[
        str,
        typer.Option(
            "--status",
            help="passed / failed (free-form for v0.1)",
        ),
    ] = "passed",
    details: Annotated[
        str | None,
        typer.Option("--details", help="Optional details string"),
    ] = None,
) -> None:
    """Append an integrity-check result to an existing audit."""
    flags = _flags(ctx)
    state_path = _state_path(flags)
    passed = status.lower() == "passed"

    try:
        with state_transaction(state_path) as state:
            record, event = audit_evi.add_integrity(
                state,
                audit_id=audit_id,
                check=check,
                passed=passed,
                details=details,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.AUDIT], record)
            append_jsonl(paths[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "audit_id": audit_id,
            "check": check,
            "passed": passed,
            "details": details,
        },
        f"audit {audit_id} integrity {check} passed={passed}",
        flags,
    )


@audit_app.command("set-verdict")
def audit_set_verdict(
    ctx: typer.Context,
    audit_id: Annotated[str, typer.Argument(help="Audit id")],
    verdict: Annotated[
        AuditVerdict,
        typer.Option("--verdict", help="pass / minor / major"),
    ],
    report: Annotated[
        str | None,
        typer.Option(
            "--report",
            help="Report artifact id (required to lift a pending audit to complete).",
        ),
    ] = None,
) -> None:
    """Stamp a verdict on an existing audit.

    A pending audit lifts to status=complete only when ``--report`` points at
    an existing artifact; a complete audit accepts ``--verdict`` updates
    in place but rejects a differing ``--report``.
    """
    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            record, event = audit_evi.set_verdict(
                state,
                audit_id=audit_id,
                verdict=verdict,
                report_artifact_id=report,
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.AUDIT], record)
            append_jsonl(paths[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "audit_id": audit_id,
            "verdict": verdict.value,
            "report_artifact_id": report,
        },
        f"audit {audit_id} verdict={verdict.value}",
        flags,
    )


@audit_app.command("show")
def audit_show(
    ctx: typer.Context,
    audit_id: Annotated[str, typer.Argument(help="Audit id")],
    md: Annotated[bool, typer.Option("--md", help="Render markdown artifact body.")] = False,
) -> None:
    """Show metadata for one audit."""
    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)

    audit = _run_read(flags, audit_evi.show_audit, state, audit_id)
    if md:
        if flags.json_output:
            cli_errors.emit_error(
                cli_errors.InvalidInput("--md and --json are contradictory"),
                flags=flags,
            )
            return
        typer.echo(render_audit_markdown(audit), nl=False)
        return
    payload = json.loads(audit.model_dump_json())
    _emit(
        payload,
        (
            f"audit {audit.id} kind={audit.kind.value} status={audit.status.value} "
            f"verdict={audit.verdict.value if audit.verdict else 'n/a'}"
        ),
        flags,
    )


@audit_app.command("list")
def audit_list(
    ctx: typer.Context,
    scope_id: Annotated[str | None, typer.Option("--scope-id", help="Filter by scope")] = None,
    kind: Annotated[AuditKind | None, typer.Option("--kind", help="Filter by kind")] = None,
    status: Annotated[AuditStatus | None, typer.Option("--status", help="Filter by status")] = None,
) -> None:
    """List audits with optional filters."""
    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    items = audit_evi.list_audits(state, scope_id=scope_id, kind=kind, status=status)

    payload = {
        "audits": [
            {
                "id": a.id,
                "scope_id": a.scope_id,
                "kind": a.kind.value,
                "status": a.status.value,
                "verdict": a.verdict.value if a.verdict else None,
            }
            for a in items
        ]
    }
    text = (
        "\n".join(
            f"{a.id}\t{a.kind.value}\t{a.status.value}\t{a.verdict.value if a.verdict else '-'}"
            for a in items
        )
        or "(none)"
    )
    _emit(payload, text, flags)


# ---- incident --------------------------------------------------------------

incident_app = typer.Typer(
    name="incident",
    help="Manage incidents (open / close / view).",
    no_args_is_help=True,
)


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

decision_app = typer.Typer(
    name="decision",
    help="Manage decisions (add / list).",
    no_args_is_help=True,
)


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
) -> None:
    """Record a durable decision."""
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
            )
            paths = store_paths(state_path)
            append_jsonl(paths[StoreKind.DECISION], record)
            append_jsonl(paths[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "decision_id": decision_id,
            "scope_id": scope_id,
            "summary": summary,
            "status": "active",
        },
        f"decision {decision_id} added",
        flags,
    )


@decision_app.command("list")
def decision_list(
    ctx: typer.Context,
    scope_id: Annotated[str | None, typer.Option("--scope-id", help="Filter by scope")] = None,
) -> None:
    """List decisions filtered by scope."""
    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    items = decision_evi.list_decisions(state, scope_id=scope_id)

    payload = {
        "decisions": [
            {
                "id": d.id,
                "scope_id": d.scope_id,
                "summary": d.summary,
                "status": d.status.value,
            }
            for d in items
        ]
    }
    text = "\n".join(f"{d.id}\t{d.status.value}\t{d.summary}" for d in items) or "(none)"
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


# ---- artifact --------------------------------------------------------------

artifact_app = typer.Typer(
    name="artifact",
    help="Manage artifacts (add / show).",
    no_args_is_help=True,
)


@artifact_app.command("add")
def artifact_add(
    ctx: typer.Context,
    artifact_id: Annotated[str, typer.Argument(help="Artifact id")],
    kind: Annotated[str, typer.Option("--kind", help="Artifact kind, e.g. audit_report")],
    uri: Annotated[str, typer.Option("--uri", help="Artifact URI (repo:... or remote URI)")],
    sha256: Annotated[str | None, typer.Option("--sha256", help="Optional SHA-256 hash")] = None,
    size: Annotated[int | None, typer.Option("--size", help="Optional size in bytes")] = None,
    scope_id: Annotated[
        str | None,
        typer.Option(
            "--scope-id",
            help="Owning scope (defaults to project code).",
        ),
    ] = None,
) -> None:
    """Register a durable artifact."""
    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            resolved_scope = scope_id
            if resolved_scope is None:
                if state.project is None:
                    raise cli_errors.InvalidInput("scope_id required when state.project is unset")
                resolved_scope = state.project.code
            event = artifact_evi.add_artifact(
                state,
                artifact_id=artifact_id,
                kind=kind,
                uri=uri,
                scope_id=resolved_scope,
                sha256=sha256,
                size_bytes=size,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "artifact_id": artifact_id,
            "kind": kind,
            "uri": uri,
            "sha256": sha256,
            "scope_id": resolved_scope,
        },
        f"artifact {artifact_id} added kind={kind}",
        flags,
    )


@artifact_app.command("show")
def artifact_show(
    ctx: typer.Context,
    artifact_id: Annotated[str, typer.Argument(help="Artifact id")],
) -> None:
    """Show artifact metadata."""
    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)
    artifact = _run_read(flags, artifact_evi.show_artifact, state, artifact_id)
    payload = json.loads(artifact.model_dump_json())
    _emit(
        payload,
        f"artifact {artifact.id} kind={artifact.kind} uri={artifact.uri}",
        flags,
    )


@artifact_app.command("validate")
def artifact_validate(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="Markdown artifact path.")],
) -> None:
    """Validate one markdown artifact body."""
    flags = _flags(ctx)
    text = path.read_text(encoding="utf-8")
    report = validate_markdown_artifact(text)
    payload = {"ok": report.ok, "errors": report.errors}
    if not report.ok:
        _emit(payload, "\n".join(report.errors), flags)
        raise typer.Exit(code=4)
    _emit(payload, "artifact validate: ok", flags)


# ---- backlog ---------------------------------------------------------------

backlog_app = typer.Typer(
    name="backlog",
    help="Manage backlog items (add / close).",
    no_args_is_help=True,
)


@backlog_app.command("add")
def backlog_add(
    ctx: typer.Context,
    item_id: Annotated[str, typer.Argument(help="Backlog item id (e.g. B023)")],
    title: Annotated[str, typer.Option("--title", help="Short title")],
    priority: Annotated[
        BacklogPriority,
        typer.Option("--priority", help="P0 / P1 / P2 / P3"),
    ] = BacklogPriority.P2,
    scope_id: Annotated[
        str | None,
        typer.Option(
            "--scope-id",
            help="Owning scope (defaults to project code).",
        ),
    ] = None,
) -> None:
    """Add a new backlog item."""
    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            resolved_scope = scope_id
            if resolved_scope is None:
                if state.project is None:
                    raise cli_errors.InvalidInput("scope_id required when state.project is unset")
                resolved_scope = state.project.code
            event = backlog_evi.add_backlog(
                state,
                item_id=item_id,
                title=title,
                priority=priority,
                scope_id=resolved_scope,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "item_id": item_id,
            "title": title,
            "priority": priority.value,
            "scope_id": resolved_scope,
            "status": "open",
        },
        f"backlog {item_id} added priority={priority.value}",
        flags,
    )


@backlog_app.command("set-priority")
def backlog_set_priority(
    ctx: typer.Context,
    item_id: Annotated[str, typer.Argument(help="Backlog item id")],
    priority: Annotated[
        BacklogPriority,
        typer.Option("--priority", help="P0 / P1 / P2 / P3"),
    ],
) -> None:
    """Update the priority of an open backlog item."""
    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            event = backlog_evi.set_priority(
                state,
                item_id=item_id,
                priority=priority,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "item_id": item_id,
            "priority": priority.value,
        },
        f"backlog {item_id} priority={priority.value}",
        flags,
    )


@backlog_app.command("close")
def backlog_close(
    ctx: typer.Context,
    item_id: Annotated[str, typer.Argument(help="Backlog item id")],
    resolution: Annotated[str, typer.Option("--resolution", help="Resolution text")],
    commit: Annotated[str, typer.Option("--commit", help="Resolving commit sha")],
    audit: Annotated[
        str,
        typer.Option(
            "--audit",
            help="Audit id (must reference a complete audit)",
        ),
    ],
) -> None:
    """Close a backlog item; requires --audit of a complete audit."""
    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        with state_transaction(state_path) as state:
            event = backlog_evi.close_backlog(
                state,
                item_id=item_id,
                resolution=resolution,
                commit=commit,
                audit_id=audit,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    _emit(
        {
            "item_id": item_id,
            "resolution": resolution,
            "commit": commit,
            "audit_id": audit,
            "status": "closed",
        },
        f"backlog {item_id} closed",
        flags,
    )


install_promote_command(audit_app, "audit")
install_promote_command(hypothesis_app, "hypothesis")
install_promote_command(decision_app, "decision")
install_promote_command(incident_app, "incident")

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
