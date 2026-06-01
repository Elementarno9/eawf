"""Audit / backlog command handlers.

Split out of :mod:`eawf.surfaces.cli.commands.evidence` (P27-W07). The
``audit_app`` / ``backlog_app`` Typer apps and the shared helpers live in
the parent module; this module attaches the command bodies via
``@<app>.command(...)``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError as PydanticValidationError

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    BacklogPriority,
    StoreKind,
)
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.evidence import (
    _emit,
    _flags,
    _run_read,
    _state_path,
    audit_app,
    backlog_app,
)

logger = logging.getLogger(__name__)


# ---- audit -----------------------------------------------------------------


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
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import audit as audit_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

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
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import audit as audit_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        if checks is not None and fixture is not None:
            raise cli_errors.UserError(
                "audit run accepts --fixture OR --checks, not both", kind="InvalidInput"
            )

        check_results_payload: list[dict[str, Any]] | None = None
        if checks is not None:
            from eawf.workflow.audit_dsl.runner import load_spec, run_checks

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
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import audit as audit_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

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
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import audit as audit_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

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
    from eawf.surfaces.render.audit_report import render_audit_markdown
    from eawf.workflow.evidence import audit as audit_evi
    from eawf.workflow.evidence._io import load_state

    flags = _flags(ctx)
    state_path = _state_path(flags)
    state = _run_read(flags, load_state, state_path)

    audit = _run_read(flags, audit_evi.show_audit, state, audit_id)
    if md:
        if flags.json_output:
            cli_errors.emit_error(
                cli_errors.UserError("--md and --json are contradictory", kind="InvalidInput"),
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
    from eawf.workflow.evidence import audit as audit_evi
    from eawf.workflow.evidence._io import load_state

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


# ---- backlog ---------------------------------------------------------------


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
    description: Annotated[
        str | None,
        typer.Option("--description", help="Long-form purpose (<=500 chars)."),
    ] = None,
) -> None:
    """Add a new backlog item."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import backlog as backlog_evi
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
            event = backlog_evi.add_backlog(
                state,
                item_id=item_id,
                title=title,
                priority=priority,
                scope_id=resolved_scope,
                description=description,
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
            "has_description": description is not None,
        },
        f"backlog {item_id} added priority={priority.value}",
        flags,
    )


@backlog_app.command("edit")
def backlog_edit(
    ctx: typer.Context,
    item_id: Annotated[str, typer.Argument(help="Backlog item id")],
    title: Annotated[
        str | None,
        typer.Option("--title", help="New short title (<=72 chars)."),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", help="New long-form purpose (<=500 chars)."),
    ] = None,
    intent_problem: Annotated[
        str | None,
        typer.Option(
            "--intent-problem",
            help=(
                "Required problem statement on the IntentBrief (<=200 chars). "
                "Passing any --intent-* flag activates the brief; both "
                "--intent-problem and --intent-desired-outcome are required "
                "when any --intent-* flag is present."
            ),
        ),
    ] = None,
    intent_desired_outcome: Annotated[
        str | None,
        typer.Option(
            "--intent-desired-outcome",
            help="Required desired-outcome statement on the IntentBrief (<=200 chars).",
        ),
    ] = None,
    intent_priority_rationale: Annotated[
        str | None,
        typer.Option(
            "--intent-priority-rationale",
            help="Optional priority rationale on the IntentBrief (<=1000 chars).",
        ),
    ] = None,
    intent_planned_steps: Annotated[
        str | None,
        typer.Option(
            "--intent-planned-steps",
            help=(
                "Comma-separated planner steps on the IntentBrief "
                "(max 10 entries, each <=500 chars)."
            ),
        ),
    ] = None,
    intent_risks: Annotated[
        str | None,
        typer.Option(
            "--intent-risks",
            help="Comma-separated risks on the IntentBrief (max 10 entries, each <=500 chars).",
        ),
    ] = None,
    intent_evidence_refs: Annotated[
        str | None,
        typer.Option("--intent-evidence-refs", help="Comma-separated evidence references."),
    ] = None,
    intent_source_brief_ids: Annotated[
        str | None,
        typer.Option("--intent-source-brief-ids", help="Comma-separated source brief ids."),
    ] = None,
    clear_intent: Annotated[
        bool,
        typer.Option("--clear-intent", help="Remove any attached IntentBrief."),
    ] = False,
) -> None:
    """Edit an open backlog item's title, description, and/or intent."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.surfaces.cli.commands.roadmap import _INTENT_FLAG_ERROR, _build_intent_from_flags
    from eawf.workflow.evidence import backlog as backlog_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)
    try:
        intent_result = _build_intent_from_flags(
            intent_problem=intent_problem,
            intent_desired_outcome=intent_desired_outcome,
            intent_priority_rationale=intent_priority_rationale,
            intent_planned_steps=intent_planned_steps,
            intent_risks=intent_risks,
            intent_evidence_refs=intent_evidence_refs,
            intent_source_brief_ids=intent_source_brief_ids,
        )
    except PydanticValidationError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid intent: {exc.errors()[0]['msg']}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if intent_result is _INTENT_FLAG_ERROR:
        cli_errors.emit_error(
            cli_errors.UserError(
                "--intent-problem and --intent-desired-outcome are required "
                "when any --intent-* flag is passed",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    intent = intent_result if isinstance(intent_result, IntentBrief) else None

    try:
        with state_transaction(state_path) as state:
            event = backlog_evi.edit_backlog(
                state,
                item_id=item_id,
                title=title,
                description=description,
                intent=intent,
                clear_intent=clear_intent,
            )
            append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    fields = sorted(
        f
        for f, changed in (
            ("title", title is not None),
            ("description", description is not None),
            ("intent", intent is not None or clear_intent),
        )
        if changed
    )
    _emit(
        {
            "item_id": item_id,
            "fields": fields,
        },
        f"backlog {item_id} edited fields={','.join(fields)}",
        flags,
    )


@backlog_app.command("backfill-titles")
def backlog_backfill_titles(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help=(
                "Persist normalized titles through the daemon-backed state "
                "transaction. Default --dry-run reports proposed changes and "
                "the title style-lint sweep without mutating state."
            ),
        ),
    ] = False,
) -> None:
    """Sweep + normalize backlog titles to the entity-title rule.

    The default ``--dry-run`` mode IS the read-only sweep: it walks every
    backlog item, runs the title style-lint, and reports the title each item
    *would* get (strip a trailing period, trim an over-cap title to a word
    boundary, derive a candidate from the description when the title is an empty
    placeholder) without touching state. ``--apply`` persists the normalized
    titles through the same state transaction the ``edit`` verb uses.
    """
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import backlog as backlog_evi
    from eawf.workflow.evidence._io import append_jsonl, load_state, store_paths

    flags = _flags(ctx)
    state_path = _state_path(flags)

    try:
        if apply:
            with state_transaction(state_path) as state:
                report, event = backlog_evi.backfill_titles(state, apply=True)
                if event is not None:
                    append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
        else:
            state = _run_read(flags, load_state, state_path)
            if state is None:
                return
            report, _ = backlog_evi.backfill_titles(state, apply=False)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload = {
        "applied": report.applied,
        "total": report.total,
        "changed": report.changed,
        "violations": report.violations,
        "rows": [
            {
                "item_id": row.item_id,
                "before": row.before,
                "after": row.after,
                "changed": row.changed,
                "violations": row.violations,
            }
            for row in report.rows
        ],
    }
    changed_lines = [
        f"  {row.item_id}: {row.before!r} -> {row.after!r}" for row in report.rows if row.changed
    ]
    violation_lines = [f"  {row.item_id}: {v}" for row in report.rows for v in row.violations]
    mode = "applied" if report.applied else "dry-run"
    headline = (
        f"backlog backfill-titles {mode}: {report.total} items, "
        f"{report.changed} title change(s), {report.violations} lint violation(s)"
    )
    body = "\n".join([headline, *changed_lines, *violation_lines])
    _emit(payload, body, flags)


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
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import backlog as backlog_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

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
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence import backlog as backlog_evi
    from eawf.workflow.evidence._io import append_jsonl, store_paths

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
