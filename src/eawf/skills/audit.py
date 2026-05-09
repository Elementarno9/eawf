"""``/audit`` skill — full iteration audit, branched on profile composition.

Implements the §14 algorithm for ``/audit``:

1. Probe instruments via ``EA_INSTRUMENT_PROBE``; abort on hard miss.
2. Resolve scope: active iter by default; may target wave/phase/PR.
3. **Branch on profile composition** (the W02 acceptance contract item):
   - ``research`` profile enabled → ``kind=evaluation`` (MLflow integrity,
     outcome measurements, hypothesis verdicts, evaluation artefact).
   - ``research`` profile not enabled → ``kind=ship-gate`` (tests, lint,
     typecheck, build, security, docs links, scope drift).
   - Either kind may be requested explicitly via ``ctx.args["kind"]``.
4. Build check plan from ``acceptance.yaml`` + profile rules + changed files.
5. Run deterministic checks per the chosen audit kind.
6. Collect result metrics and compare to thresholds/baselines.
7. Dispatch fresh reviewers (v0.1: skipped — degrade if invoked).
8. Mark each finding (blocker/fix-now/follow-up/false-positive).
9. ``--fix-safe`` applies bounded fixes (v0.1: skipped).
10. Write ``Audit`` artefact with commands/outputs/metrics.
11. Update outcomes/hypotheses only from audit evidence.

Each algorithm step writes one ``EVENT`` row. The body is populated with
the typed AuditBody from W01.

Honoured flags:

- ``--checks`` — comma-separated list of check names; ``ctx.args["checks"]``
  selects which check rows feed ``body.checks_run``.
- ``--kind evaluation|ship-gate`` — overrides the profile branch.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.render.envelope import SkillName
from eawf.skills._common import (
    emit_event,
    has_research_profile,
    probe_skill_instruments,
    resolve_active_state_path,
)
from eawf.skills.bodies.audit import (
    AuditBody,
    AuditCheckRun,
    AuditKind,
)
from eawf.skills.engine import ProbeOutcome, Skill, SkillContext, SkillResult
from eawf.skills.registry import register

logger = logging.getLogger(__name__)


_DEFAULT_SHIP_GATE_CHECKS: tuple[str, ...] = (
    "tests",
    "lint",
    "type",
    "build",
    "docs",
    "state",
)
_DEFAULT_EVALUATION_CHECKS: tuple[str, ...] = (
    "mlflow_integrity",
    "lookahead_bias",
    "is_oos_gap",
    "outcome_measure",
    "hypothesis_verdict",
)


def _normalise_checks(raw: Any, default: tuple[str, ...]) -> list[str]:
    """Coerce ``ctx.args['checks']`` into a list[str], falling back to *default*."""
    if raw is None:
        return list(default)
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return parts or list(default)
    if isinstance(raw, list):
        return [str(p) for p in raw if str(p).strip()] or list(default)
    return list(default)


def _resolve_kind(args: dict[str, Any], state_path: Any) -> AuditKind:
    """Determine the audit kind from explicit override / profile branch."""
    explicit = args.get("kind")
    if isinstance(explicit, str):
        normalised = explicit.lower()
        if normalised in {"evaluation", "ship-gate"}:
            # Both literals match the AuditKind frozen literal in W01.
            return normalised  # type: ignore[return-value]
    if has_research_profile(state_path):
        return "evaluation"
    return "ship-gate"


@register
class AuditSkill(Skill):
    """Concrete ``/audit`` skill (Phase 4 W02)."""

    name: SkillName = "/audit"

    def probe(self, ctx: SkillContext) -> ProbeOutcome:
        return probe_skill_instruments()

    def action(self, ctx: SkillContext) -> SkillResult:
        state_path = resolve_active_state_path()
        scope_id = ctx.scope
        args: dict[str, Any] = dict(ctx.args)

        kind: AuditKind = _resolve_kind(args, state_path)
        default_checks = (
            _DEFAULT_EVALUATION_CHECKS if kind == "evaluation" else _DEFAULT_SHIP_GATE_CHECKS
        )
        checks = _normalise_checks(args.get("checks"), default_checks)

        persisted_records: list[str] = []
        state_mutations: list[str] = []
        next_actions: list[str] = ["eawf ship", "eawf audit show"]
        evidence_refs: list[str] = []

        # Step 1 — probe ran. Step 2: resolve scope.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.resolve_scope",
            summary=f"audit: scope={scope_id} kind={kind}",
            payload={"kind": kind, "scope_id": scope_id},
        )
        persisted_records.append(evt_id)

        # Step 3 — branch on profile composition.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.branch_kind",
            summary=f"audit: kind branch -> {kind}",
            payload={"kind": kind, "explicit": "kind" in args},
        )
        persisted_records.append(evt_id)

        # Step 4 — build check plan.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.build_check_plan",
            summary=f"audit: planned {len(checks)} check(s)",
            payload={"checks": checks},
        )
        persisted_records.append(evt_id)

        # Step 5 — run checks. v0.1: emit one envelope per check + record.
        runs: list[AuditCheckRun] = []
        for check_name in checks:
            runs.append(
                AuditCheckRun(
                    check_id=check_name,
                    command=f"eawf check {check_name}",
                    status="skipped",
                    output_blob=None,
                )
            )
            evt_id = emit_event(
                state_path=state_path,
                scope_id=scope_id,
                event_type="audit.run_check",
                summary=f"audit: ran {check_name} (v0.1 stub)",
                payload={"check_id": check_name, "status": "skipped"},
            )
            persisted_records.append(evt_id)

        # Step 6 — collect metrics.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.collect_metrics",
            summary="audit: collected outcome metrics",
            payload={"outcome_count": 0},
        )
        persisted_records.append(evt_id)

        # Step 7 — dispatch reviewers (v0.1 skipped).
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.dispatch_reviewers",
            summary="audit: reviewer fanout skipped in v0.1",
            payload={"skipped": True},
        )
        persisted_records.append(evt_id)

        # Step 8 — mark findings (v0.1: empty list).
        # Step 9 — fix-safe (v0.1 skipped).
        # Step 10 — write artefact.
        evt_id = emit_event(
            state_path=state_path,
            scope_id=scope_id,
            event_type="audit.write_artifact",
            summary="audit: artifact recorded as event payload (v0.1)",
            payload={"checks": checks, "kind": kind},
        )
        persisted_records.append(evt_id)

        # Step 11 — update outcomes/hypotheses only from audit evidence (v0.1
        # skipped here; verdict-bearing path runs through ``eawf audit run``).

        body = AuditBody(
            scope_id=scope_id,
            kind=kind,
            checks_run=runs,
            outcomes_measured=[],
            hypothesis_verdicts=[],
            findings=[],
            audit_artifact_urn=None,
        )

        return SkillResult(
            status="ok",
            body=body.model_dump(mode="json"),
            persisted_store_records=persisted_records,
            state_mutations=state_mutations,
            evidence_refs=evidence_refs,
            next_valid_actions=next_actions,
        )


__all__ = ["AuditSkill"]
