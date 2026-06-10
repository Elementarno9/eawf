"""Validation helpers for the ``spec.sync`` daemon method."""

from __future__ import annotations

from pathlib import Path
from typing import Final

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.spec.heuristics import is_ui_scope
from eawf.kernel.spec.intent import IntentBrief
from eawf.platform.lint.eawf021_measurable_criterion import (
    MeasurabilityViolation,
    check_criterion_spec,
)
from eawf.platform.lint.eawf022_propose_coverage import (
    CoverageGapViolation,
    missing_intent_finding,
    missing_planned_steps_finding,
)
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.workflow.propose.coverage import coverage_gaps, source_brief_coverage_gaps

_AFFORDANCE_PARITY_KIND: Final[str] = "affordance_parity"


def measure_criteria(criteria: list[CriterionSpec]) -> list[MeasurabilityViolation]:
    """Return every EAWF021 measurability finding across *criteria*."""
    findings: list[MeasurabilityViolation] = []
    for criterion in criteria:
        findings.extend(check_criterion_spec(criterion))
    return findings


def find_coverage_gaps(
    criteria: list[CriterionSpec],
    *,
    wave_id: str,
    intent: IntentBrief | None,
    repo_root: Path,
) -> list[CoverageGapViolation]:
    """Return every EAWF022 coverage gap of a wave's brief detail by *criteria*."""
    if intent is None:
        return [missing_intent_finding(wave_id)]
    findings: list[CoverageGapViolation] = []
    if intent.is_required_intent and not intent.planned_steps:
        findings.append(missing_planned_steps_finding(wave_id))
    findings += coverage_gaps(criteria, planned_steps=list(intent.planned_steps))
    findings += source_brief_coverage_gaps(criteria, intent=intent, repo_root=repo_root)
    return findings


def render_lint_findings(
    measurability: list[MeasurabilityViolation],
    coverage: list[CoverageGapViolation],
) -> str:
    """Render a combined ``validation_failed`` message for lint findings."""
    bodies = [v.render() for v in measurability] + [v.render() for v in coverage]
    return "validation_failed: spec sync lint findings: " + "; ".join(bodies)


def require_affordance_parity_for_ui_scope(
    *,
    wave_id: str,
    file_scopes: list[str],
    gates: list[GateSpec],
) -> None:
    """Reject a UI-scope wave whose synced gates omit an affordance_parity gate."""
    if not is_ui_scope(file_scopes):
        return
    if any(gate.kind == _AFFORDANCE_PARITY_KIND for gate in gates):
        return
    raise DaemonValidationError(
        f"validation_failed: ui-scope wave {wave_id!r} requires an "
        f"{_AFFORDANCE_PARITY_KIND} gate; none found in synced gates"
    )
